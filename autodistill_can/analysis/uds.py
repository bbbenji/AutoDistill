"""ISO-TP reassembly and UDS decoding, for firmware-version fingerprinting.

openpilot identifies a car two ways. The cheap way is the set of message
addresses and lengths it broadcasts. The reliable way is the firmware version
strings its ECUs report over UDS, because two model years can share a message
layout but rarely share firmware.

This module reads both out of a *passive* capture. Diagnostic traffic uses
ISO-TP, which splits payloads longer than seven bytes across several frames, so
the frames have to be reassembled before anything can be read. If the car was
talked to by a scan tool during the capture — or by openpilot's own
fingerprinting — the responses are sitting in the log already.

Actively querying the ECUs is a separate matter: it means transmitting onto the
car's bus, so it lives in :mod:`autodistill_can.probe` behind an explicit flag rather
than happening as a side effect of reading a log.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..frame import CanFrame

__all__ = [
    "IsoTpMessage",
    "UdsResponse",
    "decode_uds",
    "request_address",
    "reassemble_isotp",
    "response_address",
]

#: ISO-TP protocol control information, in the top nibble of byte 0.
_SINGLE_FRAME = 0x0
_FIRST_FRAME = 0x1
_CONSECUTIVE_FRAME = 0x2
_FLOW_CONTROL = 0x3

#: Service identifiers worth naming in a report.
UDS_SERVICES = {
    0x10: "DiagnosticSessionControl",
    0x11: "ECUReset",
    0x14: "ClearDiagnosticInformation",
    0x19: "ReadDTCInformation",
    0x22: "ReadDataByIdentifier",
    0x23: "ReadMemoryByAddress",
    0x27: "SecurityAccess",
    0x2E: "WriteDataByIdentifier",
    0x31: "RoutineControl",
    0x3E: "TesterPresent",
    0x62: "ReadDataByIdentifier (response)",
    0x7F: "NegativeResponse",
}

#: Data identifiers openpilot uses when fingerprinting.
UDS_DATA_IDENTIFIERS = {
    0xF180: "bootSoftwareIdentification",
    0xF181: "applicationSoftwareIdentification",
    0xF182: "applicationDataIdentification",
    0xF183: "bootSoftwareFingerprint",
    0xF184: "applicationSoftwareFingerprint",
    0xF186: "activeDiagnosticSession",
    0xF187: "vehicleManufacturerSparePartNumber",
    0xF188: "vehicleManufacturerECUSoftwareNumber",
    0xF189: "vehicleManufacturerECUSoftwareVersionNumber",
    0xF18A: "systemSupplierIdentifier",
    0xF18B: "ECUManufacturingDate",
    0xF18C: "ECUSerialNumber",
    0xF190: "VIN",
    0xF191: "vehicleManufacturerECUHardwareNumber",
    0xF195: "applicationSoftwareVersion",
}


def request_address(response_addr: int) -> int:
    """Return the tester-to-ECU address for a UDS response address.

    openpilot's ``FW_VERSIONS`` keys use the address requests are *sent to*,
    while a passive capture naturally contains the address the ECU responded
    on. Standard 11-bit ISO-TP normally uses response = request + 8. Normal
    29-bit fixed addressing swaps the source and target bytes:
    ``18 DA F1 28`` (ECU 0x28 to tester) becomes ``18 DA 28 F1``.

    Unknown addressing schemes are returned unchanged so a human can correct
    them rather than having the tool invent a mapping.
    """
    if 0x7B8 <= response_addr <= 0x7BF or 0x7E8 <= response_addr <= 0x7EF:
        return response_addr - 8
    if 0x18DA0000 <= response_addr <= 0x18DAFFFF:
        target = (response_addr >> 8) & 0xFF
        source = response_addr & 0xFF
        if target == 0xF1:
            return (response_addr & 0xFFFF0000) | (source << 8) | target
    return response_addr


def response_address(request_addr: int) -> int:
    """Return the conventional ECU response address for a tester request."""
    if request_addr == 0x7DF:
        raise ValueError(
            "0x7DF is a functional broadcast and can have multiple responders; "
            "query physical ECU addresses instead"
        )
    if 0 <= request_addr <= 0x7F7:
        return request_addr + 8
    if 0x18DA0000 <= request_addr <= 0x18DAFFFF:
        target = (request_addr >> 8) & 0xFF
        source = request_addr & 0xFF
        if source == 0xF1:
            return (request_addr & 0xFFFF0000) | (source << 8) | target
    raise ValueError(
        f"cannot infer UDS response address for request 0x{request_addr:X}"
    )


@dataclass
class IsoTpMessage:
    """A reassembled ISO-TP payload."""

    bus: int
    addr: int
    data: bytes
    t_start: float
    t_end: float
    n_frames: int
    #: Set when the declared length was never fully received.
    truncated: bool = False


@dataclass
class UdsResponse:
    """A decoded UDS positive response."""

    bus: int
    addr: int
    service: int
    did: int | None
    payload: bytes
    t: float

    @property
    def service_name(self) -> str:
        return UDS_SERVICES.get(self.service, f"0x{self.service:02X}")

    @property
    def did_name(self) -> str:
        if self.did is None:
            return ""
        return UDS_DATA_IDENTIFIERS.get(self.did, f"0x{self.did:04X}")

    @property
    def request_addr(self) -> int:
        """Address openpilot should query to reproduce this response."""
        return request_address(self.addr)

    def text(self) -> str:
        """The payload as a printable string, which firmware versions are."""
        printable = bytes(b for b in self.payload if 0x20 <= b < 0x7F)
        return printable.decode("ascii", errors="replace").strip()

    def __str__(self) -> str:
        return (
            f"bus{self.bus} 0x{self.addr:03X} {self.did_name}: "
            f"{self.text()!r} ({self.payload.hex()})"
        )


def reassemble_isotp(
    frames: list[CanFrame], *, addresses: set[int] | None = None
) -> list[IsoTpMessage]:
    """Reassemble ISO-TP transfers from a frame list.

    Each ``(bus, addr)`` carries at most one transfer at a time, so state is
    tracked per address. Flow-control frames are skipped: they carry no payload,
    only the receiver's pacing request.

    ``addresses`` restricts which addresses are treated as ISO-TP, and callers
    should almost always set it. ISO-TP has no magic number — a single frame is
    just a length nibble followed by payload — so ordinary broadcast traffic
    parses as valid ISO-TP surprisingly often. A wheel-speed message reading
    ``07 62 07 73 ...`` decodes as a perfectly well-formed 7-byte UDS response,
    and left unchecked those phantoms end up in a firmware fingerprint.

    Frames that do not look like ISO-TP are skipped silently rather than
    reported: most of a capture is not ISO-TP, and warning about each frame
    would bury the real findings.
    """
    pending: dict[tuple[int, int], dict] = {}
    out: list[IsoTpMessage] = []

    for frame in frames:
        if not frame.data:
            continue
        if addresses is not None and frame.addr not in addresses:
            continue
        key = frame.key
        pci = frame.data[0] >> 4
        state = pending.get(key)

        if pci == _SINGLE_FRAME:
            size = frame.data[0] & 0x0F
            if size == 0 or size > len(frame.data) - 1:
                continue
            out.append(
                IsoTpMessage(
                    bus=frame.bus, addr=frame.addr, data=frame.data[1 : 1 + size],
                    t_start=frame.t, t_end=frame.t, n_frames=1,
                )
            )
            pending.pop(key, None)

        elif pci == _FIRST_FRAME:
            if len(frame.data) < 2:
                continue
            total = ((frame.data[0] & 0x0F) << 8) | frame.data[1]
            if total <= 7:
                continue  # would have been a single frame
            pending[key] = {
                "total": total,
                "data": bytearray(frame.data[2:]),
                "t_start": frame.t,
                "next_seq": 1,
                "n_frames": 1,
            }

        elif pci == _CONSECUTIVE_FRAME:
            if state is None:
                continue  # consecutive frame with no first frame; capture gap
            seq = frame.data[0] & 0x0F
            if seq != state["next_seq"] & 0x0F:
                # Lost a frame. The payload cannot be trusted, so drop it
                # rather than silently splicing the wrong bytes together.
                pending.pop(key, None)
                continue
            state["data"].extend(frame.data[1:])
            state["next_seq"] += 1
            state["n_frames"] += 1
            if len(state["data"]) >= state["total"]:
                out.append(
                    IsoTpMessage(
                        bus=frame.bus, addr=frame.addr,
                        data=bytes(state["data"][: state["total"]]),
                        t_start=state["t_start"], t_end=frame.t,
                        n_frames=state["n_frames"],
                    )
                )
                pending.pop(key, None)

        elif pci == _FLOW_CONTROL:
            continue

    for key, state in pending.items():
        out.append(
            IsoTpMessage(
                bus=key[0], addr=key[1], data=bytes(state["data"]),
                t_start=state["t_start"], t_end=state["t_start"],
                n_frames=state["n_frames"], truncated=True,
            )
        )

    return out


def decode_uds(messages: list[IsoTpMessage]) -> list[UdsResponse]:
    """Pick the UDS positive responses out of reassembled ISO-TP payloads.

    Only responses are returned. A request tells you what a scan tool asked for;
    a response tells you what the car *is*, which is what fingerprinting needs.
    """
    out: list[UdsResponse] = []
    for message in messages:
        if message.truncated or len(message.data) < 2:
            continue
        service = message.data[0]
        # Positive responses set bit 6 of the request's service id.
        if not service & 0x40:
            continue
        request_service = service & ~0x40
        if request_service not in UDS_SERVICES:
            # Not a service any tester actually asks for; almost certainly
            # ordinary traffic that happened to parse as ISO-TP.
            continue

        did: int | None = None
        payload = message.data[1:]
        if request_service in (0x22, 0x2E) and len(message.data) >= 3:
            did = (message.data[1] << 8) | message.data[2]
            payload = message.data[3:]

        out.append(
            UdsResponse(
                bus=message.bus, addr=message.addr, service=service,
                did=did, payload=payload, t=message.t_start,
            )
        )
    return out
