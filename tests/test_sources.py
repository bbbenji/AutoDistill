"""Capture file parsing.

candump emits several different formats depending on its flags, and community
captures use all of them, so each shape gets a case.
"""

from __future__ import annotations

import io

import pytest

from autodistill_can.sources.candump import (
    SYNTHETIC_PERIOD,
    parse_candump_line,
    read_candump,
    write_candump,
)
from autodistill_can.sources.csv_source import read_csv
from autodistill_can.sources.panda import PandaSource, _unpack_frames

_PANDA_DLC_TO_LEN = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


def _panda_packet(
    addr: int,
    data: bytes,
    *,
    bus: int = 0,
    fd: bool = False,
    returned: bool = False,
    rejected: bool = False,
) -> bytes:
    dlc = _PANDA_DLC_TO_LEN.index(len(data))
    word = (
        (addr << 3)
        | ((addr >= 0x800) << 2)
        | (returned << 1)
        | rejected
    )
    header = bytearray(6)
    header[0] = (dlc << 4) | (bus << 1) | fd
    header[1:5] = word.to_bytes(4, "little")
    checksum = 0
    for byte in header[:5] + data:
        checksum ^= byte
    header[5] = checksum
    return bytes(header) + data


def test_parse_log_format():
    frame = parse_candump_line("(1699887000.123456) can0 1A6#1122334455667788")
    assert frame is not None
    assert frame.t == pytest.approx(1699887000.123456)
    assert frame.addr == 0x1A6
    assert frame.data == bytes.fromhex("1122334455667788")
    assert frame.bus == 0


def test_parse_relative_timestamp_starting_at_zero():
    frame = parse_candump_line("(0.000000) can1 025#0011")
    assert frame is not None
    assert frame.t == 0.0
    assert frame.bus == 1


def test_parse_pretty_format():
    frame = parse_candump_line("can0  1A6   [8]  11 22 33 44 55 66 77 88")
    assert frame is not None
    assert frame.addr == 0x1A6
    assert frame.data == bytes.fromhex("1122334455667788")


def test_parse_pretty_format_with_ascii_column():
    # `candump -a` appends a quoted ASCII rendering; the bracketed DLC is
    # authoritative and must win over anything swept into the data group.
    frame = parse_candump_line("can0  123   [3]  41 42 43   'ABC'")
    assert frame is not None
    assert frame.data == b"ABC"


def test_parse_extended_identifier():
    frame = parse_candump_line("(1.0) can0 18DAF110#0210030000000000")
    assert frame is not None
    assert frame.addr == 0x18DAF110


def test_parse_canfd_strips_flags_nibble():
    frame = parse_candump_line("(1.0) can0 123##1112233445566778899aabbcc")
    assert frame is not None
    assert frame.addr == 0x123
    # The first hex digit after ## is the FD flags field, not payload.
    assert frame.data == bytes.fromhex("112233445566778899aabbcc")


def test_bus_number_comes_from_interface_name():
    assert parse_candump_line("(1.0) can2 100#00").bus == 2
    assert parse_candump_line("(1.0) vcan3 100#00").bus == 3
    assert parse_candump_line("(1.0) slcan 100#00").bus == 0


def test_parse_rejects_junk():
    assert parse_candump_line("") is None
    assert parse_candump_line("# a comment") is None
    assert parse_candump_line("not a frame at all") is None
    # Odd number of hex digits cannot be a payload.
    assert parse_candump_line("(1.0) can0 123#111") is None


def test_read_candump_skips_junk_but_strict_raises(tmp_path):
    path = tmp_path / "drive.log"
    path.write_text(
        "# header comment\n"
        "(1.0) can0 100#0011\n"
        "garbage line\n"
        "(2.0) can0 100#0022\n"
    )
    assert len(list(read_candump(path))) == 2
    with pytest.raises(ValueError):
        list(read_candump(path, strict=True))


def test_read_candump_gives_timestampless_logs_a_synthetic_clock(tmp_path):
    path = tmp_path / "no_time.log"
    path.write_text(
        "can0  100   [2]  00 11\n"
        "can0  100   [2]  00 22\n"
        "can0  100   [2]  00 33\n"
    )
    frames = list(read_candump(path))
    assert len(frames) == 3
    # Distinct, increasing times, so ordering and rates remain computable.
    assert [f.t for f in frames] == [
        0.0, SYNTHETIC_PERIOD, 2 * SYNTHETIC_PERIOD
    ]


