"""Checksum primitives, checked against published values.

These are the foundation the solver and the synthetic car both stand on, so
they are pinned to the standard catalogue check values rather than to each
other. A typo here would otherwise cancel itself out: the generator would
produce traffic matching the same wrong formula the solver looks for, and every
higher-level test would pass while the emitted openpilot code was wrong.
"""

from __future__ import annotations

import pytest

from autodistill_can.algos import (
    CHECKSUM_ALGOS,
    KNOWN_CRC8,
    KNOWN_CRC16,
    crc16,
    honda_checksum,
    hyundai_crc8,
    reflect,
    toyota_checksum,
    xor8,
)

CHECK_INPUT = b"123456789"


@pytest.mark.parametrize("params", KNOWN_CRC8, ids=lambda p: p.name)
def test_known_crc8_matches_catalogue_check_value(params):
    assert params(CHECK_INPUT) == params.check


@pytest.mark.parametrize("params", KNOWN_CRC16, ids=lambda p: p.name)
def test_known_crc16_matches_catalogue_check_value(params):
    # Same contract as the CRC-8 registry: a typo in a parameter set fails
    # here rather than silently mislabelling a recovered polynomial.
    assert params(CHECK_INPUT) == params.check


def test_crc16_ccitt_false():
    assert crc16(CHECK_INPUT, 0x1021, 0xFFFF, 0x0000) == 0x29B1


def test_both_crc_registries_report_their_width():
    # `_identify_crc` selects a registry by the recovered field's width, so
    # the two have to agree on how they describe themselves.
    assert {p.width for p in KNOWN_CRC8} == {8}
    assert {p.width for p in KNOWN_CRC16} == {16}


def test_reflect():
    assert reflect(0b1101, 4) == 0b1011
    assert reflect(0x01, 8) == 0x80
    assert reflect(reflect(0xA5, 8), 8) == 0xA5


def test_xor8():
    assert xor8(b"\x01\x02\x03") == 0x00
    assert xor8(b"\xff\x0f") == 0xF0


def test_toyota_checksum_folds_in_address_and_length():
    data = b"\x01\x02\x03"
    # The same payload on a different address must not produce the same value;
    # that property is what stops a message being replayed onto another id.
    assert toyota_checksum(0x25, data, 8) != toyota_checksum(0x26, data, 8)
    assert toyota_checksum(0x25, data, 8) == (0x25 + 8 + 6) & 0xFF


def test_toyota_checksum_matches_real_rav4_steering_command():
    """Pinned to comma2k19 STEERING_LKA and opendbc's Toyota formula.

    The high byte of 0x2E4 contributes 0x02 separately.  Treating the address
    as one integer gives 0xA7 and misses the captured 0xA9 checksum.
    """
    payload = bytes.fromhex("BE000000A9")
    assert toyota_checksum(0x2E4, payload[:-1], len(payload)) == payload[-1]


def test_hyundai_crc8_matches_opendbc():
    """Pinned to opendbc's shipped Hyundai CRC.

    opendbc spells it ``mk_crc8_fun(CRC8J1850, init_crc=0xFD, xor_out=0xDF)``,
    but that helper starts the register at ``init_crc ^ xor_out`` -- so the
    conventional init is 0x22, and copying 0xFD across disagrees with the car on
    every frame.
    """
    def _table(poly):
        out = []
        for i in range(256):
            c = i
            for _ in range(8):
                c = ((c << 1) ^ poly) & 0xFF if c & 0x80 else (c << 1) & 0xFF
            out.append(c)
        return out

    table = _table(0x1D)

    def opendbc(data: bytes) -> int:
        crc = 0xFD ^ 0xDF
        for b in data:
            crc = table[crc ^ b]
        return crc ^ 0xDF

    import random

    rng = random.Random(3)
    for _ in range(500):
        data = bytes(rng.randrange(256) for _ in range(rng.randint(1, 8)))
        assert hyundai_crc8(0, data, len(data)) == opendbc(data)


def test_honda_checksum_is_four_bits():
    for addr in (0x1D2, 0x17C, 0x191):
        value = honda_checksum(addr, b"\x11\x22\x33\x44\x55\x66\x77\x80", 8)
        assert 0 <= value <= 0xF


def test_registry_widths_are_declared_correctly():
    for algo in CHECKSUM_ALGOS:
        value = algo.fn(0x123, b"\x01\x02\x03\x04\x05\x06\x07", 8)
        assert 0 <= value < (1 << algo.width), algo.name


def test_registry_names_are_unique():
    names = [a.name for a in CHECKSUM_ALGOS]
    assert len(names) == len(set(names))
