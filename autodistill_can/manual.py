"""Validated human-supplied knowledge for gaps a CAN capture cannot fill.

Manual input is evidence supplied by a person, not evidence inferred by
AutoDistill.  The distinction is deliberately retained in reports and generated
ports.  In particular, engineering notes never enable actuation or relax the
``noOutput``/``dashcamOnly`` safety gate.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable, Mapping

from .analysis.message import MessageAnalysis
from .analysis.signals import Signal
from .requirements import GEAR_SHIFTERS

__all__ = [
    "ACTUATION_PURPOSES",
    "ACTUATION_VALUES",
    "CARSTATE_TARGETS",
    "CARSTATE_TRANSFORMS",
    "ECU_TYPES",
    "ESSENTIAL_ECU_TYPES",
    "SAFETY_MODELS",
    "ActuationMessage",
    "CarStateBinding",
    "LateralTuning",
    "Limits",
    "LongitudinalTuning",
    "SafetyModel",
    "SignalOverride",
    "VehicleInfo",
    "apply_vehicle_info",
    "load_vehicle_info",
    "vehicle_info_template",
    "write_vehicle_info",
]

# Current opendbc CarParams.Ecu values. Keeping this explicit turns a typo into
# a validation error instead of a generated package that fails during import.
ECU_TYPES = (
    "abs",
    "adas",
    "body",
    "combinationMeter",
    "cornerRadar",
    "debug",
    "dsu",
    "electricBrakeBooster",
    "engine",
    "epb",
    "eps",
    "fwdCamera",
    "fwdRadar",
    "gateway",
    "hud",
    "hvac",
    "hybrid",
    "parkingAdas",
    "programmedFuelInjection",
    "shiftByWire",
    "srs",
    "telematics",
    "transmission",
    "unknown",
    "vsa",
)

#: The ECU types openpilot treats as *essential* when matching firmware
#: (``ESSENTIAL_ECUS`` in opendbc's ``fw_query_definitions``). A missing
#: non-essential ECU is skipped during matching, so a FW_VERSIONS entry made
#: only of non-essential -- or unidentified -- ECUs can never be ruled out and
#: matches every car in the database.
ESSENTIAL_ECU_TYPES = (
    "engine", "eps", "abs", "fwdRadar", "fwdCamera", "vsa",
)

#: Standard OBD-II / UDS diagnostic request address to ECU type mapping.
#: Standardized OBD/UDS addresses (e.g. 0x7E0 for ECM/engine) provide safe
#: fallback defaults when the address is not explicitly overridden in vehicle_info.json.
STANDARD_ECU_MAP: dict[int, str] = {
    0x7E0: "engine",
    0x7E1: "transmission",
    0x7E2: "hybrid",
    0x7E4: "electricBrakeBooster",
    0x7D0: "fwdCamera",
    0x730: "eps",
    0x760: "abs",
}

CARSTATE_TARGETS = (
    "ret.brakePressed",
    "ret.cruiseState.available",
    "ret.cruiseState.enabled",
    "ret.cruiseState.speed",
    "ret.doorOpen",
    "ret.gasPressed",
    "ret.leftBlinker",
    "ret.rightBlinker",
    "ret.seatbeltUnlatched",
    "ret.steeringAngleDeg",
    "ret.steeringRateDeg",
    "ret.steeringTorque",
    "ret.vEgoRaw",
)

#: Every CarState field a person can bind explicitly. Wider than
#: :data:`CARSTATE_TARGETS`, which only covers the fields AutoDistill will
#: attempt to recognise on its own from a signal name.
CARSTATE_FIELDS = (
    "ret.brake",
    "ret.brakePressed",
    "ret.cruiseState.available",
    "ret.cruiseState.enabled",
    "ret.cruiseState.speed",
    "ret.cruiseState.standstill",
    "ret.doorOpen",
    "ret.espDisabled",
    "ret.gas",
    "ret.gasPressed",
    "ret.gearShifter",
    "ret.leftBlindspot",
    "ret.leftBlinker",
    "ret.rightBlindspot",
    "ret.rightBlinker",
    "ret.seatbeltUnlatched",
    "ret.steeringAngleDeg",
    "ret.steeringRateDeg",
    "ret.steeringTorque",
    "ret.steeringTorqueEps",
    "ret.vEgoRaw",
)

#: How a raw signal value becomes the value openpilot expects. Deliberately a
#: closed vocabulary: a free-text expression here would be arbitrary code in a
#: generated controller, arriving from a JSON file that gets passed around.
CARSTATE_TRANSFORMS = (
    "identity",
    "bool",
    "invert_bool",
    "threshold",
    "equals",
    "scale",
    "kph_to_ms",
    "mph_to_ms",
    "rad_to_deg",
    "percent",
    "gear_map",
)

ACTUATION_PURPOSES = ("lateral", "longitudinal", "cruise_buttons")

#: What a signal in an outgoing message may be set to. Same reasoning as
#: CARSTATE_TRANSFORMS: named quantities the generated controller computes,
#: plus plain numbers. Nothing else is accepted.
ACTUATION_VALUES = (
    "apply_torque",
    "apply_angle",
    "lat_active",
    "accel",
    "gas",
    "brake",
    "long_active",
    "enabled",
    "counter",
    "frame",
)

#: opendbc safety modes AutoDistill recognises. opendbc adds them over time, so
#: an unknown name is preserved with a TODO rather than discarded, but it does
#: not take a generated port out of ``noOutput`` until this list is updated.
SAFETY_MODELS = (
    "allOutput",
    "body",
    "cadillac",
    "chrysler",
    "chryslerCusw",
    "elm327",
    "fcaGiorgio",
    "ford",
    "gm",
    "gmAscm",
    "gmPassive",
    "hondaBosch",
    "hondaBoschGiraffe",
    "hondaNidec",
    "hongqi",
    "hyundai",
    "hyundaiCanfd",
    "hyundaiCommunity",
    "hyundaiLegacy",
    "mazda",
    "nissan",
    "noOutput",
    "psa",
    "rivian",
    "silent",
    "subaru",
    "subaruPreglobal",
    "tesla",
    "toyota",
    "toyotaIpas",
    "volkswagen",
    "volkswagenMeb",
    "volkswagenMlb",
    "volkswagenMqbEvo",
    "volkswagenPq",
)

LATERAL_TUNING_KINDS = ("torque", "pid", "angle")

_TOP_LEVEL = {
    "schema_version", "vehicle_specs", "ecu_types", "signals", "engineering",
    "carstate", "actuation", "limits", "tuning", "safety",
}
_VEHICLE_KEYS = {
    "mass_kg", "wheelbase_m", "steer_ratio", "docs_package",
    "center_to_front_m", "tire_stiffness_factor", "harness",
}
_CARSTATE_KEYS = {
    "target", "bus", "address", "signal", "transform", "threshold", "scale",
    "offset", "gear_map", "note",
}
_ACTUATION_KEYS = {
    "bus", "address", "message", "frequency_hz", "signals", "counter_signal",
    "checksum_signal", "note",
}
_LIMIT_KEYS = {
    "steer_max", "steer_delta_up", "steer_delta_down",
    "steer_driver_allowance", "steer_step", "steer_actuator_delay",
    "steer_limit_timer", "accel_min", "accel_max", "min_steer_speed_ms",
    "min_enable_speed_ms",
}
_TUNING_KEYS = {"lateral", "longitudinal"}
_LATERAL_TUNING_KEYS = {
    "kind", "max_lateral_accel", "friction", "steering_angle_deadzone_deg",
    "kp", "ki", "kf",
}
_LONGITUDINAL_TUNING_KEYS = {
    "kp", "ki", "v_ego_stopping", "v_ego_starting", "stopping_decel_rate",
}
_SAFETY_KEYS = {"model", "param"}
_SIGNAL_KEYS = {
    "bus", "address", "start_bit", "length", "name", "unit", "scale",
    "offset", "signed", "byte_order", "carstate_target",
}
_ENGINEERING_KEYS = {"actuation_notes", "safety_notes", "sources"}
_SIGNAL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")


@dataclass(frozen=True)
class SignalOverride:
    """Human-supplied meaning/scaling for one recovered signal."""

    bus: int
    address: int
    start_bit: int
    name: str
    length: int | None = None
    unit: str | None = None
    scale: float | None = None
    offset: float | None = None
    signed: bool | None = None
    byte_order: str | None = None
    carstate_target: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "bus": self.bus,
            "address": f"0x{self.address:X}",
            "start_bit": self.start_bit,
            "name": self.name,
        }
        for key in (
            "length", "unit", "scale", "offset", "signed", "byte_order",
            "carstate_target",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


@dataclass(frozen=True)
class CarStateBinding:
    """One openpilot CarState field, bound to a recovered signal by a person.

    This is the mechanism for the fields a name cannot reveal. Nothing in
    ``BRAKE_PRESSURE`` says whether 8 means the pedal is down, and nothing in a
    gear signal says which of its values is Drive; both are answerable by
    someone sitting in the car, and unanswerable from the traffic.
    """

    target: str
    bus: int
    address: int
    signal: str
    transform: str = "identity"
    threshold: float | None = None
    scale: float | None = None
    offset: float | None = None
    #: Raw value -> openpilot GearShifter name, for ``gear_map``.
    gear_map: dict[int, str] = field(default_factory=dict)
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "target": self.target,
            "bus": self.bus,
            "address": f"0x{self.address:X}",
            "signal": self.signal,
            "transform": self.transform,
        }
        for key in ("threshold", "scale", "offset", "note"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.gear_map:
            payload["gear_map"] = {
                str(raw): gear for raw, gear in sorted(self.gear_map.items())
            }
        return payload


@dataclass(frozen=True)
class ActuationMessage:
    """A message openpilot transmits, described by someone who verified it."""

    purpose: str
    bus: int
    address: int
    message: str
    frequency_hz: float
    #: Signal name -> a name from :data:`ACTUATION_VALUES`, or a number.
    signals: tuple[tuple[str, str | float], ...] = ()
    counter_signal: str | None = None
    checksum_signal: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "bus": self.bus,
            "address": f"0x{self.address:X}",
            "message": self.message,
            "frequency_hz": self.frequency_hz,
            "signals": {name: value for name, value in self.signals},
        }
        for key in ("counter_signal", "checksum_signal", "note"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


@dataclass(frozen=True)
class Limits:
    """How hard openpilot is allowed to push, in the car's own units."""

    steer_max: float | None = None
    steer_delta_up: float | None = None
    steer_delta_down: float | None = None
    steer_driver_allowance: float | None = None
    steer_step: int = 1
    steer_actuator_delay: float | None = None
    steer_limit_timer: float | None = None
    accel_min: float | None = None
    accel_max: float | None = None
    min_steer_speed_ms: float | None = None
    min_enable_speed_ms: float | None = None

    @property
    def has_content(self) -> bool:
        return any(
            getattr(self, f.name) is not None
            for f in fields(self) if f.name != "steer_step"
        ) or self.steer_step != 1

    @property
    def lateral_complete(self) -> bool:
        return None not in (
            self.steer_max, self.steer_delta_up, self.steer_delta_down,
            self.steer_driver_allowance, self.steer_actuator_delay,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if getattr(self, f.name) is not None
        }
        return payload


