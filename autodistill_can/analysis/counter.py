"""Rolling counter detection.

Most safety-relevant messages carry a small counter that increments by one on
every transmission and wraps. Finding it matters for two reasons: it has to be
carved out before signal segmentation or it pollutes a neighbouring field, and
any message openpilot needs to *send* must reproduce the counter correctly or
the receiving ECU rejects the frame.

The test is direct — take the field's value across consecutive frames, and see
whether the difference modulo the field width is the same every time. A physical
signal's low bits do not do that; a counter does.
"""

from __future__ import annotations

from collections import Counter as _Counter
from dataclasses import dataclass

from ..frame import MessageStream, extract_be
from .bitstats import BitStats

__all__ = ["CounterField", "find_counter"]

#: Four-, eight-, and sixteen-bit counters are normally nibble-aligned.  The
#: shorter widths are not: Toyota STEERING_LKA has a six-bit counter beginning
#: one bit into the payload, for example.
_WIDTHS = (4, 8, 2, 3, 6, 16)


@dataclass
class CounterField:
    """A detected rolling counter."""

    start: int
    length: int
    #: Increment per frame, modulo ``2 ** length``. Almost always 1.
    step: int
    #: Fraction of consecutive frame pairs that matched ``step``.
    score: float
    #: Distinct values observed, out of ``2 ** length`` possible.
    coverage: float

    @property
    def end(self) -> int:
        return self.start + self.length

    def __str__(self) -> str:
        return (
            f"counter [{self.start}:{self.end}] step={self.step} "
            f"match={self.score:.3f}"
        )


def _evaluate(
    stream: MessageStream, start: int, length: int
) -> tuple[int, float, float] | None:
    """Score a span as a counter. Returns ``(step, match_rate, coverage)``."""
    modulus = 1 << length
    values = [extract_be(p, start, length) for p in stream.payloads]
    if len(values) < 8:
        return None

    deltas = _Counter(
        (b - a) % modulus for a, b in zip(values, values[1:])
    )
    step, hits = deltas.most_common(1)[0]
    if step == 0:
        return None

    coverage = len(set(values)) / modulus
    return step, hits / (len(values) - 1), coverage


def find_counter(
    stream: MessageStream,
    stats: BitStats,
    *,
    min_score: float = 0.95,
    min_coverage: float = 0.5,
) -> CounterField | None:
    """Locate the message's rolling counter, if it has one.

    ``min_score`` is below 1.0 because real captures drop frames — a USB hiccup
    or a busy bus leaves a gap, and the counter legitimately jumps by two there.
    ``min_coverage`` requires the field to actually cycle through its range,
    which is what separates a counter from a slowly ramping physical value whose
    steps happen to be uniform.
    """
    best: CounterField | None = None

    for length in _WIDTHS:
        stride = 4 if length in (4, 8, 16) else 1
        for start in range(0, stats.nbits - length + 1, stride):
            # A counter's lowest bit changes on every single frame. Checking
            # that first skips the overwhelming majority of spans without
            # decoding anything.
            lsb = start + length - 1
            if stats.flips[lsb] < (stats.n_samples - 1) * min_score:
                continue

            result = _evaluate(stream, start, length)
            if result is None:
                continue
            step, score, coverage = result
            if score < min_score or coverage < min_coverage:
                continue

            candidate = CounterField(start, length, step, score, coverage)
            # Prefer a wider counter at the same quality: a 4-bit counter's low
            # 3 bits also pass the test, and the widest span that still
            # increments cleanly is the real field.
            if best is None or (candidate.length, candidate.score) > (
                best.length,
                best.score,
            ):
                best = candidate

    return best
