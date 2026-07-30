"""Core CAN data types.

Bit numbering
-------------
Everything in this package addresses payload bits MSB-first, with global index

    index = byte_index * 8 + (7 - bit_in_byte)

so index 0 is the most significant bit of byte 0. Scanning indices left to
right walks the payload the way it appears in a hex dump, which is what the
field-boundary detector in :mod:`autodistill_can.analysis.signals` assumes and what
the big-endian (Motorola) signals used by most OEMs are laid out as.

DBC files number the *start* bit of a signal differently depending on byte
order, so the conversion lives in one place only: :mod:`autodistill_can.emit.dbc`.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass, field, replace
from typing import Iterable, Iterator, Sequence

__all__ = [
    "CanFrame",
    "CanLog",
    "concatenate",
    "MessageStream",
    "extract_be",
    "extract_le_aligned",
    "extract_le_dbc",
    "payload_bits",
    "resample",
    "to_signed",
]


@dataclass(frozen=True, slots=True)
class CanFrame:
    """A single CAN frame.

    ``t`` is seconds; the origin only has to be consistent within a capture.
    ``bus`` distinguishes the physical bus a multi-bus capture came from (panda
    reports 0/1/2), and is part of a message's identity: address 0x1a6 on bus 0
    is not the same message as 0x1a6 on bus 2.
    """

    t: float
    addr: int
    data: bytes
    bus: int = 0

    def __post_init__(self) -> None:
        """Reject frames that cannot exist on a CAN bus.

        Keeping this invariant at the boundary prevents a malformed CSV row
        from reaching bit extraction much later and failing with a misleading
        shift/index error.
        """
        if not isinstance(self.t, (int, float)) or not math.isfinite(self.t):
            raise ValueError("CAN frame timestamp must be finite")
        if not isinstance(self.addr, int) or not 0 <= self.addr <= 0x1FFFFFFF:
            raise ValueError(
                f"CAN address must be in [0, 0x1FFFFFFF], got {self.addr!r}"
            )
        if not isinstance(self.bus, int) or not 0 <= self.bus <= 255:
            raise ValueError(f"CAN bus must be in [0, 255], got {self.bus!r}")
        if not isinstance(self.data, bytes):
            raise TypeError("CAN frame payload must be bytes")
        if len(self.data) > 64:
            raise ValueError(
                f"CAN payload cannot exceed 64 bytes, got {len(self.data)}"
            )

    @property
    def key(self) -> tuple[int, int]:
        return (self.bus, self.addr)

    @property
    def dlc(self) -> int:
        return len(self.data)

    def hex(self) -> str:
        return self.data.hex()

    def __str__(self) -> str:
        return f"{self.t:.6f} bus{self.bus} {self.addr:03x}#{self.data.hex()}"


def payload_bits(data: bytes) -> list[int]:
    """Expand a payload to a MSB-first list of 0/1, one entry per bit."""
    out: list[int] = []
    for byte in data:
        for shift in (7, 6, 5, 4, 3, 2, 1, 0):
            out.append((byte >> shift) & 1)
    return out


def extract_be(data: bytes, start: int, length: int) -> int:
    """Extract ``length`` bits starting at MSB-first bit index ``start``.

    This is the big-endian / Motorola reading: the bit at ``start`` is the most
    significant bit of the result.
    """
    if length <= 0:
        raise ValueError("length must be positive")
    if start < 0 or start + length > len(data) * 8:
        raise ValueError(
            f"bits [{start}, {start + length}) out of range for {len(data)}-byte payload"
        )
    # Treat the whole payload as one big integer, then shift the window down.
    whole = int.from_bytes(data, "big")
    total = len(data) * 8
    shift = total - (start + length)
    return (whole >> shift) & ((1 << length) - 1)


def extract_le_aligned(data: bytes, start: int, length: int) -> int:
    """Extract a byte-aligned little-endian (Intel) field.

    ``start`` is an MSB-first index as everywhere else, so this covers exactly
    the same span of payload as :func:`extract_be` and the two can be compared
    head to head when guessing a field's byte order. The difference is that the
    bytes within the span are joined least-significant first.

    Restricted to whole-byte spans on purpose. A non-byte-aligned span has no
    single agreed little-endian reading, and real Intel-ordered signals are
    essentially always 16/32/64-bit aligned; candidate generation in
    :mod:`autodistill_can.analysis.signals` therefore only offers little-endian for
    aligned spans. To decode an arbitrary signal from a DBC, use
    :func:`extract_le_dbc`, which implements the DBC convention exactly.
    """
    if length <= 0 or length % 8 or start % 8:
        raise ValueError("extract_le_aligned requires a byte-aligned span")
    if start < 0 or start + length > len(data) * 8:
        raise ValueError(
            f"bits [{start}, {start + length}) out of range for {len(data)}-byte payload"
        )
    first = start // 8
    return int.from_bytes(data[first : first + length // 8], "little")


def extract_le_dbc(data: bytes, start_bit: int, length: int) -> int:
    """Extract an Intel-ordered signal using the DBC convention.

    In a DBC, a little-endian signal's ``start_bit`` names its *least*
    significant bit under LSB-first numbering (``index = byte * 8 + bit``), and
    the signal grows upward through increasing bit indices. That makes the
    whole payload a little-endian integer which the field is shifted out of.
    """
    if length <= 0:
        raise ValueError("length must be positive")
    if start_bit < 0 or start_bit + length > len(data) * 8:
        raise ValueError(
            f"bits [{start_bit}, {start_bit + length}) out of range "
            f"for {len(data)}-byte payload"
        )
    whole = int.from_bytes(data, "little")
    return (whole >> start_bit) & ((1 << length) - 1)


def to_signed(value: int, length: int) -> int:
    """Reinterpret an unsigned ``length``-bit field as two's complement."""
    sign = 1 << (length - 1)
    return value - (1 << length) if value & sign else value


