# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- Changing the line speed now closes and reopens the serial port. Some
  USB-serial drivers accept a speed change on an open port but keep the old
  speed: with a CH340 adapter on macOS every service-link frame still went out
  at 1200 baud, so the controller (already on 9600) never answered or
  calibrated.

### Added

- `probe` and the `calibrate` pre-check recognise a controller that is still on
  the 9600 baud service link from an earlier attempt and ask for a power cycle
  instead of reporting a wiring problem.

## [0.1.0] - 2026-10-05

### Added

- `bafang-cal calibrate`: runs the Bafang M620 (G510) rotor-position
  calibration over a USB-UART cable by replaying the C961 calibration display:
  2 s of its riding screen at assist level 1, `11 51 25 80 F6` ×3 at 1200 baud,
  then `16 A0 01 B7` every 63 ms at 9600 baud while polling speed and status,
  with the display's cycle timing (512 ms when the controller answers the speed
  request, 500 ms when it does not).
- Automatic result detection from the controller's status codes (40 … 98
  progress, 44 finished, 51/53/63/73/84/85 failed), with explanations for every
  other outcome and meaningful exit codes.
- `bafang-cal probe`: read-only wiring check on the normal 1200 baud link.
- `bafang-cal ports`, and automatic port selection when exactly one USB serial
  adapter is connected.
- `--simulate` dry-run mode backed by a controller simulator, and `--trace`
  byte-level logging.
- `tools/analyze_firmware.py`: cross-checks every protocol constant against a
  firmware image.
- Documentation: protocol reference, firmware analysis, wiring guide.

[Unreleased]: https://github.com/jjuhala/Bafang-Motor-Calibration/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jjuhala/Bafang-Motor-Calibration/releases/tag/v0.1.0
