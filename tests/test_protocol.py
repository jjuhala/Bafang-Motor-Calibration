"""Frame builders and reply parsers, checked against bytes found in the firmware."""

from __future__ import annotations

import pytest

from bafang_cal import protocol
from bafang_cal.protocol import (
    CALIBRATION_COMMAND,
    CALIBRATION_DISPLAY_ASSIST_CODES,
    CALIBRATION_DISPLAY_SPEED_LIMIT_RPM,
    SERVICE_MODE_REQUEST,
    ChecksumError,
    Command,
    ReplyLengthError,
    assist_level_command,
    connect_request,
    lights_command,
    motor_test_command,
    motor_test_percent,
    parse_battery,
    parse_moving,
    parse_speed,
    parse_status,
    parse_temperature,
    read_request,
    speed_limit_command,
    speed_limit_rpm,
    write_request,
)


def test_firmware_constants() -> None:
    # Literal byte sequences built by the C961 G510 calibration firmware.
    assert SERVICE_MODE_REQUEST == bytes.fromhex("11 51 25 80 F6")
    assert CALIBRATION_COMMAND == bytes.fromhex("16 A0 01 B7")
    assert protocol.NORMAL_BAUDRATE == 1200
    assert protocol.SERVICE_BAUDRATE == 9600
    assert protocol.SERVICE_MODE_REQUEST_REPEAT == 3
    assert protocol.SERVICE_MODE_SWITCH_DELAY == pytest.approx(0.150)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (Command.STATUS, "11 08"),
        (Command.BATTERY, "11 11"),
        (Command.TEMPERATURE, "11 12"),
        (Command.SPEED, "11 20"),
        (Command.MOVING, "11 31"),
        (Command.HANDSHAKE, "11 90"),
    ],
)
def test_read_request(command: Command, expected: str) -> None:
    assert read_request(command) == bytes.fromhex(expected)


@pytest.mark.parametrize(
    ("baudrate", "expected"),
    [
        (1200, "11 51 04 B0 05"),  # the well-known "connect" command of config tools
        (9600, "11 51 25 80 F6"),  # what the calibration display sends
    ],
)
def test_connect_request(baudrate: int, expected: str) -> None:
    assert connect_request(baudrate) == bytes.fromhex(expected)


@pytest.mark.parametrize("baudrate", [0, -1, 0x10000])
def test_connect_request_rejects_out_of_range(baudrate: int) -> None:
    with pytest.raises(ValueError, match="16 bits"):
        connect_request(baudrate)


@pytest.mark.parametrize(
    ("command", "payload", "expected"),
    [
        (Command.CALIBRATE, [0x01], "16 A0 01 B7"),
        (Command.ASSIST_LEVEL, [0x03], "16 0B 03 24"),  # chk = 0x21 + level, as in the firmware
        (Command.MOTOR_TEST, [20], "16 06 14 30"),
        (Command.SPEED_LIMIT, [0x01, 0xF4], "16 1F 01 F4 2A"),
    ],
)
def test_write_request_checksum_includes_header(
    command: Command, payload: list[int], expected: str
) -> None:
    assert write_request(command, payload) == bytes.fromhex(expected)


def test_write_request_checksum_wraps() -> None:
    frame = write_request(0xFF, [0xFF, 0xFF])
    assert frame[-1] == (0x16 + 0xFF * 3) & 0xFF


@pytest.mark.parametrize("bad", [-1, 256])
def test_requests_reject_non_byte_commands(bad: int) -> None:
    with pytest.raises(ValueError, match="byte"):
        read_request(bad)
    with pytest.raises(ValueError, match="byte"):
        write_request(bad, [])


@pytest.mark.parametrize("level", range(1, 10))
def test_motor_test_levels(level: int) -> None:
    percent = level * 10 + 10
    assert motor_test_percent(level) == percent
    frame = motor_test_command(level)
    assert frame == bytes((0x16, 0x06, percent, (0x16 + 0x06 + percent) & 0xFF))


