"""Human-readable and machine-readable reports.

The text report is the main thing a person reads, so it is written to be
skimmed: what was captured, what was worked out, and — the part most tools skip
— what could *not* be worked out and what to record next time to fix that. A
reverse-engineering session is iterative, and the report's job is to tell you
what to do on the next drive.
"""

from __future__ import annotations

import json
import textwrap
from typing import IO, Iterable

from ..analysis.correlate import Match
from ..analysis.fingerprint import Fingerprint
from ..analysis.message import MessageAnalysis
from ..manual import VehicleInfo

__all__ = ["write_json", "write_text_report"]


def _signal_row(signal, addr: int) -> str:
    name = signal.default_name(addr)
    order = "big" if signal.big_endian else "little"
    sign = "signed" if signal.signed else "unsigned"
    detail = f"{signal.length:2d}b {order:<6} {sign:<8}"
    if signal.kind == "constant":
        detail = f"{signal.length:2d}b constant 0x{signal.raw_min:X}"
    elif signal.kind in ("counter", "checksum"):
        detail = f"{signal.length:2d}b {signal.kind}"

    scale = ""
    if signal.scale != 1.0 or signal.offset:
        scale = f" scale={signal.scale:g} offset={signal.offset:g}"
    unit = f" [{signal.unit}]" if signal.unit else ""
    return (
        f"    bits {signal.start:2d}-{signal.end - 1:2d}  {detail}  "
        f"{name}{unit}{scale}  conf={signal.confidence:.2f}"
    )


def write_text_report(
    analyses: Iterable[MessageAnalysis],
    out: IO[str],
    *,
    fingerprint: Fingerprint | None = None,
    matches: Iterable[Match] | None = None,
    capture_summary: str = "",
    vehicle_info: VehicleInfo | None = None,
) -> None:
    """Write the full human-readable analysis."""
    analyses = sorted(analyses, key=lambda a: (a.bus, a.addr))

    out.write("=" * 72 + "\n")
    out.write("AutoDistill analysis\n")
    out.write("=" * 72 + "\n")
    if capture_summary:
        out.write(f"{capture_summary}\n")
    out.write("\n")

    # Before the results, not after: if the recording is a gateway capture,
    # everything below it is a description of diagnostic chatter and reading
    # it as a description of the car is the mistake to head off.
    _write_capture_findings(analyses, out)

    for analysis in analyses:
        rate = f"{analysis.frequency:.1f} Hz" if analysis.frequency else "?"
        out.write(
            f"bus {analysis.bus}  0x{analysis.addr:03X}  {analysis.length} bytes  "
            f"{analysis.n_frames} frames  {rate}\n"
        )
        if analysis.multiplex is not None:
            mux = analysis.multiplex
            out.write(
                f"    MULTIPLEXED by bits {mux.start}-{mux.end - 1} "
                f"({len(mux.groups)} modes)\n"
            )
            for value, signals in sorted(analysis.mux_signals.items()):
                out.write(f"    mode {value}:\n")
                for signal in signals:
                    if signal.kind == "constant":
                        continue
                    out.write("  " + _signal_row(signal, analysis.addr) + "\n")
        for signal in analysis.signals:
            out.write(_signal_row(signal, analysis.addr) + "\n")
        if analysis.checksum is not None:
            checksum = analysis.checksum
            out.write(f"    checksum: {checksum.describe()}\n")
            if checksum.n_mismatches:
                out.write(
                    f"      NOTE: {checksum.n_mismatches} of {checksum.n_verified} "
                    "frames do not match this formula\n"
                )
            # A named algorithm's note is its formula, which describe() has
            # already shown; only the solver's own commentary adds anything.
            if analysis.checksum.note and analysis.checksum.method != "algo":
                out.write(f"      {analysis.checksum.note}\n")
        for note in analysis.notes:
            out.write(f"    note: {note}\n")
        out.write("\n")

    if matches:
        matches = list(matches)
        if matches:
            out.write("-" * 72 + "\n")
            out.write("Signals identified by correlation with the reference log\n")
            out.write("-" * 72 + "\n")
            for match in matches:
                out.write(f"  {match}  [{match.quality}]\n")
            out.write("\n")

    if fingerprint is not None:
        out.write("-" * 72 + "\n")
        out.write("openpilot fingerprint\n")
        out.write("-" * 72 + "\n")
        for bus in sorted(fingerprint.buses):
            messages = fingerprint.buses[bus]
            out.write(f"  bus {bus}: {len(messages)} messages\n")
            out.write(
                "    "
                + ", ".join(
                    f"{addr}: {length}" for addr, length in sorted(messages.items())
                )
                + "\n"
            )
        if fingerprint.firmware:
            out.write(f"  firmware versions ({len(fingerprint.firmware)}):\n")
            for response in fingerprint.firmware:
                out.write(f"    {response}\n")
        if fingerprint.vin:
            out.write(f"  VIN: {fingerprint.vin}\n")
        out.write("\n")

    if vehicle_info is not None and vehicle_info.has_content:
        _write_manual_info(vehicle_info, out)

    _write_next_steps(analyses, out, fingerprint, matches)


