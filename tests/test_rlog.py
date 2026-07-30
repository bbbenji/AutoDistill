"""Reading openpilot's own segments.

A comma device already logs every CAN frame it sees, so anyone who has driven
an unsupported car with one in dashcam mode has the best capture this tool can
be given. These build a real segment with openpilot's real schema and read it
back, and skip when the schema or the parser is not on this machine -- the
whole point of the design is that neither is required to use anything else.
"""

from __future__ import annotations

import bz2
from pathlib import Path

import pytest

from autodistill_can.sources import read_log
from autodistill_can.sources.rlog import find_schema, read_rlog

capnp = pytest.importorskip("capnp", reason="rlog support needs pycapnp")


@pytest.fixture(scope="module")
def schema():
    try:
        return find_schema()
    except FileNotFoundError:
        pytest.skip("openpilot's log.capnp is not on this machine")


@pytest.fixture(scope="module")
def segment(tmp_path_factory, schema):
    """A real rlog, written through openpilot's own schema."""
    capnp.remove_import_hook()
    log = capnp.load(
        str(schema), imports=[str(schema.parent), str(schema.parent.parent)]
    )
    path = tmp_path_factory.mktemp("rlog") / "rlog"
    with path.open("wb") as fh:
        for i in range(200):
            event = log.Event.new_message()
            event.logMonoTime = (i + 1) * 10_000_000  # 10ms, in nanoseconds
            frames = event.init("can", 2)
            for j, (address, bus) in enumerate(((0x1A6, 0), (0x3B7, 2))):
                frames[j].address = address
                frames[j].src = bus
                frames[j].dat = bytes([i % 256, j, 0, 0, 0, 0, 0, 0])
            event.write(fh)

            # What openpilot transmitted, which the car did not say.
            sent = log.Event.new_message()
            sent.logMonoTime = (i + 1) * 10_000_000
            out = sent.init("can", 1)
            out[0].address = 0x2B0
            out[0].src = 0x80
            out[0].dat = b"\xff" * 8
            sent.write(fh)
    return path


def test_a_segment_yields_the_cars_frames(segment):
    frames = list(read_rlog(segment))
    assert len(frames) == 400
    assert {(f.bus, f.addr) for f in frames} == {(0, 0x1A6), (2, 0x3B7)}
    # logMonoTime is nanoseconds; the timeline has to come out in seconds.
    assert frames[0].t == pytest.approx(0.01)
    assert frames[-1].t == pytest.approx(2.0)


def test_frames_openpilot_sent_are_not_treated_as_the_car(segment):
    """`sendcan` is what openpilot asked for, not what the car said.

    Logged with the high bit of `src` set. Reading them as ordinary traffic
    would put openpilot's own output into the evidence for the car.
    """
    assert not [f for f in read_rlog(segment) if f.addr == 0x2B0]
    assert [f for f in read_rlog(segment, include_sent=True) if f.addr == 0x2B0]


def test_a_segment_is_found_by_name_without_an_extension(segment):
    """openpilot names them `rlog`, with no suffix at all."""
    assert len(list(read_log(segment))) == 400


def test_a_compressed_segment_reads_the_same(segment, tmp_path):
    """Devices write rlog.zst now and rlog.bz2 before that."""
    packed = tmp_path / "rlog.bz2"
    packed.write_bytes(bz2.compress(segment.read_bytes()))
    assert [(f.t, f.bus, f.addr) for f in read_log(packed)] == [
        (f.t, f.bus, f.addr) for f in read_log(segment)
    ]


def test_something_that_is_not_a_segment_says_so(tmp_path, schema):
    path = tmp_path / "rlog"
    path.write_bytes(b"\x01\x02\x03\x04" * 64)
    with pytest.raises(ValueError, match="not a readable openpilot rlog"):
        list(read_rlog(path))


def test_a_missing_schema_explains_itself(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTODISTILL_LOG_CAPNP", raising=False)
    monkeypatch.setattr(
        "autodistill_can.sources.rlog.find_schema",
        lambda explicit=None: (_ for _ in ()).throw(FileNotFoundError("nope")),
    )
    with pytest.raises(FileNotFoundError):
        list(read_rlog(Path("whatever")))
