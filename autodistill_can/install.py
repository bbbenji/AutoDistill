"""Install a generated port into an opendbc checkout.

opendbc intentionally keeps two central registries that cannot live inside a
brand directory: the union of platform enums and the lateral-parameter table.
Copying the generated Python and DBC files alone therefore produces a package
that imports, but ``CarInterface.get_params`` cannot construct it.

This installer makes those small, deterministic integrations. It accepts an
AutoDistill ``port_status.json`` in either mode -- installing a control port is
how a person's own measured facts reach a bench -- but refuses any port
claiming ``safe_for_control``, refuses conflicting files by default, and writes
each target atomically.
"""

from __future__ import annotations

import json
import keyword
import os
import re
import tempfile
from pathlib import Path

__all__ = ["install_port"]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _single_filename(name: str) -> str:
    if not name or Path(name).name != name:
        raise ValueError(f"unsafe generated filename in manifest: {name!r}")
    return name


def _alias(brand: str) -> str:
    alias = re.sub(r"[^0-9A-Za-z_]", "_", brand).strip("_").upper()
    alias = re.sub(r"_+", "_", alias) or "MYSTERY"
    if alias[0].isdigit() or keyword.iskeyword(alias.lower()):
        alias = f"CAR_{alias}"
    return alias


def _integrate_platform(values_source: str, *, brand: str) -> str:
    alias = _alias(brand)
    import_line = f"from opendbc.car.{brand}.values import CAR as {alias}"
    if import_line not in values_source:
        marker = "\nPlatform = "
        if marker not in values_source:
            raise ValueError(
                "unrecognised opendbc/car/values.py: no Platform union found"
            )
        values_source = values_source.replace(
            marker, f"\n{import_line}\n{marker.lstrip()}", 1
        )

    lines = values_source.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("Platform = "):
            if not re.search(rf"(?:^|[| ]){re.escape(alias)}(?:$|[| \n])", line):
                lines[index] = line.rstrip("\n") + f" | {alias}\n"
            break
    else:  # pragma: no cover - guarded by the marker check above
        raise ValueError("unrecognised opendbc/car/values.py")
    return "".join(lines)


