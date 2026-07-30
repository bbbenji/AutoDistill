"""Generate a complete read-only openpilot car-port bootstrap from a capture.

What comes out is a directory the bundled installer integrates into opendbc,
with every brand-local file openpilot expects and every fact we could establish
from the traffic already filled in: the fingerprint, firmware versions,
per-bus DBCs, and — the part that is otherwise days of work — checksum and
counter code verified against the capture.

What it deliberately does **not** do is invent a control strategy.
:func:`_carcontroller` emits a controller that sends nothing. Steering torque
limits, rate limits and the safety model are not derivable from watching a bus,
and a plausible-looking guess at them is worse than an obvious blank: it invites
someone to try it. The generated port is for *reading* a car until a human fills
in the actuation and opendbc's safety model has been written and reviewed.

The layout follows opendbc's current API (``Platforms``/``PlatformConfig``,
``CarStateBase.update(can_parsers)``, ``Bus.main``), so the output is checked
against the real thing rather than against an idea of it.
"""

from __future__ import annotations

import io
import json
import keyword
import os
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable

from ..analysis.fingerprint import Fingerprint, is_diagnostic_address
from ..analysis.message import MessageAnalysis
from ..analysis.signals import Signal
from ..manual import ESSENTIAL_ECU_TYPES, VehicleInfo, apply_vehicle_info
from ..requirements import REQUIREMENTS
from ..transforms import KPH_TO_MS, MPH_TO_MS, RAD_TO_DEG
from ..transforms import apply as apply_transform
from ..transforms import render as render_transform
from .dbc import write_dbc
from .openpilot import write_checksum_function

__all__ = ["PortSpec", "carstate_bindings", "write_port"]

_NON_PROTECTIVE_SAFETY_MODELS = {
    "allOutput", "elm327", "gmPassive", "noOutput", "silent",
}

@dataclass(frozen=True)
class _FieldHint:
    """How to recognise one openpilot CarState field among recovered signals.

    Matching on the name alone is not safe. "STEERING_WHEEL_ANGLE" contains the
    word *wheel*, so a pattern looking for wheel speed claims the steering angle
    and assigns it to ``vEgoRaw`` — a port that believes it is doing 40 m/s
    while parked. Hence ``avoid``, and hence ``wants_bool``: "BRAKE_PRESSURE" is
    a pressure in bar, not the ``brakePressed`` flag it superficially resembles.
    """

    pattern: str
    target: str
    note: str
    #: Rejected even if ``pattern`` matches.
    avoid: str = ""
    #: None accepts any field; True wants a single-bit flag, False a number.
    wants_bool: bool | None = None
    #: Conversion required before assigning the DBC value to CarState.
    conversion: str = "identity"

    def matches(self, name: str, length: int, kind: str) -> bool:
        if not re.search(self.pattern, name, re.I):
            return False
        if self.avoid and re.search(self.avoid, name, re.I):
            return False
        if self.wants_bool is True and not (length == 1 or kind == "bool"):
            return False
        if self.wants_bool is False and (length == 1 or kind == "bool"):
            return False
        return True


#: Ordered most specific first, because the first match wins. Steering is
#: matched before speed so that a steering *wheel* signal cannot be taken for a
#: road *wheel* one.
_CARSTATE_HINTS: tuple[_FieldHint, ...] = (
    # Correlation names a derivative channel `D_<channel>_DT`, so the rate of
    # a steering-angle reference arrives as D_STEER_ANGLE_DEG_DT rather than
    # anything containing the word "rate".
    _FieldHint(r"steer.*rate|angle.*rate|^d_.*(steer|angle).*_dt",
               "ret.steeringRateDeg", "degrees per second", wants_bool=False,
               conversion="angle_rate"),
    _FieldHint(r"steer.*(angle|ang)|\bsas\b|angle.*steer", "ret.steeringAngleDeg",
               "degrees, positive left", wants_bool=False, conversion="angle"),
    _FieldHint(r"(steer|driver|eps).*torque|torque.*(steer|driver)",
               "ret.steeringTorque", "driver-applied torque", wants_bool=False),
    _FieldHint(r"wheel.?spe?e?d|whl.*spd", "ret.vEgoRaw",
               "m/s -- convert if the signal is km/h",
               avoid=r"steer|angle", wants_bool=False, conversion="speed"),
    _FieldHint(r"\bspeed|\bspd\b|vanz", "ret.vEgoRaw",
               "m/s -- convert if the signal is km/h",
               avoid=r"steer|angle|set|cruise|limit|wind|fan", wants_bool=False,
               conversion="speed"),
    _FieldHint(r"(gas|accel|throttle).*(pressed|switch|sw)\b",
               "ret.gasPressed", "bool", wants_bool=True, conversion="bool"),
    _FieldHint(r"brake.*(pressed|switch|sw)\b|pedal.*brake", "ret.brakePressed",
               "bool", wants_bool=True, conversion="bool"),
    _FieldHint(r"(left|lh).*(blink|turn|indic)|(blink|turn).*left",
               "ret.leftBlinker", "bool", wants_bool=True, conversion="bool"),
    _FieldHint(r"(right|rh).*(blink|turn|indic)|(blink|turn).*right",
               "ret.rightBlinker", "bool", wants_bool=True, conversion="bool"),
    _FieldHint(r"door.*open|open.*door", "ret.doorOpen", "bool",
               avoid=r"lock|handle", wants_bool=True, conversion="bool"),
    _FieldHint(r"seat.?belt.*(unlatch|unbuckle)|belt.*(unlatch|unbuckle)",
               "ret.seatbeltUnlatched", "bool", wants_bool=True, conversion="bool"),
    _FieldHint(r"seat.?belt.*(latch|buckle)|belt.*(latch|buckle)",
               "ret.seatbeltUnlatched", "bool -- inverted from latched",
               wants_bool=True, conversion="invert_bool"),
    _FieldHint(r"cruise.*set|set.*speed", "ret.cruiseState.speed", "m/s",
               wants_bool=False, conversion="speed"),
    _FieldHint(r"cruise.*(enab|activ|engag)|acc.*activ", "ret.cruiseState.enabled",
               "bool", wants_bool=True, conversion="bool"),
    _FieldHint(r"cruise.*(main|avail|on)\b", "ret.cruiseState.available", "bool",
               wants_bool=True, conversion="bool"),
)

_HEADER = '''# Generated by AutoDistill from a CAN capture.
#
# Signal names that came from correlation with a reference log are trustworthy.
# Names of the form SIG_<bit>_<width> are placeholders for fields whose meaning
# is still unknown -- they are real fields at real positions, but nobody has
# said what they mean yet.
'''


@dataclass
class PortSpec:
    """Everything needed to write a port, plus what could not be determined."""

    brand: str
    car_name: str
    analyses: list[MessageAnalysis]
    fingerprint: Fingerprint
    #: Bus the car's own powertrain traffic was seen on.
    main_bus: int = 0
    #: Bus carrying the stock camera, if the capture saw more than one.
    camera_bus: int | None = None
    #: Facts explicitly supplied by a person, kept distinct from inference.
    vehicle_info: VehicleInfo = field(default_factory=VehicleInfo)
    #: Collected as the port is written, and repeated in the README.
    todos: list[str] = field(default_factory=list)
    #: Set when the capture was replayed through the bindings.
    validation: object | None = None

    @property
    def dbc_name(self) -> str:
        return f"{self.brand}_generated"

    @property
    def camera_dbc_name(self) -> str:
        return f"{self.dbc_name}_bus{self.camera_bus}"

    def dbc_name_for(self, bus: int) -> str:
        return self.dbc_name if bus == self.main_bus else f"{self.dbc_name}_bus{bus}"

    @property
    def has_camera(self) -> bool:
        """Whether the capture saw a second bus worth wiring up.

        openpilot installs between the camera and the car, so the camera bus
        is where the messages it has to replace live. A port that cannot see
        it can read the car but never stand in for the stock system.
        """
        return self.camera_bus is not None and any(
            a.bus == self.camera_bus and a.length > 0 for a in self.analyses
        )

    def parser_for(self, bus: int) -> str:
        """Which generated CANParser a message on this bus is read from."""
        if bus == self.main_bus:
            return "cp"
        if self.has_camera and bus == self.camera_bus:
            return "cam"
        raise ValueError(
            f"bus {bus} is not wired into the generated CarState; choose it "
            "with camera_bus/--camera-bus or bind the signal on the main bus"
        )

    def control_ready(self, need: str) -> bool:
        """Whether generated control for one level may be live rather than inert."""
        command = self.vehicle_info.command(
            "longitudinal" if need == "longitudinal" else "lateral"
        )
        safety = self.vehicle_info.safety
        if command is None or safety is None or not safety.known:
            return False
        if safety.model in _NON_PROTECTIVE_SAFETY_MODELS:
            return False
        if self.validation is not None and not self.validation.ok:
            return False
        from ..coverage import evaluate

        return evaluate(
            self.analyses, self.vehicle_info,
            main_bus=self.main_bus, camera_bus=self.camera_bus,
        ).complete_for(need)

    @property
    def firmware_is_usable(self) -> bool:
        """Whether this port may take part in firmware fingerprinting.

        openpilot skips a *missing* non-essential ECU when matching firmware,
        and an unidentified ECU is never essential. So a ``FW_VERSIONS`` entry
        containing only unidentified ECUs can never be ruled out -- it matches
        every car in opendbc, and installing it breaks fingerprinting for
        vehicles that have nothing to do with this one.

        At least one essential ECU has to be identified before the entry is
        safe to publish.
        """
        identified = {
            self.vehicle_info.ecu_types[address]
            for response in self.fingerprint.firmware
            if (address := response.request_addr) in self.vehicle_info.ecu_types
        }
        return bool(identified & set(ESSENTIAL_ECU_TYPES))

    @property
    def firmware_addresses(self) -> list[int]:
        """Diagnostic addresses that answered, in order."""
        return sorted({r.request_addr for r in self.fingerprint.firmware})

    @property
    def declares_firmware(self) -> bool:
        """Whether this port joins opendbc's firmware machinery at all.

        The two tables have to be declared by the same set of brands, and a
        declaring brand must query at least one ECU -- ``get_brand_ecu_matches``
        builds a list per brand and openpilot's tests require it non-empty. A
        capture with no UDS responses can satisfy neither, so it declares
        nothing and is fingerprinted by its message list alone.
        """
        return bool(self.fingerprint.firmware)

    def message(self, addr: int) -> MessageAnalysis | None:
        return next(
            (a for a in self.analyses if a.bus == self.main_bus and a.addr == addr),
            None,
        )

    def message_on(self, bus: int, addr: int) -> MessageAnalysis | None:
        return next(
            (a for a in self.analyses if a.bus == bus and a.addr == addr), None
        )


