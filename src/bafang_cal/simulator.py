"""A software stand-in for a Bafang G510 (M620) controller's UART port.

Used by ``bafang-cal --simulate`` (to try the tool without hardware) and by the
test suite. The *protocol* side is modelled on the C961 calibration display's
firmware: request framing, checksums, reply formats and the 1200 -> 9600 baud
switch. The *controller* side is a simplified model:

* the calibration reports the status sequence from Bafang's instructions
  (40, 41, 43, 95, 96, 97, 98, then 44 = finished), spread over
  ``calibration_seconds``, and the motor "turns" while it runs;
* it only starts when the controller has been told a non-zero assist level
  first, as users report for real controllers.

Speeds, timings and the exact moments the codes change are invented. Do not
read anything about real controllers into the simulated values.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .codes import CALIBRATION_DONE
from .protocol import NORMAL_BAUDRATE, Command, FrameType
from .transport import Clock, SystemClock

__all__ = ["ManualClock", "ReceivedFrame", "SimulatedController", "SimulatorConfig"]

#: Length of a ``0x16`` write frame (including type, command and checksum)
#: per command byte. ``LIGHTS`` has no checksum.
_WRITE_LENGTHS: dict[int, int] = {
    Command.ASSIST_LEVEL: 4,
    Command.LIGHTS: 3,
    Command.SPEED_LIMIT: 5,
    Command.MOTOR_TEST: 4,
    Command.CALIBRATE: 4,
}

#: Status codes shown while calibrating, in order (Bafang's instructions).
_CALIBRATION_STEPS = (0x40, 0x41, 0x43, 0x95, 0x96, 0x97, 0x98)

#: The controller keeps running the motor test this long after the last command.
_MOTOR_TEST_HOLD = 0.30


class ManualClock:
    """A :class:`~bafang_cal.transport.Clock` that only moves when told to."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.now += seconds

    def advance(self, seconds: float) -> None:
        self.sleep(seconds)


@dataclass
class SimulatorConfig:
    """Knobs for the simulated controller."""

    #: Answer requests at all (``False`` simulates a wiring problem).
    responsive: bool = True
    #: Switch to the requested baud rate when ``11 51 <baud>`` arrives.
    honour_baud_request: bool = True
    #: Status byte reported by ``11 08`` when not calibrating.
    status: int = 0x01
    #: Battery percentage reported by ``11 11``.
    battery_percent: int = 87
    #: Temperature in °C reported by ``11 12``.
    temperature: int = 31
    #: React to the calibration command at all (``False``: unsupported controller).
    supports_calibration: bool = True
    #: Only start calibrating after a non-zero assist level was received.
    requires_assist: bool = True
    #: Seconds from the first calibration command to the final status.
    calibration_seconds: float = 7.0
    #: Final status of the calibration: 0x44 (finished) or a failure code.
    calibration_result: int = CALIBRATION_DONE
    #: Speed value reported while the simulated calibration turns the motor.
    calibration_rpm: int = 45


@dataclass(frozen=True)
class ReceivedFrame:
    """A well-formed frame as seen by the simulated controller."""

    time: float
    baudrate: int
    data: bytes


@dataclass
class _State:
    baudrate: int = NORMAL_BAUDRATE
    assist_code: int = 0
    calibration_started: float | None = None
    last_motor_command: float | None = None
    motor_percent: int = 0
    garbled_bytes: int = 0
    frames: list[ReceivedFrame] = field(default_factory=list)


