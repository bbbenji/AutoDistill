# AutoDistill: beginner setup and step-by-step guide

> [!WARNING]
> **Disclaimer:** This project is 100% vibed. Review and verify all code, analyses, and generated outputs carefully before use.

This is the plain-language guide. The main [README](README.md) explains the
algorithms and technical details.

## What this program does

A modern car has several small computers. They send short numbered messages to
each other over wires called CAN buses. The messages contain values such as
vehicle speed, steering-wheel angle, pedal state and cruise-control state, but
the message usually does not say what each number means.

AutoDistill records those messages and looks for patterns. It then creates the
starting files needed to add that car to openpilot's vehicle database,
`opendbc`.

In simple terms, it:

1. Records or reads the car's CAN traffic.
2. Groups messages by bus and message number.
3. watches which bits change together to find values inside each message.
4. Compares those values with an optional GPS or OBD reference recording to
   discover names such as speed or steering angle.
5. Finds repeating counters and checksums.
6. Reads ECU firmware identities when you explicitly run the diagnostic probe.
7. Writes DBC files and a Python openpilot port.
8. Installs that generated port into an `opendbc` checkout.

The generated port can identify and **read** the car. It is deliberately unable
to steer, accelerate or brake the car. It remains `dashcamOnly`, uses
openpilot's `noOutput` safety mode, and reports `safe_for_control: false`.

## What it cannot learn automatically

Watching traffic cannot prove:

- how much steering torque is safe;
- how quickly a command may change;
- which wiring harness is safe and correct;
- the car's exact mass, wheelbase or steering ratio;
- which ECU type owns every firmware address;
- how to implement and test the panda safety model;
- secret SecOC authentication keys, if the car uses them.

