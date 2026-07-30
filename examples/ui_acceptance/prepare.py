#!/usr/bin/env python3
"""Build the real-data browser acceptance fixture.

The source is comma.ai's public comma2k19 example segment.  Its raw rlog is
decoded with the exact cereal schema commit referenced by that dataset, then
split into received vehicle traffic and the two control messages openpilot
actually transmitted.  No CAN payload is synthesized or decoded through this
project while preparing the capture.

Run from anywhere:

    python examples/ui_acceptance/prepare.py
"""

from __future__ import annotations

import ast
import csv
import math
import os
import shutil
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable

COMMA2K19_COMMIT = "4c7f1a6e1957745beadc1def0e7225f559b09a2a"
CEREAL_COMMIT = "ab79999e5dc2de25c3eb0c9acbed47c9e81f5d79"
OPENDBC_COMMIT = "aedd88ef0b75e5032a277fcf7b0c928ad4e54ddf"
SEGMENT = (
    "Example_1/b0c9d2329ad1606b|2018-08-02--08-34-47/40"
)

HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "prepared"


def _raw(repo: str, commit: str, path: str) -> str:
    quoted = urllib.parse.quote(path, safe="/")
    return f"https://raw.githubusercontent.com/{repo}/{commit}/{quoted}"


def download(url: str, target: Path) -> Path:
    """Download one immutable source file, retaining it for provenance."""
    if target.is_file() and target.stat().st_size:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
    except (OSError, urllib.error.URLError) as exc:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"could not download {url}: {exc}") from exc
    os.replace(partial, target)
    return target


def npy(path: Path) -> tuple[tuple[int, ...], str, list[object]]:
    """Read the small set of NumPy v1 arrays used by comma2k19.

    The fixture builder deliberately has no NumPy dependency.  These files are
    plain, little-endian 64-bit numbers (or fixed eight-byte strings), so a
    narrow reader is both easier to install and stricter about unexpected data.
    """
    with path.open("rb") as stream:
        if stream.read(6) != b"\x93NUMPY":
            raise ValueError(f"{path}: not a NumPy array")
        major, minor = stream.read(2)
        if (major, minor) != (1, 0):
            raise ValueError(f"{path}: unsupported NumPy format {major}.{minor}")
        header_size = struct.unpack("<H", stream.read(2))[0]
        header = ast.literal_eval(stream.read(header_size).decode("ascii").strip())
        shape = tuple(int(value) for value in header["shape"])
        descriptor = str(header["descr"])
        if header.get("fortran_order"):
            raise ValueError(f"{path}: Fortran-ordered arrays are unsupported")
        raw = stream.read()

    formats = {"<f8": "<d", "<i8": "<q", "|S8": "8s"}
    try:
        fmt = formats[descriptor]
    except KeyError as exc:
        raise ValueError(f"{path}: unsupported dtype {descriptor}") from exc
    width = struct.calcsize(fmt)
    count = math.prod(shape)
    if len(raw) != count * width:
        raise ValueError(
            f"{path}: expected {count * width} data bytes, found {len(raw)}"
        )
    values = [row[0] for row in struct.iter_unpack(fmt, raw)]
    return shape, descriptor, values


def _write_candump(path: Path, frames: Iterable[object], *, t0: float) -> int:
    count = 0
    with path.open("w", encoding="ascii") as out:
        for frame in frames:
            out.write(
                f"({frame.t - t0:.6f}) can{frame.bus} "
                f"{frame.addr:X}#{frame.data.hex().upper()}\n"
            )
            count += 1
    return count


def _interpolate(times: list[float], values: list[float], t: float) -> float:
    """Linear interpolation for the independent pose-derived speed channel."""
    import bisect

    index = bisect.bisect_left(times, t)
    if index <= 0:
        return values[0]
    if index >= len(times):
        return values[-1]
    left, right = index - 1, index
    span = times[right] - times[left]
    if span <= 0:
        return values[left]
    fraction = (t - times[left]) / span
    return values[left] + fraction * (values[right] - values[left])


