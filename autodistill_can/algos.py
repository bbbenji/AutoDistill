"""Checksum and CRC primitives used on real vehicle buses.

Two things need these: the solver in :mod:`autodistill_can.analysis.checksum`, which
tries them against observed traffic, and the synthetic car in
:mod:`autodistill_can.synth`, which uses them to generate traffic worth solving.

Every function here takes the message address, the payload with the checksum
field already zeroed or excluded, and the message length, because OEM checksums
routinely mix the address and length into the sum — that is exactly what stops
you replaying a message on a different id.

Sources for the OEM-specific formulas are opendbc's ``can/common.cc`` and the
per-brand ``carcontroller``/``fw_versions`` code in openpilot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

__all__ = [
    "CHECKSUM_ALGOS",
    "KNOWN_CRC16",
    "KNOWN_CRC8",
    "ChecksumAlgo",
    "Crc16Params",
    "Crc8Params",
    "crc16",
    "crc8",
    "honda_checksum",
    "hyundai_crc8",
    "nibble_sum",
    "nibble_xor",
    "reflect",
    "subaru_checksum",
    "toyota_checksum",
    "volkswagen_mqb_checksum",
    "xor8",
]


def reflect(value: int, width: int) -> int:
    """Reverse the low ``width`` bits of ``value``."""
    out = 0
    for _ in range(width):
        out = (out << 1) | (value & 1)
        value >>= 1
    return out


def crc8(
    data: Iterable[int],
    poly: int = 0x07,
    init: int = 0x00,
    xorout: int = 0x00,
    refin: bool = False,
    refout: bool = False,
) -> int:
    """Table-free CRC-8 with the usual configurable parameters.

    Defaults are CRC-8/SMBUS. The two that matter most for cars:

    * ``poly=0x2F, init=0xFF, xorout=0xFF`` — AUTOSAR CRC8H2F, used by VW MQB
      and a lot of modern European traffic.
    * ``poly=0x1D, init=0xFF, xorout=0xFF`` — SAE-J1850, used by Ford.
    """
    crc = init
    for byte in data:
        if refin:
            byte = reflect(byte, 8)
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    if refout:
        crc = reflect(crc, 8)
    return crc ^ xorout


def crc16(
    data: Iterable[int],
    poly: int = 0x1021,
    init: int = 0xFFFF,
    xorout: int = 0x0000,
    refin: bool = False,
    refout: bool = False,
) -> int:
    """Table-free CRC-16, the same shape as :func:`crc8`, defaulting to
    CCITT-FALSE.

    A 16-bit checksum is not exotic once payloads reach 64 bytes: eight bits of
    protection over sixty-four is thin, so CAN-FD platforms moved to these.
    Hyundai's CAN-FD traffic is the one most people meet first.
    """
    crc = init
    for byte in data:
        if refin:
            byte = reflect(byte, 8)
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    if refout:
        crc = reflect(crc, 16)
    return crc ^ xorout


def xor8(data: Iterable[int]) -> int:
    out = 0
    for byte in data:
        out ^= byte
    return out & 0xFF


def nibble_sum(data: Iterable[int]) -> int:
    """Sum of every nibble. The basis of several Asian-OEM 4-bit checksums."""
    total = 0
    for byte in data:
        total += (byte & 0x0F) + (byte >> 4)
    return total


def nibble_xor(data: Iterable[int]) -> int:
    """XOR of every nibble, giving a 4-bit result.

    Common on Hyundai/Kia chassis messages; observed on a real Kia Soul EV's
    steering-angle message, where the checksum nibble sits beside the rolling
    counter and covers every other nibble in the payload.
    """
    out = 0
    for byte in data:
        out ^= (byte & 0x0F) ^ (byte >> 4)
    return out & 0x0F


def toyota_checksum(addr: int, data: bytes, length: int) -> int:
    """Toyota's 8-bit checksum, occupying the last byte of the message.

    ``data`` is the payload without its checksum byte. Folding in the address
    and length is what makes it message-specific.
    """
    # Toyota adds every byte of the CAN identifier separately.  Adding the
    # integer address happens to agree for 8-bit identifiers, which let the
    # old implementation pass synthetic tests while missing every normal
    # command such as STEERING_LKA (0x2E4) by the high address byte.
    total = length + sum(data)
    while addr:
        total += addr & 0xFF
        addr >>= 8
    return total & 0xFF


def honda_checksum(addr: int, data: bytes, length: int) -> int:
    """Honda's 4-bit checksum, in the low nibble of the last byte.

    ``data`` is the full payload; its final low nibble is ignored, since that is
    where the result goes.

    Messages on a 29-bit identifier take an extra ``+3``. That is not a
    derivable rule, just what Honda does, and omitting it produces a checksum
    that is right on every standard-id message and wrong on every extended one —
    which is the kind of bug that surfaces only once a real car ignores your
    frames. Matches ``honda_checksum`` in opendbc's ``hondacan.py``.
    """
    total = 0
    shifted = addr
    while shifted:
        total += shifted & 0x0F
        shifted >>= 4
    for i, byte in enumerate(data[:length]):
        if i == length - 1:
            # Final byte contributes only its high nibble.
            total += byte >> 4
        else:
            total += (byte & 0x0F) + (byte >> 4)
    total = 8 - total
    if addr > 0x7FF:
        total += 3
    return total & 0x0F


def hyundai_crc8(addr: int, data: bytes, length: int) -> int:
    """Hyundai/Kia's CRC8, as used on LKAS11 and friends.

    A J1850 polynomial with Hyundai's own init and final XOR rather than the
    usual 0xFF/0xFF. opendbc writes it as
    ``mk_crc8_fun(CRC8J1850, init_crc=0xFD, xor_out=0xDF)``.

    Note the trap in that spelling: opendbc's ``init_crc`` is *not* the value
    the register starts at. Its helper begins from ``init_crc ^ xor_out``, so
    the equivalent conventional parameters are init 0x22, not 0xFD. Copying
    0xFD across produces a checksum that disagrees with the car on every single
    frame.

    Hyundai's other two variants are plain byte sums, already reachable as
    ``sum8`` over the appropriate byte selection.
    """
    return crc8(data, poly=0x1D, init=0x22, xorout=0xDF)


def subaru_checksum(addr: int, data: bytes, length: int) -> int:
    """Subaru's checksum: sum of the address bytes and payload."""
    return (sum(data) + (addr & 0xFF) + (addr >> 8)) & 0xFF


