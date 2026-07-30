"""Analysis passes, from raw bits up to named signals."""

from __future__ import annotations

from .bitstats import BitStats, bit_stats
from .checksum import ChecksumSolution, crack_checksum
from .counter import CounterField, find_counter
from .message import (
    MessageAnalysis,
    analyse_log,
    analyse_message,
    detect_byte_order,
)
from .multiplex import MultiplexInfo, find_multiplexer
from .signals import Signal, SignalConfig, extract_signals

__all__ = [
    "BitStats",
    "ChecksumSolution",
    "CounterField",
    "MessageAnalysis",
    "MultiplexInfo",
    "Signal",
    "SignalConfig",
    "analyse_log",
    "analyse_message",
    "detect_byte_order",
    "bit_stats",
    "crack_checksum",
    "extract_signals",
    "find_counter",
    "find_multiplexer",
]
