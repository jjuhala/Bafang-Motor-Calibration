from __future__ import annotations

import pytest

from bafang_cal.protocol import (
    CALIBRATION_COMMAND,
    assist_level_command,
    read_request,
    write_request,
)
from bafang_cal.simulator import ManualClock, SimulatedController, SimulatorConfig


def test_manual_clock() -> None:
    clock = ManualClock(10.0)
    clock.sleep(-1)
    clock.advance(0.5)
    assert clock.monotonic() == 10.5


def test_mismatched_baud_rate_garbles_bytes(clock: ManualClock) -> None:
    controller = SimulatedController(clock)
    controller.set_baudrate(9600)
    controller.write(read_request(0x08))
    assert controller.frames == []
    assert controller.garbled_bytes == 2
    assert controller.drain() == b""


def test_bad_checksums_are_ignored(controller: SimulatedController) -> None:
    controller.write(bytes.fromhex("16 A0 01 B8"))  # wrong checksum
    controller.write(bytes.fromhex("11 51 25 80 F7"))  # wrong checksum
    assert controller.frames == []
    assert controller.controller_baudrate == 1200
    assert not controller.calibrating


def test_unknown_and_truncated_bytes_are_skipped(controller: SimulatedController) -> None:
    controller.write(bytes.fromhex("FF 16 99 11 08"))  # junk, unknown write cmd, then a read
    assert [f.data.hex(" ") for f in controller.frames] == ["11 08"]
    assert controller.garbled_bytes == 3
    controller.write(bytes.fromhex("16"))
    assert controller.garbled_bytes == 4


def test_lights_frame_has_no_checksum(controller: SimulatedController) -> None:
    controller.write(bytes.fromhex("16 1A F1"))
    assert [f.data.hex(" ") for f in controller.frames] == ["16 1a f1"]


def _status(controller: SimulatedController) -> int:
    controller.write(read_request(0x08))
    return controller.drain()[0]


def test_calibration_reports_bafang_sequence(
    clock: ManualClock, controller: SimulatedController
) -> None:
    controller.write(assist_level_command(0x0C))
    seen: list[int] = []
    for _ in range(90):  # 9 s of commands, 100 ms apart
        controller.write(CALIBRATION_COMMAND)
        code = _status(controller)
        if code not in seen:
            seen.append(code)
        clock.advance(0.1)
    assert seen == [0x40, 0x41, 0x43, 0x95, 0x96, 0x97, 0x98, 0x44]
    assert not controller.calibrating


def test_calibration_failure_result(clock: ManualClock) -> None:
    controller = SimulatedController(clock, SimulatorConfig(calibration_result=0x84))
    controller.write(assist_level_command(0x0C) + CALIBRATION_COMMAND)
    assert controller.calibrating
    clock.advance(10.0)
    assert _status(controller) == 0x84
    assert not controller.calibrating


def test_calibration_requires_assist_level(clock: ManualClock) -> None:
    strict = SimulatedController(clock)
    strict.write(assist_level_command(0x00) + CALIBRATION_COMMAND)
    assert not strict.calibrating
    assert _status(strict) == 0x01

    lenient = SimulatedController(clock, SimulatorConfig(requires_assist=False))
    lenient.write(CALIBRATION_COMMAND)
    assert lenient.calibrating


def test_unsupported_controller_ignores_calibration(clock: ManualClock) -> None:
    controller = SimulatedController(clock, SimulatorConfig(supports_calibration=False))
    controller.write(assist_level_command(0x0C) + CALIBRATION_COMMAND)
    assert not controller.calibrating


def test_reads_and_replies(clock: ManualClock) -> None:
    config = SimulatorConfig(temperature=-7, battery_percent=50, status=0x25)
    controller = SimulatedController(clock, config)
    controller.write(
        read_request(0x08) + read_request(0x11) + read_request(0x12) + read_request(0x31)
    )
    assert controller.drain() == bytes.fromhex("25 32 32 FF F9 F8 30 30")


def test_motor_test_makes_it_move(clock: ManualClock, controller: SimulatedController) -> None:
    controller.write(write_request(0x06, [100]))
    controller.write(read_request(0x20) + read_request(0x31))
    assert controller.drain() == bytes.fromhex("00 78 98 31 31")
    clock.advance(1.0)
    controller.write(read_request(0x20))
    assert controller.drain() == bytes.fromhex("00 00 20")


def test_calibration_motion_profile(clock: ManualClock, controller: SimulatedController) -> None:
    controller.write(assist_level_command(0x0C))
    rpm_values = []
    for _ in range(100):  # 10 s of commands, 100 ms apart
        controller.write(CALIBRATION_COMMAND)
        controller.write(read_request(0x20))
        rpm_values.append(controller.drain()[1])
        clock.advance(0.1)
    assert max(rpm_values) == SimulatorConfig().calibration_rpm
    assert rpm_values[-1] == 0  # the simulated calibration finishes


def test_closed_port_rejects_writes(controller: SimulatedController) -> None:
    controller.close()
    with pytest.raises(OSError, match="closed"):
        controller.write(b"\x11\x08")


def test_partial_read_waits_for_timeout(
    clock: ManualClock, controller: SimulatedController
) -> None:
    controller.inject(b"\x01")
    assert controller.read(3, 0.2) == b"\x01"
    assert clock.now == pytest.approx(0.2)
    assert controller.read(1, 0.0) == b""
