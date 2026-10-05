"""Service-mode sequencing against the simulated controller (no real sleeping)."""

from __future__ import annotations

from itertools import pairwise

import pytest

from bafang_cal.protocol import (
    CALIBRATION_COMMAND,
    NORMAL_BAUDRATE,
    SERVICE_BAUDRATE,
    SERVICE_MODE_REQUEST,
    Command,
    motor_test_command,
)
from bafang_cal.service import (
    CYCLE_SECONDS,
    SPEED_REPLY_RESTART,
    Poll,
    ScheduleRunner,
    Send,
    Slot,
    Telemetry,
    calibration_schedule,
    enter_service_mode,
    motor_test_schedule,
    probe,
    riding_schedule,
)
from bafang_cal.simulator import ManualClock, SimulatedController, SimulatorConfig

# ---------------------------------------------------------------- schedules


def test_calibration_schedule_matches_firmware_slots() -> None:
    slots = calibration_schedule()
    offsets_ms = [round(s.offset * 1000) for s in slots]
    assert offsets_ms == [0, 20, 63, 126, 189, 252, 315, 378, 441]
    # A valid speed reply restarts the display's tick counter at tick 12.
    assert slots[0].action == Poll(Command.SPEED, 3, restart_on_reply=SPEED_REPLY_RESTART)
    assert SPEED_REPLY_RESTART == pytest.approx(0.012)
    assert slots[1].action == Poll(Command.STATUS, 1)
    assert all(s.action == Send(CALIBRATION_COMMAND) for s in slots[2:])


def test_motor_test_schedule_matches_firmware_slots() -> None:
    running = motor_test_schedule(4)
    offsets_ms = [round(s.offset * 1000) for s in running]
    assert offsets_ms == [0, 64, 75, 128, 192, 256, 320, 384, 448]
    sends = [s.action for s in running if isinstance(s.action, Send)]
    assert sends == [Send(motor_test_command(4))] * 7
    assert Poll(Command.TEMPERATURE, 3) in [s.action for s in running]

    idle = motor_test_schedule(4, running=False)
    assert not any(isinstance(s.action, Send) for s in idle)


def test_motor_test_schedule_validates_level_even_when_idle() -> None:
    with pytest.raises(ValueError, match=r"1\.\.9"):
        motor_test_schedule(0, running=False)


# -------------------------------------------------------------- baud switch


def test_enter_service_mode_switches_both_sides(
    clock: ManualClock, controller: SimulatedController
) -> None:
    received = enter_service_mode(controller, clock)

    assert received == b""
    assert controller.baudrate == SERVICE_BAUDRATE
    assert controller.controller_baudrate == SERVICE_BAUDRATE
    # The first copy is honoured at 1200 baud; the controller has already
    # switched when the other two copies arrive, so they are line noise.
    assert [f.data for f in controller.frames] == [SERVICE_MODE_REQUEST]
    assert controller.frames[0].baudrate == NORMAL_BAUDRATE
    assert controller.garbled_bytes == 2 * len(SERVICE_MODE_REQUEST)
    # 15 bytes at 1200 baud need 125 ms; the display waits 150 ms.
    assert clock.now == pytest.approx(0.150)


def test_enter_service_mode_resets_transport_to_1200_first(
    clock: ManualClock, controller: SimulatedController
) -> None:
    controller.set_baudrate(SERVICE_BAUDRATE)
    enter_service_mode(controller, clock)
    assert controller.frames[0].baudrate == NORMAL_BAUDRATE


def test_enter_service_mode_never_switches_before_bytes_are_out(clock: ManualClock) -> None:
    controller = SimulatedController(clock)
    enter_service_mode(controller, clock, repeat=6, switch_delay=0.0)
    # 30 bytes * 10 bits / 1200 baud = 250 ms, plus margin.
    assert clock.now >= 0.250


def test_enter_service_mode_discards_stale_input(clock: ManualClock) -> None:
    controller = SimulatedController(clock)
    controller.inject(b"\x01")  # left over from before
    assert enter_service_mode(controller, clock) == b""


def test_enter_service_mode_returns_bytes_seen_at_1200(clock: ManualClock) -> None:
    class Chatty(SimulatedController):
        def write(self, data: bytes) -> None:
            super().write(data)
            self.inject(b"\xaa")

    assert enter_service_mode(Chatty(clock), clock) == b"\xaa"


