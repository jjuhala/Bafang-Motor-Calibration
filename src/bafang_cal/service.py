"""Service routines that reproduce the C961 "G510 calibration" display.

The display firmware runs a 1 ms tick. In its service mode a tick counter
drives a cycle of time slots that wraps after 500 ticks. One detail stretches
it: when the speed reply is valid, the display shows the new speed, and that
routine (0x2E5B) also clears the tick counter (at 0x2F01). The counter is at 12
when that happens, so everything after the speed request runs 12 ms later and
the cycle lasts 512 ms. Without a valid speed reply it lasts 500 ms. This
module replays the slots with the same rule:

Calibration (display sub-mode 0, entered with *Power + Down* held):

=========== =========== ======================= ===============================
 t/ms        t/ms        request                 reply
 (reply)     (none)
=========== =========== ======================= ===============================
   0           0        ``11 20`` speed         ``hi lo chk`` (0x20 + hi + lo)
  32          20        ``11 08`` status        1 status byte
  75          63        ``16 A0 01 B7``         -- (ignored by the display)
 138         126        ``16 A0 01 B7``
 ...         ...        every 63 ms up to 453 / 441
 512         500        next cycle
=========== =========== ======================= ===============================

Motor test (display sub-mode 1, entered with *Power + Up* held); times shift by
12 ms in the same way:

====== =========================== ==========================================
 t/ms   request                     reply
====== =========================== ==========================================
   0    ``11 20`` speed             ``hi lo chk``
  64    ``16 06 <pct> <chk>``       -- (only while running), every 64 ms
  75    ``11 12`` temperature       ``hi lo chk`` (chk = hi + lo), signed °C
====== =========================== ==========================================

Before either cycle starts, :func:`enter_service_mode` sends
``11 51 25 80 F6`` three times at 1200 baud and switches to 9600 baud.

Before that, the display has been in its riding screen, which keeps telling the
controller its assist level (1 after power-up). Bafang users report that the
calibration does not start at assist level 0, so :func:`riding_schedule`
reproduces that riding screen too (1200 baud, 500 ms cycle):

====== ================================================ ===================
 t/ms   request                                          reply
====== ================================================ ===================
   0    ``16 0B 0C 2D`` ``16 1A F0`` ``16 1F 00 C8 FD``   --
 120    ``11 20`` speed                                  3 bytes
 200    ``11 08`` status                                 1 byte
 300    ``11 11`` battery                                2 bytes
 400    ``11 31`` moving                                 2 bytes
====== ================================================ ===================
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TypeAlias

from .protocol import (
    CALIBRATION_COMMAND,
    CALIBRATION_DISPLAY_ASSIST_CODES,
    CALIBRATION_DISPLAY_SPEED_LIMIT_RPM,
    NORMAL_BAUDRATE,
    SERVICE_BAUDRATE,
    SERVICE_MODE_REQUEST,
    SERVICE_MODE_REQUEST_REPEAT,
    SERVICE_MODE_SWITCH_DELAY,
    BatteryReply,
    Command,
    ProtocolError,
    SpeedReply,
    StatusReply,
    assist_level_command,
    lights_command,
    motor_test_command,
    parse_battery,
    parse_moving,
    parse_speed,
    parse_status,
    parse_temperature,
    read_request,
    speed_limit_command,
)
from .transport import Clock, SystemClock, Transport

__all__ = [
    "CYCLE_SECONDS",
    "SPEED_REPLY_RESTART",
    "Poll",
    "ProbeResult",
    "ScheduleRunner",
    "Send",
    "Slot",
    "Telemetry",
    "answers_at",
    "calibration_schedule",
    "enter_service_mode",
    "motor_test_schedule",
    "probe",
    "riding_schedule",
]

#: Length of one service-mode cycle (the display's 500-tick scheduler).
CYCLE_SECONDS = 0.500

#: Tick at which the display processes the ``11 20`` reply in service mode.
#: A valid reply restarts the tick counter there, delaying the rest of the
#: cycle by this much (0x2E5B clears XDATA 0x0017:0x0018 at 0x2F01).
SPEED_REPLY_RESTART = 0.012

#: Reads never wait less than this, even when the next slot is very close.
_MIN_READ_TIMEOUT = 0.002

#: Stop reading this long before the next slot is due.
_SLOT_GUARD = 0.001

#: Extra time allowed for a USB adapter to empty its FIFO before a baud change.
_TX_MARGIN = 0.025

#: A cycle that starts later than this re-bases the schedule (no catch-up burst).
_MAX_CYCLE_LATENESS = 0.100

#: Tolerance for float comparisons of schedule times.
_EPSILON = 1e-6


@dataclass(frozen=True)
class Poll:
    """Send a ``11 <cmd>`` read request and read a fixed-length reply.

    If ``restart_on_reply`` is set and the reply is valid, the rest of the
    current cycle is delayed by that many seconds -- the display's tick counter
    restart -- and the reply may arrive until just before the delayed next slot.
    """

    command: Command
    reply_length: int
    restart_on_reply: float = 0.0


@dataclass(frozen=True)
class Send:
    """Send a frame without expecting a reply."""

    frame: bytes


Action: TypeAlias = Poll | Send


@dataclass(frozen=True)
class Slot:
    """An action scheduled ``offset`` seconds after the start of every cycle."""

    offset: float
    action: Action


POLL_SPEED = Poll(Command.SPEED, 3)
#: In service mode a valid speed reply restarts the display's tick counter.
POLL_SERVICE_SPEED = Poll(Command.SPEED, 3, restart_on_reply=SPEED_REPLY_RESTART)
POLL_STATUS = Poll(Command.STATUS, 1)
POLL_TEMPERATURE = Poll(Command.TEMPERATURE, 3)
POLL_BATTERY = Poll(Command.BATTERY, 2)
POLL_MOVING = Poll(Command.MOVING, 2)


def riding_schedule(assist_level: int = 1) -> tuple[Slot, ...]:
    """Slots of the calibration display's riding screen (normal 1200 baud link).

    ``assist_level`` is the level on the display's 0-3 scale (1..3). The other
    values are the display's factory settings: lights off, 25 km/h on 26".
    """
    try:
        code = CALIBRATION_DISPLAY_ASSIST_CODES[assist_level]
    except KeyError:
        raise ValueError(f"assist level must be 1..3, got {assist_level}") from None
    settings = (
        assist_level_command(code)
        + lights_command(False)
        + speed_limit_command(CALIBRATION_DISPLAY_SPEED_LIMIT_RPM)
    )
    return (
        Slot(0.000, Send(settings)),
        Slot(0.120, POLL_SPEED),
        Slot(0.200, POLL_STATUS),
        Slot(0.300, POLL_BATTERY),
        Slot(0.400, POLL_MOVING),
    )


def calibration_schedule() -> tuple[Slot, ...]:
    """Slots of the display's calibration cycle (sub-mode 0)."""
    slots = [Slot(0.000, POLL_SERVICE_SPEED), Slot(0.020, POLL_STATUS)]
    # The display sends the command whenever (tick % 63) == 0, except at tick 0
    # which is taken by the speed request. Offsets are those of a cycle without
    # a speed reply; a valid reply delays everything after it by 12 ms.
    slots += [Slot(0.063 * k, Send(CALIBRATION_COMMAND)) for k in range(1, 8)]
    return tuple(sorted(slots, key=lambda s: s.offset))


