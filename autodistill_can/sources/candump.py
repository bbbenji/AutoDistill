"""Reader and writer for can-utils ``candump`` logs.

``candump`` emits several shapes depending on its flags, and community CAN
captures use all of them, so the parser accepts each rather than demanding one:

    (1699887000.123456) can0 1A6#1122334455667788      # candump -l  (log mode)
    (0.123456) can0 1A6#1122334455667788               # relative timestamps
    can0  1A6   [8]  11 22 33 44 55 66 77 88           # candump default
    can0  1A6   [8]  11 22 33 44 55 66 77 88   '....'  # candump -a (ASCII)
     can0  18DAF110  [8]  02 10 03 00 00 00 00 00      # 29-bit extended id

Extended (29-bit) identifiers are reported through :attr:`CanFrame.addr`
unchanged — openpilot addresses diagnostic messages the same way.

CAN FD frames use ``##`` with a leading flags nibble; they parse fine and the
payload is kept whole.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import IO, Iterable, Iterator

from ..frame import CanFrame
from .compressed import open_maybe_compressed

__all__ = [
    "SYNTHETIC_PERIOD",
    "parse_candump_line",
    "read_candump",
    "write_candump",
]

#: Spacing given to frames from a log that carries no timestamps at all.
SYNTHETIC_PERIOD = 1e-3

# (1699887000.123456) can0 1A6#1122334455667788
_LOG_RE = re.compile(
    r"^\((?P<t>-?\d+(?:\.\d+)?)\)\s+(?P<iface>\S+)\s+"
    r"(?P<addr>[0-9A-Fa-f]+)(?P<sep>##?)(?P<data>[0-9A-Fa-f]*)"
    r"(?:\s|$)"
)

# can0  1A6   [8]  11 22 33 44 55 66 77 88
_PRETTY_RE = re.compile(
    r"^\s*(?:\((?P<t>-?\d+(?:\.\d+)?)\)\s+)?(?P<iface>\S+)\s+"
    r"(?P<addr>[0-9A-Fa-f]+)\s+\[(?P<dlc>\d+)\]\s*(?P<data>(?:[0-9A-Fa-f]{2}\s*)*)"
)


def _bus_from_iface(iface: str) -> int:
    """Map an interface name to a bus number.

    ``can0``/``vcan2``/``slcan1`` carry the number in the name, which for a
    panda-fed setup lines up with the panda bus index. Anything unnumbered is
    bus 0.
    """
    m = re.search(r"(\d+)$", iface)
    return int(m.group(1)) if m else 0


def _parse(line: str) -> tuple[CanFrame, bool] | None:
    """Parse one line into ``(frame, line_carried_a_timestamp)``.

    The timestamp flag reflects whether the *format* supplied a time, not
    whether that time was nonzero — a relative-timestamp log legitimately
    starts at ``(0.000000)``, and confusing the two would misclassify it.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    m = _LOG_RE.match(line)
    if m:
        data_hex = m.group("data")
        if m.group("sep") == "##":
            # CAN FD: first hex digit after ## is the frame's flags field.
            data_hex = data_hex[1:] if data_hex else ""
        if len(data_hex) % 2:
            return None
        frame = CanFrame(
            t=float(m.group("t")),
            addr=int(m.group("addr"), 16),
            data=bytes.fromhex(data_hex),
            bus=_bus_from_iface(m.group("iface")),
        )
        return frame, True

    m = _PRETTY_RE.match(line)
    if m:
        data_hex = m.group("data").replace(" ", "")
        if len(data_hex) % 2:
            return None
        # Trust the bracketed DLC over a trailing ASCII column that may have
        # been swept into the data group by `candump -a`.
        data = bytes.fromhex(data_hex)[: int(m.group("dlc"))]
        has_t = m.group("t") is not None
        frame = CanFrame(
            t=float(m.group("t")) if has_t else 0.0,
            addr=int(m.group("addr"), 16),
            data=data,
            bus=_bus_from_iface(m.group("iface")),
        )
        return frame, has_t

    return None


def parse_candump_line(line: str) -> CanFrame | None:
    """Parse one line of a candump log, or None if it isn't a frame."""
    try:
        parsed = _parse(line)
    except (TypeError, ValueError):
        return None
    return parsed[0] if parsed else None


def read_candump(path: Path | str, *, strict: bool = False) -> Iterator[CanFrame]:
    """Stream frames from a candump log.

    Unparseable lines are skipped unless ``strict``, because real captures are
    routinely topped with a comment header or truncated mid-line by an
    interrupted recording.

    Logs recorded without ``-l`` carry no timestamps; those frames are spaced
    :data:`SYNTHETIC_PERIOD` apart so ordering and per-message sample counts
    still work. Rates and jitter from such a log are meaningless, and
    :func:`autodistill_can.analysis.timing_is_synthetic` lets the report say so.
    """
    path = Path(path)
    synthetic_index = 0

    # Wrapped rather than opened directly so a capture stored as .bz2, .gz,
    # .xz or .zst reads the same as a bare one.
    with io.TextIOWrapper(
        open_maybe_compressed(path), errors="replace"
    ) as fh:
        for n_line, line in enumerate(fh, start=1):
            try:
                parsed = _parse(line)
            except (TypeError, ValueError) as exc:
                if strict:
                    raise ValueError(f"{path}:{n_line}: {exc}") from exc
                continue
            if parsed is None:
                if line.strip() and not line.lstrip().startswith("#"):
                    if strict:
                        raise ValueError(f"{path}:{n_line}: cannot parse {line!r}")
                continue
            frame, has_time = parsed
            if has_time:
                yield frame
            else:
                yield CanFrame(
                    t=synthetic_index * SYNTHETIC_PERIOD,
                    addr=frame.addr,
                    data=frame.data,
                    bus=frame.bus,
                )
                synthetic_index += 1


def write_candump(frames: Iterable[CanFrame], out: IO[str]) -> int:
    """Write frames in ``candump -l`` format. Returns the number written."""
    n = 0
    for frame in frames:
        out.write(
            f"({frame.t:.6f}) can{frame.bus} "
            f"{frame.addr:X}#{frame.data.hex().upper()}\n"
        )
        n += 1
    return n