# ------------------------------------------------------------------- runner


def _calibration_run(
    clock: ManualClock, controller: SimulatedController, duration: float, *, assist: bool = True
) -> Telemetry:
    runner = ScheduleRunner(controller, clock)
    if assist:  # what the display's riding screen did before the user started it
        runner.run(riding_schedule(), duration=1.0)
    enter_service_mode(controller, clock)
    return runner.run(calibration_schedule(), duration=duration)


def test_calibration_run_sends_firmware_sequence(
    clock: ManualClock, controller: SimulatedController
) -> None:
    # The simulator always answers 11 20, so every cycle lasts 512 ms.
    telemetry = _calibration_run(clock, controller, duration=10 * 0.512)

    assert telemetry.cycles == 10
    assert telemetry.commands_sent == 70  # 7 per cycle
    assert telemetry.polls_sent == 20
    assert telemetry.replies_ok == 20
    assert telemetry.replies_missing == telemetry.replies_invalid == 0
    # The simulated controller walks through Bafang's documented sequence.
    assert telemetry.statuses_seen == [0x01, 0x40, 0x41, 0x43, 0x95, 0x96]
    assert telemetry.status == 0x96
    assert telemetry.max_rpm == SimulatorConfig().calibration_rpm
    assert not telemetry.interrupted

    service_frames = [f for f in controller.frames if f.baudrate == SERVICE_BAUDRATE]
    assert [f.data for f in service_frames[:9]] == [
        bytes.fromhex("11 20"),
        bytes.fromhex("11 08"),
        *[CALIBRATION_COMMAND] * 7,
    ]


def test_calibration_needs_assist_level_first(
    clock: ManualClock, controller: SimulatedController
) -> None:
    telemetry = _calibration_run(clock, controller, duration=3.0, assist=False)
    assert telemetry.statuses_seen == [0x01]
    assert telemetry.max_rpm == 0


def _service_times(controller: SimulatedController) -> list[tuple[float, str]]:
    """(time since the first service-link frame, frame) pairs."""
    frames = [f for f in controller.frames if f.baudrate == SERVICE_BAUDRATE]
    return [(round((f.time - frames[0].time) * 1000), f.data.hex(" ")) for f in frames]


def test_valid_speed_reply_gives_512_ms_cycles(
    clock: ManualClock, controller: SimulatedController
) -> None:
    _calibration_run(clock, controller, duration=1.2)
    cal = CALIBRATION_COMMAND.hex(" ")
    assert _service_times(controller)[:10] == [
        (0, "11 20"),
        (32, "11 08"),
        *[(75 + 63 * k, cal) for k in range(7)],  # 75 ... 453
        (512, "11 20"),
    ]


def test_missing_speed_reply_gives_500_ms_cycles(clock: ManualClock) -> None:
    controller = SimulatedController(clock, SimulatorConfig(responsive=False))
    _calibration_run(clock, controller, duration=1.2)
    cal = CALIBRATION_COMMAND.hex(" ")
    assert _service_times(controller)[:10] == [
        (0, "11 20"),
        # The display would ask at 20 ms; the tool keeps listening for a late
        # speed reply until just before 32 ms, so the status request follows
        # at 31 ms. This is the only deliberate timing difference.
        (31, "11 08"),
        *[(63 * k, cal) for k in range(1, 8)],  # 63 ... 441
        (500, "11 20"),
    ]


@pytest.mark.parametrize(("responsive", "longest_gap"), [(True, 0.134), (False, 0.122)])
def test_calibration_command_cadence(
    clock: ManualClock, responsive: bool, longest_gap: float
) -> None:
    controller = SimulatedController(clock, SimulatorConfig(responsive=responsive))
    _calibration_run(clock, controller, duration=3.0)
    times = [f.time for f in controller.frames if f.data == CALIBRATION_COMMAND]
    gaps = [b - a for a, b in pairwise(times)]
    # 63 ms apart inside a cycle; across the cycle boundary 134 ms when the
    # controller answers 11 20 (512-tick cycle), 122 ms when it does not.
    assert min(gaps) == pytest.approx(0.063)
    assert max(gaps) == pytest.approx(longest_gap)


