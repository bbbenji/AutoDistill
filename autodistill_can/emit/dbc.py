"""DBC file generation.

A DBC is the lingua franca of CAN reverse engineering: opendbc uses it, cabana
displays it, cantools parses it. Emitting one means the recovered layout can be
loaded straight into the tools people already use to check the work.

Bit numbering is the fiddly part. This package indexes payload bits MSB-first
(index 0 is the top bit of byte 0). A DBC instead numbers bits LSB-first within
each byte, and its ``start bit`` means different things per byte order: the
*most* significant bit for a big-endian signal, the *least* significant for a
little-endian one. Both conversions live in :func:`dbc_start_bit`, and nowhere
else.
"""

from __future__ import annotations

from typing import IO, Iterable

from ..analysis.message import MessageAnalysis
from ..analysis.signals import Signal

__all__ = ["dbc_frame_id", "dbc_start_bit", "write_dbc"]

#: A DBC marks a 29-bit (extended) identifier by setting bit 31 of the frame id
#: it writes in the ``BO_`` line. Without it, a reader sees a standard 11-bit id
#: with an impossible value and rejects the file.
_DBC_EXTENDED_FLAG = 0x80000000
_MAX_STANDARD_ID = 0x7FF


def dbc_frame_id(addr: int) -> int:
    """The value a DBC's ``BO_`` line carries for a CAN address."""
    return addr | _DBC_EXTENDED_FLAG if addr > _MAX_STANDARD_ID else addr


def dbc_start_bit(start: int, length: int, big_endian: bool) -> int:
    """The DBC start bit for a recovered signal.

    A :class:`~autodistill_can.analysis.signals.Signal` already stores each byte
    order in its native numbering: ``start`` is an MSB-first payload index for
    Motorola signals, and for Intel signals it is a DBC start bit already, so
    only the Motorola case needs converting. A DBC's Motorola start bit names
    the signal's most significant bit, renumbered LSB-first within its byte.
    """
    if not big_endian:
        return start
    byte, bit = divmod(start, 8)
    return byte * 8 + (7 - bit)