def _torque_entry(port_dir: Path, *, car_identifier: str) -> str:
    snippet = port_dir / "torque_data_override.toml"
    if not snippet.is_file():
        raise ValueError(f"generated port is missing {snippet.name}")
    entries = [
        line.strip()
        for line in snippet.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(entries) != 1 or not re.match(r'^"[A-Z0-9_]+" = \[', entries[0]):
        raise ValueError("generated torque override must contain exactly one entry")
    if entries[0].split("=", 1)[0].strip() != f'"{car_identifier}"':
        raise ValueError("torque override does not match the manifest car identifier")
    return entries[0]


def _integrate_torque(
    override_source: str, *, entry: str, torque_dir: Path
) -> str:
    key = entry.split("=", 1)[0].strip()
    for name in ("params.toml", "substitute.toml", "override.toml"):
        path = torque_dir / name
        if path.is_file():
            matches = [
                line.strip()
                for line in path.read_text().splitlines()
                if line.strip().startswith(f"{key} =")
            ]
            if matches:
                if name == "override.toml" and matches == [entry]:
                    return override_source
                raise ValueError(
                    f"{key} already has a different torque entry in {path}"
                )
    return override_source.rstrip() + "\n" + entry + "\n"


def install_port(
    port_dir: Path | str,
    opendbc_dir: Path | str,
    *,
    force: bool = False,
) -> list[Path]:
    """Install one generated port and return the files changed.

    ``opendbc_dir`` is the repository root (the directory containing the
    ``opendbc/`` Python package). Existing identical files make this operation
    idempotent. A differing file is treated as a likely human edit and refused
    unless ``force`` is explicitly set.
    """
    port_dir = Path(port_dir).resolve()
    opendbc_dir = Path(opendbc_dir).resolve()
    manifest_path = port_dir / "port_status.json"
    if not manifest_path.is_file():
        raise ValueError(f"not a generated port: missing {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != 1:
        raise ValueError(
            f"unsupported port manifest schema {manifest.get('schema_version')!r}"
        )
    # A control port is a legitimate thing to install -- it is how a person's
    # own actuation facts reach opendbc. What must never be installed is a port
    # claiming AutoDistill vouched for it, which is what `safe_for_control`
    # would mean and which nothing sets.
    if manifest.get("mode") not in ("read_only", "control"):
        raise ValueError(f"unknown port mode {manifest.get('mode')!r}")
    if manifest.get("safe_for_control") is not False:
        raise ValueError("refusing to install a port claiming to be safe for control")

    brand = manifest.get("brand")
    if not isinstance(brand, str) or not brand.isidentifier() or keyword.iskeyword(brand):
        raise ValueError(f"invalid brand in port manifest: {brand!r}")
    car_identifier = manifest.get("car_identifier")
    if (
        not isinstance(car_identifier, str)
        or not car_identifier.isidentifier()
        or keyword.iskeyword(car_identifier)
    ):
        raise ValueError(
            f"invalid car identifier in port manifest: {car_identifier!r}"
        )

    package = opendbc_dir / "opendbc"
    car_dir = package / "car"
    dbc_dir = package / "dbc"
    values_path = car_dir / "values.py"
    torque_dir = car_dir / "torque_data"
    torque_path = torque_dir / "override.toml"
    for required in (car_dir, dbc_dir, values_path, torque_path):
        if not required.exists():
            raise ValueError(
                f"{opendbc_dir} is not a compatible opendbc checkout "
                f"(missing {required.relative_to(opendbc_dir)})"
            )

    planned: dict[Path, str] = {}
    target_brand = car_dir / brand
    python_files = sorted(port_dir.glob("*.py"))
    if not python_files:
        raise ValueError("generated port contains no Python files")
    for source in python_files:
        content = source.read_text()
        compile(content, source.name, "exec")
        planned[target_brand / source.name] = content

    dbc_manifest = manifest.get("dbc_files")
    if not isinstance(dbc_manifest, dict) or not dbc_manifest:
        raise ValueError("generated port manifest contains no DBC files")
    for name in dbc_manifest.values():
        name = _single_filename(name)
        source = port_dir / name
        if not source.is_file():
            raise ValueError(f"generated port is missing DBC {name}")
        planned[dbc_dir / name] = source.read_text()

    planned[values_path] = _integrate_platform(
        values_path.read_text(), brand=brand
    )
    entry = _torque_entry(port_dir, car_identifier=car_identifier)
    planned[torque_path] = _integrate_torque(
        torque_path.read_text(), entry=entry, torque_dir=torque_dir
    )

    conflicts = [
        path for path, content in planned.items()
        if path.exists() and path.read_text() != content
        and path not in (values_path, torque_path)
    ]
    if conflicts and not force:
        names = ", ".join(str(path.relative_to(opendbc_dir)) for path in conflicts[:5])
        raise FileExistsError(
            f"refusing to replace differing opendbc file(s): {names}; "
            "re-run with force=True (CLI: --force) only if intentional"
        )

    changed: list[Path] = []
    for path, content in planned.items():
        if path.exists() and path.read_text() == content:
            continue
        _atomic_write(path, content)
        changed.append(path)
    return changed


def fingerprint_collisions(
    port_dir: Path | str, opendbc_dir: Path | str
) -> list[str]:
    """Platforms already in opendbc that this port's fingerprint matches.

    Worth checking at install time and not before, because it is the only
    moment the comparison data is certainly present. A collision does not stop
    the install -- the port may be perfectly correct and the two cars genuinely
    similar -- but openpilot picking the wrong one is not a failure anybody
    would diagnose from the symptoms.
    """
    from .collide import check_fingerprint, fingerprint_addresses

    port_dir = Path(port_dir)
    try:
        addresses = fingerprint_addresses(port_dir)
        manifest = json.loads((port_dir / "port_status.json").read_text())
    except (OSError, ValueError):  # pragma: no cover - defensive
        return []
    # The check runs after the copy, so without this the port finds itself and
    # every single install would warn. A warning that always fires is one
    # people learn to scroll past, which costs more than it saves.
    itself = f"{manifest.get('brand')}.{manifest.get('car_identifier')}"
    return [
        f"{collision.platform}: {collision.message}"
        for collision in check_fingerprint(addresses, opendbc_dir)
        if collision.platform != itself
    ]
