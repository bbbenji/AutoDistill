"""Naming signals from a reference measurement."""

from __future__ import annotations

import pytest

from autodistill_can.analysis.correlate import (
    ReferenceSeries,
    correlate_log,
    load_reference_csv,
)


def test_correlation_identifies_steering_angle_with_its_true_scale(
    log, analyses, reference
):
    matches = correlate_log(
        log.streams, list(analyses.values()), reference, assign_names=False
    )
    angle = next(
        m for m in matches
        if m.addr == 0x025 and m.signal.start == 0 and m.channel == "steer_angle_deg"
    )
    assert abs(angle.r) > 0.999
    # The synthetic car encodes steering angle at 0.1 deg per bit.
    assert angle.scale == pytest.approx(0.1, rel=1e-6)
    assert angle.offset == pytest.approx(0.0, abs=0.5)


def test_correlation_identifies_wheel_speeds_with_their_true_scale(
    log, analyses, reference
):
    matches = correlate_log(
        log.streams, list(analyses.values()), reference, assign_names=False
    )
    wheels = [
        m for m in matches if m.addr == 0x0AA and m.channel == "speed_kph"
    ]
    # All four wheel speeds should correlate; the car encodes them at 0.01 kph.
    assert len(wheels) == 4
    for match in wheels:
        assert abs(match.r) > 0.999
        assert match.scale == pytest.approx(0.01, rel=1e-6)


def test_derivative_channels_identify_a_rate_signal(log, analyses, reference):
    # Steering *rate* correlates with nothing in the reference until the
    # reference's derivative is available to compare against.
    matches = correlate_log(
        log.streams, list(analyses.values()), reference, assign_names=False
    )
    rate = next(
        m for m in matches
        if m.addr == 0x025 and m.signal.start == 16
    )
    assert rate.channel == "d(steer_angle_deg)/dt"
    assert abs(rate.r) > 0.9


def test_names_are_assigned_once_per_channel(log, analyses, reference):
    # A car broadcasts road speed in several places; naming all of them SPEED
    # would be worse than useless.
    import copy

    copied = copy.deepcopy(list(analyses.values()))
    correlate_log(log.streams, copied, reference, assign_names=True)
    named = [s.name for a in copied for s in a.signals if s.name]
    speed_names = [n for n in named if n == "SPEED_KPH"]
    assert len(speed_names) == 1


def test_checksums_and_counters_are_never_correlated(log, analyses, reference):
    matches = correlate_log(
        log.streams, list(analyses.values()), reference, assign_names=False
    )
    for match in matches:
        assert match.signal.kind not in ("checksum", "counter", "constant")


def test_reference_derivative_units():
    series = ReferenceSeries(
        times=[0.0, 1.0, 2.0],
        channels={"speed": [0.0, 10.0, 20.0]},
        units={"speed": "km/h"},
    )
    extended = series.with_derivatives()
    assert extended.units["d(speed)/dt"] == "km/h/s"
    assert extended.channels["d(speed)/dt"] == [10.0, 10.0, 10.0]


def test_load_reference_csv_with_unit_annotations(tmp_path):
    path = tmp_path / "ref.csv"
    path.write_text(
        "time,speed [km/h],angle (deg)\n0.0,0,1\n0.1,5,2\n0.2,10,3\n0.3,15,4\n"
    )
    series = load_reference_csv(path)
    assert series.times == [0.0, 0.1, 0.2, 0.3]
    assert series.channels["speed"] == [0.0, 5.0, 10.0, 15.0]
    assert series.units == {"speed": "km/h", "angle": "deg"}


def test_load_reference_csv_drops_constant_channels(tmp_path):
    # A channel that never moves correlates with everything and nothing.
    path = tmp_path / "ref.csv"
    path.write_text("time,speed,flat\n0,1,7\n1,2,7\n2,3,7\n3,4,7\n")
    series = load_reference_csv(path)
    assert "speed" in series.channels
    assert "flat" not in series.channels


def test_load_reference_csv_requires_a_time_column(tmp_path):
    path = tmp_path / "ref.csv"
    path.write_text("a,b\n1,2\n3,4\n")
    with pytest.raises(ValueError, match="no time column"):
        load_reference_csv(path)


def test_load_reference_csv_explicit_time_column(tmp_path):
    path = tmp_path / "ref.csv"
    path.write_text("when,speed\n0,1\n1,2\n2,4\n3,9\n")
    series = load_reference_csv(path, time_column="when")
    assert series.times == [0.0, 1.0, 2.0, 3.0]


# --------------------------------------------------------------------------
# Torque Pro
#
# Torque Pro is the most common way a driver already has of recording what the
# car is doing, so its export is the reference log most people will arrive
# with. Every one of these was a reason its logs did not load at all.


def _torque(rows: str) -> str:
    return (
        "GPS Time, Device Time, Longitude,Speed (OBD)(km/h),"
        "Engine RPM(rpm),G(x),G(y)\n" + rows
    )


