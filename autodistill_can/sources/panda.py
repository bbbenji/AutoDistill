"""Live capture from a comma.ai panda over USB.

The panda is the natural sniffer for this job because it sees all three buses at
once, and on most cars bus 0 is the powertrain bus while bus 2 is the camera /
radar bus that openpilot has to impersonate. Knowing which bus a message came
from is therefore part of the answer, not a detail.

This needs ``libusb1`` (``pip install autodistill-can[panda]``) and a panda in its
normal (non-bootstub) mode. Capture is read-only: the panda is put in its
``SAFETY_SILENT``/no-output safety mode and this source exposes no CAN send
path. A panda in normal CAN mode may still acknowledge frames at the electrical
protocol level; use a SocketCAN adaptor configured ``listen-only`` when even
acknowledgements are unacceptable.

If you would rather not depend on libusb, run comma's own tooling to bridge the
panda onto SocketCAN and use ``socketcan:can0`` instead.
"""

from __future__ import annotations

import math
import time
from typing import Iterator

from ..frame import CanFrame

__all__ = ["PandaSource"]

_VENDOR_IDS = (0xBBAA, 0x3801)
_PRODUCT_IDS = (0xDDCC, 0xDDEE)

# Control requests understood by the panda firmware.
_REQ_CAN_RESET = 0xC0
_REQ_SAFETY_MODE = 0xDC
_REQ_CAN_LOOPBACK = 0xE5
_REQ_POWER_SAVE = 0xE7
_REQ_HEARTBEAT_DISABLED = 0xF8

#: Safety model that permits no transmission at all.
SAFETY_SILENT = 0

_READ_ENDPOINT = 0x81
_READ_CHUNK = 0x4000
_CAN_PACKET_HEADER_SIZE = 6
_DLC_TO_LEN = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


class PandaSource:
    """Iterable of frames from every panda bus (or one, if ``bus`` is given)."""

    def __init__(
        self,
        *,
        bus: int | None = None,
        serial: str | None = None,
        duration: float | None = None,
        limit: int | None = None,
        silent: bool = True,
    ) -> None:
        self.bus = bus
        self.serial = serial
        self.duration = duration
        self.limit = limit
        self.silent = silent
        self._ctx = None
        self._handle = None
        if bus is not None and (
            not isinstance(bus, int) or isinstance(bus, bool) or not 0 <= bus <= 7
        ):
            raise ValueError("panda bus must be an integer in [0, 7]")
        if duration is not None and (
            not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration <= 0
        ):
            raise ValueError("capture duration must be positive and finite")
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
        ):
            raise ValueError("capture frame limit must be a positive integer")
        if not silent:
            raise ValueError(
                "PandaSource is capture-only and always uses SAFETY_SILENT; "
                "use `autodistill-can probe` for explicitly authorised diagnostics"
            )

    def open(self) -> None:
        try:
            import usb1
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "panda capture needs libusb1: "
                "pip install 'autodistill-can[panda]'"
            ) from exc

        self._ctx = usb1.USBContext()
        bootstub_found = False
        for device in self._ctx.getDeviceList(skip_on_error=True):
            if (
                device.getVendorID() not in _VENDOR_IDS
                or device.getProductID() not in _PRODUCT_IDS
            ):
                continue
            try:
                this_serial = device.getSerialNumber()
            except usb1.USBError:  # pragma: no cover - permissions
                continue
            if self.serial and this_serial != self.serial:
                continue
            # A PID ending in 0xee is panda's bootstub. It exposes the same USB
            # interface but cannot receive CAN until normal firmware is booted.
            if device.getProductID() & 0xF0 == 0xE0:
                bootstub_found = True
                continue
            self._handle = device.open()
            self._handle.claimInterface(0)
            break

        if self._handle is None:
            self.close()
            if bootstub_found:
                raise RuntimeError(
                    "panda is in bootstub mode; recover or flash normal panda "
                    "firmware before capturing"
                )
            raise RuntimeError(
                "no panda found. Check the USB cable, and that you have udev "
                "permission (comma ships a rules file) or run as root."
            )

        try:
            # Reset any partial USB packet left by an earlier host, disable the
            # openpilot heartbeat watchdog, wake the device, and force the
            # no-output safety model. This source has no send path.
            self._control_write(_REQ_CAN_RESET, 0)
            self._control_write(_REQ_HEARTBEAT_DISABLED, 0)
            self._control_write(_REQ_POWER_SAVE, 0)
            self._control_write(_REQ_CAN_LOOPBACK, 0)
            self._control_write(_REQ_SAFETY_MODE, SAFETY_SILENT)
        except Exception:
            self.close()
            raise

    def _control_write(self, request: int, value: int) -> None:
        assert self._handle is not None
        self._handle.controlWrite(0x40, request, value, 0, b"")

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None
        if self._ctx is not None:
            try:
                self._ctx.close()
            finally:
                self._ctx = None

    def __enter__(self) -> "PandaSource":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __iter__(self) -> Iterator[CanFrame]:
        if self._handle is None:
            self.open()
        assert self._handle is not None
        import usb1

        deadline = time.monotonic() + self.duration if self.duration else None
        count = 0
        overflow = b""
        try:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    return
                try:
                    chunk = self._handle.bulkRead(_READ_ENDPOINT, _READ_CHUNK, 100)
                except usb1.USBErrorTimeout:
                    continue
                # The panda stamps nothing, so time the read itself. Frames
                # inside one bulk transfer share a timestamp; at 0.1s polling
                # that is accurate enough for rate estimates and far better
                # than a monotonically increasing counter.
                now = time.time()
                frames, overflow = _unpack_frames(overflow + bytes(chunk), now)
                for frame in frames:
                    if self.bus is not None and frame.bus != self.bus:
                        continue
                    yield frame
                    count += 1
                    if self.limit is not None and count >= self.limit:
                        return
        finally:
            self.close()


def _unpack_frames(chunk: bytes, t: float) -> tuple[list[CanFrame], bytes]:
    """Decode complete packets and return any partial trailing packet.

    Current panda firmware uses a six-byte header followed by a DLC-sized
    payload. USB transfers may split a packet at any byte, so the caller must
    prepend the returned overflow to its next read. Returned/rejected transmit
    status packets are deliberately omitted: they are host-side bookkeeping,
    not traffic observed on a physical vehicle bus.
    """
    frames: list[CanFrame] = []
    offset = 0
    while len(chunk) - offset >= _CAN_PACKET_HEADER_SIZE:
        header = chunk[offset : offset + _CAN_PACKET_HEADER_SIZE]
        length = _DLC_TO_LEN[header[0] >> 4]
        packet_length = _CAN_PACKET_HEADER_SIZE + length
        if len(chunk) - offset < packet_length:
            break

        packet = chunk[offset : offset + packet_length]
        if _xor_checksum(packet) != 0:
            raise RuntimeError(
                "panda CAN packet checksum mismatch; reset or reflash the "
                "panda so its firmware protocol matches this reader"
            )

        word = int.from_bytes(header[1:5], "little")
        addr = word >> 3
        returned = bool(header[1] & 0x02)
        rejected = bool(header[1] & 0x01)
        if not returned and not rejected:
            bus = (header[0] >> 1) & 0x07
            data = bytes(packet[_CAN_PACKET_HEADER_SIZE:])
            frames.append(CanFrame(t=t, addr=addr, data=data, bus=bus))
        offset += packet_length

    return frames, bytes(chunk[offset:])


def _xor_checksum(data: bytes) -> int:
    checksum = 0
    for byte in data:
        checksum ^= byte
    return checksum
