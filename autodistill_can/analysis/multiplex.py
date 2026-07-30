"""Multiplexed message detection.

Some messages reuse the same address for several different payload layouts,
with one small field selecting which. Miss it and the analysis is nonsense: the
multiplexed bytes look like a wildly noisy signal, because consecutive frames
are reporting entirely different quantities.

The tell is conditional continuity. Read frame by frame, the multiplexed bytes
lurch between unrelated quantities — road speed, then engine RPM, then coolant
temperature — so consecutive frames differ in many bits. Follow only the frames
sharing one selector value and the same bytes move smoothly again, because they
are once more a single signal sampled over time.

Note what this does *not* assume: that the multiplexed bytes go quiet within a
mode. They do not — each mode still carries a live signal. It is the *jumpiness
between* successive frames that collapses, and the comparison is conservative,
because frames within one mode are further apart in time and so have more room
to differ.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..frame import MessageStream, extract_be
from .bitstats import BitStats

__all__ = ["MultiplexInfo", "find_multiplexer"]


@dataclass
class MultiplexInfo:
    """A detected multiplexer field and the frames belonging to each value."""

    start: int
    length: int
    #: Mux value -> indices into the stream's frames.
    groups: dict[int, list[int]] = field(default_factory=dict)
    #: How much of the frame-to-frame churn conditioning removes, in [0, 1].
    gain: float = 0.0

    @property
    def end(self) -> int:
        return self.start + self.length

    def __str__(self) -> str:
        return (
            f"multiplexer [{self.start}:{self.end}] "
            f"{len(self.groups)} modes, gain={self.gain:.2f}"
        )


def _mean_churn(values: list[int], indices: list[int], mask: int) -> float:
    """Mean number of masked bits differing between successive listed frames."""
    if len(indices) < 2:
        return 0.0
    total = 0
    for a, b in zip(indices, indices[1:]):
        total += ((values[a] ^ values[b]) & mask).bit_count()
    return total / (len(indices) - 1)


def _balance(groups: dict[int, list[int]], n_frames: int) -> float:
    """How evenly the frames are spread across the modes, in [0, 1].

    A real mode selector *cycles*: an ECU that packs three different payloads
    into one address sends each of them regularly, so the frames divide roughly
    evenly and this approaches 1.

    Without this check the detector fires on any field its neighbours happen to
    track. Three bytes that change together as a unit are enough: hold one of
    them fixed and the other two look predictable, which is exactly the churn
    reduction a multiplexer produces. The difference is the shape of the
    distribution — such a field sits on one dominant value with a long tail of
    rare ones, rather than cycling.

    Measured as Shannon entropy over the mode counts, normalised by the entropy
    of a perfectly even split.
    """
    from math import log2

    if len(groups) < 2 or n_frames <= 0:
        return 0.0
    entropy = 0.0
    for indices in groups.values():
        p = len(indices) / n_frames
        if p > 0:
            entropy -= p * log2(p)
    return entropy / log2(len(groups))


def find_multiplexer(
    stream: MessageStream,
    stats: BitStats,
    *,
    exclude: list[tuple[int, int]] | None = None,
    max_modes: int = 16,
    min_gain: float = 0.4,
    min_frames_per_mode: int = 8,
    min_balance: float = 0.7,
) -> MultiplexInfo | None:
    """Find the multiplexer field, if the message has one.

    Candidates are small byte- or nibble-aligned fields taking few distinct
    values, which is what a mode selector looks like. ``gain`` is the fraction
    of frame-to-frame churn in the *rest* of the payload that disappears once
    the selector is held fixed, and ``min_balance`` additionally requires the
    modes to be visited evenly, as a cycling selector is (see :func:`_balance`).

    ``exclude`` should list the counter and checksum spans. They are kept out of
    the search twice over — neither may serve as the selector, nor count toward
    the churn being explained. A counter is otherwise a tempting false positive:
    it partitions the frames neatly, and on a message whose only lively bytes
    are its counter and checksum, "conditioning on the counter" appears to
    explain most of the variation while meaning nothing.
    """
    nbits = stats.nbits
    length_bytes = stream.length
    if nbits == 0 or len(stream) < min_frames_per_mode * 2:
        return None
    if not stats.changed_bits():
        return None

    blocked = set()
    for start, length in exclude or []:
        blocked.update(range(start, start + length))
    explained_mask = 0
    for bit in blocked:
        explained_mask |= 1 << (nbits - 1 - bit)

    payloads = [int.from_bytes(p[:length_bytes], "big") for p in stream.payloads]
    all_indices = list(range(len(payloads)))
    best: MultiplexInfo | None = None

    for width in (8, 4):
        # A byte-wide selector occupies a whole byte, and a nibble-wide one sits
        # inside a byte. Neither straddles a boundary: no ECU splits its mode
        # field across two bytes, and allowing it invents selectors at offsets
        # like bits 4-11 that fit the churn statistics by coincidence.
        step = 8 if width == 8 else 4
        for start in range(0, nbits - width + 1, step):
            span = set(range(start, start + width))
            if span & blocked:
                continue
            selector = [
                extract_be(p[:length_bytes], start, width) for p in stream.payloads
            ]
            values = sorted(set(selector))
            if not 2 <= len(values) <= max_modes:
                continue

            groups: dict[int, list[int]] = {v: [] for v in values}
            for i, v in enumerate(selector):
                groups[v].append(i)
            if any(len(g) < min_frames_per_mode for g in groups.values()):
                continue
            balance = _balance(groups, len(payloads))
            if balance < min_balance:
                continue

            # Everything but the selector and the already-explained fields.
            rest_mask = ((1 << nbits) - 1) ^ (
                ((1 << width) - 1) << (nbits - start - width)
            )
            rest_mask &= ~explained_mask
            overall = _mean_churn(payloads, all_indices, rest_mask)
            if overall <= 0:
                continue
            within = sum(
                _mean_churn(payloads, indices, rest_mask) * len(indices)
                for indices in groups.values()
            ) / len(payloads)

            gain = (overall - within) / overall
            if gain < min_gain:
                continue
            info = MultiplexInfo(start, width, groups, gain)
            # Ties go to the wider selector: a byte-wide mode field whose high
            # nibble happens to stay zero partitions the frames identically to
            # its low nibble alone, and the byte is the real field.
            if best is None or (gain, width) > (best.gain, best.length):
                best = info

    return best
