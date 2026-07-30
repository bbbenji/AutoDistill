"""Validate emitted DBCs against a real, independent DBC reader.

Our own decoder agreeing with our own encoder proves nothing about the file we
hand to somebody else. cantools is what opendbc, cabana and most tooling
actually use, so these tests load the generated file with cantools and check it
(a) parses at all and (b) decodes to the values we claim.

Both halves have caught real bugs: multiplexed messages emitted with plain
signals laid over multiplexed ones, and 29-bit identifiers written without the
flag that marks them extended. Each produced a file that looked fine and was
rejected outright by every real consumer.
"""

from __future__ import annotations

import io

import pytest

cantools = pytest.importorskip("cantools", reason="cantools not installed")

from autodistill_can.emit.dbc import dbc_frame_id, write_dbc
from autodistill_can.frame import extract_be, extract_le_dbc, to_signed


@pytest.fixture(scope="module")
def generated_dbc(analyses):
    buffer = io.StringIO()
    write_dbc(analyses.values(), buffer)
    return buffer.getvalue()


def test_generated_dbc_parses(generated_dbc, analyses, tmp_path):
    path = tmp_path / "generated.dbc"
    path.write_text(generated_dbc)
    db = cantools.database.load_file(path)
    assert len(db.messages) == len(analyses)


def test_generated_dbc_decodes_to_the_values_we_claim(
    generated_dbc, analyses, log, tmp_path
):
    path = tmp_path / "generated.dbc"
    path.write_text(generated_dbc)
    db = cantools.database.load_file(path)

    checked = 0
    for key, analysis in analyses.items():
        if analysis.multiplex is not None:
            # Multiplexed decoding needs the selector to pick a layout; the
            # non-multiplexed messages already exercise the bit arithmetic.
            continue
        message = db.get_message_by_frame_id(dbc_frame_id(analysis.addr))
        stream = log.streams[key]
        for payload in stream.payloads[:50]:
            decoded = message.decode(payload, decode_choices=False)
            for signal in analysis.signals:
                name = signal.default_name(analysis.addr)
                if name not in decoded:
                    continue
                raw = (
                    extract_be(payload, signal.start, signal.length)
                    if signal.big_endian
                    else extract_le_dbc(payload, signal.start, signal.length)
                )
                if signal.signed:
                    raw = to_signed(raw, signal.length)
                expected = raw * signal.scale + signal.offset
                actual = float(decoded[name])
                assert abs(actual - expected) <= 1e-6 * max(1.0, abs(expected)), (
                    f"0x{analysis.addr:X} {name}: cantools read {actual}, "
                    f"we meant {expected}"
                )
                checked += 1

    assert checked > 500, "expected the fixture to exercise many signals"


def test_multiplexed_message_is_valid_and_selectable(generated_dbc, analyses, tmp_path):
    """A multiplexed message must round-trip through a real reader.

    Emitting the whole-message signal list *and* the per-mode lists puts plain
    signals on top of multiplexed ones, which a DBC reader rejects.
    """
    multiplexed = [a for a in analyses.values() if a.multiplex is not None]
    if not multiplexed:
        pytest.skip("no multiplexed message in the fixture")

    path = tmp_path / "generated.dbc"
    path.write_text(generated_dbc)
    db = cantools.database.load_file(path)

    for analysis in multiplexed:
        message = db.get_message_by_frame_id(dbc_frame_id(analysis.addr))
        selectors = [s for s in message.signals if s.is_multiplexer]
        assert len(selectors) == 1, f"0x{analysis.addr:X} needs exactly one selector"
        assert any(s.multiplexer_ids for s in message.signals), (
            f"0x{analysis.addr:X} has a selector but nothing multiplexed by it"
        )


def test_extended_identifiers_are_flagged():
    # A 29-bit id needs bit 31 set, or a reader sees an impossible 11-bit id.
    assert dbc_frame_id(0x1A6) == 0x1A6
    assert dbc_frame_id(0x7FF) == 0x7FF
    assert dbc_frame_id(0x18DAF110) == 0x18DAF110 | 0x80000000
