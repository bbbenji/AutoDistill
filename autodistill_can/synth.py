"""A synthetic car, for validating the analysis against known ground truth.

Reverse engineering tools are easy to fool yourself with: a plausible-looking
signal list is not evidence that the algorithms work. So this module simulates a
vehicle driving a short route and encodes its state onto a CAN bus using the
same layout tricks real cars use — big- and little-endian fields, signed values,
scale factors, rolling counters, four different checksum families, constant
padding, a multiplexed message, event-driven traffic, and ISO-TP diagnostic
responses.

Because the message definitions below are also the ground truth, the test suite
can assert that the analysis recovers each signal's position, width, byte order
and checksum algorithm exactly. Nothing here is derived from the analysis code,
so a bug in one cannot hide a bug in the other.

The layout is *inspired by* real Toyota/Honda/VW conventions but the addresses
and scalings are invented; this is a test fixture, not a DBC for any real car.
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass
from typing import Callable, Iterator

from .algos import (
    crc8,
    honda_checksum,
    toyota_checksum,
    volkswagen_mqb_checksum,
)
from .bitpack import from_signed, pack_be, pack_le_dbc
from .frame import CanFrame

__all__ = [
    "MESSAGES",
    "SynthField",
    "SynthMessage",
    "VehicleState",
    "generate",
    "ground_truth",
    "reference_series",
]


# --------------------------------------------------------------------------
# Vehicle simulation
# --------------------------------------------------------------------------


@dataclass
class VehicleState:
    """Physical state of the car at one instant."""

    t: float = 0.0
    speed_kph: float = 0.0
    accel_mps2: float = 0.0
    wheel_speeds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    steer_angle_deg: float = 0.0
    steer_rate_dps: float = 0.0
    driver_torque_nm: float = 0.0
    eps_torque_nm: float = 0.0
    gas_pct: float = 0.0
    brake_pct: float = 0.0
    brake_pressed: bool = False
    gear: int = 0  # 0=P 1=R 2=N 3=D
    rpm: float = 0.0
    coolant_c: float = 20.0
    blinker_left: bool = False
    blinker_right: bool = False
    door_open: bool = False
    seatbelt_latched: bool = True
    cruise_on: bool = False
    cruise_engaged: bool = False
    cruise_set_kph: float = 0.0
    lkas_torque: float = 0.0
    lkas_active: bool = False


def _speed_profile(t: float, duration: float) -> tuple[float, float]:
    """A stop-start drive: pull away, cruise, brake, crawl, stop.

    Returns ``(speed_kph, accel_mps2)``. The shape matters more than realism —
    correlation needs a signal that both moves over a wide range and holds
    steady sometimes, so that a constant-looking field is distinguishable from a
    genuinely correlated one.
    """
    phase = t / duration
    if phase < 0.15:  # standing still, engine idling
        speed = 0.0
        accel = 0.0
    elif phase < 0.35:  # accelerate to 60
        frac = (phase - 0.15) / 0.20
        speed = 60.0 * frac
        accel = 60.0 / 3.6 / (0.20 * duration)
    elif phase < 0.55:  # cruise with small variation
        speed = 60.0 + 2.0 * math.sin(2 * math.pi * (phase - 0.35) * 6)
        accel = 0.3 * math.cos(2 * math.pi * (phase - 0.35) * 6)
    elif phase < 0.70:  # brake to 20
        frac = (phase - 0.55) / 0.15
        speed = 60.0 - 40.0 * frac
        accel = -40.0 / 3.6 / (0.15 * duration)
    elif phase < 0.85:  # crawl
        speed = 20.0
        accel = 0.0
    else:  # stop
        frac = min(1.0, (phase - 0.85) / 0.10)
        speed = max(0.0, 20.0 * (1.0 - frac))
        accel = -20.0 / 3.6 / (0.10 * duration)
    return speed, accel


def simulate(
    duration: float = 60.0, dt: float = 0.005, seed: int = 20250730
) -> Iterator[VehicleState]:
    """Yield vehicle state on a fine time grid; messages sample from it."""
    rng = random.Random(seed)
    t = 0.0
    while t < duration:
        speed, accel = _speed_profile(t, duration)

        # Steering: a slow weave plus a couple of firmer turns, so the angle
        # covers both signs and a wide magnitude range.
        angle = 25.0 * math.sin(2 * math.pi * t / 17.0) + 60.0 * math.sin(
            2 * math.pi * t / 53.0
        )
        rate = (
            25.0 * (2 * math.pi / 17.0) * math.cos(2 * math.pi * t / 17.0)
            + 60.0 * (2 * math.pi / 53.0) * math.cos(2 * math.pi * t / 53.0)
        )

        # Real driving is never piecewise-constant: road grade, wind and the
        # driver's own foot keep every analog channel moving, and each sensor
        # adds its own noise. Without this the pedal and torque bytes would sit
        # still for seconds at a time and no analysis — this one or a human with
        # a hex editor — could tell where their field boundaries are.
        accel += 0.25 * math.sin(2 * math.pi * t / 3.7) + rng.gauss(0, 0.06)

        moving = speed > 0.5
        gas = max(
            0.0,
            min(100.0, accel * 25.0 + (8.0 if moving else 0.0) + rng.gauss(0, 0.4)),
        )
        brake = max(0.0, min(100.0, -accel * 30.0 + rng.gauss(0, 0.2)))

        # Wheel speeds differ slightly from each other: driven wheels slip a
        # little under acceleration, and the outside wheels turn faster in a
        # corner. A real capture shows exactly this, and it is what makes four
        # near-identical 16-bit fields distinguishable.
        slip = max(0.0, accel) * 0.05
        yaw = angle * speed * 1e-4
        wheels = (
            speed - yaw + rng.gauss(0, 0.02),
            speed + yaw + rng.gauss(0, 0.02),
            speed - yaw + slip + rng.gauss(0, 0.02),
            speed + yaw + slip + rng.gauss(0, 0.02),
        )
        wheels = tuple(max(0.0, w) for w in wheels)  # type: ignore[assignment]

        cruise_on = t > duration * 0.30
        cruise_engaged = cruise_on and 0.35 < t / duration < 0.55
        lkas_active = cruise_engaged
        # The EPS opposes the driver; when LKAS is active it supplies the torque.
        driver_torque = angle * 0.08 + rng.gauss(0, 0.15)
        lkas_torque = -angle * 0.9 if lkas_active else 0.0

        yield VehicleState(
            t=t,
            speed_kph=speed,
            accel_mps2=accel,
            wheel_speeds=wheels,  # type: ignore[arg-type]
            steer_angle_deg=angle,
            steer_rate_dps=rate,
            driver_torque_nm=driver_torque,
            eps_torque_nm=-driver_torque * 0.6 + lkas_torque * 0.4,
            gas_pct=gas,
            brake_pct=brake,
            brake_pressed=brake > 1.0,
            gear=3 if moving or t > duration * 0.12 else 0,
            rpm=800.0 + speed * 42.0 + rng.gauss(0, 6.0),
            coolant_c=min(92.0, 20.0 + t * 1.4),
            blinker_left=(t % 23.0) < 2.0,
            blinker_right=(t % 31.0) < 1.5,
            door_open=t < duration * 0.05,
            seatbelt_latched=t > duration * 0.03,
            cruise_on=cruise_on,
            cruise_engaged=cruise_engaged,
            cruise_set_kph=60.0 if cruise_on else 0.0,
            lkas_torque=lkas_torque,
            lkas_active=lkas_active,
        )
        t += dt


# --------------------------------------------------------------------------
# Message definitions (these double as the ground truth)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SynthField:
    """One field in a synthetic message.

    ``start`` follows the same dual convention as the recovered
    :class:`~autodistill_can.analysis.signals.Signal`: an MSB-first payload index for
    Motorola fields, and a DBC start bit (LSB-first, naming the least
    significant bit) for Intel ones.
    ``get`` maps vehicle state to the *physical* value; the raw field value is
    ``round((physical - offset) / scale)``, which is the same convention a DBC
    uses, so ``scale``/``offset`` here are directly comparable to what the
    analysis reports.
    """

    name: str
    start: int
    length: int
    kind: str  # physical | enum | bool | counter | checksum | constant
    get: Callable[[VehicleState], float] | None = None
    scale: float = 1.0
    offset: float = 0.0
    signed: bool = False
    big_endian: bool = True
    unit: str = ""
    const: int = 0

    def payload_bits(self, nbits: int) -> list[int]:
        """MSB-first payload positions this field occupies."""
        if self.big_endian:
            return list(range(self.start, self.start + self.length))
        return sorted(
            (j // 8) * 8 + (7 - (j % 8))
            for j in range(self.start, self.start + self.length)
        )

    def raw(self, state: VehicleState) -> int:
        if self.get is None:
            return self.const
        physical = float(self.get(state))
        return round((physical - self.offset) / self.scale)


@dataclass(frozen=True)
class SynthMessage:
    """A synthetic message: address, rate, layout, and integrity fields."""

    name: str
    addr: int
    length: int
    hz: float
    fields: tuple[SynthField, ...]
    bus: int = 0
    #: ``(kind, start, length)`` for the checksum, if any. ``kind`` selects the
    #: algorithm in :func:`_apply_checksum`.
    checksum: tuple[str, int, int] | None = None
    #: ``(start, length)`` of a rolling counter, if any.
    counter: tuple[int, int] | None = None
    #: Event-driven messages fire at random rather than on a fixed period.
    event_driven: bool = False


def _bool(fn: Callable[[VehicleState], bool]) -> Callable[[VehicleState], float]:
    return lambda s: 1.0 if fn(s) else 0.0


#: Magic bytes for the VW-style counter-keyed CRC. Real MQB messages each have
#: their own table; the generic solver is expected to recover this without being
#: told it exists.
_MQB_MAGIC = bytes([0x2E, 0xC3, 0xC7, 0x3E, 0x69, 0xEE, 0x50, 0x33,
                    0x7A, 0x9D, 0x2A, 0x1F, 0x9B, 0x39, 0x54, 0x4E])


MESSAGES: tuple[SynthMessage, ...] = (
    # Four 16-bit wheel speeds, no integrity fields. The classic first target.
    SynthMessage(
        name="WHEEL_SPEEDS",
        addr=0x0AA,
        length=8,
        hz=100.0,
        fields=(
            SynthField("WHEEL_SPEED_FL", 0, 16, "physical",
                       lambda s: s.wheel_speeds[0], scale=0.01, unit="kph"),
            SynthField("WHEEL_SPEED_FR", 16, 16, "physical",
                       lambda s: s.wheel_speeds[1], scale=0.01, unit="kph"),
            SynthField("WHEEL_SPEED_RL", 32, 16, "physical",
                       lambda s: s.wheel_speeds[2], scale=0.01, unit="kph"),
            SynthField("WHEEL_SPEED_RR", 48, 16, "physical",
                       lambda s: s.wheel_speeds[3], scale=0.01, unit="kph"),
        ),
    ),
    # Signed angle and rate, a counter, and a Toyota-style checksum.
    SynthMessage(
        name="STEER_ANGLE_SENSOR",
        addr=0x025,
        length=8,
        hz=80.0,
        fields=(
            SynthField("STEER_ANGLE", 0, 16, "physical",
                       lambda s: s.steer_angle_deg, scale=0.1, signed=True,
                       unit="deg"),
            SynthField("STEER_RATE", 16, 16, "physical",
                       lambda s: s.steer_rate_dps, scale=0.1, signed=True,
                       unit="deg/s"),
            SynthField("STEER_TORQUE_DRIVER", 32, 12, "physical",
                       lambda s: s.driver_torque_nm, scale=0.05, signed=True,
                       unit="Nm"),
            SynthField("PADDING_25", 44, 8, "constant", const=0),
        ),
        counter=(52, 4),
        checksum=("toyota", 56, 8),
    ),
    # Pedals and gear, with Honda's 4-bit nibble checksum.
    SynthMessage(
        name="POWERTRAIN_DATA",
        addr=0x1D2,
        length=8,
        hz=100.0,
        fields=(
            SynthField("GAS_PEDAL", 0, 8, "physical", lambda s: s.gas_pct,
                       scale=0.5, unit="%"),
            SynthField("BRAKE_PRESSURE", 8, 10, "physical",
                       lambda s: s.brake_pct, scale=0.25, unit="%"),
            SynthField("BRAKE_PRESSED", 18, 1, "bool",
                       _bool(lambda s: s.brake_pressed)),
            SynthField("GEAR", 20, 4, "enum", lambda s: float(s.gear)),
            SynthField("ENGINE_RPM", 24, 16, "physical", lambda s: s.rpm,
                       scale=0.25, unit="rpm"),
            SynthField("PADDING_1D2", 40, 8, "constant", const=0),
            SynthField("CHECKSUM_HIGH_NIBBLE", 56, 4, "constant", const=0),
        ),
        counter=(48, 4),
        checksum=("honda", 60, 4),
    ),
    # A wholly Intel-ordered message, as Hyundai/Kia and VW messages are: real
    # cars pick one byte order per platform and keep to it. Across nine
    # opendbc databases only 3.8% of multi-signal messages mix the two, so a
    # fixture that sprinkled a lone Intel field among Motorola ones would be
    # testing something no car does. ACCEL_LONG is deliberately unaligned --
    # 12 bits straddling a byte boundary, exactly like Hyundai's CF_Clu_Vanz
    # (vehicle speed) -- because that layout cannot even be expressed as a
    # contiguous span in MSB-first numbering.
    SynthMessage(
        name="ENGINE_DATA",
        addr=0x1C4,
        length=8,
        hz=50.0,
        fields=(
            SynthField("ENGINE_TORQUE", 0, 16, "physical",
                       lambda s: s.rpm * 0.1, scale=0.5, big_endian=False,
                       unit="Nm"),
            SynthField("COOLANT_TEMP", 16, 8, "physical",
                       lambda s: s.coolant_c, scale=1.0, offset=-40.0,
                       big_endian=False, unit="degC"),
            SynthField("THROTTLE_POS", 24, 8, "physical", lambda s: s.gas_pct,
                       scale=0.4, big_endian=False, unit="%"),
            SynthField("ACCEL_LONG", 32, 12, "physical",
                       lambda s: s.accel_mps2, scale=0.01, signed=True,
                       big_endian=False, unit="m/s2"),
            # ACCEL_LONG's 12 Intel bits cover byte 4 and the *low* half of
            # byte 5, so the padding either side of the counter is split.
            SynthField("PADDING_1C4", 44, 4, "constant", const=0,
                       big_endian=False),
            SynthField("PADDING_1C4_B", 52, 4, "constant", const=0,
                       big_endian=False),
        ),
        # MSB-first bits 52-55 are Intel bits 48-51: the same nibble either way.
        counter=(52, 4),
        checksum=("crc8_autosar", 56, 8),
    ),
    # Body/comfort bits at a low rate: mostly boolean, mostly idle.
    SynthMessage(
        name="BODY_STATE",
        addr=0x4D0,
        length=8,
        hz=10.0,
        fields=(
            SynthField("DOOR_OPEN_FL", 0, 1, "bool", _bool(lambda s: s.door_open)),
            SynthField("DOOR_OPEN_FR", 1, 1, "bool", _bool(lambda s: False)),
            SynthField("DOOR_OPEN_RL", 2, 1, "bool", _bool(lambda s: False)),
            SynthField("DOOR_OPEN_RR", 3, 1, "bool", _bool(lambda s: False)),
            SynthField("SEATBELT_DRIVER", 4, 1, "bool",
                       _bool(lambda s: s.seatbelt_latched)),
            SynthField("BLINKER_LEFT", 5, 1, "bool",
                       _bool(lambda s: s.blinker_left)),
            SynthField("BLINKER_RIGHT", 6, 1, "bool",
                       _bool(lambda s: s.blinker_right)),
            SynthField("PADDING_4D0", 7, 49, "constant", const=0),
            SynthField("BODY_ID", 56, 8, "constant", const=0x5A),
        ),
    ),
    # Cruise state guarded by the counter-keyed CRC that only the generic
    # GF(2) solver can crack.
    SynthMessage(
        name="CRUISE_STATE",
        addr=0x1A6,
        length=8,
        hz=50.0,
        fields=(
            SynthField("CRUISE_SET_SPEED", 16, 8, "physical",
                       lambda s: s.cruise_set_kph, scale=0.5, unit="kph"),
            SynthField("CRUISE_MAIN_ON", 24, 1, "bool",
                       _bool(lambda s: s.cruise_on)),
            SynthField("CRUISE_ENGAGED", 25, 1, "bool",
                       _bool(lambda s: s.cruise_engaged)),
            SynthField("SET_DISTANCE", 26, 2, "enum", lambda s: 2.0),
            SynthField("PADDING_1A6", 28, 4, "constant", const=0),
            SynthField("ACC_ACCEL_REQ", 32, 12, "physical",
                       lambda s: s.accel_mps2 if s.cruise_engaged else 0.0,
                       scale=0.005, signed=True, unit="m/s2"),
            SynthField("PADDING_1A6_B", 44, 20, "constant", const=0),
        ),
        counter=(12, 4),
        checksum=("vw_mqb", 0, 8),
    ),
    # A multiplexed message: byte 0 selects what the rest means.
    SynthMessage(
        name="MULTIPLEXED_DIAG",
        addr=0x6B0,
        length=8,
        hz=20.0,
        fields=(
            SynthField("MUX_ID", 0, 8, "mux", lambda s: float(int(s.t * 20) % 3)),
            SynthField("MUX_PAYLOAD", 8, 32, "physical",
                       lambda s: (s.speed_kph if int(s.t * 20) % 3 == 0
                                  else s.rpm if int(s.t * 20) % 3 == 1
                                  else s.coolant_c),
                       scale=0.1),
            SynthField("PADDING_6B0", 40, 24, "constant", const=0),
        ),
    ),
    # Event-driven: fires only on blinker changes, so its timing is irregular.
    SynthMessage(
        name="TURN_EVENT",
        addr=0x5A0,
        length=4,
        hz=2.0,
        fields=(
            SynthField("EVENT_CODE", 0, 8, "enum", lambda s: 3.0),
            SynthField("EVENT_COUNT", 8, 16, "physical", lambda s: s.t, scale=1.0),
            SynthField("PADDING_5A0", 24, 8, "constant", const=0),
        ),
        event_driven=True,
    ),
    # The LKAS command openpilot would have to synthesise, seen coming from the
    # stock camera on bus 2. Getting its counter and checksum right is the whole
    # game when porting, so it is on the far bus with a Hyundai-style sum.
    SynthMessage(
        name="LKAS_COMMAND",
        addr=0x2E4,
        length=8,
        hz=100.0,
        bus=2,
        fields=(
            SynthField("STEER_TORQUE_CMD", 0, 16, "physical",
                       lambda s: s.lkas_torque, scale=0.1, signed=True,
                       unit="Nm"),
            SynthField("LKAS_ACTIVE", 16, 1, "bool",
                       _bool(lambda s: s.lkas_active)),
            SynthField("LKAS_STATE", 17, 3, "enum",
                       lambda s: 3.0 if s.lkas_active else 1.0),
            SynthField("PADDING_2E4", 20, 28, "constant", const=0),
        ),
        counter=(48, 4),
        checksum=("sum8_twos", 56, 8),
    ),
)


def check_layouts() -> None:
    """Assert no two fields of a message claim the same bit.

    The message table is the ground truth the tests compare against, so an
    overlap here would quietly encode one field over another and make the
    expected layout a lie. Called from the test suite.
    """
    for msg in MESSAGES:
        owner: dict[int, str] = {}
        nbits = msg.length * 8
        spans: list[tuple[str, list[int]]] = [
            (f.name, f.payload_bits(nbits)) for f in msg.fields
        ]
        if msg.counter is not None:
            spans.append(("COUNTER", list(range(msg.counter[0],
                                                msg.counter[0] + msg.counter[1]))))
        if msg.checksum is not None:
            start, length = msg.checksum[1], msg.checksum[2]
            spans.append(("CHECKSUM", list(range(start, start + length))))
        for name, bits in spans:
            if not bits or max(bits) >= nbits:
                raise AssertionError(
                    f"{msg.name}.{name} runs past the end of the message"
                )
            for bit in bits:
                if bit in owner:
                    raise AssertionError(
                        f"{msg.name}: {name} overlaps {owner[bit]} at bit {bit}"
                    )
                owner[bit] = name


def _apply_checksum(msg: SynthMessage, buf: bytearray, counter: int) -> None:
    """Fill in the message's checksum field over the already-populated payload."""
    kind, start, length = msg.checksum  # type: ignore[misc]

    if kind == "toyota":
        # Occupies the final byte; computed over everything before it.
        value = toyota_checksum(msg.addr, bytes(buf[: msg.length - 1]), msg.length)
    elif kind == "honda":
        # 4-bit, in the low nibble of the last byte, over the whole payload.
        value = honda_checksum(msg.addr, bytes(buf), msg.length)
    elif kind == "sum8_twos":
        value = (-sum(buf[: msg.length - 1])) & 0xFF
    elif kind == "crc8_autosar":
        value = crc8(bytes(buf[: msg.length - 1]), poly=0x2F, init=0xFF, xorout=0xFF)
    elif kind == "vw_mqb":
        value = volkswagen_mqb_checksum(
            msg.addr, bytes(buf), msg.length, counter, _MQB_MAGIC
        )
    else:  # pragma: no cover - guarded by the message table
        raise ValueError(f"unknown synthetic checksum kind: {kind}")

    pack_be(buf, start, length, value)


