from __future__ import annotations

import io
import time

import pytest

from bafang_cal.simulator import ManualClock, SimulatedController
from bafang_cal.transport import (
    SerialTransport,
    SystemClock,
    TracingTransport,
    Transport,
    TransportError,
    list_serial_ports,
)


def test_serial_transport_over_loopback() -> None:
    # pyserial's loop:// URL echoes everything written, exercising the real code path.
    with SerialTransport("loop://", 1200) as port:
        assert isinstance(port, Transport)
        assert port.baudrate == 1200
        port.write(bytes.fromhex("11 08"))
        port.wait_sent()
        assert port.read(2, timeout=1.0) == bytes.fromhex("11 08")
        assert port.read(0, timeout=1.0) == b""
        assert port.read(1, timeout=0.01) == b""

        port.set_baudrate(9600)
        assert port.baudrate == 9600
        port.write(b"abc")
        deadline = time.monotonic() + 1.0
        drained = b""
        while len(drained) < 3 and time.monotonic() < deadline:
            drained += port.drain()
        assert drained == b"abc"
        assert port.drain() == b""


def test_serial_transport_reports_unknown_port() -> None:
    with pytest.raises(TransportError, match="cannot open serial port"):
        SerialTransport("/dev/this-port-does-not-exist", 1200)


def test_tracing_transport_logs_every_byte(clock: ManualClock) -> None:
    stream = io.StringIO()
    inner = SimulatedController(clock)
    traced = TracingTransport(inner, stream, clock)

    traced.write(bytes.fromhex("11 08"))
    clock.advance(0.25)
    assert traced.read(1, 0.1) == b"\x01"
    inner.inject(b"\xaa")
    assert traced.drain() == b"\xaa"
    traced.set_baudrate(9600)
    traced.wait_sent()
    assert traced.baudrate == 9600
    assert traced.read(1, 0.0) == b""  # empty reads are not logged
    traced.close()

    lines = [line.split() for line in stream.getvalue().splitlines()]
    assert lines == [
        ["0.0000", "1200", "OPEN"],
        ["0.0000", "1200", "TX", "11", "08"],
        ["0.2500", "1200", "RX", "01"],
        ["0.2500", "1200", "DROP", "aa"],
        ["0.2500", "9600", "BAUD"],
        ["0.2500", "9600", "CLOSE"],
    ]
    assert inner.closed


def test_system_clock() -> None:
    clock = SystemClock()
    before = clock.monotonic()
    clock.sleep(0)
    clock.sleep(-1)
    clock.sleep(0.001)
    assert clock.monotonic() >= before


def test_list_serial_ports_returns_pairs() -> None:
    for device, description in list_serial_ports():
        assert isinstance(device, str)
        assert isinstance(description, str)


def test_list_serial_ports_can_filter_usb(monkeypatch: pytest.MonkeyPatch) -> None:
    from serial.tools import list_ports
    from serial.tools.list_ports_common import ListPortInfo

    usb = ListPortInfo("/dev/ttyUSB0")
    usb.vid, usb.pid, usb.description = 0x10C4, 0xEA60, "CP2102"
    builtin = ListPortInfo("/dev/ttyS0")
    builtin.description = "ttyS0"
    monkeypatch.setattr(list_ports, "comports", lambda: [usb, builtin])

    assert list_serial_ports() == [("/dev/ttyS0", "ttyS0"), ("/dev/ttyUSB0", "CP2102")]
    assert list_serial_ports(usb_only=True) == [("/dev/ttyUSB0", "CP2102")]
