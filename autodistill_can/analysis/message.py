"""Per-message orchestration: run the passes in the order that makes each easier.

Order matters a great deal here. A counter and a checksum are the two fields
whose identity can be *proved* rather than inferred — a counter increments, a
checksum reproduces exactly — so they are found first and carved out. Whatever
remains is then segmented into signals without those two fields dragging
neighbouring boundaries around. Multiplexing is checked first of all, since it
changes what "the payload" even means.
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable

from ..frame import CanLog, MessageStream
from .bitstats import BitStats, bit_stats
from .checksum import ChecksumSolution, crack_checksum
from .counter import CounterField, find_counter
from .multiplex import MultiplexInfo, find_multiplexer
from .signals import (
    Signal,
    SignalConfig,
    extract_signals,
    orientation_scores,
)

__all__ = ["MessageAnalysis", "analyse_log", "analyse_message", "detect_byte_order"]


@dataclass
class MessageAnalysis:
    """Everything recovered about one message."""

    bus: int
    addr: int
    length: int
    n_frames: int
    period: float | None
    frequency: float | None
    jitter: float | None
    stats: BitStats
    signals: list[Signal] = field(default_factory=list)
    counter: CounterField | None = None
    checksum: ChecksumSolution | None = None
    multiplex: MultiplexInfo | None = None
    #: Signals per multiplex value, when the message is multiplexed.
    mux_signals: dict[int, list[Signal]] = field(default_factory=dict)
    #: Observations worth telling the user about.
    notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, int]:
        return (self.bus, self.addr)

    @property
    def is_event_driven(self) -> bool:
        """Whether the message fires irregularly rather than on a fixed cycle.

        Judged by jitter relative to the period. Cyclic ECU messages hold their
        cadence to well under a millisecond; an event message fires when
        something happens and has no cadence at all.
        """
        if self.jitter is None or not self.period:
            return False
        return self.jitter > 0.25 * self.period

    @property
    def name(self) -> str:
        return f"MSG_{self.addr:03X}"

    def signal_at(self, start: int) -> Signal | None:
        for signal in self.signals:
            if signal.start == start:
                return signal
        return None

    def named_signals(self) -> list[Signal]:
        """Signals that correlation managed to identify."""
        return [s for s in self.signals if s.name]


#: How decisively the pooled evidence must favour one byte order before it is
#: imposed on every message. Real single-order cars clear this comfortably
#: (1.5-40x across the captures tested); a capture that genuinely mixes the two
#: sits near 1.3 and is better left to per-message judgement. Calibrated on a
#: handful of cars, so it is a threshold to revisit rather than a constant of
#: nature.
_BYTE_ORDER_MARGIN = 1.4


def detect_byte_order(
    streams: Iterable[MessageStream],
    *,
    signal_config: SignalConfig | None = None,
    min_frames: int = 40,
    margin: float = _BYTE_ORDER_MARGIN,
) -> bool | None:
    """Decide the byte order for a whole capture. True means Intel.

    Byte order is a property of the car, not of one message: a manufacturer
    picks Motorola or Intel for a platform and every ECU on the bus follows it.
    Every signal in opendbc's Hyundai database is Intel; Toyota's are Motorola.

    Deciding per message throws that away, and most messages cannot decide for
    themselves — a message built from sub-byte flags reads identically either
    way, so it has no evidence and falls back to a default. Pooling the evidence
    lets the handful of messages with wide fields settle it for all of them.

    Returns None unless the pooled evidence favours one order by at least
    ``margin``, leaving each message to decide for itself otherwise. The
    threshold matters: imposing a car-wide order on a capture that really does
    mix them is worse than deciding message by message.
    """
    motorola = intel = 0.0
    for stream in streams:
        if len(stream) < min_frames or stream.nbits == 0:
            continue
        stats = bit_stats(stream)
        counter = find_counter(stream, stats)
        checksum = crack_checksum(stream, stats, counter)
        exclude: list[tuple[int, int]] = []
        if counter is not None:
            exclude.append((counter.start, counter.length))
        if checksum is not None:
            exclude.append((checksum.start, checksum.length))
        m, i = orientation_scores(
            stream, stats, exclude=exclude, config=signal_config
        )
        motorola += m
        intel += i
    low, high = min(motorola, intel), max(motorola, intel)
    if high <= 0:
        return None
    if low > 0 and high / low < margin:
        return None
    return intel > motorola


def _analyse_stream_worker(args: tuple) -> MessageAnalysis:
    stream, signal_config, detect_multiplex, min_frames, lsb_first = args
    return analyse_message(
        stream,
        signal_config=signal_config,
        detect_multiplex=detect_multiplex,
        min_frames=min_frames,
        lsb_first=lsb_first,
    )


def _can_fork_workers() -> bool:
    """Whether worker processes can re-import ``__main__``.

    ``python - <<EOF`` leaves ``__main__.__file__`` set to the literal string
    ``"<stdin>"``, which no worker can import. The pool then dies mid-start and
    prints a traceback from the child even though the parent recovers -- which
    looks exactly like a crash to whoever ran the script.

    ``python -c`` sets no ``__file__`` at all and is handled fine, so the test
    is specifically "claims a file that is not there".
    """
    path = getattr(sys.modules.get("__main__"), "__file__", None)
    return path is None or os.path.isfile(path)


def analyse_log(
    log: "CanLog",
    *,
    signal_config: SignalConfig | None = None,
    detect_multiplex: bool = True,
    min_frames: int = 40,
    lsb_first: bool | None = None,
    workers: int | None = None,
) -> list[MessageAnalysis]:
    """Analyse every message in a capture, sharing what can be shared.

    The one thing worth deciding globally is byte order; see
    :func:`detect_byte_order`. Pass ``lsb_first`` to override the detection.
    Pass ``workers`` to control process concurrency (defaults to CPU count).

    Per-message analysis runs in worker processes, which means a script that
    calls this **must** guard its entry point::

        if __name__ == "__main__":
            analyse_log(log)

    Without it, each worker re-imports the calling module and runs its
    top-level code again -- so the script appears to run several times over.
    That is ordinary ``multiprocessing`` behaviour rather than anything
    specific here, but it is invisible until it happens. ``workers=1`` avoids
    processes entirely if that is easier for a caller to arrange.
    """
    streams = log.sorted_streams()
    if not streams:
        return []
    if lsb_first is None:
        lsb_first = detect_byte_order(
            streams, signal_config=signal_config, min_frames=min_frames
        )

    max_w = workers if workers is not None else (os.cpu_count() or 1)
    if max_w > 1 and len(streams) > 1 and _can_fork_workers():
        tasks = [
            (stream, signal_config, detect_multiplex, min_frames, lsb_first)
            for stream in streams
        ]
        try:
            with ProcessPoolExecutor(
                max_workers=min(max_w, len(streams))
            ) as executor:
                return list(executor.map(_analyse_stream_worker, tasks))
        except (OSError, EOFError, RuntimeError, ImportError):
            # Worker processes cannot always be started: a `python - <<EOF`
            # heredoc leaves `__main__` at the unimportable path "<stdin>",
            # and frozen or embedded interpreters have their own limits. None
            # of that is a reason to fail an analysis -- the sequential path
            # below computes the same answer, only slower.
            pass

    return [
        analyse_message(
            stream,
            signal_config=signal_config,
            detect_multiplex=detect_multiplex,
            min_frames=min_frames,
            lsb_first=lsb_first,
        )
        for stream in streams
    ]


def analyse_message(
    stream: MessageStream,
    *,
    signal_config: SignalConfig | None = None,
    detect_multiplex: bool = True,
    min_frames: int = 40,
    lsb_first: bool | None = None,
) -> MessageAnalysis:
    """Run every per-message pass and collect the results.

    ``lsb_first`` forces a byte order; left as None the message decides for
    itself, which is weaker than letting :func:`analyse_log` decide for the car.
    """
    stats = bit_stats(stream)
    analysis = MessageAnalysis(
        bus=stream.bus,
        addr=stream.addr,
        length=stream.length,
        n_frames=len(stream),
        period=stream.period,
        frequency=stream.frequency,
        jitter=stream.jitter(),
        stats=stats,
    )

    if stream.length_varies:
        analysis.notes.append(
            "payload length varies between frames; analysis uses the shortest "
            f"({stream.length} bytes) so bit positions stay comparable"
        )

    if stats.nbits == 0 or not stream.payloads:
        return analysis

    if len(stream) < min_frames:
        analysis.notes.append(
            f"only {len(stream)} frames captured; too few to analyse reliably"
        )
        return analysis

    # Provable structure first, so it cannot distort signal boundaries.
    analysis.counter = find_counter(stream, stats)
    analysis.checksum = crack_checksum(stream, stats, analysis.counter)

    exclude: list[tuple[int, int]] = []
    if analysis.counter is not None:
        exclude.append((analysis.counter.start, analysis.counter.length))
    if analysis.checksum is not None:
        exclude.append((analysis.checksum.start, analysis.checksum.length))

    if detect_multiplex:
        analysis.multiplex = find_multiplexer(stream, stats, exclude=exclude)

    if analysis.multiplex is not None:
        mux = analysis.multiplex
        exclude.append((mux.start, mux.length))
        analysis.notes.append(
            f"multiplexed: the {mux.length}-bit field at bit {mux.start} selects "
            f"between {len(mux.groups)} payload layouts. Signals are reported "
            f"per mode; a signal list ignoring the mode would be meaningless."
        )
        for value, indices in sorted(mux.groups.items()):
            subset = MessageStream(
                bus=stream.bus,
                addr=stream.addr,
                times=[stream.times[i] for i in indices],
                payloads=[stream.payloads[i] for i in indices],
            )
            analysis.mux_signals[value] = extract_signals(
                subset, bit_stats(subset), exclude=exclude,
                config=signal_config, lsb_first=lsb_first,
            )

    analysis.signals = extract_signals(
        stream, stats, exclude=exclude, config=signal_config, lsb_first=lsb_first
    )

    # Splice the proved fields back in so the layout is complete and ordered.
    if analysis.multiplex is not None:
        analysis.signals.append(
            Signal(
                start=analysis.multiplex.start,
                length=analysis.multiplex.length,
                kind="mux",
                confidence=analysis.multiplex.gain,
                raw_min=min(analysis.multiplex.groups),
                raw_max=max(analysis.multiplex.groups),
                distinct=len(analysis.multiplex.groups),
                name="MUX_MODE",
            )
        )
    if analysis.counter is not None:
        analysis.signals.append(
            Signal(
                start=analysis.counter.start,
                length=analysis.counter.length,
                kind="counter",
                confidence=analysis.counter.score,
                raw_max=(1 << analysis.counter.length) - 1,
                distinct=len({analysis.counter.step}),
                name="COUNTER",
            )
        )
    if analysis.checksum is not None:
        analysis.signals.append(
            Signal(
                start=analysis.checksum.start,
                length=analysis.checksum.length,
                kind="checksum",
                confidence=analysis.checksum.accuracy,
                raw_max=(1 << analysis.checksum.length) - 1,
                name="CHECKSUM",
                notes=[analysis.checksum.describe()],
            )
        )
    analysis.signals.sort(key=lambda s: s.start)

    if analysis.is_event_driven:
        analysis.notes.append(
            "fires irregularly rather than on a fixed cycle, so it is probably "
            "event-driven; rate figures for it mean little"
        )

    static = sum(1 for s in analysis.signals if s.kind == "constant")
    if static and stats.nbits:
        static_bits = sum(
            s.length for s in analysis.signals if s.kind == "constant"
        )
        if static_bits > stats.nbits * 0.5:
            analysis.notes.append(
                f"{static_bits} of {stats.nbits} bits never changed during the "
                "capture. Signals hiding in them cannot be found without a "
                "drive that exercises whatever they report."
            )

    return analysis
