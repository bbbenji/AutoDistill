"""Score one capture plus its human-supplied facts against the checklist.

The requirement list in :mod:`.requirements` says what a port needs. This says
which of those a particular capture and vehicle-info file actually cover, and
therefore what is left to do before the port steers or drives the car.

It exists separately from the port writer so that "what is still missing" can
be answered without writing any files -- the CLI reports it, and the web UI
turns it into a form.
"""

from __future__ import annotations

from typing import Iterable

from .analysis.fingerprint import Fingerprint
from .analysis.message import MessageAnalysis
from .emit.port import PortSpec, carstate_bindings
from .manual import VehicleInfo
from .requirements import Coverage, build_coverage

__all__ = ["evaluate"]


def evaluate(
    analyses: Iterable[MessageAnalysis],
    info: VehicleInfo | None = None,
    *,
    main_bus: int = 0,
    camera_bus: int | None = None,
) -> Coverage:
    """Which port requirements this capture and these facts satisfy."""
    info = info or VehicleInfo()
    analyses = list(analyses)
    if camera_bus is None:
        camera_bus = next(
            (a.bus for a in analyses if a.bus != main_bus and a.length > 0), None
        )
    spec = PortSpec(
        brand="coverage",
        car_name="COVERAGE",
        analyses=analyses,
        fingerprint=Fingerprint(),
        main_bus=main_bus,
        camera_bus=camera_bus,
        vehicle_info=info,
    )
    auto: dict[str, str] = {}
    manual: dict[str, str] = {}
    for binding in carstate_bindings(spec):
        where = manual if binding.origin == "manual" else auto
        where[binding.target] = binding.described

    for key, value, unit in (
        ("specs.mass", info.mass_kg, "kg"),
        ("specs.wheelbase", info.wheelbase_m, "m"),
        ("specs.steerRatio", info.steer_ratio, ""),
        ("specs.centerToFront", info.center_to_front_m, "m"),
        ("specs.tireStiffnessFactor", info.tire_stiffness_factor, ""),
        ("specs.harness", info.harness, ""),
    ):
        if value is not None:
            manual[key] = f"{value}{unit}".strip()

    for message in info.actuation:
        manual[f"control.{message.purpose}"] = (
            f"{message.message} on bus {message.bus} at 0x{message.address:X}"
        )

    limits = info.limits
    for key, value in (
        ("limits.steer_max", limits.steer_max),
        ("limits.steer_delta_up", limits.steer_delta_up),
        ("limits.steer_delta_down", limits.steer_delta_down),
        ("limits.steer_driver_allowance", limits.steer_driver_allowance),
        ("limits.steer_actuator_delay", limits.steer_actuator_delay),
        ("limits.accel_min", limits.accel_min),
        ("limits.accel_max", limits.accel_max),
    ):
        if value is not None:
            manual[key] = repr(value)

    if info.lateral_tuning is not None and info.lateral_tuning.complete:
        manual["tuning.lateral"] = f"{info.lateral_tuning.kind} tuning"
    if info.longitudinal_tuning is not None and info.longitudinal_tuning.complete:
        manual["tuning.longitudinal"] = "gains supplied"
    if info.safety is not None:
        manual["safety.model"] = info.safety.model

    return build_coverage(auto=auto, manual=manual)