def _reference(source: Path, output: Path, *, t0: float) -> int:
    def load(relative: str) -> tuple[tuple[int, ...], list[object]]:
        shape, _, values = npy(source / relative)
        return shape, values

    _, times_raw = load("processed_log/CAN/speed/t")
    _, speed_raw = load("processed_log/CAN/speed/value")
    _, angle_raw = load("processed_log/CAN/steering_angle/value")
    wheel_shape, wheels_raw = load("processed_log/CAN/wheel_speed/value")
    _, pose_times_raw = load("global_pose/frame_times")
    pose_shape, velocities_raw = load("global_pose/frame_velocities")

    times = [float(value) for value in times_raw]
    speed = [float(value) for value in speed_raw]
    angle = [float(value) for value in angle_raw]
    if wheel_shape != (len(times), 4):
        raise ValueError(f"unexpected wheel-speed shape {wheel_shape}")
    wheels = [
        tuple(float(value) for value in wheels_raw[index:index + 4])
        for index in range(0, len(wheels_raw), 4)
    ]
    if pose_shape != (len(pose_times_raw), 3):
        raise ValueError(f"unexpected pose-velocity shape {pose_shape}")
    pose_times = [float(value) for value in pose_times_raw]
    pose_speed = [
        math.sqrt(sum(float(component) ** 2 for component in velocities_raw[index:index + 3]))
        for index in range(0, len(velocities_raw), 3)
    ]
    if not (len(times) == len(speed) == len(angle) == len(wheels)):
        raise ValueError("comma2k19 CAN reference channels have different lengths")

    with output.open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow([
            "time", "VEHICLE_SPEED [m/s]", "STEERING_ANGLE [deg]",
            "WHEEL_SPEED_FL [m/s]", "WHEEL_SPEED_FR [m/s]",
            "WHEEL_SPEED_RL [m/s]", "WHEEL_SPEED_RR [m/s]",
            "POSE_SPEED [m/s]",
        ])
        for timestamp, car_speed, steer, wheel in zip(
            times, speed, angle, wheels, strict=True
        ):
            writer.writerow([
                f"{timestamp - t0:.6f}", f"{car_speed:.9g}", f"{steer:.9g}",
                *(f"{value:.9g}" for value in wheel),
                f"{_interpolate(pose_times, pose_speed, timestamp):.9g}",
            ])
    return len(times)


def _official_snapshot(output: Path) -> None:
    files = (
        "opendbc/car/toyota/carcontroller.py",
        "opendbc/car/toyota/carstate.py",
        "opendbc/car/toyota/fingerprints.py",
        "opendbc/car/toyota/interface.py",
        "opendbc/car/toyota/toyotacan.py",
        "opendbc/car/toyota/values.py",
    )
    for path in files:
        download(
            _raw("commaai/opendbc", OPENDBC_COMMIT, path),
            output / Path(path).name,
        )

    generator = "opendbc/dbc/generator/toyota"
    pieces = (
        f"{generator}/_toyota_2017.dbc",
        f"{generator}/_toyota_adas_standard.dbc",
        f"{generator}/toyota_new_mc_pt.dbc",
    )
    texts = []
    for path in pieces:
        local = download(
            _raw("commaai/opendbc", OPENDBC_COMMIT, path),
            output / "dbc-source" / Path(path).name,
        )
        texts.append(local.read_text(encoding="utf-8"))
    # This is exactly opendbc's simple IMPORT preprocessor for this DBC.
    generated = output / "toyota_new_mc_pt_generated.dbc"
    generated.write_text(
        'CM_ "AUTOGENERATED FILE, DO NOT EDIT";\n\n'
        + texts[0] + "\n\n" + texts[1] + "\n\n"
        + "\n".join(
            line for line in texts[2].splitlines()
            if not line.startswith('CM_ "IMPORT ')
        ) + "\n",
        encoding="utf-8",
    )
    (output / "COMMIT.txt").write_text(OPENDBC_COMMIT + "\n", encoding="ascii")