class SimulatedController:
    """Implements the :class:`~bafang_cal.transport.Transport` interface.

    The *host* side (the tool) and the *controller* side each have their own
    baud rate; bytes written while they differ are counted as garbage and
    dropped, exactly like on a real wire.
    """

    def __init__(self, clock: Clock | None = None, config: SimulatorConfig | None = None) -> None:
        self.clock = clock or SystemClock()
        self.config = config or SimulatorConfig()
        self._host_baudrate = NORMAL_BAUDRATE
        self._state = _State()
        self._to_host = bytearray()
        self.closed = False

    # ------------------------------------------------------------ inspection

    @property
    def controller_baudrate(self) -> int:
        return self._state.baudrate

    @property
    def frames(self) -> list[ReceivedFrame]:
        """Every well-formed frame the controller received, oldest first."""
        return self._state.frames

    @property
    def garbled_bytes(self) -> int:
        """Bytes lost because host and controller baud rates differed."""
        return self._state.garbled_bytes

    @property
    def assist_code(self) -> int:
        """Last assist-level code received (``16 0B``)."""
        return self._state.assist_code

    @property
    def calibrating(self) -> bool:
        """True between the first calibration command and the final status."""
        progress = self._calibration_progress()
        return progress is not None and progress[0] in _CALIBRATION_STEPS

    def inject(self, data: bytes) -> None:
        """Queue bytes for the host as if the controller sent them unprompted."""
        self._to_host.extend(data)

    # ------------------------------------------------------------- Transport

    @property
    def baudrate(self) -> int:
        return self._host_baudrate

    def set_baudrate(self, baudrate: int) -> None:
        self._host_baudrate = baudrate

    def write(self, data: bytes) -> None:
        if self.closed:
            raise OSError("simulated port is closed")
        if self._host_baudrate != self._state.baudrate:
            self._state.garbled_bytes += len(data)
            return
        self._consume(bytes(data))

    def wait_sent(self) -> None:
        return None

    def read(self, size: int, timeout: float) -> bytes:
        if len(self._to_host) < size:
            # A real read would block until the timeout expires.
            self.clock.sleep(timeout)
        chunk = bytes(self._to_host[:size])
        del self._to_host[:size]
        return chunk

    def drain(self) -> bytes:
        chunk = bytes(self._to_host)
        self._to_host.clear()
        return chunk

    def close(self) -> None:
        self.closed = True

    # ------------------------------------------------------- controller model

    def _consume(self, data: bytes) -> None:
        index = 0
        while index < len(data):
            # Once a baud-rate request has been honoured, the rest of this
            # write arrives at the old speed and is garbage for the controller.
            if self._host_baudrate != self._state.baudrate:
                self._state.garbled_bytes += len(data) - index
                return
            frame_type = data[index]
            if frame_type == FrameType.READ and index + 1 < len(data):
                length = 5 if data[index + 1] == Command.CONNECT else 2
            elif frame_type == FrameType.WRITE and index + 1 < len(data):
                length = _WRITE_LENGTHS.get(data[index + 1], 0)
            else:
                length = 0
            if length == 0 or index + length > len(data):
                self._state.garbled_bytes += 1
                index += 1
                continue
            self._handle(data[index : index + length])
            index += length

    @staticmethod
    def _checksum_ok(frame: bytes) -> bool:
        if frame[0] == FrameType.WRITE and frame[1] != Command.LIGHTS:
            return frame[-1] == sum(frame[:-1]) & 0xFF
        if frame[0] == FrameType.READ and frame[1] == Command.CONNECT:
            # The leading 0x11 is not part of the connect checksum.
            return frame[4] == sum(frame[1:4]) & 0xFF
        return True

    def _handle(self, frame: bytes) -> None:
        if not self._checksum_ok(frame):
            return
        self._state.frames.append(
            ReceivedFrame(self.clock.monotonic(), self._state.baudrate, bytes(frame))
        )
        if frame[0] == FrameType.READ:
            self._handle_read(frame)
        else:
            self._handle_write(frame)

    def _reply(self, data: bytes) -> None:
        if self.config.responsive:
            self._to_host.extend(data)

    def _handle_read(self, frame: bytes) -> None:
        command = frame[1]
        if command == Command.CONNECT:
            if self.config.honour_baud_request:
                self._state.baudrate = (frame[2] << 8) | frame[3]
            return
        if command == Command.STATUS:
            progress = self._calibration_progress()
            self._reply(bytes((self.config.status if progress is None else progress[0],)))
        elif command == Command.SPEED:
            rpm = self._current_rpm()
            hi, lo = rpm >> 8, rpm & 0xFF
            self._reply(bytes((hi, lo, (Command.SPEED + hi + lo) & 0xFF)))
        elif command == Command.BATTERY:
            pct = self.config.battery_percent & 0xFF
            self._reply(bytes((pct, pct)))
        elif command == Command.TEMPERATURE:
            raw = (self.config.temperature & 0xFFFF).to_bytes(2, "big")
            self._reply(raw + bytes(((raw[0] + raw[1]) & 0xFF,)))
        elif command == Command.MOVING:
            flag = 0x31 if self._current_rpm() else 0x30
            self._reply(bytes((flag, flag)))

    def _handle_write(self, frame: bytes) -> None:
        now = self.clock.monotonic()
        command = frame[1]
        if command == Command.ASSIST_LEVEL:
            self._state.assist_code = frame[2]
        elif command == Command.CALIBRATE and frame[2] == 0x01:
            ready = self.config.supports_calibration and (
                self._state.assist_code != 0 or not self.config.requires_assist
            )
            if ready and self._state.calibration_started is None:
                self._state.calibration_started = now
        elif command == Command.MOTOR_TEST:
            self._state.last_motor_command = now
            self._state.motor_percent = frame[2]

    def _calibration_progress(self) -> tuple[int, int] | None:
        """``(status, rpm)`` of the simulated calibration, ``None`` before it starts."""
        started = self._state.calibration_started
        if started is None:
            return None
        elapsed = self.clock.monotonic() - started
        duration = self.config.calibration_seconds
        if elapsed >= duration:
            return self.config.calibration_result, 0
        status = _CALIBRATION_STEPS[int(elapsed / duration * len(_CALIBRATION_STEPS))]
        return status, 0 if status == _CALIBRATION_STEPS[0] else self.config.calibration_rpm

    def _current_rpm(self) -> int:
        progress = self._calibration_progress()
        if progress is not None:
            return progress[1]
        last_motor = self._state.last_motor_command
        if last_motor is not None and self.clock.monotonic() - last_motor <= _MOTOR_TEST_HOLD:
            # Pretend 100 % is roughly 120 rpm at the wheel.
            return self._state.motor_percent * 120 // 100
        return 0
