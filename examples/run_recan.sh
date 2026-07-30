#!/usr/bin/env bash
# Run the tool against real driving data from the ReCAN dataset.
#
# ReCAN is a public dataset built specifically for CAN reverse engineering:
# full-vehicle captures from real drives, several manufacturers.
#   https://github.com/Cyberdefence-Lab-Murcia/ReCAN
#   Zago et al., "ReCAN - Dataset for reverse engineering of Controller Area
#   Networks", Data in Brief (2020). doi:10.1016/j.dib.2020.105149
#
# Unlike the Kia example there is no DBC here, so nothing can be scored against
# a human's answer. What these captures *do* test is scale and whether the
# self-verifying findings -- counters and checksums, which either reproduce
# every frame or do not -- hold up on real traffic from a moving car.
#
# Usage:  ./run_recan.sh [giulia|corsa]     (default: giulia)
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p recan && cd recan

CAR="${1:-giulia}"
case "$CAR" in
  giulia) DIR=C-1-AlfaRomeo-Giulia/Exp-2; NAME=ALFA_GIULIA ;;
  corsa)  DIR=C-2-Opel-Corsa/Exp-1;       NAME=OPEL_CORSA  ;;
  *) echo "usage: $0 [giulia|corsa]" >&2; exit 2 ;;
esac
BASE="https://raw.githubusercontent.com/Cyberdefence-Lab-Murcia/ReCAN/master/Data/$DIR"

echo "==> fetching $DIR (this is tens of MB)"
[ -f "$CAR.tar.gz" ] || curl -sSL -o "$CAR.tar.gz" "$BASE/raw.tar.gz"
[ -f "$CAR-raw.csv" ] || { tar xzOf "$CAR.tar.gz" > "$CAR-raw.csv"; }

echo "==> converting to candump format"
# ReCAN stores each payload as a string of 64 '0'/'1' characters rather than
# hex, so it needs converting before anything else can read it.
python3 - "$CAR-raw.csv" "$CAR.log" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
n = 0
with open(src) as fh, open(dst, "w") as out:
    for line in fh:
        p = line.rstrip("\n").split(",")
        if len(p) < 5:
            continue
        t, iface, addr, dlc, bits = p[:5]
        if not bits or bits.strip("01"):
            continue
        nbytes = min(int(dlc), len(bits) // 8)
        data = bytes(int(bits[i * 8:(i + 1) * 8], 2) for i in range(nbytes))
        out.write(f"({float(t):.6f}) {iface} {addr}#{data.hex().upper()}\n")
        n += 1
print(f"    {n} frames -> {dst}")
PY

echo "==> analysing"
time autodistill-can analyse "$CAR.log" \
  --dbc "$CAR.dbc" --port "${CAR}_port" --brand "$CAR" --name "$NAME" -o "$CAR-report.txt"

echo
echo "==> what was found"
printf '    messages:  %s\n' "$(grep -c '^bus 0 ' "$CAR-report.txt")"
printf '    counters:  %s\n' "$(grep -c 'b counter' "$CAR-report.txt")"
printf '    checksums: %s\n' "$(grep -c 'checksum:' "$CAR-report.txt")"
printf '    multiplexed: %s\n' "$(grep -c 'MULTIPLEXED' "$CAR-report.txt" || true)"
echo
echo "==> checksum families (one consistent family = one OEM architecture)"
grep 'checksum:' "$CAR-report.txt" | sed 's/.*checksum: //' | cut -c1-56 | sort | uniq -c | sort -rn

echo
echo "==> verifying every generated checksum against every captured frame"
python3 - "$CAR.log" "${CAR}_port/${CAR}can.py" <<'PY'
import re, sys
from autodistill_can.frame import CanLog, extract_be
from autodistill_can.sources import open_source
from autodistill_can.analysis import analyse_message

log_path, port_path = sys.argv[1], sys.argv[2]
code = "".join(re.findall(r"^def checksum_\w+\(.*?(?=^def |^# ---|^# Rolling|\Z)",
                          open(port_path).read(), re.S | re.M))
ns: dict = {}
exec(code, ns)

log = CanLog.from_frames(open_source(log_path))
total = bad = msgs = 0
for stream in log.sorted_streams():
    if len(stream) < 40:
        continue
    a = analyse_message(stream)
    if a.checksum is None:
        continue
    fn = ns.get(f"checksum_{a.addr:03x}")
    if fn is None:
        continue
    msgs += 1
    for p in stream.payloads:
        total += 1
        if fn(a.addr, p) != extract_be(p, a.checksum.start, a.checksum.length):
            bad += 1
print(f"    {msgs} checksums, {total - bad}/{total} frames reproduced"
      f"{'  ALL OK' if bad == 0 else f'  {bad} mismatch'}")
PY