def test_torque_wall_clock_is_accepted_as_a_timebase(tmp_path):
    """Torque writes "02-Aug-2026 11:34:40.210", not elapsed seconds.

    The loader used to require a column named `time` holding a float, so a
    Torque log failed twice over: the column is called "Device Time", and
    every value in it is a date.
    """
    path = tmp_path / "trackLog.csv"
    path.write_text(_torque("".join(
        f"-,02-Aug-2026 11:34:{i:02d}.210,-,{i},{800 + i * 10},0.1,0.{i}\n"
        for i in range(16)
    )))
    series = load_reference_csv(path)
    assert len(series) == 16
    # Parsed as a clock, so the samples are one second apart.
    assert series.times[1] - series.times[0] == pytest.approx(1.0)
    assert series.channels["Speed (OBD)"][:3] == [0.0, 1.0, 2.0]
    assert series.units["Speed (OBD)"] == "km/h"


def test_torque_missing_marker_does_not_discard_the_row(tmp_path):
    """Torque writes "-" for a reading it does not have.

    The old loader stopped at the first cell it could not parse and threw the
    whole row away. Torque leaves the GPS columns "-" until the phone has a
    fix, and several OBD columns "-" for the whole drive, so *every* row had
    one -- and a 181-row log loaded as "no usable rows".
    """
    # 32 rows so that the half-empty channel still clears the sample floor.
    path = tmp_path / "trackLog.csv"
    path.write_text(_torque("".join(
        f"-,02-Aug-2026 11:34:{i:02d}.210,-,{i},"
        f"{'-' if i % 2 else 800 + i * 10},0.1,0.{i}\n"
        for i in range(32)
    )))
    series = load_reference_csv(path)
    assert len(series) == 32
    assert len(series.channels["Speed (OBD)"]) == 32
    # The gappy channel is still the length of the timebase, and says which of
    # its samples are real.
    assert len(series.channels["Engine RPM"]) == 32
    assert series.measured("Engine RPM") == [i % 2 == 0 for i in range(32)]
    assert series.measured("Speed (OBD)") == [True] * 32


def test_torque_repeated_header_stems_stay_separate_channels(tmp_path):
    """"G(x)" and "G(y)" both reduce to a channel called "G".

    Reading a trailing bracket as a unit is what makes "Speed (OBD)(km/h)"
    work, but Torque also ships G(x), G(y), G(z) and G(calibrated) -- four
    columns that collapsed onto one name, three of them overwriting the
    fourth.
    """
    path = tmp_path / "trackLog.csv"
    path.write_text(_torque("".join(
        f"-,02-Aug-2026 11:34:{i:02d}.210,-,{i},{800 + i * 10},"
        f"0.{i},0.{15 - i}\n"
        for i in range(16)
    )))
    series = load_reference_csv(path)
    assert "G(x)" in series.channels
    assert "G(y)" in series.channels
    assert series.channels["G(x)"] != series.channels["G(y)"]


def test_a_filled_gap_never_reaches_a_correlation():
    """Gaps are interpolated to keep every channel rectangular.

    Those filled samples describe this loader's arithmetic, not the car, so
    fitting a CAN signal against them would be measuring the wrong thing.
    """
    times = [float(i) for i in range(40)]
    # Real where measured, and absurd where not -- if a filled sample were ever
    # fitted against, the correlation would collapse.
    truth = [float(i) for i in range(40)]
    measured = [i % 2 == 0 for i in range(40)]
    series = ReferenceSeries(
        times=times,
        channels={"speed": [v if ok else -1e6 for v, ok in zip(truth, measured)]},
        present={"speed": measured},
    )
    kept = [
        (t, v) for t, v, ok in zip(series.times, series.channels["speed"],
                                   series.measured("speed")) if ok
    ]
    assert len(kept) == 20
    assert all(v >= 0 for _, v in kept)


def test_a_torque_shaped_reference_names_the_same_signals(
    tmp_path, log, analyses, reference
):
    """The format must not change the answer.

    Same reference measurements, rewritten the way Torque Pro writes them --
    wall-clock timestamps, "-" for missing, a unit in a trailing bracket, a
    GPS column that only starts partway in. The correlations, and the scales
    fitted from them, have to come out identical.
    """
    import csv
    import datetime

    names = list(reference.channels)
    path = tmp_path / "trackLog.csv"
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["GPS Time", " Device Time", " Longitude"]
            + [f"{n} (unit)" for n in names]
        )
        for i, t in enumerate(reference.times):
            stamp = datetime.datetime.fromtimestamp(t).strftime(
                "%d-%b-%Y %H:%M:%S.%f"
            )[:-3]
            fixed = i > len(reference.times) // 3
            writer.writerow(
                [stamp if fixed else "-", stamp, "21.08" if fixed else "-"]
                + [reference.channels[n][i] for n in names]
            )

    reloaded = load_reference_csv(path)
    assert len(reloaded) == len(reference)

    def fits(series):
        matches = correlate_log(
            log.streams, list(analyses.values()), series, assign_names=False
        )
        return sorted(
            (m.addr, m.signal.start, m.channel, round(m.r, 9), round(m.scale, 9))
            for m in matches
        )

    original = fits(reference)
    assert original, "the fixture reference should match something"
    assert fits(reloaded) == original
