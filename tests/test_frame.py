"""Bit extraction, packing, and the DBC numbering conversion.

Bit numbering is the single most error-prone part of this package: three
conventions coexist (MSB-first internal, DBC Motorola, DBC Intel) and a mistake
in any of them produces plausible-looking signals that are silently wrong. So
the conversions are checked by round-trip against independent implementations
rather than against themselves.
"""

from __future__ import annotations

import random

import pytest

from autodistill_can.bitpack import from_signed, pack_be, pack_le_aligned
from autodistill_can.emit.dbc import dbc_start_bit
from autodistill_can.frame import (
    CanFrame,
    CanLog,
    MessageStream,
    concatenate,
    extract_be,
    extract_le_aligned,
    extract_le_dbc,
    payload_bits,
    resample,
    to_signed,
)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"t": float("nan")},
        {"t": float("inf")},
        {"addr": -1},
        {"addr": 0x20000000},
        {"bus": -1},
        {"bus": 256},
        {"data": b"\x00" * 65},
    ],
)
def test_frame_rejects_values_that_cannot_exist_on_can(kwargs):
    values = {"t": 1.0, "addr": 0x123, "data": b"\x00", "bus": 0}
    values.update(kwargs)
    with pytest.raises((TypeError, ValueError)):
        CanFrame(**values)


def test_frame_requires_an_immutable_bytes_payload():
    with pytest.raises(TypeError):
        CanFrame(t=1.0, addr=0x123, data=bytearray(b"\x00"))


