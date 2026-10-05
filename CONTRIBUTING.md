# Contributing

Thanks for helping! The most valuable contributions right now are **field
reports**: the protocol was recovered from the display firmware, and every run
on a real motor teaches us more about how controllers respond.

## Reporting a calibration run

1. Run the tool with a trace file:

   ```console
   bafang-cal calibrate --port /dev/ttyUSB0 --trace run.trace
   ```

2. Open a [calibration report](https://github.com/jjuhala/Bafang-Motor-Calibration/issues/new?template=calibration_report.yml)
   and include the console output, the trace file, the motor/controller label
   codes and what the motor physically did.

Never attach Bafang firmware files to issues — reference them by SHA-256 instead
(`sha256sum <file>`), and use `tools/analyze_firmware.py` to share results.

## Development setup

Requires Python 3.10 or newer.

```console
git clone https://github.com/jjuhala/Bafang-Motor-Calibration.git
cd Bafang-Motor-Calibration
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e . --group dev   # pip >= 25.1; or: uv sync
```

Run the same checks as CI before opening a pull request:

```console
ruff check .
ruff format --check .
mypy
pytest --cov
```

Optionally install the git hooks: `pip install pre-commit && pre-commit install`.

## Project layout

```text
src/bafang_cal/
  protocol.py    frame builders, checksums and reply parsers (pure functions)
  service.py     baud switch, slot scheduler, calibration / motor-test cycles, probe
  transport.py   pyserial transport, byte tracer, clock abstraction
  simulator.py   simulated controller used by --simulate and the tests
  codes.py       status / error code descriptions
  cli.py         the bafang-cal command
tools/analyze_firmware.py   verifies protocol constants against a firmware image
docs/                       protocol reference, firmware analysis, wiring guide
```

## Guidelines

- **Wire-level changes need evidence.** If a change alters bytes or timing on the
  wire, cite where the new behaviour comes from (firmware address, a trace from a
  real controller, official documentation) and update `docs/protocol.md`.
- Keep `protocol.py` free of I/O so it stays trivially testable.
- New behaviour comes with tests; the simulator plus `ManualClock` makes timing
  tests instant and deterministic.
- Keep the safety prompts intact. This tool moves motors.
- Use clear commit messages in the imperative mood ("Add …", "Fix …").

## Releasing

1. Update `__version__` in `src/bafang_cal/__init__.py` and `CHANGELOG.md`.
2. Tag `vX.Y.Z` on `main`; the CI build job produces the wheel and sdist.
