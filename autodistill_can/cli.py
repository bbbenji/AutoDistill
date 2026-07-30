"""Command-line interface."""

from __future__ import annotations

import argparse
import math
import sys
import time
from typing import Iterable

from .analysis.correlate import correlate_log, load_reference_csv
from .analysis.fingerprint import build_fingerprint
from .analysis.message import MessageAnalysis, analyse_log, detect_byte_order
from .frame import CanFrame, CanLog, concatenate
from .sources import open_source

__all__ = ["main"]


def _load_log(
    uri: str, *, limit: int | None = None, strict: bool = False
) -> CanLog:
    frames = open_source(uri, strict=strict)
    log = CanLog()
    try:
        for i, frame in enumerate(frames):
            if limit is not None and i >= limit:
                break
            log.add(frame)
    finally:
        close = getattr(frames, "close", None)
        if callable(close):
            close()
    return log


def _load_capture(args: argparse.Namespace) -> CanLog:
    """The capture to analyse, including any extra ones merged onto its end.

    Extra captures are laid end to end rather than interleaved -- see
    `frame.concatenate` for why that distinction matters. They widen what the
    analysis has seen: a field only shows up if something moved it, and one
    drive rarely exercises indicators, reverse and blind-spot alike.
    """
    log = _load_log(args.source, limit=args.limit, strict=args.strict)
    extra = getattr(args, "add_log", None)
    if not extra:
        return log
    logs = [log]
    for path in extra:
        logs.append(_load_log(path, strict=args.strict))
    return concatenate(logs)


def _fingerprint_with_firmware(
    log: CanLog, firmware_logs: list[str] | None, *, strict: bool = False
):
    """Build a fingerprint, optionally taking UDS traffic from probe logs."""
    if not firmware_logs:
        return build_fingerprint(log)
    diagnostic_log = CanLog()
    for path in firmware_logs:
        extra = _load_log(path, strict=strict)
        for frame in extra.frames():
            diagnostic_log.add(frame)
    if not diagnostic_log.streams:
        raise ValueError("firmware log(s) contained no CAN frames")
    diagnostic_log.sort()
    return build_fingerprint(log, diagnostic_log=diagnostic_log)


def _analyse_all(
    log: CanLog, *, min_frames: int, quiet: bool
) -> list[MessageAnalysis]:
    streams = log.sorted_streams()
    if not quiet:
        print("deciding byte order for the car...", end="", file=sys.stderr, flush=True)
    lsb_first = detect_byte_order(streams, min_frames=min_frames)
    if not quiet:
        order = {True: "Intel (little-endian)", False: "Motorola (big-endian)"}.get(
            lsb_first, "undetermined; deciding per message"
        )
        print(f"\rbyte order: {order}" + " " * 20, file=sys.stderr)
        if len(streams) > 1:
            print(f"analysing {len(streams)} message streams...", file=sys.stderr)

    return analyse_log(log, min_frames=min_frames, lsb_first=lsb_first)


