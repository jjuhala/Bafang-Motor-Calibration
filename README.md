# Bafang Motor Calibration

[![CI](https://github.com/jjuhala/Bafang-Motor-Calibration/actions/workflows/ci.yml/badge.svg)](https://github.com/jjuhala/Bafang-Motor-Calibration/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Calibrate a Bafang M620 (G510 / "Ultra") mid-drive motor from your computer
with a cheap USB-UART cable — no C961 calibration display required.**

After the rotor or the controller of an M620 has been replaced, the controller
has to re-learn the position of the rotor (the angle between the magnet on the
rotor shaft and the position sensor on the controller board). Until it does,
the motor vibrates, runs rough or backwards, draws too much current and gets
hot. On UART motors Bafang does this with a special C961 display running
calibration firmware. `bafang-cal` re-implements exactly what that display
sends — byte for byte, with the same timing — recovered by disassembling its
firmware ([how](docs/firmware-analysis.md)), and reads the controller's
progress codes to tell you whether it worked.

```text
$ bafang-cal calibrate --simulate --yes        # dry run against the built-in simulator
...
Showing assist level 1 (code 0C) for 2 s, like the calibration display's riding screen ...
Requesting the 9600 baud service link (11 51 25 80 F6 x3) ...
Calibrating: sending 16 A0 01 B7 every 63 ms (up to 60 s, Ctrl+C to stop).
   3.0 s | speed   45 | status 43: calibrating | sent 42 | replies 12 ok, 0 missing, 0 bad
   8.0 s | speed    0 | status 44: calibration finished | sent 112 | replies 32 ok, 0 missing, 0 bad

Status codes seen: 01, 40, 41, 43, 95, 96, 97, 98, 44
Calibration SUCCEEDED: the controller reported 44 (calibration finished).
```

(Output of the simulator, not of a real motor.)

> [!WARNING]
> This is an **unofficial** tool, provided without any warranty. It talks
> directly to a motor controller connected to a high-current battery, and **the
> motor turns on its own — fast — while calibrating**. Read the
> [safety](#safety) section first. If you can get to a Bafang dealer, that is
> the supported route.

## Contents

- [Compatibility](#compatibility)
- [What you need](#what-you-need)
- [Installation](#installation)
- [Wiring](#wiring)
- [Calibrating](#calibrating)
- [How it works](#how-it-works)
- [Safety](#safety)
- [Troubleshooting](#troubleshooting)
- [Status and limitations](#status-and-limitations)
- [Sources](#sources) · [Contributing](#contributing) · [License](#license)

## Compatibility

| | |
|---|---|
| Motor | Bafang M620 / G510 ("Ultra") with the **UART** display link (built roughly 2018–2020) |
| Controller | Bafang documents the procedure for **CR R10M.1000.SN.U 1.5**; other controller firmware may refuse it |
| Not supported | CAN-bus M620s (`MM G510.1000.C`, `DP C18.CAN`, …) — use BESST or another CAN tool |

How to tell UART from CAN: [docs/wiring.md](docs/wiring.md#uart-or-can).

## What you need

- A **USB-UART (USB-TTL) adapter**: CP2102, CH340, FT232R or PL2303 based.
  A Bafang USB programming cable for BBS motors works and already has the right
  plug.
- A way to connect to the motor's display connector — a programming cable, a
  display extension cable cut in half, or careful probing. See
  [docs/wiring.md](docs/wiring.md).
- A charged battery (at least half full) and a bike stand.
- Python 3.10+ on Windows, macOS or Linux.

## Installation

```console
pipx install git+https://github.com/jjuhala/Bafang-Motor-Calibration.git
```

or, with plain pip (inside a virtual environment):

```console
python -m pip install git+https://github.com/jjuhala/Bafang-Motor-Calibration.git
```

Check it works — this needs no hardware:

```console
bafang-cal --version
bafang-cal calibrate --simulate
```

On Linux, add yourself to the `dialout` (Debian/Ubuntu) or `uucp` (Arch) group
to access serial ports without root. On Windows you may need the
[CH340](https://www.wch-ic.com/downloads/CH341SER_EXE.html) or
[CP210x](https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers)
driver for your adapter.

## Wiring

**Full details, pinouts and a pre-flight checklist: [docs/wiring.md](docs/wiring.md).**
The adapter replaces the display — unplug the display first.

```text
 USB-UART adapter              motor display connector (UART, round 5-pin)
 ----------------              -------------------------------------------
      GND  ------------------  GND                          (black)
      RX   ------------------  data, controller -> display  (usually green)
      TX   ------------------  data, display -> controller  (usually yellow)
      VCC  x  not connected
                               battery +   (red)   --+
                               power lock  (blue)  --+-- bridge: switches the
                                                         controller on
```

> [!CAUTION]
> The red wire carries **full battery voltage (36–52 V)**. It must never touch
> the USB adapter. Only GND, TX and RX go to the adapter. Colours vary between
> harnesses — identify the pins with a multimeter.

## Calibrating

1. **Prepare the bike.** Put it on a stand and take the **chain off the
   chainring** (at the very least lift the rear wheel): the motor turns by
   itself during calibration and drives the chainring. Keep hands, clothing and
   cables clear. Charge the battery to at least 50 %.
2. **Connect** the adapter as above with the battery off, then switch the
   battery on.
3. **Check the wiring** — read-only, changes nothing:

   ```console
   bafang-cal ports                # lists serial ports; --port is optional with one adapter
   bafang-cal probe
   ```

   You should see a status code and the battery level. Error 08 (hall/position
   sensor) is normal after a rotor swap.
4. **Calibrate:**

   ```console
   bafang-cal calibrate --trace calibration.trace
   ```

   Read the checklist and type `yes`. The tool then does what the C961 display
   does when you hold *Power* + *Down*:
   - it reports assist level 1 for two seconds (the calibration does not start
     at level 0),
   - switches the controller to its 9600 baud service link,
   - sends the calibration command every 63 ms while it shows the controller's
     status. The motor should start turning after status **40**, the status
     runs through **41, 43, 95, 96, 97, 98** and **44 means finished** — the
     tool stops by itself and reports success. It usually takes a few seconds.
5. **Power-cycle:** switch the battery off and on again. The controller stays
   on the service link until it is restarted.
6. **Test:** reconnect the display, check that no error is shown, put the
   chain back on and try the assist with the rear wheel lifted before riding.

If the controller reports **51, 53, 63, 73, 84 or 85** the calibration failed:
power-cycle and run it again. Bafang says that if it keeps failing the
controller is defective (or not one that supports this procedure).

| Option | Meaning |
|--------|---------|
| `-p`, `--port PORT` | serial port, e.g. `COM3` or `/dev/ttyUSB0` (default: the only USB adapter) |
| `--trace FILE` | write a timestamped byte trace — please attach it to reports |
| `--duration SECONDS` | give up if 44 has not been reported after this long (default 60) |
| `--assist-level {1,2,3}` | assist level reported before calibrating (default 1) |
| `--skip-check` | do not require an answer on the 1200 baud link first |
| `--no-reply-timeout SECONDS` | give up when the service link stays silent (default 5, `0` = never) |
| `--simulate` | use the built-in simulated controller instead of hardware |
| `-y`, `--yes` | skip the confirmation prompt |

`python -m bafang_cal` works as well as `bafang-cal`. Exit status: 0 = the
controller reported 44, 1 = anything else.

## How it works

The calibration display talks to the controller at 1200 baud like any Bafang
display. When you hold *Power* + *Down* it:

1. sends `11 51 25 80 F6` three times — the standard "connect" command
   (`11 51 04 B0 05` = 1200 baud) with **9600** baud as its argument; Bafang's
   BESST software does the same before talking to the controller at 9600 baud;
2. switches its own UART to 9600 baud 150 ms later;
3. repeats a 500 ms cycle: read the speed (`11 20`), read the status (`11 08`,
   shown as "Er XX"), and send the **calibration command `16 A0 01 B7` every
   63 ms**.

Before that it has been showing its riding screen at assist level 1, which
sends `16 0B 0C 2D` every 500 ms; `bafang-cal` reproduces that too. The full
protocol is in [docs/protocol.md](docs/protocol.md), the evidence (code
addresses, reconstructed C) in
[docs/firmware-analysis.md](docs/firmware-analysis.md). If you own a copy of the
firmware, `python tools/analyze_firmware.py <file>` re-checks every constant the
tool uses.

The display has a second service function (*Power* + *Up*, a motor run test
with a temperature read-out). It is documented but deliberately **not**
offered by this tool: users report it heating a motor beyond 95 °C, and it is
not needed for the calibration.

## Safety

- The motor turns **by itself and fast** during calibration and drives the
  chainring. Chain off (or rear wheel off the ground), bike on a stand, hands
  clear.
- Never connect battery voltage (red wire) to the USB adapter or the computer.
  Insulate the bridged battery +/power-lock wires. A USB isolator between the
  computer and the adapter adds a margin of safety.
- Do not plug or unplug connectors while the battery is on.
- A badly calibrated motor overheats quickly under load. Do not ride until the
  calibration has reported 44 and the motor runs smoothly on the stand.

## Troubleshooting

| Symptom | What to check |
|---------|---------------|
| `probe` gets no reply | controller not switched on (power-lock bridge), TX/RX swapped (swap them — harmless), wrong port, missing GND, CAN-version motor |
| "never answered at 9600 baud" | the controller did not take the service-link request — CAN controller, or firmware without it. Power-cycle and retry with `--trace`, then open an issue |
| "never reported a calibration status" | check the controller label — firmware other than CR R10M.1000.SN.U 1.5 may not support the procedure; power-cycle and try once more |
| Failure code 51/53/63/73/84/85 | re-seat the motor connectors, power-cycle, run again; persistent failures mean a defective or unsupported controller |
| Garbage / checksum errors | try another adapter or its 5 V setting, keep wires short, avoid USB hubs; on Linux lower the FTDI latency timer |
| Error 30 on the display afterwards | the controller is still on the 9600 baud link — power-cycle it |
| Motor still rough after "44" | check that the chain was off and nothing braked the motor, run it again, and file a [calibration report](https://github.com/jjuhala/Bafang-Motor-Calibration/issues/new?template=calibration_report.yml) |

## Status and limitations

- The display side of the protocol is fully decoded and replicated, and checked
  against the firmware and Bafang's published procedure. **It has not yet been
  confirmed on real hardware with this tool** — field reports (success or
  failure, with `--trace`) are the most valuable contribution right now.
- UART controllers only. CAN-bus systems need a different tool.
- The C961 firmware is Bafang's property and is not included in this
  repository.

## Sources

- Bafang, *MM G510 控制器标定 / Ultra's Controller Calibration (CR R10M)* —
  the official instructions for the calibration display
  ([PDF](https://www.greenbikekit.com/upload/pdf/Bafang-M620-G510-UART-controller-calibration-LCD-.pdf)).
- The C961 "G510 calibration" display firmware (analysed here; not distributed).
- Bafang BESST source (via
  [OpenSourceEBike/Bafang_M500_M600](https://github.com/OpenSourceEBike/Bafang_M500_M600))
  for the `11 51 25 80 F6` baud switch, and community protocol work:
  [bbs-fw](https://github.com/danielnilsson9/bbs-fw),
  [OpenBafangTool](https://github.com/andrey-pr/OpenBafangTool),
  [ebike-protocols](https://github.com/speedysheep/ebike-protocols).
- Forum reports on electricbikereview.com, electricbike.com and
  endless-sphere.com about M620 rotor/controller swaps and the calibration
  display.

## Contributing

Bug reports, field reports and pull requests are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md). Development quick start:

```console
python -m pip install -e . --group dev
pytest && ruff check . && mypy
```

## License

[MIT](LICENSE). Bafang, M620, Ultra and BESST are trademarks of their
respective owners; this project is not affiliated with or endorsed by Bafang.