def test_extract_be_reads_msb_first():
    data = bytes([0b10110010, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    assert extract_be(data, 0, 1) == 1
    assert extract_be(data, 1, 1) == 0
    assert extract_be(data, 0, 4) == 0b1011
    assert extract_be(data, 0, 8) == 0b10110010


def test_extract_be_spans_bytes():
    data = bytes([0x12, 0x34, 0x56, 0x78, 0, 0, 0, 0])
    assert extract_be(data, 0, 16) == 0x1234
    assert extract_be(data, 8, 16) == 0x3456
    assert extract_be(data, 0, 32) == 0x12345678


def test_extract_le_aligned_reverses_bytes():
    data = bytes([0x34, 0x12, 0, 0, 0, 0, 0, 0])
    assert extract_le_aligned(data, 0, 16) == 0x1234
    assert extract_be(data, 0, 16) == 0x3412


def test_extract_le_aligned_rejects_unaligned_spans():
    data = bytes(8)
    with pytest.raises(ValueError):
        extract_le_aligned(data, 3, 16)
    with pytest.raises(ValueError):
        extract_le_aligned(data, 0, 12)


def test_extract_out_of_range():
    with pytest.raises(ValueError):
        extract_be(bytes(2), 8, 16)


def test_to_signed():
    assert to_signed(0xFFFF, 16) == -1
    assert to_signed(0x8000, 16) == -32768
    assert to_signed(0x7FFF, 16) == 32767
    assert to_signed(0x0F, 4) == -1


def test_pack_extract_round_trip():
    rng = random.Random(4)
    for _ in range(2000):
        length = rng.randint(1, 32)
        start = rng.randrange(0, 64 - length + 1)
        value = rng.randrange(0, 1 << length)
        buf = bytearray(8)
        pack_be(buf, start, length, value)
        assert extract_be(bytes(buf), start, length) == value


def test_pack_be_leaves_neighbouring_bits_alone():
    buf = bytearray(b"\xff" * 8)
    pack_be(buf, 8, 8, 0x00)
    assert bytes(buf) == b"\xff\x00\xff\xff\xff\xff\xff\xff"


def test_pack_le_aligned_round_trip():
    rng = random.Random(5)
    for _ in range(500):
        nbytes = rng.choice([1, 2, 4])
        start = rng.randrange(0, 8 - nbytes + 1) * 8
        value = rng.randrange(0, 1 << (nbytes * 8))
        buf = bytearray(8)
        pack_le_aligned(buf, start, nbytes * 8, value)
        assert extract_le_aligned(bytes(buf), start, nbytes * 8) == value


def test_from_signed_round_trip():
    for length in (4, 8, 12, 16):
        limit = 1 << (length - 1)
        for value in (-limit, -1, 0, 1, limit - 1):
            assert to_signed(from_signed(value, length), length) == value


def test_from_signed_rejects_overflow():
    with pytest.raises(ValueError):
        from_signed(128, 8)


def test_payload_bits():
    assert payload_bits(b"\xa0") == [1, 0, 1, 0, 0, 0, 0, 0]


def _decode_motorola(data: bytes, start_bit: int, length: int) -> int:
    """Independent DBC big-endian decoder, walking the sawtooth by hand."""
    value = 0
    bit = start_bit
    for _ in range(length):
        byte, offset = divmod(bit, 8)
        value = (value << 1) | ((data[byte] >> offset) & 1)
        bit = bit - 1 if bit % 8 else bit + 15
    return value


def test_dbc_start_bit_big_endian_matches_independent_decoder():
    rng = random.Random(6)
    for _ in range(3000):
        length = rng.choice([1, 2, 4, 8, 10, 12, 16, 32])
        start = rng.randrange(0, 64 - length + 1)
        data = bytes(rng.randrange(256) for _ in range(8))
        sb = dbc_start_bit(start, length, True)
        assert _decode_motorola(data, sb, length) == extract_be(data, start, length)


def test_dbc_start_bit_little_endian_matches_dbc_decoder():
    rng = random.Random(7)
    for _ in range(2000):
        nbytes = rng.choice([1, 2, 4])
        start = rng.randrange(0, 8 - nbytes + 1) * 8
        length = nbytes * 8
        data = bytes(rng.randrange(256) for _ in range(8))
        sb = dbc_start_bit(start, length, False)
        assert extract_le_dbc(data, sb, length) == extract_le_aligned(
            data, start, length
        )


def test_dbc_start_bit_known_values():
    # A 16-bit Motorola signal at the top of byte 0 starts at DBC bit 7.
    assert dbc_start_bit(0, 16, True) == 7
    # The Intel equivalent names its least significant bit, DBC bit 0.
    assert dbc_start_bit(0, 16, False) == 0
    assert dbc_start_bit(16, 16, False) == 16


def test_message_stream_timing():
    stream = MessageStream(bus=0, addr=0x100)
    for i in range(101):
        stream.add(CanFrame(t=i * 0.01, addr=0x100, data=b"\x00" * 8))
    assert stream.period == pytest.approx(0.01)
    assert stream.frequency == pytest.approx(100.0)
    assert stream.jitter() == pytest.approx(0.0, abs=1e-12)


def test_message_stream_length_varies():
    stream = MessageStream(bus=0, addr=0x100)
    stream.add(CanFrame(t=0.0, addr=0x100, data=b"\x01\x02\x03"))
    stream.add(CanFrame(t=0.1, addr=0x100, data=b"\x01\x02"))
    assert stream.length_varies
    assert stream.length == 2
    assert stream.nbits == 16


def test_canlog_groups_by_bus_and_address():
    log = CanLog.from_frames(
        [
            CanFrame(t=0.0, addr=0x100, data=b"\x00", bus=0),
            CanFrame(t=0.1, addr=0x100, data=b"\x01", bus=2),
            CanFrame(t=0.2, addr=0x100, data=b"\x02", bus=0),
        ]
    )
    # The same address on two buses is two different messages.
    assert len(log.streams) == 2
    assert len(log.streams[(0, 0x100)]) == 2
    assert len(log.streams[(2, 0x100)]) == 1
    assert log.buses == [0, 2]


def test_canlog_frames_are_re_emitted_in_time_order():
    log = CanLog.from_frames(
        [
            CanFrame(t=0.3, addr=0x200, data=b"\x00"),
            CanFrame(t=0.1, addr=0x100, data=b"\x00"),
            CanFrame(t=0.2, addr=0x300, data=b"\x00"),
        ]
    )
    assert [f.t for f in log.frames()] == [0.1, 0.2, 0.3]


def test_resample_holds_last_value():
    # Zero-order hold, not interpolation: a CAN signal really does hold its
    # last transmitted value, and interpolating would invent transitions.
    times = [0.0, 1.0, 2.0]
    values = [10.0, 20.0, 30.0]
    assert resample(times, values, [-1.0, 0.0, 0.5, 1.0, 1.9, 5.0]) == [
        10.0, 10.0, 10.0, 20.0, 20.0, 30.0,
    ]


# --------------------------------------------------------------------------
# Concatenating captures


def _cyclic(period: float, count: int, addr: int = 0x25, start: float = 0.0):
    log = CanLog()
    for i in range(count):
        log.add(CanFrame(t=start + i * period, addr=addr, bus=0,
                         data=bytes([i % 256]) + b"\0" * 7))
    return log


def test_concatenating_preserves_each_messages_cadence():
    """Separate captures each start from their own zero.

    Added together as they are, two recordings of the same car interleave into
    one impossible conversation: every message appears at twice its real rate
    with the jitter to match, which is enough to reclassify a steady broadcast
    as event-driven. Laid end to end, each keeps the cadence it was recorded
    at.
    """
    merged = concatenate([_cyclic(0.01, 2000), _cyclic(0.01, 2000)])
    stream = merged.sorted_streams()[0]

    assert len(stream) == 4000
    assert stream.period == pytest.approx(0.01, rel=1e-3)
    # One seam in four thousand frames, nowhere near the quarter-period that
    # would make this look event-driven.
    assert stream.jitter() < 0.25 * stream.period


def test_interleaving_is_what_concatenating_avoids():
    """The failure the offset exists to prevent, pinned so it stays prevented."""
    naive = CanLog()
    for log in (_cyclic(0.01, 2000), _cyclic(0.01, 2000)):
        for frame in log.frames():
            naive.add(frame)
    naive.sort()
    stream = naive.sorted_streams()[0]

    # Half the true period: the two drives fell on top of each other.
    assert stream.period == pytest.approx(0.005, rel=1e-2)
    assert stream.jitter() > 0.25 * stream.period


def test_concatenating_keeps_captures_in_the_order_given():
    merged = concatenate([_cyclic(0.01, 100), _cyclic(0.01, 100)])
    times = merged.sorted_streams()[0].times
    assert times == sorted(times)
    assert times[100] > times[99], "the second capture must start after the first"


def test_concatenating_one_capture_changes_nothing():
    single = _cyclic(0.01, 500)
    merged = concatenate([_cyclic(0.01, 500)])
    assert merged.sorted_streams()[0].times == single.sorted_streams()[0].times


def test_concatenating_skips_empty_captures():
    merged = concatenate([CanLog(), _cyclic(0.01, 50), CanLog()])
    assert len(merged.sorted_streams()[0]) == 50
