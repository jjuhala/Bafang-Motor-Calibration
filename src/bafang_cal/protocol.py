"""Bafang UART display protocol primitives.

Everything in this module is pure (no I/O) so it can be unit-tested exhaustively.
The byte values and checksum rules were taken from the C961 "G510 calibration"
display firmware; see ``docs/firmware-analysis.md`` for the code addresses each
constant was recovered from.

Frame formats
-------------
Display -> controller requests start with a *type* byte:

* ``0x11`` (read):  ``11 <cmd>`` for plain reads; the controller answers with the
  raw reply bytes (no header).
* ``0x11 0x51`` (connect / set baud rate): ``11 51 <baud_hi> <baud_lo> <chk>`` where
  ``chk = (0x51 + baud_hi + baud_lo) & 0xFF``. Note that the leading ``0x11`` is
  *not* part of the checksum.
* ``0x16`` (write): ``16 <cmd> <payload...> <chk>`` where
  ``chk = (0x16 + cmd + sum(payload)) & 0xFF``.

Replies carry no header; the requester knows the expected length.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

__all__ = [
    "CALIBRATION_COMMAND",
    "CALIBRATION_DISPLAY_ASSIST_CODES",
    "CALIBRATION_DISPLAY_SPEED_LIMIT_RPM",
    "MOTOR_TEST_LEVELS",
    "NORMAL_BAUDRATE",
    "SERVICE_BAUDRATE",
    "SERVICE_MODE_REQUEST",
    "SERVICE_MODE_REQUEST_REPEAT",
    "SERVICE_MODE_SWITCH_DELAY",
    "BatteryReply",
    "ChecksumError",
    "Command",
    "FrameType",
    "MovingReply",
    "ProtocolError",
    "ReplyLengthError",
    "SpeedReply",
    "StatusReply",
    "TemperatureReply",
    "assist_level_command",
    "connect_request",
    "lights_command",
    "motor_test_command",
    "motor_test_percent",
    "parse_battery",
    "parse_moving",
    "parse_speed",
    "parse_status",
    "parse_temperature",
    "read_request",
    "speed_limit_command",
    "speed_limit_rpm",
    "write_request",
]

#: Baud rate of the normal display <-> controller link (8N1).
NORMAL_BAUDRATE = 1200

#: Baud rate the calibration display switches the controller to (8N1).
SERVICE_BAUDRATE = 9600

#: Number of times the display sends the service-mode request back-to-back.
SERVICE_MODE_REQUEST_REPEAT = 3

#: Seconds the display waits after queueing the service-mode requests before it
#: reconfigures its own UART to :data:`SERVICE_BAUDRATE` (150 ticks of 1 ms).
#: Transmitting 3 x 5 bytes at 1200 baud takes 125 ms of that.
SERVICE_MODE_SWITCH_DELAY = 0.150

#: Valid levels for the motor test (display sub-mode 1), mapped to the
#: percentage byte sent in the ``16 06`` frame (level * 10 + 10).
MOTOR_TEST_LEVELS = range(1, 10)

#: Assist-level codes the calibration display sends in its riding screen. It is
#: a 0-3 level display (table at 0x39B1): level 1 -> 0x0C, 2 -> 0x02, 3 -> 0x03.
#: It powers up at level 1.
CALIBRATION_DISPLAY_ASSIST_CODES: dict[int, int] = {1: 0x0C, 2: 0x02, 3: 0x03}

#: Speed limit the calibration display reports with its factory settings:
#: 25 km/h on a 26 inch wheel = 200 wheel rpm.
CALIBRATION_DISPLAY_SPEED_LIMIT_RPM = 200


class FrameType(enum.IntEnum):
    """First byte of every display -> controller request."""

    READ = 0x11
    WRITE = 0x16


class Command(enum.IntEnum):
    """Command byte (second byte) of display -> controller requests."""

    #: Read status / error code. Reply: 1 byte.
    STATUS = 0x08
    #: Write assist (PAS) level. Payload: 1 byte.
    ASSIST_LEVEL = 0x0B
    #: Read battery level in percent. Reply: 2 bytes ``<pct> <chk=pct>``.
    BATTERY = 0x11
    #: Read temperature (sub-mode 1 of the calibration display, shown as "NNN°C").
    #: Reply: 3 bytes ``<hi> <lo> <chk=hi+lo>``, value is a signed 16-bit integer.
    TEMPERATURE = 0x12
    #: Write lights on/off. Payload: ``0xF1`` (on) / ``0xF0`` (off), no checksum.
    LIGHTS = 0x1A
    #: Write speed limit / wheel size. Payload: 2 bytes.
    SPEED_LIMIT = 0x1F
    #: Read wheel speed in RPM. Reply: 3 bytes ``<hi> <lo> <chk=0x20+hi+lo>``.
    SPEED = 0x20
    #: Read "moving" flag. Reply: 2 bytes ``<flag> <chk=flag>``; ``0x31`` = moving.
    MOVING = 0x31
    #: Connect / select baud rate. Payload: 2 byte baud rate, big-endian.
    CONNECT = 0x51
    #: Handshake sent once after power-on; the display expects ``90 40 D0``.
    HANDSHAKE = 0x90
    #: Motor test run (sub-mode 1). Payload: 1 byte percentage (20..100).
    MOTOR_TEST = 0x06
    #: Calibration (sub-mode 0). Payload: ``0x01``.
    CALIBRATE = 0xA0


class ProtocolError(ValueError):
    """Raised when a reply cannot be decoded."""


class ReplyLengthError(ProtocolError):
    """The reply did not have the expected number of bytes."""


class ChecksumError(ProtocolError):
    """The reply checksum did not match."""


def _u8(value: int) -> int:
    return value & 0xFF


def _check_byte(name: str, value: int) -> None:
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{name} must be a byte (0..255), got {value}")


def _expect_length(data: bytes, length: int, what: str) -> None:
    if len(data) != length:
        raise ReplyLengthError(
            f"{what} reply must be {length} byte(s), got {len(data)}: {data.hex(' ') or '<none>'}"
        )


def read_request(command: int) -> bytes:
    """Build a plain read request: ``11 <cmd>``."""
    _check_byte("command", command)
    return bytes((FrameType.READ, command))


def write_request(command: int, payload: bytes | bytearray | list[int] | tuple[int, ...]) -> bytes:
    """Build a write request: ``16 <cmd> <payload...> <chk>``.

    The checksum is the low byte of the sum of *all* preceding bytes, including
    the ``0x16`` frame type.
    """
    _check_byte("command", command)
    body = bytes((FrameType.WRITE, command, *payload))
    return body + bytes((_u8(sum(body)),))


def connect_request(baudrate: int) -> bytes:
    """Build the connect / baud-rate request: ``11 51 <hi> <lo> <chk>``.

    ``connect_request(1200)`` is the well-known ``11 51 04 B0 05`` used by
    configuration tools; the calibration display sends ``connect_request(9600)``
    (``11 51 25 80 F6``) to move the controller onto its 9600 baud service link.
    """
    if not 0 < baudrate <= 0xFFFF:
        raise ValueError(f"baud rate must fit in 16 bits, got {baudrate}")
    hi, lo = baudrate >> 8, baudrate & 0xFF
    return bytes((FrameType.READ, Command.CONNECT, hi, lo, _u8(Command.CONNECT + hi + lo)))


def motor_test_percent(level: int) -> int:
    """Percentage byte the display sends for a motor-test ``level`` (1..9)."""
    if level not in MOTOR_TEST_LEVELS:
        raise ValueError(f"motor test level must be 1..9, got {level}")
    return level * 10 + 10


def motor_test_command(level: int) -> bytes:
    """Build the motor-test frame ``16 06 <pct> <chk>`` for ``level`` (1..9)."""
    return write_request(Command.MOTOR_TEST, (motor_test_percent(level),))


def assist_level_command(code: int) -> bytes:
    """Build ``16 0B <code> <chk>`` (``code`` is the protocol value, not the level)."""
    _check_byte("assist code", code)
    return write_request(Command.ASSIST_LEVEL, (code,))


def lights_command(on: bool) -> bytes:
    """Build ``16 1A F1`` (on) or ``16 1A F0`` (off); this frame has no checksum."""
    return bytes((FrameType.WRITE, Command.LIGHTS, 0xF1 if on else 0xF0))


def speed_limit_rpm(kmh: int, wheel_inches: int) -> int:
    """Wheel rpm for a speed limit, with the display's integer maths (km/h * 208 / inch)."""
    if kmh <= 0 or wheel_inches <= 0:
        raise ValueError("speed limit and wheel size must be positive")
    return kmh * 208 // wheel_inches


