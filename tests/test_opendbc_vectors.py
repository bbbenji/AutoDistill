"""Checksum implementations pinned to real frames from real cars.

The vectors below are captured messages that openpilot ships in its own test
suite, each carrying the checksum its ECU actually produced. Checking against
them is the strongest evidence available short of a car: it is not "our encoder
agrees with our decoder" but "our arithmetic agrees with a Volkswagen".

This file has already caught two defects that every other test missed:

* Honda's checksum takes an extra ``+3`` on 29-bit identifiers, so ours was
  right on every standard-id message and wrong on every extended one.
* opendbc's ``mk_crc8_fun(init_crc=...)`` does not mean what it looks like —
  the register starts at ``init_crc ^ xor_out``. Copying Hyundai's stated 0xFD
  across gave a checksum that disagreed with the car on every frame.

Sources: ``opendbc/can/tests/test_checksums.py``, ``opendbc/car/hondacan.py``,
``opendbc/car/hyundai/hyundaican.py``, ``opendbc/car/volkswagen/mqbcan.py``,
``opendbc/car/chrysler/chryslercan.py``.
"""

from __future__ import annotations

import random

import pytest

from autodistill_can.algos import (
    crc8,
    honda_checksum,
    hyundai_crc8,
    volkswagen_mqb_checksum,
)


def _crc8_table(poly: int) -> list[int]:
    """opendbc's table generator, transcribed from ``opendbc/car/crc.py``."""
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        table.append(crc)
    return table


CRC8J1850 = _crc8_table(0x1D)
CRC8H2F = _crc8_table(0x2F)


# --------------------------------------------------------------------------
# FCA Giorgio -- the Alfa Romeo Giulia platform
# --------------------------------------------------------------------------

#: Real EPS and ABS frames; the final byte of each is the checksum the car sent.
FCA_GIORGIO_FRAMES = [
    (0xDE, b"\x17\x51\x97\xcc\x00\xdf"),
    (0xDE, b"\x17\x51\x97\xc9\x01\xa3"),
    (0xDE, b"\x17\x51\x97\xcc\x02\xe5"),
    (0x106, b"\x7c\x43\x57\x60\x00\x00\xa1"),
    (0x106, b"\x7c\x63\x58\xe0\x00\x01\xd5"),
    (0x106, b"\x7c\x63\x58\xe0\x00\x02\xf2"),
    (0x122, b"\x7b\x30\x00\xf8"),
    (0x122, b"\x7b\x10\x01\x90"),
    (0xFE, b"\x7e\x38\x00\x7d\x10\x31\x80\x32"),
    (0xFE, b"\x7e\x38\x00\x7d\x10\x31\x81\x2f"),
    (0xFE, b"\x7e\x38\x00\x7d\x20\x31\x82\x20"),
]


@pytest.mark.parametrize("addr,frame", FCA_GIORGIO_FRAMES)
def test_our_j1850_matches_real_alfa_giulia_frames(addr, frame):
    """The formula recovered from a real Giulia capture must match the car.

    AutoDistill reports these as CRC-8/SAE-J1850 with init and xorout both 0xFF;
    opendbc writes the same platform as init 0x00 with a per-address final XOR.
    Those are two spellings of one function — a CRC is affine, so a change of
    init is absorbed by a constant that depends only on message length — and
    both reproduce the byte the car actually sent.
    """
    assert crc8(frame[:-1], poly=0x1D, init=0xFF, xorout=0xFF) == frame[-1]


# --------------------------------------------------------------------------
# Volkswagen MQB -- CRC8H2F plus a counter-selected magic byte
# --------------------------------------------------------------------------

#: Real LWI_01 (0x86) frames, one per counter value. The magic table for this
#: address is a constant 0x86, from opendbc's VOLKSWAGEN_MQB_MEB_CONSTANTS.
VW_LWI_01 = [
    b"\x6b\x00\xbd\x00\x00\x00\x00\x00", b"\xee\x01\x0a\x00\x00\x00\x00\x00",
    b"\xd8\x02\xa9\x00\x00\x00\x00\x00", b"\x03\x03\xbe\xa2\x12\x00\x00\x00",
    b"\x7b\x04\x31\x20\x03\x00\x00\x00", b"\x8b\x05\xe2\x85\x09\x00\x00\x00",
    b"\x63\x06\x13\x21\x00\x00\x00\x00", b"\x66\x07\x05\x00\x00\x00\x00\x00",
    b"\x49\x08\x0d\x00\x00\x00\x00\x00", b"\x5f\x09\x7e\x60\x01\x00\x00\x00",
    b"\xaf\x0a\x72\x20\x00\x00\x00\x00", b"\x59\x0b\x1b\x00\x00\x00\x00\x00",
    b"\xa8\x0c\x06\x00\x00\x00\x00\x00", b"\xbc\x0d\x72\x20\x00\x00\x00\x00",
    b"\xf9\x0e\x0f\x00\x00\x00\x00\x00", b"\x60\x0f\x62\xc0\x00\x00\x00\x00",
]

