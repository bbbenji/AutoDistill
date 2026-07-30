"""Checksum recovery.

Reproducing a message's checksum is the hard gate on any openpilot port: until
you can compute it, the car ignores everything you send. So this module tries
hard, with two complementary engines.

**1. Known algorithms.** OEM checksums are often arithmetic sums that fold in the
address and length (see :data:`autodistill_can.algos.CHECKSUM_ALGOS`). Sums carry
between bit positions, so they are *not* linear over GF(2) and no amount of
linear algebra will find them — they have to be tried directly.

**2. A generic GF(2) affine solver.** Every CRC is an affine function of its input
bits: ``crc(data) = L(data) XOR crc(0)`` for a fixed linear map ``L``. So instead
of brute-forcing polynomial, init and xorout — 16 million combinations — we solve
for ``L`` directly by Gaussian elimination over the observed frames, in one pass.
This cracks any CRC, XOR or parity scheme *without knowing which one it is*, and
then names the polynomial afterwards by comparing against known parameter sets.

The affine solver also handles the awkward real-world case that defeats
textbook approaches: VW's MQB messages mix in a magic byte selected from a
per-address table by the counter value. That is not linear in the counter bits,
so the global solve fails — but holding the counter fixed makes it constant
again, so solving once per counter value recovers both the polynomial and the
whole magic table.

Results are always validated on frames held out of the solve, so an
underdetermined fit cannot be reported as a confident answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..algos import (
    CHECKSUM_ALGOS,
    KNOWN_CRC8,
    KNOWN_CRC16,
    Crc8Params,
    Crc16Params,
)
from ..frame import MessageStream, extract_be
from .bitstats import BitStats
from .counter import CounterField

__all__ = [
    "ChecksumSolution",
    "crack_checksum",
    "solve_affine",
]


@dataclass
class ChecksumSolution:
    """A recovered checksum: where it is and how to compute it."""

    start: int
    length: int
    #: ``algo`` for a named arithmetic algorithm, ``affine`` for a solved GF(2)
    #: map, ``affine_per_counter`` when the constant term is keyed by a counter.
    method: str
    #: Fraction of held-out frames the solution predicted correctly.
    accuracy: float
    #: Frames the accuracy was measured on.
    n_verified: int
    #: For ``method == "algo"``: registry name, e.g. ``toyota``.
    algo: str = ""
    #: For ``method == "algo"``: which payload bytes were fed in.
    input_selection: str = ""
    #: Human-readable formula.
    note: str = ""
    #: For affine solutions: one mask per output bit (MSB-first), each a bit set
    #: over the columns listed in :attr:`input_bits`, plus the constant term.
    masks: list[int] = field(default_factory=list)
    constants: list[int] = field(default_factory=list)
    #: Payload bit positions the mask columns refer to. Column ``k`` is
    #: ``input_bits[k]`` and occupies mask bit ``len(input_bits) - 1 - k``.
    input_bits: list[int] = field(default_factory=list)
    #: For ``affine_per_counter``: additive term per counter value.
    counter_constants: dict[int, int] = field(default_factory=dict)
    #: Name of the matching CRC parameter set, if one was identified.
    crc_name: str = ""
    crc_params: Crc8Params | Crc16Params | None = None
    #: Which bytes the identified CRC covers.
    crc_framing: str = ""
    #: Rank of the observation matrix, and how many unknowns it had to pin down.
    rank: int = 0
    n_unknowns: int = 0

    @property
    def end(self) -> int:
        return self.start + self.length

    @property
    def n_mismatches(self) -> int:
        """Frames the solution failed to reproduce, as a plain count.

        Reported alongside the rate because a percentage hides the thing you
        need to know. A solution that is right on 99.996% of frames rounds to
        "100.00%" at any sane number of decimal places, yet it is *not* exact --
        and for a message openpilot has to transmit, the difference between
        "always accepted" and "rejected once in twenty thousand" is worth
        seeing.
        """
        return round(self.n_verified * (1.0 - self.accuracy))

    @property
    def underdetermined(self) -> bool:
        """Whether the capture left the map ambiguous.

        When the observations do not span the unknowns, many different maps
        reproduce the data. The reported one is right for payloads like those
        seen and unreliable beyond them; the fix is a more varied capture, not a
        better solver.
        """
        return bool(self.n_unknowns) and self.rank < self.n_unknowns

    @property
    def is_confident(self) -> bool:
        return self.accuracy >= 0.999 and self.n_verified >= 20

    def describe(self) -> str:
        if self.method == "algo":
            return f"{self.algo} over {self.input_selection} — {self.note}"
        parts: list[str] = []
        if self.crc_name:
            parts.append(f"{self.crc_name} over {self.crc_framing}")
        else:
            parts.append("GF(2) affine map (polynomial not in the known set)")
        if self.method == "affine_per_counter":
            parts.append(
                f"additive term selected by the counter "
                f"({len(self.counter_constants)} entries recovered)"
            )
        if self.underdetermined:
            parts.append(
                f"UNDERDETERMINED: rank {self.rank} of {self.n_unknowns} unknowns"
            )
        return ", ".join(parts)

    def __str__(self) -> str:
        return (
            f"checksum [{self.start}:{self.end}] {self.describe()} "
            f"acc={self.accuracy:.4f} on {self.n_verified}"
        )


# --------------------------------------------------------------------------
# GF(2) linear algebra. Rows are Python ints used as bit vectors, so a whole
# row reduces with one XOR — which is what makes this fast enough to run over
# every candidate position on every message.
# --------------------------------------------------------------------------


def solve_affine(
    rows: list[int], targets: list[int], n_cols: int
) -> tuple[int, int] | None:
    """Solve ``<v, x> XOR c = y`` over GF(2) for a single output bit.

    ``rows`` holds each observation's input bits packed into an int (bit ``i`` of
    the int is input column ``i``); ``targets`` the observed output bit. Returns
    ``(v, c)`` with ``v`` packed the same way, or None if no affine function
    fits the data.

    The constant term is handled by appending an always-set column, so ``c``
    falls out of the same elimination rather than needing a separate case.
    """
    const_col = n_cols
    pivots: dict[int, tuple[int, int]] = {}

    for row, target in zip(rows, targets):
        r = row | (1 << const_col)
        t = target
        while r:
            col = r.bit_length() - 1
            pivot = pivots.get(col)
            if pivot is None:
                pivots[col] = (r, t)
                break
            r ^= pivot[0]
            t ^= pivot[1]
        else:
            if t:
                return None  # 0 = 1: no affine function fits

    # Back-substitute with every free variable set to zero. Any particular
    # solution predicts correctly on the span the training rows cover; the
    # held-out check in `_verify_affine` is what decides whether that span
    # generalises.
    #
    # Elimination leaves rows in echelon (not *reduced* echelon) form, so a
    # pivot row's remaining bits all sit at columns below its pivot. Its
    # equation therefore depends on lower-indexed unknowns, and columns must be
    # resolved in increasing order for those to be known when they are needed.
    solution = 0
    for col in sorted(pivots):
        row, target = pivots[col]
        rest = row & ~(1 << col)
        if target ^ ((solution & rest).bit_count() & 1):
            solution |= 1 << col

    const = (solution >> const_col) & 1
    return solution & ((1 << n_cols) - 1), const


def gf2_rank(rows: list[int]) -> int:
    """Rank of a set of GF(2) row vectors.

    Tells you whether the capture actually pins the map down. If the rank is
    below the number of unknowns, many different maps reproduce the data
    equally well and the one reported is an arbitrary representative — correct
    for every payload resembling those observed, but not to be trusted on
    payloads outside that span.
    """
    pivots: dict[int, int] = {}
    rank = 0
    for row in rows:
        r = row
        while r:
            col = r.bit_length() - 1
            pivot = pivots.get(col)
            if pivot is None:
                pivots[col] = r
                rank += 1
                break
            r ^= pivot
    return rank


def _input_rows(
    stream: MessageStream, start: int, length: int, indices: list[int],
    max_frames: int | None = None,
) -> tuple[list[int], list[int]]:
    """Pack each frame's non-checksum payload bits into an int, plus the target.

    Column ``k`` of the row corresponds to ``indices[k]``, an MSB-first payload
    bit position outside the checksum field.

    ``max_frames`` subsamples evenly across the capture. Pinning down a 57-bit
    map needs 57 independent observations, not the 300k frames an hour-long
    capture holds, and taking every n-th frame keeps the held-out split spread
    over the whole drive rather than one moment of it.
    """
    rows: list[int] = []
    targets: list[int] = []
    msb_shift = [len(indices) - 1 - k for k in range(len(indices))]

    payloads = stream.payloads
    if max_frames is not None and len(payloads) > max_frames:
        step = len(payloads) / max_frames
        payloads = [payloads[int(i * step)] for i in range(max_frames)]

    for payload in payloads:
        whole = int.from_bytes(payload[: stream.length], "big")
        total = stream.nbits
        row = 0
        for k, bit in enumerate(indices):
            if (whole >> (total - 1 - bit)) & 1:
                row |= 1 << msb_shift[k]
        rows.append(row)
        targets.append(extract_be(payload, start, length))
    return rows, targets


def _verify_affine(
    rows: list[int], targets: list[int], masks: list[int], constants: list[int],
    length: int,
) -> float:
    """Fraction of observations the solved map reproduces exactly."""
    if not rows:
        return 0.0
    correct = 0
    for row, target in zip(rows, targets):
        predicted = 0
        for j in range(length):
            bit = ((row & masks[j]).bit_count() & 1) ^ constants[j]
            predicted = (predicted << 1) | bit
        if predicted == target:
            correct += 1
    return correct / len(rows)


def _try_affine(
    rows: list[int], targets: list[int], n_cols: int, length: int, stride: int = 3
) -> tuple[list[int], list[int], float, int] | None:
    """Solve every output bit on a training split, then score on held-out rows.

    The split is interleaved (every ``stride``-th row is held out) rather than
    chronological. A drive passes through distinct regimes — stopped, cruising,
    braking — and training on the first stretch of one then testing on a later
    stretch asks the solved map to extrapolate to payloads unlike anything it
    was fitted on. That is a question about the *capture's* coverage, not about
    whether the field is a checksum, and :attr:`ChecksumSolution.underdetermined`
    reports it directly. Interleaving asks the question we actually mean: does
    this relation hold across the whole capture?
    """
    n = len(rows)
    train_rows = [r for i, r in enumerate(rows) if i % stride]
    train_targets = [t for i, t in enumerate(targets) if i % stride]
    test_rows = [r for i, r in enumerate(rows) if not i % stride]
    test_targets = [t for i, t in enumerate(targets) if not i % stride]
    if len(train_rows) < min(n, n_cols + 1):
        train_rows, train_targets = rows, targets
        test_rows, test_targets = rows, targets

    masks: list[int] = []
    constants: list[int] = []
    for j in range(length):
        shift = length - 1 - j  # output bit j, MSB-first
        bits = [(t >> shift) & 1 for t in train_targets]
        solved = solve_affine(train_rows, bits, n_cols)
        if solved is None:
            return None
        masks.append(solved[0])
        constants.append(solved[1])

    accuracy = _verify_affine(test_rows, test_targets, masks, constants, length)
    return masks, constants, accuracy, len(test_rows)


#: How a CRC's input might be framed. A real formula rarely feeds the CRC the
#: whole payload: it usually covers just the bytes around the checksum, and
#: sometimes appends an extra byte (VW MQB's counter-keyed magic byte). Each
#: framing maps a probe payload to the byte string actually fed to the CRC.
_FRAMINGS: tuple[tuple[str, int], ...] = (
    ("whole payload", 0),
    ("covered bytes only", 1),
    ("covered bytes plus one trailing byte", 2),
)


def _frame_input(probe: bytes, bytes_used: list[int], framing: int) -> bytes:
    if framing == 0:
        return probe
    compact = bytes(probe[b] for b in bytes_used)
    return compact + b"\x00" if framing == 2 else compact


def _identify_crc(
    masks: list[int],
    constants: list[int],
    indices: list[int],
    nbits: int,
    length: int,
    *,
    match_constant: bool = True,
) -> tuple[str, Crc8Params | Crc16Params, str] | None:
    """Name a recovered affine map by matching known CRC parameter sets.

    Each candidate CRC is turned into its own affine map by evaluating it on the
    all-zero payload (giving the constant term) and on each single-bit payload
    (giving that column of the linear part). Comparing maps is exact, so a match
    is proof, not a guess.

    ``match_constant`` is disabled when the additive term is known to be
    supplied from outside the payload — a counter-keyed magic byte, say. The
    linear part still pins down the polynomial, which is the part you cannot
    read off the data by eye.
    """
    registry = {8: KNOWN_CRC8, 16: KNOWN_CRC16}.get(length)
    if registry is None:
        return None
    n_bytes = nbits // 8
    bytes_used = sorted({bit // 8 for bit in indices})

    for framing_name, framing in _FRAMINGS:
        for params in registry:
            base = params(_frame_input(bytes(n_bytes), bytes_used, framing))
            expect_masks = [0] * length
            for k, bit in enumerate(indices):
                probe = bytearray(n_bytes)
                probe[bit // 8] |= 1 << (7 - (bit % 8))
                column = params(_frame_input(bytes(probe), bytes_used, framing)) ^ base
                for j in range(length):
                    if (column >> (length - 1 - j)) & 1:
                        expect_masks[j] |= 1 << (len(indices) - 1 - k)
            if expect_masks != masks:
                continue
            if match_constant and any(
                ((base >> (length - 1 - j)) & 1) != constants[j]
                for j in range(length)
            ):
                continue
            return params.name, params, framing_name
    return None


# --------------------------------------------------------------------------
# Engine 1: named arithmetic algorithms
# --------------------------------------------------------------------------

#: How the payload is presented to a candidate algorithm. Real formulas differ
#: in whether they cover the bytes before the checksum, after it, or all of them
#: with the field zeroed.
_SELECTIONS = ("before", "after", "zeroed", "field_zeroed", "whole")

#: Frames used per GF(2) solve. Enough to over-determine a 64-bit map sixteen
#: times over, which is what the per-counter variant needs.
_MAX_SOLVE_FRAMES = 1600


def _select_input(
    payload: bytes, length_bytes: int, start: int, length: int, selection: str
) -> bytes:
    """Present the payload to a candidate algorithm the way its formula expects.

    ``field_zeroed`` blanks exactly the checksum's own bits and keeps everything
    else. That distinction matters for sub-byte checksums: a real Kia Soul EV
    puts a 4-bit checksum in the high nibble of a byte whose low nibble holds
    the rolling counter, and the counter *is* part of what the checksum covers.
    Zeroing the whole byte, as ``zeroed`` does, would throw it away.
    """
    cb = start // 8
    if selection == "before":
        return payload[:cb]
    if selection == "after":
        return payload[cb + 1 : length_bytes]
    if selection == "zeroed":
        buf = bytearray(payload[:length_bytes])
        buf[cb] = 0
        return bytes(buf)
    if selection == "field_zeroed":
        total = length_bytes * 8
        mask = ((1 << length) - 1) << (total - start - length)
        whole = int.from_bytes(payload[:length_bytes], "big") & ~mask
        return whole.to_bytes(length_bytes, "big")
    return payload[:length_bytes]


def _try_named_algos(
    stream: MessageStream, start: int, length: int, sample: int = 48
) -> ChecksumSolution | None:
    """Try the registry at one candidate position, cheaply first then fully."""
    addr = stream.addr
    n_bytes = stream.length
    payloads = stream.payloads

    # Screen on a small sample; almost every combination dies on the first few
    # frames, and only survivors are worth a full pass.
    step = max(1, len(payloads) // sample)
    screen = payloads[::step][:sample]

    for algo in CHECKSUM_ALGOS:
        if algo.width != length:
            continue
        for selection in _SELECTIONS:
            if selection == "whole" and algo.name != "honda":
                # Feeding a checksum its own output only makes sense for
                # formulas that mask it out internally, as Honda's does.
                continue
            if _screen_algo(algo, addr, n_bytes, selection, screen, start, length):
                accuracy, n = _score_algo(
                    algo, addr, n_bytes, selection, payloads, start, length
                )
                if accuracy >= 0.999:
                    return ChecksumSolution(
                        start=start,
                        length=length,
                        method="algo",
                        accuracy=accuracy,
                        n_verified=n,
                        algo=algo.name,
                        input_selection=selection,
                        note=algo.note,
                    )
    return None


def _screen_algo(algo, addr, n_bytes, selection, payloads, start, length) -> bool:
    for payload in payloads:
        data = _select_input(payload, n_bytes, start, length, selection)
        if algo.fn(addr, data, n_bytes) != extract_be(payload, start, length):
            return False
    return True


def _score_algo(
    algo, addr, n_bytes, selection, payloads, start, length
) -> tuple[float, int]:
    correct = 0
    for payload in payloads:
        data = _select_input(payload, n_bytes, start, length, selection)
        if algo.fn(addr, data, n_bytes) == extract_be(payload, start, length):
            correct += 1
    return correct / len(payloads), len(payloads)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _candidate_positions(stats: BitStats) -> list[tuple[int, int]]:
    """Byte-aligned 16- and 8-bit spans, and nibble-aligned 4-bit ones.

    A field that never changed carries no evidence, so it is skipped: claiming
    it is a checksum would be unfalsifiable.

    16-bit spans are only worth trying on payloads long enough to warrant one.
    Eight bits of protection over sixty-four bytes is thin, so CAN-FD platforms
    use 16-bit CRCs; a classic 8-byte message never does, and offering the
    position anyway would just be two more chances for a false positive.
    """
    out: list[tuple[int, int]] = []
    if stats.nbits > 64:
        for start in range(0, stats.nbits - 15, 8):
            if any(stats.flips[b] for b in range(start, start + 16)):
                out.append((start, 16))
    for start in range(0, stats.nbits - 7, 8):
        if any(stats.flips[b] for b in range(start, start + 8)):
            out.append((start, 8))
    for start in range(0, stats.nbits - 3, 4):
        if any(stats.flips[b] for b in range(start, start + 4)):
            out.append((start, 4))
    return out


def _jumpiness(values: list[int], length: int) -> float:
    """How unlike a smooth physical quantity a field's time series is.

    A checksum changes unpredictably, so consecutive values are far apart
    relative to the field's range; a physical signal sampled at 50-100 Hz barely
    moves between frames. For a uniformly random field the mean absolute step is
    about a third of the range, so that is taken as full marks.

    This is what tells a real checksum from an *alias* of one. Sum-type
    checksums make the whole message sum to a constant, which means every byte
    is algebraically a valid checksum of the others — including a byte that is
    really the high half of a steering torque. Only the true checksum looks
    random.
    """
    if len(values) < 2:
        return 0.0
    span = max(values) - min(values)
    if span == 0:
        return 0.0
    mean_step = sum(abs(b - a) for a, b in zip(values, values[1:])) / (len(values) - 1)
    return min(1.0, (mean_step / span) / 0.33)


#: Tie-breaking nudge for a checksum sitting next to the rolling counter.
#: Deliberately small. Checksum and counter being neighbours is a real and very
#: common OEM convention, but it is weaker evidence than the field looking
#: random or sitting at the end of the message, and a large bonus here would
#: start overturning those.
_ADJACENT_TO_COUNTER_BONUS = 0.15


def _plausibility(
    solution: ChecksumSolution,
    values: list[int],
    stats: BitStats,
    counter: CounterField | None = None,
) -> float:
    """Rank competing explanations of the same message.

    Weights encode what real buses look like: a named formula beats an
    equivalent anonymous bit-mask because it tells the person doing the port
    what to write; a checksum's own bits look random; checksums sit at the end
    of a message far more often than anywhere else; and they are usually
    adjacent to the counter.

    Ranking is what resolves *aliases*. XOR- and sum-type checksums are
    symmetric -- if the whole message XORs to zero, then algebraically every
    nibble is a valid checksum of the others -- so several positions fit the
    data perfectly and only these preferences separate the real one.
    """
    score = 0.0
    score += 2.0 if solution.method == "algo" else 1.0
    score += 1.5 * _jumpiness(values, solution.length)
    if solution.end == stats.nbits:
        score += 0.75
    elif solution.start == 0:
        score += 0.25
    score += 0.5 * (solution.length / 8.0)
    if solution.crc_name:
        score += 0.5  # a positively identified polynomial is strong evidence
    if counter is not None and (
        solution.end == counter.start or solution.start == counter.end
    ):
        score += _ADJACENT_TO_COUNTER_BONUS
    return score


def _is_degenerate_affine(
    masks: list[int],
    n_cols: int,
    values: list[int],
    length: int,
    *,
    determined: bool,
) -> bool:
    """Reject GF(2) fits that are real relationships but not checksums.

    An affine solve will happily explain a byte that only ever takes two values
    and tracks a nearby flag — that *is* an affine function of the payload, just
    not an integrity check. The field ranging over most of its possible values
    is what rules that out, and it does so on its own.

    Requiring the mask to depend on a large fraction of the payload is a useful
    second check, but only when the capture determined the map uniquely. Under
    rank deficiency the solver returns a sparse representative of a whole family
    of valid maps, so a low popcount says something about the capture's variety
    rather than about the field — applying the test there would throw away
    genuine finds.
    """
    if not masks:
        return True
    if len(set(values)) < min(1 << length, 24):
        return True
    if determined:
        mean_popcount = sum(m.bit_count() for m in masks) / len(masks)
        if mean_popcount < 0.12 * n_cols:
            return True
    return False


def crack_checksum(
    stream: MessageStream,
    stats: BitStats,
    counter: CounterField | None = None,
    *,
    min_frames: int = 40,
) -> ChecksumSolution | None:
    """Find and explain the message's checksum, if it has one.

    Every candidate position is evaluated and the most plausible one returned,
    rather than the first that fits — because for sum-type checksums several
    positions fit equally well and only ranking picks the right one. See
    :func:`_plausibility`.
    """
    if len(stream) < min_frames or stats.nbits == 0:
        return None

    exclude: set[int] = set()
    if counter is not None:
        exclude = set(range(counter.start, counter.end))

    scored: list[tuple[float, ChecksumSolution]] = []

    for start, length in _candidate_positions(stats):
        if exclude & set(range(start, start + length)):
            continue  # a field cannot be both the counter and the checksum

        values = [extract_be(p, start, length) for p in stream.payloads]
        solution = _try_named_algos(stream, start, length)

        if solution is None:
            # Only spend a GF(2) solve where the field actually behaves like a
            # checksum. This is both a correctness gate and the main thing
            # keeping the analysis fast on a long capture.
            if len(set(values)) < min(1 << length, 24):
                continue
            if _jumpiness(values, length) < 0.3:
                continue
            solution = _affine_at(stream, stats, start, length, counter)
            if solution is None or not solution.is_confident:
                continue
            if _is_degenerate_affine(
                solution.masks,
                len(solution.input_bits),
                values,
                length,
                determined=not solution.underdetermined,
            ):
                continue

        scored.append((_plausibility(solution, values, stats, counter), solution))

    if not scored:
        return None
    scored.sort(key=lambda pair: (-pair[0], pair[1].start))
    return scored[0][1]


def _affine_at(
    stream: MessageStream,
    stats: BitStats,
    start: int,
    length: int,
    counter: CounterField | None,
) -> ChecksumSolution | None:
    """Attempt a GF(2) solve at one position, globally then per counter value."""
    indices = [b for b in range(stats.nbits) if not (start <= b < start + length)]
    if not indices:
        return None
    # A per-counter solve splits these rows 16 ways and each group needs to
    # outnumber the unknowns, so keep enough headroom for that.
    rows, targets = _input_rows(
        stream, start, length, indices, max_frames=_MAX_SOLVE_FRAMES
    )
    n_cols = len(indices)

    result = _try_affine(rows, targets, n_cols, length)
    if result is not None:
        masks, constants, accuracy, n = result
        if accuracy >= 0.999:
            solution = ChecksumSolution(
                start=start, length=length, method="affine", accuracy=accuracy,
                n_verified=n, masks=masks, constants=constants,
                input_bits=indices, rank=gf2_rank(rows), n_unknowns=n_cols,
            )
            named = _identify_crc(masks, constants, indices, stats.nbits, length)
            if named:
                solution.crc_name, solution.crc_params, solution.crc_framing = named
            return solution

    # No global affine fit. If the message has a counter, the additive term may
    # be keyed by it — VW MQB's magic-byte table works exactly that way.
    if counter is None or length != 8:
        return None
    return _affine_per_counter(stream, stats, start, length, counter)


def _affine_per_counter(
    stream: MessageStream,
    stats: BitStats,
    start: int,
    length: int,
    counter: CounterField,
) -> ChecksumSolution | None:
    """Solve for a shared linear part plus an additive term keyed by the counter.

    The counter value is one-hot encoded as extra columns and solved *jointly*
    with the payload bits, rather than running one solve per counter value. Two
    reasons. The linear part is then shared by construction, so it cannot
    disagree between counter values. And every frame contributes to pinning it
    down, instead of a sixteenth of them — which matters, because a message
    quiet enough to need this treatment rarely has entropy to spare.

    The counter's own bits are dropped from the payload columns: they are
    exactly what the one-hot columns encode, and leaving both in would let the
    solver split weight between them arbitrarily.
    """
    indices = [
        b
        for b in range(stats.nbits)
        if not (start <= b < start + length) and not (counter.start <= b < counter.end)
    ]
    n_payload = len(indices)
    n_values = 1 << counter.length
    n_cols = n_payload + n_values

    payload_rows, targets = _input_rows(
        stream, start, length, indices, max_frames=_MAX_SOLVE_FRAMES
    )
    counter_values = [
        extract_be(p, counter.start, counter.length) for p in stream.payloads
    ]
    if len(counter_values) > len(payload_rows):
        # `_input_rows` subsampled; take counter values the same way so the two
        # stay aligned frame for frame.
        step = len(counter_values) / len(payload_rows)
        counter_values = [
            counter_values[int(i * step)] for i in range(len(payload_rows))
        ]

    # Payload columns sit above the one-hot block, so a payload column keeps the
    # same index it had before augmentation once shifted back down.
    rows = [
        (row << n_values) | (1 << cv)
        for row, cv in zip(payload_rows, counter_values)
    ]

    result = _try_affine(rows, targets, n_cols, length)
    if result is None:
        return None
    full_masks, constants, accuracy, n_test = result
    if accuracy < 0.999:
        return None

    payload_mask = (1 << n_payload) - 1
    masks = [(m >> n_values) & payload_mask for m in full_masks]

    constants_by_counter: dict[int, int] = {}
    for cv in sorted(set(counter_values)):
        value = 0
        for j in range(length):
            bit = ((full_masks[j] >> cv) & 1) ^ constants[j]
            value = (value << 1) | bit
        constants_by_counter[cv] = value

    solution = ChecksumSolution(
        start=start,
        length=length,
        method="affine_per_counter",
        accuracy=accuracy,
        n_verified=n_test,
        masks=masks,
        constants=[0] * length,
        input_bits=indices,
        counter_constants=constants_by_counter,
        rank=gf2_rank(rows),
        n_unknowns=n_cols,
    )
    named = _identify_crc(
        masks, [0] * length, indices, stats.nbits, length, match_constant=False
    )
    if named:
        solution.crc_name, solution.crc_params, solution.crc_framing = named
    solution.note = (
        f"additive term selected by the {counter.length}-bit counter at bit "
        f"{counter.start}"
    )
    if solution.underdetermined:
        solution.note += (
            ". The capture does not vary this message enough to separate the "
            "linear part from the per-counter constants uniquely: the pair "
            "reproduces every frame seen, but the split is one of many. Record "
            "a drive that exercises this message harder to pin it down."
        )
    return solution
