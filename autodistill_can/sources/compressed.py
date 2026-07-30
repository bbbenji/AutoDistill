"""Reading captures that arrived compressed.

Captures are large -- a fifteen-minute drive is hundreds of megabytes -- so
they are routinely stored compressed, and openpilot writes its own logs that
way by default. Decompressing by hand first is a step that only exists because
the tool would not do it, and on a comma device it needs disk that is not
there.

Handled transparently wherever a capture path is accepted, including the
inner extension: ``drive.log.bz2`` is a candump log and ``drive.csv.gz`` is a
CSV, so the format sniffing sees the name it would have seen uncompressed.

gzip, bzip2 and xz are standard library on every Python this supports. Zstandard
is standard library from 3.14 (``compression.zstd``); before that it needs the
``zstandard`` package, which is why the error for a missing one says so rather
than reporting a corrupt file.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
from pathlib import Path
from typing import IO, Callable

__all__ = [
    "is_compressed",
    "looks_textual",
    "open_maybe_compressed",
    "uncompressed_name",
]

#: Magic numbers, checked in preference to the extension. A capture renamed
#: from rlog.bz2 to rlog is still bzip2, and saying "cannot parse" about it
#: would send someone looking for a fault in their recording.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x1f\x8b", ".gz"),
    (b"BZh", ".bz2"),
    (b"\xfd7zXZ\x00", ".xz"),
    (b"\x28\xb5\x2f\xfd", ".zst"),
)

_SUFFIXES = {".gz", ".bz2", ".xz", ".lzma", ".zst", ".zstd"}


def _open_zstd(path: Path) -> IO[bytes]:
    try:
        from compression.zstd import ZstdFile  # Python 3.14+
    except ImportError:
        try:
            from zstandard import open as ZstdFile  # type: ignore[no-redef]
        except ImportError as exc:
            raise ImportError(
                f"{path.name} is zstandard-compressed, which needs Python 3.14 "
                "or the 'zstandard' package: pip install zstandard"
            ) from exc
    return ZstdFile(path, "rb")


_OPENERS: dict[str, Callable[[Path], IO[bytes]]] = {
    ".gz": lambda p: gzip.open(p, "rb"),
    ".bz2": lambda p: bz2.open(p, "rb"),
    ".xz": lambda p: lzma.open(p, "rb"),
    ".lzma": lambda p: lzma.open(p, "rb"),
    ".zst": _open_zstd,
    ".zstd": _open_zstd,
}


def _sniff(path: Path) -> str | None:
    """The compression this file actually is, or None."""
    try:
        with path.open("rb") as fh:
            head = fh.read(8)
    except OSError:
        return None
    for magic, suffix in _MAGIC:
        if head.startswith(magic):
            return suffix
    return None


def is_compressed(path: Path | str) -> bool:
    return _sniff(Path(path)) is not None


def uncompressed_name(path: Path | str) -> Path:
    """The path with its compression suffix removed, for format sniffing.

    ``drive.log.bz2`` is a candump log; the inner suffix is what decides how to
    parse it. A file whose name carries no compression suffix keeps its name,
    even when the contents turn out to be compressed.
    """
    path = Path(path)
    if path.suffix.lower() in _SUFFIXES:
        return path.with_suffix("")
    return path


def open_maybe_compressed(path: Path | str) -> IO[bytes]:
    """A binary handle on the capture, decompressing if it needs it."""
    path = Path(path)
    suffix = _sniff(path)
    if suffix is None:
        return path.open("rb")
    return _OPENERS[suffix](path)


def looks_textual(path: Path | str) -> bool:
    """Whether the decompressed contents read as text rather than binary.

    Format sniffing goes by name, and the name is not always right: openpilot
    calls its segments `rlog`, so anything a person renames to `rlog` would
    otherwise be handed to a Cap'n Proto parser regardless of what is in it.
    Candump logs and CSVs are text; rlogs are not, and the distinction is
    clean enough at the first block to be worth checking.
    """
    try:
        with open_maybe_compressed(path) as handle:
            head = handle.read(512)
    except (OSError, ImportError):
        return False
    if not head:
        return False
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    # Control characters other than the usual whitespace mean binary.
    printable = sum(1 for b in head if b >= 0x20 or b in (0x09, 0x0A, 0x0D))
    return printable / len(head) > 0.95