def volkswagen_mqb_checksum(
    addr: int, data: bytes, length: int, counter: int, magic: bytes
) -> int:
    """VW MQB's CRC8H2F over the payload plus a counter-selected magic byte.

    MQB messages carry the checksum in byte 0 and a counter in the low nibble of
    byte 1. The CRC runs over bytes 1..n followed by a magic byte chosen from a
    16-entry per-address table indexed by the counter. That table is the only
    non-generic part, which is precisely why the generic GF(2) solver in
    :mod:`autodistill_can.analysis.checksum` is worth having: it recovers the
    per-counter constant without knowing the table.
    """
    body = bytes(data[1:length]) + bytes([magic[counter & 0x0F]])
    return crc8(body, poly=0x2F, init=0xFF, xorout=0xFF)


@dataclass(frozen=True)
class ChecksumAlgo:
    """A named candidate algorithm the solver can test against real traffic.

    ``fn`` receives ``(addr, payload_without_checksum, length)`` and returns the
    expected field value. ``width`` is the field width in bits, so 4-bit nibble
    checksums and 8-bit sums can live in the same registry.
    """

    name: str
    width: int
    fn: Callable[[int, bytes, int], int]
    #: Human-readable note carried into the report and generated code.
    note: str = ""


def _sum8(addr: int, data: bytes, length: int) -> int:
    return sum(data) & 0xFF


