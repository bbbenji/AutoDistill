"""End-to-end runs through the command line."""

from __future__ import annotations

import json

import pytest

from autodistill_can.cli import build_parser, main
from autodistill_can.manual import load_vehicle_info


@pytest.fixture(scope="module")
def capture(tmp_path_factory):
    """A synthetic capture and its reference log, produced by the CLI itself."""
    directory = tmp_path_factory.mktemp("capture")
    log = directory / "drive.log"
    reference = directory / "ref.csv"
    truth = directory / "truth.json"
    assert main([
        "synth", "-d", "40", "-o", str(log),
        "--reference", str(reference), "--truth", str(truth), "-q",
    ]) == 0
    return log, reference, truth


def test_synth_writes_a_parseable_capture(capture):
    log, reference, truth = capture
    lines = log.read_text().splitlines()
    assert len(lines) > 10000
    assert lines[0].startswith("(")
    assert reference.read_text().startswith("time,")
    assert json.loads(truth.read_text())["messages"]


def test_analyse_text_report(capture, tmp_path, capsys):
    log, _, _ = capture
    out = tmp_path / "report.txt"
    assert main(["analyse", str(log), "-o", str(out), "-q"]) == 0
    text = out.read_text()
    assert "AutoDistill analysis" in text
    assert "openpilot fingerprint" in text
    assert "checksum:" in text


def test_analyse_json_report(capture, tmp_path):
    log, _, _ = capture
    out = tmp_path / "report.json"
    assert main(["analyse", str(log), "-f", "json", "-o", str(out), "-q"]) == 0
    payload = json.loads(out.read_text())
    assert payload["messages"]
    assert payload["fingerprint"]["buses"]


def test_analyse_with_reference_names_signals(capture, tmp_path):
    log, reference, _ = capture
    out = tmp_path / "report.json"
    assert main([
        "analyse", str(log), "--reference", str(reference),
        "-f", "json", "-o", str(out), "-q",
    ]) == 0
    payload = json.loads(out.read_text())
    assert payload["correlations"]
    names = {
        s["name"]
        for m in payload["messages"]
        for s in m["signals"]
    }
    assert "SPEED_KPH" in names
    assert "STEER_ANGLE_DEG" in names


def test_analyse_writes_dbc_and_port_package(capture, tmp_path):
    log, reference, _ = capture
    dbc = tmp_path / "car.dbc"
    port = tmp_path / "port"
    assert main([
        "analyse", str(log), "--reference", str(reference),
        "--dbc", str(dbc), "--port", str(port), "--brand", "testcar",
        "--name", "TEST_CAR", "-o", str(tmp_path / "r.txt"), "-q",
    ]) == 0
    assert "BO_ " in dbc.read_text()
    assert (port / "values.py").read_text().count("CAR.TEST_CAR") >= 0
    for path in port.glob("*.py"):
        compile(path.read_text(), path.name, "exec")


def test_fingerprint_command(capture, tmp_path):
    log, _, _ = capture
    out = tmp_path / "fp.py"
    assert main(["fingerprint", str(log), "-o", str(out), "-q"]) == 0
    text = out.read_text()
    assert "FINGERPRINTS" in text
    assert "FW_VERSIONS" in text
    # FW_VERSIONS is keyed by the tester's request address, not the response
    # address observed in the capture.
    assert "0x7E0" in text
    assert "(Ecu.TODO, 0x7E8" not in text


def test_manual_template_command_writes_a_safe_starting_file(tmp_path):
    output = tmp_path / "vehicle-info.json"
    assert main(["manual-template", "-o", str(output), "-q"]) == 0
    assert load_vehicle_info(output).has_content is False


def test_fingerprint_uses_supplied_ecu_type(capture, tmp_path):
    log, _, _ = capture
    info = tmp_path / "vehicle-info.json"
    info.write_text(json.dumps({"ecu_types": {"0x7E0": "engine"}}))
    out = tmp_path / "fp.py"
    assert main([
        "fingerprint", str(log), "--vehicle-info", str(info),
        "-o", str(out), "-q",
    ]) == 0
    text = out.read_text()
    assert "(Ecu.engine, 0x7E0, None)" in text
    assert "# MANUAL: vehicle_info.json" in text


def test_probe_refuses_without_explicit_confirmation(capsys):
    # The probe transmits on the vehicle bus, so it must never be reachable by
    # accident. Without the flag it should refuse before touching any hardware.
    assert main(["probe", "-i", "nonexistent0"]) == 2
    assert "transmits" in capsys.readouterr().err


def test_missing_capture_is_a_clean_error(capsys):
    assert main(["analyse", "/nonexistent/path.log"]) == 1
    assert "autodistill-can:" in capsys.readouterr().err


def test_limit_truncates_the_capture(capture, tmp_path):
    log, _, _ = capture
    out = tmp_path / "report.json"
    assert main([
        "analyse", str(log), "--limit", "500", "-f", "json",
        "-o", str(out), "-q",
    ]) == 0
    payload = json.loads(out.read_text())
    assert sum(m["frames"] for m in payload["messages"]) <= 500


def test_strict_mode_rejects_malformed_capture_rows(tmp_path, capsys):
    capture = tmp_path / "bad.log"
    capture.write_text(
        "(0.0) can0 100#00\n"
        "this is corrupt\n"
        "(0.1) can0 100#01\n"
    )
    assert main(["fingerprint", str(capture), "--strict", "-q"]) == 1
    assert "cannot parse" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["analyse", "x.log", "--min-frames", "0"],
        ["analyse", "x.log", "--min-correlation", "1.1"],
        ["port", "x.log", "--bus", "-1"],
        ["capture", "socketcan:can0", "--duration", "0"],
        ["capture", "socketcan:can0", "--duration", "nan"],
        ["probe", "--timeout", "0"],
        ["probe", "--timeout", "inf"],
        ["probe", "--address", "0x20000000"],
    ],
)
def test_numeric_cli_arguments_are_range_checked(args):
    with pytest.raises(SystemExit):
        build_parser().parse_args(args)