def _identifier(text: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_]", "_", text).strip("_").upper()
    cleaned = re.sub(r"_+", "_", cleaned)
    cleaned = cleaned or "MYSTERY_CAR"
    if cleaned[0].isdigit() or keyword.iskeyword(cleaned.lower()):
        cleaned = f"CAR_{cleaned}"
    return cleaned


def _normalise_brand(text: str) -> str:
    cleaned = re.sub(r"[^0-9a-z_]", "_", text.lower()).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned) or "mystery"
    if cleaned[0].isdigit() or keyword.iskeyword(cleaned):
        cleaned = f"car_{cleaned}"
    return cleaned


def _unit_key(unit: str) -> str:
    return re.sub(r"[\s_.-]", "", unit.strip().lower())


def _signal_expression(
    analysis: MessageAnalysis,
    signal: Signal,
    hint: _FieldHint,
    parser: str = "cp",
) -> tuple[str, Callable[[float], Any]] | None:
    """A type/unit-safe CarState assignment, and the same thing as a function.

    ``CANParser`` has already applied the DBC scale and offset. This step only
    converts physical units and booleans into the schema expected by opendbc.
    Unknown units are not guessed: an omitted mapping is visible in the
    checklist, while a speed off by 3.6 looks plausible enough to be dangerous.

    The callable exists so :mod:`..validate` can reproduce exactly what the
    generated line computes. Returning both from one place is the only way the
    two stay in step -- a validator that skipped the unit conversion would
    report 120km/h as 120m/s and call a correct binding an error.
    """
    base = (
        f'{parser}.vl["{analysis.name}"]'
        f'["{signal.default_name(analysis.addr)}"]'
    )
    conversion = hint.conversion
    if conversion == "identity":
        return base, lambda value: value
    if conversion == "bool":
        return f"bool({base})", bool
    if conversion == "invert_bool":
        return f"not bool({base})", lambda value: not bool(value)

    def scaled(factor: float) -> Callable[[float], float]:
        return lambda value: value * factor

    unit = _unit_key(signal.unit)
    name = (signal.name or "").upper()
    if conversion == "speed":
        if unit in {"ms", "m/s", "mps", "meterpersecond", "meterspersecond"}:
            return base, lambda value: value
        if unit in {"kmh", "kph", "km/h"} or re.search(r"(?:^|_)K(?:M)?PH(?:_|$)", name):
            return f"{base} * CV.KPH_TO_MS", scaled(KPH_TO_MS)
        if unit in {"mph", "mi/h"} or re.search(r"(?:^|_)MPH(?:_|$)", name):
            return f"{base} * CV.MPH_TO_MS", scaled(MPH_TO_MS)
        return None
    if conversion in {"angle", "angle_rate"}:
        if unit in {"deg", "degree", "degrees", "degs", "degrees/s", "degs"}:
            return base, lambda value: value
        if "DEG" in name:
            return base, lambda value: value
        if unit in {"rad", "radian", "radians", "rad/s", "rads"}:
            return f"{base} * CV.RAD_TO_DEG", scaled(RAD_TO_DEG)
        return None
    raise AssertionError(f"unknown CarState conversion {conversion!r}")


@dataclass(frozen=True)
class _Binding:
    """One CarState field, and the expression that fills it."""

    analysis: MessageAnalysis
    signal: str
    target: str
    note: str
    expression: str
    #: "auto" if a signal name matched a hint, "manual" if a person said so.
    origin: str = "auto"
    #: What ``expression`` computes, as a function, so a capture can be
    #: replayed through it without generating and importing the port.
    evaluate: Callable[[float], Any] = lambda value: value

    @property
    def described(self) -> str:
        return f"{self.analysis.name}.{self.signal}"


def _explicit_expression(
    binding, analysis: MessageAnalysis, parser: str = "cp"
) -> str:
    """Turn a human-declared binding into a CarState assignment.

    The transform vocabulary is closed (see ``manual.CARSTATE_TRANSFORMS``), so
    this is a total function over valid input rather than an evaluator. The
    rendering itself lives in :mod:`..transforms`, beside the version that
    evaluates it, so the validator cannot drift from the generated code.
    """
    base = f'{parser}.vl["{analysis.name}"]["{binding.signal}"]'
    return render_transform(binding, base)


def carstate_bindings(spec: PortSpec) -> list[_Binding]:
    """Every CarState field this port can fill, and how.

    Three sources, in descending authority: a person's explicit
    ``carstate`` binding, a person's ``carstate_target`` on a signal override,
    and finally AutoDistill's own guess from a signal's name. A placeholder
    like ``SIG_16_8`` never qualifies for the last of those -- guessing meaning
    from bit position would be worse than leaving the field blank.
    """
    out: list[_Binding] = []
    claimed: set[str] = set()

    def parser_or_none(bus: int) -> str | None:
        """``parser_for``, but a message on an unwired bus is a skip, not a
        crash: only two ``CANParser`` instances are ever generated, so a
        message on a third bus a capture happened to see cannot be read no
        matter how well its name matches a CarState field."""
        try:
            return spec.parser_for(bus)
        except ValueError:
            return None

    by_key = {analysis.key: analysis for analysis in spec.analyses}
    for binding in spec.vehicle_info.carstate:
        analysis = by_key.get((binding.bus, binding.address))
        if analysis is None:  # already rejected by apply_vehicle_info
            continue
        parser = parser_or_none(analysis.bus)
        if parser is None:
            spec.todos.append(
                f"CarState binding {binding.target} points at "
                f"{analysis.name} on bus {analysis.bus}, which is neither "
                "the main bus nor the camera bus, so nothing reads it. Set "
                "--camera-bus, or bind a signal on one of the wired buses."
            )
            continue
        claimed.add(binding.target)
        out.append(_Binding(
            analysis=analysis,
            signal=binding.signal,
            target=binding.target,
            note=binding.note or "human-supplied binding",
            expression=_explicit_expression(binding, analysis, parser),
            origin="manual",
            evaluate=partial(apply_transform, binding),
        ))

    for analysis in spec.analyses:
        for signal in analysis.signals:
            if not signal.name or signal.kind in ("counter", "checksum", "constant"):
                continue
            if signal.carstate_target:
                if signal.carstate_target in claimed:
                    continue
                hint = next(
                    (row for row in _CARSTATE_HINTS
                     if row.target == signal.carstate_target),
                    None,
                )
                if hint is None:
                    # A target with no recognition rule of its own. It is still
                    # a field openpilot needs, so say what to do rather than
                    # dropping it silently.
                    spec.todos.append(
                        f"Manual mapping {analysis.name}.{signal.name} targets "
                        f"{signal.carstate_target}, which needs an explicit "
                        "transform; move it to the `carstate` section."
                    )
                    continue
                if hint.wants_bool is True and not (
                    signal.length == 1 or signal.kind == "bool"
                ):
                    spec.todos.append(
                        f"Manual mapping {analysis.name}.{signal.name} targets "
                        f"{hint.target}, which expects a boolean field; review it."
                    )
                    continue
                parser = parser_or_none(analysis.bus)
                if parser is None:
                    spec.todos.append(
                        f"Manual mapping {analysis.name}.{signal.name} targets "
                        f"{hint.target}, but bus {analysis.bus} is neither the "
                        "main bus nor the camera bus, so nothing reads it. Set "
                        "--camera-bus to wire it in."
                    )
                    continue
                built = _signal_expression(analysis, signal, hint, parser)
                if built is None:
                    spec.todos.append(
                        f"Manual mapping {analysis.name}.{signal.name} targets "
                        f"{hint.target}, but its unit {signal.unit!r} cannot be "
                        "converted safely; add a supported unit."
                    )
                    continue
                claimed.add(hint.target)
                out.append(_Binding(
                    analysis, signal.default_name(analysis.addr), hint.target,
                    f"{hint.note}; human-supplied mapping", built[0], "manual",
                    built[1],
                ))
                continue
            parser = parser_or_none(analysis.bus)
            if parser is None:
                continue
            for hint in _CARSTATE_HINTS:
                if hint.target in claimed:
                    continue
                if hint.matches(signal.name, signal.length, signal.kind):
                    built = _signal_expression(analysis, signal, hint, parser)
                    if built is None:
                        continue
                    claimed.add(hint.target)
                    out.append(_Binding(
                        analysis, signal.default_name(analysis.addr),
                        hint.target, hint.note, built[0], "auto", built[1],
                    ))
                    break
    return out


