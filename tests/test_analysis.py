"""Analysis against the synthetic car's known layout.

The synthetic car is defined independently of every algorithm here, so these
tests measure recovery rather than self-consistency.

Where a test asserts a *rate* rather than a per-case result, the number is a
regression baseline measured from the current implementation, not a target. It
is there so that a change which quietly makes recovery worse fails loudly.
"""

from __future__ import annotations

import pytest
from conftest import dbc_start, truth_dbc_start, truth_signal

from autodistill_can import synth
from autodistill_can.algos import crc8
from autodistill_can.analysis import bit_stats
from autodistill_can.analysis.checksum import crack_checksum, gf2_rank, solve_affine
from autodistill_can.analysis.counter import find_counter
from autodistill_can.analysis.multiplex import find_multiplexer
from autodistill_can.analysis.signals import extract_signals


def test_synthetic_message_layouts_do_not_overlap():
    # The message table doubles as ground truth, so an overlap would make the
    # expected layout a lie and quietly weaken every test below.
    synth.check_layouts()


# --------------------------------------------------------------------------
# Bit statistics
# --------------------------------------------------------------------------


def test_bit_stats_counts_flips_and_ones():
    from autodistill_can.frame import CanFrame, MessageStream

    stream = MessageStream(bus=0, addr=0x100)
    for payload in (b"\x00", b"\xff", b"\x00", b"\xff"):
        stream.add(CanFrame(t=0.0, addr=0x100, data=payload))
    stats = bit_stats(stream)
    assert stats.nbits == 8
    assert stats.flips == [3] * 8  # every bit changed on every transition
    assert stats.ones == [2] * 8


def test_bit_stats_identifies_constant_bits():
    from autodistill_can.frame import CanFrame, MessageStream

    stream = MessageStream(bus=0, addr=0x100)
    for payload in (b"\xf0", b"\xf5", b"\xfa"):
        stream.add(CanFrame(t=0.0, addr=0x100, data=payload))
    stats = bit_stats(stream)
    assert stats.constant_bits() == [0, 1, 2, 3]
    assert stats.constant_payload() == b"\xf0"


def test_wheel_speed_flip_rates_ramp_within_each_field(log):
    # The premise the whole tokenizer rests on: inside a numeric field the flip
    # rate climbs from the field's MSB to its LSB, then resets at the boundary.
    stats = bit_stats(log.streams[(0, 0x0AA)])
    rates = stats.flip_rate
    for field_start in (0, 16, 32, 48):
        assert rates[field_start] < rates[field_start + 15]
    for boundary in (16, 32, 48):
        assert rates[boundary] < rates[boundary - 1]


# --------------------------------------------------------------------------
# Counters
# --------------------------------------------------------------------------


def test_counters_found_at_exact_positions(log, truth, analyses):
    checked = 0
    for key, analysis in analyses.items():
        if key not in truth:
            continue
        expected = truth_signal(truth[key], "counter")
        found = analysis.counter
        if expected is None:
            assert found is None, f"false counter on 0x{key[1]:X}: {found}"
        else:
            assert found is not None, f"missed counter on 0x{key[1]:X}"
            assert (found.start, found.length) == (
                expected["start"],
                expected["length"],
            )
            assert found.step == 1
            checked += 1
    assert checked == 5, "expected five counters in the synthetic car"


def test_counter_detection_requires_the_field_to_cycle(log):
    # A slowly ramping physical value must not be mistaken for a counter.
    stream = log.streams[(0, 0x0AA)]
    assert find_counter(stream, bit_stats(stream)) is None


def test_six_bit_toyota_counter_need_not_be_nibble_aligned():
    """Real RAV4 STEERING_LKA puts its six-bit counter at payload bits 1..6."""
    from autodistill_can.frame import CanFrame, MessageStream

    frames = []
    for index in range(128):
        # STEER_REQUEST at bit 0, counter at bits 1..6, SET_ME_1 at bit 7.
        first = 0x81 | ((index & 0x3F) << 1)
        frames.append(CanFrame(
            t=index * 0.01, addr=0x2E4, bus=0,
            data=bytes([first, 0, 0, 0, 0]),
        ))
    stream = MessageStream(0, 0x2E4)
    for frame in frames:
        stream.add(frame)
    found = find_counter(stream, bit_stats(stream))
    assert found is not None
    assert (found.start, found.length, found.step) == (1, 6, 1)