def _encode(msg: SynthMessage, state: VehicleState, counter: int) -> bytes:
    buf = bytearray(msg.length)

    for f in msg.fields:
        raw = f.raw(state)
        if f.signed:
            limit = 1 << (f.length - 1)
            raw = max(-limit, min(limit - 1, raw))
            raw = from_signed(raw, f.length)
        else:
            raw = max(0, min((1 << f.length) - 1, raw))
        if f.big_endian:
            pack_be(buf, f.start, f.length, raw)
        else:
            pack_le_dbc(buf, f.start, f.length, raw)

    if msg.counter is not None:
        c_start, c_len = msg.counter
        pack_be(buf, c_start, c_len, counter & ((1 << c_len) - 1))

    if msg.checksum is not None:
        _apply_checksum(msg, buf, counter)

    return bytes(buf)


def generate(
    duration: float = 60.0,
    *,
    seed: int = 20250730,
    include_diagnostics: bool = True,
) -> list[CanFrame]:
    """Generate a full synthetic capture, in time order.

    Returns a list rather than an iterator because callers almost always want to
    both write it out and analyse it.
    """
    rng = random.Random(seed ^ 0x5EED)
    dt = 0.001
    states = list(simulate(duration, dt, seed))

    def state_at(t: float) -> VehicleState:
        """Nearest simulated state, standing in for an ECU's own sampling."""
        i = min(max(int(round(t / dt)), 0), len(states) - 1)
        return states[i]

    frames: list[CanFrame] = []
    # Each message is scheduled on its own exact clock rather than on the
    # simulation grid; quantising transmit times to the grid would drag every
    # rate down to a divisor of it.
    for msg in MESSAGES:
        # Real ECUs boot at unrelated moments, so give each a random phase.
        t = rng.uniform(0.0, 1.0 / msg.hz)
        counter = 0
        while t < duration:
            payload = _encode(msg, state_at(t), counter)
            frames.append(CanFrame(t=t, addr=msg.addr, data=payload, bus=msg.bus))
            counter += 1
            if msg.event_driven:
                # Irregular by design: exponential gaps around 1/hz.
                t += rng.expovariate(msg.hz)
            else:
                t += 1.0 / msg.hz + rng.gauss(0, 2e-4)  # a little ECU jitter

    if include_diagnostics:
        frames.extend(_diagnostic_frames(duration))

    frames.sort(key=lambda f: f.t)
    return frames


