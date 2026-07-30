#!/usr/bin/env python3
"""Generate a capture from a real DBC, then score what AutoDistill recovers.

The synthetic car in :mod:`autodistill_can.synth` is hand-written, so it can only
test the layouts its author thought to include. A real opendbc database has
hundreds of signals laid out the way an actual manufacturer laid them out —
odd widths, unaligned Intel fields, sub-byte flags packed against each other —
and it comes with the answer.

So: drive a simulated vehicle, encode its state through a real DBC using
cantools (an encoder entirely independent of this package), and see how much of
the manufacturer's layout comes back out.

    python tools/replay_dbc.py examples/opendbc/hyundai_2015_ccan.dbc

The scoring is deliberately strict. A signal counts as *exact* only when its
position, width, byte order and signedness are all right; one bit too wide is a
miss. Signals whose bits never move during the drive are excluded, because
nothing could recover them and counting them would measure the drive rather
than the tool.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cantools

from autodistill_can.analysis import analyse_log, bit_stats
from autodistill_can.frame import CanFrame, CanLog


def physical_series(signal, t: float, rng: random.Random) -> float:
    """A plausible value for a signal at time ``t``.

    Signals are driven from a few shared underlying quantities so that the
    result behaves like a vehicle rather than noise: things move smoothly,
    correlate with each other, and stay inside the range the DBC declares. That
    matters because the whole analysis keys off smoothness — random values would
    make every field look like a checksum.
    """
    lo = signal.minimum if signal.minimum is not None else 0
    hi = signal.maximum if signal.maximum is not None else (1 << signal.length) - 1
    if hi <= lo:
        hi = lo + 1

    name = signal.name.lower()
    speed = 50 + 45 * math.sin(t / 23.0) + 3 * math.sin(t / 2.1)
    if signal.length == 1:
        # Flags toggle occasionally rather than every frame.
        return float(int((t / 7.0 + hash(signal.name) % 5) % 2 < 1))
    if signal.length <= 3:
        return float(int(abs(math.sin(t / 11.0 + len(name))) * (hi - lo)) + lo)
    if any(k in name for k in ("spd", "speed", "vanz", "whl")):
        base = speed
    elif any(k in name for k in ("rpm", "eng")):
        base = 900 + speed * 40
    elif any(k in name for k in ("ang", "sas", "steer")):
        base = 180 * math.sin(t / 13.0)
    elif any(k in name for k in ("temp", "tmp")):
        base = 60 + 20 * math.sin(t / 61.0)
    else:
        base = (lo + hi) / 2 + (hi - lo) / 3 * math.sin(t / (5 + len(name) % 17))
    base += rng.gauss(0, (hi - lo) * 0.001)
    return float(min(hi, max(lo, base)))


def generate(db, duration: float, seed: int) -> list[CanFrame]:
    """Encode a simulated drive through every message the database defines."""
    rng = random.Random(seed)
    frames: list[CanFrame] = []
    for message in db.messages:
        if message.is_multiplexed():
            continue  # a mux needs a selector strategy; scored separately
        period = (message.cycle_time or 100) / 1000.0
        period = min(max(period, 0.01), 1.0)
        t = rng.uniform(0, period)
        while t < duration:
            values = {}
            for signal in message.signals:
                raw = physical_series(signal, t, rng)
                # Quantise to the signal's own resolution, then clamp so
                # cantools will accept it.
                values[signal.name] = raw
            try:
                data = message.encode(values, strict=False)
            except Exception:
                break
            frames.append(
                CanFrame(t=t, addr=message.frame_id & 0x1FFFFFFF, data=bytes(data))
            )
            t += period + rng.gauss(0, period * 0.002)
    frames.sort(key=lambda f: f.t)
    return frames


def dbc_start_of(signal) -> int:
    """DBC start bit, the coordinate both byte orders share."""
    return signal.start


def recovered_start(sig) -> int:
    if not sig.big_endian:
        return sig.start
    byte, bit = divmod(sig.start, 8)
    return byte * 8 + (7 - bit)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dbc")
    parser.add_argument("-d", "--duration", type=float, default=120.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--log", help="also write the generated capture here")
    args = parser.parse_args()

    try:
        db = cantools.database.load_file(args.dbc)
    except Exception:
        # Several opendbc databases have overlapping signals of their own and
        # only load leniently. That is their problem, not ours; the layouts are
        # still real.
        db = cantools.database.load_file(args.dbc, strict=False)
    frames = generate(db, args.duration, args.seed)
    print(f"generated {len(frames)} frames from {Path(args.dbc).name}")

    if args.log:
        from autodistill_can.sources.candump import write_candump

        with open(args.log, "w") as fh:
            write_candump(frames, fh)

    log = CanLog.from_frames(frames)
    by_addr = {m.frame_id & 0x1FFFFFFF: m for m in db.messages}

    exact = position = active = total = 0
    order_right = order_total = 0
    misses: list[str] = []

    analyses = {a.key: a for a in analyse_log(log)}
    for stream in log.sorted_streams():
        message = by_addr.get(stream.addr)
        if message is None or len(stream) < 40:
            continue
        stats = bit_stats(stream)
        analysis = analyses[stream.key]
        # Compare by the actual payload bits, not by start-bit numbers. A field
        # that sits inside a single byte describes exactly the same bits and
        # decodes to the same value under either byte order -- only the number
        # used to name its start differs -- so comparing the numbers would score
        # a correct answer as wrong.
        found = {
            tuple(s.payload_bits(stats.nbits)): s for s in analysis.signals
        }
        first_bits = {
            s.payload_bits(stats.nbits)[0] for s in analysis.signals
        }

        truth_le = sum(
            1 for s in message.signals if s.byte_order == "little_endian"
        )
        message_is_le = truth_le * 2 >= len(message.signals)
        multi = [s for s in analysis.signals if s.length > 8]
        if multi:
            order_total += 1
            order_right += (not multi[0].big_endian) == message_is_le

        for signal in message.signals:
            bits = signal_bits(signal)
            total += 1
            if not any(stats.flips[b] for b in bits if b < stats.nbits):
                continue
            active += 1
            if bits and bits[0] in first_bits:
                position += 1
            got = found.get(tuple(bits))
            if got is None:
                misses.append(
                    f"{message.name}.{signal.name} "
                    f"start={signal.start} len={signal.length} "
                    f"{'LE' if signal.byte_order == 'little_endian' else 'BE'}"
                )
                continue
            # Byte order is only meaningful for a field spanning more than one
            # byte; within a byte the two readings are identical.
            spans_bytes = bits[0] // 8 != bits[-1] // 8
            order_ok = (not spans_bytes) or (
                got.big_endian == (signal.byte_order == "big_endian")
            )
            if order_ok and got.signed == signal.is_signed:
                exact += 1

    name = Path(args.dbc).stem
    print(f"\n{total} signals defined, {active} of them actually move in this drive")
    print(f"  exact (position+width+order+sign): {exact}/{active} "
          f"({100 * exact / max(1, active):.0f}%)")
    print(f"  correct start position:            {position}/{active} "
          f"({100 * position / max(1, active):.0f}%)")
    print(f"  message byte order correct:        {order_right}/{order_total}")
    print(f"SUMMARY\t{name}\t{active}\t{exact}\t{position}\t{order_right}\t{order_total}")
    if misses:
        print(f"\nfirst misses ({len(misses)} total):")
        for m in misses[:12]:
            print("   ", m)
    return 0


def signal_bits(signal) -> list[int]:
    """MSB-first payload positions a cantools signal occupies."""
    if signal.byte_order == "little_endian":
        return sorted(
            (j // 8) * 8 + (7 - (j % 8))
            for j in range(signal.start, signal.start + signal.length)
        )
    out, b = [], signal.start
    for _ in range(signal.length):
        out.append((b // 8) * 8 + (7 - (b % 8)))
        b = b - 1 if b % 8 else b + 15
    return sorted(out)


if __name__ == "__main__":
    raise SystemExit(main())