# --------------------------------------------------------------------------
# Checksums
# --------------------------------------------------------------------------


def test_checksums_found_at_exact_positions(log, truth, analyses):
    checked = 0
    for key, analysis in analyses.items():
        if key not in truth:
            continue
        expected = truth_signal(truth[key], "checksum")
        found = analysis.checksum
        if expected is None:
            assert found is None, f"false checksum on 0x{key[1]:X}: {found}"
        else:
            assert found is not None, f"missed checksum on 0x{key[1]:X}"
            assert (found.start, found.length) == (
                expected["start"],
                expected["length"],
            ), f"wrong checksum position on 0x{key[1]:X}"
            assert found.accuracy >= 0.999
            checked += 1
    assert checked == 5, "expected five checksums in the synthetic car"


@pytest.mark.parametrize(
    "addr,algorithm",
    [(0x025, "toyota"), (0x1C4, "crc8_autosar_2f"), (0x1D2, "honda"),
     (0x2E4, "sum8_twos")],
)
def test_named_algorithms_are_identified(analyses, addr, algorithm):
    analysis = next(a for k, a in analyses.items() if k[1] == addr)
    assert analysis.checksum is not None
    assert analysis.checksum.method == "algo"
    assert analysis.checksum.algo == algorithm


def test_counter_keyed_checksum_is_solved_and_flagged(analyses):
    # The VW-style message mixes a counter-selected constant into a CRC, which
    # no fixed algorithm reproduces; only the per-counter GF(2) solve gets it.
    analysis = next(a for k, a in analyses.items() if k[1] == 0x1A6)
    solution = analysis.checksum
    assert solution is not None
    assert solution.method == "affine_per_counter"
    assert len(solution.counter_constants) == 16
    assert solution.accuracy >= 0.999
    # This message is too static for the split to be unique, and saying so is
    # the honest answer rather than presenting one arbitrary solution as fact.
    assert solution.underdetermined
    assert "UNDERDETERMINED" in solution.describe()


def test_checksum_alias_is_not_mistaken_for_the_real_field(analyses):
    # A sum checksum makes every byte a valid checksum of the others. The real
    # one is at the end of 0x2E4; byte 0 is the steering torque high byte.
    analysis = next(a for k, a in analyses.items() if k[1] == 0x2E4)
    assert analysis.checksum is not None
    assert analysis.checksum.start == 56


def test_sub_byte_checksum_beside_a_counter_is_named():
    """Regression: found on a real Kia Soul EV steering-angle message.

    A 4-bit checksum in the high nibble of a byte whose low nibble is the
    rolling counter, covering every other nibble in the payload. The counter is
    part of what the checksum covers, so an input selection that blanks the
    whole byte loses it and no named formula fits -- the solver then falls back
    to reporting an anonymous 60-bit mask table, which is correct but useless to
    someone writing the port.
    """
    import math

    from autodistill_can.algos import nibble_xor
    from autodistill_can.bitpack import from_signed, pack_be, pack_le_aligned
    from autodistill_can.frame import CanFrame, MessageStream

    # Laid out like the real message: a steering angle in bytes 0-1, a constant,
    # then the counter and checksum sharing byte 4. The angle must be a smooth
    # signal rather than noise -- nibble XOR is symmetric, so *every* nibble is
    # algebraically a valid checksum of the others, and what identifies the real
    # one is that it looks random while its neighbours do not.
    stream = MessageStream(bus=0, addr=0x2B0)
    for i in range(600):
        buf = bytearray(8)
        angle = round(300 * math.sin(i / 40.0))
        pack_le_aligned(buf, 0, 16, from_signed(angle, 16))
        buf[3] = 0x07
        pack_be(buf, 36, 4, i & 0xF)  # counter in the low nibble of byte 4
        pack_be(buf, 32, 4, nibble_xor(bytes(buf)))  # checksum in the high nibble
        stream.add(CanFrame(t=i * 0.01, addr=0x2B0, data=bytes(buf)))

    stats = bit_stats(stream)
    counter = find_counter(stream, stats)
    assert counter is not None and (counter.start, counter.length) == (36, 4)

    solution = crack_checksum(stream, stats, counter)
    assert solution is not None
    assert (solution.start, solution.length) == (32, 4)
    assert solution.method == "algo", "should be a named formula, not a bit mask"
    assert solution.algo == "nibble_xor4"
    assert solution.input_selection == "field_zeroed"


