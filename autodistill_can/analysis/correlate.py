"""Naming signals by correlating them against a reference measurement.

Everything else in this package works out a message's *structure* — where the
fields are and how they are encoded — without knowing what any of it means.
This is the step that attaches meaning, and it is the one that makes a port
possible: openpilot needs to know which field is wheel speed, not merely that
some 16-bit big-endian field exists at bit 0.

The method is to bring an independent measurement of a few physical quantities
and see which field tracks it. Useful references, roughly in order of how easy
they are to get:

* a phone or dashcam GPS track (speed, heading)
* an OBD-II dongle polling standard PIDs (speed, RPM, throttle, coolant)
* openpilot's own logs from an already-supported car
* a hand-written event log ("t=12.4 I pressed the brake")

Correlation also recovers the **scale and offset**, by least-squares fitting the
raw field against the reference. A field reading 6234 when GPS says 62.34 km/h
gives a scale of 0.01 — which is exactly what a DBC needs and what nobody wants
to work out by hand.

Derivatives of each reference channel are correlated too, so a steering *rate*
signal is identified from a steering *angle* reference.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from pathlib import Path

from ..frame import MessageStream, resample
from ..sources.compressed import open_maybe_compressed
from .message import MessageAnalysis
from .signals import Signal

__all__ = [
    "Match",
    "ReferenceSeries",
    "correlate_log",
    "load_reference_csv",
]


@dataclass
class ReferenceSeries:
    """Independently measured channels, on their own timebase."""

    times: list[float]
    channels: dict[str, list[float]] = field(default_factory=dict)
    #: Units per channel, carried through into the emitted DBC.
    units: dict[str, str] = field(default_factory=dict)
    #: Which samples of a channel were actually measured. Absent means "all of
    #: them", which is the case for a logger that writes every column on every
    #: row. Real logs are gappier than that -- Torque Pro leaves GPS columns
    #: blank until the phone has a fix, so the first minute of a cold-start
    #: drive has none -- and a gap has to be filled to keep every channel the
    #: same length as ``times``. Correlation drops the filled samples rather
    #: than fitting against numbers nothing measured.
    present: dict[str, list[bool]] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.times)

    def measured(self, channel: str) -> list[bool]:
        return self.present.get(channel) or [True] * len(self.times)

    def with_derivatives(self) -> "ReferenceSeries":
        """A copy with a ``d(x)/dt`` channel added for each existing channel.

        Cars broadcast rates as often as they broadcast the quantity itself —
        steering angle *and* steering rate, wheel position *and* wheel speed —
        and a rate signal correlates with nothing in the reference until its
        derivative is available to compare against.
        """
        out = ReferenceSeries(times=list(self.times), channels=dict(self.channels),
                              units=dict(self.units), present=dict(self.present))
        for name, values in self.channels.items():
            measured = self.measured(name)
            derivative: list[float] = []
            valid: list[bool] = []
            for i in range(len(values)):
                lo = max(0, i - 1)
                hi = min(len(values) - 1, i + 1)
                dt = self.times[hi] - self.times[lo]
                derivative.append((values[hi] - values[lo]) / dt if dt > 0 else 0.0)
                # A difference is only as trustworthy as both of its ends.
                valid.append(measured[lo] and measured[hi])
            out.channels[f"d({name})/dt"] = derivative
            if not all(valid):
                out.present[f"d({name})/dt"] = valid
            unit = self.units.get(name, "")
            out.units[f"d({name})/dt"] = f"{unit}/s" if unit else ""
        return out


@dataclass
class Match:
    """One signal identified against one reference channel."""

    bus: int
    addr: int
    signal: Signal
    channel: str
    #: Pearson correlation of the raw field against the reference.
    r: float
    #: Least-squares fit mapping raw field value to reference units.
    scale: float
    offset: float
    #: Residual standard deviation in reference units, after the fit.
    residual: float
    n_points: int

    @property
    def quality(self) -> str:
        if abs(self.r) >= 0.995:
            return "excellent"
        if abs(self.r) >= 0.97:
            return "good"
        return "weak"

    def __str__(self) -> str:
        sign = "-" if self.r < 0 else "+"
        return (
            f"bus{self.bus} 0x{self.addr:03X} [{self.signal.start}:"
            f"{self.signal.end}] ~ {self.channel} r={sign}{abs(self.r):.4f} "
            f"scale={self.scale:.6g} offset={self.offset:.6g}"
        )


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = syy = sxy = 0.0
    for x, y in zip(xs, ys):
        dx = x - mean_x
        dy = y - mean_y
        sxx += dx * dx
        syy += dy * dy
        sxy += dx * dy
    if sxx <= 0.0 or syy <= 0.0:
        return 0.0
    return sxy / math.sqrt(sxx * syy)


def _fit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Least-squares ``y = scale * x + offset``; returns residual std too."""
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        return 0.0, mean_y, 0.0
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    scale = sxy / sxx
    offset = mean_y - scale * mean_x
    residual = math.sqrt(
        sum((y - (scale * x + offset)) ** 2 for x, y in zip(xs, ys)) / n
    )
    return scale, offset, residual