A knowledgeable person must establish those parts. You can then hand them to
AutoDistill in a `vehicle-info.json` and it will build the complete port around
them — see [Complete the port](#complete-the-port). What it will not do is guess
any of them for you, or ever mark the result safe to drive. Do not remove the
read-only protections just to make an unfinished port engage.

## What you need

### Required

- A laptop running Linux. Ubuntu 22.04 or 24.04 is the simplest choice.
- Python 3.10 or newer.
- Git.
- This AutoDistill repository.
- A car that you own or have permission to examine.
- A safe way to connect to the correct CAN buses.

For eventual openpilot control, the vehicle normally needs factory electronic
lane keeping (LKAS) and adaptive cruise control (ACC). AutoDistill can still
decode other cars, but software cannot add steering or braking hardware the car
does not have.

### CAN hardware: choose one

**Recommended: a comma panda**

- Get a current panda from the
  [official comma shop](https://comma.ai/shop/panda).
- You also need the cable or breakout that fits your connection point. An
  OBD-C cable is convenient for the diagnostic port, but many cars expose only
  one bus there. A car-specific or developer harness may be needed to see the
  ADAS/camera bus.
- A red panda supports CAN FD on all of its buses.
- AutoDistill can read all visible panda buses with the source name `panda:`.

**Alternative: a SocketCAN-compatible adaptor**

- Use a Linux-supported USB or SPI CAN adaptor and the correct isolated cable
  or breakout.
- It appears in Linux as an interface such as `can0`.
- One interface normally records one physical bus. Multiple buses require
  multiple configured interfaces or suitable multi-channel hardware.

Do not guess at vehicle connectors or pinouts. A wrong connection can damage
the adaptor or the car. Use the vehicle wiring diagram and a fused, isolated
interface.

### Where to plug it in

**The OBD-II port** is the 16-pin trapezoidal socket under the dashboard on the
driver's side, usually within arm's reach of the steering column. On a CAN car:

| pin     | signal                |
| ------- | --------------------- |
| 6       | CAN-High              |
| 14      | CAN-Low               |
| 16      | permanent +12V        |
| 4 and 5 | chassis/signal ground |

**The catch, and it is a big one.** On most cars built since roughly the
mid-2010s, that port sits behind a gateway module that only forwards diagnostic
traffic. You will see replies to your own queries and almost nothing of the car
talking to itself. The symptom is unmistakable: a capture containing a handful
of `0x7xx` addresses and little else, where a real vehicle bus gives you dozens
of addresses arriving at 10–100 Hz.

When that happens, the bus you want is the one the **forward-facing ADAS
camera** is on, behind the windscreen near the mirror. That is where
openpilot's car harnesses connect, and a harness for your model is by far the
safest way to reach it — it plugs in between the camera and the car without
cutting or piercing anything. Check
[comma's harness list](https://comma.ai/shop) and opendbc for your model before
considering anything more invasive.

### Two things that catch almost everyone out

**Do not enable termination.** A CAN bus already has a 120Ω resistor at each
end, and you are joining in the middle as an extra listener. If your adaptor
has a termination switch or jumper, leave it off. Enabling it can disturb the
bus you are trying to observe.

A useful check before powering anything up: with the ignition off, measure the
resistance between CAN-High and CAN-Low. A healthy terminated bus reads about
**60Ω** — the two 120Ω resistors in parallel. An open circuit or a few hundred
ohms means you are not on the bus you think you are.

**The bitrate has to match.** 500 kbit/s is correct for almost every powertrain
bus. Comfort and body buses are often 125 kbit/s. A wrong bitrate gives you no
frames at all, or a stream of errors — never partial data, so it is easy to
recognise.

### Check it works before driving anywhere

Record ten seconds while parked with the ignition on:

```console
$ autodistill-can capture socketcan:can0 --duration 10 --output test.log
```

It prints a running frame count. Hundreds or thousands of frames means you are
connected. Zero means you are not, and a fifteen-minute drive will not change
that. Fix the connection, bitrate, or bus first.

With a panda and a car harness, openpilot's convention is that **bus 0** is the
car's own powertrain bus and **bus 2** is the camera side. AutoDistill reports
which buses it saw, and `--bus` selects the one the port is generated for.

### Strongly recommended: an independent reference recording

AutoDistill can find an unknown number in a message without outside help, but it
cannot know that the number means “speed.” A reference recording supplies that
meaning.

Useful sources include:

- a phone GPS export for speed;
- an OBD-II logger for speed, RPM, throttle and similar values;
- a time-stamped measurement or test log.

Export it as CSV. Its timestamps must use the same clock as the CAN log, or both
recordings must start at zero at the same moment. Put units in the column names:

```csv
time,speed [km/h],engine_rpm [rpm],steer_angle [deg]
0.00,0.0,781,0.0
0.10,1.2,814,-2.1
0.20,2.7,903,-4.0
```

Each useful reference value must change through at least three distinct values.
Start the CAN and reference recordings together. If their clocks do not line
up, correlation will find the wrong signal or nothing at all.

### Needed for the final generated package

Get the current official
[commaai/opendbc repository](https://github.com/commaai/opendbc). AutoDistill
installs its output there. The official project explains the remaining human
porting and safety work.

## Part 1: install AutoDistill

The following commands are for Ubuntu:

```console
$ sudo apt update
$ sudo apt install -y git python3 python3-venv python3-pip can-utils libusb-1.0-0
```

Open a terminal in this repository, then create an isolated Python environment:

```console
$ cd /path/to/AutoDistill
$ python3 -m venv .venv
$ source .venv/bin/activate
$ python -m pip install --upgrade pip
$ python -m pip install -e '.[panda]'
$ autodistill-can --version
```

The last command should print `AutoDistill CAN 1.4.0` or a newer version.

Run `source .venv/bin/activate` again whenever you open a new terminal. If the
terminal says `autodistill-can: command not found`, the environment is probably not
active.

### Recommended: open the guided web app

```console
$ autodistill-can ui
```

Your browser will open to `http://127.0.0.1:8765`.

**If this is your first port, leave "Guide me" switched on** — it is the button
at the top right, and it is on by default. It puts a panel in the sidebar that
tells you the single next thing to do, why it matters, and takes you to the
right control when you click it. It reads the project's actual state, so it
stays correct if you do things out of order, come back tomorrow, or switch it
on halfway through. Turn it off at any time; nothing else changes.

The four steps it walks you through:

1. Try the fake-car demo, upload a CAN log or capture from connected hardware.
2. Enter the vehicle name and optionally attach synchronized reference data.
   Open **Facts AutoDistill cannot observe** to enter measured dimensions,
   known ECU types, known signal definitions, sources, and engineering notes.
   Everything on this step is saved as you type; closing the tab loses nothing.
3. Run the decoder and review what was automatic versus what needs a person.
4. Generate and download the guarded read-only port.

![AutoDistill guided web app](docs/screenshots/01_get_can_data.png)

The server is local-only and does not send driving logs to a cloud service.
Projects are stored in `~/.autodistill/projects`. Leave the terminal open while
using the app; press `Ctrl+C` there when finished.

Each vehicle gets its own project. Click the project name in the top left to
switch between them, start another one, or delete one. Deleting asks first,
then erases that project's capture, analysis, and generated port from disk — a
drive you cannot record again is worth keeping a copy of somewhere else.

### Panda USB permission on Linux

If a connected panda is reported as missing or permission is denied, install
the current udev rules from the
[official panda README](https://github.com/commaai/panda#usage), reload the
rules, unplug the panda and reconnect it. The official page is the source of
truth because USB identifiers can change with new hardware.

## Part 2: prove the software works without a car

The easiest method is to run `autodistill-can ui`, choose **Try a guided
demo**, and follow the on-screen steps.

The equivalent command-line method is below.

Do this first. It makes a fake car recording and runs the complete safe
read-only pipeline:

```console
$ mkdir -p ~/autodistill-can-demo
$ cd ~/autodistill-can-demo
$ autodistill-can synth --duration 60 --output drive.log \
    --reference reference.csv --truth truth.json
$ autodistill-can port drive.log --reference reference.csv \
    --brand demo_car --name "Demo Car" --output demo_car_port
$ mkdir -p ~/src
$ git clone https://github.com/commaai/opendbc.git ~/src/opendbc
$ autodistill-can install demo_car_port ~/src/opendbc
```

Now check that the generated interface can be created:

```console
$ cd ~/src/opendbc
$ python -c "from opendbc.car.demo_car.values import CAR; from opendbc.car.demo_car.interface import CarInterface; c=next(iter(CAR)); p=CarInterface.get_non_essential_params(c); assert p.dashcamOnly; print(c)"
```

If this prints `DEMO_CAR` without an exception, the generator and installer are
working.

### See it produce a complete port

The port above is deliberately read-only, which is the honest result of a
capture with no facts attached. To see the same pipeline produce a port whose
generated controller really builds and sends a steering and an acceleration
command -- still not something to test on a car, since nothing about a
synthetic capture changes that -- ask `synth` to analyse its own capture and
write a complete `vehicle-info.json` alongside it:

```console
$ autodistill-can synth --duration 60 --output drive.log \
    --reference reference.csv --vehicle-info-out vehicle-info.json
$ autodistill-can requirements drive.log --reference reference.csv \
    --vehicle-info vehicle-info.json
$ autodistill-can port drive.log --reference reference.csv \
    --vehicle-info vehicle-info.json \
    --brand complete_demo --name "Complete Demo Car" --output complete_demo_port
```

`requirements` reports 16/16, 25/25 and 29/29: complete for reading, steering
and driving. `complete_demo_port/port_status.json` still says
`safe_for_control: false`, because nothing -- not this, not anything -- ever
changes that. The facts themselves are picked from whichever recovered
messages happened to carry a working counter and checksum, not from anything
resembling a real car's actual steering or braking system, so treat this as
proof the pipeline works end to end, never as an example `vehicle-info.json`
for a real port.

The web UI's own **Try the demo** option does this automatically, and shows
the attached facts under **Facts AutoDistill cannot observe** so you can see
what a complete file looks like.

## Part 3: record the real car

Park safely before connecting hardware. Confirm the wiring and bus voltage
first. Do not operate a laptop while driving; use a passenger/helper or arrange
logging before moving.

### Option A: capture directly from a panda

Connect the panda to the correct vehicle cable and to the laptop by USB. Turn
the ignition on, activate the Python environment, and run:

```console
$ mkdir -p ~/autodistill-can-work/mycar
$ cd ~/autodistill-can-work/mycar
$ autodistill-can capture panda: --duration 900 --output drive.log
```

This records 15 minutes from every bus visible to the panda. The capture source
forces panda's `SAFETY_SILENT` mode and has no CAN send path.

### Option B: capture from SocketCAN

Replace `500000` below if the selected bus uses a different bitrate:

```console
$ sudo ip link set can0 down
$ sudo ip link set can0 type can bitrate 500000 listen-only on
$ sudo ip link set can0 up
$ autodistill-can capture socketcan:can0 --duration 900 --output drive.log
```

Linux `listen-only` mode prevents the adaptor from transmitting or
acknowledging. If the interface has never been created, follow the adaptor
manufacturer's Linux/SocketCAN instructions first.

### What to do during the capture

Only do maneuvers that are safe and legal where you are. Try to make every
useful state change:

- accelerate and slow through a useful speed range;
- turn the wheel both directions;
- press and release the brake and accelerator;
- use both indicators;
- open and close each door while parked;
- select each gear while following the vehicle's normal procedure;
- switch cruise and lane-keeping features through their available states.

A value that never changes during the recording cannot be discovered.

At the same time, record the GPS/OBD reference CSV described above.

## Part 4: collect ECU firmware identities

This is optional but strongly recommended because openpilot commonly identifies
cars from their ECU firmware.

The probe uses a SocketCAN interface and **actively sends read-only diagnostic
requests**. Direct `panda:` capture remains passive and is not used for this
step. The car must be stationary, in park, with permission from its owner.

**Set the car up first:**

- Stationary, in park, parking brake on, nobody in the driver's seat pressing
  anything.
- Ignition on, so the ECUs are awake and will answer. Leaving the engine
  running is fine and avoids flattening the battery during a long sweep.
- On a car you own or are explicitly authorised to work on.

**The hardware is used differently here.** Every other step listens; this one
transmits, so the interface must have transmission enabled. If you set it up
for capture it is in `listen-only` mode and the requests never reach the wire —
which is the single most common reason a probe returns nothing at all.

Unlike the capture step, the OBD-II port is the _right_ place for this even on
a gatewayed car: forwarding diagnostics is exactly what a gateway does.

Configure the correct SocketCAN bus with transmission enabled:

```console
$ sudo ip link set can0 down
$ sudo ip link set can0 type can bitrate 500000 listen-only off
$ sudo ip link set can0 up
$ autodistill-can probe --interface can0 --i-own-this-vehicle \
    --capture firmware.log --output firmware.txt
```

If the manufacturer uses a known non-standard diagnostic address, repeat
`--address`:

```console
$ autodistill-can probe --interface can0 --i-own-this-vehicle \
    --address 0x730 --address 0x760 \
    --capture firmware.log --output firmware.txt
```

No response usually means the ignition is off, the wrong bus or bitrate is
selected, the adaptor is still listen-only, or the ECUs use other addresses.
Do not blindly probe unrelated services; this command deliberately limits
itself to read-only firmware identifiers.

## Part 5: inspect the recording

Create a readable report before generating the port:

```console
$ autodistill-can analyse drive.log --strict --reference reference.csv \
    --firmware-log firmware.log --output report.txt
```

If you skipped the reference or firmware step, omit the corresponding option:

```console
$ autodistill-can analyse drive.log --strict --output report.txt
```

Read `report.txt`. It says what was found, what is uncertain and what should be
recorded again.

### Pass along facts AutoDistill cannot learn

This step is optional. Use it when you measured a vehicle specification, found
an ECU type in a service manual, or independently confirmed what one recovered
field means. Create an empty file:

```console
$ autodistill-can manual-template --output vehicle-info.json
```

Open `vehicle-info.json` in a text editor. A filled example looks like this:

```json
{
  "schema_version": 1,
  "vehicle_specs": {
    "mass_kg": 1845,
    "wheelbase_m": 2.766,
    "steer_ratio": 14.3,
    "docs_package": "All"
  },
  "ecu_types": {
    "0x7E0": "engine",
    "0x730": "eps"
  },
  "signals": [
    {
      "bus": 0,
      "address": "0x123",
      "start_bit": 8,
      "length": 16,
      "name": "VEHICLE_SPEED",
      "unit": "km/h",
      "scale": 0.01,
      "offset": 0,
      "signed": false,
      "byte_order": "big",
      "carstate_target": "ret.vEgoRaw"
    }
  ],
  "engineering": {
    "actuation_notes": ["Workshop manual section 12 describes 0x2E4"],
    "safety_notes": ["Bench validation is still required"],
    "sources": ["Service manual page 418; measured on this car"]
  }
}
```

Usually, use the `bus`, `address`, `start_bit`, and `length` shown for that
field in a JSON analysis report. If an official DBC or measurement proves the
automatic boundary is wrong, explicit length, signedness, and byte order may
safely correct it. AutoDistill will not let a correction overwrite a counter,
checksum, or multiplexer. Do not copy the example numbers above: they are only
an example and probably do not describe your car. AutoDistill also rejects an
unknown message, an invalid ECU type, and an unsafe or misspelled CarState
destination.

Pass the file whenever you analyse or generate:

```console
$ autodistill-can analyse drive.log --reference reference.csv \
    --vehicle-info vehicle-info.json --format json --output report.json
$ autodistill-can port drive.log --reference reference.csv \
    --vehicle-info vehicle-info.json --brand mycar \
    --name "Example Make Model 2024" --output mycar_port
```

The generated port includes both `vehicle_info.json` and a readable
`MANUAL_INPUT.md`. These files clearly say the information came from a person.

### How to obtain each fact

Everything in that file is something a person measured, read, or worked out.
None of it can be inferred from traffic. This section says exactly how to get
each one.

You do not have to keep this page open: `autodistill-can requirements` prints
the same guidance for whichever facts are still missing, and the web app shows
it under **How to find it** on each item.

**A general method for anything on the bus.** Most of these come down to the
same trick: park safely with the engine running, record a short capture while
doing **one** thing repeatedly with a couple of seconds between each — press
the brake five times, say — then run the decoder and look for the field that
changed in step. A bit that toggles exactly five times is the one you want.
Doing one action per capture is what makes this quick; doing five at once
leaves you unable to tell which field belongs to which.

#### Measured vehicle specifications

| fact               | how to get it                                                                                                                                                                                                                                                                                                                                 |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Mass**           | The specifications page of the owner's manual, or the manufacturer's brochure for your exact trim. The door-jamb sticker usually gives _gross vehicle weight_, which is a much larger number and the wrong one. A public weighbridge with a full tank is the direct measurement.                                                              |
| **Wheelbase**      | Published for every car. Also measurable with a tape from front hub centre to rear hub centre; measure both sides and average.                                                                                                                                                                                                                |
| **Steering ratio** | Sometimes published as "14.3:1". To measure: park with the wheels straight on a smooth surface, turn the steering wheel exactly one full turn, and measure how far the road wheel moved with an angle gauge against the wheel face. The ratio is 360 divided by that angle. openpilot refines this while driving, so a close value is enough. |
| **Package label**  | Free text shown in openpilot's supported-car list. Name the trim or option pack that has the lane-keep hardware.                                                                                                                                                                                                                              |

#### Known ECU types

Each line maps a diagnostic _request_ address to the kind of module that answers
on it. The addresses come from the firmware probe in Part 4 — every address that
answered is one to identify. To work out which is which:

- `0x7E0` is the engine ECM on almost every car by OBD-II convention, and
  `0x7E1` is usually the transmission.
- The firmware string often contains a part number you can search, or a
  recognisable abbreviation such as EPS or ABS.
- Check an existing port for the same manufacturer in opendbc. Its
  `FW_VERSIONS` table maps the same addresses, and manufacturers reuse them
  across models and years.
- Manufacturer service documentation lists module diagnostic addresses
  directly.

A wrong guess is worse than leaving it out. Omit any you are unsure of.

#### Known signal definitions

Use these only when you already know a field from a service document or an
independent measurement — a documented scale factor, a name from a leaked DBC,
a value you confirmed with a scan tool. Copy `bus`, `address`, `start_bit` and
`length` from a JSON analysis report; AutoDistill rejects anything that does not
match a field it actually recovered.

#### openpilot CarState bindings

Use the general method above. Per field:

| field                           | what to do                                                                                                                                                                                                                                      |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Vehicle speed**               | Easiest via a reference recording — pass `--reference` and AutoDistill names it and recovers its scale itself. Otherwise hold a steady indicated speed and find the proportional field. Check units: 6234 at 62.3 km/h means 0.01 km/h per bit. |
| **Steering angle**              | Turn the wheel slowly lock to lock while parked. The field sweeping smoothly and symmetrically about a centre value is it. openpilot wants positive to the left; negate the scale if yours is the other way.                                    |
| **Driver steering torque**      | Engine running, wheels straight, push the wheel left and right _without_ letting it turn. The field responding to force rather than movement is this one.                                                                                       |
| **Accelerator / brake pressed** | Blip each pedal a few times. Prefer a dedicated switch bit over a pressure reading — a pressure idles at a non-zero value and needs a threshold.                                                                                                |
| **Gear selector**               | Foot on the brake, move the selector through every position in turn, pausing in each. Note the raw value at each stop, including any manual gate.                                                                                               |
| **Cruise engaged / main on**    | Engage and cancel stock cruise a few times, then press the main switch on and off. "Available" stays high while the system is on; "enabled" follows engagement.                                                                                 |
| **Cruise set speed**            | Engage cruise and step the set speed up and down. The field moving in the same increments is it, in the dash's unit — switch the dash between km/h and mph to find out which.                                                                   |
| **Doors, seatbelt, indicators** | One at a time. Check the _sense_: many cars report seatbelt _latched_, which needs the invert transform.                                                                                                                                        |
| **Blindspot**                   | Only if the car has it. Have someone walk past the rear quarter, or capture on a busy road and look for a bit that pulses as vehicles pass.                                                                                                     |

#### Actuation: the command messages

**This is the part that requires bench work**, and the one worth the most care.
Everything above is observation; this is a message you intend to transmit at a
car.

1. **Check opendbc first.** Command messages are shared across model years and
   whole platforms. A sibling port may already name yours — with a reviewed
   safety model to go with it, which is the harder half.
2. **Otherwise, find it from the camera.** The stock lane-keep camera sends it.
   Capture both the vehicle bus and the camera bus with the camera connected,
   drive with factory lane-keep active so it steers the car, and look for a
   message that only the camera originates and whose payload tracks the
   steering the car applies to itself.
3. **Confirm on a bench**, with the car unable to move, before it is ever sent
   in anger. A wrong checksum or counter is silently discarded; a wrong payload
   is not.

For longitudinal, the same method with the stock adaptive cruise following a
car. Many platforms do longitudinal by spamming the stock cruise buttons
instead, which is simpler and often safer — check what comparable opendbc ports
do before assuming a dedicated message is needed.

#### Limits

Every one of these can be **read off the factory system** rather than guessed.
The stock lane-keep is a working example of what this car accepts.

1. Capture the factory lane-keep working hard — a tight motorway curve is ideal.
2. Decode that capture with the generated DBC.
3. **Max steer command** is the largest magnitude the command signal ever
   reaches. The analysis report lists each signal's observed range, so this is a
   lookup rather than a measurement.
4. **Ramp up / ramp down** are the largest increase and decrease between
   consecutive frames of that signal. The ramp-down limit is usually allowed to
   be larger, because releasing torque is the safe direction.
5. **Driver override torque**: resist the wheel gradually while the stock system
   steers, and note the driver torque at which it backs off.
6. **Actuator delay** is the lag between a command and the steering angle
   responding — cross-correlate the two in a decoded capture. Typically 0.1 to
   0.4 seconds.

Never exceed what the factory system commands. It is the one bound you know the
car tolerates.

#### Tuning

Copy a comparable opendbc platform's values as a starting point; this is normal
and expected. Use `angle` control if the car accepts a steering angle directly
and `torque` otherwise — again, whatever similar platforms do. Torque tuning is
then fitted from logs of lateral acceleration against steering torque over
varied driving.

#### Safety model

openpilot's safety layer is C code in `opendbc/safety/`. It sits below
everything AutoDistill generates and rejects any message outside the limits it
enforces. It is the thing that actually stops a bad command reaching the car.

Look in `opendbc/safety/modes/` for your manufacturer. If a mode exists and
already bounds exactly the messages you intend to send, name it. If it does not,
writing it is the real work of a port: it must reject anything outside the
limits, come with tests, and be reviewed by people who know the platform.
**Naming a model here does not create one**, and a name that does not exist will
simply fail to load.

### Check your bindings before trusting them

Once you have bound some fields, ask AutoDistill whether the numbers make sense:

```console
$ autodistill-can validate drive.log --reference reference.csv \
    --vehicle-info vehicle-info.json
```

It decodes your recording exactly the way the generated port will, and looks at
the results. This is a different question from "does it work" — the port can
compile and load perfectly while being completely wrong:

```
[FAIL] ret.gearShifter
       never reads drive in this capture. openpilot only engages in drive,
       so it never would.
[FAIL] ret.brakePressed
       true in every frame. openpilot would never engage. The binding is
       probably on the wrong bit, or needs invert_bool.
```

Things it catches, all from the recording you already have:

- a speed left in km/h when openpilot wants m/s;
- a speed that goes negative, which means the wrong signedness or byte order;
- a flag that is stuck on, or stuck off;
- a pedal reading 0–100 where openpilot wants 0–1;
- a gear map that never once reads drive.

A clean result is not proof your bindings are right — only that none of them is
provably wrong. It runs automatically when you generate a port, and anything it
finds is written into the port's README and shown in the web app.

### See exactly what is still missing

```console
$ autodistill-can requirements drive.log --reference reference.csv \
    --vehicle-info vehicle-info.json
```

This prints every single thing a finished port needs, marked as found
automatically, supplied by you, or missing — and for each missing one, the exact
text to add to `vehicle-info.json`. It scores three levels separately, because
a port can be finished for one and not the others:

- **read the car** — enough for dashcam and logging.
- **steer the car** — adds a steering controller.
- **drive the car** — adds gas and brake.

The same three bars appear in the web app on the decode step, under **How much
of a port this is**.

### Complete the port

When you have done the bench work — you know which message steers the car, which
signal in it carries torque, and what limits are safe — put that in the same
file:

```json
{
  "carstate": [
    {
      "target": "ret.brakePressed",
      "bus": 0,
      "address": "0x1A0",
      "signal": "BRAKE_PRESSURE",
      "transform": "threshold",
      "threshold": 10
    }
  ],
  "actuation": {
    "lateral": {
      "bus": 0,
      "address": "0x2B0",
      "message": "MSG_2B0",
      "frequency_hz": 100,
      "signals": { "STEER_TORQUE": "apply_torque", "STEER_REQ": "lat_active" },
      "counter_signal": "COUNTER",
      "checksum_signal": "CHECKSUM"
    }
  },
  "limits": {
    "steer_max": 384,
    "steer_delta_up": 3,
    "steer_delta_down": 7,
    "steer_driver_allowance": 50,
    "steer_actuator_delay": 0.1
  },
  "tuning": {
    "lateral": { "kind": "torque", "max_lateral_accel": 2.5, "friction": 0.1 }
  },
  "safety": { "model": "hyundaiCanfd" }
}
```

This is only a fragment. Once every mandatory line in the requirements report
is complete and validation passes, `carcontroller.py` comes out
finished instead of empty: it builds that message at the exact declared rate,
limits how fast the command may change, backs off when you hold the wheel, and
attaches the checksum AutoDistill verified against your own recording. Until
then the controller stays inert behind `noOutput` and `dashcamOnly`.

**Every number above has to come from you.** None of it is in the traffic, and
AutoDistill will not invent any of it. It also refuses a command message that
was not in your capture, or a signal name that is not really in that message.

**This does not make the port safe.** `port_status.json` still says
`safe_for_control: false`, and there is no setting that changes that. The port
is as correct as the facts you gave it, and nobody has driven it yet. You still
have to write the safety model in `opendbc/safety/`, bench-test the messages,
and test on a closed course. See
[What must happen before control](#what-must-happen-before-control).

## Part 6: generate the openpilot port

Choose:

- `--brand`: a short lowercase package name with no spaces, such as
  `my_toyota`;
- `--name`: the human-readable make, model and model year.

Then run:

```console
$ autodistill-can port drive.log --strict --reference reference.csv \
    --firmware-log firmware.log \
    --vehicle-info vehicle-info.json \
    --brand mycar --name "Example Make Model 2024" \
    --output mycar_port
```

Again, omit `--reference` or `--firmware-log` only if that file was not
collected. Omit `--vehicle-info` if you did not create it. The output directory
contains:

- one DBC file for each recorded bus;
- the car fingerprint and ECU firmware values;
- Python files that read the identified signals;
- recovered counter and checksum helpers;
- `port_status.json`, a machine-readable readiness checklist;
- `vehicle_info.json` and `MANUAL_INPUT.md` when human facts were supplied;
- another README containing the TODO list for this specific car.

Check the safety gate:

```console
$ python -c "import json; s=json.load(open('mycar_port/port_status.json')); assert s['safe_for_control'] is False; print(json.dumps(s, indent=2))"
```

Do not use `--force` unless you intend to overwrite an earlier generated file.

## Part 7: install it into opendbc

Clone a clean current checkout if you did not already do so:

```console
$ mkdir -p ~/src
$ git clone https://github.com/commaai/opendbc.git ~/src/opendbc
```

From the directory containing `mycar_port`, run:

```console
$ autodistill-can install mycar_port ~/src/opendbc
```

The installer copies the brand files and DBCs and updates opendbc's central
platform and torque registries. It refuses to overwrite differing files unless
`--force` is explicitly supplied.

Run opendbc's official test entry point:

```console
$ cd ~/src/opendbc
$ ./test.sh
```

Finally, construct the generated interface. Replace `mycar` with the exact
`--brand` used above:

```console
$ python -c "from opendbc.car.mycar.values import CAR; from opendbc.car.mycar.interface import CarInterface; c=next(iter(CAR)); p=CarInterface.get_non_essential_params(c); assert p.dashcamOnly; print(c, p.carName)"
```

## Putting it into an openpilot development checkout

openpilot contains `opendbc` as the `opendbc_repo` submodule. If you are
developing an openpilot fork, install the generated package into that directory:

```console
$ git clone --recurse-submodules https://github.com/commaai/openpilot.git ~/src/openpilot
$ autodistill-can install mycar_port ~/src/openpilot/opendbc_repo
```

Then follow the current
[official openpilot development instructions](https://github.com/commaai/openpilot#to-start-developing-openpilot).
AutoDistill does not publish a fork, build a comma-device installer or make an
unfinished car safe to drive.

## What must happen before control

The generated package is the beginning of a real port, not permission to test
steering on a road. A human port developer must:

1. Validate every named signal against a second independent recording.
2. Identify the remaining signals and ECU types.
3. Enter measured vehicle dimensions.
4. Determine the exact stock actuation messages and rejection behavior.
5. Establish steering/acceleration limits and driver-override behavior.
6. Write exhaustive tests and a car-specific safety model in
   `opendbc/safety/`.
7. Have the work reviewed by experienced opendbc/openpilot maintainers.
8. Only then perform controlled testing on a closed course with a reliable
   disengagement method.

The official [opendbc porting guide](https://github.com/commaai/opendbc#how-to-port-a-car)
describes those remaining steps and the project review process.

## Common problems

**`autodistill-can: command not found`**

Return to the repository and run `source .venv/bin/activate`.

**`no panda found`**

Check the USB cable, confirm the panda is running normal firmware rather than
bootstub mode, and install the official panda udev rules.

**The log contains zero frames**

Check ignition state, the physical connection, bus selection and bitrate. An
OBD connector may not expose the ADAS bus you need.

**The report finds fields but gives them generic names**

Supply a synchronized reference CSV whose values vary during the recording.

**The generated `CarState` is mostly empty**

Record more varied actions, add more reference channels, and keep both
recordings on the same timebase.

**The firmware probe gets no answers**

Confirm the correct diagnostic bus, bitrate, ignition state and physical ECU
addresses. Confirm SocketCAN is not listen-only for the probe.

**The installer refuses an existing file**

That file differs from the generated version and may contain human work. Review
the difference. Use a new checkout or output directory rather than reaching
immediately for `--force`.

## Short version

```console
$ source /path/to/AutoDistill/.venv/bin/activate
$ autodistill-can capture panda: --duration 900 --output drive.log
$ autodistill-can port drive.log --reference reference.csv \
    --firmware-log firmware.log --vehicle-info vehicle-info.json --brand mycar \
    --name "Example Make Model 2024" --output mycar_port
$ autodistill-can install mycar_port ~/src/opendbc
```

Then read `mycar_port/README.md` and `mycar_port/port_status.json`. The result is
a guarded read-only port. Human validation and safety engineering are still
required before vehicle control.