def build(output: Path) -> None:
    try:
        from autodistill_can.sources.rlog import read_rlog
    except ImportError as exc:
        raise RuntimeError(
            "run this from the AutoDistill checkout after `pip install -e .`"
        ) from exc

    source = output / "source"
    raw_log = download(
        _raw("commaai/comma2k19", COMMA2K19_COMMIT, f"{SEGMENT}/raw_log.bz2"),
        source / "raw_log.bz2",
    )
    cereal_files = (
        "log.capnp", "car.capnp", "include/c++.capnp", "include/java.capnp",
    )
    for path in cereal_files:
        download(
            _raw("commaai/cereal", CEREAL_COMMIT, path),
            source / "cereal" / path,
        )
    schema = source / "cereal" / "log.capnp"

    reference_files = (
        "processed_log/CAN/speed/t",
        "processed_log/CAN/speed/value",
        "processed_log/CAN/steering_angle/value",
        "processed_log/CAN/wheel_speed/value",
        "global_pose/frame_times",
        "global_pose/frame_velocities",
    )
    for path in reference_files:
        download(
            _raw("commaai/comma2k19", COMMA2K19_COMMIT, f"{SEGMENT}/{path}"),
            source / path,
        )

    try:
        all_frames = list(read_rlog(raw_log, schema=schema, include_sent=True))
        received = list(read_rlog(raw_log, schema=schema))
    except ImportError as exc:
        raise RuntimeError(
            "reading the source rlog needs pycapnp; install it with "
            "`pip install -e '.[rlog]'`, then run this command again"
        ) from exc
    if not all_frames or not received:
        raise RuntimeError("the comma2k19 rlog contained no CAN frames")
    t0 = min(frame.t for frame in all_frames)
    received_keys = {(frame.t, frame.bus, frame.addr, frame.data) for frame in received}
    sent_commands = [
        frame for frame in all_frames
        if (frame.t, frame.bus, frame.addr, frame.data) not in received_keys
        and frame.bus == 0 and frame.addr in (0x2E4, 0x343)
    ]
    output.mkdir(parents=True, exist_ok=True)
    received_count = _write_candump(output / "rav4-main.log", received, t0=t0)
    command_count = _write_candump(
        output / "rav4-control.log", sent_commands, t0=t0
    )
    complete_count = _write_candump(
        output / "rav4-complete.log",
        sorted([*received, *sent_commands], key=lambda frame: frame.t),
        t0=t0,
    )
    reference_count = _reference(source, output / "rav4-reference.csv", t0=t0)
    shutil.copyfile(HERE / "vehicle-info.json", output / "vehicle-info.json")
    _official_snapshot(output / "official-opendbc")

    (output / "SOURCE.txt").write_text(
        "comma2k19 commit: " + COMMA2K19_COMMIT + "\n"
        "segment: " + SEGMENT + "\n"
        "cereal schema commit: " + CEREAL_COMMIT + "\n"
        "opendbc comparison commit: " + OPENDBC_COMMIT + "\n",
        encoding="utf-8",
    )
    print(f"ready: {output}")
    print(f"  rav4-main.log       {received_count} received CAN frames")
    print(f"  rav4-control.log    {command_count} logged openpilot command frames")
    print(f"  rav4-complete.log   {complete_count} combined UI-ready frames")
    print(f"  rav4-reference.csv  {reference_count} synchronized rows")
    print("  vehicle-info.json   complete manual facts")
    print("  official-opendbc/   pinned hand-comparison snapshot")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1 or (args and args[0] in {"-h", "--help"}):
        print("usage: prepare.py [OUTPUT_DIRECTORY]")
        return 0 if args and args[0] in {"-h", "--help"} else 2
    output = Path(args[0]).expanduser().resolve() if args else DEFAULT_OUTPUT
    try:
        build(output)
    except (RuntimeError, ValueError) as exc:
        print(f"prepare: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
