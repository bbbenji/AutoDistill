"""What a finished openpilot port needs, and which parts are still blank.

A capture answers "where are the fields and what do they contain". It cannot
answer "which field is the brake switch", "what torque may openpilot apply", or
"which message actuates the steering rack". Those come from a person with the
car, a service document, or bench time.

This module is the list of everything in that second category, so the gap can
be shown as a checklist instead of discovered one traceback at a time. Each
requirement says what it is for -- reading the car, steering it, or driving it
longitudinally -- so a port can be honestly described as complete for one and
not the others.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

__all__ = [
    "GEAR_SHIFTERS",
    "REQUIREMENTS",
    "Coverage",
    "CoverageItem",
    "Requirement",
    "requirements_for",
]

#: What a requirement unlocks. A port that satisfies only ``read`` is a
#: dashcam/logging port; ``lateral`` adds steering, ``longitudinal`` adds
#: gas and brake.
Need = Literal["read", "lateral", "longitudinal"]

#: openpilot's GearShifter enum, for mapping a raw gear signal onto it.
GEAR_SHIFTERS = (
    "unknown", "park", "drive", "neutral", "reverse", "sport", "low",
    "brake", "eco", "manumatic",
)


@dataclass(frozen=True)
class Requirement:
    """One thing a port needs that a capture alone cannot supply."""

    key: str
    title: str
    #: "carstate" | "spec" | "actuation" | "limit" | "tuning" | "safety"
    kind: str
    need: Need
    detail: str
    #: How to actually obtain it. Telling someone a field is missing is not
    #: much use if they do not know where the answer comes from, and "find the
    #: brake switch" is a different job from "measure the wheelbase".
    how: str = ""
    #: False when openpilot works without it, but works better with it.
    mandatory: bool = True


#: The general method behind most CarState bindings, so each field only has to
#: describe what is specific about it.
_ISOLATE = (
    "Record a short capture doing this one thing repeatedly with pauses "
    "between, then look for the field that changes in step with it."
)


def _carstate(key: str, title: str, detail: str, how: str = "",
              *, need: Need = "read", mandatory: bool = True) -> Requirement:
    return Requirement(key, title, "carstate", need, detail, how, mandatory)


#: Ordered as a person would work through them.
REQUIREMENTS: tuple[Requirement, ...] = (
    # ---- CarState: what openpilot reads every 10ms -----------------------
    _carstate("ret.vEgoRaw", "Vehicle speed",
              "Wheel speed or vehicle speed in m/s. Everything else keys off it.",
              "Drive with a GPS track recording, pass it as --reference, and "
              "AutoDistill names this field and recovers its scale itself. "
              "Failing that, hold a steady indicated speed and find the field "
              "holding a proportional value. Check the unit: a field reading "
              "6234 at 62.3 km/h is 0.01 km/h per bit, so use the kph_to_ms "
              "transform."),
    _carstate("ret.steeringAngleDeg", "Steering angle",
              "Degrees, positive left.",
              "Turn the wheel slowly lock to lock while parked. The field that "
              "sweeps smoothly and symmetrically about a centre value is this "
              "one. Confirm the sign: openpilot wants positive to the left, so "
              "if your field goes negative turning left, negate the scale."),
    _carstate("ret.steeringTorque", "Driver steering torque",
              "Used to detect that the driver is holding the wheel.",
              "With the engine running and the wheels straight, push the wheel "
              "left and right without letting it turn. The field that responds "
              "to force rather than movement is driver torque. It is often in "
              "the same message as the angle."),
    _carstate("ret.steeringRateDeg", "Steering rate",
              "Degrees per second.",
              "Usually beside the angle in the same message. Turn the wheel at "
              "varying speed: this field tracks how fast, not how far, and "
              "returns to zero whenever you stop moving.", mandatory=False),
    _carstate("ret.steeringTorqueEps", "EPS motor torque",
              "Torque the power steering is applying.",
              "Drive with the stock lane-keep active on a curve. The field that "
              "rises as the car steers itself, while your own torque stays at "
              "zero, is the motor's.", mandatory=False),
    _carstate("ret.gasPressed", "Accelerator pressed",
              "Boolean. Disengages openpilot's longitudinal control.",
              f"{_ISOLATE} Blip the accelerator in park. Prefer a dedicated "
              "switch bit if there is one; if only a pedal position exists, "
              "bind that with the threshold transform just above its resting "
              "value."),
    _carstate("ret.gas", "Accelerator position",
              "0-1 fraction.",
              "Sweep the pedal slowly from rest to the floor. Use the percent "
              "transform if the field reads 0-100, or scale if it reads raw "
              "counts.", mandatory=False),
    _carstate("ret.brakePressed", "Brake pressed",
              "Boolean. Disengages openpilot.",
              f"{_ISOLATE} Press the brake five times with gaps. Most cars have "
              "a brake switch bit that goes high with the brake lights; that is "
              "better than a pressure reading, which idles at a non-zero value "
              "and would need a threshold."),
    _carstate("ret.brake", "Brake position",
              "0-1 fraction.",
              "Press the pedal progressively. This is usually a master-cylinder "
              "pressure in bar, so use scale to normalise its working range to "
              "0-1.", mandatory=False),
    _carstate("ret.gearShifter", "Gear selector",
              "Mapped onto openpilot's GearShifter enum; openpilot only "
              "engages in drive.",
              "Foot on the brake, engine running, move the selector through "
              "every position in turn, pausing in each. Note the raw value at "
              "each stop and write them into gear_map. Include every position "
              "the car has, including manual gates."),
    _carstate("ret.cruiseState.enabled", "Cruise engaged",
              "Boolean. Stock cruise state openpilot mirrors.",
              "Engage stock cruise on a quiet road and cancel it a few times. "
              "This bit follows the engaged state, not the main switch, and is "
              "usually in the same message as the set speed."),
    _carstate("ret.cruiseState.available", "Cruise main on",
              "Boolean. The main cruise switch.",
              f"{_ISOLATE} Press the main cruise button on and off while "
              "stationary. This bit stays high while the system is available, "
              "whether or not it is engaged."),
    _carstate("ret.cruiseState.speed", "Cruise set speed",
              "m/s.",
              "Engage cruise and step the set speed up and down. The field that "
              "moves in the same increments is this one. It is nearly always in "
              "the car's display unit, so use kph_to_ms or mph_to_ms, and check "
              "which by switching the dash between units."),
    _carstate("ret.doorOpen", "Door open", "Boolean. Blocks engagement.",
              f"{_ISOLATE} Open and close each door in turn. Cars usually have "
              "one bit per door: bind any of them and OR the rest in by hand, "
              "or bind the combined 'any door open' bit if there is one."),
    _carstate("ret.seatbeltUnlatched", "Seatbelt unlatched",
              "Boolean. Blocks engagement.",
              f"{_ISOLATE} Buckle and unbuckle the driver's belt. Check the "
              "sense: many cars report *latched*, which needs the invert_bool "
              "transform."),
    _carstate("ret.leftBlinker", "Left indicator", "Boolean.",
              f"{_ISOLATE} Use the left indicator, then the right, so you can "
              "tell the two bits apart. Note that hazards set both."),
    _carstate("ret.rightBlinker", "Right indicator", "Boolean.",
              f"{_ISOLATE} Use the right indicator on its own, having already "
              "found the left, so the two cannot be confused. The bit is "
              "usually adjacent to the left one in the same message."),
    _carstate("ret.leftBlindspot", "Left blindspot",
              "Boolean, if the car has BSM.",
              "Only if the car has blind-spot monitoring. Have someone walk "
              "past the rear quarter, or capture on a busy road and look for a "
              "bit that pulses as vehicles pass.", mandatory=False),
    _carstate("ret.rightBlindspot", "Right blindspot",
              "Boolean, if the car has BSM.",
              "Only if the car has blind-spot monitoring. Same method as the "
              "left side, and normally the neighbouring bit in the same "
              "message: have someone walk past the rear quarter, or capture on "
              "a busy road.", mandatory=False),
    _carstate("ret.espDisabled", "Stability control disabled",
              "Boolean.",
              f"{_ISOLATE} Press and hold the traction-control button until the "
              "dashboard light comes on.", mandatory=False),

    # ---- CarSpecs: measurable, not observable ----------------------------
    Requirement("specs.mass", "Kerb mass", "spec", "read",
                "Kilograms including a driver.",
                "The specifications page of the owner's manual, or the "
                "manufacturer's brochure for your exact trim. The door-jamb "
                "sticker usually gives gross vehicle weight, which is a "
                "different and much larger number. A public weighbridge with a "
                "full tank is the direct measurement. Check how the openpilot "
                "platform you are copying from treats occupant weight and "
                "match it."),
    Requirement("specs.wheelbase", "Wheelbase", "spec", "read", "Metres.",
                "Published for every car; also directly measurable with a tape "
                "from front hub centre to rear hub centre, on both sides, "
                "averaged."),
    Requirement("specs.steerRatio", "Steering ratio", "spec", "read",
                "Steering wheel degrees per degree at the road wheels.",
                "Sometimes published as e.g. '14.3:1'. To measure it: park on a "
                "smooth surface with the wheels straight, turn the steering "
                "wheel exactly one full turn, and measure how far the road "
                "wheel turned with an angle gauge against the wheel face. The "
                "ratio is 360 divided by that angle. openpilot also learns this "
                "while driving, so a close starting value is enough."),
    Requirement("specs.centerToFront", "Centre of mass to front axle", "spec",
                "lateral", "Metres. Defaults to half the wheelbase.",
                "Weigh each axle separately at a weighbridge, then multiply the "
                "wheelbase by the fraction of the total on the *rear* axle. "
                "Leave it out and half the wheelbase is assumed, which is close "
                "enough for most cars to start with.",
                mandatory=False),
    Requirement("specs.tireStiffnessFactor", "Tyre stiffness factor", "spec",
                "lateral", "Scales the bicycle model.",
                "A tuning factor, not a measurement. Leave it out unless a "
                "similar platform in opendbc sets one; it is adjusted later if "
                "the car consistently under- or over-turns for a given "
                "commanded curvature.",
                mandatory=False),
    Requirement("specs.harness", "Comma harness", "spec",
                "lateral", "The comma harness connector for this car.",
                "Check comma's shop or opendbc CarHarness enum for the harness "
                "matching this car model (e.g., hyundai_k, toyota, honda_n)."),

    # ---- Actuation: the messages openpilot has to send -------------------
    Requirement("control.lateral", "Steering command message", "actuation",
                "lateral",
                "The message and signals that command the steering rack, with "
                "which signal carries torque or angle.",
                "This is the hardest fact to establish and the one worth most "
                "care. First check opendbc for the same OEM platform -- these "
                "are shared across model years and a sibling port may already "
                "name it. Otherwise: the stock lane-keep camera sends it, so "
                "capture both buses with the camera connected, drive with "
                "lane-keep active, and find the message that only the camera "
                "originates and whose payload tracks the steering the car "
                "applies to itself. Confirm on a bench before ever sending it."),
    Requirement("control.longitudinal", "Acceleration command message",
                "actuation", "longitudinal",
                "The message and signals that command gas and brake.",
                "Found the same way as the steering command, from a capture "
                "with the stock adaptive cruise following a car. Many platforms "
                "do longitudinal by spamming the stock cruise buttons instead, "
                "which is simpler and often safer; check what comparable "
                "opendbc ports do before assuming this message is needed."),
    Requirement("control.cruise_buttons", "Cruise button message", "actuation",
                "longitudinal",
                "Only needed when longitudinal is done by spamming stock "
                "cruise buttons.",
                "Press each cruise button in turn while capturing and note the "
                "value each sends. The message is usually on the steering-wheel "
                "or body bus and is short and event-driven.", mandatory=False),

    # ---- Limits and tuning: how hard openpilot may push ------------------
    Requirement("limits.steer_max", "Maximum steer command", "limit", "lateral",
                "In the units of the steering signal.",
                "Read it off the stock system. Capture the factory lane-keep "
                "working hard -- a tight motorway curve -- and take the largest "
                "magnitude the command signal ever reaches. AutoDistill reports "
                "each signal's observed range, so this is a number you can look "
                "up rather than guess. Never exceed what the factory system "
                "commands."),
    Requirement("limits.steer_delta_up", "Steer ramp-up limit", "limit",
                "lateral", "Maximum increase per frame.",
                "From the same capture: the largest frame-to-frame *increase* "
                "the stock system ever produces in that signal. Decode the "
                "capture with the generated DBC and take the maximum positive "
                "difference between consecutive frames."),
    Requirement("limits.steer_delta_down", "Steer ramp-down limit", "limit",
                "lateral", "Maximum decrease per frame.",
                "From the same capture, for the largest decrease. It is "
                "usually allowed to be larger than the ramp-up limit, because "
                "releasing torque is the safe direction."),
    Requirement("limits.steer_driver_allowance", "Driver override threshold",
                "limit", "lateral",
                "Driver torque tolerated before openpilot backs off.",
                "With stock lane-keep active on a curve, gradually resist the "
                "wheel while capturing. The driver torque value at which the "
                "stock system gives up or reduces its command is the threshold "
                "the car itself uses."),
    Requirement("limits.steer_actuator_delay", "Steering actuator delay",
                "limit", "lateral", "Seconds between command and response.",
                "Decode a capture of the stock system steering and cross-"
                "correlate the command signal against the steering angle rate: "
                "the lag at peak correlation is the delay. Typically 0.1-0.4s. "
                "Start at the value a comparable opendbc platform uses if you "
                "cannot measure it yet."),
    Requirement("limits.accel_min", "Maximum commanded deceleration", "limit",
                "longitudinal", "Most-negative acceleration in m/s².",
                "Capture the stock adaptive cruise braking behind slower "
                "traffic and use no more deceleration than its command reaches. "
                "Cross-check the same platform in opendbc and keep the safety "
                "model's limit at least as strict."),
    Requirement("limits.accel_max", "Maximum commanded acceleration", "limit",
                "longitudinal", "Most-positive acceleration in m/s².",
                "Capture the stock adaptive cruise accelerating from a low set "
                "speed and use no more acceleration than its command reaches. "
                "Cross-check the value against the safety model and validate it "
                "on a closed course."),
    Requirement("tuning.lateral", "Lateral tuning", "tuning", "lateral",
                "torque, pid, or angle, with its gains.",
                "Use the same control type as comparable platforms in opendbc: "
                "'angle' if the car accepts a steering angle directly, 'torque' "
                "otherwise. Torque tuning needs lateral acceleration and "
                "steering torque logged together over varied driving; "
                "openpilot's tuning tools fit the two constants from those "
                "logs. Copying a similar platform's values as a starting point "
                "is normal and expected."),
    Requirement("tuning.longitudinal", "Longitudinal tuning", "tuning",
                "longitudinal", "Gains and acceleration limits.",
                "Start from a comparable opendbc platform and adjust on a "
                "closed course. The acceleration limits should be no wider than "
                "what the stock adaptive cruise uses, which you can read off a "
                "capture of it following traffic."),

    # ---- Safety: the layer that actually stops a bad command -------------
    Requirement("safety.model", "opendbc safety model", "safety", "lateral",
                "The reviewed safety mode in opendbc/safety that bounds every "
                "message openpilot sends.",
                "Look in opendbc/safety/modes/ for your manufacturer. If a mode "
                "exists and already bounds the exact messages you send, name "
                "it. If it does not, writing one is the real work: it is C, it "
                "must reject anything outside the limits above, it needs tests, "
                "and it has to be reviewed upstream. Naming a model here does "
                "not create it."),
)

#: Requirements that hold for every level above the one they are declared at.
_IMPLIES: dict[str, tuple[Need, ...]] = {
    "read": ("read",),
    "lateral": ("read", "lateral"),
    "longitudinal": ("read", "lateral", "longitudinal"),
}


#: What a single recording of the stock driver-assist working answers. Each of
#: these says "capture the stock system" on its own, which reads as eight
#: separate expeditions; it is one drive with lane-keep active, and then
#: decoding. Worth saying, because it is the difference between the control
#: path looking impossible and looking like an afternoon.
STOCK_CAPTURE_ANSWERS: tuple[str, ...] = (
    "control.lateral",
    "control.longitudinal",
    "control.cruise_buttons",
    "limits.steer_max",
    "limits.steer_delta_up",
    "limits.steer_delta_down",
    "limits.steer_driver_allowance",
    "limits.steer_actuator_delay",
    "limits.accel_min",
    "limits.accel_max",
    "tuning.longitudinal",
)


def requirements_for(need: Need) -> tuple[Requirement, ...]:
    """Every requirement that a port at this level has to satisfy."""
    wanted = _IMPLIES[need]
    return tuple(item for item in REQUIREMENTS if item.need in wanted)


@dataclass(frozen=True)
class CoverageItem:
    """One requirement, and how (or whether) it was met."""

    requirement: Requirement
    #: "auto" recovered from the capture, "manual" supplied by a person,
    #: "derived" computed from another field, or "missing".
    status: str
    source: str = ""

    @property
    def met(self) -> bool:
        return self.status != "missing"


@dataclass(frozen=True)
class Coverage:
    """The whole checklist, evaluated against one analysis plus its facts."""

    items: tuple[CoverageItem, ...]

    def __iter__(self):
        return iter(self.items)

    def by_key(self, key: str) -> CoverageItem | None:
        return next((item for item in self.items if item.requirement.key == key), None)

    def met(self, key: str) -> bool:
        item = self.by_key(key)
        return item is not None and item.met

    def missing(self, need: Need, *, mandatory_only: bool = True) -> list[Requirement]:
        """Requirements still unmet for a port at this level."""
        wanted = _IMPLIES[need]
        return [
            item.requirement
            for item in self.items
            if item.requirement.need in wanted
            and not item.met
            and (item.requirement.mandatory or not mandatory_only)
        ]

    def complete_for(self, need: Need) -> bool:
        return not self.missing(need)

    def ratio(self, need: Need) -> tuple[int, int]:
        """(met, total) mandatory requirements at this level."""
        wanted = _IMPLIES[need]
        relevant = [
            item for item in self.items
            if item.requirement.need in wanted and item.requirement.mandatory
        ]
        return sum(item.met for item in relevant), len(relevant)

    def to_dict(self) -> dict:
        met_read, total_read = self.ratio("read")
        met_lat, total_lat = self.ratio("lateral")
        met_long, total_long = self.ratio("longitudinal")
        return {
            "read": {"met": met_read, "total": total_read,
                     "complete": self.complete_for("read")},
            "lateral": {"met": met_lat, "total": total_lat,
                        "complete": self.complete_for("lateral")},
            "longitudinal": {"met": met_long, "total": total_long,
                             "complete": self.complete_for("longitudinal")},
            "items": [
                {
                    "key": item.requirement.key,
                    "title": item.requirement.title,
                    "kind": item.requirement.kind,
                    "need": item.requirement.need,
                    "mandatory": item.requirement.mandatory,
                    "detail": item.requirement.detail,
                    "how": item.requirement.how,
                    "status": item.status,
                    "source": item.source,
                }
                for item in self.items
            ],
        }


#: What to add to vehicle-info.json to satisfy each kind of requirement.
#: Printed next to a missing item so the answer to "how do I fix this" is the
#: line itself rather than a section of documentation to go and find.
_SNIPPETS = {
    "carstate": (
        '"carstate": [{{"target": "{key}", "bus": 0, "address": "0xNNN", '
        '"signal": "SIGNAL_NAME", "transform": "identity"}}]'
    ),
    "spec": '"vehicle_specs": {{"{short}": <number>}}',
    "actuation": (
        '"actuation": {{"{short}": {{"bus": 0, "address": "0xNNN", '
        '"message": "NAME", "frequency_hz": 100, '
        '"signals": {{"TORQUE_SIGNAL": "apply_torque"}}}}}}'
    ),
    "limit": '"limits": {{"{short}": <number>}}',
    "tuning": (
        '"tuning": {{"lateral": {{"kind": "torque", "max_lateral_accel": <number>, '
        '"friction": <number>}}}}'
    ),
    "safety": '"safety": {{"model": "<opendbc SafetyModel name>"}}',
}

_SPEC_KEYS = {
    "specs.mass": "mass_kg",
    "specs.wheelbase": "wheelbase_m",
    "specs.steerRatio": "steer_ratio",
    "specs.centerToFront": "center_to_front_m",
    "specs.tireStiffnessFactor": "tire_stiffness_factor",
    "specs.harness": "harness",
}

#: The transform a field usually needs, so the suggested fragment is closer to
#: the answer than to a form. Overridable -- it is only a starting point.
SUGGESTED_TRANSFORM = {
    "ret.vEgoRaw": "kph_to_ms",
    "ret.cruiseState.speed": "kph_to_ms",
    "ret.gearShifter": "gear_map",
    "ret.gas": "percent",
    "ret.brake": "percent",
}


def suggested_transform(key: str) -> str:
    if key in SUGGESTED_TRANSFORM:
        return SUGGESTED_TRANSFORM[key]
    item = next((r for r in REQUIREMENTS if r.key == key), None)
    if item is not None and item.detail.startswith("Boolean"):
        return "bool"
    return "identity"


def snippet_for(requirement: Requirement) -> str:
    """A ready-to-paste vehicle-info fragment that would satisfy this item."""
    if requirement.key == "control.cruise_buttons":
        return (
            '"engineering": {"actuation_notes": ['
            '"Cruise-button control needs hand-written cancel/resume mapping"]}'
        )
    template = _SNIPPETS.get(requirement.kind, "")
    short = _SPEC_KEYS.get(
        requirement.key, requirement.key.split(".", 1)[-1]
    )
    fragment = template.format(key=requirement.key, short=short)
    if requirement.kind == "carstate":
        transform = suggested_transform(requirement.key)
        fragment = fragment.replace('"transform": "identity"', f'"transform": "{transform}"')
        if transform == "gear_map":
            fragment = fragment.replace(
                "}]", ', "gear_map": {"0": "park", "1": "reverse", "2": "neutral", '
                      '"3": "drive"}}]'
            )
    return fragment


def build_coverage(
    *,
    auto: dict[str, str],
    manual: dict[str, str],
    derived: dict[str, str] | None = None,
    extra: Iterable[Requirement] = (),
) -> Coverage:
    """Assemble a checklist from what each source of evidence provided.

    ``auto``/``manual``/``derived`` map a requirement key to a short,
    human-readable description of where the value came from. A key present in
    more than one wins in the order manual, auto, derived: a person's answer
    overrides a guess made from a signal name.
    """
    derived = derived or {}
    items = []
    for requirement in (*REQUIREMENTS, *extra):
        key = requirement.key
        if key in manual:
            items.append(CoverageItem(requirement, "manual", manual[key]))
        elif key in auto:
            items.append(CoverageItem(requirement, "auto", auto[key]))
        elif key in derived:
            items.append(CoverageItem(requirement, "derived", derived[key]))
        else:
            items.append(CoverageItem(requirement, "missing"))
    return Coverage(tuple(items))
