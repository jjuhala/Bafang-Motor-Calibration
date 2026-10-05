"""Command-line interface: ``bafang-cal``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import TextIO

from . import __version__
from .codes import (
    CALIBRATION_DONE,
    CALIBRATION_FAILURES,
    CalibrationState,
    calibration_state,
    describe_calibration_status,
    describe_status,
    display_code,
)
from .protocol import (
    CALIBRATION_COMMAND,
    CALIBRATION_DISPLAY_ASSIST_CODES,
    NORMAL_BAUDRATE,
    SERVICE_BAUDRATE,
    SERVICE_MODE_REQUEST,
)
from .service import (
    ProbeResult,
    ScheduleRunner,
    Telemetry,
    answers_at,
    calibration_schedule,
    enter_service_mode,
    probe,
    riding_schedule,
)
from .simulator import SimulatedController
from .transport import (
    Clock,
    SerialTransport,
    SystemClock,
    TracingTransport,
    Transport,
    TransportError,
    list_serial_ports,
)

__all__ = ["main"]

DEFAULT_CALIBRATION_SECONDS = 60.0
DEFAULT_NO_REPLY_TIMEOUT = 5.0
RIDING_PREAMBLE_SECONDS = 2.0

CALIBRATION_WARNING = """\
Bafang M620 (G510) rotor-position calibration
=============================================
Bafang documents this procedure for the UART controller CR R10M.1000.SN.U 1.5.
Before you continue, make sure that:
  * motor and controller are fully assembled and every connector is plugged in
    (never plug or unplug connectors with the battery switched on);
  * the bike is on a stand and the CHAIN IS OFF the chainring (or at least the
    rear wheel is off the ground) -- the motor turns by itself, and fast;
  * hands, clothing and cables are clear of the chainring and cranks;
  * the battery is at least half charged, and only GND, TX and RX of the
    USB-UART adapter are connected (see docs/wiring.md).
This sends the same bytes as Bafang's C961 calibration display. It is
unofficial and comes with NO WARRANTY -- you use it at your own risk.
"""

NO_RESPONSE_HELP = """\
The controller did not answer on the 1200 baud display link. Check that:
  * the controller is switched on -- without a display the power-lock wire has
    to be connected to battery + (see docs/wiring.md);
  * adapter TX goes to the controller's RX and adapter RX to its TX (try
    swapping them) and the grounds are connected;
  * the right serial port is selected (`bafang-cal ports` lists them);
  * the motor is a UART version -- CAN-bus motors (DP C18.CAN etc.) do not
    speak this protocol.
Use --skip-check to send the calibration commands anyway.
"""

STILL_ON_SERVICE_LINK = """\
The controller answers on the 9600 baud service link: it is still there from an
earlier calibration attempt and ignores the normal 1200 baud link until it is
restarted. Switch the battery OFF, wait about 10 seconds, switch it ON again and
retry.
"""

NEXT_STEPS = """\
Next steps:
  1. Switch the battery OFF, wait about 10 seconds and switch it ON again. The
     controller stays on the 9600 baud service link until it is restarted
     (Bafang: "after restarting the display, the motor can work normally").
  2. Reconnect the normal display and check that no error code is shown.
  3. Put the chain back on, lift the rear wheel and check that the assist works
     smoothly before riding.
