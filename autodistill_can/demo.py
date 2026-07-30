"""Build a complete ``vehicle-info.json`` for a capture this package made itself.

``autodistill-can synth`` invents a car with known ground truth so the
analysis pipeline can be checked against an answer nobody hand-tuned to match.
This module goes one step further: given an *already analysed* synthetic
capture, it picks a set of facts -- from what was actually recovered -- that
satisfies every mandatory requirement in :mod:`.requirements`: CarState
bindings, vehicle specs, a lateral *and* a longitudinal command message,
limits, tuning, and a safety model. Fed back into ``port``, the result is a
port that is complete at all three levels ``requirements`` scores: read,
steer, and drive.

None of this claims to be a *realistic* car -- the steering "torque" is
whichever counter-and-checksum-carrying message on the camera bus AutoDistill
actually recovered, and the acceleration "command" is the next one it found on
the main bus. What is real is the pipeline: the checksum function this
produces is verified against the capture that was just generated, the
generated ``carcontroller.py`` really does build and send that message, and
every opendbc API a real port would exercise gets exercised the same way.
``port_status.json`` still reports ``safe_for_control: false`` -- nothing here
changes that, and nothing could: no capture, synthetic or real, makes a port
safe.

Facts are chosen from the analysis rather than hard-coded to the synthetic
car's field names, so this keeps working if :mod:`.synth` changes.
``tools/control_facts.py`` is the same idea, run in CI against a fresh
synthetic capture on every push -- it now calls this module rather than
keeping its own copy.
"""

from __future__ import annotations

from .analysis.correlate import correlate_log, load_reference_csv
from .analysis.message import MessageAnalysis, analyse_log, detect_byte_order
from .analysis.signals import Signal
from .frame import CanLog
from .requirements import REQUIREMENTS
from .sources import open_source

__all__ = ["analyse_for_demo_facts", "build_demo_facts"]

#: target -> the largest value that target may plausibly reach, so a scaled
#: binding stays inside the range `validate` accepts from a real car.
_CEILINGS = {
    "ret.vEgoRaw": 30.0, "ret.cruiseState.speed": 30.0,
    "ret.gas": 1.0, "ret.brake": 1.0, "ret.steeringAngleDeg": 400.0,
}


def analyse_for_demo_facts(
    capture: str, reference: str,
) -> tuple[list[MessageAnalysis], CanLog]:
    """Analyse exactly as the CLI does, so the facts built from this validate.

    Running `analyse_message` per stream instead skips the whole-car byte-order
    decision, which can split a payload differently and name signals
    differently -- producing facts the pipeline then rejects because the
    fields they reference do not exist.
    """
    log = CanLog.from_frames(open_source(capture))
    streams = log.sorted_streams()
    lsb_first = detect_byte_order(streams, min_frames=40)
    analyses = analyse_log(log, min_frames=40, lsb_first=lsb_first)
    correlate_log(log.streams, analyses, load_reference_csv(reference))
    return analyses, log


def _command_candidate(
    analyses: list[MessageAnalysis], *, bus: int, taken: set[tuple[int, int]],
) -> MessageAnalysis | None:
    """A message on ``bus``, not already used, provable enough to command.

    "Provable" means a rolling counter and a checksum were both recovered and
    verified against the capture -- the same bar a real command message has
    to clear before AutoDistill will wire it into a generated controller.
    """
    return next(
        (
            a for a in analyses
            if a.bus == bus and a.key not in taken
            and a.counter is not None and a.checksum is not None
            and any(s.kind not in ("counter", "checksum") for s in a.signals)
        ),
        None,
    )


def _payload_signal(analysis: MessageAnalysis) -> Signal:
    return next(
        s for s in analysis.signals if s.kind not in ("counter", "checksum")
    )


