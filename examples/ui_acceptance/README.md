# Real Toyota RAV4 UI acceptance test

This kit exercises the browser workflow with a real one-minute Toyota RAV4
route and enough pinned human facts to generate a complete lateral and
longitudinal port. It is designed for hand comparison with the existing
official Toyota port, not for installation in a car.

The capture is comma.ai's public comma2k19 `Example_1`, segment 40. The builder
uses the exact historical cereal schema to preserve each CAN frame's real DLC.
It separates received traffic from the real 100 Hz `STEERING_LKA` and 33⅓ Hz
`ACC_CONTROL` frames logged as sent by openpilot. Nothing in the CAN payloads
is synthesized.

## Prepare the files

From the repository root:

```console
$ python -m pip install -e '.[rlog]'
$ python examples/ui_acceptance/prepare.py
```

The first run downloads immutable, commit-pinned source files. Later runs use
the files already in `prepared/`. A successful run prints these counts:

```text
rav4-main.log       98800 received CAN frames
rav4-control.log    8000 logged openpilot command frames
rav4-complete.log   106800 combined UI-ready frames
rav4-reference.csv  4974 synchronized rows
```

The UI inputs are all in `examples/ui_acceptance/prepared/`:

| File | Where to use it | What it contains |
|---|---|---|
| `rav4-complete.log` | CAN capture | All real received traffic plus the real logged 0x2E4 and 0x343 commands; this is the foolproof one-file UI input |
| `rav4-main.log` | Optional provenance | Received RAV4 traffic before command frames are combined |
| `rav4-control.log` | Optional provenance | Logged openpilot command frames before they are combined |
| `rav4-reference.csv` | Reference data | Vehicle speed, steering, four wheel speeds, and independent pose speed |
| `vehicle-info.json` | Load a vehicle-info file | Official signal layouts, CarState bindings, specs, control facts, limits, tuning, and Toyota safety selection |
| `official-opendbc/` | Hand-comparison target | Toyota port and generated DBC pinned to commit `aedd88e` |
| `SOURCE.txt` | Provenance | Every source commit and route identifier |

`prepared/` is ignored by Git because it is reproducible and contains about
16 MiB of source data.

## Run the browser test

Start the local UI:

```console
$ autodistill-can ui
```

Then perform these steps exactly:

1. Create a new project and upload `prepared/rav4-complete.log` as the CAN log.
2. Choose `prepared/rav4-reference.csv` under **Reference data**.
3. Open **What you already know**, click **Choose JSON**, and select
   `prepared/vehicle-info.json`. The status must say that it was loaded and
   saved. You can inspect or edit every imported row afterward.
4. Enter these remaining UI-only values:

   - Vehicle name: `Toyota RAV4 2016-18 UI Acceptance`
   - Brand package: `toyota_ui_acceptance`
   - Main CAN bus: `Bus 0`
   - Strict file parsing: enabled

   Leave **Minimum frames per message** at its default `40`. The route contains
   only eight event-driven `BLINKERS_STATE` frames; the explicit official JSON
   definition now handles such below-threshold messages without inventing
   statistical evidence.
5. Click **Decode this drive**. Expect about 1–2 minutes on a typical laptop.
   The analysis should report 106,800 frames, 132 address/bus streams, and two
   physical buses.
6. Review **Finish the port facts**. Every mandatory read, lateral, and
   longitudinal requirement should already be filled by the imported JSON.
7. Click **Generate port**, wait for completion, then download the port ZIP.

## Expected result

The result is correct when the port screen and `port_status.json` show:

- mode `control`;
- lateral control `true` and longitudinal control `true`;
- read coverage `16/16`;
- lateral coverage `25/25`;
- longitudinal coverage `29/29`;
- validation `ok`, with 16 checked CarState bindings;
- no validation errors (notes for doors, brake, seatbelt, and blinkers not
  being exercised are expected);
- `safe_for_control: false`.

That last value is mandatory. The fixture proves generation and replay, not
safety on a physical car. AutoDistill intentionally never certifies its own
output for driving.

Run the bundled checker against the downloaded ZIP or an extracted directory:

```console
$ python examples/ui_acceptance/verify.py ~/Downloads/port.zip
```

It checks the manifest, compiles every generated Python file, confirms both
controller paths, and compares 33 key DBC signal layouts with the pinned
official Toyota DBC. Its final line should begin with `PASS`.

## Open the generated DBC in Cabana

Cabana requires a CAN stream even when you only want to inspect a DBC. In
Cabana v1.1.2's **Open stream** dialog:

1. Select the **candump** tab.
2. In the upper **candump file(s)** field, select
   `prepared/rav4-complete.log`.
3. In the lower **dbc File** field, select
   `toyota_ui_acceptance_generated.dbc` from the downloaded port.
4. Click **Open**.

Do not select the `.dbc` in the upper field. Cabana reports "Could not parse
any CAN frames" when it is asked to treat a DBC as a candump stream; that
message is about the upper file, not the DBC parser. The prepared log contains
106,800 Cabana-compatible candump frames over 59.99 seconds.

The initially loaded DBC is the bus 0 definition. To inspect camera bus 1,
open **File → Manage DBC Files → Bus 1 → Open DBC File** and select
`toyota_ui_acceptance_generated_bus1.dbc`.

## What to compare by hand

Use the generated files on the left and `prepared/official-opendbc/` on the
right:

| Generated | Official | High-value checks |
|---|---|---|
| `*_generated.dbc` | `toyota_new_mc_pt_generated.dbc` | IDs 0x25, 0xAA, 0x1D2, 0x1D3, 0x224, 0x260, 0x2E4, 0x343, 0x3BC, 0x614, and 0x620 |
| `carstate.py` | `carstate.py` | speed units, signed steering values, gas/brake sense, gear values, cruise, doors, seatbelt, and `TURN_SIGNALS == 1/2` |
| `carcontroller.py` | `carcontroller.py` and `toyotacan.py` | 0x2E4 at 100 Hz, six-bit counter, torque/request fields, 0x343 at every third control frame, and Toyota checksums |
| `values.py` | `values.py` | 1655.61175 kg, 2.65 m wheelbase, 16.88 steering ratio, 1500 torque limit |
| `interface.py` | `interface.py` | Toyota safety model, parameter 329, 0.12 s actuator delay, torque tuning |

Expected differences are important rather than failures:

- The generated DBC also describes anonymous traffic actually observed in the
  route; the official DBC is a curated manufacturer-wide definition.
- The generic generated `CarState` reads one correctly scaled wheel speed and
  the base steering angle. The official Toyota code averages four wheels, adds
  the fractional angle, and maintains a filtered precise-angle offset.
- The generated longitudinal message reproduces the constant fields observed
  in this 2018 route. Current official Toyota code chooses lead, standstill,
  cancel, distance, and fault-avoidance fields dynamically.
- This segment has no UDS firmware probe, so the generated port fingerprints
  from observed CAN IDs and lengths. The official port supports many Toyota
  firmware versions collected across vehicles.
- The generated package remains an unreviewed acceptance artifact even though
  all control code paths are present. Never install this fixture in a car.

These differences make the test useful: it confirms the tool produces a
coherent, mechanically complete bootstrap while leaving visible exactly where
the hand-maintained official port contains platform expertise beyond a single
route.