# --------------------------------------------------------------------------
# Individual files
# --------------------------------------------------------------------------


def _fw_query_config(spec: PortSpec) -> str:
    """The firmware query. Always emitted, even with an empty FW_VERSIONS.

    opendbc requires the two to be declared by exactly the same set of brands:
    ``test_missing_versions_and_configs`` compares the key sets directly, and
    ``match_fw_to_car`` indexes ``FW_QUERY_CONFIGS[brand]`` for every brand in
    ``VERSIONS``. Declaring one without the other raises ``KeyError`` deep
    inside fingerprinting -- for every car in the database, not just this one.

    So the pairing is structural, not an optimisation to skip. An empty
    ``FW_VERSIONS`` beside a live config is a brand that queries firmware and
    matches nothing, which is exactly the honest state of a port whose ECUs
    have not been identified yet.
    """
    if not spec.declares_firmware:
        return (
            "# No FW_QUERY_CONFIG and no FW_VERSIONS.\n"
            "#\n"
            "# opendbc requires both to be declared by the same brands, and a\n"
            "# declaring brand must query at least one ECU. This capture saw no\n"
            "# UDS responses, so there is nothing to query and nothing to match;\n"
            "# the car is fingerprinted by its message list alone. Run\n"
            "#   autodistill-can probe --interface can0 --i-own-this-vehicle\n"
            "# and regenerate to take part in firmware fingerprinting.\n"
        )

    # Addresses that answered but whose module is still unidentified are
    # queried, never matched. That is what `extra_ecus` is for, and it is also
    # what keeps this brand's ECU list non-empty, which openpilot requires of
    # any brand that declares a query config at all.
    extra = ""
    if not spec.firmware_is_usable:
        entries = ",\n    ".join(
            f"(Ecu.unknown, 0x{address:X}, None)"
            for address in spec.firmware_addresses
        )
        extra = f"""
  # Queried but not matched against: nobody has said which module each of
  # these is. Identify one in vehicle_info.json to fingerprint on it.
  extra_ecus=[
    {entries},
  ],"""
    return f'''FW_QUERY_CONFIG = FwQueryConfig(
  requests=[
    Request(
      [StdQueries.TESTER_PRESENT_REQUEST, StdQueries.UDS_VERSION_REQUEST],
      [StdQueries.TESTER_PRESENT_RESPONSE, StdQueries.UDS_VERSION_RESPONSE],
      bus={spec.main_bus},
    ),
  ],
  # The byte format a real firmware string takes is manufacturer-specific
  # (see any hand-written brand's own fw_version_regex) and not something a
  # capture reveals on its own, so this accepts anything rather than guess a
  # shape and risk rejecting a real one. Tighten it once the platform's actual
  # format is known.
  fw_version_regex=rb"[\\x00-\\xff]+",{extra}
)
'''


def _dbc_map(spec: PortSpec) -> str:
    """The platform's bus -> DBC mapping.

    openpilot sits between the camera and the car, so a port that never names
    the camera bus can read the vehicle but not stand in for the stock system.
    The capture already produced a DBC for it; this is what makes it reachable.
    """
    entries = [f"Bus.main: {spec.dbc_name!r}"]
    if spec.has_camera:
        entries.append(f"Bus.cam: {spec.camera_dbc_name!r}")
    return ", ".join(entries)


def _controller_params(spec: PortSpec) -> str:
    """``CarControllerParams``: the bounds on what openpilot may command.

    Supplied values are written as given. Absent ones stay at zero, which
    disables steering rather than substituting a number that looks measured.
    """
    limits = spec.vehicle_info.limits
    if not limits.lateral_complete:
        missing = [
            name for name, value in (
                ("steer_max", limits.steer_max),
                ("steer_delta_up", limits.steer_delta_up),
                ("steer_delta_down", limits.steer_delta_down),
                ("steer_driver_allowance", limits.steer_driver_allowance),
                ("steer_actuator_delay", limits.steer_actuator_delay),
            ) if value is None
        ]
        spec.todos.append(
            "Steering limits are incomplete (" + ", ".join(missing) +
            "); STEER_MAX stays 0, which sends no steering."
        )
    docstring = (
        '''"""Actuation limits, supplied by a person who measured them.

  None of these are readable from a bus: they describe how hard openpilot is
  allowed to push the car, not what the car reports. They still need review and
  closed-course validation before this platform is trusted.
  """'''
        if limits.lateral_complete else
        '''"""Actuation limits.

  Every value here is a TODO. None of them can be read off a bus: they describe
  how hard openpilot is allowed to push the car, not what the car reports. Copy
  the shape from a similar supported platform and tune on a closed course.
  """'''
    )

    def value(number: float | None, fallback: str = "0") -> str:
        return fallback if number is None else repr(number)

    if spec.vehicle_info.command("longitudinal") is not None and (
        limits.accel_min is None or limits.accel_max is None
    ):
        spec.todos.append(
            "Longitudinal acceleration limits are incomplete; ACCEL_MIN and "
            "ACCEL_MAX stay 0, so the generated controller cannot accelerate "
            "or brake. Supply both measured limits."
        )
    # Zero is the only defensible default. Generic -3.5/2.0 m/s² values look
    # plausible, but a capture says nothing about what this particular car or
    # its safety model permits.
    accel_min = value(limits.accel_min)
    accel_max = value(limits.accel_max)
    return f'''class CarControllerParams:
  {docstring}

  STEER_STEP = {limits.steer_step}
  STEER_MAX = {value(limits.steer_max)}
  STEER_DELTA_UP = {value(limits.steer_delta_up)}
  STEER_DELTA_DOWN = {value(limits.steer_delta_down)}
  STEER_DRIVER_ALLOWANCE = {value(limits.steer_driver_allowance)}
  STEER_DRIVER_MULTIPLIER = 1
  STEER_DRIVER_FACTOR = 1
  STEER_THRESHOLD = {value(limits.steer_driver_allowance)}

  ACCEL_MIN = {accel_min}
  ACCEL_MAX = {accel_max}

  def __init__(self, CP):
    pass'''


def _values(spec: PortSpec) -> str:
    car = _identifier(spec.car_name)
    info = spec.vehicle_info
    mass = info.mass_kg if info.mass_kg is not None else 1500
    wheelbase = info.wheelbase_m if info.wheelbase_m is not None else 2.7
    steer_ratio = info.steer_ratio if info.steer_ratio is not None else 15.0
    docs_package = info.docs_package or "TODO"
    missing_specs = [
        name
        for name, value in (
            ("mass", info.mass_kg),
            ("wheelbase", info.wheelbase_m),
            ("steering ratio", info.steer_ratio),
        )
        if value is None
    ]
    if missing_specs:
        spec.todos.append(
            "Replace the placeholder CarSpecs value(s) for "
            f"{', '.join(missing_specs)} with measured or published values."
        )
    if info.docs_package is None:
        spec.todos.append(
            "Set the CarDocs package/trim label in values.py."
        )
    spec_source = (
        "Values marked MANUAL came from vehicle_info.json; remaining values are "
        "non-zero placeholders."
        if any((info.mass_kg, info.wheelbase_m, info.steer_ratio))
        else "Non-zero placeholders keep the read-only interface constructible."
    )
    harness = info.harness
    docs_imports = ", CarHarness, CarParts" if harness else ""
    if harness:
        car_parts = f", car_parts=CarParts.common([CarHarness.{harness}])"
    else:
        car_parts = ""
        spec.todos.append(
            "Name the comma harness for this car in vehicle_info.json "
            "(vehicle_specs.harness). openpilot's own docs test refuses a "
            "supported platform without one, and it is also how anyone else "
            "knows which harness to buy."
        )

    extra_specs = "".join(
        f", {name}={value!r}"
        for name, value in (
            ("centerToFront", info.center_to_front_m),
            ("tireStiffnessFactor", info.tire_stiffness_factor),
        )
        if value is not None
    )
    car_docs_name = spec.car_name if re.search(r"\b\d{4}\b", spec.car_name) else f"{spec.car_name} 2021"
    return f'''{_HEADER}
from opendbc.car import Bus, CarSpecs, PlatformConfig, Platforms
from opendbc.car.structs import CarParams
from opendbc.car.docs_definitions import CarDocs{docs_imports}
from opendbc.car.fw_query_definitions import FwQueryConfig, Request, StdQueries

Ecu = CarParams.Ecu


{_controller_params(spec)}


class CAR(Platforms):
  {car} = PlatformConfig(
    [CarDocs({car_docs_name!r}, package={docs_package!r}{car_parts})],
    # These values cannot come from a CAN capture. Measure or look them up.
    #   mass         kerb mass in kg, including a driver
    #   wheelbase    metres
    #   steerRatio   steering wheel degrees per degree at the road wheels
    # {spec_source}
    # Every value still needs an independent human review before control work.
    CarSpecs(mass={mass!r}, wheelbase={wheelbase!r}, steerRatio={steer_ratio!r}{extra_specs}),
    {{{_dbc_map(spec)}}},
  )


{_fw_query_config(spec)}
DBC = CAR.create_dbc_map()
'''


