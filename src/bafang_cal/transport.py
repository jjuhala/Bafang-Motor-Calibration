"""Byte transports: a real serial port, plus helpers shared by every transport."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol, TextIO, runtime_checkable

if TYPE_CHECKING:
    from types import TracebackType

    import serial

__all__ = [
    "Clock",
    "SerialTransport",
    "SystemClock",
    "TracingTransport",
    "Transport",
    "TransportError",
    "list_serial_ports",
]


class TransportError(OSError):
    """Raised when the underlying port cannot be opened or used."""


@runtime_checkable
class Transport(Protocol):
    """Minimal byte-stream interface used by the service routines."""

    @property
    def baudrate(self) -> int: ...

    def set_baudrate(self, baudrate: int) -> None:
        """Reconfigure the line speed (8N1 is always used)."""

    def write(self, data: bytes) -> None:
        """Queue ``data`` for transmission."""

    def wait_sent(self) -> None:
        """Block until queued data has left the host (best effort)."""

    def read(self, size: int, timeout: float) -> bytes:
        """Return up to ``size`` bytes, waiting at most ``timeout`` seconds."""

    def drain(self) -> bytes:
        """Return (and discard from the buffer) all bytes received so far."""

    def close(self) -> None: ...


class Clock(Protocol):
    """Time source, injectable so the schedules can be tested without sleeping."""

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real wall clock."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class SerialTransport:
    """A pyserial port configured for the Bafang UART link (8N1, no flow control).

    ``port`` is a device name (``/dev/ttyUSB0``, ``COM3``) or any pyserial URL
    such as ``socket://host:port`` for network-attached adapters.
    """

    def __init__(self, port: str, baudrate: int) -> None:
        import serial  # imported lazily so --simulate works without pyserial

        try:
            self._serial: serial.Serial = serial.serial_for_url(
                port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0,
                write_timeout=2.0,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
        except serial.SerialException as exc:
            raise TransportError(f"cannot open serial port {port!r}: {exc}") from exc
        self._serial.reset_input_buffer()

    @property
    def baudrate(self) -> int:
        return int(self._serial.baudrate)

    def set_baudrate(self, baudrate: int) -> None:
        self._serial.baudrate = baudrate

    def write(self, data: bytes) -> None:
        import serial

        try:
            self._serial.write(data)
        except serial.SerialException as exc:
            raise TransportError(f"serial write failed: {exc}") from exc

    def wait_sent(self) -> None:
        self._serial.flush()

    def read(self, size: int, timeout: float) -> bytes:
        import serial

        if size <= 0:
            return b""
        try:
            self._serial.timeout = max(timeout, 0.0)
            return bytes(self._serial.read(size))
        except serial.SerialException as exc:
            raise TransportError(f"serial read failed: {exc}") from exc

    def drain(self) -> bytes:
        waiting = self._serial.in_waiting
        if not waiting:
            return b""
        self._serial.timeout = 0
        return bytes(self._serial.read(waiting))

    def close(self) -> None:
        self._serial.close()

    def __enter__(self) -> SerialTransport:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class TracingTransport:
    """Wraps another transport and logs every byte to a text stream.

    Each line is ``<seconds> <baud> <TX|RX|DROP> <hex bytes>``; ``DROP`` marks
    bytes discarded by :meth:`drain` (e.g. unsolicited replies). Traces are the
    most useful thing to attach to bug reports.
    """

    def __init__(self, inner: Transport, stream: TextIO, clock: Clock | None = None) -> None:
        self._inner = inner
        self._stream = stream
        self._clock = clock or SystemClock()
        self._t0 = self._clock.monotonic()
        self._log("OPEN", b"")

    def _log(self, kind: str, data: bytes) -> None:
        stamp = self._clock.monotonic() - self._t0
        self._stream.write(f"{stamp:10.4f} {self._inner.baudrate:5d} {kind:<4} {data.hex(' ')}\n")
        self._stream.flush()

    @property
    def baudrate(self) -> int:
        return self._inner.baudrate

    def set_baudrate(self, baudrate: int) -> None:
        self._inner.set_baudrate(baudrate)
        self._log("BAUD", b"")

    def write(self, data: bytes) -> None:
        self._inner.write(data)
        self._log("TX", data)

    def wait_sent(self) -> None:
        self._inner.wait_sent()

    def read(self, size: int, timeout: float) -> bytes:
        data = self._inner.read(size, timeout)
        if data:
            self._log("RX", data)
        return data

    def drain(self) -> bytes:
        data = self._inner.drain()
        if data:
            self._log("DROP", data)
        return data

    def close(self) -> None:
        self._log("CLOSE", b"")
        self._inner.close()


def list_serial_ports(*, usb_only: bool = False) -> list[tuple[str, str]]:
    """Return ``(device, description)`` for the serial ports pyserial can see.

    With ``usb_only`` only USB devices (adapters) are returned, which skips
    built-in UARTs and Bluetooth ports.
    """
    from serial.tools import list_ports

    return sorted(
        (p.device, p.description or "")
        for p in list_ports.comports()
        if not usb_only or p.vid is not None
    )
