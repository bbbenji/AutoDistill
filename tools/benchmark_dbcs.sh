#!/usr/bin/env bash
# Score signal recovery against every real opendbc database we can load.
#
# Downloads the databases openpilot actually ships, drives a simulated vehicle
# through each one (encoding with cantools, which is independent of this
# package), and reports how much of each manufacturer's layout comes back.
#
# This is the broadest check available without a fleet of cars: thousands of
# signals laid out the way real manufacturers laid them out, with the answer
# supplied.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p examples/opendbc

BASE=https://raw.githubusercontent.com/commaai/opendbc/master/opendbc/dbc
# Chosen for coverage rather than size: Hyundai and VW are wholly Intel, Mazda
# is almost wholly Motorola, and Tesla mixes the two.
DBCS="hyundai_2015_ccan vw_mqb mazda_3_2019 tesla_model3_party tesla_powertrain nissan_xterra_2011"

for d in $DBCS; do
  [ -s "examples/opendbc/$d.dbc" ] || curl -sSL -o "examples/opendbc/$d.dbc" "$BASE/$d.dbc"
done

DURATION="${1:-90}"
tmp=$(mktemp)
for d in $DBCS; do
  echo "==> $d"
  python tools/replay_dbc.py "examples/opendbc/$d.dbc" -d "$DURATION" \
    | tee /dev/stderr | grep '^SUMMARY' >> "$tmp" || true
done

python - "$tmp" <<'PY'
import sys
rows = [l.split("\t") for l in open(sys.argv[1]) if l.startswith("SUMMARY")]
print()
print(f"{'database':<24} {'signals':>8} {'exact':>13} {'position':>13} {'byte order':>12}")
print("-" * 74)
ta = te = tp = tor = tot = 0
for _, name, active, exact, pos, order_ok, order_n in rows:
    active, exact, pos = int(active), int(exact), int(pos)
    order_ok, order_n = int(order_ok), int(order_n)
    ta += active; te += exact; tp += pos; tor += order_ok; tot += order_n
    print(f"{name:<24} {active:>8} {exact:>7} ({100*exact/max(1,active):>3.0f}%) "
          f"{pos:>7} ({100*pos/max(1,active):>3.0f}%) {order_ok:>6}/{order_n:<5}")
print("-" * 74)
print(f"{'TOTAL':<24} {ta:>8} {te:>7} ({100*te/max(1,ta):>3.0f}%) "
      f"{tp:>7} ({100*tp/max(1,ta):>3.0f}%) {tor:>6}/{tot:<5}")
PY
rm -f "$tmp"