def test_late_speed_reply_still_counts(clock: ManualClock) -> None:
    """A reply delayed by a slow USB adapter (e.g. FTDI latency) is not lost."""

    class SlowAdapter(SimulatedController):
        latency = 0.025

        def write(self, data: bytes) -> None:
            self.sent_at = self.clock.monotonic()
            super().write(data)

        def read(self, size: int, timeout: float) -> bytes:
            ready_at = self.sent_at + self.latency
            wait = ready_at - self.clock.monotonic()
            if wait > timeout:
                self.clock.sleep(timeout)
                return b""
            self.clock.sleep(wait)
            return super().read(size, 0.0)

    controller = SlowAdapter(clock)
    telemetry = _calibration_run(clock, controller, duration=1.0)
    assert telemetry.replies_missing == 0
    assert telemetry.replies_ok == telemetry.polls_sent
    assert _service_times(controller)[1] == (32, "11 08")


def test_calibration_without_replies(clock: ManualClock) -> None:
    controller = SimulatedController(clock, SimulatorConfig(responsive=False))
    telemetry = _calibration_run(clock, controller, duration=2.0)
    assert telemetry.commands_sent == 28
    assert telemetry.replies_ok == 0
    assert telemetry.replies_missing == telemetry.polls_sent == 8
    assert not telemetry.got_replies
    assert telemetry.rpm is None


def test_controller_that_ignores_baud_request_gets_no_commands(clock: ManualClock) -> None:
    controller = SimulatedController(clock, SimulatorConfig(honour_baud_request=False))
    telemetry = _calibration_run(clock, controller, duration=1.0)
    assert not telemetry.got_replies
    assert controller.controller_baudrate == NORMAL_BAUDRATE
    assert CALIBRATION_COMMAND not in [f.data for f in controller.frames]
    assert not controller.calibrating


def test_invalid_replies_are_counted(clock: ManualClock) -> None:
    class Corrupting(SimulatedController):
        def read(self, size: int, timeout: float) -> bytes:
            data = bytearray(super().read(size, timeout))
            if len(data) == 3:
                data[-1] ^= 0xFF
            return bytes(data)

    controller = Corrupting(clock)
    telemetry = _calibration_run(clock, controller, duration=1.0)
    assert telemetry.replies_invalid == 2  # the speed replies
    assert telemetry.replies_ok == 2  # the status replies
    assert telemetry.rpm is None


def test_should_stop_is_checked_after_each_cycle(
    clock: ManualClock, controller: SimulatedController
) -> None:
    enter_service_mode(controller, clock)
    seen: list[int] = []

    def stop_after_three(t: Telemetry) -> bool:
        seen.append(t.cycles)
        return t.cycles == 3

    telemetry = ScheduleRunner(controller, clock).run(
        calibration_schedule(), duration=60, on_cycle=lambda t: None, should_stop=stop_after_three
    )
    assert seen == [1, 2, 3]
    assert telemetry.cycles == 3
    # Each cycle is 500 ms plus the 12 ms restart after the valid speed reply.
    assert telemetry.elapsed == pytest.approx(3 * (CYCLE_SECONDS + SPEED_REPLY_RESTART))


def test_keyboard_interrupt_is_reported(
    clock: ManualClock, controller: SimulatedController
) -> None:
    def interrupt(t: Telemetry) -> None:
        if t.cycles == 2:
            raise KeyboardInterrupt

    runner = ScheduleRunner(controller, clock)
    telemetry = runner.run(calibration_schedule(), duration=60, on_cycle=interrupt)
    assert telemetry.interrupted
    assert telemetry.cycles == 2

    with pytest.raises(KeyboardInterrupt):
        runner.run(calibration_schedule(), duration=60, on_cycle=interrupt, catch_interrupt=False)


def test_runner_reanchors_instead_of_bursting(
    clock: ManualClock, controller: SimulatedController
) -> None:
    enter_service_mode(controller, clock)

    def stall(t: Telemetry) -> None:
        if t.cycles == 1:
            clock.advance(5.0)  # the host froze for 5 seconds

    ScheduleRunner(controller, clock).run(calibration_schedule(), duration=7.0, on_cycle=stall)
    times = [f.time for f in controller.frames if f.data == CALIBRATION_COMMAND]
    gaps = [b - a for a, b in pairwise(times)]
    # No burst of back-to-back catch-up frames after the stall.
    assert min(gaps) == pytest.approx(0.063)


