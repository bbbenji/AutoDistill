"""Replaying a capture through the bindings catches what compiling cannot.

The generated port can compile, load, and construct perfectly while
``ret.brakePressed`` sits on the wrong bit and ``ret.vEgoRaw`` is in km/h.
These check that such a port is caught here, on the recording the user already
has, rather than on the road.

The counterpart matters as much: a validator that fires on correct bindings
would train people to ignore it, so the sane cases must come back clean.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from autodistill_can.manual import apply_vehicle_info, load_vehicle_info
from autodistill_can.validate import validate


@pytest.fixture(scope="module")
def correlated(analyses, log, reference):
    from autodistill_can.analysis.correlate import correlate_log

    ordered = deepcopy(list(analyses.values()))
    correlate_log(log.streams, ordered, reference)
    return ordered


def _bind(correlated, target, signal_name, **binding):
    """A vehicle-info binding a named signal to a CarState field."""
    for analysis in correlated:
        for signal in analysis.signals:
            if signal.default_name(analysis.addr) == signal_name:
                return load_vehicle_info({"carstate": [{
                    "target": target, "bus": analysis.bus,
                    "address": analysis.addr, "signal": signal_name,
                    **binding,
                }]})
    raise AssertionError(f"{signal_name} is not in this capture")


def test_correlated_bindings_come_back_clean(correlated, log):
    """The auto-detected fields of a well-formed capture raise nothing.

    This is the false-positive check. Every finding has to mean something, or
    the report becomes noise people scroll past.
    """
    report = validate(correlated, log)
    assert report.ok, [f.message for f in report.errors]
    targets = {entry.target for entry in report.fields}
    assert "ret.vEgoRaw" in targets
    speed = next(e for e in report.fields if e.target == "ret.vEgoRaw")
    # Converted to m/s, not left in the signal's own km/h.
    assert 0 <= speed.minimum < 1 and speed.maximum < 40


def test_a_speed_left_in_kmh_is_caught(correlated, log):
    info = _bind(correlated, "ret.vEgoRaw", "SPEED_KPH", transform="identity")
    apply_vehicle_info(deepcopy(correlated), info)
    report = validate(correlated, log, info)
    assert not report.ok
    message = " ".join(f.message for f in report.errors)
    # The unit the correlation recovered says so outright, which is the only
    # way to catch this on a capture that never exceeds 90km/h.
    assert "is in km/h" in message and "kph_to_ms" in message


def test_a_speed_that_goes_negative_is_caught(correlated, log):
    # A signed reading of an unsigned field: the classic byte-order symptom.
    info = _bind(
        correlated, "ret.vEgoRaw", "SPEED_KPH", transform="scale", scale=-0.1
    )
    report = validate(correlated, log, info)
    assert any("negative" in f.message for f in report.errors)


def test_a_flag_stuck_on_is_caught(correlated, log):
    info = _bind(
        correlated, "ret.brakePressed", "SPEED_KPH",
        transform="threshold", threshold=-1,
    )
    report = validate(correlated, log, info)
    assert any(
        f.target == "ret.brakePressed" and "every frame" in f.message
        for f in report.errors
    )


def test_a_non_blocking_flag_stuck_on_does_not_claim_engagement_is_impossible(
    correlated, log,
):
    info = _bind(
        correlated, "ret.cruiseState.available", "SPEED_KPH",
        transform="threshold", threshold=-1,
    )
    report = validate(correlated, log, info)
    finding = next(f for f in report.findings
                   if f.target == "ret.cruiseState.available")
    assert finding.level == "warning"
    assert "never exercised its inactive state" in finding.message
    assert "never engage" not in finding.message


def test_a_gear_map_that_never_reaches_drive_is_caught(correlated, log):
    info = _bind(
        correlated, "ret.gearShifter", "SPEED_KPH",
        transform="gear_map", gear_map={"999999": "drive"},
    )
    report = validate(correlated, log, info)
    assert any(
        "never reads drive" in f.message for f in report.errors
    ), [f.message for f in report.findings]


def test_a_pedal_outside_zero_to_one_is_caught(correlated, log):
    info = _bind(correlated, "ret.gas", "SPEED_KPH", transform="identity")
    report = validate(correlated, log, info)
    assert any("expects 0 to 1" in f.message for f in report.errors)


def test_an_untouched_flag_is_a_note_not_an_error(correlated, log):
    # A door that was never opened is normal, and must not read as a defect.
    info = _bind(
        correlated, "ret.doorOpen", "SPEED_KPH",
        transform="threshold", threshold=1e9,
    )
    report = validate(correlated, log, info)
    assert report.ok
    assert any(
        f.level == "note" and "never true" in f.message for f in report.findings
    )


def test_findings_reach_the_generated_port(correlated, log, tmp_path):
    from autodistill_can.analysis.fingerprint import build_fingerprint
    from autodistill_can.emit.port import write_port

    info = _bind(correlated, "ret.vEgoRaw", "SPEED_KPH", transform="identity")
    write_port(
        deepcopy(correlated), build_fingerprint(log), tmp_path / "port",
        brand="testcar", car_name="TEST CAR", vehicle_info=info, log=log,
    )
    import json

    status = json.loads((tmp_path / "port" / "port_status.json").read_text())
    assert status["validation"]["ok"] is False
    # And it is in the list a person actually reads, not only the manifest.
    assert any("is in km/h" in item for item in status["human_required"])
    assert "is in km/h" in (tmp_path / "port" / "README.md").read_text()


def test_a_port_written_without_the_capture_says_so(correlated, log, tmp_path):
    from autodistill_can.analysis.fingerprint import build_fingerprint
    from autodistill_can.emit.port import write_port

    write_port(
        deepcopy(correlated), build_fingerprint(log), tmp_path / "port",
        brand="testcar", car_name="TEST CAR",
    )
    import json

    status = json.loads((tmp_path / "port" / "port_status.json").read_text())
    assert status["validation"] == {}
