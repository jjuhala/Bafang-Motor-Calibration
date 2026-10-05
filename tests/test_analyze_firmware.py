"""Tests for tools/analyze_firmware.py using a synthetic image.

The real firmware is not part of the repository, so the image below is
assembled from the short instruction sequences the analyser looks for.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

TOOL = Path(__file__).resolve().parent.parent / "tools" / "analyze_firmware.py"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("analyze_firmware", TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


analyzer = _load_tool()

FRAGMENTS = {
    # uart_init(baud) at 0x0100: TH1 = 256 - 48000 / baud
    0x0100: "ad 89 75 98 50 53 05 0f 43 05 20 8d 89 ab 07 aa 06 e4 f9 f8 7f 80 7e bb",
    0x0200: "7f b0 7e 04 12 01 00",  # uart_init(1200)
    0x0210: "7f 80 7e 25 12 01 00",  # uart_init(9600)
    0x0220: "75 8a 00 75 8c fa",  # timer 0 reload 0xFA00
    0x0300: (
        "74 11 f0 a3 74 51 f0 a3 74 25 f0 a3 74 80 f0 a3 74 f6 f0"
        + " 7b 01 7a 00 79 48 7d 05 12 34 e8" * 3  # uart_write(frame, 5) x3
        + " 00 00 75 14 96 c2 01 22"
    ),
    0x0400: (
        "7c 00 7d 3f 12 1e 5b ed 4c 70 28 90 00 48 74 16 f0 a3 74 a0 f0 a3 74 01 f0"
        " c3 90 00 18 e0 94 f4 90 00 17 e0 94 01 40 04"
    ),
    # if (t == 12) { read 11 20 reply; check 0x20 + hi + lo; show_speed(force=1) }
    0x0470: "90 00 17 e0 70 04 a3 e0 64 0c 70 4a",
    0x0480: "e0 fd 90 00 48 e0 ff 2d 24 20 f5 25 00 7f 01 12 06 a0",
    0x06A0: "8f 2b 00 00 e4 90 00 17 f0 a3 f0 22",  # show_speed clears t
    0x0500: "54 3f 70 29 30 01 26 90 00 48 74 16 f0 a3 74 06 f0 90 00 0a e0 75 f0 0a a4 24 0a",
    0x0580: "74 11 f0 a3 74 20 f0 74 11 f0 a3 04 f0 74 11 f0 a3 f0 74 11 f0 a3 74 08 f0",
    0x0600: "d0 83 d0 82 f8 e4 93 70 12 74 01 93 70 0d a3 a3 93 f8",
    0x0700: "12 06 00 0c e0 00 00 0d f7 00 64 00 00 0e 87",
    # assist-code tables for 2 and 3 levels, selected by a switch on XDATA 0Eh
    0x0740: "90 00 0a e0 90 07 80 80 0c 90 00 0a e0 90 07 83 93",
    0x0780: "00 02 03 00 0c 02 03",
    # factory settings: wheel 26", 25 km/h, 3 levels, assist level 1
    0x07A0: "90 00 02 74 1a f0 90 00 07 74 19 f0 74 04 f0 90 00 0e 14 f0",
    0x07C0: "74 02 f0 90 00 0a 14 f0 90 00 07 e0 75 f0 d0 a4",
}


def build_image(overrides: dict[int, str] | None = None) -> bytes:
    fragments = {**FRAGMENTS, **(overrides or {})}
    image = bytearray(0x800)
    for address, text in fragments.items():
        data = bytes.fromhex(text)
        image[address : address + len(data)] = data
    return bytes(image)


def to_intel_hex(image: bytes, *, base: int = 0) -> str:
    lines = []
    if base:
        segment = (base >> 16).to_bytes(2, "big")
        record = bytes((2, 0, 0, 4)) + segment
        lines.append(":" + (record + bytes(((-sum(record)) & 0xFF,))).hex().upper())
    for offset in range(0, len(image), 16):
        chunk = image[offset : offset + 16]
        record = bytes((len(chunk), offset >> 8, offset & 0xFF, 0)) + chunk
        lines.append(":" + (record + bytes(((-sum(record)) & 0xFF,))).hex().upper())
    lines.append(":00000001FF")
    return "\n".join(lines) + "\n"


def test_load_intel_hex_roundtrip() -> None:
    image = build_image()
    assert analyzer.load_intel_hex(to_intel_hex(image)) == image


def test_load_intel_hex_fills_gaps_and_handles_extended_address() -> None:
    text = ":0100020055A8\n:00000001FF\n"
    assert analyzer.load_intel_hex(text) == b"\xff\xff\x55"
    extended = analyzer.load_intel_hex(to_intel_hex(b"\x01\x02", base=0x10000))
    assert extended[0x10000:] == b"\x01\x02"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("0100020055A8\n", "missing ':'"),
        (":0100020055A9\n", "checksum"),
        (":01000200\n", "length"),
        (":zz\n", "line 1"),
        (":00000001FF\n", "no data"),
    ],
)
def test_load_intel_hex_rejects_bad_input(text: str, message: str) -> None:
    with pytest.raises(analyzer.HexFormatError, match=message):
        analyzer.load_intel_hex(text)


def test_analyser_accepts_matching_image(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "fw.hex"
    path.write_text(to_intel_hex(build_image()))
    assert analyzer.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "unknown image" in out
    assert "[ok] baud rates configured @0x0100: 1200, 9600" in out
    assert "[ok] frame @0x0300: 11 51 25 80 F6" in out
    assert "[ok] frame @0x040E: 16 A0 01 B7" in out
    assert "[ok] cadence (tick % N == 0): every 63 ms" in out
    assert "[ok] speed reply read at tick: 12 ms" in out
    assert "[ok] speed display restarts the tick counter @0x06A4: cycle 512 ms" in out
    assert "commands built: 11 08, 11 11, 11 12, 11 20" in out
    assert "0->0x0CE0, 100->0x0DF7" in out
    assert "[ok] assist codes, 3-level display @0x0783: 1 -> 0C, 2 -> 02, 3 -> 03" in out
    assert "[ok] factory settings: 3 levels, assist level 1" in out
    assert "[ok] speed limit (25 km/h * 208 / 26 in): 200 rpm" in out
    assert "All protocol constants used by bafang_cal match" in out


def test_analyser_flags_differences(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    changed = build_image({0x0416: "74 02 f0"})  # calibration payload 0x01 -> 0x02
    path = tmp_path / "fw.hex"
    path.write_text(to_intel_hex(changed))
    assert analyzer.main([str(path)]) == 1
    out = capsys.readouterr().out
    assert "[!!] frame @0x040E: 16 A0 02 B8   (bafang_cal expects: 16 A0 01 B7)" in out


def test_analyser_notices_missing_counter_restart(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changed = build_image({0x06A0: "8f 2b 00 00 00 00 00 00 00 00 00 22"})
    path = tmp_path / "fw.hex"
    path.write_text(to_intel_hex(changed))
    assert analyzer.main([str(path)]) == 1
    assert "[!!] speed display restarts the tick counter: no" in capsys.readouterr().out


def test_analyser_reports_missing_patterns() -> None:
    checks = analyzer.analyse(bytes(64))
    assert not all(check.ok for check in checks)
    assert {c.found for c in checks if not c.ok} == {"pattern not found"}


def test_analyser_reports_unreadable_file(tmp_path: Path) -> None:
    assert analyzer.main([str(tmp_path / "missing.hex")]) == 2