def test_gf2_solver_recovers_a_crc_from_observations_alone():
    import random

    rng = random.Random(11)
    payloads = [bytes(rng.randrange(256) for _ in range(7)) for _ in range(400)]
    rows = [int.from_bytes(p, "big") for p in payloads]
    assert gf2_rank(rows) == 56  # the observations pin the map down completely

    masks, constants = [], []
    for bit in range(8):
        targets = [(crc8(p, 0x2F, 0xFF, 0xFF) >> (7 - bit)) & 1 for p in payloads]
        solved = solve_affine(rows, targets, 56)
        assert solved is not None
        masks.append(solved[0])
        constants.append(solved[1])

    # A real CRC depends on roughly half the input bits per output bit.
    assert all(20 <= m.bit_count() <= 40 for m in masks)

    # And the solved map must generalise to payloads it never saw.
    for _ in range(500):
        payload = bytes(rng.randrange(256) for _ in range(7))
        row = int.from_bytes(payload, "big")
        predicted = 0
        for bit in range(8):
            parity = ((row & masks[bit]).bit_count() & 1) ^ constants[bit]
            predicted = (predicted << 1) | parity
        assert predicted == crc8(payload, 0x2F, 0xFF, 0xFF)


def test_gf2_solver_reports_inconsistency():
    # y = 1 and y = 0 for the same input has no affine solution.
    assert solve_affine([0b01, 0b01], [1, 0], 2) is None


def test_checksum_not_invented_for_messages_without_one(log):
    for key in ((0, 0x0AA), (0, 0x4D0)):
        stream = log.streams[key]
        stats = bit_stats(stream)
        assert crack_checksum(stream, stats, find_counter(stream, stats)) is None


# --------------------------------------------------------------------------
# Multiplexing
# --------------------------------------------------------------------------


def test_multiplexer_found_only_where_one_exists(log, truth, analyses):
    for key, analysis in analyses.items():
        if key not in truth:
            continue
        is_multiplexed = any(s["kind"] == "mux" for s in truth[key]["signals"])
        if is_multiplexed:
            assert analysis.multiplex is not None, f"missed mux on 0x{key[1]:X}"
            assert (analysis.multiplex.start, analysis.multiplex.length) == (0, 8)
            assert len(analysis.multiplex.groups) == 3
        else:
            assert analysis.multiplex is None, (
                f"false mux on 0x{key[1]:X}: {analysis.multiplex}"
            )


def test_correlated_bytes_are_not_mistaken_for_a_multiplexer():
    """Regression: found on a real Kia Soul EV capture.

    Three bytes that change together as a unit, with one dominant value and a
    long tail of rare ones. Holding any one of them fixed makes the other two
    look predictable, which is exactly the churn reduction a multiplexer causes
    — so gain alone reported a 12-mode multiplexer on three separate messages.
    What tells them apart is the shape of the distribution: a real selector
    cycles evenly, this does not.
    """
    import random

    from autodistill_can.frame import CanFrame, MessageStream

    rng = random.Random(3)
    stream = MessageStream(bus=0, addr=0x073)
    for i in range(2000):
        if rng.random() < 0.8:
            tail = b"\x2d\x01\x31"  # the dominant value, most of the time
        else:
            tail = bytes(rng.randrange(256) for _ in range(3))
        stream.add(
            CanFrame(t=i * 0.02, addr=0x073, data=b"\x05\xcc\x00\x00\x00" + tail)
        )

    stats = bit_stats(stream)
    assert find_multiplexer(stream, stats) is None