@dataclass
class MessageStream:
    """All frames observed for one ``(bus, addr)`` pair, in capture order.

    Frames of a single address are the unit every analysis in this package
    works on: bit statistics, counters, checksums and signal extraction are all
    per-message. Payload length is expected to be constant; if a message ever
    changes length the shortest length wins for bit-indexed analysis and
    :attr:`length_varies` is set so callers can report it.
    """

    bus: int
    addr: int
    times: list[float] = field(default_factory=list)
    payloads: list[bytes] = field(default_factory=list)

    def add(self, frame: CanFrame) -> None:
        self.times.append(frame.t)
        self.payloads.append(frame.data)

    def __len__(self) -> int:
        return len(self.payloads)

    @property
    def key(self) -> tuple[int, int]:
        return (self.bus, self.addr)

    @property
    def length(self) -> int:
        """Payload length used for bit-indexed analysis (the shortest seen)."""
        return min((len(p) for p in self.payloads), default=0)

    @property
    def length_varies(self) -> bool:
        return len({len(p) for p in self.payloads}) > 1

    @property
    def nbits(self) -> int:
        return self.length * 8

    @property
    def duration(self) -> float:
        if len(self.times) < 2:
            return 0.0
        return self.times[-1] - self.times[0]

    @property
    def period(self) -> float | None:
        """Mean inter-frame interval in seconds, or None if undeterminable."""
        if len(self.times) < 2 or self.duration <= 0:
            return None
        return self.duration / (len(self.times) - 1)

    @property
    def frequency(self) -> float | None:
        p = self.period
        return (1.0 / p) if p else None

    def jitter(self) -> float | None:
        """Standard deviation of the inter-frame interval, in seconds.

        Cyclic messages sit near zero; event-driven ones are wildly irregular,
        which is a useful hint when deciding whether a message is a periodic
        state broadcast or a one-shot request.
        """
        if len(self.times) < 3:
            return None
        deltas = [b - a for a, b in zip(self.times, self.times[1:])]
        mean = sum(deltas) / len(deltas)
        var = sum((d - mean) ** 2 for d in deltas) / len(deltas)
        return var**0.5

    def unique_payloads(self) -> int:
        return len(set(self.payloads))

    def values_be(self, start: int, length: int, signed: bool = False) -> list[int]:
        """Time series of a big-endian field across every frame."""
        vals = [extract_be(p, start, length) for p in self.payloads]
        return [to_signed(v, length) for v in vals] if signed else vals

    def values_le(self, start: int, length: int, signed: bool = False) -> list[int]:
        """Time series of a byte-aligned little-endian field across every frame."""
        vals = [extract_le_aligned(p, start, length) for p in self.payloads]
        return [to_signed(v, length) for v in vals] if signed else vals

    def values(
        self, start: int, length: int, big_endian: bool = True, signed: bool = False
    ) -> list[int]:
        if big_endian:
            return self.values_be(start, length, signed)
        return self.values_le(start, length, signed)