def test_write_candump_round_trips():
    from autodistill_can.frame import CanFrame

    frames = [
        CanFrame(t=1.5, addr=0x1A6, data=b"\x11\x22", bus=0),
        CanFrame(t=2.5, addr=0x7E8, data=b"\x03", bus=2),
    ]
    buffer = io.StringIO()
    assert write_candump(frames, buffer) == 2
    reparsed = [parse_candump_line(l) for l in buffer.getvalue().splitlines()]
    assert [(f.t, f.addr, f.data, f.bus) for f in reparsed] == [
        (1.5, 0x1A6, b"\x11\x22", 0),
        (2.5, 0x7E8, b"\x03", 2),
    ]


def test_read_csv_hex_payload(tmp_path):
    path = tmp_path / "log.csv"
    path.write_text("time,id,data\n0.0,1A6,1122334455667788\n0.01,0AA,0000\n")
    frames = list(read_csv(path))
    assert [f.addr for f in frames] == [0x1A6, 0x0AA]
    assert frames[0].data == bytes.fromhex("1122334455667788")


def test_read_csv_per_byte_columns(tmp_path):
    path = tmp_path / "log.csv"
    path.write_text("Time,ID,Length,d1,d2,d3\n0.0,100,3,11,22,33\n")
    frames = list(read_csv(path))
    assert frames[0].data == b"\x11\x22\x33"


def test_read_csv_python_can_header(tmp_path):
    path = tmp_path / "log.csv"
    path.write_text("timestamp,arbitration_id,data\n0.5,0x1a6,aabb\n")
    frames = list(read_csv(path))
    assert frames[0].addr == 0x1A6
    assert frames[0].data == b"\xaa\xbb"


def test_read_csv_channel_column(tmp_path):
    path = tmp_path / "log.csv"
    path.write_text("time,channel,id,data\n0.0,can2,100,00\n")
    assert list(read_csv(path))[0].bus == 2


def test_read_csv_rejects_unrecognisable_header(tmp_path):
    path = tmp_path / "log.csv"
    path.write_text("alpha,beta\n1,2\n")
    with pytest.raises(ValueError):
        list(read_csv(path))


def test_panda_unpack_current_variable_length_protocol():
    classic = _panda_packet(0x123, b"\x01\x02\x03", bus=1)
    extended_fd = _panda_packet(
        0x18DAF110, bytes(range(12)), bus=2, fd=True
    )

    frames, overflow = _unpack_frames(classic + extended_fd, 4.25)

    assert overflow == b""
    assert [(f.t, f.addr, f.data, f.bus) for f in frames] == [
        (4.25, 0x123, b"\x01\x02\x03", 1),
        (4.25, 0x18DAF110, bytes(range(12)), 2),
    ]


def test_panda_unpack_preserves_packet_split_between_usb_reads():
    packet = _panda_packet(0x456, bytes(range(16)), bus=0, fd=True)

    frames, overflow = _unpack_frames(packet[:9], 1.0)
    assert frames == []
    assert overflow == packet[:9]

    frames, overflow = _unpack_frames(overflow + packet[9:], 1.1)
    assert len(frames) == 1
    assert frames[0].data == bytes(range(16))
    assert frames[0].t == 1.1
    assert overflow == b""


def test_panda_unpack_rejects_corrupt_packets():
    packet = bytearray(_panda_packet(0x123, b"\x00\x01", bus=0))
    packet[-1] ^= 0xFF

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        _unpack_frames(bytes(packet), 0.0)


def test_panda_unpack_ignores_transmit_status_packets():
    returned = _panda_packet(0x123, b"\x01", returned=True)
    rejected = _panda_packet(0x456, b"\x02", rejected=True)

    frames, overflow = _unpack_frames(returned + rejected, 0.0)

    assert frames == []
    assert overflow == b""


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"bus": 8}, "bus"),
        ({"duration": 0}, "duration"),
        ({"duration": float("nan")}, "duration"),
        ({"limit": 0}, "limit"),
        ({"silent": False}, "capture-only"),
    ],
)
def test_panda_source_rejects_unsafe_or_invalid_configuration(kwargs, message):
    with pytest.raises(ValueError, match=message):
        PandaSource(**kwargs)
