"""Generic CSV reader, for logs exported by other tools.

Column names vary between tools, so the header is matched case-insensitively
against a set of aliases. The recognised shapes cover the exports people
actually have lying around:

    time,addr,data                       # this package's own export
    Time,ID,Length,Data                  # SavvyCAN, Vehicle Spy
    timestamp,arbitration_id,data        # python-can's CSV writer
    time,bus,address,d1,d2,...,d8        # cabana / one-byte-per-column

Payloads may be a hex string (``1122aabb``, optionally ``0x``-prefixed or
space/dash separated) or spread across per-byte columns.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Iterator, Sequence

from ..frame import CanFrame
from .compressed import open_maybe_compressed

__all__ = ["read_csv"]

_TIME_KEYS = ("time", "timestamp", "time_stamp", "ts", "abstime", "time (s)")
_ADDR_KEYS = (
    "addr",
    "address",
    "id",
    "canid",
    "can_id",
    "arbitration_id",
    "arbid",
    "identifier",
    "messageid",
)
_DATA_KEYS = ("data", "payload", "bytes", "databytes", "data_bytes", "hex")
_BUS_KEYS = ("bus", "channel", "busno", "bus_no", "interface", "iface")
_LEN_KEYS = ("len", "length", "dlc", "datalength")


def _norm(name: str) -> str:
    return "".join(ch for ch in name.strip().lower() if ch.isalnum() or ch == "_")


def _find(header: Sequence[str], keys: Sequence[str]) -> int | None:
    normalised = [_norm(h) for h in header]
    for key in keys:
        if key in normalised:
            return normalised.index(key)
    return None


def _byte_columns(header: Sequence[str]) -> list[int]:
    """Indices of per-byte payload columns (``d1..d8``, ``byte0..byte7``)."""
    out: list[tuple[int, int]] = []
    for i, name in enumerate(header):
        n = _norm(name)
        for prefix in ("d", "b", "byte", "data"):
            rest = n[len(prefix) :]
            if n.startswith(prefix) and rest.isdigit():
                out.append((int(rest), i))
                break
    out.sort()
    return [i for _, i in out]


def _parse_addr(text: str) -> int:
    text = text.strip()
    if not text:
        raise ValueError("empty address")
    if text.lower().startswith("0x"):
        return int(text, 16)
    # Bare digits are ambiguous. Hex is the near-universal convention for CAN
    # ids in exported logs, and a decimal reading would silently produce a
    # different address, so hex wins whenever the text is valid hex.
    try:
        return int(text, 16)
    except ValueError:
        return int(text, 10)


def _parse_hex_payload(text: str) -> bytes:
    cleaned = text.strip().replace("0x", "").replace(" ", "").replace("-", "")
    cleaned = cleaned.replace(",", "").replace(":", "")
    if len(cleaned) % 2:
        cleaned = "0" + cleaned
    return bytes.fromhex(cleaned) if cleaned else b""


def read_csv(path: Path | str, *, strict: bool = False) -> Iterator[CanFrame]:
    """Stream frames from a CSV export."""
    path = Path(path)
    with io.TextIOWrapper(
        open_maybe_compressed(path), newline="", errors="replace"
    ) as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return

        i_time = _find(header, _TIME_KEYS)
        i_addr = _find(header, _ADDR_KEYS)
        i_data = _find(header, _DATA_KEYS)
        i_bus = _find(header, _BUS_KEYS)
        i_len = _find(header, _LEN_KEYS)
        byte_cols = _byte_columns(header) if i_data is None else []

        if i_addr is None:
            raise ValueError(
                f"{path}: no recognisable address column in header {header!r}"
            )
        if i_data is None and not byte_cols:
            raise ValueError(
                f"{path}: no recognisable data column(s) in header {header!r}"
            )

        for n_row, row in enumerate(reader, start=2):
            if not row or len(row) <= i_addr:
                continue
            try:
                addr = _parse_addr(row[i_addr])
                if i_data is not None:
                    data = _parse_hex_payload(row[i_data])
                else:
                    data = bytes(
                        int(row[c], 16) & 0xFF
                        for c in byte_cols
                        if c < len(row) and row[c].strip()
                    )
                if i_len is not None and i_len < len(row) and row[i_len].strip():
                    data = data[: int(row[i_len])]
                t = (
                    float(row[i_time])
                    if i_time is not None and i_time < len(row) and row[i_time].strip()
                    else n_row * 1e-3
                )
                bus = 0
                if i_bus is not None and i_bus < len(row) and row[i_bus].strip():
                    bus = _bus_value(row[i_bus])
                frame = CanFrame(t=t, addr=addr, data=data, bus=bus)
            except (TypeError, ValueError, IndexError) as exc:
                if strict:
                    raise ValueError(f"{path}:{n_row}: {exc}") from exc
                continue
            yield frame


def _bus_value(text: str) -> int:
    """Coerce a bus/channel cell to an int, tolerating names like ``can1``."""
    text = text.strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else 0
