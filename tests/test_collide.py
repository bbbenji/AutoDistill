"""A fingerprint that matches somebody else's car is a silent failure.

openpilot identifies a car by the set of addresses it broadcasts. If the set a
capture produced is contained in an existing platform's, that platform matches
this car too, and openpilot may load the wrong port -- with another vehicle's
steering limits. Nothing about the symptoms would lead anyone to the cause.

Most brands upstream have moved to firmware-based fingerprinting, so the pool
this compares against is small and shrinking. It is still worth doing: the
comparison is cheap, and a generated port does ship an address table.
"""

from __future__ import annotations

import textwrap

import pytest

from autodistill_can.collide import (
    check_fingerprint,
    fingerprint_addresses,
    load_opendbc_fingerprints,
)


@pytest.fixture
def opendbc(tmp_path):
    """An opendbc-shaped tree with two overlapping platforms."""
    brand = tmp_path / "opendbc" / "car" / "hyundai"
    brand.mkdir(parents=True)
    (brand / "fingerprints.py").write_text(textwrap.dedent("""
        from opendbc.car.hyundai.values import CAR

        FINGERPRINTS = {
          CAR.BIG_CAR: [{
            67: 8, 127: 8, 128: 8, 273: 8, 274: 8, 275: 8, 339: 8, 356: 4,
            544: 8, 593: 8, 688: 5, 832: 8, 897: 8, 902: 8, 903: 8, 916: 8,
          }],
          CAR.SMALL_CAR: [{
            67: 8, 127: 8, 128: 8,
          }],
        }
        FW_VERSIONS = {}
    """))
    return tmp_path


def test_platforms_are_parsed_without_importing_opendbc(opendbc):
    # Parsed, not imported: opendbc pulls in compiled extensions, and this has
    # to work wherever the port was generated.
    platforms = load_opendbc_fingerprints(opendbc)
    assert set(platforms) == {"hyundai.BIG_CAR", "hyundai.SMALL_CAR"}
    assert len(platforms["hyundai.BIG_CAR"]) == 16


def test_a_subset_fingerprint_is_reported(opendbc):
    ours = {67, 127, 128, 273, 274, 275, 339, 356}
    collisions = check_fingerprint(ours, opendbc)
    assert [c.platform for c in collisions] == ["hyundai.BIG_CAR"]
    assert collisions[0].relation == "subset"
    assert "may identify this car as that platform" in collisions[0].message


def test_a_superset_fingerprint_is_reported(opendbc):
    ours = {67, 127, 128, 273, 999, 1000, 1001, 1002, 1003}
    collisions = check_fingerprint(ours, opendbc, minimum=3)
    assert any(c.relation == "superset" for c in collisions)


def test_an_identical_fingerprint_says_firmware_is_the_only_way(opendbc):
    ours = load_opendbc_fingerprints(opendbc)["hyundai.BIG_CAR"]
    collisions = check_fingerprint(set(ours), opendbc)
    assert collisions[0].relation == "identical"
    assert "firmware versions are the only way" in collisions[0].message


def test_a_distinct_fingerprint_collides_with_nothing(opendbc):
    # The false-positive check. A warning that fires on a correct port is
    # worse than none, because it teaches people to ignore the real one.
    assert check_fingerprint({2000 + i for i in range(30)}, opendbc) == []


def test_tiny_platforms_are_not_compared_against(opendbc):
    # A three-address table says more about that port being sparse than about
    # this car resembling it.
    ours = {67, 127, 128, 555, 556}
    assert not any(
        c.platform == "hyundai.SMALL_CAR" for c in check_fingerprint(ours, opendbc)
    )


def test_a_directory_that_is_not_opendbc_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="does not look like an opendbc"):
        load_opendbc_fingerprints(tmp_path)


def test_addresses_are_read_back_out_of_a_generated_port(analyses, log, tmp_path):
    from autodistill_can.analysis.fingerprint import build_fingerprint
    from autodistill_can.emit.port import write_port

    write_port(
        list(analyses.values()), build_fingerprint(log), tmp_path / "port",
        brand="testcar", car_name="TEST CAR",
    )
    addresses = fingerprint_addresses(tmp_path / "port")
    assert addresses
    # Every one is a real address the capture contained on the main bus.
    seen = {a.addr for a in analyses.values() if a.bus == 0}
    assert addresses <= seen