def build_demo_facts(analyses: list[MessageAnalysis], log: CanLog) -> dict:
    """A `vehicle-info.json`-shaped dict complete at the read/steer/drive levels."""
    # The camera bus is where a real stock lane-keep camera's command would be
    # seen, so it is tried first for lateral; falling back to the main bus
    # keeps this working for a single-bus capture too.
    lateral = (
        _command_candidate(analyses, bus=2, taken=set())
        or _command_candidate(analyses, bus=0, taken=set())
    )
    if lateral is None:
        raise ValueError(
            "no message with both a counter and a checksum was found to act "
            "as a command; drive longer or vary more signals"
        )
    longitudinal = (
        _command_candidate(analyses, bus=0, taken={lateral.key})
        or _command_candidate(analyses, bus=2, taken={lateral.key})
    )
    if longitudinal is None:
        raise ValueError(
            "only one message with both a counter and a checksum was found; "
            "a complete lateral+longitudinal demo needs a second"
        )
    lateral_signal = _payload_signal(lateral)
    longitudinal_signal = _payload_signal(longitudinal)

    # Decoded ranges, so the bindings below can be given transforms that put
    # each field inside what a real car could produce -- and so `validate`
    # (which `port` runs automatically) has nothing to flag as impossible.
    decoded: list[tuple] = []
    for analysis in analyses:
        if analysis.bus != 0:
            continue
        stream = log.streams[(analysis.bus, analysis.addr)]
        for signal in analysis.signals:
            if signal.kind in ("counter", "checksum", "constant"):
                continue
            values = [
                raw * signal.scale + signal.offset
                for raw in signal.values(stream)
            ]
            decoded.append((analysis, signal, min(values), max(values), values))

    flags = [row for row in decoded if row[1].length == 1] or decoded
    positive = [row for row in decoded if row[2] >= 0 and row[3] > 0] or decoded

    carstate, flag_index, number_index = [], 0, 0
    for requirement in (
        r for r in REQUIREMENTS if r.kind == "carstate" and r.mandatory
    ):
        boolean = requirement.detail.startswith("Boolean")
        if requirement.key == "ret.gearShifter":
            analysis, signal, _, _, values = positive[number_index % len(positive)]
            number_index += 1
            # Map values the capture actually contains, so drive really occurs.
            seen = sorted({int(v) for v in values})[:3]
            gears = ("drive", "park", "reverse")
            binding = {
                "transform": "gear_map",
                "gear_map": {str(v): gears[i] for i, v in enumerate(seen)},
            }
        elif boolean:
            analysis, signal, _, _, _ = flags[flag_index % len(flags)]
            flag_index += 1
            binding = {"transform": "bool"}
        else:
            analysis, signal, low, high, _ = positive[number_index % len(positive)]
            number_index += 1
            ceiling = _CEILINGS.get(requirement.key)
            if ceiling is not None and high > 0:
                binding = {"transform": "scale", "scale": round(ceiling / high, 6)}
            else:
                binding = {"transform": "identity"}
        carstate.append({
            "target": requirement.key,
            "bus": analysis.bus,
            "address": analysis.addr,
            "signal": signal.default_name(analysis.addr),
            **binding,
        })

    return {
        "schema_version": 1,
        "vehicle_specs": {
            "mass_kg": 1845, "wheelbase_m": 2.766, "steer_ratio": 14.3,
            "docs_package": "All", "harness": "hyundai_k",
        },
        "carstate": carstate,
        "actuation": {
            "lateral": {
                "bus": lateral.bus,
                "address": f"0x{lateral.addr:X}",
                "message": lateral.name,
                "frequency_hz": 100,
                "signals": {
                    lateral_signal.default_name(lateral.addr): "apply_torque",
                },
                "counter_signal": "COUNTER",
                "checksum_signal": "CHECKSUM",
            },
            "longitudinal": {
                "bus": longitudinal.bus,
                "address": f"0x{longitudinal.addr:X}",
                "message": longitudinal.name,
                "frequency_hz": 50,
                "signals": {
                    longitudinal_signal.default_name(longitudinal.addr): "accel",
                },
                "counter_signal": "COUNTER",
                "checksum_signal": "CHECKSUM",
            },
        },
        "limits": {
            "steer_max": 384, "steer_delta_up": 3, "steer_delta_down": 7,
            "steer_driver_allowance": 50, "steer_actuator_delay": 0.1,
            "accel_min": -3.5, "accel_max": 2.0,
        },
        "tuning": {
            "lateral": {
                "kind": "torque", "max_lateral_accel": 2.5, "friction": 0.1,
            },
            # kp is accepted for older files but no longer wired into
            # generated code -- opendbc moved it under a `deprecated` group
            # and no current brand sets it, so it is left out here.
            "longitudinal": {"ki": 0.05},
        },
        "safety": {"model": "hyundai"},
    }
