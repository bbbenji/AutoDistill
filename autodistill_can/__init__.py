"""Automatic CAN bus reverse engineering.

Point it at a capture and it works out what the messages mean: where the fields
are, how they are encoded, which one is the rolling counter, and how the
checksum is computed. Give it a reference log too and it names the signals and
recovers their scale factors.

    from autodistill_can import CanLog, analyse_message
    from autodistill_can.sources import open_source

    log = CanLog.from_frames(open_source("drive.log"))
    for stream in log.sorted_streams():
        print(analyse_message(stream))

The analysis core is pure standard library, so it runs on a comma device as
happily as on a laptop.
"""

from __future__ import annotations

__version__ = "1.4.4"

from .analysis.message import MessageAnalysis, analyse_message
from .frame import CanFrame, CanLog, MessageStream
from .manual import VehicleInfo, apply_vehicle_info, load_vehicle_info

__all__ = [
    "CanFrame",
    "CanLog",
    "MessageAnalysis",
    "MessageStream",
    "VehicleInfo",
    "__version__",
    "analyse_message",
    "apply_vehicle_info",
    "load_vehicle_info",
]