def test_multiplexer_selector_does_not_straddle_a_byte_boundary(log, analyses):
    """Regression: found on a real Opel Corsa capture.

    Byte-wide selectors were being proposed at nibble offsets, giving
    implausible answers like "the mode field is bits 4-11". No ECU splits a mode
    field across two bytes; those candidates only fit because a span overlapping
    two real fields can reduce churn statistics by coincidence.
    """
    for analysis in analyses.values():
        mux = analysis.multiplex
        if mux is None:
            continue
        if mux.length == 8:
            assert mux.start % 8 == 0, (
                f"0x{analysis.addr:X}: byte-wide selector at bit {mux.start} "
                "straddles a byte boundary"
            )
        else:
            assert mux.start // 8 == (mux.end - 1) // 8, (
                f"0x{analysis.addr:X}: selector spans two bytes"
            )


def test_counter_is_not_mistaken_for_a_multiplexer(log):
    # Conditioning on a counter partitions the frames tidily but explains
    # nothing; excluding known fields is what keeps this from firing.
    stream = log.streams[(0, 0x1A6)]
    stats = bit_stats(stream)
    counter = find_counter(stream, stats)
    assert counter is not None
    assert find_multiplexer(
        stream, stats, exclude=[(counter.start, counter.length), (0, 8)]
    ) is None


# --------------------------------------------------------------------------
# Signal layout
# --------------------------------------------------------------------------


def _recoverable(truth_message, stats):
    """Ground-truth signals whose bits actually move during the capture.

    A field that never changes leaves no trace, so counting it as a miss would
    measure the drive rather than the algorithm.
    """
    for signal in truth_message["signals"]:
        if signal["kind"] in ("counter", "checksum"):
            continue
        if any(
            stats.flips[b]
            for b in range(signal["start"], signal["start"] + signal["length"])
        ):
            yield signal


def test_signal_layout_recovery_rate(log, truth, analyses):
    """Overall recovery, as a regression baseline.

    Exact means position, width, byte order and signedness all correct.
    """
    exact = boundary = total = 0
    for key, analysis in analyses.items():
        if key not in truth:
            continue
        found = {
            (dbc_start(s.start, s.big_endian), s.length): s
            for s in analysis.signals
        }
        starts = {dbc_start(s.start, s.big_endian) for s in analysis.signals}
        for expected in _recoverable(truth[key], analysis.stats):
            total += 1
            if truth_dbc_start(expected) in starts:
                boundary += 1
            signal = found.get((truth_dbc_start(expected), expected["length"]))
            if signal is None:
                continue
            if expected["kind"] == "constant" or (
                signal.big_endian == expected.get("big_endian", True)
                and signal.signed == bool(expected.get("signed", False))
            ):
                exact += 1

    # Measured baselines, not targets. They exist so a change that quietly
    # makes recovery worse fails loudly. "Exact" is demanding: position, width,
    # byte order and signedness must all be right, and a field one bit too wide
    # counts as a miss.
    assert total >= 30, "the fixture should offer plenty of recoverable signals"
    assert exact / total >= 0.58, f"exact recovery regressed: {exact}/{total}"
    assert boundary / total >= 0.70, (
        f"boundary detection regressed: {boundary}/{total}"
    )


def test_wheel_speeds_recovered_exactly(analyses):
    # Four identical 16-bit fields with nothing to separate them but their
    # flip-rate ramps: the cleanest test that segmentation works.
    analysis = next(a for k, a in analyses.items() if k[1] == 0x0AA)
    layout = [(s.start, s.length, s.big_endian, s.signed) for s in analysis.signals]
    assert layout == [
        (0, 16, True, False),
        (16, 16, True, False),
        (32, 16, True, False),
        (48, 16, True, False),
    ]


def test_signed_fields_are_detected_as_signed(analyses):
    # Steering angle and rate both swing through zero; read as unsigned they
    # would appear to leap between 0 and 65535.
    analysis = next(a for k, a in analyses.items() if k[1] == 0x025)
    for start in (0, 16):
        signal = analysis.signal_at(start)
        assert signal is not None
        assert signal.length == 16
        assert signal.signed, f"field at bit {start} should be signed"


