"""Shared fixtures.

The synthetic capture is expensive to generate and every analysis test wants
the same one, so it is built once per session.
"""

from __future__ import annotations

import pytest

from autodistill_can import synth
from autodistill_can.analysis.message import analyse_message
from autodistill_can.frame import CanLog

#: Long enough for the drive to pass through every phase (standing, pulling
#: away, cruising, braking, crawling, stopping) and for a 10 Hz message to
#: gather enough frames to say anything about.
CAPTURE_SECONDS = 45.0


@pytest.fixture(scope="session")
def frames():
    return synth.generate(CAPTURE_SECONDS)


@pytest.fixture(scope="session")
def log(frames):
    return CanLog.from_frames(frames)


@pytest.fixture(scope="session")
def truth():
    """Ground truth keyed by ``(bus, addr)``."""
    return {
        (m["bus"], m["addr"]): m for m in synth.ground_truth()["messages"]
    }


@pytest.fixture(scope="session")
def analyses(log):
    """Full analysis of every message with enough frames to be worth analysing."""
    return {
        stream.key: analyse_message(stream)
        for stream in log.sorted_streams()
        if len(stream) >= 40
    }


@pytest.fixture(scope="session")
def reference():
    from autodistill_can.analysis.correlate import ReferenceSeries

    rows = synth.reference_series(CAPTURE_SECONDS)
    return ReferenceSeries(
        times=[r["time"] for r in rows],
        channels={
            "speed_kph": [r["speed_kph"] for r in rows],
            "steer_angle_deg": [r["steer_angle_deg"] for r in rows],
        },
        units={"speed_kph": "km/h", "steer_angle_deg": "deg"},
    )


def dbc_start(start: int, big_endian: bool) -> int:
    """Put either byte order into the one coordinate system they share.

    A recovered signal stores Motorola fields as MSB-first payload indices and
    Intel fields as DBC start bits, so comparing raw ``start`` values across
    byte orders is meaningless. The DBC start bit is what both conventions
    ultimately mean, and what a ported car would be written against.
    """
    if not big_endian:
        return start
    byte, bit = divmod(start, 8)
    return byte * 8 + (7 - bit)


def truth_dbc_start(signal: dict) -> int:
    """The DBC start bit of a ground-truth signal from the synthetic car."""
    return dbc_start(signal["start"], signal.get("big_endian", True))


def truth_signal(truth_message: dict, kind: str) -> dict | None:
    """The ground-truth signal of a given kind, if the message has one."""
    return next((s for s in truth_message["signals"] if s["kind"] == kind), None)
