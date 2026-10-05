#!/usr/bin/env python3
"""Cross-check bafang-cal's protocol constants against a C961 calibration firmware.

Usage::

    python tools/analyze_firmware.py path/to/c961_display_g510_cal.hex

The firmware image is *not* distributed with this repository (it is Bafang's
copyrighted code). If you have the file, this script locates every value the
tool relies on directly in the 8051 machine code and reports whether it matches
``bafang_cal.protocol``. Exit status: 0 = everything matches, 1 = a mismatch or
a pattern was not found, 2 = the file could not be read.

See docs/firmware-analysis.md for the full write-up.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bafang_cal import protocol

#: SHA-256 of the image analysed in docs/firmware-analysis.md.
KNOWN_SHA256 = "a67025f73719a0f2e6b796a09c9b0341eb80ac9f34e1e1506e0c442fee3ceffc"

#: Entry sequence of Keil C51's ``?C?ICASE`` (switch on an int) helper.
ICASE_SIGNATURE = bytes.fromhex("d0 83 d0 82 f8 e4 93 70 12 74 01 93 70 0d a3 a3 93 f8")


class HexFormatError(ValueError):
    """The input is not a valid Intel HEX file."""


def load_intel_hex(text: str, fill: int = 0xFF) -> bytes:
    """Return the flat memory image described by Intel HEX ``text``."""
    memory: dict[int, int] = {}
    upper = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise HexFormatError(f"line {number}: missing ':'")
        try:
            record = bytes.fromhex(line[1:])
        except ValueError as exc:
            raise HexFormatError(f"line {number}: {exc}") from None
        if len(record) < 5 or len(record) != record[0] + 5:
            raise HexFormatError(f"line {number}: bad record length")
        if sum(record) & 0xFF:
            raise HexFormatError(f"line {number}: checksum mismatch")
        count, address, kind = record[0], int.from_bytes(record[1:3], "big"), record[3]
        data = record[4 : 4 + count]
        if kind == 0x00:
            for offset, value in enumerate(data):
                memory[upper + address + offset] = value
        elif kind == 0x01:
            break
        elif kind == 0x02:
            upper = int.from_bytes(data, "big") << 4
        elif kind == 0x04:
            upper = int.from_bytes(data, "big") << 16
        # 0x03 / 0x05 (start address) are irrelevant for a flat image.
    if not memory:
        raise HexFormatError("no data records")
    image = bytearray([fill]) * (max(memory) + 1)
    for addr, value in memory.items():
        image[addr] = value
    return bytes(image)


# ----------------------------------------------------------------------- report


@dataclass
class Check:
    section: str
    label: str
    found: str
    expected: str | None = None
    address: int | None = None

    @property
    def ok(self) -> bool:
        return self.expected is None or self.found == self.expected


def _hex(data: bytes) -> str:
    return data.hex(" ").upper()


def _search(pattern: bytes, image: bytes, start: int = 0) -> Iterator[re.Match[bytes]]:
    return re.compile(pattern, re.DOTALL).finditer(image, start)


def _first(pattern: bytes, image: bytes, start: int = 0) -> re.Match[bytes] | None:
    return next(_search(pattern, image, start), None)


def _missing(section: str, label: str) -> Check:
    return Check(section, label, "pattern not found", expected="present")


def analyse(image: bytes) -> list[Check]:
    checks: list[Check] = []

    # --- UART -----------------------------------------------------------------
    # uart_init(baud): MOV R5,TMOD; MOV SCON,#50h; ... R4..R7 = numerator; LCALL div
    init = _first(rb"\xad\x89\x75\x98\x50", image)
    if init is None:
        checks.append(_missing("UART", "init routine (SCON = 0x50)"))
        crystal = None
    else:
        start = init.start()
        numerator = _first(rb"\x7f(.)\x7e(.)", image[start : start + 32])
        if numerator is None:
            crystal = None
            checks.append(_missing("UART", "baud-rate numerator"))
        else:
            value = numerator.group(2)[0] << 8 | numerator.group(1)[0]
            crystal = value * 384  # 12T core, SMOD = 0: baud = Fosc / 384 / (256 - TH1)
            checks.append(
                Check(
                    "UART",
                    "TH1 = 256 - N / baud",
                    f"N = {value} -> {crystal / 1e6:.3f} MHz crystal",
                    address=start,
                )
            )
        target = start.to_bytes(2, "big")
        bauds = sorted(
            (m.group(2)[0] << 8 | m.group(1)[0], m.start())
            for m in _search(rb"\x7f(.)\x7e(.)\x12" + re.escape(target), image)
        )
        found = ", ".join(str(b) for b, _ in bauds)
        expected = f"{protocol.NORMAL_BAUDRATE}, {protocol.SERVICE_BAUDRATE}"
        checks.append(Check("UART", "baud rates configured", found, expected, start))

    reload = _first(rb"\x75\x8a(.)\x75\x8c(.)", image)
    if reload is not None and crystal:
        counts = 0x10000 - (reload.group(2)[0] << 8 | reload.group(1)[0])
        tick_ms = counts * 12 / crystal * 1000
        checks.append(
            Check("UART", "timer 0 tick", f"{tick_ms:.3f} ms", "1.000 ms", reload.start())
        )

    # --- service-mode entry ----------------------------------------------------
    section = "Service mode entry"
    frame = _first(
        rb"\x74\x11\xf0\xa3\x74\x51\xf0\xa3\x74(.)\xf0\xa3\x74(.)\xf0\xa3\x74(.)\xf0"
        rb"((?:\x7b\x01\x7a\x00\x79.\x7d\x05\x12..)+)",
        image,
    )
    if frame is None:
        checks.append(_missing(section, "connect frame"))
    else:
        data = bytes((0x11, 0x51, *(frame.group(i)[0] for i in (1, 2, 3))))
        checks.append(
            Check(section, "frame", _hex(data), _hex(protocol.SERVICE_MODE_REQUEST), frame.start())
        )
        repeats = len(frame.group(4)) // 11
        checks.append(
            Check(section, "repetitions", str(repeats), str(protocol.SERVICE_MODE_REQUEST_REPEAT))
        )
        delay = _first(rb"\x75\x14(.)\xc2\x01\x22", image, frame.end())
        if delay is None:
            checks.append(_missing(section, "baud switch delay"))
        else:
            ticks = delay.group(1)[0]
            expected_ms = round(protocol.SERVICE_MODE_SWITCH_DELAY * 1000)
            checks.append(
                Check(section, "switch delay", f"{ticks} ms", f"{expected_ms} ms", delay.start())
            )

    # --- calibration (sub-mode 0) ---------------------------------------------
    section = "Calibration"
    cal = _first(rb"\x74\x16\xf0\xa3\x74\xa0\xf0\xa3\x74(.)\xf0", image)
    if cal is None:
        checks.append(_missing(section, "calibration frame"))
    else:
        payload = cal.group(1)[0]
        data = protocol.write_request(protocol.Command.CALIBRATE, (payload,))
        checks.append(
            Check(section, "frame", _hex(data), _hex(protocol.CALIBRATION_COMMAND), cal.start())
        )
        window = image[max(cal.start() - 64, 0) : cal.start()]
        cadence = _first(rb"\x7c\x00\x7d(.)\x12..\xed\x4c", window)
        found = "not found" if cadence is None else f"every {cadence.group(1)[0]} ms"
        checks.append(Check(section, "cadence (tick % N == 0)", found, "every 63 ms"))
        cycle = _first(rb"\xc3\x90\x00.\xe0\x94(.)\x90\x00.\xe0\x94(.)\x40", image, cal.end())
        found = "not found" if cycle is None else f"{cycle.group(2)[0] << 8 | cycle.group(1)[0]} ms"
        checks.append(Check(section, "cycle length", found, "500 ms"))

    speed = _first(rb"\xe0\xfd\x90\x00.\xe0\xff\x2d\x24(.)\xf5", image)
    if speed is not None:
        found = f"0x{speed.group(1)[0]:02X} + hi + lo"
        expected = f"0x{protocol.Command.SPEED:02X} + hi + lo"
        checks.append(Check(section, "speed reply checksum", found, expected, speed.start()))

    # --- motor test (sub-mode 1) ------------------------------------------------
    section = "Motor test"
    motor = _first(rb"\x74\x16\xf0\xa3\x74\x06\xf0.{0,8}\x75\xf0(.)\xa4\x24(.)", image)
    if motor is None:
        checks.append(_missing(section, "motor-test frame"))
    else:
        mul, add = motor.group(1)[0], motor.group(2)[0]
        checks.append(
            Check(
                section,
                "frame",
                f"16 06 <level*{mul}+{add}> <chk>",
                "16 06 <level*10+10> <chk>",
                motor.start(),
            )
        )
        cadence64 = _first(rb"\x54\x3f\x70", image[max(motor.start() - 16, 0) : motor.start()])
        checks.append(
            Check(
                section,
                "cadence (tick & 0x3F == 0)",
                "every 64 ms" if cadence64 else "not found",
                "every 64 ms",
            )
        )

    # --- riding screen (sent before the calibration starts) -------------------
    section = "Riding screen"
    # switch on the number of assist levels: MOV DPTR,#000Ah; MOVX A,@DPTR;
    # MOV DPTR,#table; SJMP/MOVC -- one table per level count, 2..9 levels.
    tables = [
        int.from_bytes(m.group(1), "big")
        for m in _search(rb"\x90\x00\x0a\xe0\x90(..)[\x80\x93]", image)
    ]
    if len(tables) < 2:
        checks.append(_missing(section, "assist-level code tables"))
    else:
        start = tables[1]  # the 3-level table
        codes = image[start + 1 : start + 4]
        found = ", ".join(f"{level} -> {code:02X}" for level, code in enumerate(codes, 1))
        expected = ", ".join(
            f"{level} -> {code:02X}"
            for level, code in sorted(protocol.CALIBRATION_DISPLAY_ASSIST_CODES.items())
        )
        checks.append(Check(section, "assist codes, 3-level display", found, expected, start))

    levels = _first(rb"\x74(.)\xf0\x90\x00\x0e\x14\xf0", image)
    level = _first(rb"\x74(.)\xf0\x90\x00\x0a\x14\xf0", image)
    wheel = _first(rb"\x90\x00\x02\x74(.)\xf0", image)
    limit = _first(rb"\x90\x00\x07\x74(.)\xf0", image)
    factor = _first(rb"\x90\x00\x07\xe0\x75\xf0(.)\xa4", image)
    if levels is None or level is None or wheel is None or limit is None or factor is None:
        checks.append(_missing(section, "factory settings"))
    else:
        checks.append(
            Check(
                section,
                "factory settings",
                f"{levels.group(1)[0] - 1} levels, assist level {level.group(1)[0] - 1}",
                "3 levels, assist level 1",
            )
        )
        kmh, inches, mul = limit.group(1)[0], wheel.group(1)[0], factor.group(1)[0]
        rpm = kmh * mul // inches
        checks.append(
            Check(
                section,
                f"speed limit ({kmh} km/h * {mul} / {inches} in)",
                f"{rpm} rpm",
                f"{protocol.CALIBRATION_DISPLAY_SPEED_LIMIT_RPM} rpm",
            )
        )

    # --- read requests -----------------------------------------------------------
    commands = {m.group(1)[0] for m in _search(rb"\x74\x11\xf0\xa3\x74(.)\xf0", image)}
    if _first(rb"\x74\x11\xf0\xa3\x04\xf0", image):  # MOV A,#11h; ...; INC A -> 0x12
        commands.add(0x12)
    if _first(rb"\x74\x11\xf0\xa3\xf0", image):  # second byte reuses A = 0x11
        commands.add(0x11)
    commands.discard(0x51)
    checks.append(
        Check(
            "Read requests",
            "commands built",
            ", ".join(f"11 {c:02X}" for c in sorted(commands)),
        )
    )

    # --- Keil switch tables (normal-mode schedule) --------------------------------
    helper = image.find(ICASE_SIGNATURE)
    if helper >= 0:
        call = b"\x12" + helper.to_bytes(2, "big")
        for match in _search(re.escape(call), image):
            cases = _decode_icase(image, match.end())
            if cases:
                text = ", ".join(f"{value}->0x{target:04X}" for value, target in cases)
                checks.append(Check("Switch tables", f"table at 0x{match.end():04X}", text))
    return checks


def _decode_icase(image: bytes, address: int) -> list[tuple[int, int]]:
    cases: list[tuple[int, int]] = []
    while address + 4 <= len(image) and len(cases) < 64:
        target = int.from_bytes(image[address : address + 2], "big")
        value = int.from_bytes(image[address + 2 : address + 4], "big")
        if target == 0:
            return cases
        cases.append((value, target))
        address += 4
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("firmware", type=Path, help="Intel HEX file (.hex/.txt)")
    args = parser.parse_args(argv)

    try:
        raw = args.firmware.read_bytes()
        image = load_intel_hex(raw.decode("ascii"))
    except (OSError, UnicodeDecodeError, HexFormatError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    digest = hashlib.sha256(raw).hexdigest()
    known = "matches the analysed image" if digest == KNOWN_SHA256 else "unknown image"
    print(f"Firmware: {args.firmware}")
    print(f"  SHA-256: {digest} ({known})")
    print(f"  Size:    {len(image)} bytes (0x0000-0x{len(image) - 1:04X})")

    checks = analyse(image)
    section = None
    for check in checks:
        if check.section != section:
            section = check.section
            print(f"\n{section}")
        mark = "  " if check.expected is None else ("ok" if check.ok else "!!")
        where = "" if check.address is None else f" @0x{check.address:04X}"
        line = f"  [{mark}] {check.label}{where}: {check.found}"
        if check.expected is not None and not check.ok:
            line += f"   (bafang_cal expects: {check.expected})"
        print(line)

    failed = [c for c in checks if not c.ok]
    print()
    if failed:
        print(f"{len(failed)} check(s) did not match bafang_cal -- please open an issue.")
        return 1
    print("All protocol constants used by bafang_cal match this firmware.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