#: Scale factors OEMs actually choose. A fit lands *near* one of these, never
#: exactly on one, because the reference measurement carries noise of its own.
_COMMON_SCALES: tuple[float, ...] = (
    1.0, 0.5, 0.25, 0.2, 0.125, 0.1, 0.05, 0.025, 0.02, 0.01, 0.005,
    0.002, 0.001, 1 / 256, 1 / 128, 1 / 64, 1 / 32, 1 / 16, 1 / 8,
    2.0, 3.6, 4.0, 5.0, 8.0, 10.0, 100.0,
)

#: How far a fitted scale may sit from a common value and still be snapped to
#: it. A GPS track is good to roughly a percent, so a tighter bound would refuse
#: to snap the very cases worth snapping, and a looser one would start rewriting
#: scales that are genuinely unusual.
_SCALE_SNAP_TOLERANCE = 0.015


def _round_scale(scale: float) -> float:
    """Snap a fitted scale to a clean value when it is within reference noise.

    Reporting a scale of 0.00995296 is no more accurate than reporting 0.01 —
    the extra digits are GPS noise, not information — and it makes the emitted
    DBC look like it was measured rather than understood. The *nearest* common
    value wins, so neighbouring candidates like 0.02 and 0.025 cannot capture
    each other's fits.
    """
    if scale == 0:
        return 0.0
    magnitude = abs(scale)
    best = min(_COMMON_SCALES, key=lambda c: abs(magnitude - c) / c)
    if abs(magnitude - best) / best <= _SCALE_SNAP_TOLERANCE:
        return math.copysign(best, scale)
    return scale


def correlate_signal(
    stream: MessageStream,
    signal: Signal,
    reference: ReferenceSeries,
    *,
    min_abs_r: float = 0.9,
) -> list[Match]:
    """Correlate one signal against every reference channel."""
    if signal.kind in ("checksum", "counter", "constant", "unknown"):
        return []
    raw = signal.values(stream)
    if len(set(raw)) < 3:
        return []

    # Hold the CAN signal onto the reference's timebase. The reference is
    # usually much slower (a 10 Hz GPS against a 100 Hz message), so this
    # decimates rather than interpolates, and a zero-order hold is what a real
    # signal does between frames anyway.
    sampled = resample(stream.times, [float(v) for v in raw], reference.times)

    out: list[Match] = []
    for channel, values in reference.channels.items():
        measured = reference.measured(channel)
        if all(measured):
            xs, ys = sampled, values
        else:
            # Fitting a CAN signal against a filled-in gap would be fitting it
            # against this loader's interpolation, not against the car.
            pairs = [(x, y) for x, y, ok in zip(sampled, values, measured) if ok]
            if len(pairs) < _MIN_SAMPLES:
                continue
            xs = [x for x, _ in pairs]
            ys = [y for _, y in pairs]
        r = _pearson(xs, ys)
        if abs(r) < min_abs_r:
            continue
        scale, offset, residual = _fit(xs, ys)
        out.append(
            Match(
                bus=stream.bus,
                addr=stream.addr,
                signal=signal,
                channel=channel,
                r=r,
                scale=_round_scale(scale),
                offset=offset,
                residual=residual,
                n_points=len(ys),
            )
        )
    return out