def _fingerprints(spec: PortSpec) -> str:
    car = _identifier(spec.car_name)
    lines = [
        _HEADER,
        "from opendbc.car.structs import CarParams",
        f"from opendbc.car.{spec.brand}.values import CAR",
        "",
        "Ecu = CarParams.Ecu",
        "",
        "FINGERPRINTS = {",
        f"  CAR.{car}: [{{",
    ]
    messages = spec.fingerprint.buses.get(spec.main_bus, {})
    lines.append(f"    # physical bus {spec.main_bus}")
    entries = ", ".join(f"{a}: {l}" for a, l in sorted(messages.items()))
    lines.append(f"    {entries},")
    lines.append("  }],")
    lines.append("}")
    lines.append("")

    if spec.fingerprint.excluded:
        lines.append("# Left out of the fingerprint on purpose:")
        for addr, reason in sorted(spec.fingerprint.excluded.items()):
            lines.append(f"#   0x{addr:X}: {reason}")
        lines.append("")

    by_address: dict[int, list] = defaultdict(list)
    for response in spec.fingerprint.firmware:
        by_address[response.request_addr].append(response)

    def entry(addr: int, responses: list, live: bool) -> list[str]:
        payloads = list(dict.fromkeys(bytes(r.payload) for r in responses))
        ecu_type = spec.vehicle_info.ecu_types.get(addr, "unknown")
        is_live = live and (ecu_type != "unknown")
        mark = "" if is_live else "# "
        source = "  # MANUAL" if is_live else ""
        out = [f"    {mark}(Ecu.{ecu_type}, 0x{addr:X}, None): {payloads!r},{source}"]
        out += [
            f"    #   {r.did_name}: {r.text()!r} "
            f"(observed response on 0x{r.addr:X})"
            for r in responses
        ]
        return out

    if spec.firmware_is_usable:
        lines.append("FW_VERSIONS = {")
        lines.append(f"  CAR.{car}: {{")
        lines.append("    # ECU types marked MANUAL came from vehicle_info.json.")
        lines.append("    # Addresses whose module is still unidentified are")
        lines.append("    # commented out -- see the note below for why.")
        for addr, responses in sorted(by_address.items()):
            lines += entry(addr, responses, addr in spec.vehicle_info.ecu_types)
        lines.append("  },")
        lines.append("}")
        unresolved = sorted(set(by_address) - set(spec.vehicle_info.ecu_types))
        if unresolved:
            addresses = ", ".join(f"0x{addr:X}" for addr in unresolved)
            spec.todos.append(
                f"Identify the ECU type for firmware address(es) {addresses}, "
                "then regenerate to include them in FW_VERSIONS."
            )
    elif not spec.declares_firmware:
        # Declaring FW_VERSIONS without a FW_QUERY_CONFIG raises KeyError deep
        # inside opendbc's fingerprinting -- for every car, not just this one.
        # With no responses to query there is nothing to pair it with, so this
        # brand stays out of the firmware machinery entirely.
        lines.append("# No FW_VERSIONS: this capture saw no UDS responses, so")
        lines.append("# there is nothing to query and nothing to match. See the")
        lines.append("# note beside FW_QUERY_CONFIG in values.py.")
        lines.append("#")
        lines.append("# Collect them with:")
        lines.append(
            "#   autodistill-can probe --interface can0 --i-own-this-vehicle"
        )
        spec.todos.append(
            "No firmware versions were captured. openpilot fingerprints most "
            "cars by firmware; run `autodistill-can probe` to collect them."
        )
    else:
        # An entry whose ECUs are all `unknown` is worse than no entry at all.
        # openpilot skips a missing non-essential ECU when matching, and
        # `unknown` is never essential -- so nothing could ever rule this
        # platform out and it would match *every car in opendbc*, breaking
        # fingerprinting for vehicles that have nothing to do with this one.
        lines.append("# FW_VERSIONS is deliberately empty.")
        lines.append("#")
        lines.append("# openpilot ignores a missing non-essential ECU when it")
        lines.append("# matches firmware, and an unidentified ECU is never")
        lines.append("# essential. A table of nothing but unidentified ECUs")
        lines.append("# therefore matches every car in the database, not just")
        lines.append("# this one. Identify at least one of engine, eps, abs,")
        lines.append("# fwdRadar, fwdCamera or vsa in vehicle_info.json and")
        lines.append("# regenerate; until then this car is fingerprinted by its")
        lines.append("# message list alone.")
        lines.append("FW_VERSIONS = {}")
        if by_address:
            lines.append("")
            lines.append("# Observed, waiting on identification:")
            for addr, responses in sorted(by_address.items()):
                lines += entry(addr, responses, live=False)
            addresses = ", ".join(f"0x{addr:X}" for addr in sorted(by_address))
            spec.todos.append(
                f"Identify at least one essential ECU (engine, eps, abs, "
                f"fwdRadar, fwdCamera, vsa) among {addresses}. Until then "
                "FW_VERSIONS stays empty, because a table of unidentified "
                "ECUs would match every car in opendbc."
            )
    return "\n".join(lines) + "\n"


def _carstate(spec: PortSpec) -> str:
    mapped = carstate_bindings(spec)
    body: list[str] = []

    if mapped:
        for binding in mapped:
            body.append(f"    {binding.target} = {binding.expression}  # {binding.note}")
    else:
        body.append("    # No signal could be identified by name.")
        body.append("    # Record a drive alongside a reference log (GPS speed,")
        body.append("    # OBD-II PIDs) and re-run with --reference.")
        spec.todos.append(
            "No signals were identified, so CarState is empty. Capture a "
            "reference log and re-run with --reference."
        )

    mapped_targets = {binding.target for binding in mapped}

    # Fields openpilot derives rather than reads. They cost nothing when their
    # input is present and are a common omission in hand-written ports.
    derived: list[str] = []
    if "ret.steeringTorque" in mapped_targets:
        derived.append(
            "    ret.steeringPressed = abs(ret.steeringTorque) > STEER_THRESHOLD"
        )
    if "ret.vEgoRaw" in mapped_targets and "ret.cruiseState.standstill" not in mapped_targets:
        derived.append("    ret.cruiseState.standstill = ret.standstill")

    missing = [
        item.key for item in REQUIREMENTS
        if item.kind == "carstate" and item.key not in mapped_targets
    ]
    todo_lines = "\n".join(f"    #   {t}" for t in sorted(set(missing)))
    if missing:
        spec.todos.append(
            f"{len(set(missing))} CarState fields are still unset "
            "(see carstate.py); openpilot needs most of them."
        )

    gear_binding = next(
        (b for b in spec.vehicle_info.carstate if b.transform == "gear_map"), None
    )
    gear_block = ""
    if gear_binding is not None:
        rows = "\n".join(
            f"  {raw}: structs.CarState.GearShifter.{gear},"
            for raw, gear in sorted(gear_binding.gear_map.items())
        )
        gear_block = (
            "\n# Raw gear value -> openpilot's gear enum, supplied by a person\n"
            "# who watched the signal while moving the selector.\n"
            f"GEAR_MAP = {{\n{rows}\n}}\n"
        )

    threshold = spec.vehicle_info.limits.steer_driver_allowance
    threshold_block = ""
    if "ret.steeringTorque" in mapped_targets:
        if threshold is None:
            threshold = 100.0
            spec.todos.append(
                "STEER_THRESHOLD in carstate.py is a placeholder; measure the "
                "driver torque that should count as hands-on."
            )
        threshold_block = (
            "\n# Driver torque above which openpilot treats the wheel as held.\n"
            f"STEER_THRESHOLD = {threshold!r}\n"
        )

    def periodic(bus: int) -> list[MessageAnalysis]:
        return [
            a for a in spec.analyses
            if a.bus == bus
            and a.length > 0
            and not is_diagnostic_address(a.addr)
            and not a.is_event_driven
            and (a.frequency or 0) >= 1
        ]

    def message_list(bus: int, indent: str) -> str:
        return f",\n{indent}".join(
            f'("{a.name}", {max(1, round(a.frequency or 0))})'
            for a in periodic(bus)
        )

    checks = message_list(spec.main_bus, "      ")
    camera_block = ""
    if spec.has_camera:
        camera_block = f'''
    camera_messages = [
      {message_list(spec.camera_bus, "      ")}
    ]'''
    camera_binding = "\n    cam = can_parsers[Bus.cam]" if spec.has_camera else ""
    camera_parser = ""
    if spec.has_camera:
        camera_parser = (
            f",\n            Bus.cam: CANParser("
            f"DBC[CP.carFingerprint][Bus.cam], camera_messages, "
            f"{spec.camera_bus})"
        )

    return f'''{_HEADER}
from opendbc.can import CANParser
from opendbc.car import Bus, structs
from opendbc.car.common.conversions import Conversions as CV
from opendbc.car.interfaces import CarStateBase
from opendbc.car.{spec.brand}.values import DBC
{gear_block}{threshold_block}

class CarState(CarStateBase):
  def update(self, can_parsers) -> structs.CarState:
    cp = can_parsers[Bus.main]{camera_binding}
    ret = structs.CarState()

{chr(10).join(body)}

    # Still to fill in. openpilot will not engage without most of these:
{todo_lines or "    #   (none)"}

    ret.vEgo, ret.aEgo = self.update_speed_kf(ret.vEgoRaw)
    ret.standstill = ret.vEgoRaw < 0.01
{chr(10).join(derived)}

    return ret

  @staticmethod
  def get_can_parsers(CP):
    # (message, expected frequency in Hz). Frequencies are measured from the
    # capture; openpilot uses them to notice a message going missing.
    messages = [
      {checks}
    ]{camera_block}
    return {{
            Bus.main: CANParser(
                DBC[CP.carFingerprint][Bus.main], messages, {spec.main_bus}
            ){camera_parser}
        }}
'''