def test_runner_accumulates_into_existing_telemetry(
    clock: ManualClock, controller: SimulatedController
) -> None:
    enter_service_mode(controller, clock)
    runner = ScheduleRunner(controller, clock)
    telemetry = runner.run(motor_test_schedule(2), duration=1.0)
    sent = telemetry.commands_sent
    runner.run(motor_test_schedule(2, running=False), duration=1.0, telemetry=telemetry)
    assert telemetry.commands_sent == sent
    assert telemetry.cycles == 4
    assert telemetry.elapsed == pytest.approx(2.0, abs=0.01)
    assert telemetry.temperature == SimulatorConfig().temperature


@pytest.mark.parametrize(
    "slots",
    [(), (Slot(0.5, Send(b"x")),), (Slot(0.0, Send(b"x")), Slot(0.75, Send(b"y")))],
)
def test_runner_rejects_bad_schedules(clock: ManualClock, slots: tuple[Slot, ...]) -> None:
    runner = ScheduleRunner(SimulatedController(clock), clock)
    with pytest.raises(ValueError, match=r"schedule|cycle"):
        runner.run(slots, duration=1.0)


def test_statuses_seen_are_deduplicated(clock: ManualClock) -> None:
    config = SimulatorConfig(status=0x08, supports_calibration=False)
    controller = SimulatedController(clock, config)
    telemetry = _calibration_run(clock, controller, duration=1.0)
    config.status = 0x01
    ScheduleRunner(controller, clock).run(calibration_schedule(), duration=1.0, telemetry=telemetry)
    assert telemetry.statuses_seen == [0x08, 0x01]


def test_riding_schedule_matches_display() -> None:
    slots = riding_schedule()
    assert [round(s.offset * 1000) for s in slots] == [0, 120, 200, 300, 400]
    assert slots[0].action == Send(bytes.fromhex("16 0B 0C 2D 16 1A F0 16 1F 00 C8 FD"))
    assert [s.action for s in slots[1:]] == [
        Poll(Command.SPEED, 3),
        Poll(Command.STATUS, 1),
        Poll(Command.BATTERY, 2),
        Poll(Command.MOVING, 2),
    ]
    assert riding_schedule(2)[0].action == Send(
        bytes.fromhex("16 0B 02 23 16 1A F0 16 1F 00 C8 FD")
    )


@pytest.mark.parametrize("level", [0, 4])
def test_riding_schedule_rejects_bad_levels(level: int) -> None:
    with pytest.raises(ValueError, match=r"1\.\.3"):
        riding_schedule(level)


def test_riding_run_collects_normal_values(
    clock: ManualClock, controller: SimulatedController
) -> None:
    telemetry = ScheduleRunner(controller, clock).run(riding_schedule(), duration=2.0)
    assert telemetry.replies_ok == telemetry.polls_sent == 16
    assert telemetry.battery == 87
    assert telemetry.moving is False
    assert telemetry.status == 0x01
    assert controller.assist_code == 0x0C
    assert controller.controller_baudrate == NORMAL_BAUDRATE


# -------------------------------------------------------------------- probe


def test_probe_reads_everything(clock: ManualClock, controller: SimulatedController) -> None:
    result = probe(controller, clock)
    assert result.responded
    assert result.status is not None
    assert result.status.code == 0x01
    assert result.battery is not None
    assert result.battery.percent == 87
    assert result.speed is not None
    assert result.speed.rpm == 0
    assert result.errors == {}
    assert [f.data.hex(" ") for f in controller.frames] == ["11 08", "11 11", "11 20"]


def test_probe_without_controller(clock: ManualClock) -> None:
    controller = SimulatedController(clock, SimulatorConfig(responsive=False))
    result = probe(controller, clock, attempts=2)
    assert not result.responded
    assert result.errors == {"status": "no reply", "battery": "no reply", "speed": "no reply"}
    assert len(controller.frames) == 6  # every request retried


def test_probe_reports_bad_checksums(clock: ManualClock) -> None:
    class BadBattery(SimulatedController):
        def read(self, size: int, timeout: float) -> bytes:
            data = super().read(size, timeout)
            return b"\x57\x00" if size == 2 else data

    result = probe(BadBattery(clock), clock, attempts=1)
    assert result.battery is None
    assert "checksum" in result.errors["battery"]
    assert result.status is not None


def test_probe_switches_back_to_normal_baud(
    clock: ManualClock, controller: SimulatedController
) -> None:
    controller.set_baudrate(SERVICE_BAUDRATE)
    probe(controller, clock)
    assert controller.baudrate == NORMAL_BAUDRATE
