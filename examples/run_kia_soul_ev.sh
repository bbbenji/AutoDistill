#!/usr/bin/env bash
# Reproduce the real-vehicle walkthrough end to end.
#
# Downloads a public Kia Soul EV capture, converts it, runs the analysis, and
# scores the result against the human-written DBC that ships alongside it.
#
# Source: https://github.com/aroongta/CANBUS_Decoder (MIT). The capture is real
# traffic from a Kia Soul EV fitted with a PolySync OSCC / DriveKit drive-by-wire
# kit, logged with candump and annotated by cantools.
set -euo pipefail

cd "$(dirname "$0")"
BASE=https://raw.githubusercontent.com/aroongta/CANBUS_Decoder/master

echo "==> fetching the capture and its DBCs"
[ -f kia_annotated.txt ] || curl -sSL -o kia_annotated.txt "$BASE/can0_rec1_cantools.txt"
[ -f kia_chassis.dbc ]   || curl -sSL -o kia_chassis.dbc   "$BASE/DBC/chassis_kia_soul_ev.dbc"

echo "==> converting to a candump log plus a reference series"
# The log's ':: NAME(SIG: value unit)' suffix is cantools' decode of the very
# same bytes. It stands in for a reference measurement here, but note it is not
# an independent one -- on a real job this would be a GPS track or OBD-II poll.
python3 - <<'PY'
import csv, re
from pathlib import Path

frame_re = re.compile(
    r"^\s*\((\d+\.\d+)\)\s+\S+\s+([0-9A-Fa-f]+)\s+\[(\d+)\]\s+((?:[0-9A-Fa-f]{2}\s+)+)"
)
sig_re = re.compile(r"(\w+):\s*(-?[\d.]+)")

frames, rows, t0 = [], [], None
for line in Path("kia_annotated.txt").open():
    m = frame_re.match(line)
    if not m:
        continue
    t = float(m.group(1))
    t0 = t if t0 is None else t0
    frames.append((t - t0, int(m.group(2), 16),
                   bytes.fromhex(m.group(4).replace(" ", ""))[: int(m.group(3))]))
    if "::" in line and "Unknown" not in line:
        row = {"time": round(t - t0, 6)}
        for name, value in sig_re.findall(line.split("::", 1)[1]):
            row[name] = float(value)
        if len(row) > 1:
            rows.append(row)

with open("kia_real.log", "w") as fh:
    for t, addr, data in frames:
        fh.write(f"({t:.6f}) can0 {addr:X}#{data.hex().upper()}\n")

channels = sorted({k for r in rows for k in r if k != "time"})
merged, last = [], {}
for r in rows:
    last.update({k: v for k, v in r.items() if k != "time"})
    if len(last) == len(channels):
        merged.append({"time": r["time"], **last})

with open("kia_reference.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["time"] + channels)
    w.writeheader()
    w.writerows(merged)

print(f"    {len(frames)} frames, {len(merged)} reference rows")
PY

echo "==> analysing"
autodistill-can analyse kia_real.log \
  --reference kia_reference.csv \
  --dbc kia_recovered.dbc \
  --port kia_port --brand kiasoul \
  --name KIA_SOUL_EV \
  -o kia_report.txt

echo
echo "==> recovered layout for the steering-angle message"
sed -n '/0x2B0/,/^$/p' kia_report.txt

echo "==> the same signal, ours vs the human-written DBC"
grep 'SG_ STEERING_WHEEL_ANGLE' kia_recovered.dbc kia_chassis.dbc

echo
echo "==> the checksum openpilot would have to reproduce to send this message"
sed -n "/^def checksum_2b0/,/^$/p" kia_port/kiasoulcan.py

echo "wrote: kia_report.txt  kia_recovered.dbc  kia_port/"