@dataclass(frozen=True)
class LateralTuning:
    kind: str = "torque"
    max_lateral_accel: float | None = None
    friction: float | None = None
    steering_angle_deadzone_deg: float | None = None
    kp: float | None = None
    ki: float | None = None
    kf: float | None = None

    @property
    def complete(self) -> bool:
        if self.kind == "torque":
            return self.max_lateral_accel is not None and self.friction is not None
        if self.kind == "pid":
            return self.kp is not None and self.ki is not None
        return True  # angle control has no gains of its own

    def to_dict(self) -> dict[str, Any]:
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if getattr(self, f.name) is not None
        }


@dataclass(frozen=True)
class LongitudinalTuning:
    #: Accepted, but no longer wired into generated code: opendbc moved this
    #: gain under a `deprecated` group in its own schema, and no current
    #: brand's interface sets it. Kept in the schema only so an older
    #: vehicle-info.json that supplied one still loads.
    kp: float | None = None
    ki: float | None = None
    v_ego_stopping: float | None = None
    v_ego_starting: float | None = None
    stopping_decel_rate: float | None = None

    @property
    def complete(self) -> bool:
        return self.ki is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if getattr(self, f.name) is not None
        }


@dataclass(frozen=True)
class SafetyModel:
    """The opendbc safety mode this platform runs behind."""

    model: str
    param: int = 0

    @property
    def known(self) -> bool:
        return self.model in SAFETY_MODELS

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.model, "param": self.param}


