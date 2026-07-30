"""One definition of each CarState transform, used two ways.

:func:`render` writes the transform as Python for the generated ``carstate.py``.
:func:`apply` performs it here, so a capture can be replayed through the same
bindings and the results checked before anyone installs anything.

Those two must agree. They are deliberately in one small file, next to each
other, and ``tests/test_transforms.py`` evaluates the rendered source against
the applied value for every transform -- because a validator that quietly
disagrees with the code it is validating is worse than no validator.
"""

from __future__ import annotations

from typing import Any

__all__ = ["KPH_TO_MS", "MPH_TO_MS", "RAD_TO_DEG", "apply", "render"]

#: opendbc's Conversions, as multipliers. Shared with the port writer so the
#: generated `CV.KPH_TO_MS` and the value computed here mean the same thing.
KPH_TO_MS = 0.277778
MPH_TO_MS = 0.44704
RAD_TO_DEG = 57.2958


def render(binding, base: str) -> str:
    """The transform as a Python expression over ``base``."""
    transform = binding.transform
    if transform == "identity":
        return base
    if transform == "bool":
        return f"bool({base})"
    if transform == "invert_bool":
        return f"not bool({base})"
    if transform == "threshold":
        return f"{base} > {binding.threshold!r}"
    if transform == "equals":
        return f"{base} == {binding.threshold!r}"
    if transform == "scale":
        expression = f"{base} * {binding.scale!r}"
        if binding.offset:
            expression = f"({expression} + {binding.offset!r})"
        return expression
    if transform == "kph_to_ms":
        return f"{base} * CV.KPH_TO_MS"
    if transform == "mph_to_ms":
        return f"{base} * CV.MPH_TO_MS"
    if transform == "rad_to_deg":
        return f"{base} * CV.RAD_TO_DEG"
    if transform == "percent":
        return f"{base} / 100."
    if transform == "gear_map":
        return f"GEAR_MAP.get(int({base}), structs.CarState.GearShifter.unknown)"
    raise AssertionError(f"unknown transform {transform!r}")


def apply(binding, value: float) -> Any:
    """The same transform, evaluated on one decoded signal value."""
    transform = binding.transform
    if transform == "identity":
        return value
    if transform == "bool":
        return bool(value)
    if transform == "invert_bool":
        return not bool(value)
    if transform == "threshold":
        return value > binding.threshold
    if transform == "equals":
        return value == binding.threshold
    if transform == "scale":
        result = value * binding.scale
        return result + binding.offset if binding.offset else result
    if transform == "kph_to_ms":
        return value * KPH_TO_MS
    if transform == "mph_to_ms":
        return value * MPH_TO_MS
    if transform == "rad_to_deg":
        return value * RAD_TO_DEG
    if transform == "percent":
        return value / 100.0
    if transform == "gear_map":
        return binding.gear_map.get(int(value), "unknown")
    raise AssertionError(f"unknown transform {transform!r}")
