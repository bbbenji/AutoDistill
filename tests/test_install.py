"""Safe integration of a generated package into an opendbc checkout."""

from __future__ import annotations

import json

import pytest

from autodistill_can.install import install_port


@pytest.fixture
def generated_port(tmp_path):
    port = tmp_path / "port"
    port.mkdir()
    (port / "port_status.json").write_text(json.dumps({
        "schema_version": 1,
        "brand": "testcar",
        "car_identifier": "TEST_CAR",
        "dbc_name": "testcar_generated",
        "mode": "read_only",
        "safe_for_control": False,
        "dbc_files": {
            "0": "testcar_generated.dbc",
            "2": "testcar_generated_bus2.dbc",
        },
    }))
    (port / "__init__.py").write_text("")
    (port / "values.py").write_text("class CAR:\n  pass\n")
    (port / "interface.py").write_text("READ_ONLY = True\n")
    (port / "testcar_generated.dbc").write_text('VERSION "main"\n')
    (port / "testcar_generated_bus2.dbc").write_text('VERSION "bus2"\n')
    (port / "torque_data_override.toml").write_text(
        "# dashcam-only placeholder\n"
        '"TEST_CAR" = [nan, 1.0, nan]\n'
    )
    return port


@pytest.fixture
def opendbc_checkout(tmp_path):
    root = tmp_path / "opendbc-repo"
    car = root / "opendbc" / "car"
    torque = car / "torque_data"
    dbc = root / "opendbc" / "dbc"
    torque.mkdir(parents=True)
    dbc.mkdir(parents=True)
    (car / "values.py").write_text(
        "from opendbc.car.mock.values import CAR as MOCK\n"
        "\n"
        "Platform = MOCK\n"
        "BRANDS = ()\n"
    )
    (torque / "params.toml").write_text(
        'legend = ["LAT_ACCEL_FACTOR", "MAX_LAT_ACCEL_MEASURED", "FRICTION"]\n'
    )
    (torque / "substitute.toml").write_text("")
    (torque / "override.toml").write_text(
        'legend = ["LAT_ACCEL_FACTOR", "MAX_LAT_ACCEL_MEASURED", "FRICTION"]\n'
    )
    return root


def test_install_integrates_all_opendbc_registries(
    generated_port, opendbc_checkout
):
    changed = install_port(generated_port, opendbc_checkout)
    assert changed
    package = opendbc_checkout / "opendbc"
    assert (package / "car" / "testcar" / "interface.py").is_file()
    assert (package / "dbc" / "testcar_generated.dbc").is_file()
    assert (package / "dbc" / "testcar_generated_bus2.dbc").is_file()

    values = (package / "car" / "values.py").read_text()
    assert "from opendbc.car.testcar.values import CAR as TESTCAR" in values
    assert "Platform = MOCK | TESTCAR" in values
    override = (package / "car" / "torque_data" / "override.toml").read_text()
    assert override.count('"TEST_CAR" = [nan, 1.0, nan]') == 1

    # Re-running an unchanged installation is a no-op, not an error.
    assert install_port(generated_port, opendbc_checkout) == []


def test_install_preserves_a_human_edited_file_without_force(
    generated_port, opendbc_checkout
):
    install_port(generated_port, opendbc_checkout)
    target = opendbc_checkout / "opendbc" / "car" / "testcar" / "interface.py"
    target.write_text("# human edit\n")
    with pytest.raises(FileExistsError):
        install_port(generated_port, opendbc_checkout)
    assert target.read_text() == "# human edit\n"

    install_port(generated_port, opendbc_checkout, force=True)
    assert target.read_text() == "READ_ONLY = True\n"


def test_install_refuses_a_port_claiming_to_be_safe_for_control(
    generated_port, opendbc_checkout
):
    """Nothing sets this, so a manifest asserting it has been tampered with."""
    manifest = generated_port / "port_status.json"
    payload = json.loads(manifest.read_text())
    payload["safe_for_control"] = True
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="safe for control"):
        install_port(generated_port, opendbc_checkout)


def test_a_control_port_installs(generated_port, opendbc_checkout):
    """A port carrying a person's own actuation facts is a legitimate install.

    The gate is not "read-only"; it is that AutoDistill never vouched for it.
    Refusing mode=control would have made the control path uninstallable.
    """
    manifest = generated_port / "port_status.json"
    payload = json.loads(manifest.read_text())
    payload["mode"] = "control"
    manifest.write_text(json.dumps(payload))
    assert install_port(generated_port, opendbc_checkout)


def test_install_refuses_an_unknown_port_mode(generated_port, opendbc_checkout):
    manifest = generated_port / "port_status.json"
    payload = json.loads(manifest.read_text())
    payload["mode"] = "whatever"
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="unknown port mode"):
        install_port(generated_port, opendbc_checkout)


ADDRESSES = (67, 127, 128, 273, 274, 275, 339, 356, 544, 593)


def _write_fingerprints(directory, car, addresses=ADDRESSES):
    """A fingerprints.py of the shape the port writer produces."""
    entries = ", ".join(f"{address}: 8" for address in sorted(addresses))
    (directory / "fingerprints.py").write_text(
        f"FINGERPRINTS = {{\n  CAR.{car}: [{{{entries}}}],\n}}\n"
        "FW_VERSIONS = {}\n"
    )


def test_a_port_does_not_report_colliding_with_itself(
    generated_port, opendbc_checkout
):
    """The check runs after the copy, so the port is in the tree it searches.

    Without excluding itself, every install would warn -- and a warning that
    always fires is one people learn to scroll past, which costs more than it
    saves.
    """
    from autodistill_can.install import fingerprint_collisions

    _write_fingerprints(generated_port, "TEST_CAR")
    install_port(generated_port, opendbc_checkout)
    installed = opendbc_checkout / "opendbc" / "car" / "testcar" / "fingerprints.py"
    assert installed.is_file(), "the fixture must actually install a fingerprint"
    assert fingerprint_collisions(generated_port, opendbc_checkout) == []


def test_a_genuinely_colliding_platform_is_reported(
    generated_port, opendbc_checkout
):
    from autodistill_can.collide import fingerprint_addresses
    from autodistill_can.install import fingerprint_collisions

    _write_fingerprints(generated_port, "TEST_CAR")
    install_port(generated_port, opendbc_checkout)
    addresses = fingerprint_addresses(generated_port)
    assert addresses == set(ADDRESSES)

    # A second brand claiming the same message set: openpilot could load
    # either port for this car.
    other = opendbc_checkout / "opendbc" / "car" / "othercar"
    other.mkdir(parents=True, exist_ok=True)
    _write_fingerprints(other, "OTHER_CAR")
    collisions = fingerprint_collisions(generated_port, opendbc_checkout)
    assert any("othercar.OTHER_CAR" in line for line in collisions)
    assert any("firmware versions are the only way" in line for line in collisions)