def _sum8_complement(addr: int, data: bytes, length: int) -> int:
    return (~sum(data)) & 0xFF


def _sum8_twos(addr: int, data: bytes, length: int) -> int:
    return (-sum(data)) & 0xFF


def _sum8_plus_len(addr: int, data: bytes, length: int) -> int:
    return (sum(data) + length) & 0xFF


def _xor8(addr: int, data: bytes, length: int) -> int:
    return xor8(data)


def _nibble_sum8(addr: int, data: bytes, length: int) -> int:
    return nibble_sum(data) & 0xFF


def _nibble_xor4(addr: int, data: bytes, length: int) -> int:
    return nibble_xor(data)


def _crc8_hyundai(addr: int, data: bytes, length: int) -> int:
    return hyundai_crc8(addr, data, length)


def _crc8_autosar(addr: int, data: bytes, length: int) -> int:
    return crc8(data, poly=0x2F, init=0xFF, xorout=0xFF)


def _crc8_j1850(addr: int, data: bytes, length: int) -> int:
    return crc8(data, poly=0x1D, init=0xFF, xorout=0xFF)


def _crc8_smbus(addr: int, data: bytes, length: int) -> int:
    return crc8(data, poly=0x07, init=0x00, xorout=0x00)


def _crc8_1d_zero(addr: int, data: bytes, length: int) -> int:
    return crc8(data, poly=0x1D, init=0x00, xorout=0x00)


#: Ordered so that the more specific, address-mixing algorithms are reported in
#: preference to a bare sum that happens to fit as well.
CHECKSUM_ALGOS: tuple[ChecksumAlgo, ...] = (
    ChecksumAlgo(
        "toyota", 8, toyota_checksum,
        "(sum(addr bytes) + len + sum(data)) & 0xFF",
    ),
    ChecksumAlgo("subaru", 8, subaru_checksum, "sum(data) + addr bytes"),
    ChecksumAlgo("sum8", 8, _sum8, "sum(data) & 0xFF"),
    ChecksumAlgo("sum8_plus_len", 8, _sum8_plus_len, "(sum(data) + len) & 0xFF"),
    ChecksumAlgo("sum8_complement", 8, _sum8_complement, "~sum(data) & 0xFF"),
    ChecksumAlgo("sum8_twos", 8, _sum8_twos, "-sum(data) & 0xFF"),
    ChecksumAlgo("xor8", 8, _xor8, "XOR of data bytes"),
    ChecksumAlgo("nibble_sum8", 8, _nibble_sum8, "sum of all nibbles & 0xFF"),
    ChecksumAlgo("crc8_autosar_2f", 8, _crc8_autosar, "CRC8H2F poly=0x2F init=0xFF xorout=0xFF"),
    ChecksumAlgo("crc8_j1850", 8, _crc8_j1850, "CRC8 poly=0x1D init=0xFF xorout=0xFF"),
    ChecksumAlgo("crc8_hyundai", 8, _crc8_hyundai,
                 "Hyundai CRC8 poly=0x1D register=0x22 xorout=0xDF"),
    ChecksumAlgo("crc8_smbus", 8, _crc8_smbus, "CRC8 poly=0x07 init=0x00"),
    ChecksumAlgo("crc8_1d", 8, _crc8_1d_zero, "CRC8 poly=0x1D init=0x00"),
    ChecksumAlgo("honda", 4, honda_checksum, "4-bit, (8 - nibble sum incl. addr) & 0xF"),
    ChecksumAlgo("nibble_xor4", 4, _nibble_xor4, "4-bit, XOR of every other nibble"),
)

