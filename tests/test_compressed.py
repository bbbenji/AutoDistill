"""Captures that arrive compressed, and openpilot's own segments.

A fifteen-minute drive is hundreds of megabytes, so captures are routinely
stored compressed and openpilot writes its logs that way by default.
Decompressing by hand first is a step that exists only because the tool would
not do it.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import subprocess

import pytest

from autodistill_can.sources import open_source, read_log
from autodistill_can.sources.compressed import is_compressed, uncompressed_name


@pytest.fixture(scope="module")
def drive(tmp_path_factory):
    path = tmp_path_factory.mktemp("compressed") / "drive.log"
    subprocess.run(
        ["autodistill-can", "synth", "-d", "10", "-o", str(path), "-q"],
        check=True, capture_output=True,
    )
    return path


def _frames(path):
    return [(f.t, f.bus, f.addr, f.data) for f in read_log(path)]


@pytest.mark.parametrize("suffix,compress", [
    (".gz", gzip.compress),
    (".bz2", bz2.compress),
    (".xz", lzma.compress),
])
def test_a_compressed_capture_reads_identically(drive, tmp_path, suffix, compress):
    packed = tmp_path / f"drive.log{suffix}"
    packed.write_bytes(compress(drive.read_bytes()))
    assert _frames(packed) == _frames(drive)


def test_compression_is_detected_by_content_not_only_by_name(drive, tmp_path):
    """openpilot's segments are named `rlog`, with no suffix at all.

    A capture renamed without its suffix is still compressed, and reporting
    "cannot parse" about it would send someone looking for a fault in their
    recording.
    """
    disguised = tmp_path / "capture_with_no_suffix"
    disguised.write_bytes(bz2.compress(drive.read_bytes()))
    assert is_compressed(disguised)
    assert _frames(disguised) == _frames(drive)


def test_the_inner_extension_decides_the_format(tmp_path):
    """`drive.csv.gz` is a CSV; the compression suffix is not the format."""
    assert uncompressed_name("drive.csv.gz").name == "drive.csv"
    assert uncompressed_name("drive.log.bz2").name == "drive.log"
    assert uncompressed_name("rlog.zst").name == "rlog"
    # A name that carries no compression suffix keeps the name it has.
    assert uncompressed_name("rlog").name == "rlog"


def test_a_file_named_like_a_scheme_is_still_a_file(drive, tmp_path):
    """openpilot names every segment exactly `rlog`.

    `open_source` splits a URI on its first colon, so without requiring the
    colon a file called `rlog` is read as the `rlog:` scheme with an empty
    path -- and that is the commonest filename this ever reads.
    """
    for name in ("rlog", "candump", "csv", "panda"):
        path = tmp_path / name
        path.write_bytes(drive.read_bytes())
        frames = list(open_source(str(path)))
        assert frames, f"{name} was not read as a path"


def test_an_uncompressed_capture_is_untouched(drive):
    assert not is_compressed(drive)
    assert _frames(drive)
