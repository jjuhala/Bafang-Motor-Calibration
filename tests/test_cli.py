"""End-to-end CLI runs against the simulator with a manual clock (instant)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from bafang_cal import __version__, cli
from bafang_cal.protocol import CALIBRATION_COMMAND, NORMAL_BAUDRATE, SERVICE_BAUDRATE
from bafang_cal.simulator import ManualClock, SimulatedController, SimulatorConfig


@pytest.fixture(autouse=True)
def instant_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "SystemClock", ManualClock)


def use_simulator(
    monkeypatch: pytest.MonkeyPatch, *, on_service_link: bool = False, **config: object
) -> list[SimulatedController]:
    created: list[SimulatedController] = []

    def factory(clock: ManualClock) -> SimulatedController:
        controller = SimulatedController(clock, SimulatorConfig(**config))  # type: ignore[arg-type]
        if on_service_link:  # left on the 9600 baud link by an earlier run
            controller.write(bytes.fromhex("11 51 25 80 F6"))
        created.append(controller)
        return controller

    monkeypatch.setattr(cli, "SimulatedController", factory)
    return created


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(list(argv), out=out)
    return code, out.getvalue()


def sent(controller: SimulatedController, baudrate: int | None = None) -> list[str]:
    return [
        f.data.hex(" ") for f in controller.frames if baudrate is None or f.baudrate == baudrate
    ]


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_command_is_required() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2


def test_motor_test_is_not_offered() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["motor-test", "--simulate"])
    assert excinfo.value.code == 2


# ------------------------------------------------------------------ calibrate


def test_calibrate_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sims = use_simulator(monkeypatch)
    code, output = run("calibrate", "--simulate", "--yes")

    assert code == 0
    assert "status   01: normal operation" in output
    assert "Showing assist level 1 (code 0C)" in output
    assert "11 51 25 80 F6 x3" in output
    assert "Status codes seen: 01, 40, 41, 43, 95, 96, 97, 98, 44" in output
    assert "Calibration SUCCEEDED" in output
    assert "Switch the battery OFF" in output

    controller = sims[0]
    assert controller.controller_baudrate == SERVICE_BAUDRATE
    assert controller.closed
    normal = sent(controller, NORMAL_BAUDRATE)
    assert normal[:3] == ["11 08", "11 11", "11 20"]  # read-only check first
    assert normal.count("16 0b 0c 2d") == 4  # 2 s of the riding screen
    assert "16 1a f0" in normal
    assert "16 1f 00 c8 fd" in normal
    assert normal[-1] == "11 51 25 80 f6"
    # Stops by itself soon after the controller reports 44 (7 s in the simulator).
    service = sent(controller, SERVICE_BAUDRATE)
    assert 90 <= service.count(CALIBRATION_COMMAND.hex(" ")) <= 120


def test_calibrate_reports_failure_code(monkeypatch: pytest.MonkeyPatch) -> None:
    use_simulator(monkeypatch, calibration_result=0x84)
    code, output = run("calibrate", "--simulate", "--yes")
    assert code == 1
    assert "Calibration FAILED: the controller reported 84: calibration failed." in output
    assert "run the calibration again" in output


def test_calibrate_unsupported_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    use_simulator(monkeypatch, supports_calibration=False)
    code, output = run("calibrate", "--simulate", "--yes", "--duration", "6")
    assert code == 1
    assert "never reported a\ncalibration status" in output
    assert "CR R10M.1000.SN.U 1.5" in output


def test_calibrate_times_out_while_calibrating(monkeypatch: pytest.MonkeyPatch) -> None:
    use_simulator(monkeypatch, calibration_seconds=30.0)
    code, output = run("calibrate", "--simulate", "--yes", "--duration", "5")
    assert code == 1
    assert "NOT CONFIRMED: the controller was still calibrating" in output
    assert "the time limit was reached" in output


def test_calibrate_interrupted_while_calibrating(monkeypatch: pytest.MonkeyPatch) -> None:
    use_simulator(monkeypatch, calibration_seconds=30.0)
    calls = {"n": 0}
    real_update = cli.LiveLine.update

    def update(self: cli.LiveLine, elapsed: float, text: str) -> None:
        calls["n"] += 1
        if calls["n"] == 6:
            raise KeyboardInterrupt
        real_update(self, elapsed, text)

    monkeypatch.setattr(cli.LiveLine, "update", update)
    code, output = run("calibrate", "--simulate", "--yes")
    assert code == 1
    assert "when you stopped it" in output


def test_calibrate_interrupted_during_riding_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    sims = use_simulator(monkeypatch)
    real_sleep = ManualClock.sleep

    def sleep(self: ManualClock, seconds: float) -> None:
        if self.now > 1.0:
            raise KeyboardInterrupt
        real_sleep(self, seconds)

    monkeypatch.setattr(ManualClock, "sleep", sleep)
    code, output = run("calibrate", "--simulate", "--yes")
    assert code == 1
    assert "controller is still on the normal link" in output
    assert "11 51 25 80 f6" not in sent(sims[0])


def test_calibrate_assist_level_option(monkeypatch: pytest.MonkeyPatch) -> None:
    sims = use_simulator(monkeypatch)
    code, output = run("calibrate", "--simulate", "--yes", "--assist-level", "3")
    assert code == 0
    assert "assist level 3 (code 03)" in output
    assert "16 0b 03 24" in sent(sims[0])


def test_calibrate_rejects_assist_level_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["calibrate", "--simulate", "--assist-level", "0"])
    assert excinfo.value.code == 2


def test_calibrate_asks_for_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    sims = use_simulator(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda: "no")
    code, output = run("calibrate", "--simulate")
    assert code == 1
    assert "Aborted -- nothing was sent." in output
    assert sims == []


def test_calibrate_confirmation_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    use_simulator(monkeypatch)

    def eof() -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    code, _ = run("calibrate", "--simulate")
    assert code == 1


def test_calibrate_accepts_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    use_simulator(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda: " YES ")
    code, _ = run("calibrate", "--simulate")
    assert code == 0


def test_calibrate_stops_when_controller_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    sims = use_simulator(monkeypatch, responsive=False)
    code, output = run("calibrate", "--simulate", "--yes")
    assert code == 1
    assert "did not answer on the 1200 baud display link" in output
    assert "16 0b" not in " ".join(sent(sims[0]))  # nothing but reads was sent


def test_calibrate_skip_check_without_service_link(monkeypatch: pytest.MonkeyPatch) -> None:
    use_simulator(monkeypatch, honour_baud_request=False)
    code, output = run("calibrate", "--simulate", "--yes", "--skip-check")
    assert code == 1
    assert "Checking the controller" not in output
    # Gives up after the default 5 s instead of the full 60 s.
    assert "sent in 5.0 s" in output
    assert "never answered at 9600 baud" in output


def test_calibrate_no_reply_timeout_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    use_simulator(monkeypatch, honour_baud_request=False)
    code, output = run(
        "calibrate", "--simulate", "--yes", "--duration", "8", "--no-reply-timeout", "0"
    )
    assert code == 1
    assert "sent in 8.0 s" in output


def test_calibrate_notes_existing_error(monkeypatch: pytest.MonkeyPatch) -> None:
    use_simulator(monkeypatch, status=0x08)
    code, output = run("calibrate", "--simulate", "--yes")
    assert code == 0
    assert "08: motor hall / position sensor signal fault" in output
    assert "what the calibration should fix" in output


def test_calibrate_trace_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    use_simulator(monkeypatch)
    trace = tmp_path / "trace.txt"
    code, _ = run("calibrate", "--simulate", "--yes", "--trace", str(trace))
    assert code == 0
    text = trace.read_text()
    assert " 1200 TX   16 0b 0c 2d 16 1a f0 16 1f 00 c8 fd" in text
    assert " 1200 TX   11 51 25 80 f6 11 51 25 80 f6 11 51 25 80 f6" in text
    assert " 9600 TX   16 a0 01 b7" in text
    assert " 9600 RX   44" in text


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_duration_must_be_positive(value: str) -> None:
    with pytest.raises(SystemExit):
        cli.main(["calibrate", "--simulate", "--duration", value])


@pytest.mark.parametrize("value", ["-1", "abc"])
def test_no_reply_timeout_must_be_non_negative(value: str) -> None:
    with pytest.raises(SystemExit):
        cli.main(["calibrate", "--simulate", "--no-reply-timeout", value])


# ---------------------------------------------------------------------- probe


def test_trace_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_simulator(monkeypatch)
    code, _ = run("probe", "--simulate", "--trace", "-")
    assert code == 0
    assert "TX   11 08" in capsys.readouterr().err


def test_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    sims = use_simulator(monkeypatch)
    code, output = run("probe", "--simulate")
    assert code == 0
    assert "battery  87 %" in output
    assert sent(sims[0]) == ["11 08", "11 11", "11 20"]  # read-only


def test_probe_silent_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    use_simulator(monkeypatch, responsive=False)
    code, output = run("probe", "--simulate")
    assert code == 1
    assert "status   no valid reply (no reply)" in output


def test_probe_watch_until_interrupted(monkeypatch: pytest.MonkeyPatch) -> None:
    use_simulator(monkeypatch)
    calls = {"n": 0}
    real_sleep = ManualClock.sleep

    def sleep(self: ManualClock, seconds: float) -> None:
        if seconds == 1.0:
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt
        real_sleep(self, seconds)

    monkeypatch.setattr(ManualClock, "sleep", sleep)
    code, output = run("probe", "--simulate", "--watch")
    assert code == 0
    assert output.count("Reading the controller") == 3


# ---------------------------------------------------------------------- ports


def test_port_is_required_without_adapters(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "list_serial_ports", lambda usb_only: [])
    code, _ = run("probe")
    assert code == 1
    assert "no USB serial adapter found" in capsys.readouterr().err


def test_port_is_required_with_several_adapters(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "list_serial_ports", lambda usb_only: [("COM3", "CH340"), ("COM7", "CP2102")]
    )
    code, _ = run("probe")
    assert code == 1
    assert "several serial adapters found (COM3, COM7)" in capsys.readouterr().err


def test_single_adapter_is_used_automatically(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []

    def fake_serial(port: str, baudrate: int) -> SimulatedController:
        opened.append(port)
        return SimulatedController(ManualClock())

    monkeypatch.setattr(cli, "list_serial_ports", lambda usb_only: [("/dev/ttyUSB0", "CP2102")])
    monkeypatch.setattr(cli, "SerialTransport", fake_serial)
    code, output = run("probe")
    assert code == 0
    assert opened == ["/dev/ttyUSB0"]
    assert "Using serial port /dev/ttyUSB0 (CP2102)." in output


def test_explicit_port_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []

    def fake_serial(port: str, baudrate: int) -> SimulatedController:
        opened.append(port)
        return SimulatedController(ManualClock())

    monkeypatch.setattr(cli, "SerialTransport", fake_serial)
    code, _ = run("probe", "--port", "COM9")
    assert code == 0
    assert opened == ["COM9"]


def test_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "list_serial_ports", lambda: [("/dev/ttyUSB0", "CP2102 USB to UART")])
    code, output = run("ports")
    assert code == 0
    assert output == "/dev/ttyUSB0\tCP2102 USB to UART\n"

    monkeypatch.setattr(cli, "list_serial_ports", list)
    code, output = run("ports")
    assert code == 1
    assert "No serial ports found" in output


# --------------------------------------------------------------------- output


def test_live_line_on_terminal() -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    stream = Tty()
    line = cli.LiveLine(stream)
    line.update(0.5, "long status text")
    line.update(1.0, "short")
    line.finish()
    assert stream.getvalue() == "\rlong status text\rshort           \n"


def test_live_line_when_redirected() -> None:
    stream = io.StringIO()
    line = cli.LiveLine(stream, interval=2.0)
    for step in range(10):
        line.update(step * 0.5, f"t={step * 0.5}")
    line.finish()
    assert stream.getvalue().splitlines() == ["t=0.0", "t=2.0", "t=4.0"]


def test_keyboard_interrupt_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(args: object, out: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_cmd_ports", boom)
    code, _ = run("ports")
    assert code == 130


def test_probe_recognises_controller_left_on_service_link(monkeypatch: pytest.MonkeyPatch) -> None:
    use_simulator(monkeypatch, on_service_link=True)
    code, output = run("probe", "--simulate")
    assert code == 1
    assert "still there from an\nearlier calibration attempt" in output
    assert "Switch the battery OFF" in output
    assert "did not answer on the 1200 baud display link" not in output


def test_calibrate_stops_when_controller_is_still_on_service_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sims = use_simulator(monkeypatch, on_service_link=True)
    code, output = run("calibrate", "--simulate", "--yes")
    assert code == 1
    assert "still there from an\nearlier calibration attempt" in output
    assert CALIBRATION_COMMAND.hex(" ") not in sent(sims[0])