def speed_limit_command(rpm: int) -> bytes:
    """Build ``16 1F <rpm hi> <rpm lo> <chk>``."""
    if not 0 <= rpm <= 0xFFFF:
        raise ValueError(f"speed limit must fit in 16 bits, got {rpm}")
    return write_request(Command.SPEED_LIMIT, (rpm >> 8, rpm & 0xFF))


#: ``11 51 25 80 F6`` -- request the 9600 baud service link (sent 3x at 1200 baud).
SERVICE_MODE_REQUEST = connect_request(SERVICE_BAUDRATE)

#: ``16 A0 01 B7`` -- calibration command, repeated every ~63 ms by the display.
CALIBRATION_COMMAND = write_request(Command.CALIBRATE, (0x01,))


# --------------------------------------------------------------------------- replies


@dataclass(frozen=True)
class StatusReply:
    """Reply to ``11 08``: a single status / error-code byte."""

    code: int

    @property
    def is_error(self) -> bool:
        """Codes below 4 (``0x01`` normal, ``0x03`` braking) are not errors in riding mode."""
        return self.code >= 0x04


@dataclass(frozen=True)
class SpeedReply:
    """Reply to ``11 20``: wheel speed as a raw 16-bit RPM value."""

    rpm: int


@dataclass(frozen=True)
class TemperatureReply:
    """Reply to ``11 12``: signed temperature in degrees Celsius."""

    celsius: int


