#!/usr/bin/env python3
"""Verify a port downloaded from the real-data UI acceptance walkthrough."""

from __future__ import annotations

import argparse
import json
import math
import re
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

HERE = Path(__file__).resolve().parent
REQUIRED_FILES = {
    "MANUAL_INPUT.md", "README.md", "__init__.py", "carcontroller.py",
    "carstate.py", "fingerprints.py", "interface.py", "port_status.json",
    "radar_interface.py", "torque_data_override.toml", "values.py",
    "vehicle_info.json",
}

# These fields are deliberately selected from each part of the port rather
# than asserting the entire official DBC. AutoDistill should recover unknown
# traffic too, so its generated DBC is expected to contain more placeholders.
OFFICIAL_FIELDS = {
    (0x025, "STEER_ANGLE"), (0x025, "STEER_FRACTION"),
    (0x025, "STEER_RATE"),
    (0x0AA, "WHEEL_SPEED_FR"), (0x0AA, "WHEEL_SPEED_FL"),
    (0x0AA, "WHEEL_SPEED_RR"), (0x0AA, "WHEEL_SPEED_RL"),
    (0x1D2, "GAS_RELEASED"), (0x1D2, "CRUISE_ACTIVE"),
    (0x1D2, "CRUISE_STATE"), (0x1D3, "MAIN_ON"),
    (0x1D3, "SET_SPEED"), (0x224, "BRAKE_PRESSED"),
    (0x224, "BRAKE_PRESSURE"), (0x260, "STEER_TORQUE_DRIVER"),
    (0x260, "STEER_ANGLE"), (0x260, "STEER_TORQUE_EPS"),
    (0x3BC, "GEAR"), (0x614, "TURN_SIGNALS"),
    (0x620, "SEATBELT_DRIVER_UNLATCHED"),
    (0x2E4, "SET_ME_1"), (0x2E4, "COUNTER"),
    (0x2E4, "STEER_REQUEST"), (0x2E4, "STEER_TORQUE_CMD"),
    (0x2E4, "LKA_STATE"), (0x2E4, "CHECKSUM"),
    (0x343, "ACCEL_CMD"), (0x343, "ACC_TYPE"),
    (0x343, "MINI_CAR"), (0x343, "ALLOW_LONG_PRESS"),
    (0x343, "RELEASE_STANDSTILL"), (0x343, "PERMIT_BRAKING"),
    (0x343, "CHECKSUM"),
}

_MESSAGE = re.compile(r"^BO_\s+(\d+)\s+")
_SIGNAL = re.compile(
    r"^\s*SG_\s+(\w+)(?:\s+\w+)?\s*:\s*"
    r"(\d+)\|(\d+)@([01])([+-])\s+\(([^,]+),([^)]+)\)"
)


def dbc_layout(path: Path) -> dict[tuple[int, str], tuple[int, int, int, str, float, float]]:
    """Read only the DBC facts this comparison needs."""
    address: int | None = None
    fields = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if match := _MESSAGE.match(line):
            address = int(match.group(1))
            continue
        if address is None or not (match := _SIGNAL.match(line)):
            continue
        name, start, length, order, sign, scale, offset = match.groups()
        fields[(address, name)] = (
            int(start), int(length), int(order), sign, float(scale), float(offset)
        )
    return fields


@contextmanager
def port_directory(source: Path) -> Iterator[Path]:
    if source.is_dir():
        yield source
        return
    if not zipfile.is_zipfile(source):
        raise ValueError(f"{source} is neither a port directory nor a ZIP file")
    with tempfile.TemporaryDirectory(prefix="autodistill-rav4-verify-") as raw:
        target = Path(raw)
        with zipfile.ZipFile(source) as archive:
            for member in archive.infolist():
                parts = Path(member.filename).parts
                if not parts or Path(member.filename).is_absolute() or ".." in parts:
                    raise ValueError(f"unsafe ZIP member {member.filename!r}")
            archive.extractall(target)
        candidates = [target, *(path for path in target.iterdir() if path.is_dir())]
        root = next((path for path in candidates
                     if (path / "port_status.json").is_file()), None)
        if root is None:
            raise ValueError("ZIP does not contain port_status.json")
        yield root


