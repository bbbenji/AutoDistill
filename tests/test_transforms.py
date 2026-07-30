"""The rendered transform and the evaluated one must agree.

``render`` writes Python into the generated CarState; ``apply`` computes the
same thing so a capture can be replayed through the bindings without importing
opendbc. If those two ever disagree, the validator quietly stops describing the
code it is validating -- which is worse than having no validator, because it
would report a correct binding as broken or bless a wrong one.

So every transform is checked by evaluating its own rendered source against its
own applied value, in an environment that stands in for openpilot's.
"""

from __future__ import annotations

import pytest

from autodistill_can.manual import CARSTATE_TRANSFORMS, load_vehicle_info
from autodistill_can.transforms import KPH_TO_MS, MPH_TO_MS, RAD_TO_DEG
from autodistill_can.transforms import apply as apply_transform
from autodistill_can.transforms import render as render_transform


class _Conversions:
    """Stands in for opendbc's `Conversions`, with the same constants."""

    KPH_TO_MS = KPH_TO_MS
    MPH_TO_MS = MPH_TO_MS
    RAD_TO_DEG = RAD_TO_DEG


def _binding(transform: str, **extra):
    payload = {
        "target": "ret.vEgoRaw", "bus": 0, "address": "0x100",
        "signal": "FIELD", "transform": transform, **extra,
    }
    if transform == "gear_map":
        payload["target"] = "ret.gearShifter"
    return load_vehicle_info({"carstate": [payload]}).carstate[0]


#: One binding per transform, with whatever companion value it requires.
_CASES = {
    "identity": {},
    "bool": {},
    "invert_bool": {},
    "threshold": {"threshold": 10},
    "equals": {"threshold": 5},
    "scale": {"scale": 0.5, "offset": -3},
    "kph_to_ms": {},
    "mph_to_ms": {},
    "rad_to_deg": {},
    "percent": {},
    "gear_map": {"gear_map": {"0": "park", "5": "drive"}},
}


def test_every_transform_is_covered():
    # A new transform must arrive with a case here, or the agreement check
    # below silently stops covering it.
    assert set(_CASES) == set(CARSTATE_TRANSFORMS)


@pytest.mark.parametrize("transform", sorted(_CASES))
@pytest.mark.parametrize("value", [0, 1, 5, 11.5, -4])
def test_rendered_source_computes_what_apply_computes(transform, value):
    binding = _binding(transform, **_CASES[transform])
    source = render_transform(binding, "VALUE")

    class _Gear:
        """`structs.CarState.GearShifter.park` resolves to the name 'park'."""

        def __getattr__(self, name):
            return name

    namespace = {
        "VALUE": value,
        "CV": _Conversions,
        "GEAR_MAP": {raw: gear for raw, gear in binding.gear_map.items()},
        "structs": type("structs", (), {
            "CarState": type("CarState", (), {"GearShifter": _Gear()}),
        }),
    }
    rendered = eval(source, namespace)  # noqa: S307 - our own generated source
    applied = apply_transform(binding, value)
    if isinstance(applied, float):
        assert rendered == pytest.approx(applied)
    else:
        assert rendered == applied
        assert type(rendered) is type(applied)


def test_gear_map_renders_and_applies_the_same_gear():
    binding = _binding("gear_map", gear_map={"0": "park", "5": "drive"})
    assert apply_transform(binding, 5) == "drive"
    assert apply_transform(binding, 5.0) == "drive"
    # Anything unmapped is unknown, in both directions.
    assert apply_transform(binding, 9) == "unknown"
    assert "GearShifter.unknown" in render_transform(binding, "VALUE")


def test_an_unknown_transform_is_rejected_by_both_halves():
    fake = _binding("identity")
    object.__setattr__(fake, "transform", "telepathy")
    with pytest.raises(AssertionError):
        render_transform(fake, "VALUE")
    with pytest.raises(AssertionError):
        apply_transform(fake, 1)