@dataclass(frozen=True)
class VehicleInfo:
    """Facts and notes that came from a person rather than CAN inference."""

    mass_kg: float | None = None
    wheelbase_m: float | None = None
    steer_ratio: float | None = None
    docs_package: str | None = None
    center_to_front_m: float | None = None
    tire_stiffness_factor: float | None = None
    #: opendbc `CarHarness` member name. Which harness fits is a fact only
    #: somebody looking at the car can supply, and openpilot's own docs test
    #: refuses a supported platform without one.
    harness: str | None = None
    ecu_types: dict[int, str] = field(default_factory=dict)
    signals: tuple[SignalOverride, ...] = ()
    carstate: tuple[CarStateBinding, ...] = ()
    actuation: tuple[ActuationMessage, ...] = ()
    limits: Limits = field(default_factory=Limits)
    lateral_tuning: LateralTuning | None = None
    longitudinal_tuning: LongitudinalTuning | None = None
    safety: SafetyModel | None = None
    actuation_notes: tuple[str, ...] = ()
    safety_notes: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()

    @property
    def has_content(self) -> bool:
        return any((
            self.mass_kg is not None,
            self.wheelbase_m is not None,
            self.steer_ratio is not None,
            self.docs_package is not None,
            self.center_to_front_m is not None,
            self.tire_stiffness_factor is not None,
            self.harness is not None,
            self.ecu_types,
            self.signals,
            self.carstate,
            self.actuation,
            self.limits.has_content,
            self.lateral_tuning is not None,
            self.longitudinal_tuning is not None,
            self.safety is not None,
            self.actuation_notes,
            self.safety_notes,
            self.sources,
        ))

    def command(self, purpose: str) -> ActuationMessage | None:
        return next((m for m in self.actuation if m.purpose == purpose), None)

    def get_ecu_type(self, addr: int) -> str | None:
        """Return the ECU type for a request address, checking manual overrides first."""
        if addr in self.ecu_types:
            return self.ecu_types[addr]
        return STANDARD_ECU_MAP.get(addr)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "vehicle_specs": {
                "mass_kg": self.mass_kg,
                "wheelbase_m": self.wheelbase_m,
                "steer_ratio": self.steer_ratio,
                "docs_package": self.docs_package,
                "center_to_front_m": self.center_to_front_m,
                "tire_stiffness_factor": self.tire_stiffness_factor,
                "harness": self.harness,
            },
            "ecu_types": {
                f"0x{address:X}": ecu
                for address, ecu in sorted(self.ecu_types.items())
            },
            "signals": [signal.to_dict() for signal in self.signals],
            "carstate": [binding.to_dict() for binding in self.carstate],
            "actuation": {
                message.purpose: message.to_dict() for message in self.actuation
            },
            "limits": self.limits.to_dict(),
            "tuning": {},
            "engineering": {
                "actuation_notes": list(self.actuation_notes),
                "safety_notes": list(self.safety_notes),
                "sources": list(self.sources),
            },
        }
        if self.lateral_tuning is not None:
            payload["tuning"]["lateral"] = self.lateral_tuning.to_dict()
        if self.longitudinal_tuning is not None:
            payload["tuning"]["longitudinal"] = self.longitudinal_tuning.to_dict()
        if self.safety is not None:
            payload["safety"] = self.safety.to_dict()
        return payload


def vehicle_info_template() -> dict[str, Any]:
    """An empty, immediately usable file accepted by :func:`load_vehicle_info`.

    Examples belong in the documentation, not in the generated file: a pretend
    ECU address or signal looks too much like evidence once this JSON is copied
    into a real project.
    """
    return {
        "schema_version": 1,
        "vehicle_specs": {
            "mass_kg": None,
            "wheelbase_m": None,
            "steer_ratio": None,
            "docs_package": None,
            "center_to_front_m": None,
            "tire_stiffness_factor": None,
            "harness": None,
        },
        "ecu_types": {},
        "signals": [],
        "carstate": [],
        "actuation": {},
        "limits": {},
        "tuning": {},
        "engineering": {
            "actuation_notes": [],
            "safety_notes": [],
            "sources": [],
        },
    }


