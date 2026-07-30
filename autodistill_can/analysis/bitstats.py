"""Per-bit statistics for one message.

Everything downstream is built on these numbers, so they are computed once per
message and passed around. The important one is the **bit-flip rate** (BFR): the
fraction of consecutive frames in which a bit changed value.

BFR is what makes field boundaries visible without knowing anything about the
car. Inside a multi-byte numeric field the low-order bits change almost every
frame while the high-order bits change rarely, so BFR rises steadily from the
field's most significant bit to its least. At the next field's boundary it drops
back down. That single asymmetry is enough to segment a payload, and it is what
:mod:`autodistill_can.analysis.signals` builds on.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..frame import MessageStream

__all__ = ["BitStats", "bit_stats"]


@dataclass
class BitStats:
    """Per-bit summary over every frame of one message.

    All lists are indexed by MSB-first bit position, so index 0 is the most
    significant bit of byte 0.
    """

    n_samples: int
    nbits: int
    #: Times the bit differed from the previous frame's value.
    flips: list[int]
    #: Frames in which the bit was 1.
    ones: list[int]

    @property
    def flip_rate(self) -> list[float]:
        """Flips per transition opportunity, in [0, 1]."""
        denom = max(1, self.n_samples - 1)
        return [f / denom for f in self.flips]

    @property
    def one_rate(self) -> list[float]:
        denom = max(1, self.n_samples)
        return [o / denom for o in self.ones]

    def is_constant(self, bit: int) -> bool:
        return self.flips[bit] == 0

    def constant_value(self, bit: int) -> int | None:
        """The held value of a never-changing bit, or None if it changed."""
        if self.flips[bit] != 0:
            return None
        return 1 if self.ones[bit] else 0

    def entropy(self, bit: int) -> float:
        """Shannon entropy of the bit's value distribution, in bits.

        Distinguishes a bit that is genuinely balanced from one that is almost
        always the same value — useful for telling an active boolean apart from
        a flag that happened to toggle once during the capture.
        """
        from math import log2

        p = self.one_rate[bit]
        if p <= 0.0 or p >= 1.0:
            return 0.0
        return -(p * log2(p) + (1 - p) * log2(1 - p))

    def changed_bits(self) -> list[int]:
        return [i for i in range(self.nbits) if self.flips[i]]

    def constant_bits(self) -> list[int]:
        return [i for i in range(self.nbits) if not self.flips[i]]

    def constant_payload(self) -> bytes:
        """The message's payload with every non-constant bit zeroed.

        Reveals the fixed skeleton of a message — identifier bytes, unused
        padding set to a magic value, and so on.
        """
        out = bytearray(self.nbits // 8)
        for i in range(self.nbits):
            if self.flips[i] == 0 and self.ones[i]:
                out[i // 8] |= 1 << (7 - (i % 8))
        return bytes(out)


def bit_stats(stream: MessageStream) -> BitStats:
    """Compute per-bit flip and one counts for a message.

    Implemented with whole-payload integer XOR rather than a per-bit loop: an
    hour-long capture of a 100 Hz message is 360k frames, and Python-level
    iteration over 64 bits each would dominate the runtime. XOR-ing consecutive
    payloads as big integers gets the whole flip mask in one operation, and
    ``int.bit_count`` locates the set bits cheaply.
    """
    nbits = stream.nbits
    flips = [0] * nbits
    ones = [0] * nbits
    if nbits == 0 or not stream.payloads:
        return BitStats(n_samples=len(stream), nbits=nbits, flips=flips, ones=ones)

    length = stream.length
    prev: int | None = None
    for payload in stream.payloads:
        # A message that changes length mid-capture is truncated to the
        # shortest seen, so bit indices stay comparable across frames.
        value = int.from_bytes(payload[:length], "big")

        # Count set bits, MSB-first.
        v = value
        while v:
            low = v & -v  # lowest set bit
            pos = low.bit_length() - 1  # 0 = LSB of last byte
            ones[nbits - 1 - pos] += 1
            v ^= low

        if prev is not None:
            diff = prev ^ value
            while diff:
                low = diff & -diff
                pos = low.bit_length() - 1
                flips[nbits - 1 - pos] += 1
                diff ^= low
        prev = value

    return BitStats(n_samples=len(stream), nbits=nbits, flips=flips, ones=ones)