def correlate_log(
    streams: dict[tuple[int, int], MessageStream],
    analyses: list[MessageAnalysis],
    reference: ReferenceSeries,
    *,
    min_abs_r: float = 0.9,
    include_derivatives: bool = True,
    assign_names: bool = True,
) -> list[Match]:
    """Correlate every recovered signal against the reference.

    Returns matches sorted strongest first. When ``assign_names`` is set, the
    best match for each reference channel writes its name, unit, scale and
    offset back onto the :class:`~autodistill_can.analysis.signals.Signal`, so the
    emitted DBC carries real names in real units.

    Only the best signal per channel is named. A car broadcasts road speed in
    several places at once — four wheel speeds, a dash reading, a cruise-control
    copy — and they all correlate nearly perfectly, so naming every one of them
    ``SPEED`` would be worse than useless. The rest are still returned for
    inspection.
    """
    if include_derivatives:
        reference = reference.with_derivatives()

    matches: list[Match] = []
    for analysis in analyses:
        stream = streams.get(analysis.key)
        if stream is None:
            continue
        for signal in analysis.signals:
            matches.extend(
                correlate_signal(stream, signal, reference, min_abs_r=min_abs_r)
            )

    matches.sort(key=lambda m: -abs(m.r))

    if assign_names:
        claimed: set[str] = set()
        for match in matches:
            if match.channel in claimed or match.signal.name:
                continue
            claimed.add(match.channel)
            signal = match.signal
            signal.name = _identifier(match.channel)
            signal.unit = reference.units.get(match.channel, "")
            signal.scale = match.scale
            signal.offset = match.offset
            signal.notes.append(
                f"named by correlation with {match.channel!r} (r={match.r:.4f})"
            )

    return matches


def _identifier(channel: str) -> str:
    """Turn a reference channel name into a DBC-safe signal name."""
    out = []
    for ch in channel:
        out.append(ch if ch.isalnum() else "_")
    name = "".join(out).strip("_").upper()
    while "__" in name:
        name = name.replace("__", "_")
    if not name or name[0].isdigit():
        name = f"REF_{name}"
    return name



#: Fewest measured samples worth fitting a channel against. Below this a high
#: correlation is chance as often as it is a match.
_MIN_SAMPLES = 12

#: Columns that hold elapsed seconds, matched exactly.
_TIME_NAMES = {"time", "timestamp", "t", "ts", "seconds", "sec", "secs"}

#: Columns that hold a wall clock instead, in the order we would rather have
#: them. Torque Pro writes both: "GPS Time" is blank until the phone has a fix,
#: which on a cold start is the first minute of the drive, while "Device Time"
#: is written for every row -- so the device clock is the better timebase even
#: though the GPS one is the more accurate.
_CLOCK_NAMES = (
    "device time", "gps time", "datetime", "date/time", "date time", "utc time",
)

#: What a logger writes where it has no reading. Torque Pro uses "-", which is
#: why every row of one of its logs used to be discarded: the old loader took
#: any unparseable cell as the end of the usable row.
_MISSING = {"", "-", "--", "n/a", "na", "nan", "null", "none", "?"}

#: Wall-clock layouts seen in the wild. Torque Pro's comes first.
_TIME_FORMATS = (
    "%d-%b-%Y %H:%M:%S.%f",
    "%d-%b-%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M:%S.%f",
    "%d/%m/%Y %H:%M:%S",
)


def _parse_time(cell: str) -> float | None:
    """Seconds, from either elapsed seconds or a wall clock.

    A wall clock becomes epoch seconds in local time, which is the same
    timebase a candump capture carries, so the two line up without the user
    converting anything -- provided both devices agree on the clock.
    """
    try:
        return float(cell)
    except ValueError:
        pass
    from datetime import datetime

    try:
        return datetime.fromisoformat(cell).timestamp()
    except ValueError:
        pass
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(cell, fmt).timestamp()
        except ValueError:
            continue
    return None


