"""The downloadable real-data walkthrough must remain a valid complete fixture."""

from pathlib import Path

from autodistill_can.manual import load_vehicle_info

ROOT = Path(__file__).parents[1]
KIT = ROOT / "examples/ui_acceptance"


def test_rav4_vehicle_info_is_complete_and_loadable():
    info = load_vehicle_info(KIT / "vehicle-info.json")
    assert (info.mass_kg, info.wheelbase_m, info.steer_ratio) == (
        1655.61175, 2.65, 16.88,
    )
    assert len(info.signals) == 39
    assert len(info.carstate) == 16
    assert info.command("lateral").address == 0x2E4
    assert info.command("longitudinal").address == 0x343
    assert info.limits.lateral_complete
    assert info.lateral_tuning.complete
    assert info.longitudinal_tuning.complete
    assert info.safety.model == "toyota" and info.safety.param == 329


def test_acceptance_sources_are_immutable_and_the_large_output_is_ignored():
    source = (KIT / "prepare.py").read_text(encoding="utf-8")
    for commit in (
        "4c7f1a6e1957745beadc1def0e7225f559b09a2a",
        "ab79999e5dc2de25c3eb0c9acbed47c9e81f5d79",
        "aedd88ef0b75e5032a277fcf7b0c928ad4e54ddf",
    ):
        assert commit in source
    assert (KIT / ".gitignore").read_text().splitlines() == ["prepared/"]


def test_walkthrough_names_every_file_and_expected_result():
    readme = (KIT / "README.md").read_text(encoding="utf-8")
    for name in (
        "rav4-complete.log", "rav4-main.log", "rav4-control.log",
        "rav4-reference.csv",
        "vehicle-info.json", "official-opendbc/", "verify.py",
    ):
        assert name in readme
    for expectation in (
        "106,800 frames", "29/29", "safe_for_control: false", "33 key DBC",
        "Open the generated DBC in Cabana", "candump file(s)",
        "toyota_ui_acceptance_generated_bus1.dbc",
    ):
        assert expectation in readme