@dataclass(frozen=True)
class BatteryReply:
    """Reply to ``11 11``: battery level in percent."""

    percent: int


@dataclass(frozen=True)
class MovingReply:
    """Reply to ``11 31``: ``0x31`` while the bike is moving."""

    flag: int

    @property
    def moving(self) -> bool:
        return self.flag == 0x31


def parse_status(data: bytes) -> StatusReply:
    _expect_length(data, 1, "status")
    return StatusReply(data[0])


def parse_speed(data: bytes) -> SpeedReply:
    _expect_length(data, 3, "speed")
    hi, lo, chk = data
    expected = _u8(Command.SPEED + hi + lo)
    if chk != expected:
        raise ChecksumError(f"speed reply checksum {chk:#04x} != {expected:#04x}")
    return SpeedReply((hi << 8) | lo)


def parse_temperature(data: bytes) -> TemperatureReply:
    _expect_length(data, 3, "temperature")
    hi, lo, chk = data
    expected = _u8(hi + lo)
    if chk != expected:
        raise ChecksumError(f"temperature reply checksum {chk:#04x} != {expected:#04x}")
    return TemperatureReply(int.from_bytes(bytes((hi, lo)), "big", signed=True))


def parse_battery(data: bytes) -> BatteryReply:
    _expect_length(data, 2, "battery")
    value, chk = data
    if chk != value:
        raise ChecksumError(f"battery reply checksum {chk:#04x} != {value:#04x}")
    return BatteryReply(value)


def parse_moving(data: bytes) -> MovingReply:
    _expect_length(data, 2, "moving")
    value, chk = data
    if chk != value:
        raise ChecksumError(f"moving reply checksum {chk:#04x} != {value:#04x}")
    return MovingReply(value)
