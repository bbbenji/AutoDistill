"""Active probe protocol mechanics, tested without touching CAN hardware."""

from __future__ import annotations

import struct

import pytest

from autodistill_can.analysis.uds import decode_uds, reassemble_isotp
from autodistill_can.probe import _collect, probe_socketcan


class _FakeSocket:
    def __init__(self, received):
        self.received = list(received)
        self.sent: list[bytes] = []
        self.timeouts: list[float] = []

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv(self, _size):
        return self.received.pop(0)

    def send(self, payload):
        self.sent.append(payload)


def _frame(addr: int, data: bytes) -> bytes:
    return struct.pack("=IB3x8s", addr, len(data), data.ljust(8, b"\x00"))


def test_probe_sends_flow_control_and_collects_a_multiframe_response():
    payload = b"\x62\xF1\x88ABCDEFGHIJK"
    first = bytes([0x10, len(payload)]) + payload[:6]
    consecutive_1 = b"\x21" + payload[6:13]
    consecutive_2 = b"\x22" + payload[13:]
    sock = _FakeSocket([
        _frame(0x7E8, first),
        _frame(0x7E8, consecutive_1),
        _frame(0x7E8, consecutive_2),
    ])

    frames = _collect(
        sock, timeout=0.5, bus=0, watch=0x7E8, flow_control_addr=0x7E0
    )

    assert len(frames) == 3
    assert len(sock.sent) == 1
    fc_addr, fc_length, fc_data = struct.unpack("=IB3x8s", sock.sent[0])
    assert fc_addr == 0x7E0
    assert fc_length == 8
    assert fc_data[:3] == b"\x30\x00\x00"

    responses = decode_uds(reassemble_isotp(frames))
    assert len(responses) == 1
    assert responses[0].payload == b"ABCDEFGHIJK"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": float("nan")},
        {"bus": 256},
        {"dids": (-1,)},
        {"addresses": (0x20000000,)},
    ],
)
def test_probe_validates_configuration_before_touching_hardware(kwargs):
    with pytest.raises(ValueError):
        probe_socketcan(**kwargs)
