"""Human knowledge is accepted deliberately, validated, and kept identifiable."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from autodistill_can.analysis import BitStats, MessageAnalysis, Signal
from autodistill_can.manual import (
    apply_vehicle_info,
    load_vehicle_info,
    vehicle_info_template,
)


def test_empty_template_is_valid_and_contains_no_pretend_evidence():
    template = vehicle_info_template()
    info = load_vehicle_info(template)
    assert info.has_content is False
    assert template["ecu_types"] == {}
    assert template["signals"] == []


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"mystery": True}, "unknown top-level"),
        ({"schema_version": True}, "unsupported vehicle info schema"),
        ({"signals": [{"bus": 0, "address": "0x123", "start_bit": 0,
                        "name": "SPEED", "signed": "yes"}]},
         "signed must be true or false"),
        ({"signals": [{"bus": 0, "address": "0x123", "start_bit": 0,
                        "name": "SPEED", "byte_order": "sideways"}]},
         "byte_order must be 'big' or 'little'"),
        ({"vehicle_specs": {"mass_kg": 100}}, "mass_kg"),
        ({"ecu_types": {"0x7E0": "motor_thing"}}, "must be one of"),
        ({
            "signals": [{
                "bus": 0.5, "address": "0x123", "start_bit": 0,
                "name": "SPEED",
            }],
        }, "bus must be an integer"),
        ({
            "signals": [{
                "bus": 0, "address": "0x123", "start_bit": 0,
                "name": "SPEED", "scale": 0,
            }],
        }, "scale must not be zero"),
        ({
            "signals": [{
                "bus": 0, "address": "0x123", "start_bit": 0,
                "name": "SPEED", "carstate_target": "ret.unsafeGuess",
            }],
        }, "carstate_target must be one of"),
        ({
            "signals": [
                {
                    "bus": 0, "address": "0x123", "start_bit": 0,
                    "name": "SPEED_A", "carstate_target": "ret.vEgoRaw",
                },
                {
                    "bus": 0, "address": "0x124", "start_bit": 0,
                    "name": "SPEED_B", "carstate_target": "ret.vEgoRaw",
                },
            ],
        }, "duplicate manual CarState target"),
    ],
)
def test_manual_schema_rejects_unsafe_or_ambiguous_values(payload, match):
    with pytest.raises(ValueError, match=match):
        load_vehicle_info(payload)


def test_signal_override_changes_only_the_recovered_field(analyses):
    copied = deepcopy(list(analyses.values()))
    analysis = next(
        row for row in copied
        if any(signal.kind not in {"counter", "checksum", "constant"}
               for signal in row.signals)
    )
    signal = next(
        signal for signal in analysis.signals
        if signal.kind not in {"counter", "checksum", "constant"}
    )
    info = load_vehicle_info({
        "signals": [{
            "bus": analysis.bus,
            "address": f"0x{analysis.addr:X}",
            "start_bit": signal.start,
            "length": signal.length,
            "name": "CONFIRMED_FIELD",
            "unit": "km/h",
            "scale": 0.125,
            "offset": -2,
            "carstate_target": "ret.vEgoRaw",
        }],
    })

    apply_vehicle_info(copied, info)

    assert signal.name == "CONFIRMED_FIELD"
    assert signal.unit == "km/h"
    assert signal.scale == 0.125
    assert signal.offset == -2
    assert signal.carstate_target == "ret.vEgoRaw"
    assert "human-supplied vehicle-info override" in signal.notes


def test_override_must_match_a_field_autodistill_recovered(analyses):
    info = load_vehicle_info({
        "signals": [{
            "bus": 0,
            "address": "0x1ABCDE",
            "start_bit": 0,
            "name": "INVENTED",
        }],
    })
    with pytest.raises(ValueError, match="unknown message"):
        apply_vehicle_info(deepcopy(list(analyses.values())), info)


def _split_toyota_command() -> MessageAnalysis:
    return MessageAnalysis(
        bus=0, addr=0x2E4, length=5, n_frames=100,
        period=0.01, frequency=100.0, jitter=0.0,
        stats=BitStats(100, 40, [0] * 40, [0] * 40),
        signals=[
            Signal(0, 8, "constant"),
            Signal(8, 8, "physical"),
            Signal(16, 7, "physical"),
            Signal(23, 1, "bool"),
            Signal(24, 8, "constant"),
            Signal(32, 8, "checksum", name="CHECKSUM"),
        ],
    )


def test_explicit_manual_width_can_correct_a_recovered_split():
    analysis = _split_toyota_command()
    info = load_vehicle_info({"signals": [{
        "bus": 0, "address": "0x2E4", "start_bit": 8, "length": 16,
        "name": "STEER_TORQUE_CMD", "signed": True, "byte_order": "big",
        "scale": 1,
    }]})

    apply_vehicle_info([analysis], info)

    merged = next(signal for signal in analysis.signals
                  if signal.name == "STEER_TORQUE_CMD")
    assert (merged.start, merged.length, merged.signed, merged.big_endian) == (
        8, 16, True, True,
    )
    assert next(signal for signal in analysis.signals
                if signal.kind == "checksum").start == 32
    occupied = [bit for signal in analysis.signals
                for bit in signal.payload_bits(40)]
    assert sorted(occupied) == list(range(40))
    assert len(occupied) == len(set(occupied))


def test_boundary_correction_requires_explicit_encoding():
    analysis = _split_toyota_command()
    info = load_vehicle_info({"signals": [{
        "bus": 0, "address": "0x2E4", "start_bit": 8, "length": 16,
        "name": "AMBIGUOUS_ENCODING",
    }]})
    with pytest.raises(ValueError, match="declare both signed and byte_order"):
        apply_vehicle_info([analysis], info)


def test_explicit_fact_can_define_a_field_in_a_below_threshold_message():
    analysis = MessageAnalysis(
        bus=0, addr=0x614, length=8, n_frames=8,
        period=None, frequency=None, jitter=None,
        stats=BitStats(8, 64, [0] * 64, [0] * 64),
    )
    info = load_vehicle_info({"signals": [{
        "bus": 0, "address": "0x614", "start_bit": 26, "length": 2,
        "name": "TURN_SIGNALS", "signed": False, "byte_order": "big",
    }]})

    apply_vehicle_info([analysis], info)

    turn = next(signal for signal in analysis.signals
                if signal.name == "TURN_SIGNALS")
    assert (turn.start, turn.length) == (26, 2)
    occupied = [bit for signal in analysis.signals
                for bit in signal.payload_bits(64)]
    assert sorted(occupied) == list(range(64))
    assert len(occupied) == len(set(occupied))


def test_manual_width_cannot_overwrite_integrity_bits():
    analysis = _split_toyota_command()
    info = load_vehicle_info({"signals": [{
        "bus": 0, "address": "0x2E4", "start_bit": 24, "length": 16,
        "name": "NOT_REALLY_DATA", "signed": False, "byte_order": "big",
    }]})
    with pytest.raises(ValueError, match="protected checksum"):
        apply_vehicle_info([analysis], info)


# --------------------------------------------------------------------------
# Facts that complete a port: bindings, actuation, limits, safety
# --------------------------------------------------------------------------


@pytest.mark.parametrize("payload,message", [
    ({"carstate": [{"target": "ret.notAField", "bus": 0, "address": "0x1",
                    "signal": "X"}]}, "target must be one of"),
    ({"carstate": [{"target": "ret.vEgoRaw", "bus": 0, "address": "0x1",
                    "signal": "X", "transform": "magic"}]},
     "transform must be one of"),
    ({"carstate": [{"target": "ret.brakePressed", "bus": 0, "address": "0x1",
                    "signal": "X", "transform": "threshold"}]},
     "needs a threshold"),
    ({"carstate": [{"target": "ret.leftBlinker", "bus": 0, "address": "0x1",
                    "signal": "X", "transform": "equals"}]},
     "needs a threshold"),
    ({"carstate": [{"target": "ret.gearShifter", "bus": 0, "address": "0x1",
                    "signal": "X", "transform": "gear_map"}]},
     "needs a gear_map"),
    ({"carstate": [{"target": "ret.gearShifter", "bus": 0, "address": "0x1",
                    "signal": "X", "transform": "gear_map",
                    "gear_map": {"0": "overdrive"}}]}, "must be one of"),
    ({"carstate": [
        {"target": "ret.vEgoRaw", "bus": 0, "address": "0x1", "signal": "A"},
        {"target": "ret.vEgoRaw", "bus": 0, "address": "0x1", "signal": "B"},
    ]}, "duplicate carstate target"),
    ({"actuation": {"sideways": {}}}, "unknown actuation purpose"),
    ({"actuation": {"lateral": {"bus": 0, "address": "0x1",
                                "frequency_hz": 100, "signals": {}}}},
     "at least one signal"),
    ({"actuation": {"lateral": {"bus": 0, "address": "0x1", "frequency_hz": 100,
                                "signals": {"S": "hack_the_car"}}}},
     "must be a number or one of"),
    ({"actuation": {"lateral": {"bus": 0, "address": "0x1",
                                "signals": {"S": 1}}}}, "frequency_hz is required"),
    ({"actuation": {"lateral": {"bus": 0, "address": "0x1",
                                "frequency_hz": 101, "signals": {"S": 1}}}},
     "frequency_hz must be"),
    ({"actuation": {"cruise_buttons": {"bus": 0, "address": "0x1",
                                        "frequency_hz": 10,
                                        "signals": {"BUTTON": 1}}}},
     "cruise_buttons is not supported"),
    ({"safety": {"model": "Not A Model"}}, "SafetyModel name"),
    ({"limits": {"steer_max": -5}}, "steer_max must be"),
    ({"tuning": {"lateral": {"kind": "telepathy"}}}, "kind must be one of"),
])
def test_invalid_control_facts_are_rejected(payload, message):
    with pytest.raises(ValueError, match=message):
        load_vehicle_info({"schema_version": 1, **payload})


def test_actuation_values_are_a_closed_vocabulary():
    """No free-text expressions.

    Whatever is written here ends up inside a generated controller, and the
    file it comes from is the kind of thing people email each other. A closed
    set of names keeps that from being a way to run code.
    """
    info = load_vehicle_info({
        "actuation": {"lateral": {
            "bus": 0, "address": "0x2B0", "message": "LKAS",
            "frequency_hz": 100,
            "signals": {"TORQUE": "apply_torque", "SET_ME_1": 1},
        }},
    })
    assert info.command("lateral").signals == (
        ("TORQUE", "apply_torque"), ("SET_ME_1", 1.0)
    )


def test_control_facts_round_trip_through_json():
    payload = {
        "schema_version": 1,
        "vehicle_specs": {"mass_kg": 1845, "wheelbase_m": 2.7,
                          "steer_ratio": 14.0, "center_to_front_m": 1.2},
        "carstate": [{"target": "ret.brakePressed", "bus": 0,
                      "address": "0x1A0", "signal": "BRAKE_PRESSURE",
                      "transform": "threshold", "threshold": 10}],
        "actuation": {"lateral": {
            "bus": 0, "address": "0x2B0", "message": "LKAS",
            "frequency_hz": 100, "signals": {"TORQUE": "apply_torque"},
            "counter_signal": "COUNTER", "checksum_signal": "CHECKSUM",
        }},
        "limits": {"steer_max": 384, "steer_delta_up": 3,
                   "steer_delta_down": 7, "steer_driver_allowance": 50,
                   "steer_actuator_delay": 0.1},
        "tuning": {"lateral": {"kind": "torque", "max_lateral_accel": 2.5,
                               "friction": 0.1}},
        "safety": {"model": "hyundaiCanfd", "param": 4},
    }
    info = load_vehicle_info(payload)
    assert info.limits.lateral_complete
    assert info.lateral_tuning.complete
    assert info.safety.known
    assert load_vehicle_info(json.loads(json.dumps(info.to_dict()))) == info


def test_an_unknown_safety_model_is_accepted_but_flagged():
    # opendbc adds safety modes; preserve an unfamiliar name for a future
    # generator, but do not treat it as enough to activate control today.
    info = load_vehicle_info({"safety": {"model": "someNewPlatform"}})
    assert info.safety.model == "someNewPlatform"
    assert not info.safety.known


@pytest.mark.parametrize(
    "model", ["fcaGiorgio", "hondaBoschGiraffe", "volkswagenMqbEvo"]
)
def test_current_opendbc_safety_models_are_recognised(model):
    assert load_vehicle_info({"safety": {"model": model}}).safety.known
