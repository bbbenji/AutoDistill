"""Whether the recording itself is any good, judged before its contents are.

Every other check here asks what a capture *contains*. These ask whether it was
worth analysing at all, because the three ways a capture is wasted all end the
same way: an analysis that completes, reports a few numbers, and produces
nothing usable, with no indication of why.

The one that matters most is the gateway. On most cars built since roughly the
mid-2010s the OBD-II port sits behind a gateway that forwards only diagnostic
traffic, so a capture taken there holds the replies to your own queries and
almost none of the car talking to itself. It looks like a successful recording.
It is the most common way a first attempt fails, and the fix -- reaching the
bus the forward-facing camera is on -- is not something anyone guesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .analysis.fingerprint import is_diagnostic_address
from .analysis.message import MessageAnalysis

__all__ = ["Finding", "capture_findings"]

#: Above this share of diagnostic messages, a capture is a gateway capture
#: rather than a recording of the car. Set high deliberately: a normal capture
#: taken with the engine running carries a handful of diagnostic addresses too,
#: and calling those gatewayed would be worse than saying nothing.
_GATEWAY_SHARE = 0.8

#: Under this many seconds there is not enough of anything -- too few frames to
#: solve a checksum, too little movement to correlate against, too few counter
#: wraps to be sure of a period.
_SHORT_SECONDS = 120.0

#: A capture where this share of recovered fields never changed value is one
#: where the car was not exercised: sitting in a driveway with the ignition on
#: produces exactly this, as does a drive where nothing but speed varied.
_STATIC_SHARE = 0.9


@dataclass(frozen=True)
class Finding:
    """Something about the recording that limits everything downstream."""

    #: "blocking" -- nothing useful can come of this capture as it stands;
    #: "important" -- results will be weak or unverifiable.
    level: str
    title: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"level": self.level, "title": self.title, "detail": self.detail}


def _duration(analyses: list[MessageAnalysis]) -> float:
    """Seconds spanned, inferred from the messages' own frame counts and rates.

    Taken from the analyses rather than the log so this can run against a
    report that has already been written, which is where the UI reads it.
    """
    spans = [
        a.n_frames * a.period
        for a in analyses
        if a.period and a.n_frames > 1
    ]
    return max(spans) if spans else 0.0


def capture_findings(analyses: Iterable[MessageAnalysis]) -> list[Finding]:
    """Judge the recording. Empty means nothing stood out."""
    analyses = list(analyses)
    if not analyses:
        return [Finding(
            "blocking",
            "The capture holds no messages",
            "Nothing was recorded. Check that the adaptor is on the right bus "
            "and that the interface is up before capturing again.",
        )]

    findings: list[Finding] = []

    diagnostic = [a for a in analyses if is_diagnostic_address(a.addr)]
    share = len(diagnostic) / len(analyses)
    if share >= _GATEWAY_SHARE:
        findings.append(Finding(
            "blocking",
            "This looks like a gatewayed OBD-II capture",
            f"{len(diagnostic)} of {len(analyses)} addresses are diagnostic "
            "ones, so this is mostly replies to queries rather than the car "
            "talking to itself. On most cars built since the mid-2010s the "
            "OBD-II port sits behind a gateway that forwards nothing else. "
            "The bus worth recording is the one the forward-facing camera is "
            "on, behind the windscreen, which is where openpilot's car "
            "harnesses connect. Nothing below this line can be fixed by "
            "analysing this file differently.",
        ))

    seconds = _duration(analyses)
    if 0 < seconds < _SHORT_SECONDS:
        findings.append(Finding(
            "important",
            f"The capture is about {seconds / 60:.1f} minutes long",
            "Short captures leave checksums underdetermined, counters "
            "unconfirmed, and correlations fitted to too little movement. "
            "Fifteen minutes of varied driving is the usual advice.",
        ))

    signals = [s for a in analyses for s in a.signals]
    if signals:
        static = sum(1 for s in signals if s.kind == "constant")
        if static / len(signals) >= _STATIC_SHARE:
            findings.append(Finding(
                "important",
                "Almost nothing in this capture changed",
                f"{static} of {len(signals)} recovered fields held one value "
                "throughout. That is what a stationary car with the ignition "
                "on records. Fields can only be found where something moved, "
                "so drive the route before capturing again.",
            ))

    return findings
