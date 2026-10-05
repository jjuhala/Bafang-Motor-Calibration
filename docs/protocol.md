# Protocol reference

The Bafang display link is an asynchronous serial line, **8 data bits, no
parity, 1 stop bit**, no flow control. The display is always the master: it
sends a request and the controller may answer. Replies have no header — the
requester knows which reply to expect and how long it is.

Everything here was recovered from the C961 "G510 calibration" display
firmware; see [firmware-analysis.md](firmware-analysis.md) for addresses and
evidence.

## Frame formats

| Kind | Bytes | Checksum |
|------|-------|----------|
| Read | `11 <cmd>` | none |
| Connect / baud rate | `11 51 <baud hi> <baud lo> <chk>` | `(0x51 + hi + lo) & 0xFF` — the leading `0x11` is **not** included |
| Write | `16 <cmd> <payload…> <chk>` | `(0x16 + cmd + Σ payload) & 0xFF` — the `0x16` **is** included |
| Lights | `16 1A F1` (on) / `16 1A F0` (off) | none |

## Commands

| Request | Reply | Meaning |
|---------|-------|---------|
| `11 08` | `<status>` | status / error code. `0x01` normal, `0x03` braking, ≥ `0x04` error; the hex digits are the number printed on displays (`0x21` → "21") |
| `11 11` | `<pct> <pct>` | battery level in % |
| `11 12` | `<hi> <lo> <(hi+lo)&FF>` | temperature in °C, signed 16-bit (service link; shown as "NNN°C") |
| `11 20` | `<hi> <lo> <(0x20+hi+lo)&FF>` | wheel speed, rpm |
| `11 31` | `<flag> <flag>` | `0x31` while the bike is moving |
| `11 90` | `90 40 D0` | handshake the display performs after power-on (not required) |
| `11 51 04 B0 05` | — | connect at 1200 baud (used by configuration tools) |
| `11 51 25 80 F6` | — | **switch the controller to the 9600 baud service link** (BESST sends the same before using 9600 baud) |
| `16 0B <code> <chk>` | — | assist level; the 0-3 level calibration display sends `0C`, `02`, `03` for levels 1-3 and `00` for 0 |
| `16 1F <hi> <lo> <chk>` | — | speed limit, as wheel rpm (km/h × 208 / wheel diameter in inches) |
| `16 A0 01 B7` | — | **calibration** (service link) |
| `16 06 <pct> <chk>` | — | **motor test** at `pct` = 20…100 (service link) |

## Normal link — 1200 baud

The display repeats this 500 ms cycle (times relative to the cycle start):

| t / ms | Request | Reply read at |
|-------:|---------|--------------:|
| 0   | `16 0B …`, `16 1A …`, `16 1F …` | — |
| 120 | `11 20` | 200 |
| 200 | `11 08` | 300 |
| 300 | `11 11` | 400 |
| 400 | `11 31` | 500 |

Eight missed replies in a row → the display shows **Er 30** (communication fault).

## Service link — 9600 baud

### Entering it

0. Before the user starts the calibration, the display has been on its riding
   screen, sending the normal cycle above — with the calibration display's
   factory settings that is `16 0B 0C 2D` (assist level 1), `16 1A F0` and
   `16 1F 00 C8 FD` (25 km/h on 26") every 500 ms. Users report that the
   calibration does not start at assist level 0. `bafang-cal` sends this
   riding cycle for 2 s first.
1. At 1200 baud, send `11 51 25 80 F6` **three times** back-to-back (15 bytes,
   125 ms on the wire). The display ignores any answer.
2. 150 ms after queueing the first byte, reconfigure the local UART to 9600 baud.
3. Start the service cycle immediately.

There is no command to leave the service link; the display itself never
switches back. **Power-cycle the controller** to return to the normal link.

### Calibration cycle

The display's tick counter wraps after 500 ms, but a valid speed reply restarts
it 12 ms into the cycle (the speed display routine clears it), so with a
controller that answers `11 20` the cycle lasts **512 ms**:

| t / ms, speed reply valid | t / ms, no speed reply | Request | Reply |
|--------------------------:|-----------------------:|---------|-------|
| 0 | 0 | `11 20` | 3 bytes, read 12 ms later |
| 32 | 20 | `11 08` | 1 byte, read 10 ms later |
| 75, 138, 201, 264, 327, 390, 453 | 63, 126, 189, 252, 315, 378, 441 | `16 A0 01 B7` | ignored |
| 512 | 500 | next cycle | |

`bafang-cal` follows the same rule. Its only deliberate difference: it keeps
listening for a late speed reply (slow USB adapters) until just before 32 ms,
so when no reply comes at all its status request goes out at 31 ms instead of
20 ms.

Before every read request the receive buffer is flushed, so anything the
controller sends after `16 A0 01 B7` is discarded.

### Calibration status codes

While calibrating, the `11 08` status byte reports progress (Bafang's
"Ultra's Controller Calibration (CR R10M)" instructions; the display shows the
byte as "Er XX"):

| Status | Meaning |
|--------|---------|
| `40` | calibration started — the motor starts turning by itself |
| `41`, `43` | calibrating |
| `95`, `96`, `97`, `98` | calibrating (shown in this order) |
| `44` | **calibration finished** |
| `51`, `53`, `63`, `73`, `84`, `85` | **calibration failed** — calibrate again; if it persists the controller is defective |

Note that `41`–`44` mean battery faults on the riding screen; the meaning
depends on the mode.

### Motor-test cycle (500 ms, 512 ms with a speed reply)

| t / ms | Request | Reply |
|-------:|---------|-------|
| 0 | `11 20` | 3 bytes |
| 64, 128, 192, 256, 320, 384, 448 | `16 06 <pct> <chk>` — only while running | ignored |
| 75 | `11 12` | 3 bytes, read ≈ 10 ms later |

`pct = level × 10 + 10` for levels 1…9, e.g. level 1 → `16 06 14 30`,
level 9 → `16 06 64 80`. This is the display's *Power* + *Up* function. It is
documented here for completeness only: users report it heating a motor beyond
95 °C, and `bafang-cal` does not offer it.

## Using a plain terminal program

If you cannot run Python you can reproduce the calibration with any program
that can send hex bytes on a timer (RealTerm, CoolTerm, HTerm, …):

1. Open the port at 1200 8N1 and send `16 0B 0C 2D` a few times, 500 ms apart
   (assist level 1).
2. Send `11 51 25 80 F6 11 51 25 80 F6 11 51 25 80 F6`.
3. Wait ~150 ms, reopen/reconfigure the port at **9600** 8N1.
4. Send `16 A0 01 B7` repeatedly, about every 63 ms like the display (it never
   leaves more than 134 ms between two commands). Interleave `11 08` and watch
   the answer: `40` … `98` while calibrating, `44` when finished.
5. Power-cycle the controller.

`bafang-cal` does exactly this, with the display's precise timing, a wiring
check beforehand, live status output and automatic success/failure detection.