def _brandcan(spec: PortSpec) -> str:
    """Message-building helpers, including the verified checksum code."""
    buffer = io.StringIO()
    buffer.write(_HEADER)
    buffer.write(
        '\n"""Message construction helpers.\n\n'
        "The checksum functions below were recovered from the capture and are\n"
        "verified against every frame in it -- see the docstring on each for the\n"
        "count. They are the part of a port that is hardest to get right by hand,\n"
        "and the part a car silently rejects you for getting wrong.\n"
        '"""\n\n'
    )

    with_checksums = [a for a in spec.analyses if a.checksum is not None]
    address_counts = Counter(a.addr for a in with_checksums)
    for analysis in with_checksums:
        function_name = None
        if address_counts[analysis.addr] > 1:
            function_name = f"checksum_bus{analysis.bus}_{analysis.addr:03x}"
        write_checksum_function(analysis, buffer, function_name=function_name)

    counters = [a for a in spec.analyses if a.counter is not None]
    if counters:
        buffer.write("# Rolling counters. A message openpilot sends must carry the\n")
        buffer.write("# next value in sequence or the receiving ECU discards it.\n")
        buffer.write("COUNTERS = {\n")
        for analysis in counters:
            counter = analysis.counter
            assert counter is not None
            buffer.write(
                f"  ({analysis.bus}, 0x{analysis.addr:X}): "
                f"{{'start_bit': {counter.start}, 'length': {counter.length}, "
                f"'step': {counter.step}}},\n"
            )
        buffer.write("}\n\n")

    if not with_checksums:
        buffer.write(
            "# No checksums were found in this capture. That may be correct --\n"
            "# some platforms rely on the CAN controller's own CRC -- or it may\n"
            "# mean the messages that carry them were not exercised.\n"
        )
    return buffer.getvalue()


def _checksum_function_name(spec: PortSpec, analysis: MessageAnalysis) -> str:
    """The name `_brandcan` gave this message's checksum function.

    Two buses can carry the same address with different checksums, so the
    writer disambiguates by bus in that case. The controller has to ask for the
    same name it wrote.
    """
    duplicated = sum(
        1 for a in spec.analyses if a.checksum is not None and a.addr == analysis.addr
    ) > 1
    if duplicated:
        return f"checksum_bus{analysis.bus}_{analysis.addr:03x}"
    return f"checksum_{analysis.addr:03x}"


def _cadence(spec: PortSpec, message) -> tuple[list[str], str]:
    """How often to send a message, and the counter value to send with it.

    openpilot's controller runs at 100Hz, so a 50Hz message is every second
    frame. The counter's width and step come from the capture: a receiving ECU
    checks the sequence, and one that wraps at the wrong value is rejected as
    surely as a bad checksum.
    """
    # Preserve the declared rate exactly over time. ``frame % round(100 / hz)``
    # silently turns 40 Hz into 50 Hz and cannot represent rates such as 60 Hz
    # at all. Convert the JSON decimal through ``str`` so the generated integer
    # arithmetic has no accumulating floating-point drift.
    rate = Fraction(str(message.frequency_hz))
    numerator = rate.numerator
    denominator = rate.denominator * 100
    lines = []
    indent = "    "
    if numerator != denominator:
        lines.append(
            f"    if (self.frame * {numerator}) // {denominator} != "
            f"((self.frame - 1) * {numerator}) // {denominator}:"
        )
        indent = "      "
    if message.counter_signal:
        analysis = spec.message_on(message.bus, message.address)
        # apply_vehicle_info rejects a declaration without a recovered counter,
        # so none of these values is a guess by the time code generation starts.
        counter = analysis.counter
        modulus = 1 << counter.length
        increment = counter.step
        tick = (
            "self.frame"
            if numerator == denominator
            else f"((self.frame * {numerator}) // {denominator})"
        )
        expression = (
            f"{tick} % {modulus}" if increment == 1
            else f"({tick} * {increment}) % {modulus}"
        )
        lines.append(f"{indent}counter = {expression}")
    return lines, indent


def _command_block(
    spec: PortSpec, message, *, indent: str = "      "
) -> list[str]:
    """The lines that build and queue one outgoing message."""
    lines = [f"{indent}values = {{"]
    for name, value in message.signals:
        rendered = (
            {"enabled": "CC.enabled", "frame": "self.frame"}.get(value, value)
            if isinstance(value, str) else repr(value)
        )
        lines.append(f'{indent}  "{name}": {rendered},')
    if message.counter_signal:
        lines.append(f'{indent}  "{message.counter_signal}": counter,')
    if message.checksum_signal:
        lines.append(f'{indent}  "{message.checksum_signal}": 0,')
    lines.append(f"{indent}}}")

    if message.checksum_signal:
        analysis = spec.message_on(message.bus, message.address)
        function = _checksum_function_name(spec, analysis) if analysis else None
        lines += [
            f"{indent}# Pack once with a zeroed checksum field, checksum the",
            f"{indent}# bytes, then pack again. The checksum function is the one",
            f"{indent}# verified against this car's own captured frames.",
            f'{indent}_, data, _ = self.packers[{message.bus}].make_can_msg('
            f'"{message.message}", {message.bus}, values)',
            f'{indent}values["{message.checksum_signal}"] = '
            f"{spec.brand}can.{function}(0x{message.address:X}, data)",
        ]
    lines.append(
        f'{indent}can_sends.append(self.packers[{message.bus}].make_can_msg('
        f'"{message.message}", {message.bus}, values))'
    )
    return lines


def _lateral_block(spec: PortSpec, message, kind: str) -> list[str]:
    """Rate-limited steering, written out rather than imported.

    The limiting is the safety-relevant part of a controller, so it is spelled
    out at the point it is applied instead of hidden behind a helper: someone
    reviewing this port should be able to see what bounds the command without
    opening another file.
    """
    if kind == "angle":
        body = [
            "    # Angle control: limit how fast the commanded angle may move.",
            "    apply_angle = 0.0",
            "    if CC.latActive:",
            "      target = actuators.steeringAngleDeg",
            "      up = CarControllerParams.STEER_DELTA_UP",
            "      down = CarControllerParams.STEER_DELTA_DOWN",
            "      if target > self.apply_angle_last:",
            "        apply_angle = min(target, self.apply_angle_last + up)",
            "      else:",
            "        apply_angle = max(target, self.apply_angle_last - down)",
            "      apply_angle = min(max(apply_angle, -CarControllerParams.STEER_MAX),",
            "                        CarControllerParams.STEER_MAX)",
            "    self.apply_angle_last = apply_angle",
            "    lat_active = 1 if CC.latActive else 0",
        ]
    else:
        body = [
            "    # Torque control. Two bounds apply: an absolute maximum, and a",
            "    # per-frame ramp so a step in the target cannot become a step at",
            "    # the wheel. Driver torque above the allowance shrinks the",
            "    # ceiling, so holding the wheel wins.",
            "    apply_torque = 0",
            "    if CC.latActive:",
            "      ceiling = CarControllerParams.STEER_MAX",
            "      driver = abs(CS.out.steeringTorque)",
            "      if driver > CarControllerParams.STEER_DRIVER_ALLOWANCE:",
            "        ceiling = max(0, ceiling - (driver - CarControllerParams"
            ".STEER_DRIVER_ALLOWANCE) * CarControllerParams.STEER_DRIVER_FACTOR)",
            "      target = int(round(actuators.torque * CarControllerParams.STEER_MAX))",
            "      target = int(min(max(target, -ceiling), ceiling))",
            "      up = CarControllerParams.STEER_DELTA_UP",
            "      down = CarControllerParams.STEER_DELTA_DOWN",
            "      if target > self.apply_torque_last:",
            "        step = up if self.apply_torque_last >= 0 else down",
            "        apply_torque = int(min(target, self.apply_torque_last + step))",
            "      else:",
            "        step = up if self.apply_torque_last <= 0 else down",
            "        apply_torque = int(max(target, self.apply_torque_last - step))",
            "    self.apply_torque_last = apply_torque",
            "    lat_active = 1 if CC.latActive else 0",
        ]
    cadence, indent = _cadence(spec, message)
    return body + ["", *cadence, *_command_block(spec, message, indent=indent)]


def _longitudinal_block(spec: PortSpec, message) -> list[str]:
    cadence, indent = _cadence(spec, message)
    return [
        "",
        "    # Longitudinal. Acceleration is clamped to the declared envelope;",
        "    # gas and brake are its positive and negative halves.",
        "    accel = 0.0",
        "    if CC.longActive:",
        "      accel = min(max(actuators.accel, CarControllerParams.ACCEL_MIN),",
        "                  CarControllerParams.ACCEL_MAX)",
        "    gas = max(accel, 0.0)",
        "    brake = max(-accel, 0.0)",
        "    long_active = 1 if CC.longActive else 0",
        "",
        *cadence,
        *_command_block(spec, message, indent=indent),
    ]


