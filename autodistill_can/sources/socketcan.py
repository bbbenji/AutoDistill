"""Live SocketCAN capture using only the standard library.

CPython exposes ``AF_CAN``/``CAN_RAW`` directly, so there is no need for
``python-can`` just to read frames. Timestamps come from ``SO_TIMESTAMPNS`` on
the socket, i.e. from the kernel at frame arrival, which is far steadier than
stamping in userspace after the read returns.

Typical setup for a real car, via a USB or SPI CAN adaptor:

    sudo ip link set can0 type can bitrate 500000 listen-only on
    sudo ip link set can0 up

``listen-only`` is worth using for a first capture: the adaptor then never
acknowledges or transmits anything, so it cannot disturb the car's bus.
"""

from __future__ import annotations

import errno
import math
import socket
import struct
import time
from typing import Iterator

from ..frame import CanFrame

__all__ = ["SocketCanSource", "available"]

# struct can_frame { canid_t can_id; __u8 len; __u8 __pad, __res0, len8_dlc;
#                    __u8 data[8] __attribute__((aligned(8))); }
_CAN_FRAME_FMT = "=IB3x8s"
_CAN_FRAME_SIZE = struct.calcsize(_CAN_FRAME_FMT)

# struct canfd_frame { canid_t can_id; __u8 len, flags, __res0, __res1;
#                      __u8 data[64]; }
_CANFD_FRAME_FMT = "=IB3x64s"
_CANFD_FRAME_SIZE = struct.calcsize(_CANFD_FRAME_FMT)

_CAN_EFF_FLAG = 0x80000000  # extended (29-bit) identifier
_CAN_RTR_FLAG = 0x40000000  # remote transmission request
_CAN_ERR_FLAG = 0x20000000  # error frame, not real traffic
_CAN_SFF_MASK = 0x000007FF
_CAN_EFF_MASK = 0x1FFFFFFF


def available() -> bool:
    """Whether this kernel/build can do SocketCAN at all."""
    return hasattr(socket, "AF_CAN") and hasattr(socket, "CAN_RAW")


class SocketCanSource:
    """Iterable of frames from a SocketCAN interface.

    ``bus`` defaults to the trailing number in the interface name so that
    ``can0``/``can1``/``can2`` line up with panda bus numbering; pass it
    explicitly to override.
    """

    def __init__(
        self,
        channel: str = "can0",
        *,
        bus: int | None = None,
        fd: bool = False,
        duration: float | None = None,
        limit: int | None = None,
    ) -> None:
        if not available():
            raise RuntimeError("this Python/kernel has no SocketCAN support")
        if not channel:
            raise ValueError("SocketCAN channel cannot be empty")
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
        self.channel = channel
        self.fd = fd
        self.duration = duration
        self.limit = limit
        if bus is None:
            trailing = "".join(c for c in channel if c.isdigit())
            bus = int(trailing) if trailing else 0
        if not isinstance(bus, int) or isinstance(bus, bool) or not 0 <= bus <= 255:
            raise ValueError("SocketCAN bus must be an integer in [0, 255]")
        self.bus = bus
        self.sock: socket.socket | None = None

    def open(self) -> None:
        sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        if self.fd:
            try:
                sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FD_FRAMES, 1)
            except OSError as exc:  # pragma: no cover - kernel dependent
                sock.close()
                raise RuntimeError(f"{self.channel}: CAN FD not supported: {exc}")
        # Kernel-side arrival timestamps.
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_TIMESTAMPNS, 1)
        except OSError:  # pragma: no cover - fall back to userspace clock
            pass
        try:
            sock.bind((self.channel,))
        except OSError as exc:
            sock.close()
            if exc.errno == errno.ENODEV:
                raise RuntimeError(
                    f"no such CAN interface: {self.channel!r}. Bring one up with "
                    f"`sudo ip link set {self.channel} type can bitrate 500000 "
                    f"listen-only on && sudo ip link set {self.channel} up`"
                ) from exc
            raise
        self.sock = sock

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def __enter__(self) -> "SocketCanSource":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __iter__(self) -> Iterator[CanFrame]:
        if self.sock is None:
            self.open()
        assert self.sock is not None
        sock = self.sock
        frame_size = _CANFD_FRAME_SIZE if self.fd else _CAN_FRAME_SIZE
        fmt = _CANFD_FRAME_FMT if self.fd else _CAN_FRAME_FMT
        # Room for the SO_TIMESTAMPNS control message.
        anc_size = socket.CMSG_SPACE(16)

        deadline = time.monotonic() + self.duration if self.duration else None
        count = 0
        try:
            while True:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    sock.settimeout(remaining)
                try:
                    payload, ancdata, _flags, _addr = sock.recvmsg(
                        frame_size, anc_size
                    )
                except TimeoutError:
                    return
                except OSError as exc:  # pragma: no cover - transient bus errors
                    if exc.errno in (errno.EINTR, errno.EAGAIN):
                        continue
                    raise
                if len(payload) < frame_size:
                    continue

                can_id, length, data = struct.unpack(fmt, payload)
                if can_id & _CAN_ERR_FLAG:
                    # Bus error frames are diagnostics, not traffic.
                    continue
                if can_id & _CAN_RTR_FLAG:
                    continue
                addr = can_id & (
                    _CAN_EFF_MASK if can_id & _CAN_EFF_FLAG else _CAN_SFF_MASK
                )

                t = _timestamp_from(ancdata)
                yield CanFrame(
                    t=t if t is not None else time.time(),
                    addr=addr,
                    data=data[:length],
                    bus=self.bus,
                )
                count += 1
                if self.limit is not None and count >= self.limit:
                    return
        finally:
            self.close()


def _timestamp_from(ancdata: list[tuple[int, int, bytes]]) -> float | None:
    """Pull the kernel arrival time out of a SO_TIMESTAMPNS control message."""
    for level, ctype, cdata in ancdata:
        if level == socket.SOL_SOCKET and ctype == getattr(
            socket, "SO_TIMESTAMPNS", -1
        ):
            if len(cdata) >= 16:
                sec, nsec = struct.unpack("=qq", cdata[:16])
                return sec + nsec * 1e-9
    return None