def _unknown_keys(value: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown {where} field(s): {', '.join(unknown)}")


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be a JSON object")
    return value


def _number(
    value: Any,
    name: str,
    *,
    minimum: float,
    maximum: float,
    allow_zero: bool = False,
) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    lower_ok = result >= minimum if allow_zero else result > minimum
    if not lower_ok or result > maximum:
        operator = ">=" if allow_zero else ">"
        raise ValueError(f"{name} must be {operator} {minimum} and <= {maximum}")
    return result


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _address(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a CAN address")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            result = int(text, 0)
        except ValueError:
            try:
                result = int(text, 16)
            except ValueError as exc:
                raise ValueError(f"{name} must be a CAN address") from exc
    else:
        raise ValueError(f"{name} must be a CAN address")
    if not 0 <= result <= 0x1FFFFFFF:
        raise ValueError(f"{name} is outside the CAN address range")
    return result


def _short_text(value: Any, name: str, *, maximum: int = 300) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    text = value.strip()
    if not text or len(text) > maximum or any(ord(ch) < 32 for ch in text):
        raise ValueError(f"{name} must be 1-{maximum} printable characters")
    return text


def _text_list(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError(f"{name} must be a list of at most 100 text entries")
    out = []
    for index, item in enumerate(value):
        text = _short_text(item, f"{name}[{index}]", maximum=1000)
        if text is not None:
            out.append(text)
    return tuple(out)


def _carstate_bindings(payload: Any) -> tuple[CarStateBinding, ...]:
    if payload is None:
        return ()
    if not isinstance(payload, list) or len(payload) > 200:
        raise ValueError("carstate must be a list of at most 200 entries")
    bindings: list[CarStateBinding] = []
    seen: set[str] = set()
    for index, item in enumerate(payload):
        where = f"carstate[{index}]"
        entry = _mapping(item, where)
        _unknown_keys(entry, _CARSTATE_KEYS, where)
        target = _short_text(entry.get("target"), f"{where}.target", maximum=80)
        if target not in CARSTATE_FIELDS:
            raise ValueError(
                f"{where}.target must be one of {', '.join(CARSTATE_FIELDS)}"
            )
        if target in seen:
            raise ValueError(
                f"duplicate carstate target {target}; each field may be bound once"
            )
        seen.add(target)
        signal = _short_text(entry.get("signal"), f"{where}.signal", maximum=80)
        if signal is None or not _SIGNAL_NAME.fullmatch(signal):
            raise ValueError(f"{where}.signal must be a DBC identifier")
        transform = _short_text(
            entry.get("transform") or "identity", f"{where}.transform", maximum=40
        )
        if transform not in CARSTATE_TRANSFORMS:
            raise ValueError(
                f"{where}.transform must be one of {', '.join(CARSTATE_TRANSFORMS)}"
            )
        threshold = _number(
            entry.get("threshold"), f"{where}.threshold",
            minimum=-1e9, maximum=1e9, allow_zero=True,
        )
        if transform in {"threshold", "equals"} and threshold is None:
            raise ValueError(f"{where}.transform {transform!r} needs a threshold")
        scale = _number(
            entry.get("scale"), f"{where}.scale",
            minimum=-1e9, maximum=1e9, allow_zero=True,
        )
        if transform == "scale" and scale in (None, 0):
            raise ValueError(f"{where}.transform 'scale' needs a non-zero scale")
        gear_payload = _mapping(entry.get("gear_map"), f"{where}.gear_map")
        gear_map: dict[int, str] = {}
        for raw_value, gear in gear_payload.items():
            raw = _integer(raw_value, f"{where}.gear_map key", minimum=0, maximum=2**32)
            if gear not in GEAR_SHIFTERS:
                raise ValueError(
                    f"{where}.gear_map[{raw}] must be one of "
                    f"{', '.join(GEAR_SHIFTERS)}"
                )
            gear_map[raw] = str(gear)
        if transform == "gear_map" and not gear_map:
            raise ValueError(f"{where}.transform 'gear_map' needs a gear_map")
        if gear_map and target != "ret.gearShifter":
            raise ValueError(f"{where}.gear_map only applies to ret.gearShifter")
        bindings.append(CarStateBinding(
            target=target,
            bus=_integer(entry.get("bus"), f"{where}.bus", minimum=0, maximum=15),
            address=_address(entry.get("address"), f"{where}.address"),
            signal=signal.upper(),
            transform=transform,
            threshold=threshold,
            scale=scale,
            offset=_number(
                entry.get("offset"), f"{where}.offset",
                minimum=-1e9, maximum=1e9, allow_zero=True,
            ),
            gear_map=gear_map,
            note=_short_text(entry.get("note"), f"{where}.note"),
        ))
    return tuple(bindings)


def _actuation_signals(payload: Any, where: str) -> tuple[tuple[str, str | float], ...]:
    entries = _mapping(payload, where)
    if not entries:
        raise ValueError(f"{where} must name at least one signal to send")
    if len(entries) > 64:
        raise ValueError(f"{where} must have at most 64 signals")
    out: list[tuple[str, str | float]] = []
    for raw_name, raw_value in entries.items():
        name = _short_text(raw_name, f"{where} signal name", maximum=80)
        if name is None or not _SIGNAL_NAME.fullmatch(name):
            raise ValueError(f"{where}: {raw_name!r} is not a DBC identifier")
        if isinstance(raw_value, bool):
            raise ValueError(f"{where}.{name} must be a number or a named value")
        if isinstance(raw_value, (int, float)):
            out.append((name.upper(), float(raw_value)))
            continue
        value = _short_text(raw_value, f"{where}.{name}", maximum=40)
        if value not in ACTUATION_VALUES:
            raise ValueError(
                f"{where}.{name} must be a number or one of "
                f"{', '.join(ACTUATION_VALUES)}"
            )
        out.append((name.upper(), value))
    return tuple(out)


def _actuation(payload: Any) -> tuple[ActuationMessage, ...]:
    entries = _mapping(payload, "actuation")
    if "cruise_buttons" in entries:
        raise ValueError(
            "actuation.cruise_buttons is not supported by the generated "
            "controller: button values need separate cancel/resume mappings. "
            "Add that logic by hand instead of generating a message that is "
            "silently sent with the wrong button value"
        )
    unknown = sorted(set(entries) - set(ACTUATION_PURPOSES))
    if unknown:
        raise ValueError(
            f"unknown actuation purpose(s): {', '.join(unknown)}; expected "
            f"{', '.join(ACTUATION_PURPOSES)}"
        )
    messages: list[ActuationMessage] = []
    for purpose in ACTUATION_PURPOSES:
        if purpose not in entries:
            continue
        where = f"actuation.{purpose}"
        entry = _mapping(entries[purpose], where)
        _unknown_keys(entry, _ACTUATION_KEYS, where)
        address = _address(entry.get("address"), f"{where}.address")
        message = _short_text(entry.get("message"), f"{where}.message", maximum=80)
        if message is None:
            message = f"MSG_{address:03X}"
        if not _SIGNAL_NAME.fullmatch(message):
            raise ValueError(f"{where}.message must be a DBC identifier")
        frequency = _number(
            entry.get("frequency_hz"), f"{where}.frequency_hz",
            # CarController.update runs at 100 Hz. Higher rates would require
            # multiple frames per update, which this schema cannot describe.
            minimum=0, maximum=100,
        )
        if frequency is None:
            raise ValueError(f"{where}.frequency_hz is required")
        counter = _short_text(
            entry.get("counter_signal"), f"{where}.counter_signal", maximum=80
        )
        checksum = _short_text(
            entry.get("checksum_signal"), f"{where}.checksum_signal", maximum=80
        )
        for label, value in (("counter_signal", counter), ("checksum_signal", checksum)):
            if value is not None and not _SIGNAL_NAME.fullmatch(value):
                raise ValueError(f"{where}.{label} must be a DBC identifier")
        messages.append(ActuationMessage(
            purpose=purpose,
            bus=_integer(entry.get("bus"), f"{where}.bus", minimum=0, maximum=15),
            address=address,
            message=message.upper(),
            frequency_hz=frequency,
            signals=_actuation_signals(entry.get("signals"), f"{where}.signals"),
            counter_signal=counter.upper() if counter else None,
            checksum_signal=checksum.upper() if checksum else None,
            note=_short_text(entry.get("note"), f"{where}.note"),
        ))
    return tuple(messages)


def _limits(payload: Any) -> Limits:
    entry = _mapping(payload, "limits")
    _unknown_keys(entry, _LIMIT_KEYS, "limits")

    def positive(name: str, maximum: float) -> float | None:
        return _number(entry.get(name), f"limits.{name}", minimum=0, maximum=maximum)

    step = entry.get("steer_step")
    return Limits(
        steer_max=positive("steer_max", 1e6),
        steer_delta_up=positive("steer_delta_up", 1e6),
        steer_delta_down=positive("steer_delta_down", 1e6),
        steer_driver_allowance=_number(
            entry.get("steer_driver_allowance"), "limits.steer_driver_allowance",
            minimum=0, maximum=1e6, allow_zero=True,
        ),
        steer_step=(
            1 if step is None
            else _integer(step, "limits.steer_step", minimum=1, maximum=100)
        ),
        steer_actuator_delay=positive("steer_actuator_delay", 2),
        steer_limit_timer=positive("steer_limit_timer", 60),
        accel_min=_number(
            entry.get("accel_min"), "limits.accel_min",
            minimum=-20, maximum=0, allow_zero=True,
        ),
        accel_max=positive("accel_max", 20),
        min_steer_speed_ms=_number(
            entry.get("min_steer_speed_ms"), "limits.min_steer_speed_ms",
            minimum=0, maximum=100, allow_zero=True,
        ),
        min_enable_speed_ms=_number(
            entry.get("min_enable_speed_ms"), "limits.min_enable_speed_ms",
            minimum=0, maximum=100, allow_zero=True,
        ),
    )


def _tuning(payload: Any) -> tuple[LateralTuning | None, LongitudinalTuning | None]:
    entry = _mapping(payload, "tuning")
    _unknown_keys(entry, _TUNING_KEYS, "tuning")
    lateral = None
    if entry.get("lateral") is not None:
        lat = _mapping(entry["lateral"], "tuning.lateral")
        _unknown_keys(lat, _LATERAL_TUNING_KEYS, "tuning.lateral")
        kind = _short_text(lat.get("kind") or "torque", "tuning.lateral.kind",
                           maximum=20)
        if kind not in LATERAL_TUNING_KINDS:
            raise ValueError(
                f"tuning.lateral.kind must be one of "
                f"{', '.join(LATERAL_TUNING_KINDS)}"
            )

        def gain(name: str, maximum: float) -> float | None:
            return _number(
                lat.get(name), f"tuning.lateral.{name}",
                minimum=0, maximum=maximum, allow_zero=True,
            )

        lateral = LateralTuning(
            kind=kind,
            max_lateral_accel=gain("max_lateral_accel", 10),
            friction=gain("friction", 10),
            steering_angle_deadzone_deg=gain("steering_angle_deadzone_deg", 10),
            kp=gain("kp", 1e4),
            ki=gain("ki", 1e4),
            kf=gain("kf", 1e4),
        )
    longitudinal = None
    if entry.get("longitudinal") is not None:
        lon = _mapping(entry["longitudinal"], "tuning.longitudinal")
        _unknown_keys(lon, _LONGITUDINAL_TUNING_KEYS, "tuning.longitudinal")

        def value(name: str, maximum: float) -> float | None:
            return _number(
                lon.get(name), f"tuning.longitudinal.{name}",
                minimum=0, maximum=maximum, allow_zero=True,
            )

        longitudinal = LongitudinalTuning(
            kp=value("kp", 1e4),
            ki=value("ki", 1e4),
            v_ego_stopping=value("v_ego_stopping", 10),
            v_ego_starting=value("v_ego_starting", 10),
            stopping_decel_rate=value("stopping_decel_rate", 10),
        )
    return lateral, longitudinal


def _harness(value: Any) -> str | None:
    """An opendbc ``CarHarness`` member name.

    Validated by shape rather than against a list: opendbc adds harnesses, and
    refusing a new one would age badly. A wrong name fails at import, which is
    a fine place to find out.
    """
    name = _short_text(value, "vehicle_specs.harness", maximum=60)
    if name is None:
        return None
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,59}", name):
        raise ValueError(
            "vehicle_specs.harness must be an opendbc CarHarness name, "
            "e.g. hyundai_k"
        )
    return name


def _safety(payload: Any) -> SafetyModel | None:
    entry = _mapping(payload, "safety")
    if not entry:
        return None
    _unknown_keys(entry, _SAFETY_KEYS, "safety")
    model = _short_text(entry.get("model"), "safety.model", maximum=40)
    if model is None:
        raise ValueError("safety.model is required when a safety section is given")
    if not re.fullmatch(r"[a-z][A-Za-z0-9]{1,39}", model):
        raise ValueError(
            "safety.model must be an opendbc SafetyModel name, e.g. hyundaiCanfd"
        )
    param = entry.get("param", 0)
    return SafetyModel(
        model=model,
        param=_integer(param, "safety.param", minimum=0, maximum=2**31 - 1),
    )


def load_vehicle_info(
    value: Path | str | Mapping[str, Any] | VehicleInfo | None,
) -> VehicleInfo:
    """Load and validate manual knowledge from a path or decoded JSON object."""
    if value is None:
        return VehicleInfo()
    if isinstance(value, VehicleInfo):
        return value
    if isinstance(value, (str, Path)):
        path = Path(value)
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: invalid JSON: {exc}") from exc
    else:
        raw = value
    root = _mapping(raw, "vehicle info")
    _unknown_keys(root, _TOP_LEVEL, "top-level")
    schema_version = root.get("schema_version", 1)
    if isinstance(schema_version, bool) or schema_version != 1:
        raise ValueError(
            f"unsupported vehicle info schema {schema_version!r}"
        )

    specs = _mapping(root.get("vehicle_specs"), "vehicle_specs")
    _unknown_keys(specs, _VEHICLE_KEYS, "vehicle_specs")
    engineering = _mapping(root.get("engineering"), "engineering")
    _unknown_keys(engineering, _ENGINEERING_KEYS, "engineering")

    ecu_payload = _mapping(root.get("ecu_types"), "ecu_types")
    ecu_types: dict[int, str] = {}
    for raw_address, raw_ecu in ecu_payload.items():
        address = _address(raw_address, f"ECU address {raw_address!r}")
        if address in ecu_types:
            raise ValueError(f"duplicate ECU address 0x{address:X}")
        if raw_ecu not in ECU_TYPES:
            raise ValueError(
                f"ECU type for 0x{address:X} must be one of {', '.join(ECU_TYPES)}"
            )
        ecu_types[address] = str(raw_ecu)

    signal_payload = root.get("signals", [])
    if not isinstance(signal_payload, list) or len(signal_payload) > 1000:
        raise ValueError("signals must be a list of at most 1000 entries")
    signals: list[SignalOverride] = []
    seen: set[tuple[int, int, int]] = set()
    seen_names: set[tuple[int, int, str]] = set()
    seen_targets: set[str] = set()
    for index, item in enumerate(signal_payload):
        entry = _mapping(item, f"signals[{index}]")
        _unknown_keys(entry, _SIGNAL_KEYS, f"signals[{index}]")
        name = _short_text(entry.get("name"), f"signals[{index}].name", maximum=80)
        if name is None or not _SIGNAL_NAME.fullmatch(name):
            raise ValueError(
                f"signals[{index}].name must be a DBC identifier"
            )
        bus = _integer(entry.get("bus"), f"signals[{index}].bus", minimum=0, maximum=15)
        address = _address(entry.get("address"), f"signals[{index}].address")
        start = _integer(
            entry.get("start_bit"), f"signals[{index}].start_bit",
            minimum=0, maximum=511,
        )
        length = entry.get("length")
        if length is not None:
            length = _integer(
                length, f"signals[{index}].length", minimum=1, maximum=64
            )
        key = (bus, address, start)
        if key in seen:
            raise ValueError(f"duplicate manual signal at bus{bus}:0x{address:X}:{start}")
        seen.add(key)
        target = _short_text(
            entry.get("carstate_target"),
            f"signals[{index}].carstate_target",
            maximum=80,
        )
        if target is not None and target not in CARSTATE_TARGETS:
            raise ValueError(
                f"signals[{index}].carstate_target must be one of "
                f"{', '.join(CARSTATE_TARGETS)}"
            )
        if target is not None and target in seen_targets:
            raise ValueError(
                f"duplicate manual CarState target {target}; each target may "
                "come from only one signal"
            )
        if target is not None:
            seen_targets.add(target)
        normalized_name = name.upper()
        name_key = (bus, address, normalized_name)
        if name_key in seen_names:
            raise ValueError(
                f"duplicate manual signal name {normalized_name} in "
                f"bus{bus}:0x{address:X}"
            )
        seen_names.add(name_key)
        scale = _number(
            entry.get("scale"), f"signals[{index}].scale",
            minimum=-1e12, maximum=1e12, allow_zero=True,
        )
        if scale == 0:
            raise ValueError(f"signals[{index}].scale must not be zero")
        signed = entry.get("signed")
        if signed is not None and not isinstance(signed, bool):
            raise ValueError(f"signals[{index}].signed must be true or false")
        byte_order = _short_text(
            entry.get("byte_order"), f"signals[{index}].byte_order", maximum=20
        )
        if byte_order not in (None, "big", "little"):
            raise ValueError(
                f"signals[{index}].byte_order must be 'big' or 'little'"
            )
        signals.append(SignalOverride(
            bus=bus,
            address=address,
            start_bit=start,
            length=length,
            name=normalized_name,
            unit=_short_text(entry.get("unit"), f"signals[{index}].unit", maximum=40),
            scale=scale,
            offset=_number(
                entry.get("offset"), f"signals[{index}].offset",
                minimum=-1e12, maximum=1e12, allow_zero=True,
            ),
            signed=signed,
            byte_order=byte_order,
            carstate_target=target,
        ))

    lateral_tuning, longitudinal_tuning = _tuning(root.get("tuning"))
    return VehicleInfo(
        mass_kg=_number(
            specs.get("mass_kg"), "vehicle_specs.mass_kg",
            minimum=100, maximum=20000,
        ),
        wheelbase_m=_number(
            specs.get("wheelbase_m"), "vehicle_specs.wheelbase_m",
            minimum=0.5, maximum=15,
        ),
        steer_ratio=_number(
            specs.get("steer_ratio"), "vehicle_specs.steer_ratio",
            minimum=1, maximum=100,
        ),
        docs_package=_short_text(
            specs.get("docs_package"), "vehicle_specs.docs_package", maximum=120
        ),
        center_to_front_m=_number(
            specs.get("center_to_front_m"), "vehicle_specs.center_to_front_m",
            minimum=0.1, maximum=10,
        ),
        tire_stiffness_factor=_number(
            specs.get("tire_stiffness_factor"),
            "vehicle_specs.tire_stiffness_factor", minimum=0.01, maximum=10,
        ),
        harness=_harness(specs.get("harness")),
        ecu_types=ecu_types,
        signals=tuple(signals),
        carstate=_carstate_bindings(root.get("carstate")),
        actuation=_actuation(root.get("actuation")),
        limits=_limits(root.get("limits")),
        lateral_tuning=lateral_tuning,
        longitudinal_tuning=longitudinal_tuning,
        safety=_safety(root.get("safety")),
        actuation_notes=_text_list(
            engineering.get("actuation_notes"), "engineering.actuation_notes"
        ),
        safety_notes=_text_list(
            engineering.get("safety_notes"), "engineering.safety_notes"
        ),
        sources=_text_list(engineering.get("sources"), "engineering.sources"),
    )


def write_vehicle_info(info: VehicleInfo, path: Path | str) -> Path:
    """Write normalized manual knowledge as deterministic JSON."""
    target = Path(path)
    target.write_text(json.dumps(info.to_dict(), indent=2) + "\n")
    return target


def _all_signals(analysis: MessageAnalysis) -> list[Signal]:
    found: list[Signal] = []
    seen: set[int] = set()
    for signal in analysis.signals:
        if id(signal) not in seen:
            seen.add(id(signal))
            found.append(signal)
    for signals in analysis.mux_signals.values():
        for signal in signals:
            if id(signal) not in seen:
                seen.add(id(signal))
                found.append(signal)
    return found


def _contiguous_runs(bits: set[int]) -> list[tuple[int, int]]:
    """Return ``(start, length)`` runs for MSB-first payload bit positions."""
    if not bits:
        return []
    ordered = sorted(bits)
    runs: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for bit in ordered[1:]:
        if bit != previous + 1:
            runs.append((start, previous - start + 1))
            start = bit
        previous = bit
    runs.append((start, previous - start + 1))
    return runs


def _field_for_override(
    analysis: MessageAnalysis, override: SignalOverride, location: str
) -> Signal:
    """Find, or safely re-tile, the field identified by a manual fact.

    Statistical tiling cannot always recover an official field boundary. A
    signed value that crosses zero can look like adjacent bytes and flags, and
    a switch that was never exercised looks exactly like padding. When a
    person supplies both start and length, permit those already-observed bits
    to be joined or split. Proved integrity fields remain immutable.
    """
    all_signals = _all_signals(analysis)
    exact = [
        signal
        for signal in all_signals
        if signal.start == override.start_bit
        and (override.length is None or signal.length == override.length)
    ]
    if len(exact) > 1:
        raise ValueError(
            f"manual signal {location} is ambiguous across multiplex modes; "
            "specify a unique field"
        )
    if exact:
        signal = exact[0]
        if signal.kind in {"counter", "checksum", "mux"}:
            raise ValueError(
                f"manual signal {location} cannot relabel a {signal.kind} field"
            )
        # A constant may be a real switch that simply was not exercised. Only
        # accept that assertion when the human supplied its exact width.
        if signal.kind == "constant" and override.length is None:
            raise ValueError(
                f"manual signal {location} cannot relabel a constant field "
                "without an explicit length"
            )
        return signal

    if override.length is None:
        raise ValueError(
            f"manual signal {location} does not match a recovered field; "
            "supply length to correct a recovered boundary"
        )
    if analysis.multiplex is not None:
        raise ValueError(
            f"manual signal {location} cannot re-tile a multiplexed message"
        )
    if override.signed is None or override.byte_order is None:
        raise ValueError(
            f"manual signal {location} corrects a recovered boundary and must "
            "declare both signed and byte_order"
        )

    big_endian = override.byte_order != "little"
    replacement = Signal(
        start=override.start_bit,
        length=override.length,
        kind="bool" if override.length == 1 else "physical",
        big_endian=big_endian,
        signed=bool(override.signed),
    )
    nbits = analysis.length * 8
    target_bits = set(replacement.payload_bits(nbits))
    if not target_bits or min(target_bits) < 0 or max(target_bits) >= nbits:
        raise ValueError(f"manual signal {location} extends beyond the payload")

    # Integrity metadata is normally represented in analysis.signals too, but
    # check it directly before handling a below-threshold message whose signal
    # list is empty. A manual fact may fill absent statistical analysis; it
    # still may never overwrite a proved counter/checksum/multiplexer.
    protected = []
    for kind, metadata_field in (
        ("counter", analysis.counter),
        ("checksum", analysis.checksum),
        ("mux", analysis.multiplex),
    ):
        if metadata_field is None:
            continue
        bits = set(
            range(
                metadata_field.start,
                metadata_field.start + metadata_field.length,
            )
        )
        if target_bits.intersection(bits):
            protected.append(kind)
    if protected:
        raise ValueError(
            f"manual signal {location} overlaps protected "
            f"{', '.join(sorted(set(protected)))} bits"
        )

    overlapping = [
        signal
        for signal in analysis.signals
        if target_bits.intersection(signal.payload_bits(nbits))
    ]
    if not overlapping:
        if not analysis.signals:
            # `min_frames` intentionally skips statistical inference on very
            # rare messages. The message and payload length were still truly
            # observed, so a fully explicit service-manual/official-DBC fact
            # can define its field. Keep every other payload bit as unknown so
            # the generated DBC remains complete and non-overlapping.
            survivors = [
                Signal(start=start, length=length, kind="unknown")
                for start, length in _contiguous_runs(set(range(nbits)) - target_bits)
            ]
            survivors.append(replacement)
            survivors.sort(key=lambda signal: min(signal.payload_bits(nbits)))
            analysis.signals = survivors
            return replacement
        raise ValueError(
            f"manual signal {location} does not cover any recovered field"
        )
    integrity = [
        signal for signal in overlapping
        if signal.kind in {"counter", "checksum", "mux"}
    ]
    if integrity:
        kinds = ", ".join(sorted({signal.kind for signal in integrity}))
        raise ValueError(
            f"manual signal {location} overlaps protected {kinds} bits"
        )
    covered = set().union(*(
        set(signal.payload_bits(nbits)) for signal in overlapping
    ))
    if not target_bits.issubset(covered):
        raise ValueError(
            f"manual signal {location} includes bits absent from the recovered layout"
        )

    # Replace only the intersecting portions. Any bits left around the new
    # field stay represented, but lose speculative physical meaning after the
    # boundary correction. Constant remnants remain known constants.
    survivors = [signal for signal in analysis.signals if signal not in overlapping]
    for original in overlapping:
        residual = set(original.payload_bits(nbits)) - target_bits
        for start, length in _contiguous_runs(residual):
            survivors.append(Signal(
                start=start,
                length=length,
                kind="constant" if original.kind == "constant" else "unknown",
                confidence=original.confidence,
            ))
    survivors.append(replacement)
    survivors.sort(key=lambda signal: min(signal.payload_bits(nbits)))
    analysis.signals = survivors
    return replacement


def _check_bindings(
    by_key: dict[tuple[int, int], MessageAnalysis], info: VehicleInfo
) -> None:
    """Reject CarState bindings and commands that name fields nobody saw.

    Both halves have to point at something real. A binding onto a signal that
    is not in the capture generates a ``CarState`` that raises on the first
    frame; a command message that is not in the DBC cannot be packed at all.
    Catching it here means the error names the entry, not a traceback from
    inside openpilot.
    """
    def named(analysis: MessageAnalysis) -> dict[str, list[Signal]]:
        result: dict[str, list[Signal]] = {}
        for signal in _all_signals(analysis):
            result.setdefault(signal.default_name(analysis.addr), []).append(signal)
        return result

    for binding in info.carstate:
        location = f"bus{binding.bus}:0x{binding.address:X}"
        analysis = by_key.get((binding.bus, binding.address))
        if analysis is None:
            raise ValueError(
                f"carstate binding for {binding.target} refers to unknown "
                f"message {location}"
            )
        names = named(analysis)
        if binding.signal not in names:
            raise ValueError(
                f"carstate binding for {binding.target} refers to signal "
                f"{binding.signal} which is not in {location}"
            )
        if len(names[binding.signal]) != 1:
            raise ValueError(
                f"carstate binding for {binding.target} refers to ambiguous "
                f"signal {binding.signal}, which appears more than once in {location}"
            )

    used_messages: dict[tuple[int, int], str] = {}
    for message in info.actuation:
        location = f"bus{message.bus}:0x{message.address:X}"
        analysis = by_key.get((message.bus, message.address))
        if analysis is None:
            raise ValueError(
                f"actuation.{message.purpose} names message {location}, which is "
                "not in this capture. openpilot can only send a message whose "
                "layout is in the DBC, so capture the ECU that already sends it."
            )
        previous = used_messages.get(analysis.key)
        if previous is not None:
            raise ValueError(
                f"actuation.{message.purpose} and actuation.{previous} both use "
                f"{location}; the generator cannot safely merge two commands "
                "into one counter/checksum sequence"
            )
        used_messages[analysis.key] = message.purpose
        if analysis.multiplex is not None:
            raise ValueError(
                f"actuation.{message.purpose} uses multiplexed message {location}; "
                "the generated command schema cannot select a multiplex mode"
            )
        if message.message != analysis.name:
            raise ValueError(
                f"actuation.{message.purpose}.message is {message.message}, but "
                f"the generated DBC names {location} {analysis.name}; use "
                f"{analysis.name!r} (or omit message to use that default)"
            )

        names = named(analysis)
        declared = [name for name, _ in message.signals]
        for name in (
            *declared,
            *(n for n in (message.counter_signal, message.checksum_signal) if n),
        ):
            if name not in names:
                raise ValueError(
                    f"actuation.{message.purpose} sets {name}, which is not a "
                    f"field of {location}"
                )
            if len(names[name]) != 1:
                raise ValueError(
                    f"actuation.{message.purpose} refers to ambiguous signal "
                    f"{name}, which appears more than once in {location}"
                )

        integrity = {
            value for value in (message.counter_signal, message.checksum_signal)
            if value is not None
        }
        overlap = sorted(integrity & set(declared))
        if overlap:
            raise ValueError(
                f"actuation.{message.purpose}.signals also sets integrity field(s) "
                f"{', '.join(overlap)}; declare each only as counter_signal or "
                "checksum_signal"
            )

        counter_field = next(
            (
                signal for signal in _all_signals(analysis)
                if analysis.counter is not None
                and signal.kind == "counter"
                and signal.start == analysis.counter.start
                and signal.length == analysis.counter.length
            ),
            None,
        )
        expected_counter = (
            counter_field.default_name(analysis.addr)
            if counter_field is not None else None
        )
        if analysis.counter is not None and message.counter_signal is None:
            raise ValueError(
                f"actuation.{message.purpose} omits counter_signal, but {location} "
                f"has recovered rolling counter {expected_counter or '(unnamed)'}"
            )
        if message.counter_signal is not None and analysis.counter is None:
            raise ValueError(
                f"actuation.{message.purpose} names a counter signal, but no "
                f"counter was recovered for {location}; refusing to guess its "
                "width, step, or wrap point"
            )
        if (
            message.counter_signal is not None
            and message.counter_signal != expected_counter
        ):
            raise ValueError(
                f"actuation.{message.purpose}.counter_signal must be "
                f"{expected_counter!r}, the counter verified in {location}"
            )

        checksum_field = next(
            (
                signal for signal in _all_signals(analysis)
                if analysis.checksum is not None
                and signal.kind == "checksum"
                and signal.start == analysis.checksum.start
                and signal.length == analysis.checksum.length
            ),
            None,
        )
        expected_checksum = (
            checksum_field.default_name(analysis.addr)
            if checksum_field is not None else None
        )
        if analysis.checksum is not None and message.checksum_signal is None:
            raise ValueError(
                f"actuation.{message.purpose} omits checksum_signal, but {location} "
                f"has recovered checksum {expected_checksum or '(unnamed)'}"
            )
        if message.checksum_signal and analysis.checksum is None:
            raise ValueError(
                f"actuation.{message.purpose} names a checksum signal, but no "
                f"checksum was solved for {location}; openpilot would send "
                "frames the car rejects"
            )
        if (
            message.checksum_signal is not None
            and message.checksum_signal != expected_checksum
        ):
            raise ValueError(
                f"actuation.{message.purpose}.checksum_signal must be "
                f"{expected_checksum!r}, the checksum verified in {location}"
            )
        if analysis.checksum is not None and (
            analysis.checksum.accuracy != 1.0
            or analysis.checksum.n_verified < 20
            or analysis.checksum.underdetermined
        ):
            reason = (
                "is underdetermined"
                if analysis.checksum.underdetermined
                else "does not reproduce every captured frame exactly"
            )
            raise ValueError(
                f"actuation.{message.purpose} cannot use {location}: its "
                f"checksum solution {reason}; collect more varied traffic"
            )

        kind = (
            info.lateral_tuning.kind
            if info.lateral_tuning is not None else "torque"
        )
        allowed_values = {
            "lateral": {"lat_active", "enabled", "frame", "counter"}
            | ({"apply_angle"} if kind == "angle" else {"apply_torque"}),
            "longitudinal": {
                "accel", "gas", "brake", "long_active", "enabled", "frame",
                "counter",
            },
        }[message.purpose]
        for signal_name, value in message.signals:
            if isinstance(value, str) and value not in allowed_values:
                raise ValueError(
                    f"actuation.{message.purpose}.signals.{signal_name} uses "
                    f"{value!r}, which is not defined for a {message.purpose} "
                    f"{kind if message.purpose == 'lateral' else ''} command"
                )
            if value == "counter" and message.counter_signal is None:
                raise ValueError(
                    f"actuation.{message.purpose}.signals.{signal_name} uses "
                    "'counter' without declaring counter_signal"
                )


def apply_vehicle_info(
    analyses: Iterable[MessageAnalysis], info: VehicleInfo
) -> None:
    """Apply validated manual signal facts to recovered analyses in place."""
    by_key = {analysis.key: analysis for analysis in analyses}
    for override in info.signals:
        analysis = by_key.get((override.bus, override.address))
        location = f"bus{override.bus}:0x{override.address:X}:{override.start_bit}"
        if analysis is None:
            raise ValueError(f"manual signal {location} refers to an unknown message")
        signal = _field_for_override(analysis, override, location)
        signal.name = override.name
        if override.unit is not None:
            signal.unit = override.unit
        if override.scale is not None:
            signal.scale = override.scale
        if override.offset is not None:
            signal.offset = override.offset
        if override.signed is not None:
            signal.signed = override.signed
        if override.byte_order is not None:
            requested_big_endian = override.byte_order == "big"
            if signal.big_endian != requested_big_endian:
                before = set(signal.payload_bits(analysis.length * 8))
                probe = Signal(
                    start=signal.start,
                    length=signal.length,
                    kind=signal.kind,
                    big_endian=requested_big_endian,
                )
                if before != set(probe.payload_bits(analysis.length * 8)):
                    raise ValueError(
                        f"manual signal {location} cannot change byte order in "
                        "place because that changes which payload bits it occupies"
                    )
            signal.big_endian = requested_big_endian
        if override.carstate_target is not None:
            signal.carstate_target = override.carstate_target
        note = "human-supplied vehicle-info override"
        if note not in signal.notes:
            signal.notes.append(note)

    # After the renames, so a binding may refer to a signal by the name a
    # person just gave it rather than by its placeholder.
    _check_bindings(by_key, info)
