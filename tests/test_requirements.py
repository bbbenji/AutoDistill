"""The checklist that says how much of a port exists yet.

A port is not one thing. It can read a car, steer it, or drive it, and each of
those needs strictly more than the last. These check that the scoring says so
honestly: that nothing is credited to AutoDistill that a person supplied, and
that supplying facts is what moves an item, not generating a port.
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from autodistill_can.coverage import evaluate
from autodistill_can.manual import load_vehicle_info
from autodistill_can.requirements import (
    REQUIREMENTS,
    requirements_for,
    snippet_for,
)


def test_levels_are_cumulative():
    read = set(requirements_for("read"))
    lateral = set(requirements_for("lateral"))
    longitudinal = set(requirements_for("longitudinal"))
    assert read < lateral < longitudinal
    # Steering a car needs strictly more than reading it.
    assert {r.key for r in lateral - read} >= {"control.lateral", "safety.model"}
    assert {r.key for r in longitudinal - lateral} >= {
        "limits.accel_min", "limits.accel_max"
    }


def test_a_bare_capture_satisfies_nothing_by_itself(analyses):
    # From a pristine copy: correlation renames signals in place, and this is
    # specifically about a capture nobody has correlated or annotated.
    coverage = evaluate(deepcopy(list(analyses.values())))
    assert not coverage.complete_for("read")
    # Without a reference log nothing is named, so nothing can be recognised.
    assert all(item.status == "missing" for item in coverage)


def test_named_signals_are_credited_as_automatic(analyses, log, reference):
    from autodistill_can.analysis.correlate import correlate_log

    ordered = deepcopy(list(analyses.values()))
    correlate_log(log.streams, ordered, reference, min_abs_r=0.9)
    coverage = evaluate(ordered)
    speed = coverage.by_key("ret.vEgoRaw")
    assert speed is not None and speed.status == "auto"
    assert "SPEED_KPH" in speed.source


def test_supplying_facts_moves_items_to_human(analyses):
    info = load_vehicle_info({
        "schema_version": 1,
        "vehicle_specs": {"mass_kg": 1800, "wheelbase_m": 2.7, "steer_ratio": 14.0},
        "safety": {"model": "hyundai"},
    })
    coverage = evaluate(deepcopy(list(analyses.values())), info)
    assert coverage.by_key("specs.mass").status == "manual"
    assert coverage.by_key("safety.model").status == "manual"
    assert coverage.by_key("safety.model").source == "hyundai"
    # And nothing else was credited along with them.
    assert not coverage.complete_for("read")


def test_missing_reports_only_mandatory_items_by_default(analyses):
    coverage = evaluate(deepcopy(list(analyses.values())))
    mandatory = coverage.missing("read")
    everything = coverage.missing("read", mandatory_only=False)
    assert len(mandatory) < len(everything)
    assert all(item.mandatory for item in mandatory)


def test_every_requirement_says_how_to_obtain_it():
    """Naming a gap is only half of it.

    "ret.gearShifter is missing" is not actionable to someone who has never
    reverse-engineered a car. Each requirement carries the procedure, so a new
    one cannot be added without saying where its answer comes from.
    """
    for requirement in REQUIREMENTS:
        how = requirement.how
        assert how, f"{requirement.key} does not say how to obtain it"
        assert len(how) > 60, f"{requirement.key}: {how!r} is not a procedure"
        assert how[0].isupper() and how.rstrip().endswith((".", "'"))


def test_the_procedure_reaches_consumers_of_the_coverage(analyses):
    payload = evaluate(deepcopy(list(analyses.values()))).to_dict()
    gear = next(i for i in payload["items"] if i["key"] == "ret.gearShifter")
    assert "selector" in gear["how"]


def test_every_requirement_offers_a_way_to_supply_it():
    for requirement in REQUIREMENTS:
        snippet = snippet_for(requirement)
        assert snippet, f"{requirement.key} has no snippet"
        # The fragment names a real vehicle-info section.
        assert snippet.split('"')[1] in {
            "carstate", "vehicle_specs", "actuation", "limits", "tuning", "safety",
            "engineering",
        }


@pytest.mark.parametrize("key,expected", [
    ("ret.brakePressed", '"transform": "bool"'),
    ("ret.vEgoRaw", '"transform": "kph_to_ms"'),
    ("ret.gearShifter", '"gear_map"'),
])
def test_snippets_suggest_the_transform_a_field_usually_needs(key, expected):
    requirement = next(r for r in REQUIREMENTS if r.key == key)
    assert expected in snippet_for(requirement)


def test_coverage_serialises_for_the_report_and_the_ui(analyses):
    payload = json.loads(json.dumps(evaluate(list(analyses.values())).to_dict()))
    assert set(payload) == {"read", "lateral", "longitudinal", "items"}
    assert payload["read"]["total"] > 0
    assert {"key", "status", "need", "mandatory"} <= set(payload["items"][0])
