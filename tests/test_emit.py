"""Output artifacts.

The generated openpilot checksum code gets the closest scrutiny, because it is
the one output whose correctness cannot be eyeballed and whose failure mode is
a car silently ignoring every command openpilot sends. The test executes the
emitted source and checks it reproduces the checksum of every captured frame.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import replace

import pytest

from autodistill_can.algos import honda_checksum, hyundai_crc8
from autodistill_can.analysis.checksum import ChecksumSolution
from autodistill_can.analysis.correlate import correlate_log
from autodistill_can.analysis.fingerprint import build_fingerprint, is_diagnostic_address
from autodistill_can.analysis.uds import request_address, response_address
from autodistill_can.emit.dbc import write_dbc
from autodistill_can.emit.openpilot import write_checksum_function
from autodistill_can.emit.report import write_json, write_text_report
from autodistill_can.frame import CanFrame, extract_be
from autodistill_can.manual import load_vehicle_info

# --------------------------------------------------------------------------
# Generated checksum code
# --------------------------------------------------------------------------


def _generated_checksums(analyses) -> dict[int, object]:
    """Emit every checksum function and exec them into a namespace."""
    source = io.StringIO()
    for analysis in analyses.values():
        write_checksum_function(analysis, source)
    namespace: dict = {}
    exec(compile(source.getvalue(), "<generated>", "exec"), namespace)
    return namespace


def test_generated_checksum_code_reproduces_every_captured_frame(log, analyses):
    namespace = _generated_checksums(analyses)
    verified = 0

    for key, analysis in analyses.items():
        if analysis.checksum is None:
            continue
        function = namespace.get(f"checksum_{analysis.addr:03x}")
        assert function is not None, f"no function emitted for 0x{analysis.addr:X}"

        stream = log.streams[key]
        solution = analysis.checksum
        for payload in stream.payloads:
            expected = extract_be(payload, solution.start, solution.length)
            assert function(analysis.addr, payload) == expected, (
                f"generated checksum for 0x{analysis.addr:X} disagrees on "
                f"{payload.hex()}"
            )
        verified += len(stream.payloads)

    assert verified > 15000, "expected the fixture to exercise this heavily"


def test_generated_checksum_code_is_emitted_for_every_solved_message(analyses):
    for analysis in analyses.values():
        buffer = io.StringIO()
        emitted = write_checksum_function(analysis, buffer)
        assert emitted == (analysis.checksum is not None)


def test_generated_affine_checksum_carries_the_counter_table(analyses):
    analysis = next(a for k, a in analyses.items() if k[1] == 0x1A6)
    buffer = io.StringIO()
    write_checksum_function(analysis, buffer)
    source = buffer.getvalue()
    assert "constants = {" in source
    assert source.count("0x") > 16  # masks plus a 16-entry constant table


def _generated_named_checksum(analyses, *, addr, solution):
    base = next(iter(analyses.values()))
    analysis = replace(base, addr=addr, length=8, checksum=solution)
    source = io.StringIO()
    write_checksum_function(analysis, source)
    namespace = {}
    exec(compile(source.getvalue(), "<generated>", "exec"), namespace)
    return namespace[f"checksum_{addr:03x}"]


def test_generated_hyundai_crc_is_executable(analyses):
    function = _generated_named_checksum(
        analyses,
        addr=0x123,
        solution=ChecksumSolution(
            start=56, length=8, method="algo", accuracy=1.0, n_verified=100,
            algo="crc8_hyundai", input_selection="before",
        ),
    )
    for prefix in (bytes(range(7)), b"\xFF\x00\xA5\x5A\x11\x22\x33"):
        payload = prefix + b"\x00"
        assert function(0x123, payload) == hyundai_crc8(0x123, prefix, 8)


def test_generated_honda_checksum_handles_extended_ids(analyses):
    addr = 0x18DA00F1
    function = _generated_named_checksum(
        analyses,
        addr=addr,
        solution=ChecksumSolution(
            start=60, length=4, method="algo", accuracy=1.0, n_verified=100,
            algo="honda", input_selection="whole",
        ),
    )
    for payload in (
        b"\x00" * 8,
        b"\x12\x34\x56\x78\x9A\xBC\xDE\xF0",
    ):
        assert function(addr, payload) == honda_checksum(addr, payload, 8)


# --------------------------------------------------------------------------
# DBC
# --------------------------------------------------------------------------


def test_dbc_structure(analyses):
    buffer = io.StringIO()
    count = write_dbc(analyses.values(), buffer)
    text = buffer.getvalue()

    assert count == len(analyses)
    assert text.startswith("VERSION")
    assert "BS_:" in text and "BU_:" in text
    assert text.count("BO_ ") >= count

    for analysis in analyses.values():
        assert f"BO_ {analysis.addr} " in text


def test_dbc_signal_lines_are_well_formed(analyses):
    buffer = io.StringIO()
    write_dbc(analyses.values(), buffer)
    pattern = re.compile(
        r"^ SG_ (\w+)( M| m\d+)? : (\d+)\|(\d+)@([01])([+-]) "
        r"\(([-\d.]+),([-\d.]+)\) \[([-\d.]+)\|([-\d.]+)\] \"(\w*)\" \w+$"
    )
    lines = [l for l in buffer.getvalue().splitlines() if l.startswith(" SG_")]
    assert lines
    for line in lines:
        match = pattern.match(line)
        assert match is not None, f"malformed DBC signal line: {line!r}"
        start_bit, length = int(match.group(3)), int(match.group(4))
        assert 0 <= start_bit <= 63
        assert 1 <= length <= 64


def test_dbc_signal_names_are_unique_within_a_message(analyses):
    buffer = io.StringIO()
    write_dbc(analyses.values(), buffer)
    current: list[str] = []
    for line in buffer.getvalue().splitlines():
        if line.startswith("BO_ "):
            assert len(current) == len(set(current))
            current = []
        elif line.startswith(" SG_ "):
            current.append(line.split()[1])
    assert len(current) == len(set(current))


def test_dbc_marks_multiplexed_signals(analyses):
    buffer = io.StringIO()
    write_dbc(analyses.values(), buffer)
    text = buffer.getvalue()
    # 0x6B0 is multiplexed; its selector is M and its mode signals mN.
    body = text.split("BO_ 1712 ")[1].split("BO_ ")[0]
    assert " M : " in body
    assert re.search(r" m\d+ : ", body)


def test_dbc_numbers_never_use_exponent_notation(analyses):
    # A DBC parser will choke on 1e-05; the scale of a fine-grained signal is
    # exactly where Python would reach for scientific notation.
    buffer = io.StringIO()
    write_dbc(analyses.values(), buffer)
    for line in buffer.getvalue().splitlines():
        if line.startswith(" SG_"):
            assert "e-" not in line.lower().split('"')[0]
            assert "e+" not in line.lower().split('"')[0]


# --------------------------------------------------------------------------
# Fingerprint
# --------------------------------------------------------------------------


def test_diagnostic_address_classification():
    for addr in (0x7DF, 0x7E0, 0x7E8, 0x7EF, 0x7B0, 0x7B8, 0x18DAF110):
        assert is_diagnostic_address(addr), hex(addr)
    for addr in (0x025, 0x0AA, 0x1A6, 0x2E4, 0x6B0):
        assert not is_diagnostic_address(addr), hex(addr)


def test_fingerprint_excludes_diagnostics_but_keeps_broadcast_traffic(log):
    fingerprint = build_fingerprint(log)
    assert fingerprint.buses[0]
    assert fingerprint.buses[2] == {0x2E4: 8}
    for addr in fingerprint.buses[0]:
        assert not is_diagnostic_address(addr)
    # Every diagnostic address should be excluded with a stated reason.
    assert 0x7E8 in fingerprint.excluded
    assert "diagnostic" in fingerprint.excluded[0x7E8]


def test_fingerprint_recovers_firmware_versions(log):
    fingerprint = build_fingerprint(log)
    recovered = {r.text() for r in fingerprint.firmware}
    assert recovered == {"89663-33010", "8965B-45070", "89541-06040"}
    request_addresses = set()
    for response in fingerprint.firmware:
        assert response.did == 0xF188
        request_addresses.add(response.request_addr)
    assert request_addresses == {0x7B0, 0x7E0, 0x7E2}


def test_uds_response_addresses_are_converted_to_request_addresses():
    assert request_address(0x7E8) == 0x7E0
    assert request_address(0x7B8) == 0x7B0
    assert request_address(0x18DAF128) == 0x18DA28F1
    assert request_address(0x123) == 0x123
    assert response_address(0x7E0) == 0x7E8
    assert response_address(0x18DA28F1) == 0x18DAF128


def test_fingerprint_can_take_firmware_from_a_separate_log(log):
    traffic_only = type(log).from_frames(
        frame for frame in log.frames() if not is_diagnostic_address(frame.addr)
    )
    assert not build_fingerprint(traffic_only).firmware
    recovered = build_fingerprint(traffic_only, diagnostic_log=log)
    assert {r.text() for r in recovered.firmware} == {
        "89663-33010", "8965B-45070", "89541-06040"
    }


def test_fingerprint_excludes_extended_broadcast_ids(log):
    frames = list(log.frames())
    frames.extend(
        CanFrame(t=100 + i, addr=0x18FF1234, data=b"\x00" * 8, bus=0)
        for i in range(3)
    )
    extended = type(log).from_frames(frames)
    fingerprint = build_fingerprint(extended)
    assert 0x18FF1234 not in fingerprint.buses[0]
    assert "29-bit" in fingerprint.excluded[0x18FF1234]


def test_fingerprint_ignores_broadcast_traffic_that_looks_like_isotp(log):
    # Ordinary payloads parse as valid ISO-TP more often than you would think,
    # and phantom firmware strings in a fingerprint are worse than none.
    fingerprint = build_fingerprint(log)
    for response in fingerprint.firmware:
        assert is_diagnostic_address(response.addr)


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------


def test_text_report_mentions_what_it_could_not_determine(log, analyses):
    buffer = io.StringIO()
    write_text_report(
        analyses.values(), buffer, fingerprint=build_fingerprint(log)
    )
    text = buffer.getvalue()
    assert "AutoDistill analysis" in text
    assert "What this capture could not determine" in text
    # No reference was supplied, so it must say so rather than stay silent.
    assert "--reference" in text


def test_json_report_is_valid_and_complete(log, analyses, reference):
    matches = correlate_log(
        log.streams, list(analyses.values()), reference, assign_names=False
    )
    buffer = io.StringIO()
    write_json(
        analyses.values(), buffer, fingerprint=build_fingerprint(log),
        matches=matches,
    )
    payload = json.loads(buffer.getvalue())

    assert len(payload["messages"]) == len(analyses)
    assert payload["fingerprint"]["firmware"]
    assert {
        row["request_address"] for row in payload["fingerprint"]["firmware"]
    } == {"0x7B0", "0x7E0", "0x7E2"}
    assert payload["correlations"]

    message = next(m for m in payload["messages"] if m["address"] == 0x025)
    assert message["checksum"]["algorithm"] == "toyota"
    assert message["counter"]["step"] == 1
    assert message["signals"]


def test_reports_preserve_human_supplied_knowledge(analyses):
    info = load_vehicle_info({
        "vehicle_specs": {"mass_kg": 1775, "wheelbase_m": 2.71},
        "ecu_types": {"0x7E0": "engine"},
        "engineering": {
            "safety_notes": ["Validate on a bench"],
            "sources": ["Workshop manual page 44"],
        },
    })
    machine = io.StringIO()
    write_json(analyses.values(), machine, vehicle_info=info)
    payload = json.loads(machine.getvalue())
    assert payload["manual_info"] == info.to_dict()

    human = io.StringIO()
    write_text_report(analyses.values(), human, vehicle_info=info)
    text = human.getvalue()
    assert "Human-supplied knowledge (not inferred" in text
    assert "mass: 1775.0 kg" in text
    assert "ECU 0x7E0: engine" in text
    assert "Validate on a bench" in text
    assert "never enable vehicle control" in text


def test_dbc_refuses_to_collapse_duplicate_addresses_across_buses(analyses):
    analysis = next(iter(analyses.values()))
    duplicate = replace(analysis, bus=analysis.bus + 1)
    buffer = io.StringIO()
    with pytest.raises(ValueError, match="one DBC per bus"):
        write_dbc([analysis, duplicate], buffer)
    assert buffer.getvalue() == ""