@pytest.mark.parametrize("level", [0, 10, -3])
def test_motor_test_rejects_invalid_levels(level: int) -> None:
    with pytest.raises(ValueError, match=r"1\.\.9"):
        motor_test_command(level)


# ------------------------------------------------------------------ replies


def test_parse_status() -> None:
    assert parse_status(b"\x01").code == 0x01
    assert not parse_status(b"\x01").is_error
    assert not parse_status(b"\x03").is_error
    assert parse_status(b"\x21").is_error
    with pytest.raises(ReplyLengthError):
        parse_status(b"")
    with pytest.raises(ReplyLengthError):
        parse_status(b"\x01\x01")


@pytest.mark.parametrize(
    ("rpm", "reply"), [(0, "00 00 20"), (45, "00 2D 4D"), (0x1234, "12 34 66")]
)
def test_parse_speed(rpm: int, reply: str) -> None:
    assert parse_speed(bytes.fromhex(reply)).rpm == rpm


def test_parse_speed_rejects_bad_checksum_and_length() -> None:
    with pytest.raises(ChecksumError):
        parse_speed(bytes.fromhex("00 2D 4E"))
    with pytest.raises(ReplyLengthError):
        parse_speed(bytes.fromhex("00 2D"))


@pytest.mark.parametrize(
    ("celsius", "reply"),
    [(31, "00 1F 1F"), (0, "00 00 00"), (-5, "FF FB FA"), (150, "00 96 96"), (300, "01 2C 2D")],
)
def test_parse_temperature_is_signed(celsius: int, reply: str) -> None:
    assert parse_temperature(bytes.fromhex(reply)).celsius == celsius


def test_parse_temperature_checksum_excludes_command() -> None:
    # Unlike the speed reply, 0x12 is *not* added to the checksum.
    with pytest.raises(ChecksumError):
        parse_temperature(bytes.fromhex("00 1F 31"))
    with pytest.raises(ReplyLengthError):
        parse_temperature(b"\x00")


def test_parse_battery_and_moving() -> None:
    assert parse_battery(bytes.fromhex("57 57")).percent == 87
    with pytest.raises(ChecksumError):
        parse_battery(bytes.fromhex("57 58"))
    assert parse_moving(bytes.fromhex("31 31")).moving
    assert not parse_moving(bytes.fromhex("30 30")).moving
    with pytest.raises(ChecksumError):
        parse_moving(bytes.fromhex("31 30"))
    with pytest.raises(ReplyLengthError):
        parse_battery(b"\x57")


# ------------------------------------------------------- riding-screen frames


def test_calibration_display_riding_frames() -> None:
    # The 0-3 level calibration display at its factory settings.
    assert CALIBRATION_DISPLAY_ASSIST_CODES == {1: 0x0C, 2: 0x02, 3: 0x03}
    assert assist_level_command(0x0C) == bytes.fromhex("16 0B 0C 2D")
    assert assist_level_command(0x00) == bytes.fromhex("16 0B 00 21")
    assert lights_command(False) == bytes.fromhex("16 1A F0")
    assert lights_command(True) == bytes.fromhex("16 1A F1")
    assert CALIBRATION_DISPLAY_SPEED_LIMIT_RPM == speed_limit_rpm(25, 26) == 200
    assert speed_limit_command(200) == bytes.fromhex("16 1F 00 C8 FD")


def test_speed_limit_uses_display_integer_maths() -> None:
    assert speed_limit_rpm(25, 29) == 179  # 25 * 208 // 29
    assert speed_limit_rpm(32, 28) == 237


@pytest.mark.parametrize(("kmh", "inches"), [(0, 26), (25, 0), (-5, 26)])
def test_speed_limit_rejects_nonsense(kmh: int, inches: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        speed_limit_rpm(kmh, inches)


@pytest.mark.parametrize("rpm", [-1, 0x10000])
def test_speed_limit_command_range(rpm: int) -> None:
    with pytest.raises(ValueError, match="16 bits"):
        speed_limit_command(rpm)


def test_assist_level_command_range() -> None:
    with pytest.raises(ValueError, match="byte"):
        assist_level_command(0x100)