def _carcontroller(spec: PortSpec) -> str:
    declared_lateral = spec.vehicle_info.command("lateral")
    declared_longitudinal = spec.vehicle_info.command("longitudinal")
    lateral = declared_lateral if spec.control_ready("lateral") else None
    longitudinal = (
        declared_longitudinal if spec.control_ready("longitudinal") else None
    )
    for purpose, declared, ready in (
        ("lateral", declared_lateral, lateral),
        ("longitudinal", declared_longitudinal, longitudinal),
    ):
        if declared is not None and ready is None:
            spec.todos.append(
                f"The {purpose} command is declared but its mandatory port "
                "facts, validation, or protective safety model are incomplete; "
                "carcontroller.py keeps that path inert. See coverage in "
                "port_status.json."
            )
    if lateral is None and longitudinal is None:
        return _inert_carcontroller(spec)

    tuning = spec.vehicle_info.lateral_tuning
    kind = tuning.kind if tuning is not None else "torque"
    blocks: list[str] = []
    state: list[str] = []
    if lateral is not None:
        blocks += _lateral_block(spec, lateral, kind)
        state.append(
            "    self.apply_angle_last = 0.0" if kind == "angle"
            else "    self.apply_torque_last = 0"
        )
    if longitudinal is not None:
        blocks += _longitudinal_block(spec, longitudinal)

    needs_can = any(
        m is not None and m.checksum_signal for m in (lateral, longitudinal)
    )
    can_import = f"\nfrom opendbc.car.{spec.brand} import {spec.brand}can" if needs_can else ""
    reported = (
        "    new_actuators.steeringAngleDeg = apply_angle"
        if kind == "angle" and lateral is not None else
        "    new_actuators.torque = apply_torque / max(CarControllerParams.STEER_MAX, 1)\n"
        "    new_actuators.torqueOutputCan = apply_torque"
        if lateral is not None else ""
    )
    if longitudinal is not None:
        reported += ("\n" if reported else "") + "    new_actuators.accel = accel"

    command_buses = sorted({
        message.bus for message in (lateral, longitudinal) if message is not None
    })
    packers = ", ".join(
        f"{bus}: CANPacker({spec.dbc_name_for(bus)!r})" for bus in command_buses
    )

    spec.todos.append(
        "Every line of carcontroller.py came from vehicle_info.json, not from "
        "the capture. Bench-test each message before a closed-course test, and "
        "never on a public road before the platform is upstreamed."
    )
    return f'''{_HEADER}
"""Control output, built from human-declared actuation facts.

Nothing here was inferred from the capture. The message addresses, the signals
inside them, and every limit came from `vehicle_info.json` -- from a person who
identified them on the car. AutoDistill only wired them together and attached
the checksum and counter code it verified against real frames.

That makes this code as correct as the facts it was given and no more. It has
never been run against the car it claims to control. Before it drives anything:

  1. `opendbc/safety/modes/{spec.brand}.h` must exist, bound every message this
     file sends, and have been reviewed. The safety layer is what stops a bad
     command, and it is not generated.
  2. Bench-test with the car powered but not driveable, and confirm the ECU
     accepts the frames (a wrong checksum or counter is silently discarded).
  3. Closed course, with a way to disengage, before anywhere else.
"""

from opendbc.can import CANPacker
from opendbc.car import Bus
from opendbc.car.interfaces import CarControllerBase{can_import}
from opendbc.car.{spec.brand}.values import CarControllerParams


class CarController(CarControllerBase):
  def __init__(self, dbc_names, CP):
    super().__init__(dbc_names, CP)
    # Keyed by the physical transmit bus. A camera/secondary-bus message is
    # not present in the main DBC and must never be packed with its packer.
    self.packers = {{{packers}}}
    self.params = CarControllerParams(CP)
{chr(10).join(state)}

  def update(self, CC, CS, now_nanos):
    actuators = CC.actuators
    can_sends = []

{chr(10).join(blocks)}

    new_actuators = CC.actuators.as_builder()
{reported}

    self.frame += 1
    return new_actuators, can_sends
'''


def _inert_carcontroller(spec: PortSpec) -> str:
    return f'''{_HEADER}
"""Control output. Deliberately inert.

`update` returns no CAN messages. That is not an oversight and not a stub left
half-finished: **nothing about how to actuate this car is derivable from a
capture.** Watching a bus tells you what the stock camera sends, not what
torque is safe, how fast it may ramp, or how the car behaves when a command is
rejected. A generated guess would look authoritative and be untested against
the one thing that matters.

Before this sends anything:

  1. Write and review a safety model in `opendbc/safety/modes/`, with tests.
     openpilot's safety layer is what stops a bad command reaching the car, and
     it is C code that must be reviewed by someone who knows the platform.
  2. Fill in `CarControllerParams` in values.py -- torque limits, ramp rates,
     driver-override thresholds.
  3. Build the control message here using the verified checksum and counter
     helpers in {spec.brand}can.py.
  4. Test on a closed course, with a way to disengage, and never on a public
     road until the platform is upstreamed and reviewed.
"""

from opendbc.can import CANPacker
from opendbc.car import Bus
from opendbc.car.interfaces import CarControllerBase
from opendbc.car.{spec.brand}.values import CarControllerParams


class CarController(CarControllerBase):
  def __init__(self, dbc_names, CP):
    super().__init__(dbc_names, CP)
    self.packer = CANPacker(dbc_names[Bus.main])
    if Bus.cam in dbc_names:
      self.camera_packer = CANPacker(dbc_names[Bus.cam])
    self.params = CarControllerParams(CP)

  def update(self, CC, CS, now_nanos):
    can_sends = []

    # TODO: build the steering (and, if applicable, longitudinal) command here.
    # See the module docstring for what has to be true first. Until then this
    # port is read-only, which is the correct state for an unreviewed platform.

    new_actuators = CC.actuators.as_builder()
    new_actuators.torque = 0.0
    new_actuators.torqueOutputCan = 0

    self.frame += 1
    return new_actuators, can_sends
'''


def _lateral_tuning_block(spec: PortSpec) -> str:
    tuning = spec.vehicle_info.lateral_tuning
    if tuning is None or not tuning.complete:
        spec.todos.append(
            "Lateral tuning is not set; the placeholder PID gains are zero and "
            "steer nothing. Measure them on a closed course."
        )
        return '''    # Keep the port constructible without pretending it has a tuned
    # lateral controller. These zero PID gains steer nothing.
    ret.lateralTuning.pid.kpBP = [0.]
    ret.lateralTuning.pid.kpV = [0.]
    ret.lateralTuning.pid.kiBP = [0.]
    ret.lateralTuning.pid.kiV = [0.]'''
    if tuning.kind == "torque":
        deadzone = tuning.steering_angle_deadzone_deg or 0.0
        return f'''    # Torque control, tuned by a person on this car.
    CarInterfaceBase.configure_torque_tune(
      candidate, ret.lateralTuning,
      steering_angle_deadzone_deg={deadzone!r},
    )
    ret.lateralTuning.torque.friction = {tuning.friction!r}
    ret.lateralTuning.torque.latAccelFactor = {tuning.max_lateral_accel!r}'''
    if tuning.kind == "angle":
        return "    ret.steerControlType = structs.CarParams.SteerControlType.angle"
    return f'''    # PID lateral tuning, supplied by a person.
    ret.lateralTuning.pid.kpBP = [0.]
    ret.lateralTuning.pid.kpV = [{tuning.kp!r}]
    ret.lateralTuning.pid.kiBP = [0.]
    ret.lateralTuning.pid.kiV = [{tuning.ki!r}]
    ret.lateralTuning.pid.kf = {(tuning.kf if tuning.kf is not None else 0.0)!r}'''


