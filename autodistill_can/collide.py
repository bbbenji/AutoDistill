"""Check a generated fingerprint against the platforms opendbc already ships.

openpilot identifies a car by the set of message addresses it broadcasts. If
the set this capture produced is a *subset* of an existing platform's, that
platform will match this car too -- and openpilot may load somebody else's port
for your vehicle, with their steering limits and their safety model.

The failure is silent and the symptom is bizarre, so it is worth ruling out
before an install rather than after. The check needs a real opendbc checkout,
which the user already has if they are installing into one.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Collision", "check_fingerprint", "load_opendbc_fingerprints"]


@dataclass(frozen=True)
class Collision:
    """An existing platform this fingerprint cannot be told apart from."""

    platform: str
    #: "subset" -- ours is contained in theirs, so theirs also matches us.
    #: "superset" -- theirs is contained in ours, so we also match theirs.
    #: "identical" -- neither can ever be distinguished by messages alone.
    relation: str
    shared: int
    theirs: int

    @property
    def message(self) -> str:
        if self.relation == "identical":
            return (
                f"identical message set to {self.platform} "
                f"({self.shared} addresses). Fingerprinting cannot tell these "
                "two apart at all; firmware versions are the only way."
            )
        if self.relation == "subset":
            return (
                f"every one of this car's {self.shared} addresses also appears "
                f"in {self.platform}, which broadcasts {self.theirs}. openpilot "
                "may identify this car as that platform and load its port."
            )
        return (
            f"this car's addresses include all {self.shared} of "
            f"{self.platform}'s. That platform's fingerprint will match this "
            "car, so whichever is tried first wins."
        )


def load_opendbc_fingerprints(opendbc_dir: Path | str) -> dict[str, set[int]]:
    """Every shipped platform's address set, read from opendbc's source.

    Parsed rather than imported: importing opendbc pulls in compiled
    extensions, and this has to work anywhere the port was generated. The
    tables are plain literals, so :mod:`ast` reads them exactly.
    """
    root = Path(opendbc_dir).expanduser()
    car = root / "opendbc" / "car"
    if not car.is_dir():
        raise ValueError(f"{root} does not look like an opendbc checkout")

    found: dict[str, set[int]] = {}
    for path in sorted(car.glob("*/fingerprints.py")):
        brand = path.parent.name
        try:
            tree = ast.parse(path.read_text())
        except (OSError, SyntaxError):  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            names = {
                t.id for t in node.targets if isinstance(t, ast.Name)
            }
            if "FINGERPRINTS" not in names:
                continue
            for key, value in zip(
                getattr(node.value, "keys", []), getattr(node.value, "values", [])
            ):
                platform = _platform_name(key, brand)
                addresses: set[int] = set()
                for entry in getattr(value, "elts", []):
                    for address in getattr(entry, "keys", []):
                        if isinstance(address, ast.Constant) and isinstance(
                            address.value, int
                        ):
                            addresses.add(address.value)
                if addresses:
                    found[platform] = addresses
    return found


def _platform_name(node: ast.expr, brand: str) -> str:
    """`CAR.HYUNDAI_SONATA` -> `hyundai.HYUNDAI_SONATA`, best effort."""
    if isinstance(node, ast.Attribute):
        return f"{brand}.{node.attr}"
    if isinstance(node, ast.Constant):
        return f"{brand}.{node.value}"
    return f"{brand}.{ast.dump(node)[:40]}"


def check_fingerprint(
    addresses: set[int],
    opendbc_dir: Path | str,
    *,
    minimum: int = 8,
) -> list[Collision]:
    """Platforms whose fingerprint cannot be distinguished from this one.

    ``minimum`` skips comparisons against platforms with very few addresses,
    where a subset relation says more about the other port's sparse table than
    about this car.
    """
    if not addresses:
        return []
    collisions: list[Collision] = []
    for platform, theirs in load_opendbc_fingerprints(opendbc_dir).items():
        if len(theirs) < minimum:
            continue
        if addresses == theirs:
            relation = "identical"
        elif addresses <= theirs:
            relation = "subset"
        elif theirs <= addresses:
            relation = "superset"
        else:
            continue
        collisions.append(
            Collision(platform, relation, len(addresses & theirs), len(theirs))
        )
    collisions.sort(key=lambda c: (c.relation != "identical", -c.shared))
    return collisions


def fingerprint_addresses(port_dir: Path | str) -> set[int]:
    """The main-bus addresses a generated port claims, from its own source."""
    source = (Path(port_dir) / "fingerprints.py").read_text()
    block = source[source.index("FINGERPRINTS"):]
    block = block[: block.index("FW_VERSIONS")] if "FW_VERSIONS" in block else block
    return {int(pair[0]) for pair in re.findall(r"(\d+):\s*(\d+)", block)}