def _write_manual_info(info: VehicleInfo, out: IO[str]) -> None:
    """Keep human evidence visible and distinct from inferred evidence."""
    out.write("-" * 72 + "\n")
    out.write("Human-supplied knowledge (not inferred by AutoDistill)\n")
    out.write("-" * 72 + "\n")
    for label, value, unit in (
        ("mass", info.mass_kg, " kg"),
        ("wheelbase", info.wheelbase_m, " m"),
        ("steering ratio", info.steer_ratio, ""),
        ("docs package", info.docs_package, ""),
    ):
        if value is not None:
            out.write(f"  {label}: {value}{unit}\n")
    for address, ecu in sorted(info.ecu_types.items()):
        out.write(f"  ECU 0x{address:X}: {ecu}\n")
    for signal in info.signals:
        target = f" -> {signal.carstate_target}" if signal.carstate_target else ""
        length = f", length {signal.length}" if signal.length is not None else ""
        unit = f", unit {signal.unit}" if signal.unit else ""
        scale = f", scale {signal.scale}" if signal.scale is not None else ""
        offset = f", offset {signal.offset}" if signal.offset is not None else ""
        out.write(
            f"  signal bus {signal.bus} 0x{signal.address:X} bit "
            f"{signal.start_bit}{length}: {signal.name}{unit}{scale}{offset}"
            f"{target}\n"
        )
    for heading, values in (
        ("actuation note", info.actuation_notes),
        ("safety note", info.safety_notes),
        ("source", info.sources),
    ):
        for value in values:
            out.write(f"  {heading}: {value}\n")
    out.write(
        "  These claims still require review and never enable vehicle control.\n\n"
    )


def _write_capture_findings(analyses: list[MessageAnalysis], out: IO[str]) -> None:
    """Anything about the recording that limits every result below it."""
    from ..quality import capture_findings

    findings = capture_findings(analyses)
    if not findings:
        return
    for finding in findings:
        marker = "!!" if finding.level == "blocking" else "! "
        out.write(f"{marker} {finding.title}\n")
        for line in textwrap.wrap(finding.detail, width=68):
            out.write(f"   {line}\n")
        out.write("\n")


def _write_next_steps(
    analyses: list[MessageAnalysis],
    out: IO[str],
    fingerprint: Fingerprint | None,
    matches,
) -> None:
    """Say what the capture could not determine, and how to fix it."""
    suggestions: list[str] = []

    static_heavy = [
        a for a in analyses
        if a.stats.nbits
        and len(a.stats.constant_bits()) > a.stats.nbits * 0.75
    ]
    if static_heavy:
        addrs = ", ".join(f"0x{a.addr:X}" for a in static_heavy[:8])
        suggestions.append(
            f"{len(static_heavy)} message(s) barely changed ({addrs}). Their bits "
            "cannot be split into fields without something to move them. Record a "
            "drive that exercises doors, blinkers, gear positions, wipers and "
            "cruise control."
        )

    if not matches:
        suggestions.append(
            "No reference log was supplied, so no signal could be given a real "
            "name or scale factor. Record GPS speed or OBD-II PIDs alongside the "
            "CAN capture and pass it with --reference; that is what turns "
            "'16-bit field at bit 0' into 'wheel speed, 0.01 km/h per bit'."
        )

    unsolved = [a for a in analyses if a.checksum is None and a.counter is not None]
    if unsolved:
        addrs = ", ".join(f"0x{a.addr:X}" for a in unsolved[:8])
        suggestions.append(
            f"{len(unsolved)} message(s) have a counter but no checksum was found "
            f"({addrs}). If openpilot must send one of these, the checksum has to "
            "be solved first -- a longer capture with more payload variety gives "
            "the solver more to work with."
        )

    underdetermined = [
        a for a in analyses
        if a.checksum is not None and a.checksum.underdetermined
    ]
    if underdetermined:
        addrs = ", ".join(f"0x{a.addr:X}" for a in underdetermined[:8])
        suggestions.append(
            f"{len(underdetermined)} checksum(s) fit the data but are not uniquely "
            f"determined by it ({addrs}). They reproduce every frame captured, but "
            "may be wrong for payloads the capture never contained. Drive the car "
            "harder through whatever those messages report."
        )

    if fingerprint is not None and not fingerprint.firmware:
        suggestions.append(
            "No firmware versions were seen. openpilot fingerprints most cars by "
            "firmware; run `autodistill-can probe` to query the ECUs directly "
            "(this "
            "transmits on the bus -- read the warning first)."
        )

    if not suggestions:
        return

    out.write("-" * 72 + "\n")
    out.write("What this capture could not determine\n")
    out.write("-" * 72 + "\n")
    for i, suggestion in enumerate(suggestions, start=1):
        out.write(f"  {i}. {suggestion}\n\n")