def _column_names(header: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split each header into a name and a unit, keeping names distinct.

    A trailing bracket is read as a unit, so "Speed (OBD)(km/h)" is the channel
    "Speed (OBD)" measured in km/h. That rule alone collapses Torque Pro's
    "G(x)", "G(y)", "G(z)" and "G(calibrated)" onto one channel called "G",
    three of which then overwrite the other -- so where stripping the bracket
    creates a collision, the full header is kept instead.
    """
    import re

    stripped: list[tuple[str, str]] = []
    for raw_name in header:
        m = re.match(r"^\s*(.*?)\s*[\[(]([^\])]*)[\])]\s*$", raw_name)
        if m:
            stripped.append((m.group(1).strip(), m.group(2).strip()))
        else:
            stripped.append((raw_name.strip(), ""))

    counts: dict[str, int] = {}
    for name, _ in stripped:
        counts[name] = counts.get(name, 0) + 1

    names: list[str] = []
    units: dict[str, str] = {}
    for (name, unit), raw_name in zip(stripped, header):
        if counts[name] > 1 or not name:
            names.append(raw_name.strip())
        else:
            names.append(name)
            if unit:
                units[name] = unit
    return names, units


def _fill_gaps(values: list[float | None]) -> list[float]:
    """Make a gappy channel dense, so every channel is len(times) long.

    Interior gaps are interpolated and the ends are held. None of these filled
    samples reach a correlation -- ``ReferenceSeries.present`` marks them and
    they are dropped before fitting -- they exist so the structure stays
    rectangular for everything that reads it.
    """
    known = [i for i, v in enumerate(values) if v is not None]
    if not known:
        return [0.0] * len(values)
    out: list[float] = []
    for i, value in enumerate(values):
        if value is not None:
            out.append(value)
            continue
        before = [k for k in known if k < i]
        after = [k for k in known if k > i]
        if before and after:
            lo, hi = before[-1], after[0]
            a, b = values[lo], values[hi]
            span = hi - lo
            out.append(a + (b - a) * ((i - lo) / span))
        elif before:
            out.append(values[before[-1]])
        else:
            out.append(values[after[0]])
    return out


def load_reference_csv(
    path: Path | str, *, time_column: str | None = None
) -> ReferenceSeries:
    """Load a reference log from CSV.

    The time column is detected by name unless given: either elapsed seconds
    (``time``, ``timestamp``, ``t``, ...) or a wall clock (``Device Time``,
    ``GPS Time``, ...), which is converted to epoch seconds. Every other
    numeric column becomes a channel. A ``unit`` hint may be appended to a
    header in brackets or parentheses, as in ``speed [km/h]``,
    ``steer_angle (deg)``, or Torque Pro's ``Speed (OBD)(km/h)``.

    Cells a logger leaves blank or marks "-" are gaps in that one channel, not
    the end of the row. They are filled so every channel stays the same length
    as ``times``, and recorded in ``present`` so correlation can drop them.
    """
    import csv

    path = Path(path)

    # A reference log is as large as the capture it accompanies and gets
    # compressed just as often, so it is opened the same way.
    with io.TextIOWrapper(
        open_maybe_compressed(path), newline="", errors="replace"
    ) as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"{path}: empty reference file")

        names, units = _column_names(header)
        lowered = [n.strip().lower() for n in names]

        if time_column is not None:
            if time_column not in names:
                raise ValueError(
                    f"{path}: no column named {time_column!r}; have {names!r}"
                )
            i_time = names.index(time_column)
        else:
            i_time = next(
                (i for i, n in enumerate(lowered) if n in _TIME_NAMES), -1
            )
            for clock in _CLOCK_NAMES:
                if i_time >= 0:
                    break
                i_time = next((i for i, n in enumerate(lowered) if n == clock), -1)
            if i_time < 0:
                raise ValueError(
                    f"{path}: no time column found in {names!r}; "
                    "pass one explicitly with --reference-time"
                )

        times: list[float] = []
        columns: dict[str, list[float | None]] = {
            n: [] for i, n in enumerate(names) if i != i_time
        }
        for row in reader:
            if len(row) <= i_time:
                continue
            t = _parse_time(row[i_time].strip())
            if t is None:
                continue
            times.append(t)
            for i, name in enumerate(names):
                if i == i_time:
                    continue
                cell = row[i].strip() if i < len(row) else ""
                if cell.lower() in _MISSING:
                    columns[name].append(None)
                    continue
                try:
                    columns[name].append(float(cell))
                except ValueError:
                    columns[name].append(None)

    if not times:
        raise ValueError(f"{path}: no usable rows")

    # A channel earns its place by having been measured often enough to fit
    # against, and by varying -- a constant correlates with everything and
    # nothing.
    channels: dict[str, list[float]] = {}
    present: dict[str, list[bool]] = {}
    for name, values in columns.items():
        measured = [v is not None for v in values]
        seen = {v for v in values if v is not None}
        if len(seen) <= 2:
            continue
        # The sample floor is about gaps, not about short files: a channel the
        # logger wrote on every row is as good as the log is long, but one it
        # managed 23 times in 181 rows has little to fit against and a high
        # correlation from it is as likely to be chance.
        if not all(measured) and sum(measured) < _MIN_SAMPLES:
            continue
        channels[name] = _fill_gaps(values)
        if not all(measured):
            present[name] = measured

    if not channels:
        raise ValueError(f"{path}: no numeric channels that vary over time")

    return ReferenceSeries(
        times=times,
        channels=channels,
        units={k: v for k, v in units.items() if k in channels},
        present=present,
    )
