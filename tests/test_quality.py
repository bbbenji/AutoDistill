"""Whether the recording was worth analysing, judged before its contents are.

A gatewayed capture is the one failure that looks like a success: the analysis
completes, reports a couple of addresses, finds nothing, and used to say
nothing about why. It is the most common way a first attempt fails and the fix
is not something anyone guesses, so it is worth a check of its own.
"""

from __future__ import annotations

from autodistill_can.analysis.bitstats import BitStats
from autodistill_can.analysis.message import MessageAnalysis
from autodistill_can.analysis.signals import Signal
from autodistill_can.quality import capture_findings


def _message(addr: int, *, frames: int = 12000, period: float = 0.01,
             signals: list[Signal] | None = None) -> MessageAnalysis:
    return MessageAnalysis(
        bus=0, addr=addr, length=8, n_frames=frames, period=period,
        frequency=1 / period, jitter=0.0,
        stats=BitStats(n_samples=frames, nbits=64,
                       flips=[1] * 64, ones=[1] * 64),
        signals=signals or [],
    )


def _levels(findings) -> set[str]:
    return {f.level for f in findings}


def test_a_gatewayed_capture_is_called_out():
    """Only diagnostic addresses answered, which is what a gateway forwards."""
    findings = capture_findings(
        [_message(a) for a in (0x7DF, 0x7E0, 0x7E8, 0x7EA, 0x7B0)]
    )
    gateway = [f for f in findings if "gateway" in f.title.lower()]
    assert gateway, [f.title for f in findings]
    assert gateway[0].level == "blocking"
    # It has to name the fix, not just the symptom.
    assert "harness" in gateway[0].detail


def test_a_normal_capture_is_not_called_gatewayed():
    """Real captures carry some diagnostic traffic; that is not a gateway."""
    messages = [_message(a) for a in (0x25, 0xAA, 0x1D2, 0x1C4, 0x2B0, 0x7E0)]
    assert not [
        f for f in capture_findings(messages) if "gateway" in f.title.lower()
    ]


def test_a_short_capture_is_called_out():
    findings = capture_findings([_message(a, frames=1500) for a in (0x25, 0xAA)])
    assert [f for f in findings if "minutes long" in f.title]


def test_a_long_varied_capture_passes_clean():
    signals = [Signal(start=0, length=8, kind="scalar", confidence=0.9)]
    messages = [_message(a, signals=signals) for a in (0x25, 0xAA, 0x1D2)]
    assert capture_findings(messages) == []


def test_a_capture_where_nothing_moved_is_called_out():
    """A stationary car with the ignition on records exactly this."""
    static = [Signal(start=i * 8, length=8, kind="constant", confidence=1.0)
              for i in range(8)]
    messages = [_message(a, signals=static) for a in (0x25, 0xAA, 0x1D2)]
    findings = capture_findings(messages)
    assert [f for f in findings if "changed" in f.title]


def test_an_empty_capture_is_blocking():
    findings = capture_findings([])
    assert _levels(findings) == {"blocking"}


def test_findings_reach_the_json_report():
    """The web UI reads them from the report rather than re-analysing."""
    import io
    import json

    from autodistill_can.emit.report import write_json

    out = io.StringIO()
    write_json([_message(a) for a in (0x7DF, 0x7E0, 0x7E8)], out)
    payload = json.loads(out.getvalue())
    assert payload["capture"], "capture findings missing from the report"
    assert payload["capture"][0]["level"] == "blocking"