def write_json(
    analyses: Iterable[MessageAnalysis],
    out: IO[str],
    *,
    fingerprint: Fingerprint | None = None,
    matches: Iterable[Match] | None = None,
    vehicle_info: VehicleInfo | None = None,
) -> None:
    """Write the analysis as JSON, for feeding into other tools."""
    from ..coverage import evaluate
    from ..quality import capture_findings

    analyses = list(analyses)
    payload: dict = {"messages": []}
    if vehicle_info is not None and vehicle_info.has_content:
        payload["manual_info"] = vehicle_info.to_dict()
    # What a finished port still needs. Carried in the report so a consumer --
    # the local UI, most obviously -- can show the gap without re-analysing
    # several million frames to work it out again.
    payload["coverage"] = evaluate(analyses, vehicle_info).to_dict()
    # Whether the recording was worth analysing at all. Carried alongside the
    # results because the answer changes how much any of them mean.
    payload["capture"] = [f.to_dict() for f in capture_findings(analyses)]

    for analysis in sorted(analyses, key=lambda a: (a.bus, a.addr)):
        entry = {
            "bus": analysis.bus,
            "address": analysis.addr,
            "address_hex": f"0x{analysis.addr:X}",
            # The DBC message name, so a consumer can quote the same one the
            # generated CarState will index with.
            "name": analysis.name,
            "length": analysis.length,
            "frames": analysis.n_frames,
            "frequency_hz": analysis.frequency,
            "jitter_s": analysis.jitter,
            "event_driven": analysis.is_event_driven,
            "notes": analysis.notes,
            "signals": [
                {
                    "name": s.default_name(analysis.addr),
                    "start_bit": s.start,
                    "length": s.length,
                    "byte_order": s.byte_order,
                    "signed": s.signed,
                    "kind": s.kind,
                    "scale": s.scale,
                    "offset": s.offset,
                    "unit": s.unit,
                    "confidence": round(s.confidence, 4),
                    "raw_min": s.raw_min,
                    "raw_max": s.raw_max,
                    "distinct_values": s.distinct,
                    "notes": s.notes,
                    "carstate_target": s.carstate_target or None,
                }
                for s in analysis.signals
            ],
        }
        if analysis.mux_signals:
            entry["multiplexed"] = {
                str(mode): [
                    {
                        "name": s.default_name(analysis.addr),
                        "start_bit": s.start,
                        "length": s.length,
                        "kind": s.kind,
                        "unit": s.unit,
                    }
                    for s in signals
                ]
                for mode, signals in sorted(analysis.mux_signals.items())
            }
        if analysis.counter is not None:
            entry["counter"] = {
                "start_bit": analysis.counter.start,
                "length": analysis.counter.length,
                "step": analysis.counter.step,
                "match_rate": round(analysis.counter.score, 6),
            }
        if analysis.checksum is not None:
            checksum = analysis.checksum
            entry["checksum"] = {
                "start_bit": checksum.start,
                "length": checksum.length,
                "method": checksum.method,
                "algorithm": checksum.algo,
                "input_selection": checksum.input_selection,
                "crc_name": checksum.crc_name,
                "description": checksum.describe(),
                "accuracy": round(checksum.accuracy, 6),
                "verified_on": checksum.n_verified,
                "underdetermined": checksum.underdetermined,
                "masks": [f"0x{m:X}" for m in checksum.masks],
                "input_bits": checksum.input_bits,
                "counter_constants": {
                    str(k): v for k, v in sorted(checksum.counter_constants.items())
                },
            }
        if analysis.multiplex is not None:
            entry["multiplex"] = {
                "start_bit": analysis.multiplex.start,
                "length": analysis.multiplex.length,
                "modes": sorted(analysis.multiplex.groups),
                "gain": round(analysis.multiplex.gain, 4),
            }
        payload["messages"].append(entry)

    if fingerprint is not None:
        payload["fingerprint"] = {
            "buses": {
                str(bus): {str(a): l for a, l in sorted(msgs.items())}
                for bus, msgs in sorted(fingerprint.buses.items())
            },
            "excluded": {
                f"0x{addr:X}": reason
                for addr, reason in sorted(fingerprint.excluded.items())
            },
            "vin": fingerprint.vin,
            "firmware": [
                {
                    "address": f"0x{r.request_addr:X}",
                    "request_address": f"0x{r.request_addr:X}",
                    "response_address": f"0x{r.addr:X}",
                    "data_identifier": r.did_name,
                    "text": r.text(),
                    "raw": r.payload.hex(),
                }
                for r in fingerprint.firmware
            ],
        }

    if matches:
        payload["correlations"] = [
            {
                "bus": m.bus,
                "address": f"0x{m.addr:X}",
                "start_bit": m.signal.start,
                "length": m.signal.length,
                "channel": m.channel,
                "r": round(m.r, 6),
                "scale": m.scale,
                "offset": m.offset,
                "residual": m.residual,
                "quality": m.quality,
            }
            for m in matches
        ]

    json.dump(payload, out, indent=2)
    out.write("\n")