def _interface(spec: PortSpec) -> str:
    info = spec.vehicle_info
    limits = info.limits
    safety = info.safety
    lateral_control = spec.control_ready("lateral")
    long_control = spec.control_ready("longitudinal")
    control_active = lateral_control or long_control

    if not control_active:
        if safety is not None and not safety.known:
            spec.todos.append(
                f"Safety model {safety.model!r} is not one AutoDistill knows; "
                "the port stays on noOutput until the generator is updated or "
                "the model is independently wired by hand."
            )
        elif safety is not None and safety.model in _NON_PROTECTIVE_SAFETY_MODELS:
            spec.todos.append(
                f"Safety model {safety.model!r} does not protect a vehicle "
                "control port; the generated interface stays on noOutput."
            )
        safety_block = f'''    # TODO: `noOutput` sends nothing, which is the only safe default for a
    # platform whose complete, validated control facts and protective safety
    # model are not all present. Swap it only once the coverage manifest is
    # complete and opendbc/safety/modes/{spec.brand}.h has reviewed tests.
    ret.safetyConfigs = [get_safety_config(structs.CarParams.SafetyModel.noOutput)]
    ret.dashcamOnly = True'''
    else:
        param = (
            f"\n    ret.safetyConfigs[0].safetyParam = {safety.param}"
            if safety.param else ""
        )
        safety_block = f'''    # Declared by a person. openpilot's safety layer -- C code in
    # opendbc/safety/modes/ -- is what actually bounds every message sent from
    # here, and it must be reviewed for this platform before use.
    ret.safetyConfigs = [
      get_safety_config(structs.CarParams.SafetyModel.{safety.model})
    ]{param}
    ret.dashcamOnly = False'''

    delay = limits.steer_actuator_delay
    timer = limits.steer_limit_timer
    speed_lines = []
    if limits.min_steer_speed_ms is not None:
        speed_lines.append(f"    ret.minSteerSpeed = {limits.min_steer_speed_ms!r}")
    if limits.min_enable_speed_ms is not None:
        speed_lines.append(f"    ret.minEnableSpeed = {limits.min_enable_speed_ms!r}")

    long_tuning = info.longitudinal_tuning
    long_lines = []
    if long_control:
        # kp/kpBP moved under a `deprecated` group in opendbc's own schema and
        # no current brand's interface sets them -- only the integral term is
        # live. Setting the plain (non-deprecated) `kpBP` attribute doesn't
        # exist any more and raises `AttributeError` the moment openpilot
        # builds this CarParams, which is worse than just not setting it.
        long_lines = [
            "    ret.longitudinalTuning.kiBP = [0.]",
            f"    ret.longitudinalTuning.kiV = [{long_tuning.ki!r}]",
        ]
        for attribute, value in (
            ("vEgoStopping", long_tuning.v_ego_stopping),
            ("vEgoStarting", long_tuning.v_ego_starting),
            ("stoppingDecelRate", long_tuning.stopping_decel_rate),
        ):
            if value is not None:
                long_lines.append(f"    ret.{attribute} = {value!r}")
    elif info.command("longitudinal") is not None:
        spec.todos.append(
            "A longitudinal command message is declared but the full "
            "longitudinal checklist is incomplete; "
            "openpilotLongitudinalControl stays off."
        )
    long_flag = long_control
    return f'''{_HEADER}
from opendbc.car import get_safety_config, structs
from opendbc.car.{spec.brand}.carcontroller import CarController
from opendbc.car.{spec.brand}.carstate import CarState
from opendbc.car.interfaces import CarInterfaceBase


class CarInterface(CarInterfaceBase):
  CarState = CarState
  CarController = CarController

  @staticmethod
  def _get_params(ret: structs.CarParams, candidate, fingerprint, car_fw,
                  alpha_long, is_release, docs) -> structs.CarParams:
    ret.brand = {spec.brand!r}

{safety_block}

{_lateral_tuning_block(spec)}

    ret.steerActuatorDelay = {(delay if delay is not None else 0.1)!r}
    ret.steerLimitTimer = {(timer if timer is not None else 0.4)!r}
{chr(10).join(speed_lines)}

    ret.radarUnavailable = True
    ret.openpilotLongitudinalControl = {long_flag}
{chr(10).join(long_lines)}

    return ret
'''


def _radar_interface(spec: PortSpec) -> str:
    return f'''{_HEADER}
from opendbc.car.interfaces import RadarInterfaceBase


class RadarInterface(RadarInterfaceBase):
  """No radar parsing. Set `radarUnavailable = False` in interface.py and
  implement this once the radar messages have been identified."""
  pass
'''


def _readme(spec: PortSpec) -> str:
    named = carstate_bindings(spec)
    checksums = [a for a in spec.analyses if a.checksum is not None]
    imperfect_checksums = [
        a for a in checksums
        if a.checksum is not None and a.checksum.n_mismatches
    ]
    underdetermined_checksums = [
        a for a in checksums
        if a.checksum is not None and a.checksum.underdetermined
    ]
    counters = [a for a in spec.analyses if a.counter is not None]
    if imperfect_checksums:
        addresses = ", ".join(
            f"bus{a.bus}:0x{a.addr:X}" for a in imperfect_checksums
        )
        spec.todos.append(
            "Do not transmit messages with non-exact checksum solutions "
            f"({addresses}); collect more varied traffic and solve them exactly."
        )
    if underdetermined_checksums:
        addresses = ", ".join(
            f"bus{a.bus}:0x{a.addr:X}" for a in underdetermined_checksums
        )
        spec.todos.append(
            "Checksum solutions reproduce the capture but are not uniquely "
            f"determined for {addresses}; validate on a more varied capture."
        )

    todo_block = "\n".join(f"- {t}" for t in spec.todos) or "- (none recorded)"
    signal_block = (
        "\n".join(
            f"- `{b.described}` -> `{b.target}` ({b.origin})"
            for b in named
        )
        or "- none; no reference log was supplied"
    )
    manual_block = (
        "Human-supplied facts are preserved in `vehicle_info.json` and explained "
        "in `MANUAL_INPUT.md`. They are marked separately from inferred evidence "
        "and still require independent review."
        if spec.vehicle_info.has_content
        else "No `vehicle_info.json` was supplied; all non-inferable facts remain TODOs."
    )
    lateral_control = spec.control_ready("lateral")
    longitudinal_control = spec.control_ready("longitudinal")
    control_active = lateral_control or longitudinal_control
    construction_check = (
        "assert not CP.dashcamOnly  # declared control path; still requires review"
        if control_active else
        "assert CP.dashcamOnly"
    )
    safety_text = (
        "`carcontroller.py` contains the human-declared control path and the "
        "interface requests that person's selected safety model. **This is not "
        "a safety approval.** `safe_for_control` remains false: verify the "
        "model permits only the intended addresses and limits, bench-test the "
        "frames, and validate on a closed course before driving."
        if control_active else
        "`carcontroller.py` is inert, and `interface.py` requests the `noOutput` "
        "safety model and forces `dashcamOnly = True`. Incomplete or invalid "
        "manual facts cannot make the generated port transmit."
    )

    return f'''# {spec.car_name} — generated openpilot port

Produced by AutoDistill from a CAN capture. **Not a finished port.** Read this
before wiring it to anything.

## What is established

These came from the traffic and are checked, not guessed:

- **Fingerprint**: {spec.fingerprint.total_messages} messages across
  {len(spec.fingerprint.buses)} bus(es), diagnostic and extended addresses
  excluded. The legacy openpilot fingerprint contains physical bus
  {spec.main_bus}; the other buses remain in their own DBCs.
- **Firmware versions**: {len(spec.fingerprint.firmware)} recovered from UDS
  responses in the capture.
- **Checksums**: {len(checksums)} solved; {len(checksums) - len(imperfect_checksums)}
  reproduce every captured frame exactly. See `{spec.brand}can.py` and the
  human checklist for any partial or underdetermined solution.
- **Counters**: {len(counters)} located, with position, width and step.
- **DBCs**: one per observed physical bus; `{spec.dbc_name}.dbc` is the selected
  main bus. Every generated DBC is validated by the AutoDistill test suite.

Signals mapped to CarState fields:

{signal_block}

## Inspecting the DBCs in Cabana

A DBC is not a CAN stream. Cabana needs both the original capture and the DBC
that describes it. In Cabana's **Open stream** dialog, select the **candump**
tab, put the original `.log` capture in the upper **candump file(s)** field,
and put `{spec.dbc_name}.dbc` in the lower **dbc File** field. Selecting a DBC
as the candump file produces Cabana's "Could not parse any CAN frames" error.

After the stream opens, attach any other generated bus DBC through **File →
Manage DBC Files → Bus N → Open DBC File**. Do not combine per-bus DBCs: CAN
addresses can legitimately be reused on different physical buses.

## Manual knowledge supplied

{manual_block}

## What a human has to supply

{todo_block}

Beyond the list above, none of the following can come from a capture:

- `CarSpecs` in `values.py` — mass, wheelbase, steering ratio.
- `CarControllerParams` — torque and rate limits.
- The **safety model** in `opendbc/safety/modes/`, with tests. This is the layer
  that stops a bad command reaching the car.
- Which ECU each unresolved firmware address belongs to (`Ecu.unknown`).

## Installing it

```console
autodistill-can install . <opendbc>
```

The installer copies the Python and per-bus DBC files, registers the new
platform in opendbc's central platform union, and adds the clearly marked
dashcam-only torque placeholder. It refuses to replace differing files unless
you pass `--force`.

Then confirm the interface constructs before anything else:

```python
from opendbc.car.{spec.brand}.values import CAR, DBC
from opendbc.car.{spec.brand}.interface import CarInterface
candidate = next(iter(CAR))
CP = CarInterface.get_non_essential_params(candidate)
{construction_check}
print(candidate, DBC)
```

## Safety

{safety_text}

Work up in this order: confirm the signals against the car with cabana, then
write the safety model, then attempt control — on a closed course, with a way
to disengage.
'''


def _torque_override(spec: PortSpec) -> str:
    car = _identifier(spec.car_name)
    return (
        "# Append this entry to opendbc/car/torque_data/override.toml.\n"
        "# It only makes the generated dashcam/read-only interface constructible.\n"
        "# These are NOT control parameters; replace them with measured values\n"
        "# before removing dashcamOnly or enabling a safety model.\n"
        f'"{car}" = [nan, 1.0, nan]\n'
    )


