"""Capture sources.

Every source is an iterable of :class:`~autodistill_can.frame.CanFrame`, so live
capture and log replay are interchangeable everywhere downstream.

Sources are named by URI-ish strings so the CLI can take one argument:

    drive.log                candump log file (extension-sniffed)
    drive.log.bz2            any of the above, compressed (gz, bz2, xz, zst)
    candump:drive.log        candump log file, explicitly
    csv:drive.csv            generic CSV
    rlog:rlog.zst            openpilot segment (needs pycapnp)
    socketcan:can0           live SocketCAN interface
    panda:                   live comma.ai panda over USB (all buses)
    panda:1                  live panda, bus 1 only
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ..frame import CanFrame

__all__ = ["open_source", "read_log"]


def open_source(uri: str, *, strict: bool = False, **kwargs) -> Iterator[CanFrame]:
    """Resolve a source URI to a frame iterator."""
    # A scheme only counts when a colon actually separates it. Without this,
    # any file whose name happens to match one is read as an empty URI -- and
    # openpilot names its segments exactly "rlog", so that is not a hypothetical
    # collision but the commonest filename this reads.
    scheme, separator, rest = uri.partition(":")
    if not separator:
        return read_log(Path(uri), strict=strict, **kwargs)

    if scheme == "socketcan":
        from .socketcan import SocketCanSource

        return iter(SocketCanSource(rest or "can0", **kwargs))
    if scheme == "panda":
        from .panda import PandaSource

        bus = int(rest) if rest else None
        return iter(PandaSource(bus=bus, **kwargs))
    if scheme == "candump":
        from .candump import read_candump

        return read_candump(Path(rest), strict=strict)
    if scheme == "csv":
        from .csv_source import read_csv

        return read_csv(Path(rest), strict=strict)
    if scheme == "rlog":
        from .rlog import read_rlog

        return read_rlog(Path(rest), strict=strict)

    # A colon that introduced no scheme we know: still a path. Windows drive
    # letters and filenames with colons in them both land here.
    return read_log(Path(uri), strict=strict, **kwargs)


def read_log(path: Path, **kwargs) -> Iterator[CanFrame]:
    """Read a capture file, guessing the format from its name and first line."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no such capture: {path}")

    # The compression suffix is not the format: drive.log.bz2 is a candump
    # log and drive.csv.gz is a CSV, so sniff the name underneath it.
    from .compressed import uncompressed_name

    if uncompressed_name(path).suffix.lower() == ".csv":
        from .csv_source import read_csv

        return read_csv(path, **kwargs)

    # openpilot's own segments: rlog, rlog.zst, rlog.bz2, and the qlog
    # variants, which carry a decimated subset but the same event stream.
    # Checked against the contents as well as the name, because "rlog" is a
    # name anything can be given and Cap'n Proto is never text.
    from .compressed import looks_textual

    stem = uncompressed_name(path).name.lower()
    named_rlog = (
        stem.endswith(".rlog") or stem in {"rlog", "qlog"} or stem.startswith("rlog.")
    )
    if named_rlog and not looks_textual(path):
        from .rlog import read_rlog

        return read_rlog(path, **kwargs)

    from .candump import read_candump

    return read_candump(path, **kwargs)