# --------------------------------------------------------------------------
# Diagnostics: ISO-TP framed UDS responses, as openpilot's FW fingerprinting
# collects them.
# --------------------------------------------------------------------------

#: ``(request_addr, response_addr, ecu_name, firmware_bytes)``. The firmware
#: bytes are the payload *after* the service and data identifier, which is where
#: a real ECU puts its part number.
SYNTH_ECUS: tuple[tuple[int, int, str, bytes], ...] = (
    (0x7E0, 0x7E8, "engine", b"89663-33010\x00\x00"),
    (0x7E2, 0x7EA, "eps", b"8965B-45070\x00\x00"),
    (0x7B0, 0x7B8, "abs", b"89541-06040\x00\x00"),
)

_UDS_READ_DATA_BY_ID = 0x22
_UDS_POSITIVE_OFFSET = 0x40


def _isotp_frames(
    addr: int, payload: bytes, t0: float, bus: int = 0
) -> list[CanFrame]:
    """Split a UDS payload into ISO-TP single/first/consecutive frames."""
    out: list[CanFrame] = []
    if len(payload) <= 7:
        data = bytes([len(payload)]) + payload
        out.append(CanFrame(t=t0, addr=addr, data=data.ljust(8, b"\x00"), bus=bus))
        return out

    first = bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:6]
    out.append(CanFrame(t=t0, addr=addr, data=first.ljust(8, b"\x00"), bus=bus))
    rest = payload[6:]
    seq = 1
    t = t0
    while rest:
        chunk, rest = rest[:7], rest[7:]
        t += 0.005
        data = bytes([0x20 | (seq & 0x0F)]) + chunk
        out.append(CanFrame(t=t, addr=addr, data=data.ljust(8, b"\x00"), bus=bus))
        seq += 1
    return out


