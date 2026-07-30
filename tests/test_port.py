"""The generated openpilot port package.

Two things are being protected here. The first is that the output is *valid* —
every file parses, the DBC loads, the fingerprint is well-formed — because a
port that does not import is worthless however good the analysis behind it was.

The second matters more: that the output is *inert*. A generated port must not
actuate a car. Nothing about how to drive a vehicle can be read off its bus, so
a controller that looks finished would be an invitation to try it. The tests
below fail if the generated controller ever starts sending, or if the safety
model stops being ``noOutput``.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import replace

import pytest

from autodistill_can.analysis.fingerprint import build_fingerprint, is_diagnostic_address
from autodistill_can.emit.port import (
    _CARSTATE_HINTS,
    PortSpec,
    carstate_bindings,
    write_port,
)
from autodistill_can.manual import load_vehicle_info
from autodistill_can.requirements import REQUIREMENTS

cantools = pytest.importorskip("cantools", reason="cantools not installed")


@pytest.fixture(scope="module")
def port(analyses, log, reference, tmp_path_factory):
    from autodistill_can.analysis.correlate import correlate_log

    # Correlation renames signals in place, and `analyses` is session-scoped.
    # Without a copy this fixture would leave named signals behind for every
    # later test, whichever order they happen to run in.
    ordered = deepcopy(list(analyses.values()))
    correlate_log(log.streams, ordered, reference)
    directory = tmp_path_factory.mktemp("port")
    write_port(
        ordered, build_fingerprint(log), directory,
        brand="testcar", car_name="TEST CAR 2024",
    )
    return directory


# --------------------------------------------------------------------------
# Validity
# --------------------------------------------------------------------------


def test_every_expected_file_is_written(port):
    expected = {
        "__init__.py", "values.py", "fingerprints.py", "carstate.py",
        "carcontroller.py", "interface.py", "radar_interface.py",
        "testcarcan.py", "testcar_generated.dbc",
        "testcar_generated_bus2.dbc", "torque_data_override.toml",
        "port_status.json", "README.md",
    }
    assert expected <= {p.name for p in port.iterdir()}


def test_generated_python_parses(port):
    for path in sorted(port.glob("*.py")):
        compile(path.read_text(), path.name, "exec")


def test_generated_dbc_loads(port):
    db = cantools.database.load_file(port / "testcar_generated.dbc")
    assert db.messages


def test_values_declares_the_platform_and_its_dbc(port):
    source = (port / "values.py").read_text()
    assert "class CAR(Platforms)" in source
    assert "TEST_CAR_2024 = PlatformConfig(" in source
    # openpilot installs between the camera and the car, so a capture that saw
    # the camera bus must wire its DBC in too -- otherwise the port can read
    # the vehicle but never stand in for the stock system.
    assert "Bus.main: 'testcar_generated'" in source
    assert "Bus.cam: 'testcar_generated_bus2'" in source
    assert "DBC = CAR.create_dbc_map()" in source
    # Non-zero placeholders keep current opendbc's read-only interface from
    # dividing by zero, but must remain unmistakably marked as placeholders.
    assert "CarSpecs(mass=1500, wheelbase=2.7, steerRatio=15.0)" in source
    assert "Non-zero placeholders" in source


def test_fingerprint_entries_are_integers(port):
    body = (port / "fingerprints.py").read_text()
    block = body[body.index("FINGERPRINTS"): body.index("FW_VERSIONS")]
    pairs = re.findall(r"(\d+):\s*(\d+)", block)
    assert pairs, "no fingerprint entries were written"
    for addr, length in pairs:
        assert 0 < int(length) <= 64
        assert int(addr) >= 0


def test_carstate_parser_checks_only_periodic_main_bus_messages(port, analyses):
    source = (port / "carstate.py").read_text()
    for analysis in analyses.values():
        if (
            analysis.bus == 0
            and analysis.length > 0
            and not is_diagnostic_address(analysis.addr)
            and not analysis.is_event_driven
            and (analysis.frequency or 0) >= 1
        ):
            assert f'"{analysis.name}"' in source
    # Event-driven traffic must not make canValid false when no event occurs.
    assert '"MSG_5A0"' not in source


def test_checksum_helpers_are_carried_into_the_port(port, analyses):
    source = (port / "testcarcan.py").read_text()
    for analysis in analyses.values():
        if analysis.checksum is not None:
            assert f"def checksum_{analysis.addr:03x}" in source


def test_counter_table_is_carried_into_the_port(port, analyses):
    source = (port / "testcarcan.py").read_text()
    if any(a.counter is not None for a in analyses.values()):
        assert "COUNTERS = {" in source


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------


def test_generated_controller_sends_nothing(port):
    """The single most important property of the output.

    How to actuate a car is not recoverable from watching its bus. A generated
    controller that appeared complete would invite someone to run it against a
    vehicle on the strength of a guess.
    """
    source = (port / "carcontroller.py").read_text()
    body = source[source.index("def update"):]
    assert "can_sends = []" in body
    # Nothing may be appended to the outgoing list.
    assert "can_sends.append" not in body
    assert "return new_actuators, can_sends" in body


def test_generated_interface_requests_the_no_output_safety_model(port):
    source = (port / "interface.py").read_text()
    assert "SafetyModel.noOutput" in source
    assert "ret.dashcamOnly = True" in source


# --------------------------------------------------------------------------
# The other half: a port completed by human-supplied facts
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def control_analyses(analyses, log, reference):
    """A private, correlated copy.

    The read-only `port` fixture renames signals in place as it correlates, so
    a facts file built against the shared analyses would name fields that no
    longer exist by the time it is applied.
    """
    from autodistill_can.analysis.correlate import correlate_log

    ordered = deepcopy(list(analyses.values()))
    correlate_log(log.streams, ordered, reference)
    return ordered


@pytest.fixture(scope="module")
def declared_facts(control_analyses):
    """A vehicle-info file describing actuation, built from real signals.

    Picked out of the analysis rather than hard-coded, so the fixture keeps
    describing this car if the synthetic one changes.
    """
    commandable = next(
        a for a in control_analyses
        if a.counter is not None and a.checksum is not None
        and any(s.kind not in ("counter", "checksum") for s in a.signals)
    )
    payload_signal = next(
        s for s in commandable.signals if s.kind not in ("counter", "checksum")
    )
    usable = [
        (a, s) for a in control_analyses for s in a.signals
        if s.kind not in ("counter", "checksum", "constant")
    ]
    # Bind every mandatory CarState field, so this fixture is a port that is
    # complete on paper -- which is what the safety assertions below are
    # actually about.
    carstate = []
    for index, requirement in enumerate(
        r for r in REQUIREMENTS if r.kind == "carstate" and r.mandatory
    ):
        analysis, signal = usable[index % len(usable)]
        binding = {
            "target": requirement.key,
            "bus": analysis.bus,
            "address": analysis.addr,
            "signal": signal.default_name(analysis.addr),
            "transform": (
                "gear_map" if requirement.key == "ret.gearShifter"
                else "bool" if requirement.detail.startswith("Boolean")
                else "identity"
            ),
        }
        if binding["transform"] == "gear_map":
            binding["gear_map"] = {"0": "park", "1": "reverse", "3": "drive"}
        carstate.append(binding)

    return {
        "schema_version": 1,
        "vehicle_specs": {
            "mass_kg": 1845, "wheelbase_m": 2.766, "steer_ratio": 14.3,
            "docs_package": "All", "harness": "hyundai_k",
        },
        "carstate": carstate,
        "actuation": {
            "lateral": {
                "bus": commandable.bus,
                "address": commandable.addr,
                "message": commandable.name,
                "frequency_hz": 100,
                "signals": {
                    payload_signal.default_name(commandable.addr): "apply_torque",
                },
                "counter_signal": "COUNTER",
                "checksum_signal": "CHECKSUM",
            },
        },
        "limits": {
            "steer_max": 384, "steer_delta_up": 3, "steer_delta_down": 7,
            "steer_driver_allowance": 50, "steer_actuator_delay": 0.1,
        },
        "tuning": {
            "lateral": {"kind": "torque", "max_lateral_accel": 2.5, "friction": 0.1}
        },
        "safety": {"model": "hyundai", "param": 4},
    }


@pytest.fixture(scope="module")
def control_port(control_analyses, log, declared_facts, tmp_path_factory):
    directory = tmp_path_factory.mktemp("control_port")
    write_port(
        deepcopy(control_analyses), build_fingerprint(log), directory,
        brand="testcar", car_name="TEST CAR 2024",
        vehicle_info=load_vehicle_info(declared_facts),
    )
    return directory


def test_declared_actuation_produces_a_controller_that_sends(control_port):
    source = (control_port / "carcontroller.py").read_text()
    body = source[source.index("def update"):]
    assert "can_sends.append(self.packers[0].make_can_msg(" in body
    assert "apply_torque" in body
    # Rate limiting is the safety-relevant part and must be present, not just
    # the send. A controller that passes the target straight through would be
    # worse than the inert one it replaced.
    assert "STEER_DELTA_UP" in body and "STEER_DELTA_DOWN" in body
    assert "STEER_DRIVER_ALLOWANCE" in body


def test_the_sent_message_carries_a_verified_checksum(control_port):
    source = (control_port / "carcontroller.py").read_text()
    assert re.search(r'values\["CHECKSUM"\] = testcarcan\.checksum_\w+\(', source)
    # It is computed over the packed bytes, not guessed.
    assert "make_can_msg" in source.split('"CHECKSUM": 0')[1]


def test_declared_limits_reach_carcontrollerparams(control_port):
    source = (control_port / "values.py").read_text()
    assert "STEER_MAX = 384" in source
    assert "STEER_DELTA_UP = 3" in source
    assert "STEER_DRIVER_ALLOWANCE = 50" in source
    assert "CarSpecs(mass=1845" in source


def test_declared_safety_model_reaches_the_interface(control_port):
    source = (control_port / "interface.py").read_text()
    assert "SafetyModel.hyundai" in source
    assert "ret.safetyConfigs[0].safetyParam = 4" in source
    assert "ret.dashcamOnly = False" in source
    assert "ret.lateralTuning.torque.friction = 0.1" in source


def test_bound_carstate_fields_are_read(control_port):
    source = (control_port / "carstate.py").read_text()
    assert re.search(r"ret\.brakePressed = bool\(cp\.vl\[", source)
    # Derived fields come along once their input exists.
    assert "ret.standstill = " in source


def test_a_complete_port_is_still_never_safe_for_control(control_port):
    status = json.loads((control_port / "port_status.json").read_text())
    assert status["mode"] == "control"
    assert status["controls"]["lateral"] is True
    assert status["controls"]["source"] == "human-declared"
    # The whole point. AutoDistill cannot drive a car, so it is not the thing
    # that gets to say the port is safe -- however complete the checklist is.
    assert status["safe_for_control"] is False
    assert status["coverage"]["lateral"]["complete"] is True


def test_control_port_still_compiles(control_port):
    for path in sorted(control_port.glob("*.py")):
        compile(path.read_text(), path.name, "exec")


def test_longitudinal_tuning_uses_only_current_opendbc_fields(
    control_analyses, log, tmp_path
):
    """opendbc moved `LongitudinalPIDTuning.kpBP`/`kpV` under a `deprecated`
    group, and no current brand's interface sets them -- only `kiBP`/`kiV`
    are live. Setting the plain (non-deprecated) attribute name no longer
    exists in that struct and raises `AttributeError` the instant a real
    `CarParams` is built, which nothing here caught until this test: the
    existing `control_port` fixture only ever declares lateral actuation, so
    the longitudinal branch of `_interface` was untested code until a
    capture-driven facts file (`autodistill_can.demo`) started using it.
    """
    from autodistill_can.demo import build_demo_facts

    facts = build_demo_facts(deepcopy(control_analyses), log)
    directory = tmp_path / "long_port"
    write_port(
        deepcopy(control_analyses), build_fingerprint(log), directory,
        brand="longtest", car_name="LONG TEST", vehicle_info=load_vehicle_info(facts),
    )
    source = (directory / "interface.py").read_text()
    assert "ret.openpilotLongitudinalControl = True" in source
    assert "ret.longitudinalTuning.kiBP = " in source
    assert "ret.longitudinalTuning.kiV = " in source
    assert "longitudinalTuning.kpBP" not in source
    assert "longitudinalTuning.kpV" not in source
    for path in sorted(directory.glob("*.py")):
        compile(path.read_text(), path.name, "exec")


def test_actuation_must_name_a_message_that_was_captured(
    control_analyses, declared_facts
):
    from autodistill_can.manual import apply_vehicle_info

    facts = deepcopy(declared_facts)
    facts["actuation"]["lateral"]["address"] = 0x7FE
    with pytest.raises(ValueError, match="not in this capture"):
        apply_vehicle_info(deepcopy(control_analyses), load_vehicle_info(facts))


def test_actuation_must_name_signals_that_exist(control_analyses, declared_facts):
    from autodistill_can.manual import apply_vehicle_info

    facts = deepcopy(declared_facts)
    facts["actuation"]["lateral"]["signals"] = {"NOT_A_FIELD": "apply_torque"}
    with pytest.raises(ValueError, match="not a field of"):
        apply_vehicle_info(deepcopy(control_analyses), load_vehicle_info(facts))


def test_actuation_must_use_the_generated_dbc_message_name(
    control_analyses, declared_facts
):
    from autodistill_can.manual import apply_vehicle_info

    facts = deepcopy(declared_facts)
    facts["actuation"]["lateral"]["message"] = "LOOKS_PLAUSIBLE_BUT_IS_WRONG"
    with pytest.raises(ValueError, match="generated DBC names"):
        apply_vehicle_info(deepcopy(control_analyses), load_vehicle_info(facts))


def test_actuation_cannot_omit_a_recovered_integrity_field(
    control_analyses, declared_facts
):
    from autodistill_can.manual import apply_vehicle_info

    for field in ("counter_signal", "checksum_signal"):
        facts = deepcopy(declared_facts)
        del facts["actuation"]["lateral"][field]
        with pytest.raises(ValueError, match=f"omits {field}"):
            apply_vehicle_info(deepcopy(control_analyses), load_vehicle_info(facts))


def test_actuation_values_must_exist_in_the_selected_control_path(
    control_analyses, declared_facts
):
    from autodistill_can.manual import apply_vehicle_info

    facts = deepcopy(declared_facts)
    signal = next(iter(facts["actuation"]["lateral"]["signals"]))
    facts["actuation"]["lateral"]["signals"][signal] = "accel"
    with pytest.raises(ValueError, match="not defined for a lateral"):
        apply_vehicle_info(deepcopy(control_analyses), load_vehicle_info(facts))


def test_underdetermined_checksum_cannot_be_used_for_control(
    control_analyses, declared_facts
):
    from autodistill_can.manual import apply_vehicle_info

    uncertain = next(
        analysis for analysis in control_analyses
        if analysis.checksum is not None and analysis.checksum.underdetermined
        and analysis.counter is not None
    )
    payload_signal = next(
        signal.default_name(uncertain.addr) for signal in uncertain.signals
        if signal.kind not in ("counter", "checksum", "constant")
    )
    facts = deepcopy(declared_facts)
    facts["actuation"]["lateral"].update({
        "bus": uncertain.bus,
        "address": uncertain.addr,
        "message": uncertain.name,
        "signals": {payload_signal: "apply_torque"},
    })
    with pytest.raises(ValueError, match="checksum solution is underdetermined"):
        apply_vehicle_info(deepcopy(control_analyses), load_vehicle_info(facts))


def test_secondary_bus_control_uses_its_own_packer_and_exact_cadence(
    control_analyses, log, declared_facts, tmp_path
):
    camera = next(
        analysis for analysis in control_analyses
        if analysis.bus != 0 and analysis.counter is not None
        and analysis.checksum is not None and not analysis.checksum.underdetermined
    )
    payload_signal = next(
        signal.default_name(camera.addr) for signal in camera.signals
        if signal.kind not in ("counter", "checksum", "constant")
    )
    facts = deepcopy(declared_facts)
    facts["actuation"]["lateral"].update({
        "bus": camera.bus,
        "address": camera.addr,
        "message": camera.name,
        "frequency_hz": 60,
        "signals": {payload_signal: "apply_torque"},
    })
    write_port(
        deepcopy(control_analyses), build_fingerprint(log), tmp_path / "camera_ctl",
        brand="testcar", car_name="TEST CAR 2024",
        vehicle_info=load_vehicle_info(facts),
    )
    source = (tmp_path / "camera_ctl" / "carcontroller.py").read_text()
    assert f"{camera.bus}: CANPacker('testcar_generated_bus{camera.bus}')" in source
    assert f"self.packers[{camera.bus}].make_can_msg" in source
    cadence = re.search(
        r"if \(self\.frame \* (\d+)\) // (\d+) != ", source
    )
    assert cadence is not None
    numerator, denominator = map(int, cadence.groups())
    sends = [
        frame for frame in range(100)
        if (frame * numerator) // denominator
        != ((frame - 1) * numerator) // denominator
    ]
    assert len(sends) == 60


def test_steer_limits_are_zero_until_a_human_sets_them(port):
    source = (port / "values.py").read_text()
    assert re.search(r"STEER_MAX\s*=\s*0\b", source)
    assert re.search(r"ACCEL_MIN\s*=\s*0\b", source)
    assert re.search(r"ACCEL_MAX\s*=\s*0\b", source)


def test_readme_states_what_cannot_come_from_a_capture(port):
    readme = (port / "README.md").read_text()
    for phrase in ("safety model", "CarSpecs", "closed course"):
        assert phrase in readme


def test_readme_explains_how_to_open_the_dbc_in_cabana(port):
    readme = (port / "README.md").read_text()
    for phrase in (
        "A DBC is not a CAN stream",
        "candump file(s)",
        "Could not parse any CAN frames",
        "Manage DBC Files",
    ):
        assert phrase in readme


# --------------------------------------------------------------------------
# Signal mapping
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,length,kind,expected",
    [
        # The trap: "STEERING_WHEEL_ANGLE" contains "wheel", and a naive speed
        # pattern claims it -- giving a port that thinks it is moving at 40 m/s
        # while parked.
        ("STEERING_WHEEL_ANGLE", 16, "physical", "ret.steeringAngleDeg"),
        ("WHEEL_SPEED_FL", 16, "physical", "ret.vEgoRaw"),
        ("SPEED_KPH", 16, "physical", "ret.vEgoRaw"),
        # A pressure in bar is not a CarState bool and must remain unmapped.
        ("BRAKE_PRESSURE", 16, "physical", None),
        ("BRAKE_PRESSED", 1, "bool", "ret.brakePressed"),
        ("CRUISE_SET_SPEED", 8, "physical", "ret.cruiseState.speed"),
        ("STEER_TORQUE_DRIVER", 12, "physical", "ret.steeringTorque"),
        # Correlation names a derivative channel D_<x>_DT, never "rate".
        ("D_STEER_ANGLE_DEG_DT", 16, "physical", "ret.steeringRateDeg"),
        # A multi-position stalk field ("0=off/1=left/2=right/3=hazard") is not
        # the leftBlinker bool it superficially resembles: bool(2) and bool(3)
        # are both True, so a right or hazard state would misreport as left.
        ("LEFT_BLINKER_STALK", 3, "physical", None),
        ("LEFT_TURN_SIGNAL", 1, "bool", "ret.leftBlinker"),
        ("DOOR_OPEN_STATUS", 4, "enum", None),
        # A multi-bit seatbelt reading is not the latched/unlatched flag.
        ("SEATBELT_LATCHED", 2, "physical", None),
        ("CRUISE_ENABLED", 8, "physical", None),
        ("CRUISE_MAIN_ON", 1, "bool", "ret.cruiseState.available"),
    ],
)
def test_signal_names_map_to_the_right_carstate_field(name, length, kind, expected):
    hit = next(
        (h.target for h in _CARSTATE_HINTS if h.matches(name, length, kind)), None
    )
    assert hit == expected


def test_unnamed_signals_are_never_mapped(port):
    """A placeholder name says nothing about meaning.

    Guessing a field's purpose from its bit position would be worse than
    leaving it blank, because it would look like knowledge.
    """
    source = (port / "carstate.py").read_text()
    assigned = re.findall(r'ret\.\w[\w.]* = cp\.vl\["\w+"\]\["(\w+)"\]', source)
    for name in assigned:
        assert not name.startswith(("SIG_", "UNKNOWN_", "CONST_", "FLAG_", "ENUM_"))


def test_speed_is_converted_from_reference_units_to_meters_per_second(port):
    source = (port / "carstate.py").read_text()
    assert "ret.vEgoRaw" in source
    assert "* CV.KPH_TO_MS" in source


def test_generated_carstate_uses_only_current_schema_fields(port):
    source = (port / "carstate.py").read_text()
    assert "ret.gas =" not in source
    assert "ret.brake =" not in source
    assert "ret.gearShifter =" not in source


def test_legacy_fingerprint_contains_only_the_selected_main_bus(port):
    source = (port / "fingerprints.py").read_text()
    fingerprint_block = source.split("FINGERPRINTS =", 1)[1].split(
        "FW_VERSIONS =", 1
    )[0]
    assert "physical bus 0" in fingerprint_block
    assert "bus 2" not in fingerprint_block
    # Synthetic bus 2's only address is 0x2E4 (740).
    assert "740: 8" not in fingerprint_block


def test_firmware_uses_request_address_and_groups_versions(port):
    source = (port / "fingerprints.py").read_text()
    assert "# (Ecu.unknown, 0x7E0, None)" in source
    assert "(Ecu.unknown, 0x7E8, None)" not in source
    assert source.count("# (Ecu.unknown, 0x7E0, None)") == 1
    assert "FW_VERSIONS = {}" in source


def test_machine_readable_status_keeps_control_gated(port):
    status = json.loads((port / "port_status.json").read_text())
    assert status["mode"] == "read_only"
    assert status["safe_for_control"] is False
    assert status["human_required"]
    assert status["dbc_files"] == {
        "0": "testcar_generated.dbc",
        "2": "testcar_generated_bus2.dbc",
    }


def test_write_port_refuses_to_overwrite_without_force(
    analyses, log, tmp_path
):
    destination = tmp_path / "port"
    ordered = list(analyses.values())
    fingerprint = build_fingerprint(log)
    write_port(ordered, fingerprint, destination, brand="safe", car_name="SAFE")
    hand_edit = destination / "carstate.py"
    hand_edit.write_text("# human edit\n")

    with pytest.raises(FileExistsError):
        write_port(
            ordered, fingerprint, destination, brand="safe", car_name="SAFE"
        )
    assert hand_edit.read_text() == "# human edit\n"

    write_port(
        ordered, fingerprint, destination, brand="safe", car_name="SAFE",
        force=True,
    )
    assert "class CarState" in hand_edit.read_text()


def test_multibus_duplicate_addresses_get_separate_dbcs(
    analyses, log, tmp_path
):
    ordered = list(analyses.values())
    original = next(a for a in ordered if a.bus == 0)
    ordered.append(replace(original, bus=2))
    destination = tmp_path / "duplicate"
    write_port(
        ordered, build_fingerprint(log), destination,
        brand="multi", car_name="MULTI",
    )
    main = cantools.database.load_file(destination / "multi_generated.dbc")
    other = cantools.database.load_file(destination / "multi_generated_bus2.dbc")
    assert main.get_message_by_frame_id(original.addr)
    assert other.get_message_by_frame_id(original.addr)


def test_a_named_signal_on_an_unwired_third_bus_is_skipped_not_fatal(
    analyses, log
):
    """Only two ``CANParser`` instances are ever generated (main and camera),

    so a message on a third bus a multi-bus capture happened to see cannot be
    read no matter how well a signal's name matches a CarState hint. That used
    to reach ``PortSpec.parser_for`` and raise, aborting the whole port over a
    coincidental name match on traffic the port was never going to use.
    """
    ordered = list(analyses.values())
    original = next(a for a in ordered if a.bus == 0 and a.signals)
    stray_signal = replace(
        original.signals[0], name="DOOR_OPEN_FL", length=1, kind="bool",
    )
    stray = replace(
        original, bus=5, addr=original.addr + 1000, signals=[stray_signal],
    )
    spec = PortSpec(
        brand="multi3", car_name="MULTI3",
        analyses=[*ordered, stray],
        fingerprint=build_fingerprint(log),
        main_bus=0, camera_bus=2,
    )
    bindings = carstate_bindings(spec)  # must not raise
    assert not any(b.analysis.bus == 5 for b in bindings)


def test_a_manual_signal_override_on_an_unwired_bus_becomes_a_todo(analyses, log):
    """The human-directed equivalent of the above: explained, not fatal."""
    ordered = list(analyses.values())
    original = next(a for a in ordered if a.bus == 0 and a.signals)
    stray_signal = replace(
        original.signals[0], name="MY_DOOR", length=1, kind="bool",
        carstate_target="ret.doorOpen",
    )
    stray = replace(
        original, bus=5, addr=original.addr + 1000, signals=[stray_signal],
    )
    spec = PortSpec(
        brand="multi3b", car_name="MULTI3B",
        analyses=[*ordered, stray],
        fingerprint=build_fingerprint(log),
        main_bus=0, camera_bus=2,
    )
    bindings = carstate_bindings(spec)  # must not raise
    assert not any(b.target == "ret.doorOpen" and b.analysis.bus == 5
                   for b in bindings)
    assert any("bus 5" in todo for todo in spec.todos)


def test_generated_identifiers_are_valid_even_for_hostile_names(
    analyses, log, tmp_path
):
    destination = tmp_path / "odd"
    write_port(
        analyses.values(), build_fingerprint(log), destination,
        brand="123-class", car_name='2024 "quoted" car',
    )
    for path in destination.glob("*.py"):
        compile(path.read_text(), path.name, "exec")
    values = (destination / "values.py").read_text()
    assert "CAR_2024_QUOTED_CAR = PlatformConfig" in values
    assert "opendbc.car.car_123_class" in (
        destination / "interface.py"
    ).read_text()


def test_manual_vehicle_facts_are_carried_without_enabling_control(
    analyses, log, tmp_path
):
    destination = tmp_path / "manual-port"
    fingerprint = build_fingerprint(log)
    request_address = fingerprint.firmware[0].request_addr
    info = load_vehicle_info({
        "vehicle_specs": {
            "mass_kg": 1845,
            "wheelbase_m": 2.766,
            "steer_ratio": 14.3,
            "docs_package": "All",
        },
        "ecu_types": {f"0x{request_address:X}": "engine"},
        "engineering": {
            "actuation_notes": ["OEM workshop manual section 12"],
            "safety_notes": ["Bench validation still required"],
            "sources": ["Measured on vehicle VIN ending 1234"],
        },
    })
    write_port(
        deepcopy(list(analyses.values())), fingerprint, destination,
        brand="manual", car_name="MANUAL CAR", vehicle_info=info,
    )

    values = (destination / "values.py").read_text()
    fingerprints = (destination / "fingerprints.py").read_text()
    status = json.loads((destination / "port_status.json").read_text())
    assert "CarSpecs(mass=1845.0, wheelbase=2.766, steerRatio=14.3)" in values
    assert "package='All'" in values
    assert f"(Ecu.engine, 0x{request_address:X}, None)" in fingerprints
    assert "# MANUAL" in fingerprints
    assert (destination / "vehicle_info.json").is_file()
    assert "not inferred" in (destination / "MANUAL_INPUT.md").read_text()
    assert status["manual_input"]["provided"] is True
    assert status["manual_input"]["ecu_types_supplied"] == 1
    assert status["safe_for_control"] is False
    assert "SafetyModel.noOutput" in (destination / "interface.py").read_text()
    assert "can_sends = []" in (destination / "carcontroller.py").read_text()


def test_the_camera_bus_is_parsed_not_just_written(port):
    """A DBC nobody reads is a file, not a feature.

    The camera bus was analysed and written out as its own DBC long before
    anything referenced it, which meant a CarState field bound to a
    camera-bus signal generated code that raised on the first frame.
    """
    source = (port / "carstate.py").read_text()
    assert "cam = can_parsers[Bus.cam]" in source
    assert "camera_messages = [" in source
    assert "Bus.cam: CANParser(DBC[CP.carFingerprint][Bus.cam]" in source


def test_a_camera_bus_binding_reads_from_the_camera_parser(analyses, log, tmp_path):
    from autodistill_can.analysis.fingerprint import build_fingerprint
    from autodistill_can.emit.port import write_port

    ordered = deepcopy(list(analyses.values()))
    camera = next(a for a in ordered if a.bus == 2 and a.signals)
    signal = camera.signals[0].default_name(camera.addr)
    info = load_vehicle_info({"carstate": [{
        "target": "ret.brakePressed", "bus": camera.bus,
        "address": camera.addr, "signal": signal, "transform": "bool",
    }]})
    write_port(
        ordered, build_fingerprint(log), tmp_path / "port",
        brand="testcar", car_name="TEST CAR", vehicle_info=info, log=log,
    )
    source = (tmp_path / "port" / "carstate.py").read_text()
    assert f'ret.brakePressed = bool(cam.vl["{camera.name}"]["{signal}"])' in source


def test_a_single_bus_capture_gets_no_camera_wiring(analyses, log, tmp_path):
    from autodistill_can.analysis.fingerprint import build_fingerprint
    from autodistill_can.emit.port import write_port

    main_only = [deepcopy(a) for a in analyses.values() if a.bus == 0]
    write_port(
        main_only, build_fingerprint(log), tmp_path / "port",
        brand="testcar", car_name="TEST CAR",
    )
    values = (tmp_path / "port" / "values.py").read_text()
    carstate = (tmp_path / "port" / "carstate.py").read_text()
    assert "Bus.cam" not in values
    assert "Bus.cam" not in carstate


# --------------------------------------------------------------------------
# Firmware tables: being a good citizen in opendbc, not just a valid one
# --------------------------------------------------------------------------


def _fingerprint_without_firmware(log):
    from autodistill_can.analysis.fingerprint import build_fingerprint

    fingerprint = build_fingerprint(log)
    fingerprint.firmware.clear()
    return fingerprint


def test_unidentified_ecus_never_reach_fw_versions(port):
    """The bug this guards is not in our port, it is in everyone else's.

    openpilot skips a missing *non-essential* ECU when matching firmware, and
    an unidentified ECU is never essential. A FW_VERSIONS entry made only of
    unidentified ECUs can therefore never be ruled out: it matches every car in
    opendbc. Installing one broke fingerprinting for 460 unrelated vehicles.
    """
    source = (port / "fingerprints.py").read_text()
    assert "FW_VERSIONS = {}" in source
    live = [
        line for line in source.splitlines()
        if not line.lstrip().startswith("#") and "(Ecu.unknown," in line
    ]
    assert not live, live
    # The observations are kept, just not as a live table.
    assert "Observed, waiting on identification" in source


def test_observed_ecus_are_queried_as_extra_ecus(port):
    """A brand that declares a query config must query at least one ECU.

    `get_brand_ecu_matches` builds a list per brand and openpilot asserts it is
    non-empty. Unidentified addresses go in `extra_ecus`: queried, never
    matched, which is exactly their status.
    """
    source = (port / "values.py").read_text()
    assert "extra_ecus=[" in source
    assert "(Ecu.unknown, 0x" in source


def test_the_two_firmware_tables_are_declared_together(analyses, log, tmp_path):
    """opendbc indexes FW_QUERY_CONFIGS[brand] for every brand in VERSIONS.

    Declaring one without the other raises KeyError inside fingerprinting for
    every car in the database. The two must appear, or not appear, together.
    """
    from autodistill_can.emit.port import write_port

    for name, fingerprint in (
        ("with", build_fingerprint(log)),
        ("without", _fingerprint_without_firmware(log)),
    ):
        directory = tmp_path / name
        write_port(
            deepcopy(list(analyses.values())), fingerprint, directory,
            brand="testcar", car_name="TEST CAR",
        )
        values = (directory / "values.py").read_text()
        fingerprints = (directory / "fingerprints.py").read_text()
        declares_config = "FW_QUERY_CONFIG = " in values
        declares_versions = bool(
            re.search(r"^FW_VERSIONS = ", fingerprints, re.M)
        )
        assert declares_config == declares_versions, name


def test_fw_query_config_declares_a_version_regex(port):
    """`FwQueryConfig.fw_version_regex` is a required field in current opendbc.

    It has no default, so a `FW_QUERY_CONFIG = FwQueryConfig(requests=[...])`
    without it raises `TypeError` on import -- not a generated-code smell, an
    exception at the moment openpilot tries to construct this exact port.
    """
    source = (port / "values.py").read_text()
    assert "FW_QUERY_CONFIG = " in source
    assert "fw_version_regex=" in source


def test_a_capture_without_firmware_declares_neither(analyses, log, tmp_path):
    from autodistill_can.emit.port import write_port

    write_port(
        deepcopy(list(analyses.values())), _fingerprint_without_firmware(log),
        tmp_path / "port", brand="testcar", car_name="TEST CAR",
    )
    assert "FW_QUERY_CONFIG = " not in (tmp_path / "port" / "values.py").read_text()
    fingerprints = (tmp_path / "port" / "fingerprints.py").read_text()
    assert not re.search(r"^FW_VERSIONS = ", fingerprints, re.M)


def test_an_identified_essential_ecu_produces_a_real_table(analyses, log, tmp_path):
    from autodistill_can.analysis.fingerprint import build_fingerprint
    from autodistill_can.emit.port import write_port

    fingerprint = build_fingerprint(log)
    assert fingerprint.firmware, "fixture needs firmware responses"
    address = fingerprint.firmware[0].request_addr
    info = load_vehicle_info({"ecu_types": {f"0x{address:X}": "engine"}})
    write_port(
        deepcopy(list(analyses.values())), fingerprint, tmp_path / "port",
        brand="testcar", car_name="TEST CAR", vehicle_info=info,
    )
    source = (tmp_path / "port" / "fingerprints.py").read_text()
    assert "FW_VERSIONS = {}" not in source
    assert f"(Ecu.engine, 0x{address:X}, None)" in source
    # Still-unidentified addresses stay commented out rather than going live.
    assert re.search(r"^\s*# \(Ecu\.unknown, 0x", source, re.M)


def test_a_non_essential_ecu_alone_is_not_enough(analyses, log, tmp_path):
    """`combinationMeter` is skippable during matching, so it cannot anchor."""
    from autodistill_can.analysis.fingerprint import build_fingerprint
    from autodistill_can.emit.port import write_port

    fingerprint = build_fingerprint(log)
    address = fingerprint.firmware[0].request_addr
    info = load_vehicle_info({"ecu_types": {f"0x{address:X}": "combinationMeter"}})
    write_port(
        deepcopy(list(analyses.values())), fingerprint, tmp_path / "port",
        brand="testcar", car_name="TEST CAR", vehicle_info=info,
    )
    assert "FW_VERSIONS = {}" in (tmp_path / "port" / "fingerprints.py").read_text()