def motor_test_schedule(level: int, *, running: bool = True) -> tuple[Slot, ...]:
    """Slots of the display's motor-test cycle (sub-mode 1).

    ``running`` mirrors the display's start/stop toggle (short press on Power):
    the ``16 06`` frames are only sent while running. Provided to document the
    protocol; the CLI deliberately does not use it (users report this mode
    overheating motors).
    """
    frame = motor_test_command(level)  # validates the level even when idle
    slots = [Slot(0.000, POLL_SERVICE_SPEED), Slot(0.075, POLL_TEMPERATURE)]
    if running:
        # Sent when (tick & 0x3F) == 0 on the low byte of the tick counter.
        slots += [Slot(0.064 * k, Send(frame)) for k in range(1, 8)]
    return tuple(sorted(slots, key=lambda s: s.offset))


@dataclass
class Telemetry:
    """Live counters and the latest decoded values of a service session."""

    elapsed: float = 0.0
    cycles: int = 0
    commands_sent: int = 0
    polls_sent: int = 0
    replies_ok: int = 0
    replies_missing: int = 0
    replies_invalid: int = 0
    rpm: int | None = None
    max_rpm: int = 0
    status: int | None = None
    statuses_seen: list[int] = field(default_factory=list)
    temperature: int | None = None
    battery: int | None = None
    moving: bool | None = None
    last_reply_elapsed: float | None = None
    interrupted: bool = False

    @property
    def got_replies(self) -> bool:
        return self.replies_ok > 0