def _status(spec: PortSpec, dbc_files: dict[int, str]) -> str:
    named = carstate_bindings(spec)
    firmware_addresses = {
        response.request_addr for response in spec.fingerprint.firmware
    }
    unresolved_ecus = sorted(
        firmware_addresses - set(spec.vehicle_info.ecu_types)
    )
    standard_human_work = [
        "Validate every mapped signal against an independent capture.",
        "Identify and validate actuation messages on a closed course.",
        "Measure controller limits and vehicle dynamics.",
        "Implement and review the opendbc safety model with exhaustive tests.",
    ]
    if unresolved_ecus:
        standard_human_work.insert(
            1, "Identify each unresolved firmware address's ECU type."
        )
    from ..coverage import evaluate

    coverage = evaluate(
        spec.analyses, spec.vehicle_info,
        main_bus=spec.main_bus, camera_bus=spec.camera_bus,
    )
    declared_lateral = spec.vehicle_info.command("lateral") is not None
    declared_longitudinal = spec.vehicle_info.command("longitudinal") is not None
    lateral = spec.control_ready("lateral")
    longitudinal = spec.control_ready("longitudinal")
    payload = {
        "schema_version": 1,
        "brand": spec.brand,
        "car_identifier": _identifier(spec.car_name),
        "dbc_name": spec.dbc_name,
        "mode": "control" if (lateral or longitudinal) else "read_only",
        "controls": {
            "lateral": lateral,
            "longitudinal": longitudinal,
            "declared_lateral": declared_lateral,
            "declared_longitudinal": declared_longitudinal,
            # Every line of the control path came from a person. AutoDistill
            # infers nothing about actuation from traffic.
            "source": "human-declared" if (lateral or longitudinal) else "none",
        },
        # Never true. AutoDistill cannot test a car, so it is not the thing
        # that can declare a port safe to drive; a human review is.
        "safe_for_control": False,
        "coverage": coverage.to_dict(),
        # Empty when the port was written without the capture to replay.
        "validation": (
            spec.validation.to_dict() if spec.validation is not None else {}
        ),
        "main_bus": spec.main_bus,
        "dbc_files": {str(bus): name for bus, name in sorted(dbc_files.items())},
        "evidence": {
            "analysed_messages": len(spec.analyses),
            "main_bus_fingerprint_messages": len(
                spec.fingerprint.buses.get(spec.main_bus, {})
            ),
            "firmware_responses": len(spec.fingerprint.firmware),
            "solved_checksums": sum(
                a.checksum is not None for a in spec.analyses
            ),
            "exact_checksums": sum(
                a.checksum is not None and not a.checksum.n_mismatches
                for a in spec.analyses
            ),
            "underdetermined_checksums": sum(
                a.checksum is not None and a.checksum.underdetermined
                for a in spec.analyses
            ),
            "rolling_counters": sum(a.counter is not None for a in spec.analyses),
            "mapped_carstate_fields": sorted({binding.target for binding in named}),
        },
        "manual_input": {
            "provided": spec.vehicle_info.has_content,
            "signal_overrides": len(spec.vehicle_info.signals),
            "ecu_types_supplied": len(spec.vehicle_info.ecu_types),
            "sources": list(spec.vehicle_info.sources),
        },
        "human_required": list(dict.fromkeys(
            spec.todos + standard_human_work
        )),
    }
    return json.dumps(payload, indent=2) + "\n"


def _manual_input_markdown(info: VehicleInfo) -> str:
    """Render a conspicuous human-evidence handoff for the generated package."""
    def lines(items: tuple[str, ...]) -> str:
        return "\n".join(f"- {item}" for item in items) or "- none supplied"

    specs = [
        ("Mass", info.mass_kg, "kg"),
        ("Wheelbase", info.wheelbase_m, "m"),
        ("Steering ratio", info.steer_ratio, ""),
        ("Documentation package", info.docs_package, ""),
    ]
    spec_lines = "\n".join(
        f"- **{name}:** {value} {unit}".rstrip()
        for name, value, unit in specs
        if value is not None
    ) or "- none supplied"
    ecu_lines = "\n".join(
        f"- `0x{address:X}` → `Ecu.{ecu}`"
        for address, ecu in sorted(info.ecu_types.items())
    ) or "- none supplied"
    signal_lines = "\n".join(
        f"- bus {row.bus}, `0x{row.address:X}`, bit {row.start_bit} → "
        f"`{row.name}`" + (
            f" → `{row.carstate_target}`" if row.carstate_target else ""
        )
        for row in info.signals
    ) or "- none supplied"
    return f'''# Human-supplied vehicle knowledge

This file records information a person gave AutoDistill. **It was not inferred
or independently verified.** It is useful handoff evidence, not authorization
to enable control. Check `port_status.json` for what was complete enough to
generate; `safe_for_control` remains false in every case.

## Vehicle specifications

{spec_lines}

## ECU address types

{ecu_lines}

## Known signal overrides

{signal_lines}

## Actuation research notes

{lines(info.actuation_notes)}

## Safety engineering notes

{lines(info.safety_notes)}

## Sources / citations

{lines(info.sources)}
'''


def _atomic_write(path: Path, content: str) -> None:
    """Replace one generated file without exposing a partially written file."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def write_port(
    analyses: Iterable[MessageAnalysis],
    fingerprint: Fingerprint,
    out_dir: Path | str,
    *,
    brand: str = "mystery",
    car_name: str = "MYSTERY CAR",
    main_bus: int = 0,
    camera_bus: int | None = None,
    force: bool = False,
    vehicle_info: VehicleInfo | None = None,
    log: object | None = None,
) -> list[Path]:
    """Write a complete port package. Returns the files written.

    Passing the capture as ``log`` also replays it through the bindings and
    records anything impossible -- a speed in the wrong unit, a brake pressed
    in every frame -- in the port's TODO list and manifest. It costs one pass
    over data already in memory, and it catches the errors that compiling the
    output never will.
    """
    analyses = sorted(analyses, key=lambda a: (a.bus, a.addr))
    info = vehicle_info or VehicleInfo()
    apply_vehicle_info(analyses, info)
    brand = _normalise_brand(brand)
    if main_bus < 0:
        raise ValueError("main bus must be non-negative")
    main_analyses = [a for a in analyses if a.bus == main_bus and a.length > 0]
    if not main_analyses:
        available = sorted({a.bus for a in analyses})
        raise ValueError(
            f"no analysable messages found on main bus {main_bus}; "
            f"available analysed buses: {available}"
        )
    available_camera_buses = sorted({
        a.bus for a in analyses if a.bus != main_bus and a.length > 0
    })
    if camera_bus is not None:
        if camera_bus == main_bus:
            raise ValueError("camera bus must differ from the main bus")
        if camera_bus not in available_camera_buses:
            raise ValueError(
                f"no analysable messages found on camera bus {camera_bus}; "
                f"available secondary buses: {available_camera_buses}"
            )
    else:
        camera_bus = next(
            (bus for bus in sorted(fingerprint.buses) if bus != main_bus),
            available_camera_buses[0] if available_camera_buses else None,
        )

    spec = PortSpec(
        brand=brand,
        car_name=car_name,
        analyses=list(analyses),
        fingerprint=fingerprint,
        main_bus=main_bus,
        camera_bus=camera_bus,
        vehicle_info=info,
    )

    if log is not None:
        from ..validate import validate

        spec.validation = validate(
            spec.analyses, log, info,
            main_bus=spec.main_bus, camera_bus=spec.camera_bus,
        )
        for finding in spec.validation.findings:
            if finding.level == "note":
                continue
            spec.todos.append(
                f"{finding.target}: {finding.message}"
            )

    # carstate and fingerprints append to spec.todos, so the README goes last.
    files = {
        "__init__.py": "",
        "values.py": _values(spec),
        "fingerprints.py": _fingerprints(spec),
        "carstate.py": _carstate(spec),
        f"{brand}can.py": _brandcan(spec),
        "carcontroller.py": _carcontroller(spec),
        "interface.py": _interface(spec),
        "radar_interface.py": _radar_interface(spec),
        "torque_data_override.toml": _torque_override(spec),
    }

    dbc_files: dict[int, str] = {}
    by_bus: dict[int, list[MessageAnalysis]] = defaultdict(list)
    for analysis in analyses:
        if analysis.length > 0:
            by_bus[analysis.bus].append(analysis)
    for bus, bus_analyses in sorted(by_bus.items()):
        dbc_name = (
            f"{spec.dbc_name}.dbc"
            if bus == main_bus
            else f"{spec.dbc_name}_bus{bus}.dbc"
        )
        buffer = io.StringIO()
        write_dbc(bus_analyses, buffer, title=dbc_name.removesuffix(".dbc"))
        files[dbc_name] = buffer.getvalue()
        dbc_files[bus] = dbc_name

    files["README.md"] = _readme(spec)
    files["port_status.json"] = _status(spec, dbc_files)
    if spec.vehicle_info.has_content:
        files["vehicle_info.json"] = (
            json.dumps(spec.vehicle_info.to_dict(), indent=2) + "\n"
        )
        files["MANUAL_INPUT.md"] = _manual_input_markdown(spec.vehicle_info)

    # Validate all generated Python before touching an existing output.
    for name, content in files.items():
        if name.endswith(".py"):
            compile(content, name, "exec")

    out_dir = Path(out_dir)
    existing = [out_dir / name for name in files if (out_dir / name).exists()]
    if existing and not force:
        names = ", ".join(path.name for path in existing[:5])
        raise FileExistsError(
            f"{out_dir} already contains generated target(s): {names}; "
            "pass force=True (CLI: --force) to replace them"
        )
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for name, content in files.items():
        path = out_dir / name
        _atomic_write(path, content)
        written.append(path)

    return written
