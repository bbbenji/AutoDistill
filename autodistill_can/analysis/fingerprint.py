"""Fingerprint generation for openpilot.

openpilot recognises a car before it can drive it. Two mechanisms:

* a **message fingerprint** — the ``{address: length}`` map of everything the
  car broadcasts, matched against known cars; and
* **firmware versions** — strings read from each ECU over UDS, which distinguish
  model years that share a message layout.

Both come straight out of a capture. The message fingerprint needs a little
care: diagnostic and tester traffic must be excluded, or the fingerprint
depends on whether a scan tool happened to be plugged in, and openpilot would
fail to match the car in normal use.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..frame import CanLog
from .uds import UdsResponse, decode_uds, reassemble_isotp

__all__ = ["Fingerprint", "build_fingerprint", "is_diagnostic_address"]

#: Standard OBD-II / UDS diagnostic ranges. These are request/response channels
#: for a scan tool, not the car's own broadcast traffic, so they must not enter
#: a fingerprint.
_OBD_FUNCTIONAL_REQUEST = 0x7DF
_OBD_PHYSICAL_LOW = 0x7E0
_OBD_PHYSICAL_HIGH = 0x7EF
#: Many OEMs use a second block just below the standard one.
_OEM_DIAG_LOW = 0x7B0
_OEM_DIAG_HIGH = 0x7BF
#: 29-bit diagnostic addressing (ISO 15765-4 extended).
_EXT_DIAG_LOW = 0x18DA0000
_EXT_DIAG_HIGH = 0x18DBFFFF


def is_diagnostic_address(addr: int) -> bool:
    """Whether an address belongs to diagnostics rather than normal traffic."""
    if addr == _OBD_FUNCTIONAL_REQUEST:
        return True
    if _OBD_PHYSICAL_LOW <= addr <= _OBD_PHYSICAL_HIGH:
        return True
    if _OEM_DIAG_LOW <= addr <= _OEM_DIAG_HIGH:
        return True
    if _EXT_DIAG_LOW <= addr <= _EXT_DIAG_HIGH:
        return True
    return False


@dataclass
class Fingerprint:
    """What openpilot needs to recognise this car."""

    #: bus -> {address: payload length}, diagnostics excluded.
    buses: dict[int, dict[int, int]] = field(default_factory=dict)
    #: Firmware strings recovered from passively observed UDS responses.
    firmware: list[UdsResponse] = field(default_factory=list)
    #: Addresses left out, and why.
    excluded: dict[int, str] = field(default_factory=dict)
    #: VIN, if one was seen in a UDS response.
    vin: str = ""

    @property
    def total_messages(self) -> int:
        return sum(len(m) for m in self.buses.values())

    def as_openpilot_dict(self, bus: int = 0) -> dict[int, int]:
        """The ``{addr: length}`` mapping for one bus, in openpilot's format."""
        return dict(sorted(self.buses.get(bus, {}).items()))


def build_fingerprint(
    log: CanLog,
    *,
    min_frames: int = 3,
    include_diagnostics: bool = False,
    diagnostic_log: CanLog | None = None,
) -> Fingerprint:
    """Derive a fingerprint from a capture.

    Messages seen fewer than ``min_frames`` times are excluded. A fingerprint
    has to match on every drive, and a message that appeared twice in a
    ten-minute capture — a one-off power-up announcement, or a frame caught
    mid-transition — will not reliably appear in the next one.
    """
    fingerprint = Fingerprint()

    for (bus, addr), stream in sorted(log.streams.items()):
        if not include_diagnostics and is_diagnostic_address(addr):
            fingerprint.excluded[addr] = (
                "diagnostic address: present only when a scan tool is connected"
            )
            continue
        if addr > 0x7FF:
            fingerprint.excluded[addr] = (
                "29-bit address: openpilot's legacy CAN fingerprint only "
                "matches 11-bit broadcast identifiers"
            )
            continue
        if len(stream) < min_frames:
            fingerprint.excluded[addr] = (
                f"seen only {len(stream)} time(s); too rare to fingerprint on"
            )
            continue
        if stream.length_varies:
            fingerprint.excluded[addr] = (
                "payload length varies between frames, so no single length "
                "describes it"
            )
            continue
        fingerprint.buses.setdefault(bus, {})[addr] = stream.length

    # Only diagnostic addresses are parsed as ISO-TP. Broadcast traffic parses
    # as valid ISO-TP often enough to inject phantom firmware strings otherwise.
    firmware_source = diagnostic_log if diagnostic_log is not None else log
    diagnostic = {
        addr for _bus, addr in firmware_source.streams
        if is_diagnostic_address(addr)
    }
    responses = decode_uds(
        reassemble_isotp(list(firmware_source.frames()), addresses=diagnostic)
    )
    fingerprint.firmware = [r for r in responses if r.did is not None and r.payload]
    for response in fingerprint.firmware:
        if response.did == 0xF190:
            fingerprint.vin = response.text()

    return fingerprint