def test_little_endian_field_is_detected(analyses):
    # 0x1C4's engine torque is Intel-ordered; a flip-rate scan alone sees its
    # ramp restart at the byte boundary and splits it in two.
    analysis = next(a for k, a in analyses.items() if k[1] == 0x1C4)
    signal = analysis.signal_at(0)
    assert signal is not None
    assert signal.length == 16
    assert not signal.big_endian


def test_signals_tile_the_payload_without_gaps_or_overlaps(analyses):
    # Compared as bit *sets*, because a message's signals may be Motorola or
    # Intel and the two number their start bits differently.
    for key, analysis in analyses.items():
        if not analysis.signals:
            continue
        seen: set[int] = set()
        for signal in analysis.signals:
            bits = set(signal.payload_bits(analysis.stats.nbits))
            overlap = seen & bits
            assert not overlap, (
                f"0x{key[1]:X}: {signal} overlaps at bits {sorted(overlap)[:4]}"
            )
            seen |= bits
        missing = set(range(analysis.stats.nbits)) - seen
        assert not missing, f"0x{key[1]:X}: bits {sorted(missing)[:8]} unexplained"


def test_static_message_is_reported_as_unresolvable(analyses):
    # 0x4D0 barely changes; the report must say so rather than invent fields.
    analysis = next(a for k, a in analyses.items() if k[1] == 0x4D0)
    assert any("never changed" in note for note in analysis.notes)


def test_event_driven_message_is_recognised(analyses):
    analysis = next(a for k, a in analyses.items() if k[1] == 0x5A0)
    assert analysis.is_event_driven
    assert any("event-driven" in note for note in analysis.notes)


def test_extract_signals_handles_an_empty_stream():
    from autodistill_can.frame import MessageStream

    stream = MessageStream(bus=0, addr=0x100)
    assert extract_signals(stream, bit_stats(stream)) == []


# --------------------------------------------------------------------------
# Byte order
# --------------------------------------------------------------------------


def test_byte_order_is_decided_for_the_whole_capture(log):
    """The synthetic car is predominantly Motorola, so the capture should say so.

    Byte order is a property of the car rather than of one message: an OEM picks
    one for a platform and every ECU follows. Most messages cannot decide for
    themselves — anything built from sub-byte fields reads identically either
    way — so pooling the evidence is what lets the few messages with wide fields
    settle it for the rest.
    """
    from autodistill_can.analysis import detect_byte_order

    # The synthetic car deliberately carries both orders — Motorola messages
    # plus one wholly Intel one — so the pooled evidence is genuinely mixed and
    # the right answer is to decline and let each message decide. Real captures
    # come from one car and clear the margin easily.
    assert detect_byte_order(log.sorted_streams()) is None


def test_intel_car_is_detected_as_intel():
    """A capture whose messages are Intel-ordered must be recognised as such.

    Built here rather than taken from the fixture because the synthetic car is
    Motorola; Hyundai and Kia, two of openpilot's biggest platforms, are Intel
    throughout.
    """
    import math

    from autodistill_can.analysis import detect_byte_order
    from autodistill_can.bitpack import from_signed, pack_le_dbc
    from autodistill_can.frame import CanFrame, CanLog

    frames = []
    for i in range(1500):
        t = i * 0.01
        for addr, phase in ((0x100, 0.0), (0x200, 1.7)):
            buf = bytearray(8)
            # Only a wide field that sweeps its range carries evidence about
            # byte order; narrow or barely-moving ones read the same either way.
            pack_le_dbc(buf, 0, 16, from_signed(
                round(30000 * math.sin(t / 1.7 + phase)), 16))
            pack_le_dbc(buf, 16, 12, round(2048 + 2000 * math.sin(t / 1.1 + phase)))
            frames.append(CanFrame(t=t, addr=addr, data=bytes(buf)))

    assert detect_byte_order(CanLog.from_frames(frames).sorted_streams()) is True