def _sanitise(name: str, fallback: str) -> str:
    """Coerce a name into the identifier subset a DBC allows."""
    cleaned = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
    cleaned = cleaned.strip("_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = fallback
    return cleaned


def _signal_line(
    signal: Signal, addr: int, used: set[str], mux: str = ""
) -> str:
    name = _sanitise(signal.default_name(addr), f"SIG_{signal.start}")
    # DBC signal names must be unique within a message.
    if name in used:
        suffix = 2
        while f"{name}_{suffix}" in used:
            suffix += 1
        name = f"{name}_{suffix}"
    used.add(name)

    start_bit = dbc_start_bit(signal.start, signal.length, signal.big_endian)
    order = "0" if signal.big_endian else "1"
    sign = "-" if signal.signed else "+"
    scale = signal.scale if signal.scale else 1.0
    offset = signal.offset

    raw_min, raw_max = signal.raw_min, signal.raw_max
    phys_min = raw_min * scale + offset
    phys_max = raw_max * scale + offset
    if phys_min > phys_max:
        phys_min, phys_max = phys_max, phys_min

    mux_field = f" {mux}" if mux else ""
    return (
        f' SG_ {name}{mux_field} : {start_bit}|{signal.length}@{order}{sign}'
        f' ({_num(scale)},{_num(offset)})'
        f' [{_num(phys_min)}|{_num(phys_max)}]'
        f' "{signal.unit}" XXX'
    )


def _num(value: float) -> str:
    """Format a number the way DBC parsers expect: plain decimal, no exponent."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    formatted = f"{value:.10f}".rstrip("0").rstrip(".")
    return formatted or "0"


def write_dbc(
    analyses: Iterable[MessageAnalysis],
    out: IO[str],
    *,
    title: str = "AutoDistill",
    node: str = "XXX",
) -> int:
    """Write recovered messages as a DBC. Returns the number of messages written.

    A DBC has no notion of physical buses, so all analyses passed here must have
    unique addresses. :func:`autodistill_can.emit.port.write_port` calls this once
    per bus; direct callers get a clear error if two buses reuse an address.
    """
    analyses = sorted(analyses, key=lambda a: (a.bus, a.addr))
    seen_addresses: dict[int, int] = {}
    for analysis in analyses:
        if analysis.length <= 0:
            continue
        if analysis.addr in seen_addresses:
            previous_bus = seen_addresses[analysis.addr]
            raise ValueError(
                f"DBC cannot represent address 0x{analysis.addr:X} on both "
                f"bus {previous_bus} and bus {analysis.bus}; write one DBC per bus"
            )
        seen_addresses[analysis.addr] = analysis.bus

    out.write(f'VERSION "{title}"\n\n\n')
    out.write("NS_ :\n\tBA_\n\tBA_DEF_\n\tBA_DEF_DEF_\n\tBA_DEF_DEF_REL_\n")
    out.write("\tBA_DEF_REL_\n\tBA_DEF_SGTYPE_\n\tBA_REL_\n\tBA_SGTYPE_\n")
    out.write("\tBU_BO_REL_\n\tBU_EV_REL_\n\tBU_SG_REL_\n\tCAT_\n\tCAT_DEF_\n")
    out.write("\tCM_\n\tENVVAR_DATA_\n\tEV_DATA_\n\tFILTER\n\tNS_DESC_\n")
    out.write("\tSGTYPE_\n\tSGTYPE_VAL_\n\tSG_MUL_VAL_\n\tSIGTYPE_VALTYPE_\n")
    out.write("\tSIG_GROUP_\n\tSIG_TYPE_REF_\n\tSIG_VALTYPE_\n\tVAL_\n")
    out.write("\tVAL_TABLE_\n\n")
    out.write("BS_:\n\n")
    out.write(f"BU_: {node}\n\n")

    comments: list[str] = []
    written = 0

    for analysis in analyses:
        if analysis.length <= 0:
            continue
        name = f"{analysis.name}"
        out.write(
            f"BO_ {dbc_frame_id(analysis.addr)} {name}: "
            f"{analysis.length} {node}\n"
        )

        used: set[str] = set()
        if analysis.multiplex is not None:
            # A multiplexed message is described *only* by its selector and the
            # per-mode signals. Emitting the whole-message signal list as well
            # would place plain signals over the top of multiplexed ones, and a
            # DBC reader rejects that outright: overlap is legal between signals
            # carrying different multiplexer values, never between a
            # multiplexed signal and a plain one.
            mux = analysis.multiplex
            selector = Signal(
                start=mux.start, length=mux.length, kind="enum", name="MUX_MODE"
            )
            selector.raw_max = (1 << mux.length) - 1
            out.write(_signal_line(selector, analysis.addr, used, mux="M") + "\n")
            for value, signals in sorted(analysis.mux_signals.items()):
                for signal in signals:
                    if signal.kind == "constant" or (
                        mux.start <= signal.start < mux.end
                    ):
                        continue
                    out.write(
                        _signal_line(signal, analysis.addr, used, mux=f"m{value}")
                        + "\n"
                    )
        else:
            for signal in analysis.signals:
                out.write(_signal_line(signal, analysis.addr, used) + "\n")
        out.write("\n")
        written += 1

        note_bits: list[str] = [f"bus {analysis.bus}"]
        if analysis.frequency:
            note_bits.append(f"{analysis.frequency:.0f} Hz")
        if analysis.checksum is not None:
            note_bits.append(f"checksum: {analysis.checksum.describe()}")
        if analysis.is_event_driven:
            note_bits.append("event-driven")
        comments.append(
            f'CM_ BO_ {dbc_frame_id(analysis.addr)} "{"; ".join(note_bits)}";'
        )

    for comment in comments:
        out.write(comment.replace("\n", " ") + "\n")

    return written