class CanLog:
    """An ordered capture, indexed by ``(bus, addr)``.

    Sources yield :class:`CanFrame` objects; ``CanLog.from_frames`` buckets
    them into :class:`MessageStream` objects for analysis. Frames are assumed
    to arrive in non-decreasing time order, as every capture format we read
    does; ``sort()`` is available for the ones that don't.
    """

    def __init__(self) -> None:
        self.streams: dict[tuple[int, int], MessageStream] = {}
        self.n_frames = 0
        self.t_start: float | None = None
        self.t_end: float | None = None

    @classmethod
    def from_frames(cls, frames: Iterable[CanFrame]) -> "CanLog":
        log = cls()
        for frame in frames:
            log.add(frame)
        return log

    def add(self, frame: CanFrame) -> None:
        stream = self.streams.get(frame.key)
        if stream is None:
            stream = MessageStream(bus=frame.bus, addr=frame.addr)
            self.streams[frame.key] = stream
        stream.add(frame)
        self.n_frames += 1
        if self.t_start is None or frame.t < self.t_start:
            self.t_start = frame.t
        if self.t_end is None or frame.t > self.t_end:
            self.t_end = frame.t

    def sort(self) -> None:
        """Restore time order inside every stream (and hence the whole log)."""
        for stream in self.streams.values():
            order = sorted(range(len(stream)), key=stream.times.__getitem__)
            stream.times = [stream.times[i] for i in order]
            stream.payloads = [stream.payloads[i] for i in order]

    @property
    def duration(self) -> float:
        if self.t_start is None or self.t_end is None:
            return 0.0
        return self.t_end - self.t_start

    @property
    def buses(self) -> list[int]:
        return sorted({bus for bus, _ in self.streams})

    def sorted_streams(self) -> list[MessageStream]:
        """Streams ordered by bus then address — stable output ordering."""
        return [self.streams[k] for k in sorted(self.streams)]

    def frames(self) -> Iterator[CanFrame]:
        """Re-emit every frame in global time order."""
        merged: list[tuple[float, int, int, bytes]] = []
        for stream in self.streams.values():
            for t, payload in zip(stream.times, stream.payloads):
                merged.append((t, stream.bus, stream.addr, payload))
        merged.sort(key=lambda row: row[0])
        for t, bus, addr, payload in merged:
            yield CanFrame(t=t, addr=addr, data=payload, bus=bus)

    def drop_short_streams(self, min_frames: int) -> list[tuple[int, int]]:
        """Remove messages with too few samples to say anything about.

        Statistics over a handful of frames are noise, and a capture always
        catches a few one-off diagnostic or transition messages. Returns the
        keys that were dropped so the report can mention them.
        """
        dropped = [k for k, s in self.streams.items() if len(s) < min_frames]
        for key in dropped:
            stream = self.streams.pop(key)
            self.n_frames -= len(stream)
        return sorted(dropped)

    def __len__(self) -> int:
        return self.n_frames

    def summary(self) -> str:
        return (
            f"{self.n_frames} frames, {len(self.streams)} distinct messages "
            f"on {len(self.buses)} bus(es), {self.duration:.1f}s"
        )


def resample(
    times: Sequence[float], values: Sequence[float], grid: Sequence[float]
) -> list[float]:
    """Sample-and-hold a series onto ``grid``.

    Zero-order hold rather than interpolation: a CAN signal genuinely holds its
    last transmitted value until the next frame, and interpolating would invent
    transitions that the bus never carried. Grid points before the first sample
    take the first value.
    """
    if not times:
        raise ValueError("cannot resample an empty series")
    out: list[float] = []
    for g in grid:
        i = bisect_left(times, g)
        if i == 0:
            out.append(float(values[0]))
        elif i >= len(times):
            out.append(float(values[-1]))
        elif times[i] == g:
            out.append(float(values[i]))
        else:
            out.append(float(values[i - 1]))
    return out


#: Seam left between concatenated captures. Deliberately tiny: this gap lands
#: in every message's inter-frame deltas once, and a large one would sit there
#: as an enormous outlier, inflating the jitter that decides whether a message
#: is a cyclic broadcast or event-driven.
_SEAM = 0.001


def concatenate(logs: "Iterable[CanLog]", *, seam: float = _SEAM) -> "CanLog":
    """Lay several captures of the same car end to end on one timeline.

    Separate recordings each start from their own zero, so merging them as
    they are interleaves two drives into one impossible conversation: every
    message appears twice as fast as it is, its period halves, and its counter
    seems to run twice at once. Offsetting each capture to begin just after the
    previous one ended keeps each message's real cadence, and costs one bad
    step per message per join -- negligible against thousands of frames, and
    far cheaper than the alternative.

    What extra captures buy is coverage: fields that only move when something
    happens (indicators, reverse, blind-spot) need a drive that did it, and an
    underdetermined checksum needs more variety than one drive gave. What they
    do not do is extend correlation, which only ever spans the reference log's
    own window.
    """
    merged = CanLog()
    offset = 0.0
    end: float | None = None
    for log in logs:
        if log.t_start is None:
            continue
        offset = 0.0 if end is None else (end - log.t_start) + seam
        for frame in log.frames():
            merged.add(replace(frame, t=frame.t + offset))
        end = log.t_end + offset
    merged.sort()
    return merged