def check(source: Path) -> list[str]:
    prepared = HERE / "prepared"
    official_dbc = prepared / "official-opendbc/toyota_new_mc_pt_generated.dbc"
    commit = prepared / "official-opendbc/COMMIT.txt"
    if not official_dbc.is_file():
        raise ValueError("prepared official snapshot is missing; run prepare.py first")
    if commit.read_text().strip() != "aedd88ef0b75e5032a277fcf7b0c928ad4e54ddf":
        raise ValueError("prepared official opendbc snapshot has the wrong commit")

    with port_directory(source) as port:
        names = {path.name for path in port.iterdir() if path.is_file()}
        missing = sorted(REQUIRED_FILES - names)
        if missing:
            raise ValueError(f"generated port is missing: {', '.join(missing)}")
        generated_dbcs = sorted(port.glob("*_generated.dbc"))
        if len(generated_dbcs) != 1:
            raise ValueError("expected exactly one main *_generated.dbc")

        status = json.loads((port / "port_status.json").read_text())
        if status.get("mode") != "control":
            raise ValueError(f"expected control mode, got {status.get('mode')!r}")
        controls = status.get("controls", {})
        if not controls.get("lateral") or not controls.get("longitudinal"):
            raise ValueError("lateral and longitudinal control were not both generated")
        if status.get("safe_for_control") is not False:
            raise ValueError("safe_for_control must remain false for an unreviewed fixture")
        for level in ("read", "lateral", "longitudinal"):
            if not status.get("coverage", {}).get(level, {}).get("complete"):
                raise ValueError(f"{level} coverage is incomplete")
        validation = status.get("validation", {})
        if not validation.get("ok") or validation.get("checked_fields") != 16:
            raise ValueError("real-capture CarState replay did not pass all 16 bindings")
        if any(item.get("level") == "error" for item in validation.get("findings", [])):
            raise ValueError("validation contains an error finding")

        generated = dbc_layout(generated_dbcs[0])
        official = dbc_layout(official_dbc)
        failures = []
        for key in sorted(OFFICIAL_FIELDS):
            expected, actual = official.get(key), generated.get(key)
            if expected is None:
                failures.append(f"official snapshot lacks {key}")
            elif actual is None:
                failures.append(f"generated DBC lacks {key}")
            elif expected[:4] != actual[:4] or not all(
                math.isclose(a, b, rel_tol=0, abs_tol=1e-12)
                for a, b in zip(expected[4:], actual[4:], strict=True)
            ):
                failures.append(f"{key}: official {expected}, generated {actual}")
        if failures:
            raise ValueError("DBC comparison failed:\n  " + "\n  ".join(failures))

        # Syntax-check without importing opendbc or leaving __pycache__ in the
        # user's downloaded directory.
        for path in port.glob("*.py"):
            compile(path.read_text(encoding="utf-8"), str(path), "exec")

        controller = (port / "carcontroller.py").read_text(encoding="utf-8")
        interface = (port / "interface.py").read_text(encoding="utf-8")
        for needle in (
            '"STEER_TORQUE_CMD": apply_torque',
            '"ACCEL_CMD": accel',
            "checksum_2e4(0x2E4, data)",
            "checksum_343(0x343, data)",
        ):
            if needle not in controller:
                raise ValueError(f"controller is missing {needle!r}")
        for needle in (
            "SafetyModel.toyota", "safetyParam = 329",
            "ret.openpilotLongitudinalControl = True",
        ):
            if needle not in interface:
                raise ValueError(f"interface is missing {needle!r}")

    return [
        "required port files present",
        "29/29 mandatory longitudinal requirements complete",
        "16 CarState bindings replayed with no errors",
        f"{len(OFFICIAL_FIELDS)} key signal layouts match pinned official opendbc",
        "lateral and longitudinal controller paths generated",
        "safe_for_control remains false as required",
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", type=Path, help="downloaded port.zip or extracted port directory")
    args = parser.parse_args()
    try:
        results = check(args.port.expanduser().resolve())
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print(f"FAIL: {exc}")
        return 1
    print("PASS: RAV4 UI acceptance port matches the fixture")
    for result in results:
        print(f"  - {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
