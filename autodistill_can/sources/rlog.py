"""Reading openpilot's own logs.

An openpilot device already records every CAN frame it sees, so anyone who has
driven an unsupported car with a comma device in dashcam mode is sitting on the
best capture this tool can be given -- several buses at once, correctly
timestamped, with no extra hardware beyond what porting needs anyway.

Segments are written as ``rlog.zst`` (older devices: ``rlog.bz2``), which is a
stream of Cap'n Proto ``Event`` structs. Decompression is handled upstream of
this module, so only the Cap'n Proto part is here.

Unlike everything else in the package this needs a dependency, because Cap'n
Proto is a schema-driven binary format and the schema belongs to openpilot. The
schema is deliberately *not* vendored: it changes with openpilot, and a stale
copy that still parses would be worse than no copy at all. It is located, in
order, from an explicit argument, ``AUTODISTILL_LOG_CAPNP``, or an installed
openpilot.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from ..frame import CanFrame
from .compressed import open_maybe_compressed

__all__ = ["find_schema", "read_rlog"]

#: openpilot logs frames it transmitted with the high bit of `src` set, so a
#: capture made while it was controlling a car carries both the car's traffic
#: and openpilot's own. Only the car's is evidence about the car.
_SENT_FLAG = 0x80


def find_schema(explicit: str | Path | None = None) -> Path:
    """Locate openpilot's ``log.capnp``.

    Searched in the order someone would expect to win: what they passed, what
    they exported, then whatever openpilot is installed.
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("AUTODISTILL_LOG_CAPNP")
    if env:
        candidates.append(Path(env))

    try:  # an installed openpilot carries the schema beside its Python
        import openpilot  # type: ignore[import-not-found]

        root = Path(openpilot.__file__).resolve().parent
        candidates.append(root / "cereal" / "log.capnp")
        candidates.append(root.parent / "cereal" / "log.capnp")
    except Exception:  # noqa: BLE001 - absence is normal, any failure is "no"
        pass

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise FileNotFoundError(
        "openpilot's log.capnp is needed to read an rlog and was not found. "
        "Point at it with AUTODISTILL_LOG_CAPNP=/path/to/openpilot/cereal/"
        "log.capnp, or install openpilot. The schema is not shipped here "
        "because it changes with openpilot and a stale copy would parse the "
        "wrong fields."
    )


def _load_schema(schema: str | Path | None):
    try:
        import capnp  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "reading openpilot rlogs needs Cap'n Proto: pip install "
            "'autodistill-can[rlog]' (or pip install pycapnp). Every other "
            "capture format here is standard library."
        ) from exc

    path = find_schema(schema)
    capnp.remove_import_hook()
    # log.capnp imports car.capnp from opendbc and its siblings by relative
    # path, so its own directory and parent both have to be importable.
    imports = [str(path.parent), str(path.parent.parent)]
    return capnp.load(str(path), imports=imports)


def read_rlog(
    path: Path | str,
    *,
    schema: str | Path | None = None,
    include_sent: bool = False,
    strict: bool = False,
) -> Iterator[CanFrame]:
    """Stream CAN frames out of an openpilot segment.

    Only ``can`` events are read. openpilot also logs ``sendcan`` -- what it
    asked the car to do -- which is not the car speaking and would be recorded
    as though it were.

    ``logMonoTime`` is nanoseconds since boot, so the timeline is consistent
    within a segment but shares no origin with a wall clock. That is the same
    contract a candump log has, and the same one correlation needs.
    """
    log = _load_schema(schema)
    path = Path(path)

    with open_maybe_compressed(path) as handle:
        events = log.Event.read_multiple_bytes(handle.read())

        while True:
            # Cap'n Proto reports a malformed stream when the iterator is
            # advanced rather than when it is created, so the framing has to
            # be guarded here and not around the call above. A broken stream
            # is a different thing from one odd event, and only the second is
            # worth skipping past.
            try:
                event = next(events)
            except StopIteration:
                break
            except Exception as exc:  # noqa: BLE001 - capnp raises its own types
                raise ValueError(
                    f"{path}: not a readable openpilot rlog ({exc})"
                ) from exc

            try:
                if event.which() != "can":
                    continue
                seconds = event.logMonoTime / 1e9
                for frame in event.can:
                    src = int(frame.src)
                    if src & _SENT_FLAG and not include_sent:
                        continue
                    yield CanFrame(
                        t=seconds,
                        addr=int(frame.address),
                        bus=src & (_SENT_FLAG - 1),
                        data=bytes(frame.dat),
                    )
            except Exception as exc:  # noqa: BLE001
                if strict:
                    raise ValueError(f"{path}: unreadable event ({exc})") from exc
                continue