#: Getriebe_11 (0xAD), whose magic table genuinely varies per counter value —
#: the case that no fixed formula reproduces and only the per-counter GF(2)
#: solve recovers.
VW_GETRIEBE_11 = [
    b"\xf8\xe0\xbf\xff\x5f\x20\x20\x20", b"\xb0\xe1\xbf\xff\xc6\x98\x21\x80",
    b"\xd2\xe2\xbf\xff\x5f\x20\x20\x20", b"\x00\xe3\xbf\xff\xaa\x20\x20\x10",
    b"\xf1\xe4\xbf\xff\x5f\x20\x20\x20", b"\xc4\xe5\xbf\xff\x5f\x20\x20\x20",
    b"\xda\xe6\xbf\xff\x5f\x20\x20\x20", b"\x85\xe7\xbf\xff\x5f\x20\x20\x20",
    b"\x12\xe8\xbf\xff\x5f\x20\x20\x20", b"\x45\xe9\xbf\xff\xaa\x20\x20\x10",
    b"\x03\xea\xbf\xff\xcc\x20\x20\x10", b"\xfc\xeb\xbf\xff\x5f\x20\x21\x20",
    b"\xfe\xec\xbf\xff\xad\x20\x20\x10", b"\xbd\xed\xbf\xff\xaa\x20\x20\x10",
    b"\x67\xee\xbf\xff\xaa\x20\x20\x10", b"\x36\xef\xbf\xff\xaa\x20\x20\x10",
]
GETRIEBE_11_MAGIC = bytes([
    0x3F, 0x69, 0x39, 0xDC, 0x94, 0xF9, 0x14, 0x64,
    0xD8, 0x6A, 0x34, 0xCE, 0xA2, 0x55, 0xB5, 0x2C,
])


def test_volkswagen_mqb_checksum_matches_real_frames():
    for frame in VW_LWI_01:
        counter = frame[1] & 0x0F
        assert volkswagen_mqb_checksum(
            0x86, frame, len(frame), counter, bytes([0x86] * 16)
        ) == frame[0]


def test_volkswagen_mqb_counter_keyed_magic_table_matches_real_frames():
    """The per-counter magic table is the whole difficulty of MQB.

    Sixteen frames, one per counter value, each mixing in a different constant.
    A fixed CRC reproduces none of them.
    """
    for frame in VW_GETRIEBE_11:
        counter = frame[1] & 0x0F
        assert volkswagen_mqb_checksum(
            0xAD, frame, len(frame), counter, GETRIEBE_11_MAGIC
        ) == frame[0]


def test_a_fixed_crc_cannot_reproduce_mqb():
    # Establishes that the magic table is doing real work, so the test above is
    # not passing for some incidental reason.
    plain = sum(
        crc8(frame[1:], poly=0x2F, init=0xFF, xorout=0xFF) == frame[0]
        for frame in VW_GETRIEBE_11
    )
    assert plain <= 1, "a plain CRC8H2F should not explain these frames"


# --------------------------------------------------------------------------
# Honda and Hyundai, pinned to opendbc's shipped implementations
# --------------------------------------------------------------------------


def _honda_opendbc(address: int, d: bytes) -> int:
    s = 0
    extended = address > 0x7FF
    addr = address
    while addr:
        s += addr & 0xF
        addr >>= 4
    for i in range(len(d)):
        x = d[i]
        if i == len(d) - 1:
            x >>= 4
        s += (x & 0xF) + (x >> 4)
    s = 8 - s
    if extended:
        s += 3
    return s & 0xF


@pytest.mark.parametrize("addr", [0x1D2, 0x17C, 0x18DA00F1, 0x1FFFFFFF])
def test_honda_checksum_matches_opendbc_including_extended_ids(addr):
    rng = random.Random(11)
    for _ in range(300):
        data = bytes(rng.randrange(256) for _ in range(8))
        assert honda_checksum(addr, data, 8) == _honda_opendbc(addr, data)


def test_hyundai_crc8_matches_opendbc():
    def opendbc(data: bytes) -> int:
        crc = 0xFD ^ 0xDF  # opendbc's helper starts here, not at 0xFD
        for b in data:
            crc = CRC8J1850[crc ^ b]
        return crc ^ 0xDF

    rng = random.Random(13)
    for _ in range(500):
        data = bytes(rng.randrange(256) for _ in range(rng.randint(1, 8)))
        assert hyundai_crc8(0, data, len(data)) == opendbc(data)
