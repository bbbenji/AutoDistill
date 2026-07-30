"""Bit-field packing, the inverse of the extractors in :mod:`autodistill_can.frame`.

Used by the synthetic car to build payloads, and by the emitters to show a
worked example of a control message.
"""

from __future__ import annotations

__all__ = ["from_signed", "pack_be", "pack_le_aligned", "pack_le_dbc"]


def from_signed(value: int, length: int) -> int:
    """Encode a signed integer as an unsigned ``length``-bit two's complement."""
    limit = 1 << length
    if not -(limit >> 1) <= value < (limit >> 1):
        raise ValueError(f"{value} does not fit in {length} signed bits")
    return value & (limit - 1)


def pack_be(buf: bytearray, start: int, length: int, value: int) -> None:
    """Write ``value`` into ``buf`` at MSB-first bit index ``start``, in place.

    ``start`` names the field's most significant bit, matching
    :func:`autodistill_can.frame.extract_be`.
    """
    total = len(buf) * 8
    if length <= 0 or start < 0 or start + length > total:
        raise ValueError(
            f"bits [{start}, {start + length}) out of range for {len(buf)} bytes"
        )
    mask = (1 << length) - 1
    shift = total - start - length
    whole = int.from_bytes(buf, "big")
    whole = (whole & ~(mask << shift)) | ((value & mask) << shift)
    buf[:] = whole.to_bytes(len(buf), "big")


def pack_le_aligned(buf: bytearray, start: int, length: int, value: int) -> None:
    """Write a byte-aligned little-endian field, matching ``extract_le_aligned``."""
    if length <= 0 or length % 8 or start % 8:
        raise ValueError("pack_le_aligned requires a byte-aligned span")
    total = len(buf) * 8
    if start < 0 or start + length > total:
        raise ValueError(
            f"bits [{start}, {start + length}) out of range for {len(buf)} bytes"
        )
    nbytes = length // 8
    first = start // 8
    buf[first : first + nbytes] = (value & ((1 << length) - 1)).to_bytes(
        nbytes, "little"
    )


def pack_le_dbc(buf: bytearray, start_bit: int, length: int, value: int) -> None:
    """Write an Intel-ordered field using the DBC convention.

    ``start_bit`` names the field's least significant bit under LSB-first
    numbering, and the field grows upward through increasing indices — the
    inverse of :func:`autodistill_can.frame.extract_le_dbc`. Unlike
    :func:`pack_le_aligned` this handles fields that straddle a byte boundary,
    which real cars are full of.
    """
    total = len(buf) * 8
    if length <= 0 or start_bit < 0 or start_bit + length > total:
        raise ValueError(
            f"bits [{start_bit}, {start_bit + length}) out of range "
            f"for {len(buf)} bytes"
        )
    mask = (1 << length) - 1
    whole = int.from_bytes(buf, "little")
    whole = (whole & ~(mask << start_bit)) | ((value & mask) << start_bit)
    buf[:] = whole.to_bytes(len(buf), "little")
