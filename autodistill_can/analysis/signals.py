"""Signal extraction: deciding where fields start, how wide they are, and how
they are encoded.

Bit-flip rates alone can propose where boundaries *might* be, but they cannot
settle the three questions that matter most:

* **Byte order.** A little-endian 16-bit field looks like two fields to a
  flip-rate scan, because its ramp restarts at the byte boundary.
* **Signedness.** A signed value swinging through zero looks, read as unsigned,
  like a field that leaps between 0 and 65535 every few frames.
* **Width.** Adjacent quiet fields merge; a noisy one fragments.

All three are settled here by decoding each candidate and asking whether the
result behaves like a physical quantity. A correctly decoded signal from a car
moves *smoothly*: sampled at 50-100 Hz, road speed and steering angle barely
change between consecutive frames. Decode the same bits with the bytes swapped,
or as unsigned when they are signed, and consecutive values leap across the
range. That asymmetry is strong, cheap to measure, and needs no ground truth.

So the pass runs in two stages: flip rates propose candidate boundaries, then a
dynamic program picks the non-overlapping set of decoded fields that best
explains the payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..frame import (
    MessageStream,
    extract_be,
    extract_le_dbc,
    to_signed,
)
from .bitstats import BitStats, bit_stats

__all__ = ["Signal", "SignalConfig", "extract_signals", "orientation_scores"]

#: Pseudo-observations mixed into the joint-flip ratio, holding it near neutral
#: until a span has shown enough real changes to speak for itself.
_COHESION_PRIOR = 3.0


@dataclass(frozen=True)
class SignalConfig:
    """Tunables for candidate generation and scoring."""

    #: Fields wider than this are not considered. Real signals almost never
    #: exceed 32 bits, and allowing more invites a single field to swallow a
    #: whole message.
    max_width: int = 32
    #: Frames used when scoring candidates. Smoothness is a distributional
    #: property, so a few thousand samples measure it as well as a million.
    max_eval_frames: int = 1500
    #: Mean absolute step, as a fraction of the field's range, at which a field
    #: is considered to carry no smooth structure at all.
    jump_saturation: float = 0.25
    #: Score per bit awarded to a span that never changes. Deliberately modest:
    #: "this is padding" is a weak claim, and scoring it highly would tempt the
    #: tiler to split a real field's quiet leading bits off as their own.
    constant_score: float = 0.35
    #: Score for a bit no candidate explains.
    unknown_score: float = 0.10
    #: Weights for the two evidence sources. Chosen by grid search against the
    #: synthetic car's known layout; the surface is fairly flat, so these are a
    #: plateau rather than a knife edge.
    weight_smooth: float = 0.45
    weight_ramp: float = 0.55
    #: Exponent applied to the ramp score. Sharpens a soft preference into a
    #: near-requirement: a span straddling two fields still scores ~0.6 on the
    #: raw ramp, which is not far from a real field's ~0.95, but raised to this
    #: power the gap becomes decisive.
    ramp_power: float = 4.0
    #: Penalty weight for a decoding that leaves a large hole in its own range,
    #: which is the signature of a signed value read as unsigned.
    weight_gap: float = 0.5
    #: Values needed before the range-hole test means anything.
    gap_min_distinct: int = 16
    #: A field's least significant bit must flip at least this fraction as often
    #: as the busiest bit in the field. Not 1.0, because "the LSB is busiest"
    #: only holds for a value that moves in small steps. A signal changing by
    #: tens of units per frame saturates every bit below the step size, so the
    #: busiest bit sits partway up the field and the LSB merely looks random.
    lsb_dominance: float = 0.6
    #: Penalty weight for a multi-bit span whose bits change independently.
    weight_cohesion: float = 1.0
    #: Joint-flip ratio at which a span counts as fully cohesive.
    cohesion_saturation: float = 0.3
    #: Charged once per field reported. Regularises the tiling: without it,
    #: splitting a padding run into single bits scores exactly the same as
    #: reporting it once, and the choice comes down to a floating-point tie.
    field_cost: float = 0.3
    #: A boundary is proposed where a bit's flip rate falls to this fraction of
    #: the previous bit's or below -- the point where one field's ramp ends and
    #: the next field's slow-moving MSB begins.
    drop_ratio: float = 0.5
    #: Flip rates at or below this are noise, not signal, when judging a drop.
    noise_floor: float = 0.002


@dataclass
class Signal:
    """One recovered field of a message.

    The meaning of :attr:`start` depends on :attr:`big_endian`, because the two
    byte orders genuinely do not share a coordinate system:

    * **big-endian** — ``start`` is an MSB-first payload bit index, as used
      everywhere else in this package. The field occupies the contiguous span
      ``[start, start + length)``.
    * **little-endian** — ``start`` is a *DBC* start bit: LSB-first numbering
      (``byte * 8 + bit``), naming the field's least significant bit, with the
      field growing upward through increasing indices.

    They are kept in their native conventions rather than forced into one,
    because an Intel field that is not byte-aligned occupies a set of bits that
    is simply *not contiguous* under MSB-first numbering. A 12-bit Intel signal
    at DBC bit 32 covers byte 4 plus the low nibble of byte 5 — MSB-first that
    is bits 32-39 and 44-47, with a hole. Real cars are full of these (they are
    4% of the signals in opendbc's Hyundai database, including vehicle speed),
    so a representation that cannot express them cannot describe those cars.

    :meth:`payload_bits` gives the actual bits either way, for overlap checks.
    """

    start: int
    length: int
    #: ``physical`` (a smooth numeric quantity), ``enum``, ``bool``,
    #: ``constant``, ``counter``, ``checksum``, or ``unknown``.
    kind: str
    big_endian: bool = True
    signed: bool = False
    confidence: float = 0.0
    #: Raw (unscaled) statistics over the capture.
    raw_min: int = 0
    raw_max: int = 0
    distinct: int = 0
    #: Assigned by :mod:`autodistill_can.analysis.correlate` when a reference series
    #: identifies the signal.
    name: str = ""
    unit: str = ""
    scale: float = 1.0
    offset: float = 0.0
    #: Free-text notes carried into the report.
    notes: list[str] = field(default_factory=list)
    #: Optional explicit openpilot field supplied through vehicle-info.json.
    carstate_target: str = ""

    @property
    def end(self) -> int:
        """One past the last bit, in whichever numbering :attr:`start` uses."""
        return self.start + self.length

    @property
    def byte_order(self) -> str:
        return "big" if self.big_endian else "little"

    def payload_bits(self, nbits: int) -> list[int]:
        """MSB-first payload bit positions this signal occupies.

        The one place both conventions are reconciled, so overlap and coverage
        checks can compare signals of either byte order.
        """
        if self.big_endian:
            return list(range(self.start, self.end))
        # Intel: LSB-first indices [start, end) mapped into MSB-first space.
        return sorted(
            (j // 8) * 8 + (7 - (j % 8)) for j in range(self.start, self.end)
        )

    def values(self, stream: MessageStream) -> list[int]:
        """Decode this signal across every frame of ``stream``."""
        if self.big_endian:
            return stream.values_be(self.start, self.length, self.signed)
        raw = [
            extract_le_dbc(p, self.start, self.length) for p in stream.payloads
        ]
        return [to_signed(v, self.length) for v in raw] if self.signed else raw

    def default_name(self, addr: int) -> str:
        """A stable placeholder name, used until something better is known."""
        if self.name:
            return self.name
        prefix = {
            "counter": "COUNTER",
            "checksum": "CHECKSUM",
            "mux": "MUX_MODE",
            "constant": "CONST",
            "bool": "FLAG",
            "enum": "ENUM",
            "unknown": "UNKNOWN",
        }.get(self.kind, "SIG")
        if self.kind in ("counter", "checksum", "mux"):
            return prefix
        return f"{prefix}_{self.start}_{self.length}"

    def __str__(self) -> str:
        sign = "s" if self.signed else "u"
        order = "be" if self.big_endian else "le"
        return (
            f"[{self.start}:{self.end}] {self.kind} {sign}{self.length}{order} "
            f"conf={self.confidence:.2f}"
        )


# --------------------------------------------------------------------------
# Candidate scoring
# --------------------------------------------------------------------------


def _dbc_bit(msb_first: int) -> int:
    """MSB-first payload index to LSB-first (DBC) index. Its own inverse."""
    return (msb_first // 8) * 8 + (7 - (msb_first % 8))


def _to_reversed_bit(msb_first: int, nbits: int) -> int:
    """Where a payload bit lands once the payload's bytes are reversed.

    Byte reversal is *not* a plain index reversal — it moves whole bytes and
    leaves the offset within each byte alone — so a span cannot simply be
    subtracted from ``nbits``. Going via the LSB-first coordinate is what makes
    it exact: the reversed payload's MSB-first numbering is the original's
    LSB-first numbering, backwards.
    """
    return nbits - 1 - _dbc_bit(msb_first)


def _free_spans(nbits: int, blocked: set[int]) -> list[tuple[int, int]]:
    """Contiguous runs of ``[0, nbits)`` that are not blocked.

    Fields are recovered within each free run independently, because a signal
    never straddles the counter or checksum sitting between them.
    """
    spans: list[tuple[int, int]] = []
    run_start: int | None = None
    for bit in range(nbits):
        if bit in blocked:
            if run_start is not None:
                spans.append((run_start, bit - run_start))
                run_start = None
        elif run_start is None:
            run_start = bit
    if run_start is not None:
        spans.append((run_start, nbits - run_start))
    return spans


def _sample_indices(n: int, limit: int) -> list[int]:
    if n <= limit:
        return list(range(n))
    step = n / limit
    return [int(i * step) for i in range(limit)]


def _smoothness(values: list[int]) -> float:
    """Mean absolute step between consecutive samples, relative to the range.

    Near 0 for a correctly decoded physical quantity; around 1/3 for values
    drawn at random, which is what a wrongly decoded field looks like.
    """
    if len(values) < 2:
        return 1.0
    span = max(values) - min(values)
    if span == 0:
        return 0.0
    total = sum(abs(b - a) for a, b in zip(values, values[1:]))
    return (total / (len(values) - 1)) / span


def _largest_gap_fraction(values: list[int]) -> float:
    """Size of the biggest hole in a field's observed range, as a fraction of it.

    This is what catches a signed value being read as unsigned. Steering angle
    swinging through zero occupies ``0..850`` and ``64686..65535`` when read
    unsigned — two clumps with 97% of the range empty between them. Read
    correctly as signed it is one contiguous band.

    Smoothness alone cannot see this, because it divides the step size by the
    range and the wrapped reading inflates *both*: the rare 65000-sized jumps
    are hidden by the 65535-wide range they created.
    """
    unique = sorted(set(values))
    if len(unique) < 3:
        return 0.0
    span = unique[-1] - unique[0]
    if span <= 0:
        return 0.0
    gap = max(b - a for a, b in zip(unique, unique[1:]))
    return gap / span


def _ramp_score(rates: list[float], start: int, length: int) -> float:
    """How cleanly flip rates climb from a field's MSB to its LSB.

    Within a field the rate rises monotonically toward the least significant
    bit. Intel layouts are handled by analysing byte-reversed payloads rather
    than by a second case here.

    The score weighs decreases by *size*, not by how many there are. Counting
    them barely separates anything: a span covering two 16-bit signals has one
    single decrease out of 31 adjacent pairs and scores 0.97, almost as well as
    a genuine field. But that one decrease is a collapse from 0.47 back to zero
    — the entire ramp unwinding — and measured by magnitude it is unmistakable.
    """
    if length < 2:
        return 1.0
    seq = rates[start : start + length]
    rises = sum(max(0.0, b - a) for a, b in zip(seq, seq[1:]))
    drops = sum(max(0.0, a - b) for a, b in zip(seq, seq[1:]))
    total = rises + drops
    return 1.0 if total <= 0 else rises / total


@dataclass
class _Candidate:
    start: int
    length: int
    big_endian: bool
    signed: bool
    kind: str
    score: float
    raw_min: int
    raw_max: int
    distinct: int


def _cohesion(xors: list[int], mask: int, neutral: float) -> float:
    """Fraction of this span's changes that moved more than one bit at once.

    Separates a multi-bit number from a row of unrelated booleans. Incrementing
    a number carries: whenever a high bit changes, every bit below it changes in
    the same frame, so joint changes are routine. Independent flags — a door
    switch beside a blinker — almost never move together, because nothing
    couples them.

    Bit-flip rates cannot see this at all: they are computed per bit and know
    nothing about which bits moved in the *same* frame.
    """
    changed = 0
    joint = 0
    for x in xors:
        bits = (x & mask).bit_count()
        if bits:
            changed += 1
            if bits >= 2:
                joint += 1
    # Shrink toward "no opinion" in proportion to how little was observed.
    # Taking the raw ratio would read absence of evidence as evidence of
    # independence and bury any field that sits still for most of a capture — a
    # lane-keeping torque command, say, which is zero until the system engages.
    # A hard threshold is no good either: blinker flags toggle only a handful of
    # times in a minute of driving, and they are exactly what this test is for.
    # So a few pseudo-observations at the neutral value are mixed in, and real
    # evidence outweighs them as soon as there is any.
    return (joint + _COHESION_PRIOR * neutral) / (changed + _COHESION_PRIOR)


def _evaluate(
    stream: MessageStream,
    stats: BitStats,
    start: int,
    length: int,
    indices: list[int],
    xors: list[int],
    cfg: SignalConfig,
) -> _Candidate | None:
    """Score every encoding of one span and return the best."""
    payloads = stream.payloads
    rates = stats.flip_rate

    if all(stats.flips[b] == 0 for b in range(start, start + length)):
        value = extract_be(payloads[0], start, length)
        return _Candidate(
            start, length, True, False, "constant",
            cfg.constant_score * length - cfg.field_cost, value, value, 1,
        )

    best: _Candidate | None = None

    span_rates = rates[start : start + length]
    busiest = max(span_rates)
    span_mask = ((1 << length) - 1) << (stats.nbits - start - length)
    cohesion = (
        _cohesion(xors, span_mask, cfg.cohesion_saturation) if length > 1 else 1.0
    )

    # Only the big-endian reading is scored. Byte order is decided once per
    # message by `_extract_one_orientation`, which re-runs this whole pass over
    # byte-reversed payloads to cover Intel; searching per candidate as well
    # would let a single message mix the two, which no ECU does.
    #
    # A number's least significant bit is its busiest. If the bit sitting in
    # that position is not, the span does not line up with a real field —
    # either it runs past the end into padding, or it has swallowed a
    # neighbour whose own LSB is the noisy one. Nothing else in the score
    # catches this: trailing constant bits merely rescale the value, and
    # smoothness is normalised by range, so a field that overruns into padding
    # decodes just as smoothly as the real one while covering more bits.
    if rates[start + length - 1] < busiest * cfg.lsb_dominance:
        return None

    # Nor may a field's most significant byte be entirely static. Appending
    # constant high-order bytes leaves every decoded value untouched, so such a
    # span scores identically to the real field while covering more bits, and
    # wins on width alone.
    if length > 8 and all(stats.flips[b] == 0 for b in range(start, start + 8)):
        return None

    raw = [extract_be(payloads[i], start, length) for i in indices]
    ramp = _ramp_score(rates, start, length) ** cfg.ramp_power

    for signed in (False, True):
        if signed and length < 2:
            continue
        values = [to_signed(v, length) for v in raw] if signed else raw
        smooth = 1.0 - min(1.0, _smoothness(values) / cfg.jump_saturation)
        # No bonus for byte alignment. Rewarding it was measurably harmful:
        # most fields are aligned anyway, so the bonus mostly served to split
        # the ones that are not into aligned pieces -- and unaligned Intel
        # fields are where a car keeps its vehicle speed.
        per_bit = cfg.weight_smooth * smooth + cfg.weight_ramp * ramp
        distinct = len(set(values))
        if distinct >= cfg.gap_min_distinct:
            per_bit -= cfg.weight_gap * _largest_gap_fraction(values)
        if length > 1:
            per_bit -= cfg.weight_cohesion * (
                1.0 - min(1.0, cohesion / cfg.cohesion_saturation)
            )

        kind = _kind_for(length, distinct, values)
        candidate = _Candidate(
            start, length, True, signed, kind,
            per_bit * length - cfg.field_cost,
            min(values), max(values), distinct,
        )
        # Ties go to the simpler reading: a field that never goes negative
        # decodes identically either way, and calling it signed on a coin flip
        # would be noise in the output.
        if best is None or candidate.score > best.score + 1e-9:
            best = candidate

    return best


def _kind_for(length: int, distinct: int, values: list[int]) -> str:
    if length == 1:
        return "bool"
    if distinct <= 2:
        return "bool" if set(values) <= {0, 1} else "enum"
    if length <= 4 and distinct <= 8:
        return "enum"
    return "physical"


# --------------------------------------------------------------------------
# Boundary proposal and tiling
# --------------------------------------------------------------------------


def _boundaries(
    stats: BitStats, span_start: int, span_length: int, cfg: SignalConfig
) -> list[int]:
    """Bit positions where a field plausibly begins or ends.

    Byte edges are always included — most fields respect them — along with
    every sharp drop in flip rate and both edges of each constant run, since a
    field's quiet leading bits and a padding region look alike until the tiler
    weighs them against each other.
    """
    rates = stats.flip_rate
    end = span_start + span_length
    marks = {span_start, end}

    for bit in range(span_start, end):
        if bit % 8 == 0:
            marks.add(bit)
        if bit > span_start:
            prev, cur = rates[bit - 1], rates[bit]
            if prev > cfg.noise_floor and cur <= prev * cfg.drop_ratio:
                marks.add(bit)
            was_const = stats.flips[bit - 1] == 0
            is_const = stats.flips[bit] == 0
            if was_const != is_const:
                marks.add(bit)

    return sorted(m for m in marks if span_start <= m <= end)


def _tile(
    candidates: dict[int, list[_Candidate]],
    span_start: int,
    span_end: int,
    cfg: SignalConfig,
) -> list[_Candidate]:
    """Choose the highest-scoring non-overlapping cover of a span.

    A greedy left-to-right choice would commit to a locally attractive narrow
    field and strand the rest, so this is a dynamic program over start
    positions: the best cover of ``[i, end)`` is the best candidate at ``i``
    followed by the best cover of whatever it leaves. Any bit no candidate
    explains falls back to a one-bit ``unknown``, which scores low enough that
    the tiler only resorts to it when nothing fits.
    """
    n = span_end - span_start
    best_score = [0.0] * (n + 1)
    choice: list[_Candidate | None] = [None] * (n + 1)

    for offset in range(n - 1, -1, -1):
        bit = span_start + offset
        fallback = cfg.unknown_score - cfg.field_cost + best_score[offset + 1]
        best_score[offset] = fallback
        choice[offset] = None
        for candidate in candidates.get(bit, ()):
            tail = offset + candidate.length
            if tail > n:
                continue
            total = candidate.score + best_score[tail]
            if total > best_score[offset] + 1e-9:
                best_score[offset] = total
                choice[offset] = candidate

    out: list[_Candidate] = []
    offset = 0
    while offset < n:
        candidate = choice[offset]
        if candidate is None:
            bit = span_start + offset
            out.append(
                _Candidate(
                    bit, 1, True, False, "unknown",
                    cfg.unknown_score - cfg.field_cost, 0, 0, 0,
                )
            )
            offset += 1
        else:
            out.append(candidate)
            offset += candidate.length
    return out


def _merge_unknowns(candidates: list[_Candidate]) -> list[_Candidate]:
    """Collapse runs of single-bit ``unknown`` results into one span."""
    out: list[_Candidate] = []
    for candidate in candidates:
        if (
            candidate.kind == "unknown"
            and out
            and out[-1].kind == "unknown"
            and out[-1].start + out[-1].length == candidate.start
        ):
            out[-1] = _Candidate(
                out[-1].start, out[-1].length + candidate.length, True, False,
                "unknown", out[-1].score + candidate.score, 0, 0, 0,
            )
        else:
            out.append(candidate)
    return out


def extract_signals(
    stream: MessageStream,
    stats: BitStats,
    *,
    exclude: list[tuple[int, int]] | None = None,
    config: SignalConfig | None = None,
    lsb_first: bool | None = None,
) -> list[Signal]:
    """Recover the signal layout of one message.

    ``exclude`` lists spans already explained — the counter and checksum — which
    are left untouched so the caller can splice in its own, better-identified
    entries.

    The message is analysed twice, once assuming its fields are Motorola-ordered
    and once assuming Intel, and the better-scoring reading wins. Pass
    ``lsb_first`` to force one, which is what
    :func:`~autodistill_can.analysis.message.analyse_log` does after deciding the
    byte order for the car as a whole.
    """
    cfg = config or SignalConfig()
    if stats.nbits == 0 or not stream.payloads:
        return []

    if lsb_first is not None:
        return _extract_one_orientation(
            stream, stats, exclude, cfg, lsb_first=lsb_first
        )[0]

    motorola, motorola_score = _extract_one_orientation(
        stream, stats, exclude, cfg, lsb_first=False
    )
    intel, intel_score = _extract_one_orientation(
        stream, stats, exclude, cfg, lsb_first=True
    )
    # An exact tie means the message contains nothing that distinguishes the two
    # — every field fits inside a byte — so the choice genuinely does not matter
    # and Motorola is reported for stability.
    return intel if intel_score > motorola_score else motorola


def orientation_scores(
    stream: MessageStream,
    stats: BitStats,
    *,
    exclude: list[tuple[int, int]] | None = None,
    config: SignalConfig | None = None,
) -> tuple[float, float]:
    """``(motorola, intel)`` evidence for this message's byte order.

    Only multi-byte fields contribute, since narrower ones read identically
    either way. A message made entirely of sub-byte flags therefore scores a
    tie, correctly reporting that it holds no evidence at all.
    """
    cfg = config or SignalConfig()
    if stats.nbits == 0 or not stream.payloads:
        return 0.0, 0.0
    _, motorola = _extract_one_orientation(
        stream, stats, exclude, cfg, lsb_first=False
    )
    _, intel = _extract_one_orientation(stream, stats, exclude, cfg, lsb_first=True)
    return motorola, intel


def _reversed_stream(stream: MessageStream) -> MessageStream:
    """The same message with every payload's bytes reversed.

    This is the whole trick behind Intel support. Reversing the bytes turns any
    little-endian field into a plain big-endian one at a contiguous span, with
    *the same value*, because ``int.from_bytes(d, "little")`` is by definition
    ``int.from_bytes(d[::-1], "big")``. So the existing machinery — flip rates,
    ramp scoring, cohesion, the tiling program — analyses Intel layouts without
    knowing they are Intel, and the spans it finds are converted back at the end.

    Without this, an Intel field that is not byte-aligned cannot be described at
    all: under MSB-first numbering its bits are not contiguous.
    """
    length = stream.length
    return MessageStream(
        bus=stream.bus,
        addr=stream.addr,
        times=stream.times,
        payloads=[p[:length][::-1] for p in stream.payloads],
    )


def _extract_one_orientation(
    stream: MessageStream,
    stats: BitStats,
    exclude: list[tuple[int, int]] | None,
    cfg: SignalConfig,
    *,
    lsb_first: bool,
) -> tuple[list[Signal], float]:
    """Tile the payload assuming a single byte order throughout.

    Returns the signals and a score reflecting only the multi-byte fields, for
    comparing one orientation against the other.

    Assuming one order per message is not a simplification for convenience: an
    OEM picks a byte order for a platform and sticks to it. Every signal in
    opendbc's Hyundai database is Intel; Toyota's are Motorola. Scoring the two
    readings against each other and taking the winner therefore recovers the
    manufacturer's convention rather than guessing per field.
    """
    nbits = stats.nbits
    blocked = {
        bit
        for start, length in (exclude or [])
        for bit in range(max(0, start), min(nbits, start + length))
    }
    if lsb_first:
        stream = _reversed_stream(stream)
        stats = bit_stats(stream)
        blocked = {_to_reversed_bit(bit, nbits) for bit in blocked}

    indices = _sample_indices(len(stream), cfg.max_eval_frames)

    # Frame-to-frame change masks. Cohesion asks which bits moved together, so
    # each mask must compare *adjacent* frames — the strided sample used for
    # smoothness would count every change as simultaneous.
    #
    # The pairs themselves are spread across the whole capture rather than taken
    # from the front. A drive is not uniform: a lane-keeping command sits at
    # zero until the system engages, so the opening frames of a long capture can
    # contain no evidence about it whatsoever.
    length_bytes = stream.length
    payloads = stream.payloads
    n_pairs = len(payloads) - 1
    stride = max(1, n_pairs // cfg.max_eval_frames) if n_pairs > 0 else 1
    xors = [
        int.from_bytes(payloads[i][:length_bytes], "big")
        ^ int.from_bytes(payloads[i + 1][:length_bytes], "big")
        for i in range(0, n_pairs, stride)
    ]

    signals: list[Signal] = []
    total_score = 0.0
    wide_score = 0.0
    for span_start, span_length in _free_spans(stats.nbits, blocked):
        marks = _boundaries(stats, span_start, span_length, cfg)
        by_start: dict[int, list[_Candidate]] = {}

        # Offer every bit as a field in its own right. Neighbouring booleans —
        # door switches, blinker flags — have no ramp to separate them, so
        # nothing in the boundary set distinguishes three independent bits from
        # one 3-bit number. Letting the tiler compare both readings settles it:
        # independent flags each look smooth alone and erratic combined.
        for bit in range(span_start, span_start + span_length):
            candidate = _evaluate(stream, stats, bit, 1, indices, xors, cfg)
            if candidate is not None:
                by_start.setdefault(bit, []).append(candidate)

        for i, start in enumerate(marks):
            for finish in marks[i + 1 :]:
                length = finish - start
                if length > cfg.max_width:
                    # Constant runs are exempt: padding really can span most of
                    # a message, and calling it one field is the honest answer.
                    if not all(
                        stats.flips[b] == 0 for b in range(start, finish)
                    ):
                        continue
                candidate = _evaluate(
                    stream, stats, start, length, indices, xors, cfg
                )
                if candidate is not None:
                    by_start.setdefault(start, []).append(candidate)

        chosen = _merge_unknowns(
            _tile(by_start, span_start, span_start + span_length, cfg)
        )
        total_score += sum(c.score for c in chosen)
        # Only wide *numeric* fields carry evidence about byte order. An 8-bit
        # or sub-byte field decodes identically either way, and a constant span
        # says nothing whatever — yet a long padding run scores highly enough to
        # swamp the comparison, which is exactly how a little-endian message
        # ends up reported as big-endian.
        wide_score += sum(
            c.score
            for c in chosen
            if c.length > 8 and c.kind not in ("constant", "unknown")
        )
        for candidate in chosen:
            per_bit = candidate.score / max(1, candidate.length)
            signals.append(
                Signal(
                    start=candidate.start,
                    length=candidate.length,
                    kind=candidate.kind,
                    big_endian=candidate.big_endian,
                    signed=candidate.signed,
                    confidence=min(1.0, per_bit),
                    raw_min=candidate.raw_min,
                    raw_max=candidate.raw_max,
                    distinct=candidate.distinct,
                )
            )

    if lsb_first:
        # Convert each span back into a DBC little-endian start bit.
        for signal in signals:
            signal.start = nbits - signal.start - signal.length
            signal.big_endian = False

    signals.sort(key=lambda s: s.start)
    return signals, wide_score