class ScheduleRunner:
    """Executes a slot schedule cycle after cycle against a :class:`Transport`."""

    def __init__(
        self,
        transport: Transport,
        clock: Clock | None = None,
        *,
        cycle: float = CYCLE_SECONDS,
    ) -> None:
        self.transport = transport
        self.clock = clock or SystemClock()
        self.cycle = cycle

    def run(
        self,
        slots: Sequence[Slot],
        *,
        duration: float,
        telemetry: Telemetry | None = None,
        on_cycle: Callable[[Telemetry], None] | None = None,
        should_stop: Callable[[Telemetry], bool] | None = None,
        catch_interrupt: bool = True,
    ) -> Telemetry:
        """Run ``slots`` repeatedly for ``duration`` seconds.

        ``on_cycle`` and ``should_stop`` are called at the end of every cycle.
        Returns early when ``should_stop`` returns true or, if
        ``catch_interrupt`` is set, when the user presses Ctrl+C (the returned
        telemetry then has ``interrupted=True``). Passing an existing
        ``telemetry`` continues its counters and elapsed time.
        """
        if not slots:
            raise ValueError("schedule is empty")
        ordered = sorted(slots, key=lambda s: s.offset)
        if ordered[-1].offset >= self.cycle:
            raise ValueError("slot offsets must be shorter than the cycle")

        if telemetry is None:
            telemetry = Telemetry()
        start = self.clock.monotonic()
        base_elapsed = telemetry.elapsed
        # Times below are relative to ``start`` to keep float error small.
        anchor = 0.0
        try:
            while True:
                late = self.clock.monotonic() - start - anchor
                if late > _MAX_CYCLE_LATENESS:
                    # The host stalled: shift the schedule instead of bursting
                    # all missed frames at once.
                    anchor += late
                for index, slot in enumerate(ordered):
                    due = anchor + slot.offset
                    if due >= duration - _EPSILON:
                        self._sleep_until(start + duration)
                        return telemetry
                    self._sleep_until(start + due)
                    if index + 1 < len(ordered):
                        next_due = anchor + ordered[index + 1].offset
                    else:
                        next_due = anchor + self.cycle
                    restart = slot.action.restart_on_reply if isinstance(slot.action, Poll) else 0
                    read_deadline = start + next_due + restart - _SLOT_GUARD
                    valid = self._execute(
                        slot.action, read_deadline, telemetry, start, base_elapsed
                    )
                    if valid and restart:
                        anchor += restart  # the display restarts its tick counter
                anchor += self.cycle
                # Let the cycle run out (the display's tick counter keeps going).
                self._sleep_until(start + min(anchor, duration))
                telemetry.cycles += 1
                telemetry.elapsed = base_elapsed + self.clock.monotonic() - start
                if on_cycle is not None:
                    on_cycle(telemetry)
                if should_stop is not None and should_stop(telemetry):
                    return telemetry
        except KeyboardInterrupt:
            if not catch_interrupt:
                raise
            telemetry.interrupted = True
            return telemetry
        finally:
            telemetry.elapsed = base_elapsed + self.clock.monotonic() - start

    # ----------------------------------------------------------------- helpers

    def _sleep_until(self, deadline: float) -> None:
        remaining = deadline - self.clock.monotonic()
        if remaining > 0:
            self.clock.sleep(remaining)

    def _execute(
        self,
        action: Action,
        read_deadline: float,
        telemetry: Telemetry,
        start: float,
        base_elapsed: float,
    ) -> bool:
        """Perform one slot; return True when a poll got a valid reply."""
        if isinstance(action, Send):
            self.transport.write(action.frame)
            telemetry.commands_sent += 1
            return False

        # Like the display: throw away anything stale before asking.
        self.transport.drain()
        self.transport.write(read_request(action.command))
        telemetry.polls_sent += 1
        timeout = max(read_deadline - self.clock.monotonic(), _MIN_READ_TIMEOUT)
        data = self.transport.read(action.reply_length, timeout)
        if len(data) < action.reply_length:
            telemetry.replies_missing += 1
            return False
        try:
            _apply_reply(action.command, data, telemetry)
        except ProtocolError:
            telemetry.replies_invalid += 1
            return False
        telemetry.replies_ok += 1
        telemetry.last_reply_elapsed = base_elapsed + self.clock.monotonic() - start
        return True