def test_unaligned_intel_field_is_recovered():
    """A 12-bit Intel field straddling a byte boundary must come back exactly.

    These cannot be expressed as a contiguous span in MSB-first numbering, so
    they were previously invisible — and they are not a curiosity: Hyundai's
    vehicle speed and longitudinal acceleration are both laid out this way.
    """
    import math

    from autodistill_can.analysis import analyse_log
    from autodistill_can.bitpack import pack_le_dbc
    from autodistill_can.frame import CanFrame, CanLog

    # The signals must actually sweep their range during the capture. A field
    # whose high byte barely moves cannot be told from two separate bytes by
    # anything, and that is a property of the drive rather than of the analysis.
    frames = []
    for i in range(3000):
        t = i * 0.01
        buf = bytearray(8)
        pack_le_dbc(buf, 0, 16, round(32000 + 31000 * math.sin(t / 1.7)))
        pack_le_dbc(buf, 16, 12, round(2048 + 2000 * math.sin(t / 1.1)))
        pack_le_dbc(buf, 28, 12, round(2048 + 2000 * math.cos(t / 0.9)))
        frames.append(CanFrame(t=t, addr=0x300, data=bytes(buf)))

    analysis = analyse_log(CanLog.from_frames(frames))[0]
    layout = {
        (s.start, s.length) for s in analysis.signals if not s.big_endian
    }
    assert (0, 16) in layout, f"aligned Intel field missed: {analysis.signals}"
    # The 12-bit field spanning byte 2 and the low half of byte 3 is the case
    # that a contiguous MSB-first model cannot even express. Recovering one of
    # the two is the capability being locked in here; the second (bits 28-39)
    # is still split at the byte boundary, which is a known limitation rather
    # than an accident.
    assert (16, 12) in layout, f"unaligned Intel field missed: {analysis.signals}"


# --------------------------------------------------------------------------
# CAN FD: 64-byte payloads and the 16-bit checksums that come with them
# --------------------------------------------------------------------------


from autodistill_can.analysis.message import analyse_message as _analyse


def _canfd_stream(crc_bits: int, n: int = 250):
    """A 64-byte message with smooth signals, a counter, and a real CRC."""
    import math

    from autodistill_can.algos import crc8, crc16
    from autodistill_can.frame import CanFrame, CanLog

    frames = []
    for i in range(n):
        t = i * 0.01
        body = bytearray(64)
        body[0:2] = int(3000 + 2500 * math.sin(t / 7)).to_bytes(2, "big")
        body[2:4] = int(30000 + 8000 * math.sin(t / 3)).to_bytes(2, "big")
        body[4:6] = int(1500 + 300 * math.sin(t / 11)).to_bytes(2, "big")
        for b in range(6, 60):
            body[b] = (b * 7) & 0xFF
        body[60] = i & 0x0F
        width = crc_bits // 8
        payload = bytes(body[: 64 - width])
        crc = crc16(payload) if crc_bits == 16 else crc8(payload, 0x2F, 0xFF, 0xFF)
        frames.append(CanFrame(t, 0x2A0, payload + crc.to_bytes(width, "big"), 0))
    return CanLog.from_frames(frames).streams[(0, 0x2A0)]


def test_a_16_bit_crc_on_a_can_fd_payload_is_recovered():
    """CAN FD raised payloads to 64 bytes, and checksums to 16 bits with them.

    Eight bits of protection over sixty-four bytes is thin, so modern
    platforms -- Hyundai's CAN FD traffic among them -- use 16-bit CRCs. A
    solver that only ever looks at 8- and 4-bit fields finds nothing on those
    messages, which means openpilot can read the car but never transmit to it.
    """
    analysis = _analyse(_canfd_stream(16))
    assert analysis.checksum is not None
    solution = analysis.checksum
    assert solution.length == 16
    assert solution.start == 62 * 8  # the last two bytes
    assert solution.accuracy == 1.0
    assert solution.n_mismatches == 0


