"""Replay a capture through the bindings and see whether the result is sane.

Everything else in AutoDistill checks that generated code is *well formed*: it
compiles, the DBC loads, the interface constructs. None of that notices that
``ret.brakePressed`` was bound to the wrong bit, or that a speed in km/h was
read as m/s. Those are the mistakes a person actually makes when binding
fields by hand, and they are invisible until a car does something surprising.

So this decodes the capture the way the generated ``CarState`` would and looks
at the values. A speed that reaches 90 m/s, a brake that is pressed in every
frame, a gear selector that never once says drive: none of those need a car to
diagnose, only the recording you already have.

The checks are deliberately conservative. Every one of them describes something
that cannot be true of a real car, so a finding is a real problem rather than a
prompt to go and look. Things that are merely suspicious are reported as notes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .analysis.message import MessageAnalysis
from .frame import CanLog
from .manual import VehicleInfo

__all__ = ["Finding", "FieldReport", "ValidationReport", "validate"]

#: Nothing on a road does 90 m/s (324 km/h). A field that does is a unit
#: error, almost always km/h read as m/s.
_MAX_SPEED_MS = 90.0
#: Beyond about two and a half turns either way is not a steering wheel.
_MAX_ANGLE_DEG = 900.0


@dataclass(frozen=True)
class Finding:
    """One thing wrong, or worth a second look."""

    level: str  # "error" | "warning" | "note"
    target: str
    message: str


@dataclass
class FieldReport:
    """What one CarState field actually did over the capture."""

    target: str
    source: str
    origin: str
    samples: int = 0
    minimum: float = 0.0
    maximum: float = 0.0
    distinct: int = 0
    #: For booleans: how many frames were true.
    true_frames: int = 0
    #: For ``gear_map``: the gear names actually seen.
    gears: tuple[str, ...] = ()

    @property
    def constant(self) -> bool:
        return self.distinct <= 1


@dataclass
class ValidationReport:
    fields: list[FieldReport] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checked_fields": len(self.fields),
            "fields": [
                {
                    "target": report.target,
                    "source": report.source,
                    "origin": report.origin,
                    "samples": report.samples,
                    "min": round(report.minimum, 6),
                    "max": round(report.maximum, 6),
                    "distinct": report.distinct,
                    "true_frames": report.true_frames,
                    "gears": list(report.gears),
                }
                for report in self.fields
            ],
            "findings": [
                {"level": f.level, "target": f.target, "message": f.message}
                for f in self.findings
            ],
        }


def _series(
    log: CanLog, analysis: MessageAnalysis, signal_name: str
) -> tuple[list[float], object] | None:
    """The decoded, scaled time series of one named signal, and the signal."""
    stream = log.streams.get((analysis.bus, analysis.addr))
    if stream is None:
        return None
    signal = next(
        (
            s for s in analysis.signals
            if s.default_name(analysis.addr) == signal_name
        ),
        None,
    )
    if signal is None:
        return None
    # The DBC scale and offset are applied by CANParser in the real port, so
    # they have to be applied here too or every comparison is off by a factor.
    values = [raw * signal.scale + signal.offset for raw in signal.values(stream)]
    return values, signal


#: Unit strings that mean a speed, and the transform that converts them.
_SPEED_UNITS = {
    "kmh": "kph_to_ms", "kph": "kph_to_ms", "km/h": "kph_to_ms",
    "kilometreperhour": "kph_to_ms",
    "mph": "mph_to_ms", "mi/h": "mph_to_ms", "milesperhour": "mph_to_ms",
}


def _unit_findings(binding, signal, transform: str | None) -> list[Finding]:
    """Catch a speed left in the unit the car reports it in.

    A range check cannot do this on its own: a capture that never exceeds
    90km/h read as m/s is still under any plausible ceiling, so it looks fine
    while being wrong by a factor of 3.6. The unit the correlation recovered
    says so outright.
    """
    if binding.target not in ("ret.vEgoRaw", "ret.cruiseState.speed"):
        return []
    unit = re.sub(r"[\s_.-]", "", (signal.unit or "").strip().lower())
    wanted = _SPEED_UNITS.get(unit)
    if wanted is None or transform is None or transform == wanted:
        return []
    if transform == "scale":
        return []  # an explicit factor is the user saying they know
    return [Finding(
        "error", binding.target,
        f"{binding.described} is in {signal.unit}, but the binding uses "
        f"'{transform}'. openpilot wants m/s -- use the {wanted} transform, "
        "or the value is wrong by a constant factor at every speed.",
    )]


def _numeric_findings(report: FieldReport) -> list[Finding]:
    target, out = report.target, []
    if report.constant:
        out.append(Finding(
            "warning", target,
            f"never changes (always {report.minimum:g}). Either the capture "
            "never exercised it or the binding is on the wrong field.",
        ))
    if target in ("ret.vEgoRaw", "ret.cruiseState.speed"):
        if report.minimum < -0.5:
            out.append(Finding(
                "error", target,
                f"goes negative ({report.minimum:.1f} m/s). A speed cannot; "
                "this is usually a signed/unsigned or byte-order error.",
            ))
        if report.maximum > _MAX_SPEED_MS:
            out.append(Finding(
                "error", target,
                f"reaches {report.maximum:.0f} m/s ({report.maximum * 3.6:.0f} "
                "km/h). Almost certainly a unit error -- try the kph_to_ms or "
                "mph_to_ms transform.",
            ))
    if target == "ret.steeringAngleDeg":
        extreme = max(abs(report.minimum), abs(report.maximum))
        if extreme > _MAX_ANGLE_DEG:
            out.append(Finding(
                "error", target,
                f"reaches {extreme:.0f} degrees. A steering wheel does not "
                "turn that far; check the scale and signedness.",
            ))
    if target in ("ret.gas", "ret.brake") and (
        report.minimum < -0.05 or report.maximum > 1.05
    ):
        out.append(Finding(
            "error", target,
            f"ranges {report.minimum:.2f} to {report.maximum:.2f}, but "
            "openpilot expects 0 to 1. Use the percent or scale transform.",
        ))
    return out


def _boolean_findings(report: FieldReport) -> list[Finding]:
    target, out = report.target, []
    if report.true_frames == 0:
        out.append(Finding(
            "note", target,
            "never true in this capture, so the binding is untested. Record a "
            "few seconds while exercising it.",
        ))
    elif report.true_frames == report.samples:
        blocking = target in (
            "ret.brakePressed", "ret.gasPressed", "ret.seatbeltUnlatched",
            "ret.doorOpen",
        )
        message = (
            "true in every frame. openpilot would never engage. The binding "
            "is probably on the wrong bit, or needs invert_bool."
            if blocking else
            "true in every frame, so this capture never exercised its inactive "
            "state. Confirm the binding with a capture that toggles it."
        )
        out.append(Finding(
            "error" if blocking else "warning", target, message,
        ))
    return out


def validate(
    analyses: Iterable[MessageAnalysis],
    log: CanLog,
    info: VehicleInfo | None = None,
    *,
    main_bus: int = 0,
    camera_bus: int | None = None,
) -> ValidationReport:
    """Decode the capture through each CarState binding and check the result."""
    from .analysis.fingerprint import Fingerprint
    from .emit.port import PortSpec, carstate_bindings

    info = info or VehicleInfo()
    analyses = list(analyses)
    if camera_bus is None:
        camera_bus = next(
            (a.bus for a in analyses if a.bus != main_bus and a.length > 0), None
        )
    spec = PortSpec(
        brand="validate", car_name="VALIDATE", analyses=analyses,
        fingerprint=Fingerprint(), main_bus=main_bus, camera_bus=camera_bus,
        vehicle_info=info,
    )
    gear_bound = any(b.transform == "gear_map" for b in info.carstate)
    declared = {b.target: b.transform for b in info.carstate}
    report = ValidationReport()

    for binding in carstate_bindings(spec):
        found = _series(log, binding.analysis, binding.signal)
        if found is None:
            report.findings.append(Finding(
                "error", binding.target,
                f"{binding.described} is not in the capture, so the generated "
                "CarState would raise on its first frame.",
            ))
            continue
        series, signal = found
        report.findings.extend(
            _unit_findings(binding, signal, declared.get(binding.target))
        )

        # One path for both origins: the binding carries the same computation
        # the generated line performs, unit conversion included.
        values = [binding.evaluate(raw) for raw in series]

        entry = FieldReport(
            target=binding.target,
            source=binding.described,
            origin=binding.origin,
            samples=len(values),
        )
        if binding.target == "ret.gearShifter" and gear_bound:
            entry.gears = tuple(sorted({str(v) for v in values}))
            entry.distinct = len(entry.gears)
            report.fields.append(entry)
            if "drive" not in entry.gears:
                report.findings.append(Finding(
                    "error", binding.target,
                    "never reads drive in this capture. openpilot only engages "
                    f"in drive, so it never would. Seen: {', '.join(entry.gears)}.",
                ))
            continue

        numbers = [float(v) for v in values]
        entry.minimum = min(numbers)
        entry.maximum = max(numbers)
        entry.distinct = len(set(numbers))
        entry.true_frames = sum(1 for v in numbers if v)
        report.fields.append(entry)

        # A field whose every value is True or False is a flag, whichever
        # route produced it: `bool(...)`, `> threshold`, or a raw single bit.
        if all(isinstance(v, bool) for v in values) or (
            entry.distinct <= 2 and {entry.minimum, entry.maximum} <= {0.0, 1.0}
        ):
            report.findings.extend(_boolean_findings(entry))
        else:
            report.findings.extend(_numeric_findings(entry))

    if not report.fields:
        report.findings.append(Finding(
            "warning", "",
            "No CarState field is bound, so there was nothing to check.",
        ))
    return report
