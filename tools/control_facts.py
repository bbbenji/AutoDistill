#!/usr/bin/env python3
"""Write a valid control-facts file for a capture, for CI to build a port from.

The point of the port this produces is not that it steers or drives anything
sensibly -- the capture is synthetic and the command messages are whichever
ones happen to carry both a counter and a checksum. The point is that every
part of the generated control path (the CANPacker call, the checksum wiring,
the torque and longitudinal tuning, the gear map, the safety model) gets
constructed against the real opendbc, which is the only way to find out that
an attribute was renamed.

The actual fact-building lives in :mod:`autodistill_can.demo`, shared with the
web UI's demo project so both stay in step with the synthetic car.

    usage: control_facts.py CAPTURE REFERENCE OUTPUT.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from autodistill_can.demo import analyse_for_demo_facts, build_demo_facts
from autodistill_can.manual import load_vehicle_info


def build(capture: str, reference: str) -> dict:
    analyses, log = analyse_for_demo_facts(capture, reference)
    return build_demo_facts(analyses, log)


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    capture, reference, output = sys.argv[1:]
    facts = build(capture, reference)
    # Fail here rather than three steps later if the schema rejects it.
    load_vehicle_info(facts)
    Path(output).write_text(json.dumps(facts, indent=2) + "\n")
    print(
        f"wrote {output}: commanding {facts['actuation']['lateral']['message']} "
        f"(lateral) and {facts['actuation']['longitudinal']['message']} "
        "(longitudinal)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