def _apply_reply(command: Command, data: bytes, telemetry: Telemetry) -> None:
    if command is Command.SPEED:
        telemetry.rpm = parse_speed(data).rpm
        telemetry.max_rpm = max(telemetry.max_rpm, telemetry.rpm)
    elif command is Command.STATUS:
        code = parse_status(data).code
        telemetry.status = code
        if code not in telemetry.statuses_seen:
            telemetry.statuses_seen.append(code)
    elif command is Command.TEMPERATURE:
        telemetry.temperature = parse_temperature(data).celsius
    elif command is Command.BATTERY:
        telemetry.battery = parse_battery(data).percent
    elif command is Command.MOVING:
        telemetry.moving = parse_moving(data).moving
    else:  # pragma: no cover - schedules only poll the commands above
        raise ProtocolError(f"no parser for command {command!r}")


def enter_service_mode(
    transport: Transport,
    clock: Clock | None = None,
    *,
    repeat: int = SERVICE_MODE_REQUEST_REPEAT,
    switch_delay: float = SERVICE_MODE_SWITCH_DELAY,
) -> bytes:
    """Move the controller (and the transport) onto the 9600 baud service link.

    Sends ``11 51 25 80 F6`` ``repeat`` times at 1200 baud, waits until the
    frames have certainly been transmitted, then reconfigures the transport to
    9600 baud. Returns whatever the controller sent back at 1200 baud (useful
    for diagnostics; the display ignores it).
    """
    clock = clock or SystemClock()
    if transport.baudrate != NORMAL_BAUDRATE:
        transport.set_baudrate(NORMAL_BAUDRATE)
    transport.drain()

    payload = SERVICE_MODE_REQUEST * repeat
    started = clock.monotonic()
    transport.write(payload)
    transport.wait_sent()
    # 10 bits per byte (8N1). Never switch before the last stop bit is out.
    on_wire = len(payload) * 10 / NORMAL_BAUDRATE
    switch_at = started + max(switch_delay, on_wire + _TX_MARGIN)
    remaining = switch_at - clock.monotonic()
    if remaining > 0:
        clock.sleep(remaining)

    received = transport.drain()
    transport.set_baudrate(SERVICE_BAUDRATE)
    transport.drain()
    return received


# ----------------------------------------------------------------------- probing


def answers_at(transport: Transport, baudrate: int, *, timeout: float = 0.3) -> bool:
    """True if the controller returns a valid speed reply at ``baudrate``.

    Used to recognise a controller that is still on the 9600 baud service link
    from an earlier run: it then ignores the normal 1200 baud link until it is
    power-cycled. The transport is put back to its previous speed afterwards.
    """
    previous = transport.baudrate
    transport.set_baudrate(baudrate)
    try:
        transport.drain()
        transport.write(read_request(Command.SPEED))
        parse_speed(transport.read(3, timeout))
    except ProtocolError:
        return False
    else:
        return True
    finally:
        transport.set_baudrate(previous)


@dataclass
class ProbeResult:
    """Outcome of :func:`probe` (normal 1200 baud link)."""

    status: StatusReply | None = None
    battery: BatteryReply | None = None
    speed: SpeedReply | None = None
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def responded(self) -> bool:
        return any(v is not None for v in (self.status, self.battery, self.speed))


def probe(
    transport: Transport,
    clock: Clock | None = None,
    *,
    attempts: int = 3,
    timeout: float = 0.30,
) -> ProbeResult:
    """Read status, battery level and speed over the normal 1200 baud link.

    This is read-only and safe to run at any time; it is used to verify the
    wiring before anything is changed on the controller.
    """
    clock = clock or SystemClock()
    if transport.baudrate != NORMAL_BAUDRATE:
        transport.set_baudrate(NORMAL_BAUDRATE)

    result = ProbeResult()
    reads: tuple[tuple[str, Command, int], ...] = (
        ("status", Command.STATUS, 1),
        ("battery", Command.BATTERY, 2),
        ("speed", Command.SPEED, 3),
    )
    for name, command, length in reads:
        for _ in range(attempts):
            transport.drain()
            transport.write(read_request(command))
            data = transport.read(length, timeout)
            if len(data) < length:
                result.errors[name] = "no reply" if not data else f"short reply: {data.hex(' ')}"
                continue
            try:
                if command is Command.STATUS:
                    result.status = parse_status(data)
                elif command is Command.BATTERY:
                    result.battery = parse_battery(data)
                else:
                    result.speed = parse_speed(data)
            except ProtocolError as exc:
                result.errors[name] = str(exc)
                continue
            result.errors.pop(name, None)
            break
        # Leave a gap like the display does between requests.
        clock.sleep(0.05)
    return result