"""


# --------------------------------------------------------------------- output


class LiveLine:
    """Prints a status line in place on a terminal, or periodically otherwise."""

    def __init__(self, stream: TextIO, interval: float = 2.0) -> None:
        self.stream = stream
        self.interactive = stream.isatty()
        self.interval = interval
        self._last_printed: float | None = None
        self._width = 0

    def update(self, elapsed: float, text: str) -> None:
        if self.interactive:
            padded = text.ljust(self._width)
            self._width = len(text)
            self.stream.write("\r" + padded)
            self.stream.flush()
        elif self._last_printed is None or elapsed - self._last_printed >= self.interval:
            self._last_printed = elapsed
            self.stream.write(text + "\n")
            self.stream.flush()

    def finish(self) -> None:
        if self.interactive and self._width:
            self.stream.write("\n")
            self.stream.flush()


def _calibration_line(t: Telemetry) -> str:
    rpm = "--" if t.rpm is None else str(t.rpm)
    status = "--" if t.status is None else describe_calibration_status(t.status)
    return (
        f"{t.elapsed:6.1f} s | speed {rpm:>4} | status {status} | "
        f"sent {t.commands_sent} | replies {t.replies_ok} ok, "
        f"{t.replies_missing} missing, {t.replies_invalid} bad"
    )


def _print_probe(result: ProbeResult, out: TextIO) -> None:
    def show(name: str, value: str | None) -> None:
        if value is None:
            value = f"no valid reply ({result.errors.get(name, 'no reply')})"
        out.write(f"  {name:<8} {value}\n")

    show("status", None if result.status is None else describe_status(result.status.code))
    show("battery", None if result.battery is None else f"{result.battery.percent} %")
    show("speed", None if result.speed is None else f"{result.speed.rpm} rpm (raw)")
    out.flush()


def _confirm(prompt: str, out: TextIO) -> bool:
    out.write(prompt)
    out.flush()
    try:
        answer = input()
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _calibration_outcome(t: Telemetry) -> CalibrationState:
    """Overall result of a calibration run, judged by the status codes seen."""
    if CALIBRATION_DONE in t.statuses_seen:
        return CalibrationState.DONE
    if any(code in CALIBRATION_FAILURES for code in t.statuses_seen):
        return CalibrationState.FAILED
    if any(calibration_state(code) is CalibrationState.IN_PROGRESS for code in t.statuses_seen):
        return CalibrationState.IN_PROGRESS
    return CalibrationState.NOT_STARTED


def _report_calibration(t: Telemetry, out: TextIO) -> int:
    """Explain the outcome of a calibration run; return the exit status."""
    out.write(
        f"\n{t.commands_sent} calibration commands sent in {t.elapsed:.1f} s, "
        f"{t.replies_ok}/{t.polls_sent} replies valid, highest speed reading {t.max_rpm}.\n"
    )
    if t.statuses_seen:
        seen = ", ".join(display_code(code) for code in t.statuses_seen)
        out.write(f"Status codes seen: {seen}\n")

    outcome = _calibration_outcome(t)
    last = "--" if t.status is None else describe_calibration_status(t.status)
    if outcome is CalibrationState.DONE:
        out.write("\nCalibration SUCCEEDED: the controller reported 44 (calibration finished).\n")
    elif outcome is CalibrationState.FAILED:
        out.write(
            f"\nCalibration FAILED: the controller reported {last}.\n"
            "Bafang's advice: run the calibration again. If it keeps failing, the\n"
            "controller is defective or not supported (the procedure is documented\n"
            "for CR R10M.1000.SN.U 1.5 only). Check the motor connectors first.\n"
        )
    elif not t.got_replies:
        out.write(
            f"\nCalibration NOT DONE: the controller never answered at {SERVICE_BAUDRATE} baud,\n"
            "so it did not enter the service mode. Check that this is a UART G510\n"
            "controller, then power-cycle and try again with --trace.\n"
        )
    elif outcome is CalibrationState.IN_PROGRESS:
        stopped = "you stopped it" if t.interrupted else "the time limit was reached"
        out.write(
            f"\nCalibration NOT CONFIRMED: the controller was still calibrating ({last})\n"
            f"when {stopped}. Power-cycle and run it again, if necessary with a\n"
            "longer --duration.\n"
        )
    else:
        out.write(
            "\nCalibration NOT CONFIRMED: the controller answered, but never reported a\n"
            "calibration status (40 ...). It may not support this calibration -- Bafang\n"
            "documents it only for CR R10M.1000.SN.U 1.5 -- or it ignored the command\n"
            "(the assist level must not be 0).\n"
        )
    out.write("\n" + NEXT_STEPS)
    return 0 if outcome is CalibrationState.DONE else 1


# ------------------------------------------------------------------ transport


def _single_usb_port(out: TextIO) -> str:
    """Pick the only USB serial adapter, or explain why that is not possible."""
    ports = list_serial_ports(usb_only=True)
    if len(ports) == 1:
        device, description = ports[0]
        out.write(f"Using serial port {device} ({description or 'USB serial'}).\n")
        return device
    if not ports:
        raise TransportError("no USB serial adapter found -- plug it in, or pass --port explicitly")
    names = ", ".join(device for device, _ in ports)
    raise TransportError(f"several serial adapters found ({names}) -- choose one with --port")


@contextmanager
def _open_transport(args: argparse.Namespace, clock: Clock) -> Iterator[Transport]:
    transport: Transport
    if args.simulate:
        transport = SimulatedController(clock)
    else:
        transport = SerialTransport(args.port or _single_usb_port(args.out), NORMAL_BAUDRATE)

    trace_stream: TextIO | None = None
    try:
        if args.trace:
            trace_stream = (
                sys.stderr if args.trace == "-" else open(args.trace, "w", encoding="utf-8")  # noqa: SIM115
            )
            transport = TracingTransport(transport, trace_stream, clock)
        yield transport
    finally:
        transport.close()
        if trace_stream is not None and trace_stream is not sys.stderr:
            trace_stream.close()


def _silence_help(transport: Transport) -> str:
    """Explain why the controller is silent at 1200 baud."""
    if answers_at(transport, SERVICE_BAUDRATE):
        return STILL_ON_SERVICE_LINK
    return NO_RESPONSE_HELP


def _preflight(transport: Transport, clock: Clock, args: argparse.Namespace, out: TextIO) -> bool:
    if args.skip_check:
        return True
    out.write(f"Checking the controller on the normal {NORMAL_BAUDRATE} baud link ...\n")
    result = probe(transport, clock)
    _print_probe(result, out)
    if not result.responded:
        out.write("\n" + _silence_help(transport))
        return False
    if result.status is not None and result.status.is_error:
        out.write(
            "  note: the controller reports an error; after a rotor or controller\n"
            "        swap that is expected and is what the calibration should fix.\n"
        )
    return True


def _calibration_stop(no_reply_timeout: float) -> Callable[[Telemetry], bool]:
    """Stop on 44 (finished), on a failure code, or when the link stays silent."""

    def should_stop(t: Telemetry) -> bool:
        if t.status is not None and calibration_state(t.status) in (
            CalibrationState.DONE,
            CalibrationState.FAILED,
        ):
            return True
        return no_reply_timeout > 0 and not t.got_replies and t.elapsed >= no_reply_timeout

    return should_stop


# ------------------------------------------------------------------- commands


def _cmd_ports(args: argparse.Namespace, out: TextIO) -> int:
    ports = list_serial_ports()
    if not ports:
        out.write(
            "No serial ports found. Is the USB-UART adapter plugged in and its driver installed?\n"
        )
        return 1
    for device, description in ports:
        out.write(f"{device}\t{description}\n")
    return 0


def _cmd_probe(args: argparse.Namespace, out: TextIO) -> int:
    clock = SystemClock()
    result = ProbeResult()
    with _open_transport(args, clock) as transport:
        try:
            while True:
                out.write(f"Reading the controller at {NORMAL_BAUDRATE} baud:\n")
                result = probe(transport, clock)
                _print_probe(result, out)
                if not args.watch:
                    break
                clock.sleep(1.0)
        except KeyboardInterrupt:
            out.write("\n")
        if not result.responded:
            out.write("\n" + _silence_help(transport))
            return 1
    return 0


def _cmd_calibrate(args: argparse.Namespace, out: TextIO) -> int:
    out.write(CALIBRATION_WARNING + "\n")
    if not args.yes and not _confirm("Type 'yes' to start the calibration: ", out):
        out.write("Aborted -- nothing was sent.\n")
        return 1

    clock = SystemClock()
    with _open_transport(args, clock) as transport:
        if not _preflight(transport, clock, args, out):
            return 1

        runner = ScheduleRunner(transport, clock)
        code = CALIBRATION_DISPLAY_ASSIST_CODES[args.assist_level]
        out.write(
            f"Showing assist level {args.assist_level} (code {code:02X}) for "
            f"{RIDING_PREAMBLE_SECONDS:g} s, like the calibration display's riding screen ...\n"
        )
        preamble = runner.run(riding_schedule(args.assist_level), duration=RIDING_PREAMBLE_SECONDS)
        if preamble.interrupted:
            out.write("Stopped -- the controller is still on the normal link.\n")
            return 1

        out.write(
            f"Requesting the {SERVICE_BAUDRATE} baud service link "
            f"({SERVICE_MODE_REQUEST.hex(' ').upper()} x3) ...\n"
        )
        enter_service_mode(transport, clock)
        out.write(
            f"Calibrating: sending {CALIBRATION_COMMAND.hex(' ').upper()} every 63 ms "
            f"(up to {args.duration:g} s, Ctrl+C to stop).\n"
            "Expected: status 40, then the motor turns while 41, 43, 95-98 follow;\n"
            "44 means finished.\n"
        )
        live = LiveLine(out)
        telemetry = runner.run(
            calibration_schedule(),
            duration=args.duration,
            on_cycle=lambda t: live.update(t.elapsed, _calibration_line(t)),
            should_stop=_calibration_stop(args.no_reply_timeout),
        )
        live.finish()

    return _report_calibration(telemetry, out)


# --------------------------------------------------------------------- parser


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def _non_negative_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bafang-cal",
        description=(
            "Calibrate a Bafang M620 (G510) mid-drive over a USB-UART cable, "
            "replacing the C961 calibration display."
        ),
        epilog="Documentation: https://github.com/jjuhala/Bafang-Motor-Calibration",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    link = argparse.ArgumentParser(add_help=False)
    link.add_argument(
        "-p",
        "--port",
        help=(
            "serial port of the USB-UART adapter, e.g. /dev/ttyUSB0 or COM3 "
            "(default: the only USB serial adapter connected)"
        ),
    )
    link.add_argument(
        "--simulate",
        action="store_true",
        help="talk to a built-in simulated controller instead of a serial port (dry run)",
    )
    link.add_argument(
        "--trace",
        metavar="FILE",
        help="write a timestamped TX/RX byte trace to FILE ('-' for stderr)",
    )

    p_ports = sub.add_parser("ports", help="list serial ports")
    p_ports.set_defaults(func=_cmd_ports)

    p_probe = sub.add_parser(
        "probe",
        parents=[link],
        help="read status, battery and speed at 1200 baud (read-only wiring check)",
    )
    p_probe.add_argument("--watch", action="store_true", help="repeat every second until Ctrl+C")
    p_probe.set_defaults(func=_cmd_probe)

    p_cal = sub.add_parser(
        "calibrate",
        parents=[link],
        help="run the G510 rotor-position calibration",
    )
    p_cal.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p_cal.add_argument(
        "--duration",
        type=_positive_seconds,
        default=DEFAULT_CALIBRATION_SECONDS,
        metavar="SECONDS",
        help=(
            "give up if the controller has not reported 44 (finished) after this long "
            f"(default: {DEFAULT_CALIBRATION_SECONDS:g})"
        ),
    )
    p_cal.add_argument(
        "--assist-level",
        type=int,
        choices=sorted(CALIBRATION_DISPLAY_ASSIST_CODES),
        default=1,
        help="assist level (0-3 scale) reported before calibrating; must not be 0 (default: 1)",
    )
    p_cal.add_argument(
        "--skip-check",
        action="store_true",
        help="do not verify that the controller answers at 1200 baud first",
    )
    p_cal.add_argument(
        "--no-reply-timeout",
        type=_non_negative_seconds,
        default=DEFAULT_NO_REPLY_TIMEOUT,
        metavar="SECONDS",
        help=(
            f"give up if the controller has not answered on the {SERVICE_BAUDRATE} baud link "
            f"after this long (default: {DEFAULT_NO_REPLY_TIMEOUT:g}; 0 = never give up)"
        ),
    )
    p_cal.set_defaults(func=_cmd_calibrate)
    return parser


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    """Entry point of the ``bafang-cal`` console script."""
    stream = out or sys.stdout
    parser = build_parser()
    args = parser.parse_args(argv)
    args.out = stream
    try:
        return int(args.func(args, stream))
    except TransportError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130