def test_the_16_bit_solution_generates_working_code():
    import io

    from autodistill_can.emit.openpilot import write_checksum_function
    from autodistill_can.frame import extract_be

    stream = _canfd_stream(16)
    analysis = _analyse(stream)
    buffer = io.StringIO()
    assert write_checksum_function(analysis, buffer)
    namespace: dict = {}
    exec(buffer.getvalue(), namespace)
    function = namespace[f"checksum_{analysis.addr:03x}"]

    solution = analysis.checksum
    assert solution is not None
    for payload in stream.payloads:
        expected = extract_be(payload, solution.start, solution.length)
        assert function(analysis.addr, payload) == expected


def test_an_8_bit_crc_is_still_preferred_where_that_is_what_the_car_uses():
    # Offering 16-bit positions must not make the solver start seeing them in
    # messages that do not have one.
    analysis = _analyse(_canfd_stream(8))
    assert analysis.checksum is not None
    assert analysis.checksum.length == 8


def test_short_messages_are_not_offered_16_bit_positions(analyses):
    # A classic 8-byte message never carries a 16-bit CRC, and offering the
    # position would only be two more chances at a false positive.
    for analysis in analyses.values():
        if analysis.length <= 8 and analysis.checksum is not None:
            assert analysis.checksum.length in (4, 8)


def test_a_named_16_bit_polynomial_is_identified():
    """The polynomial is named, not just the map solved.

    Pinning an N-bit map down needs more than N independent observations, so
    this uses a 16-byte payload (112 input bits) and 250 frames. A 64-byte
    message needs 500-odd frames before the solve stops being underdetermined
    -- which is a fact about the capture, not the solver, and is what
    `underdetermined` reports.
    """
    import random

    from autodistill_can.algos import crc16
    from autodistill_can.frame import CanFrame, CanLog

    random.seed(11)
    frames = []
    for i in range(250):
        body = bytearray(random.getrandbits(8) for _ in range(14))
        crc = crc16(bytes(body), 0x1021, 0x0000, 0x0000)
        frames.append(CanFrame(i * 0.01, 0x2A0, bytes(body) + crc.to_bytes(2, "big"), 0))
    stream = CanLog.from_frames(frames).streams[(0, 0x2A0)]
    solution = _analyse(stream).checksum
    assert solution is not None
    assert solution.length == 16
    assert solution.crc_name == "CRC-16/XMODEM"
    assert not solution.underdetermined


def test_parallel_and_sequential_analysis_agree(log):
    """The worker pool must be an optimisation, never a change of answer."""
    from autodistill_can.analysis.message import analyse_log

    parallel = analyse_log(log, min_frames=40, workers=4)
    sequential = analyse_log(log, min_frames=40, workers=1)
    assert len(parallel) == len(sequential)
    for a, b in zip(parallel, sequential):
        assert a.key == b.key
        assert [str(s) for s in a.signals] == [str(s) for s in b.signals]
        assert (a.counter is None) == (b.counter is None)
        assert (a.checksum is None) == (b.checksum is None)
        if a.checksum and b.checksum:
            assert a.checksum.start == b.checksum.start
            assert a.checksum.masks == b.checksum.masks


def test_analysis_survives_an_unimportable_main(log, monkeypatch):
    """`python - <<EOF` sets `__main__.__file__` to the string "<stdin>".

    Worker processes then try to import that path, the pool dies mid-start,
    and a traceback appears from the child even though the parent could carry
    on. Shell heredocs are how a lot of glue code calls a library, so this has
    to degrade to the sequential path rather than look like a crash.
    """
    import sys

    from autodistill_can.analysis.message import _can_fork_workers, analyse_log

    main = sys.modules["__main__"]
    monkeypatch.setattr(main, "__file__", "<stdin>", raising=False)
    assert not _can_fork_workers()
    assert len(analyse_log(log, min_frames=40)) > 1


def test_a_real_main_still_uses_workers(log, monkeypatch):
    import sys

    from autodistill_can.analysis.message import _can_fork_workers

    main = sys.modules["__main__"]
    monkeypatch.setattr(main, "__file__", __file__, raising=False)
    assert _can_fork_workers()
    # `python -c` sets no __file__ at all, which is handled fine.
    monkeypatch.delattr(main, "__file__", raising=False)
    assert _can_fork_workers()