def _diagnostic_frames(duration: float) -> list[CanFrame]:
    """A diagnostic exchange partway through the drive."""
    out: list[CanFrame] = []
    t = duration * 0.5
    for req_addr, resp_addr, _name, fw in SYNTH_ECUS:
        did = 0xF188
        request = bytes([_UDS_READ_DATA_BY_ID, did >> 8, did & 0xFF])
        out.extend(_isotp_frames(req_addr, request, t))
        response = bytes([_UDS_READ_DATA_BY_ID + _UDS_POSITIVE_OFFSET,
                          did >> 8, did & 0xFF]) + fw
        out.extend(_isotp_frames(resp_addr, response, t + 0.01))
        t += 0.5
    return out


# --------------------------------------------------------------------------
# Ground truth
# --------------------------------------------------------------------------


def ground_truth() -> dict:
    """The full known layout, in the shape the report and tests compare against."""
    messages = []
    for msg in MESSAGES:
        signals = []
        for f in msg.fields:
            entry = {
                k: v for k, v in asdict(f).items() if k not in ("get",)
            }
            signals.append(entry)
        if msg.counter is not None:
            signals.append({
                "name": "COUNTER", "start": msg.counter[0],
                "length": msg.counter[1], "kind": "counter",
                "scale": 1.0, "offset": 0.0, "signed": False,
                "big_endian": True, "unit": "", "const": 0,
            })
        if msg.checksum is not None:
            kind, start, length = msg.checksum
            signals.append({
                "name": "CHECKSUM", "start": start, "length": length,
                "kind": "checksum", "algorithm": kind, "scale": 1.0,
                "offset": 0.0, "signed": False, "big_endian": True,
                "unit": "", "const": 0,
            })
        messages.append({
            "name": msg.name,
            "addr": msg.addr,
            "bus": msg.bus,
            "length": msg.length,
            "hz": msg.hz,
            "event_driven": msg.event_driven,
            "signals": sorted(signals, key=lambda s: s["start"]),
        })
    return {
        "description": "AutoDistill synthetic car (test fixture, not a real vehicle)",
        "messages": sorted(messages, key=lambda m: (m["bus"], m["addr"])),
        "ecus": [
            {"request": req, "response": resp, "name": name,
             "fw": fw.rstrip(b"\x00").decode("latin-1")}
            for req, resp, name, fw in SYNTH_ECUS
        ],
    }


def reference_series(
    duration: float = 60.0, hz: float = 10.0, seed: int = 20250730
) -> list[dict[str, float]]:
    """A ground-truth reference log, standing in for GPS/OBD-II/openpilot data.

    This is what you would bring to a real capture to name signals: an
    independent measurement of a few physical quantities, sampled at its own
    rate. Deliberately coarse and slightly noisy so the correlator has to cope
    with real conditions.
    """
    rng = random.Random(seed ^ 0xBEEF)
    step = 1.0 / hz
    rows: list[dict[str, float]] = []
    t = 0.0
    while t < duration:
        speed, _ = _speed_profile(t, duration)
        angle = 25.0 * math.sin(2 * math.pi * t / 17.0) + 60.0 * math.sin(
            2 * math.pi * t / 53.0
        )
        rows.append({
            "time": round(t, 4),
            "speed_kph": round(speed + rng.gauss(0, 0.15), 3),
            "steer_angle_deg": round(angle + rng.gauss(0, 0.3), 3),
        })
        t += step
    return rows