@dataclass(frozen=True)
class Crc8Params:
    """A named CRC-8 parameter set, as ``(poly, init, xorout, refin, refout)``.

    Check values are for the standard ``b"123456789"`` input and are asserted in
    the test suite, so a typo here fails loudly rather than silently mislabelling
    a recovered polynomial.
    """

    name: str
    poly: int
    init: int
    xorout: int
    refin: bool = False
    refout: bool = False
    check: int = 0

    #: Output width in bits, so the two registries can be handled uniformly.
    width: int = 8

    def __call__(self, data: Iterable[int]) -> int:
        return crc8(data, self.poly, self.init, self.xorout, self.refin, self.refout)


@dataclass(frozen=True)
class Crc16Params:
    """A named CRC-16 parameter set. Same contract as :class:`Crc8Params`."""

    name: str
    poly: int
    init: int
    xorout: int
    refin: bool = False
    refout: bool = False
    check: int = 0

    #: Output width in bits, so the two registries can be handled uniformly.
    width: int = 16

    def __call__(self, data: Iterable[int]) -> int:
        return crc16(data, self.poly, self.init, self.xorout, self.refin, self.refout)


#: Known CRC-16 parameter sets. CAN-FD raised payloads to 64 bytes, and eight
#: bits of protection over sixty-four is thin, so 16-bit checksums are now
#: normal on modern platforms -- Hyundai's CAN-FD traffic among them.
KNOWN_CRC16: tuple[Crc16Params, ...] = (
    Crc16Params("CRC-16/XMODEM", 0x1021, 0x0000, 0x0000, check=0x31C3),
    Crc16Params("CRC-16/CCITT-FALSE", 0x1021, 0xFFFF, 0x0000, check=0x29B1),
    Crc16Params("CRC-16/GENIBUS", 0x1021, 0xFFFF, 0xFFFF, check=0xD64E),
    Crc16Params("CRC-16/ARC", 0x8005, 0x0000, 0x0000, True, True, check=0xBB3D),
    Crc16Params("CRC-16/MODBUS", 0x8005, 0xFFFF, 0x0000, True, True, check=0x4B37),
    Crc16Params("CRC-16/USB", 0x8005, 0xFFFF, 0xFFFF, True, True, check=0xB4C8),
    Crc16Params("CRC-16/KERMIT", 0x1021, 0x0000, 0x0000, True, True, check=0x2189),
)


#: Known CRC-8 parameter sets, used to put a name to a polynomial that the
#: generic GF(2) solver recovered. Car buses use the non-reflected sets almost
#: exclusively, but the reflected ones are cheap to keep and do turn up.
KNOWN_CRC8: tuple[Crc8Params, ...] = (
    Crc8Params("CRC-8/AUTOSAR-H2F", 0x2F, 0xFF, 0xFF, check=0xDF),
    Crc8Params("CRC-8/SAE-J1850", 0x1D, 0xFF, 0xFF, check=0x4B),
    Crc8Params("CRC-8/SMBUS", 0x07, 0x00, 0x00, check=0xF4),
    Crc8Params("CRC-8/CDMA2000", 0x9B, 0xFF, 0x00, check=0xDA),
    Crc8Params("CRC-8/ITU", 0x07, 0x00, 0x55, check=0xA1),
    Crc8Params("CRC-8/8H2F-ZERO", 0x2F, 0x00, 0x00, check=0x3E),
    Crc8Params("CRC-8/1D-ZERO", 0x1D, 0x00, 0x00, check=0x37),
    # Vendor parameter sets, from opendbc. Worth naming because the generic
    # GF(2) solver recovers the map either way, and a name is what the person
    # writing the port can act on.
    Crc8Params("CRC-8/HYUNDAI", 0x1D, 0x22, 0xDF, check=0x1E),
    Crc8Params("CRC-8/OPENDBC-BODY", 0xD5, 0x00, 0x00, check=0xBC),
    Crc8Params("CRC-8/MAXIM-DOW", 0x31, 0x00, 0x00, True, True, check=0xA1),
    Crc8Params("CRC-8/DARC", 0x39, 0x00, 0x00, True, True, check=0x15),
    Crc8Params("CRC-8/ROHC", 0x07, 0xFF, 0x00, True, True, check=0xD0),
)
