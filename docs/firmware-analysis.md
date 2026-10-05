# Firmware analysis: C961 "G510 calibration" display

This document records how the protocol implemented by `bafang-cal` was recovered
from the firmware of Bafang's C961 calibration display. Everything marked
**(firmware)** was read directly from the machine code; anything that is an
interpretation is labelled as such. Open questions are collected at the end.

> The firmware itself is copyrighted by Bafang and is **not** part of this
> repository. If you have a copy you can re-run the key checks with
> `python tools/analyze_firmware.py <file>` (see [Reproducing](#reproducing-the-analysis)).

## The image

| Property  | Value |
|-----------|-------|
| File      | `c961_display_g510_cal` (Intel HEX, distributed as a `.txt`) |
| SHA-256   | `a67025f73719a0f2e6b796a09c9b0341eb80ac9f34e1e1506e0c442fee3ceffc` |
| Content   | 16,040 bytes of code/data at `0x0000`–`0x3EA7` |
| CPU       | 8051 (MCS-51) |
| Toolchain | Keil C51 (runtime helpers `?C?ICASE` at `0x213E`, `?C?UIDIV` at `0x1E5B`, generic pointers in R1–R3) |

### Microcontroller and clock

* A community member read the display's MCU with STC-ISP: an **STC12C5616AD**
  (1T 8051, 16 KB flash) at 18.432 MHz. The code agrees: in-application
  programming registers at `0xE2`–`0xE7` with the trigger sequence
  `0x46, 0xB9` (routine at `0x3803`) and STC port-mode registers written at
  start-up (`0x3741`). The display keeps its settings (wheel size, speed limit,
  …) in that EEPROM. Its timers run in the classic 12-clock mode.
* The UART initialisation (below) computes `TH1 = 256 − 48000 / baud` with
  `SMOD = 0`. For a classic 12-clock 8051 that means a crystal of
  48000 × 384 = **18.432 MHz**, which gives exact 1200 and 9600 baud.
* Timer 0 is reloaded with `0xFA00` (`0x379F`): 1536 machine cycles =
  **1.000 ms** at 18.432 MHz. Both derivations agree, which is good evidence for
  the clock.
* `main` (`0x3842`) initialises the hardware, sets the UART to **1200 baud**,
  enables interrupts and then just idles (`ORL PCON,#1` loop at `0x387B`).
  **All** application logic runs inside the 1 ms Timer 0 interrupt
  (`0x3782` → `0x08DA`).

## UART driver (firmware)

| Address  | Function | Notes |
|----------|----------|-------|
| `0x37C3` | `uart_init(baud)` | `SCON = 0x50` (mode 1, 8N1, receiver on), Timer 1 mode 2 auto-reload, `TH1 = TL1 = 256 − 48000/baud`, enables `ES` |
| `0x3357` | UART ISR | 32-byte RX ring at XDATA `0x004F`, 32-byte TX ring at XDATA `0x006F` |
| `0x34E8` | `uart_write(ptr, len)` | copies into the TX ring, kicks `SBUF` if idle |
| `0x31F5` | `uart_read(ptr, max) → n` | copies up to `max` bytes out of the RX ring, returns the count |

`uart_init` is called from exactly two places:

| Call site | Argument | Meaning |
|-----------|----------|---------|
| `0x3864` (main)          | `0x04B0` = **1200**  | normal display link after power-on |
| `0x0426` (service mode)  | `0x2580` = **9600**  | after requesting the service link |

There is no other call, so **the display never returns to 1200 baud** once it has
entered the service mode — it has to be power-cycled.

## Buttons and key codes (firmware)

The key scanner (`0x18C0`) debounces three buttons — *Power*, *Up* (P2.5) and
*Down* (P2.1) — in 8 ms steps; a long press is 220 steps ≈ 1.76 s (100 steps
when the display is off). It returns these codes:

| Code | Gesture | Used for |
|------|---------|----------|
| 1  | Power short | toggles the motor-test run flag in service mode |
| 2  | Power long  | power off / on |
| 3  | Up short    | assist level + |
| 4  | Up long     | lights |
| 5  | Down short  | assist level − |
| 6  | Down long   | walk assist |
| 7  | Power + Up held long | enter/leave service mode, **motor test** |
| 8  | tap Up while holding Power | enter/leave service mode, **motor test** |
| 9  | Power + Down held long | enter/leave service mode, **calibration** |
| 10 | Up + Down held long | leave the riding screen for the settings menu |
| 11 | all three held long | (unused by the protocol code) |
| 12 | hold Up + Down, press Power 8× | settings |

## Display modes (firmware)

XDATA `0x0014` holds the mode:

| Value | Mode | UART |
|-------|------|------|
| 0 | settings menu (speed limit, wheel size, …) | normal 1200 baud polling |
| 1 | riding screen — **the power-on default** (`0x3BD5`), at assist level 1 (`0x09D2`) | normal 1200 baud polling |
| 2 | service mode | 9600 baud service cycle |

From the riding screen, key 9 (*Power + Down*, held ≈ 2 s) enters service mode
with XDATA `0x000A` ("level") = 0 — the **calibration** sub-mode. Keys 7 or 8
enter it with level = 1 — the **motor-test** sub-mode. Pressing 7, 8 or 9 again
returns to the riding screen (`0x0454`) without restoring 1200 baud.

## Normal 1200 baud protocol (firmware)

Outside service mode the display runs the familiar Bafang display protocol.
A 1 ms counter (XDATA `0x0033:0x0034`) drives two Keil `switch` tables — one for
sending (`0x0C6B`) and one for reading replies (`0x0EA1`):

Steady state (the counter runs from 500 to 1000 and is then reset to 500, so
the cycle repeats every 500 ms):

| t / ms | Sent | Reply read at | Reply accepted when |
|-------:|------|--------------:|---------------------|
| 500 | `16 0B <code> <chk>` assist level, `chk = 0x21 + code` | — | — |
|     | `16 1A F1` / `16 1A F0` lights on / off (no checksum) | — | — |
|     | `16 1F <hi> <lo> <chk>` speed limit as wheel rpm (km/h × 208 / wheel inches) | — | — |
| 620 | `11 20` speed | 700 | 3 bytes, `chk = 0x20 + hi + lo` |
| 700 | `11 08` status | 800 | 1 byte; shown once three consecutive reads agree, codes < 4 hidden |
| 800 | `11 11` battery % | 900 | 2 bytes, `chk = value` |
| 900 | `11 31` moving | 1000 | 2 bytes, `chk = value`; `0x31` = moving (keeps the display awake) |
| 10000 | `11 90` handshake | 10200 | must be exactly `90 40 D0`, else retried up to 8× |

The assist byte comes from a table at `0x39AE` indexed by the number of levels
(XDATA `0x000E`, factory default 3 — a "0-3 level" display) and the current
level: on the 3-level scale level 1 → `0x0C`, 2 → `0x02`, 3 → `0x03`, 0 →
`0x00`; walk assist sends `0x06`. So the calibration display, freshly switched
on, sends `16 0B 0C 2D`.

During the very first second the requests are also sent at 0/100/200/300/400 ms,
but those replies are never read. The handshake is started at power-on
(`0x0A95`) and after leaving the settings menu (`0x03ED`); failing it does not
block anything. Eight consecutive missing replies set XDATA `0x0012`, which the
display shows as **"Er 30"** (communication fault).

## Entering the service mode (firmware, `0x0711`–`0x077F`)

```text
0711  MOV  DPTR,#0014h ; mode = 2
0717  ...              ; level = (key == 9) ? 0 : 1
0729  ...              ; service tick counter t (XDATA 0017h:0018h) = 0
0730  LCALL 3E8Ch      ; flush the TX ring
0733  MOV  DPTR,#0048h ; frame = 11 51 25 80 F6
      MOV  A,#11h / MOVX ... A,#51h ... A,#25h ... A,#80h ... A,#0F6h
0749  uart_write(frame, 5)
0754  uart_write(frame, 5)
075F  uart_write(frame, 5)        ; sent three times back-to-back at 1200 baud
076B  ...              ; clear the LCD, show the level digit
077A  MOV  14h,#96h    ; countdown = 150 ticks (150 ms)
077D  CLR  20h.1       ; run flag off
```

`11 51` is the "connect" command that configuration tools send as
`11 51 04 B0 05` — `0x04B0` is 1200. Here the two payload bytes are
`0x2580` = **9600**, i.e. the display asks the controller to move to 9600 baud.
The checksum covers the bytes after the leading `0x11`:
`0x51 + 0x25 + 0x80 = 0xF6`.

While the countdown runs, every received byte is discarded (`0x0430`). When it
reaches zero (`0x041F`), the display calls `uart_init(9600)` and resets `t`.
Transmitting the 15 bytes takes 125 ms at 1200 baud, so the switch happens
≈ 25 ms after the last byte has left.

## The service cycle (firmware, `0x0410`–`0x06F0`)

Reconstructed C, one call per 1 ms tick (`key` is the code from the table above):

```c
if (countdown) {                         /* IRAM 14h */
    if (--countdown == 0) { uart_init(9600); t = 0; }
    while (uart_read(&junk, 1)) {}       /* discard */
    return;
}
if (key == 7 || key == 8 || key == 9) { leave_service_mode(); return; }
if (level != 0) {                        /* motor-test sub-mode only */
    if (key == UP   && level < 9) level++;
    if (key == DOWN && level > 1) level--;
    if (key == POWER) running = !running;
}

if      (t == 0)                  { send("11 20"); flush_rx(); }
else if (t == 12)                 { if (read(buf, 4) == 3 && buf[2] == 0x20 + buf[0] + buf[1])
                                        show_speed(buf[0] << 8 | buf[1]); }
else if (level && t == 75)        { send("11 12"); flush_rx(); }
else if (level && t == 85)        { if (read(buf, 4) == 3 && buf[2] == buf[0] + buf[1])
                                        show_temperature((int16_t)(buf[0] << 8 | buf[1])); }
else if (!level && t == 20)       { send("11 08"); flush_rx(); }
else if (!level && t == 30)       { if (read(buf, 4) == 1)
                                        show_error(buf[0] >= 3 ? buf[0] : 0); }
else if (level && (t & 0x3F) == 0 && running)
                                  { send(16 06 (level * 10 + 10) chk); }
else if (!level && t % 63 == 0)   { send("16 A0 01 B7"); }

if (++t >= 500) t = 0;
```

Resulting timelines (one 500 ms cycle):

**Calibration (level 0)** — what `bafang-cal calibrate` sends:

| t / ms | Frame | Reply |
|-------:|-------|-------|
| 0   | `11 20` | `hi lo chk`, `chk = 0x20 + hi + lo` |
| 20  | `11 08` | 1 status byte |
| 63, 126, 189, 252, 315, 378, 441 | `16 A0 01 B7` | not read (flushed before the next request) |

**Motor test (level 1–9)** — documented only; `bafang-cal` does not offer it (see the README):

| t / ms | Frame | Reply |
|-------:|-------|-------|
| 0   | `11 20` | `hi lo chk`, `chk = 0x20 + hi + lo` |
| 64, 128, 192, 256, 320, 384, 448 | `16 06 <pct> <chk>` (only while running), `pct = level × 10 + 10` = 20…100 | not read |
| 75  | `11 12` | `hi lo chk`, `chk = hi + lo` (no command byte!), signed |

The calibration command is not gated by anything: as soon as the calibration
sub-mode is entered the display sends `16 A0 01 B7` every 63 ms until the user
leaves the mode or switches the display off. In the motor-test sub-mode the
`16 06` frames only flow while the run flag (Power short press) is set.

## What the display shows in service mode (firmware)

The seven-segment font at `0x391C` (bits 7…0 = segments a, b, c, d, –, f, g, e)
decodes to `0–9`, then `E r i n H b L t P d J c o F C ° - ␣` for glyphs
`0x0A`–`0x1B`. With that:

* status bytes ≥ 3 (filter at `0x0605`) are shown as **`Er` + two hex digits**
  (renderer at `0x3542`); byte `0x21` appears as "Er 21". Bytes below 3 (e.g.
  `0x01`, normal) are hidden.
  Note that the riding screen hides everything below 4 — `0x03` (braking in
  normal use) *is* shown during calibration.
* the `11 20` value is converted to km/h with the configured wheel size and
  shown as the speed;
* the `11 12` value is shown as **`NNN°C`** (glyphs `0x19`, `0x18`), clamped to
  −99…999 — hence "temperature";
* the assist digit shows the level (0 in calibration).

## External confirmation

Independent sources agree with the reading above:

* **Bafang's instructions** ("MM G510 控制器标定 / Ultra's Controller
  Calibration (CR R10M)", for display software DPC01E10110.1): "Long press the
  power key and minus key to enter the test mode", after which the display
  shows "Er 40" with speed 0.0 and **"0" in the assist digit**, the motor turns
  by itself, the code runs through 41, 43, 95–98, and "Er 44 means the
  calibration is over"; 51/53/63/73/84/85 mean it failed. That is exactly
  sub-mode 0: key 9 (*Power* + *Down*), level 0, the `11 08` byte rendered as
  "Er XX" — so those codes are the controller's calibration progress.
* **A user report** of *Power* + *Up* giving a "1–10" menu with a temperature
  read-out that spun the motor matches sub-mode 1 (levels 1–9 plus the blinking
  "10", `11 12` rendered as "NNN°C").
* **Bafang's BESST software** sends `11 51 25 80 F6` and then switches its own
  UART to 9600 baud, confirming the meaning of the baud-rate request.
* Users report that the calibration does not start at **assist level 0** — the
  display powers up at level 1, so its riding screen has normally sent
  `16 0B 0C 2D` before the calibration is started.

## Open questions

These cannot be answered from the display firmware alone; field reports with
`--trace` output are very welcome (see [CONTRIBUTING.md](../CONTRIBUTING.md)):

1. What the individual progress codes (41, 43, 95–98) and failure codes stand
   for, and how long the calibration takes on different controllers (users say
   "a few seconds").
2. Whether the controller answers `11 51 25 80 F6` (the display ignores any
   answer).
3. Whether the controller needs the `16 A0 01` stream to continue, or would
   finish after a single command.
4. Whether the controller ever falls back to 1200 baud without a power cycle.
5. Whether `16 06` controls speed, torque or duty cycle in the motor test.

## Reproducing the analysis

```console
$ python tools/analyze_firmware.py c961_display_g510_cal.hex   # cross-checks every constant
$ objcopy -I ihex -O binary c961_display_g510_cal.hex fw.bin     # or: python -m intelhex.hex2bin
$ r2 -a 8051 -m 0 fw.bin                                         # radare2, then e.g. "s 0x0711; pd 40"
```

Addresses in this document are code addresses in that flat binary.
