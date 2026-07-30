"""Active UDS querying — reads ECU firmware versions by asking for them.

Everything else in this package is passive: it listens. This module transmits,
and that difference deserves care. The requests it sends are read-only
diagnostics (``ReadDataByIdentifier``), the same thing any garage scan tool
sends and the same thing openpilot itself sends when fingerprinting. It cannot
write to an ECU, clear a fault, or unlock anything.

Even so, transmitting onto a live vehicle bus is not free. Query only a car you
own or have permission to work on, with the vehicle stationary and in park, and
prefer a bench setup where you have one. The CLI requires an explicit flag to
get here; there is no path that transmits by accident.
"""

from __future__ import annotations

import math
import socket
import time
from dataclasses import dataclass

from .analysis.uds import (
    UdsResponse,
    decode_uds,
    reassemble_isotp,
    response_address,
)
from .frame import CanFrame

__all__ = ["FINGERPRINT_DIDS", "ProbeResult", "probe_socketcan"]

#: Data identifiers openpilot reads when fingerprinting, in the order it tries.
FINGERPRINT_DIDS: tuple[int, ...] = (
    0xF188,  # vehicleManufacturerECUSoftwareNumber
    0xF189,  # vehicleManufacturerECUSoftwareVersionNumber
    0xF18C,  # ECUSerialNumber
    0xF191,  # vehicleManufacturerECUHardwareNumber
    0xF190,  # VIN
)

#: ECU request addresses worth trying. The 0x7Ex block is standard OBD-II; the
#: 0x7Bx block is where several OEMs put ABS and EPS.
DEFAULT_REQUEST_ADDRESSES: tuple[int, ...] = tuple(range(0x7E0, 0x7E8)) + tuple(
    range(0x7B0, 0x7B8)
)

_UDS_READ_DATA_BY_IDENTIFIER = 0x22
@dataclass
class ProbeResult:
    """What the probe learned, plus the raw frames for later re-analysis."""

    responses: list[UdsResponse]
    frames: list[CanFrame]
    queried: int
    answered: int


def probe_socketcan(
    channel: str = "can0",
    *,
    addresses: tuple[int, ...] = DEFAULT_REQUEST_ADDRESSES,
    dids: tuple[int, ...] = FINGERPRINT_DIDS,
    timeout: float = 0.15,
    bus: int = 0,
) -> ProbeResult:
    """Query each ECU for each data identifier over SocketCAN.

    Requests are sent one at a time and the reply window is short, because an
    ECU that is going to answer answers immediately. Addresses that never
    respond are simply absent from the result — most of the address space is
    unpopulated on any given car, and that is not an error.

    The interface must *not* be in ``listen-only`` mode for this to work, which
    is a useful safety property: the mode you would use for passive capture
    physically cannot transmit.
    """
    if (
        not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("probe timeout must be positive and finite")
    if not isinstance(bus, int) or isinstance(bus, bool) or not 0 <= bus <= 255:
        raise ValueError("probe bus must be an integer in [0, 255]")
    if any(not isinstance(did, int) or not 0 <= did <= 0xFFFF for did in dids):
        raise ValueError("UDS data identifiers must be integers in [0, 0xFFFF]")
    if any(
        not isinstance(addr, int)
        or isinstance(addr, bool)
        or not 0 <= addr <= 0x1FFFFFFF
        for addr in addresses
    ):
        raise ValueError("CAN request addresses must be integers in [0, 0x1FFFFFFF]")
    if not hasattr(socket, "AF_CAN"):  # pragma: no cover - platform dependent
        raise RuntimeError("SocketCAN is not available on this system")

    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    try:
        sock.bind((channel,))
    except OSError as exc:
        sock.close()
        raise RuntimeError(
            f"cannot open {channel!r}: {exc}. Bring the interface up first, and "
            "note that a listen-only interface cannot transmit."
        ) from exc

    frames: list[CanFrame] = []
    queried = 0

    try:
        for addr in addresses:
            expected = response_address(addr)
            for did in dids:
                request = bytes(
                    [
                        3,  # ISO-TP single frame, 3 payload bytes
                        _UDS_READ_DATA_BY_IDENTIFIER,
                        did >> 8,
                        did & 0xFF,
                    ]
                ).ljust(8, b"\x00")
                _send(sock, addr, request)
                queried += 1
                frames.extend(
                    _collect(
                        sock,
                        timeout=timeout,
                        bus=bus,
                        watch=expected,
                        flow_control_addr=addr,
                    )
                )
    finally:
        sock.close()

    responses = decode_uds(reassemble_isotp(frames))
    answered = len({r.addr for r in responses})
    return ProbeResult(
        responses=responses, frames=frames, queried=queried, answered=answered
    )


def _send(sock: socket.socket, addr: int, data: bytes) -> None:
    import struct

    if not 0 <= addr <= 0x1FFFFFFF:
        raise ValueError(f"CAN address out of range: 0x{addr:X}")
    if len(data) > 8:
        raise ValueError("probe only sends classic CAN payloads up to 8 bytes")
    can_id = addr
    if addr > 0x7FF:
        can_id |= 0x80000000  # extended identifier flag
    sock.send(struct.pack("=IB3x8s", can_id, len(data), data))


def _collect(
    sock: socket.socket,
    *,
    timeout: float,
    bus: int,
    watch: int,
    flow_control_addr: int,
) -> list[CanFrame]:
    """Read frames until the reply window closes.

    Everything received is kept, not just the address expected. A car may use a
    non-standard response address, and a frame that arrived while we were
    listening is evidence worth keeping either way — the caller reassembles the
    whole lot.
    """
    import struct

    out: list[CanFrame] = []
    deadline = time.monotonic() + timeout
    expected_payload_bytes: int | None = None
    received_payload_bytes = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return out
        sock.settimeout(remaining)
        try:
            payload = sock.recv(16)
        except TimeoutError:
            return out
        if len(payload) < 16:
            continue
        can_id, length, data = struct.unpack("=IB3x8s", payload)
        addr = can_id & (0x1FFFFFFF if can_id & 0x80000000 else 0x7FF)
        out.append(
            CanFrame(t=time.time(), addr=addr, data=data[:length], bus=bus)
        )
        if addr != watch or not data:
            continue

        # Treat timeout as an idle timeout for the ECU being queried. A long
        # ISO-TP response can legitimately take longer than one initial reply
        # window, but each consecutive frame should arrive promptly.
        deadline = time.monotonic() + timeout
        pci = data[0] >> 4
        if pci == 0:
            return out  # single-frame reply: nothing more is coming
        if pci == 1 and length >= 2:
            expected_payload_bytes = ((data[0] & 0x0F) << 8) | data[1]
            received_payload_bytes = max(0, length - 2)
            # ISO-TP senders stop after a First Frame until the receiver grants
            # permission to continue. Block size 0 means "send the remainder";
            # STmin 0 asks for no extra delay.
            flow_control = bytes([0x30, 0x00, 0x00]).ljust(8, b"\x00")
            _send(sock, flow_control_addr, flow_control)
        elif pci == 2 and expected_payload_bytes is not None:
            received_payload_bytes += max(0, length - 1)
            if received_payload_bytes >= expected_payload_bytes:
                return out