def _open_out(path: str | None):
    """Return a writable handle, defaulting to stdout for ``-`` or None."""
    if path is None or path == "-":
        return sys.stdout, False
    return open(path, "w"), True


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_analyse(args: argparse.Namespace) -> int:
    from .emit.dbc import write_dbc
    from .emit.port import write_port
    from .emit.report import write_json, write_text_report
    from .manual import apply_vehicle_info, load_vehicle_info

    started = time.time()
    vehicle_info = load_vehicle_info(args.vehicle_info)
    log = _load_capture(args)
    if not log.streams:
        print(f"no CAN frames found in {args.source!r}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"loaded {log.summary()}", file=sys.stderr)

    analyses = _analyse_all(log, min_frames=args.min_frames, quiet=args.quiet)
    fingerprint = _fingerprint_with_firmware(
        log, args.firmware_log, strict=args.strict
    )

    matches = []
    if args.reference:
        reference = load_reference_csv(
            args.reference, time_column=args.reference_time
        )
        if not args.quiet:
            print(
                f"reference: {len(reference)} rows, channels "
                f"{sorted(reference.channels)}",
                file=sys.stderr,
            )
        matches = correlate_log(
            log.streams, analyses, reference, min_abs_r=args.min_correlation
        )
    apply_vehicle_info(analyses, vehicle_info)

    out, should_close = _open_out(args.output)
    try:
        if args.format == "json":
            write_json(
                analyses, out, fingerprint=fingerprint, matches=matches,
                vehicle_info=vehicle_info,
            )
        else:
            write_text_report(
                analyses,
                out,
                fingerprint=fingerprint,
                matches=matches,
                capture_summary=log.summary(),
                vehicle_info=vehicle_info,
            )
    finally:
        if should_close:
            out.close()

    if args.dbc:
        dbc_analyses = [a for a in analyses if a.bus == args.bus]
        if not dbc_analyses:
            raise ValueError(f"no analysed messages found on DBC bus {args.bus}")
        with open(args.dbc, "w") as fh:
            count = write_dbc(dbc_analyses, fh, title=args.name)
        if not args.quiet:
            print(f"wrote {count} messages to {args.dbc}", file=sys.stderr)

    if args.port:
        files = write_port(
            analyses, fingerprint, args.port,
            brand=args.brand, car_name=args.name, main_bus=args.bus,
            camera_bus=args.camera_bus,
            force=args.force, vehicle_info=vehicle_info, log=log,
        )
        if not args.quiet:
            print(
                f"wrote {len(files)} port files to {args.port}; "
                f"read {args.port}/README.md before installing",
                file=sys.stderr,
            )

    if not args.quiet:
        print(f"done in {time.time() - started:.1f}s", file=sys.stderr)
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    from .sources.candump import write_candump

    source = open_source(
        args.source, duration=args.duration, limit=args.limit
    )
    out, should_close = _open_out(args.output)
    written = 0
    try:
        started = time.time()
        buffer: list[CanFrame] = []
        for frame in source:
            buffer.append(frame)
            if len(buffer) >= 512:
                written += write_candump(buffer, out)
                buffer.clear()
                out.flush()
                if not args.quiet:
                    print(
                        f"\r{written} frames, {time.time() - started:.0f}s  ",
                        end="",
                        file=sys.stderr,
                        flush=True,
                    )
        written += write_candump(buffer, out)
    except KeyboardInterrupt:
        if not args.quiet:
            print("\ninterrupted", file=sys.stderr)
    finally:
        if should_close:
            out.close()
    if not args.quiet:
        print(f"\ncaptured {written} frames", file=sys.stderr)
    return 0


def cmd_port(args: argparse.Namespace) -> int:
    """Write a complete read-only openpilot bootstrap package."""
    from .analysis.correlate import correlate_log, load_reference_csv
    from .emit.port import write_port
    from .manual import apply_vehicle_info, load_vehicle_info

    vehicle_info = load_vehicle_info(args.vehicle_info)
    log = _load_capture(args)
    if not log.streams:
        print(f"no CAN frames found in {args.source!r}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"loaded {log.summary()}", file=sys.stderr)

    analyses = _analyse_all(log, min_frames=args.min_frames, quiet=args.quiet)
    fingerprint = _fingerprint_with_firmware(
        log, args.firmware_log, strict=args.strict
    )

    if args.reference:
        reference = load_reference_csv(
            args.reference, time_column=args.reference_time
        )
        correlate_log(log.streams, analyses, reference)
    elif not args.quiet:
        print(
            "note: no --reference given, so no signal can be named and the "
            "generated CarState will be empty. That is the single most useful "
            "thing to add.",
            file=sys.stderr,
        )
    apply_vehicle_info(analyses, vehicle_info)

    files = write_port(
        analyses, fingerprint, args.output or f"{args.brand}_port",
        brand=args.brand, car_name=args.name, main_bus=args.bus, force=args.force,
        camera_bus=args.camera_bus,
        vehicle_info=vehicle_info, log=log,
    )
    if not args.quiet:
        print(f"wrote {len(files)} files to {files[0].parent}", file=sys.stderr)
        print(f"read {files[0].parent / 'README.md'} before installing it",
              file=sys.stderr)
    return 0


def cmd_fingerprint(args: argparse.Namespace) -> int:
    from .emit.openpilot import write_fingerprint
    from .manual import load_vehicle_info

    vehicle_info = load_vehicle_info(args.vehicle_info)
    log = _load_capture(args)
    if not log.streams:
        print(f"no CAN frames found in {args.source!r}", file=sys.stderr)
        return 1
    fingerprint = _fingerprint_with_firmware(
        log, args.firmware_log, strict=args.strict
    )
    out, should_close = _open_out(args.output)
    try:
        write_fingerprint(
            fingerprint, out, car_name=args.name, bus=args.bus,
            ecu_types=vehicle_info.ecu_types,
        )
    finally:
        if should_close:
            out.close()
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from .probe import probe_socketcan

    if not args.i_own_this_vehicle:
        print(
            "probe transmits diagnostic requests onto the vehicle bus.\n"
            "\n"
            "The requests are read-only (ReadDataByIdentifier), the same ones a\n"
            "garage scan tool and openpilot itself send. Even so, only do this on\n"
            "a vehicle you own or are authorised to work on, parked, in park,\n"
            "engine off or idling -- never while anyone is driving it.\n"
            "\n"
            "Re-run with --i-own-this-vehicle to confirm.",
            file=sys.stderr,
        )
        return 2

    probe_kwargs = {
        "timeout": args.timeout,
        "bus": args.bus,
    }
    if args.address:
        probe_kwargs["addresses"] = tuple(args.address)
    result = probe_socketcan(args.interface, **probe_kwargs)
    if args.capture:
        from .sources.candump import write_candump

        capture, capture_should_close = _open_out(args.capture)
        try:
            write_candump(result.frames, capture)
        finally:
            if capture_should_close:
                capture.close()
    out, should_close = _open_out(args.output)
    try:
        out.write(
            f"# queried {result.queried} (address, identifier) pairs; "
            f"{result.answered} ECU(s) answered\n"
        )
        for response in result.responses:
            out.write(f"{response}\n")
    finally:
        if should_close:
            out.close()
    if not result.responses:
        print(
            "no ECU responded. Check the interface is up and not listen-only, "
            "and that the ignition is on.",
            file=sys.stderr,
        )
    return 0


def cmd_synth(args: argparse.Namespace) -> int:
    """Write a synthetic capture, for trying the tool without a car."""
    import json

    from . import synth
    from .sources.candump import write_candump

    frames = synth.generate(args.duration, seed=args.seed)
    out, should_close = _open_out(args.output)
    try:
        write_candump(frames, out)
    finally:
        if should_close:
            out.close()

    if args.reference:
        import csv

        rows = synth.reference_series(args.duration, seed=args.seed)
        with open(args.reference, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["time", "speed_kph [km/h]", "steer_angle_deg [deg]"])
            for row in rows:
                writer.writerow(
                    [row["time"], row["speed_kph"], row["steer_angle_deg"]]
                )

    if args.truth:
        with open(args.truth, "w") as fh:
            json.dump(synth.ground_truth(), fh, indent=2)

    if args.vehicle_info_out:
        if not args.reference:
            print(
                "--vehicle-info-out needs --reference: without a reference "
                "log nothing can be named, so there is nothing to bind",
                file=sys.stderr,
            )
            return 2
        if not args.output or args.output == "-":
            print(
                "--vehicle-info-out needs --output to be a real file: the "
                "capture has to be re-read from disk to analyse it",
                file=sys.stderr,
            )
            return 2
        from .demo import analyse_for_demo_facts, build_demo_facts

        analyses, log = analyse_for_demo_facts(args.output, args.reference)
        facts = build_demo_facts(analyses, log)
        with open(args.vehicle_info_out, "w") as fh:
            json.dump(facts, fh, indent=2)
            fh.write("\n")

    if not args.quiet:
        print(
            f"wrote {len(frames)} synthetic frames "
            f"({args.duration:.0f}s of driving)",
            file=sys.stderr,
        )
        if args.vehicle_info_out:
            print(
                f"wrote {args.vehicle_info_out}: a complete demo-only "
                "vehicle-info.json (see its docstring in autodistill_can/demo.py "
                "-- never use this approach on a real car)",
                file=sys.stderr,
            )
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """Install a generated read-only package into an opendbc checkout."""
    from .install import fingerprint_collisions, install_port

    changed = install_port(
        args.port_dir, args.opendbc_dir, force=args.force
    )
    if not args.quiet:
        if changed:
            print(f"installed {len(changed)} file(s):", file=sys.stderr)
            for path in changed:
                print(f"  {path}", file=sys.stderr)
        else:
            print("port is already installed and up to date", file=sys.stderr)

    # Always reported, install or no-op: openpilot loading a different car's
    # port is not something anyone would diagnose from the symptoms.
    collisions = fingerprint_collisions(args.port_dir, args.opendbc_dir)
    if collisions and not args.quiet:
        print(
            f"\nWARNING: this fingerprint collides with {len(collisions)} "
            "platform(s) already in opendbc:",
            file=sys.stderr,
        )
        for line in collisions[:5]:
            print(f"  {line}", file=sys.stderr)
        print(
            "Collect ECU firmware versions (autodistill-can probe) so openpilot "
            "can tell them apart.",
            file=sys.stderr,
        )
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """Run the local guided web interface."""
    from .web import run_server

    run_server(
        port=args.port,
        workspace=args.workspace,
        open_browser=not args.no_browser,
    )
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Replay the capture through the bindings and check the values are sane."""
    import json
    import textwrap

    from .manual import apply_vehicle_info, load_vehicle_info
    from .validate import validate

    vehicle_info = load_vehicle_info(args.vehicle_info)
    log = _load_capture(args)
    if not log.streams:
        print(f"no CAN frames found in {args.source!r}", file=sys.stderr)
        return 1
    analyses = _analyse_all(log, min_frames=args.min_frames, quiet=args.quiet)
    if args.reference:
        reference = load_reference_csv(
            args.reference, time_column=args.reference_time
        )
        correlate_log(
            log.streams, analyses, reference, min_abs_r=args.min_correlation
        )
    apply_vehicle_info(analyses, vehicle_info)
    report = validate(analyses, log, vehicle_info)

    out, should_close = _open_out(args.output)
    try:
        if args.format == "json":
            json.dump(report.to_dict(), out, indent=2)
            out.write("\n")
        else:
            out.write("=" * 72 + "\n")
            out.write("CarState validation\n")
            out.write("=" * 72 + "\n\n")
            out.write(
                f"{'field':<28}{'from':<10}{'min':>12}{'max':>12}{'distinct':>10}\n"
            )
            for entry in report.fields:
                if entry.gears:
                    out.write(
                        f"  {entry.target:<26}{entry.origin:<10}"
                        f"{', '.join(entry.gears):>34}\n"
                    )
                    continue
                out.write(
                    f"  {entry.target:<26}{entry.origin:<10}"
                    f"{entry.minimum:>12.3f}{entry.maximum:>12.3f}"
                    f"{entry.distinct:>10}\n"
                )
            out.write("\n")
            for finding in report.findings:
                mark = {"error": "FAIL", "warning": "warn", "note": "note"}[
                    finding.level
                ]
                out.write(f"[{mark}] {finding.target or 'capture'}\n")
                for line in textwrap.wrap(finding.message, width=68):
                    out.write(f"       {line}\n")
            if report.ok:
                out.write(
                    "\nNo impossible values. That is not proof the bindings are "
                    "right,\nonly that none of them is provably wrong.\n"
                )
            else:
                out.write(
                    f"\n{len(report.errors)} binding(s) produce values a real "
                    "car cannot.\nFix those before generating a port.\n"
                )
    finally:
        if should_close:
            out.close()
    return 0 if report.ok else 1


_STATUS_MARK = {
    "auto": "auto ",
    "manual": "human",
    "derived": "calc ",
    "missing": "  -  ",
}


def cmd_requirements(args: argparse.Namespace) -> int:
    """Report what a finished port still needs, and how to supply it."""
    import json
    import textwrap

    from .coverage import evaluate
    from .manual import apply_vehicle_info, load_vehicle_info
    from .requirements import snippet_for

    vehicle_info = load_vehicle_info(args.vehicle_info)
    log = _load_capture(args)
    if not log.streams:
        print(f"no CAN frames found in {args.source!r}", file=sys.stderr)
        return 1
    analyses = _analyse_all(log, min_frames=args.min_frames, quiet=args.quiet)
    if args.reference:
        # Correlation is what names signals, and an unnamed signal cannot be
        # recognised automatically. Without this the checklist would report a
        # gap that generating the port would not have.
        reference = load_reference_csv(
            args.reference, time_column=args.reference_time
        )
        correlate_log(
            log.streams, analyses, reference, min_abs_r=args.min_correlation
        )
    apply_vehicle_info(analyses, vehicle_info)
    coverage = evaluate(analyses, vehicle_info)

    out, should_close = _open_out(args.output)
    try:
        if args.format == "json":
            json.dump(coverage.to_dict(), out, indent=2)
            out.write("\n")
            return 0
        out.write("=" * 72 + "\n")
        out.write("openpilot port requirements\n")
        out.write("=" * 72 + "\n\n")
        for level, label in (
            ("read", "read the car (dashcam / logging)"),
            ("lateral", "steer the car"),
            ("longitudinal", "drive the car (gas and brake)"),
        ):
            met, total = coverage.ratio(level)
            state = "COMPLETE" if coverage.complete_for(level) else "incomplete"
            out.write(f"{label:<38} {met:>3}/{total:<3} {state}\n")
        out.write("\n")

        last_kind = None
        for item in coverage:
            requirement = item.requirement
            if requirement.kind != last_kind:
                out.write(f"\n{requirement.kind}\n")
                last_kind = requirement.kind
            optional = "" if requirement.mandatory else "  (optional)"
            mark = _STATUS_MARK[item.status]
            out.write(
                f"  [{mark}] {requirement.key:<34} {requirement.title}{optional}\n"
            )
            if item.met:
                if item.source:
                    out.write(f"           from {item.source}\n")
                continue
            out.write(f"           {requirement.detail}\n")
            if requirement.how:
                # Indented under the item and wrapped, because a paragraph of
                # procedure is the point of the line, not a footnote to it.
                for line in textwrap.wrap(
                    requirement.how, width=68,
                    initial_indent="           how: ",
                    subsequent_indent="                ",
                ):
                    out.write(line + "\n")
            snippet = snippet_for(requirement)
            if snippet:
                out.write(f"           add {snippet}\n")

        missing = coverage.missing("longitudinal")
        out.write(
            "\n" + ("Nothing missing. Every requirement is supplied.\n"
                    if not missing else
                    f"{len(missing)} mandatory item(s) still missing. Add them to "
                    "a vehicle-info.json\nand pass it with --vehicle-info.\n")
        )
        out.write(
            "\nA complete checklist is not a validated port: AutoDistill cannot "
            "test a car.\nsafe_for_control stays false until a person reviews "
            "and drives it.\n"
        )
    finally:
        if should_close:
            out.close()
    return 0


def cmd_manual_template(args: argparse.Namespace) -> int:
    """Write an empty manual-knowledge file."""
    import json

    from .manual import vehicle_info_template

    out, should_close = _open_out(args.output)
    try:
        json.dump(vehicle_info_template(), out, indent=2)
        out.write("\n")
    finally:
        if should_close:
            out.close()
    return 0


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def _positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def _nonnegative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return value


def _positive_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return value


def _correlation(text: str) -> float:
    value = float(text)
    if not 0 <= value <= 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return value


def _can_address(text: str) -> int:
    try:
        value = int(text, 0)
    except ValueError:
        try:
            value = int(text, 16)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("must be a CAN address") from exc
    if not 0 <= value <= 0x1FFFFFFF:
        raise argparse.ArgumentTypeError("must be in [0, 0x1FFFFFFF]")
    return value


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="autodistill-can",
        description=(
            "Automatic CAN bus reverse engineering: find signals, counters and "
            "checksums in a capture, and bootstrap an openpilot car port."
        ),
        epilog=(
            "Sources may be a capture file (candump or CSV) or a live interface "
            "(socketcan:can0, panda:). Try `autodistill-can synth -o drive.log` "
            "for a "
            "synthetic capture to experiment with."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"AutoDistill CAN {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-o", "--output", help="output file (default: stdout)")
        p.add_argument(
            "-q", "--quiet", action="store_true", help="suppress progress output"
        )

    def add_capture_inputs(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--strict",
            action="store_true",
            help="fail on the first malformed capture row instead of skipping it",
        )
        p.add_argument(
            "--add-log",
            action="append",
            metavar="CAPTURE",
            help=(
                "merge another capture of the same car into this analysis, "
                "laid end to end after the first; may be given more than once"
            ),
        )
        p.add_argument(
            "--firmware-log",
            action="append",
            metavar="CAPTURE",
            help=(
                "take UDS firmware responses from this probe capture; may be "
                "given more than once"
            ),
        )
        p.add_argument(
            "--vehicle-info",
            metavar="JSON",
            help=(
                "validated human-supplied vehicle facts, ECU types, signal "
                "overrides, and engineering notes"
            ),
        )

    # analyse
    p = sub.add_parser(
        "analyse", aliases=["analyze"], help="analyse a capture and report findings"
    )
    p.add_argument("source", help="capture file or live source URI")
    add_common(p)
    add_capture_inputs(p)
    p.add_argument(
        "-f", "--format", choices=("text", "json"), default="text",
        help="report format (default: text)",
    )
    p.add_argument("--dbc", help="also write a DBC file here")
    p.add_argument(
        "--port",
        metavar="DIR",
        help="also write a read-only openpilot bootstrap into this directory",
    )
    p.add_argument(
        "--brand", default="mystery",
        help="package name for --port, used as opendbc/car/<brand>/",
    )
    p.add_argument(
        "--name", default="MYSTERY_CAR", help="car name used in generated code"
    )
    p.add_argument(
        "--bus", type=_nonnegative_int, default=0,
        help="physical bus used for --dbc and --port (default: 0)",
    )
    p.add_argument(
        "--camera-bus", type=_nonnegative_int,
        help="physical stock-camera bus for --port (auto-detected by default)",
    )
    p.add_argument(
        "--reference",
        help=(
            "CSV of independently measured channels (GPS speed, OBD-II PIDs) "
            "used to name signals and recover their scale factors"
        ),
    )
    p.add_argument(
        "--reference-time",
        help="name of the time column in the reference CSV, if not auto-detected",
    )
    p.add_argument(
        "--min-correlation", type=_correlation, default=0.9,
        help="minimum |r| to report a correlation match (default: 0.9)",
    )
    p.add_argument(
        "--min-frames", type=_positive_int, default=40,
        help="skip messages with fewer frames than this (default: 40)",
    )
    p.add_argument("--limit", type=_positive_int, help="stop after this many frames")
    p.add_argument(
        "--force", action="store_true",
        help="replace generated files that already exist in --port",
    )
    p.set_defaults(func=cmd_analyse)

    # capture
    p = sub.add_parser("capture", help="record traffic to a candump log")
    p.add_argument(
        "source", help="live source URI, e.g. socketcan:can0 or panda:"
    )
    add_common(p)
    p.add_argument("-d", "--duration", type=_positive_float, help="seconds to record")
    p.add_argument("--limit", type=_positive_int, help="stop after this many frames")
    p.set_defaults(func=cmd_capture)

    # port
    p = sub.add_parser(
        "port",
        help="generate a read-only openpilot port bootstrap from a capture",
        description=(
            "Write an openpilot port directory: fingerprint, firmware versions, "
            "DBC, verified checksum and counter code, and a CarState mapped from "
            "whatever the reference log identified. The generated controller "
            "sends nothing -- how to actuate a car cannot be read off its bus."
        ),
    )
    p.add_argument("source", help="capture file or live source URI")
    add_common(p)
    add_capture_inputs(p)
    p.add_argument(
        "--brand", default="mystery",
        help="package name, used as opendbc/car/<brand>/ (default: mystery)",
    )
    p.add_argument(
        "--name", default="MYSTERY CAR", help="human-readable car name"
    )
    p.add_argument(
        "--bus", type=_nonnegative_int, default=0,
        help="bus carrying the car's own traffic",
    )
    p.add_argument(
        "--camera-bus", type=_nonnegative_int,
        help="physical stock-camera bus (auto-detected when omitted)",
    )
    p.add_argument(
        "--reference",
        help="CSV of measured channels; without it no signal can be named",
    )
    p.add_argument("--reference-time", help="name of the reference time column")
    p.add_argument(
        "--min-frames", type=_positive_int, default=40,
        help="skip messages with fewer frames than this (default: 40)",
    )
    p.add_argument("--limit", type=_positive_int, help="stop after this many frames")
    p.add_argument(
        "--force", action="store_true",
        help="replace generated files that already exist in the output directory",
    )
    p.set_defaults(func=cmd_port)

    # fingerprint
    p = sub.add_parser(
        "fingerprint", help="print just the openpilot fingerprint for a capture"
    )
    p.add_argument("source", help="capture file or live source URI")
    add_common(p)
    add_capture_inputs(p)
    p.add_argument("--name", default="MYSTERY_CAR", help="car name in the output")
    p.add_argument(
        "--bus", type=_nonnegative_int, default=0,
        help="physical bus to emit in the legacy fingerprint",
    )
    p.add_argument("--limit", type=_positive_int, help="stop after this many frames")
    p.set_defaults(func=cmd_fingerprint)

    # probe
    p = sub.add_parser(
        "probe",
        help="query ECUs for firmware versions (TRANSMITS on the bus)",
        description=(
            "Sends read-only UDS ReadDataByIdentifier requests to each ECU and "
            "reports what they answer. This transmits on the vehicle bus."
        ),
    )
    p.add_argument("-i", "--interface", default="can0", help="SocketCAN interface")
    add_common(p)
    p.add_argument(
        "--bus", type=_nonnegative_int, default=0,
        help="bus number to record against",
    )
    p.add_argument(
        "--timeout", type=_positive_float, default=0.15,
        help="seconds to wait for each reply (default: 0.15)",
    )
    p.add_argument(
        "--i-own-this-vehicle", action="store_true",
        help="confirm you are authorised to transmit on this vehicle's bus",
    )
    p.add_argument(
        "--capture",
        metavar="PATH",
        help=(
            "also save raw ECU response frames as candump data for "
            "`port --firmware-log`"
        ),
    )
    p.add_argument(
        "--address",
        action="append",
        type=_can_address,
        help=(
            "query this tester-to-ECU address instead of the default blocks; "
            "may be repeated (for example 0x730 or 0x18DA28F1)"
        ),
    )
    p.set_defaults(func=cmd_probe)

    # synth
    p = sub.add_parser(
        "synth", help="generate a synthetic capture with known ground truth"
    )
    add_common(p)
    p.add_argument(
        "-d", "--duration", type=_positive_float, default=60.0,
        help="seconds of driving",
    )
    p.add_argument("--seed", type=int, default=20250730, help="random seed")
    p.add_argument("--reference", help="also write the reference CSV here")
    p.add_argument("--truth", help="also write the ground-truth layout JSON here")
    p.add_argument(
        "--vehicle-info-out",
        help=(
            "also analyse the capture just written and save a vehicle-info.json "
            "complete enough for `port` to produce a full read+steer+drive port "
            "from it (requires --reference). For trying the whole pipeline "
            "without a car; never point this at a real capture -- see "
            "autodistill_can.demo"
        ),
    )
    p.set_defaults(func=cmd_synth)

    # install
    p = sub.add_parser(
        "install",
        help="install a generated port into an opendbc checkout",
    )
    p.add_argument(
        "port_dir", help="directory produced by `autodistill-can port`"
    )
    p.add_argument(
        "opendbc_dir",
        help="opendbc repository root (the directory containing opendbc/)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="replace differing generated brand/DBC files",
    )
    p.add_argument(
        "-q", "--quiet", action="store_true", help="suppress installed-file output"
    )
    p.set_defaults(func=cmd_install)

    # ui
    p = sub.add_parser(
        "ui",
        help="open the guided local web interface",
        description=(
            "Run AutoDistill's privacy-first browser interface on this computer. "
            "The server binds only to 127.0.0.1 and does not upload CAN data."
        ),
    )
    p.add_argument(
        "--port",
        type=_nonnegative_int,
        default=8765,
        help="local TCP port (default: 8765; use 0 to choose a free port)",
    )
    p.add_argument(
        "--workspace",
        help="project storage directory (default: ~/.autodistill/projects)",
    )
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="print the local URL without opening a browser",
    )
    p.set_defaults(func=cmd_ui)

    # validate
    p = sub.add_parser(
        "validate",
        help="check the bound CarState fields against the capture",
        description=(
            "Decode the capture the way the generated CarState would and check "
            "the values could come from a real car. Catches a binding on the "
            "wrong bit, a speed in the wrong unit, or a gear map that never "
            "reads drive -- none of which a compile or an import would notice. "
            "Exits non-zero if any field produces impossible values."
        ),
    )
    p.add_argument("source", help="capture file or live source URI")
    add_common(p)
    add_capture_inputs(p)
    p.add_argument(
        "-f", "--format", choices=("text", "json"), default="text",
        help="report format (default: text)",
    )
    p.add_argument(
        "--reference",
        help="CSV of measured channels, as used by analyse; it names the "
             "signals AutoDistill can bind on its own",
    )
    p.add_argument(
        "--reference-time",
        help="name of the time column in the reference CSV, if not auto-detected",
    )
    p.add_argument(
        "--min-correlation", type=_correlation, default=0.9,
        help="minimum |r| to accept a correlation match (default: 0.9)",
    )
    p.add_argument(
        "--min-frames", type=_positive_int, default=40,
        help="skip messages with fewer frames than this (default: 40)",
    )
    p.add_argument("--limit", type=_positive_int, help="stop after this many frames")
    p.set_defaults(func=cmd_validate)

    # requirements
    p = sub.add_parser(
        "requirements",
        help="list what a finished port still needs from a person",
        description=(
            "Score a capture, plus any facts already supplied, against "
            "everything openpilot needs. Prints each requirement as automatic, "
            "human-supplied, or missing, with the vehicle-info fragment that "
            "would satisfy it."
        ),
    )
    p.add_argument("source", help="capture file or live source URI")
    add_common(p)
    add_capture_inputs(p)
    p.add_argument(
        "-f", "--format", choices=("text", "json"), default="text",
        help="report format (default: text)",
    )
    p.add_argument(
        "--reference",
        help="CSV of measured channels, as used by analyse; it is what names "
             "signals, so the checklist matches what a port would recover",
    )
    p.add_argument(
        "--reference-time",
        help="name of the time column in the reference CSV, if not auto-detected",
    )
    p.add_argument(
        "--min-correlation", type=_correlation, default=0.9,
        help="minimum |r| to accept a correlation match (default: 0.9)",
    )
    p.add_argument(
        "--min-frames", type=_positive_int, default=40,
        help="skip messages with fewer frames than this (default: 40)",
    )
    p.add_argument("--limit", type=_positive_int, help="stop after this many frames")
    p.set_defaults(func=cmd_requirements)

    # manual-template
    p = sub.add_parser(
        "manual-template",
        help="write a vehicle-info.json template for facts CAN cannot reveal",
        description=(
            "Create the JSON format accepted by --vehicle-info. Manual facts are "
            "marked as human-supplied and never enable vehicle control."
        ),
    )
    add_common(p)
    p.set_defaults(func=cmd_manual_template)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return args.func(args)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        print(f"autodistill-can: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
