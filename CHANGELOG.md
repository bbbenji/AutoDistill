# Changelog

Notable changes to AutoDistill CAN. Record-keeping starts at 1.4.0; earlier
versions predate this file and their history is not reliably reconstructible.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
`vehicle-info.json` carries its own `schema_version`, which is **1** and has
stayed 1: every addition below is optional, so files written for earlier
releases still load.

## 1.4.4

### Fixed

- **A generated longitudinal control path could fail to construct against
  current opendbc.** `LongitudinalPIDTuning.kpBP`/`kpV` moved under a
  `deprecated` group upstream; the generated `interface.py` still set them as
  plain attributes, which raised `AttributeError: struct has no such member;
  name = kpBP` the moment openpilot built the port's `CarParams` -- caught by
  CI's `opendbc-current` job, on the control port it now generates with a
  longitudinal command declared (see 1.4.2). No current opendbc brand sets
  this gain any more; the generated code now only sets `kiBP`/`kiV`, matching
  every hand-written interface. `vehicle-info.json`'s `tuning.longitudinal.kp`
  is still accepted, for files that supplied one, but is no longer required
  for longitudinal tuning to count as complete and is no longer wired into
  generated code. The web UI's "Long. kp / ki" field required both halves of
  a pair it then discarded on the way in; it is now a single "Longitudinal
  gain (ki)" field.

## 1.4.3

### Fixed

- **`FW_QUERY_CONFIG` no longer fails to construct against current opendbc.**
  `FwQueryConfig.fw_version_regex` became a required field upstream; a
  generated `values.py` that omitted it raised `TypeError:
  FwQueryConfig.__init__() missing 1 required positional argument:
  'fw_version_regex'` the moment openpilot tried to build the port's
  `CarInterface` -- caught by CI's `opendbc-current` job, which constructs a
  generated interface against a fresh opendbc checkout rather than only
  asserting on generated text. Every brand's own `fw_version_regex` encodes
  that manufacturer's real firmware-string format, which a capture cannot
  reveal; the generated one now declares a permissive `[\x00-\xff]+`,
  matching the same "accept anything, tighten it once the format is known"
  default GM's own hand-written config uses, and is verified against the real
  byte strings a capture actually recovers.

## 1.4.2

### Added

- **The demo project is complete at every level, not just read-only.** Loading
  it in the web UI (or running `autodistill-can synth --vehicle-info-out
  vehicle-info.json`) now analyses the synthetic capture it just wrote and
  attaches a `vehicle-info.json` satisfying every mandatory read, lateral and
  longitudinal requirement -- `autodistill-can requirements` reports 16/16,
  25/25 and 29/29, and the generated port really does build and send both a
  lateral and a longitudinal command, checksummed against that capture.
  `safe_for_control` still stays `false`: nothing about this, or anything
  else, changes that. The facts are chosen from what analysis actually
  recovers rather than hard-coded to the synthetic car's field names (see
  `autodistill_can.demo`), so this keeps working if the synthetic car changes,
  and `tools/control_facts.py` -- which CI uses to build and install a control
  port on every push -- now shares this logic and exercises the longitudinal
  path too, which it previously did not.
- The web UI's port-generation status message now distinguishes a read-only
  result from a control one, instead of always saying "read-only".

## 1.4.1

Bug fixes found in a production-readiness pass, plus the licence file the
repository had been missing.

### Fixed

- **A capture spanning three or more buses could abort port generation
  outright.** `carstate_bindings` walked every analysed message regardless of
  bus, and only two `CANParser` instances are ever generated (main and
  camera); a named signal on any third bus whose name happened to match a
  CarState hint reached `PortSpec.parser_for` and raised, failing the whole
  `port` run over traffic the port was never going to read. A message on an
  unwired bus is now skipped by the automatic matcher and reported as a TODO
  for an explicit human binding, instead of aborting generation.
- **Several boolean CarState hints accepted a multi-bit field.**
  `leftBlinker`, `rightBlinker`, `doorOpen`, `seatbeltUnlatched`,
  `cruiseState.enabled` and `cruiseState.available` were missing the
  `wants_bool` guard that `gasPressed`/`brakePressed` already had, so a
  multi-position field (a blinker stalk reading 0/1/2/3 for off/left/right/
  hazard, say) could be silently bound as `bool(value)` -- true for right or
  hazard states as well as left.
- **The web UI's "try the demo" action left a stale analysis and port in
  place.** `upload` and firmware probing both clear the project's `analysis`,
  `port` and `review` when the underlying capture changes; loading demo data
  onto a project that already had a real capture analysed and ported did not,
  so the UI kept showing -- and serving for download -- the old result as if
  it described the demo capture just loaded.
- A background analysis or port-generation job that failed with an exception
  outside `(OSError, ValueError, KeyError, JSONDecodeError)` left the
  project's persisted status stuck at `"running"` forever, since nothing else
  ever wrote a terminal status for it. The job's outer handler now catches
  any exception there, since this is the last point before the state file
  would otherwise never be updated again.
- Facts a person typed into `save_details` while a long analysis was still
  running could be silently discarded: the analysis job's completion handler
  re-wrote `manual_info` from the snapshot taken when the job *started*,
  overwriting any edit saved in the meantime. It is now written once, at the
  start of the run, and left alone.
- Added the `LICENSE` file. `pyproject.toml` and the README both declared MIT,
  but no licence file shipped in the repository.

## 1.4.0

The release where a generated port stops being a starting point you finish by
hand and becomes one you finish by *telling AutoDistill what it cannot see*.

### Fixed

- Generated Hyundai CRC8 helpers now execute instead of raising
  `NotImplementedError`, and generated Honda checksums include the required
  extended-identifier adjustment.
- Control messages use the DBC packer for their physical bus. Secondary/camera
  commands no longer try to pack a message that exists only in that bus's DBC
  with the main-bus packer. `--camera-bus` makes ambiguous multi-bus captures
  explicit.
- Arbitrary command rates up to the 100 Hz controller rate are scheduled
  exactly over time; rates such as 40 or 60 Hz are no longer rounded to an
  incorrect integer frame interval.
- Control generation now rejects wrong DBC message names, missing or mismatched
  counter/checksum fields, underdetermined checksums, multiplexed commands, and
  value names that would be undefined in the selected control path.
- Incomplete control facts remain inert behind `noOutput`/`dashcamOnly`.
  Longitudinal acceleration limits default to zero and are mandatory instead
  of silently inheriting generic acceleration and braking guesses.
- The safety-model allowlist matches current opendbc; newer legitimate models
  are recognized, while passive/no-output modes still cannot activate a
  generated control path.

### Added

- **`autodistill-can requirements`** — everything a finished port still needs,
  scored separately for reading the car, steering it, and driving it. Each item
  says how to obtain it (the bench procedure, what to measure, or where it is
  published) and prints the `vehicle-info.json` fragment that satisfies it.
- **`autodistill-can validate`** — replays the capture through the CarState
  bindings and reports values a real car cannot produce: a speed left in km/h,
  a flag stuck on, a pedal outside 0–1, a gear map that never reads drive. Runs
  automatically during `port`; exits non-zero when something is impossible.
- **A complete control path from human-supplied facts.** `vehicle-info.json`
  gained `carstate` bindings (with a closed transform vocabulary), `actuation`
  messages, `limits`, `tuning`, `safety`, and the comma `harness`. Supply them
  and `carcontroller.py`, `values.py` and `interface.py` come out finished —
  rate-limited, counter-incremented, checksummed with the function verified
  against your own frames. `safe_for_control` stays `false` regardless; there
  is no input that changes it.
- **CRC-16 recovery**, for the CAN FD platforms that use it. Seven parameter
  sets, 16-bit candidate positions on payloads over eight bytes, and
  polynomial naming.
- **Fingerprint collision checking** at install time, against the platforms
  opendbc already ships.
- **The camera bus is wired in**: its DBC is registered as `Bus.cam` and read by
  a second `CANParser`, so a CarState field can be bound to a camera-bus signal.
  Previously that DBC was written and never referenced.
- **Guided mode in the web UI.** A sidebar panel deriving the next single action
  from the project's real state, with the reason for it, and a spotlight on the
  control when you follow it. Knows about unidentified ECUs, impossible
  bindings, and the missing harness.
- Web UI: project deletion, autosave of the vehicle form, a completeness panel,
  and walkthroughs for hardware, ECU identification, bindings, actuation,
  limits and the safety model.

- **The capture itself is now judged, before its contents are.** Three ways a
  recording is wasted all ended the same way — an analysis that completes,
  reports a few numbers and produces nothing usable, with no indication of why.
  The important one is the gateway: on most cars built since the mid-2010s the
  OBD-II port forwards only diagnostic traffic, so a capture taken there holds
  replies to your own queries and almost none of the car talking to itself. It
  is the most common way a first attempt fails, and the fix — reaching the bus
  the forward-facing camera is on — is not something anyone guesses. Also
  flagged: a capture too short to settle a checksum, and one where almost
  nothing changed, which is what a stationary car with the ignition on records.
  Reported at the top of the text report and first in the web review list,
  above the results they undermine.
- **The gap list says which facts share one drive.** Eight of the control-path
  requirements each say "capture the stock system" on their own, which reads as
  eight separate expeditions; it is one recording with the car's own lane-keep
  active, and then decoding.
- **Where firmware responses come from** is explained where they are asked
  for. The card offered to attach "an existing probe capture" without saying
  what one is or where it comes from — it is an ordinary candump capture that
  happens to contain the diagnostic exchange, obtainable by letting AutoDistill
  ask, by recording while openpilot fingerprints at startup, or by recording
  while a scan tool reads ECU information. It also notes that a gatewayed
  OBD-II port still works for this, since forwarding diagnostics is what the
  gateway is for.
- Torque Pro is documented where someone choosing what to record would look —
  the reference-data card and its walkthrough — including what an OBD-II logger
  can and cannot name.

- **Several captures of the same car can be merged into one analysis.**
  `--add-log` on the command line, "Merge another capture" in the UI. One drive
  rarely exercises everything a port needs: indicators, reverse and blind-spot
  only appear in a recording where someone used them, an underdetermined
  checksum needs more variety than one drive gave, and the stock lane-keep
  drive that answers eight of the control-path facts is a separate outing from
  the general one.

  They are laid **end to end**, not interleaved. Two recordings each start from
  their own zero, so adding them together as they are puts two drives on top of
  each other: every message then reads at twice its real rate with the jitter
  to match, which is enough to reclassify a steady broadcast as event-driven.
  Offsetting each capture to begin just after the previous one ended keeps
  every message's own cadence and costs one bad step per message per join.
  Correlation is unaffected either way, since it only ever spans the reference
  log's own window.

- **Compressed captures are read as they are** — gzip, bzip2, xz and
  zstandard, for the capture, the reference log and the firmware log alike. A
  fifteen-minute drive is hundreds of megabytes, so they are routinely stored
  compressed, and on a comma device decompressing first needs disk that is not
  there. Detected by content rather than by name, so a segment renamed without
  its suffix is not reported as unparseable. gzip, bzip2 and xz are standard
  library; zstandard is from Python 3.14, and before that needs the
  `zstandard` package, which the error says rather than reporting corruption.
- **openpilot's own segments are a capture source.** `rlog`, `rlog.zst` and
  `rlog.bz2`, read straight from a comma device. A device in dashcam mode
  already logs every frame on every bus, which makes it the best capture this
  tool can be handed and needs no hardware beyond what porting needs anyway.
  Frames openpilot *transmitted* are excluded — they are not the car speaking,
  and counting them as evidence about the car would be wrong.

  This is the only format that needs a dependency, because Cap'n Proto is
  schema-driven: `pip install 'autodistill-can[rlog]'`, plus openpilot's
  `log.capnp`, located from an installed openpilot or `AUTODISTILL_LOG_CAPNP`.
  The schema is deliberately not vendored — it changes with openpilot, and a
  stale copy that still parsed would be worse than none. Everything else here
  remains standard library.

### Fixed

- **A generated port no longer breaks fingerprinting for other cars.** A
  `FW_VERSIONS` table of unidentified ECUs could never be ruled out — openpilot
  skips a missing non-essential ECU when matching, and an unidentified ECU is
  never essential — so it matched *every car in opendbc*. Installing one made
  460 unrelated vehicles fail their fingerprint tests. Firmware tables are now
  declared in one of three honest states, and `FW_VERSIONS` and
  `FW_QUERY_CONFIG` are always declared together.
- `analyse_log` crashed under `python - <<EOF`, where `__main__.__file__` is the
  unimportable string `"<stdin>"` and worker processes cannot start. It now
  falls back to sequential analysis instead.
- The web form silently dropped `scale` and `offset` on CarState bindings: one
  input serves both `threshold` and `scale` but loaded only from `threshold`, so
  a scale binding came back empty and the next edit anywhere on the page failed
  to save.
- `install` refused a control port, making the control path uninstallable. The
  gate now checks the invariant that matters — that nothing claims to be safe
  for control.
- Two CSS cascade bugs: the manual-facts heading lost its padding to a more
  general rule, and the install card's heading was squeezed into a grid column
  meant for an icon.
- **The UI now meets WCAG AA on contrast, and a test keeps it there.** `--faint`
  was 2.9:1 on white while carrying the copy that explains what each field
  wants; `--amber` failed in both directions at once, as badge text on
  `--amber-soft` and as the fill behind white text on `.button-warning`; and
  four colours on the dark sidebar — including the guide's own "how do I do
  this?" link, at 2.7:1 — were below the line.
- Controls no longer clip the text that distinguishes them. The CarState
  binding editor gave each select about 110px, enough to cut
  `ret.cruiseState.standstill` in half; the drive-length select clipped the
  guidance in every option ("15 minutes · recom"); and a message was labelled
  `MSG_0AA · bus 0 · 0xAA`, spending a third of the width repeating itself.
- Layout at narrow widths: the five-field specification row never collapsed, and
  stacked choice cards kept the height floor that levels them side by side, so
  `margin-top: auto` opened a gap in the middle of the upload card.
- Field rows line up on their controls rather than their boxes, via subgrid.
  Aligning boxes fails in two opposite ways: stretched, a label that wraps to
  two lines pushes its input a line below its neighbours' and a row of five
  inputs steps up and down; bottom-aligned, a field carrying a hint under its
  input drags its *neighbour* down instead — the safety model's "Must exist in
  opendbc/safety" line pushed "Safety param" to the bottom of the row, well
  below the label it belongs to.
- The modal close button scrolled out of sight partway down the longer help
  topics, which are several screens long.
- The engineering checklist titled every row "Human task 3" and demoted the
  instruction to small grey; the project list printed the internal stage key
  (`analysis`) instead of the stage's name, and drew the only outlined button on
  each row around **Delete**.
- Empty states: the project list showed "Choose a vehicle project" above
  nothing, the capture row offered to "Replace" a capture that was not there,
  and guided mode ticked off "bindings produce believable values" on a fresh
  project, where it is vacuously true.
- Which of the four steps you are on reaches a screen reader, via `aria-current`
  rather than colour and a lime bar alone.
- `tools/control_facts.py` analysed differently from the pipeline, producing
  facts files `port` then rejected.
- The analysis panel's handoff card numbered its items "Human task 3" and
  demoted the instruction to small grey, the same defect as the port panel's
  checklist — this half of it lived in the server rather than the browser.
- `install` still described itself as read-only, in both its `--help` and its
  module docstring, after it was changed to accept a control port. CI installs
  a control port through that exact command.
- **The facts that finish a port moved to step 3, beside the gaps that ask for
  them.** They were on step 2, before the decoder had run — but the CarState
  binding editor is populated from the decoded message and signal inventory, so
  on a forward pass through the wizard it could only say "Run the decoder
  first", and the completeness panel's "Fill in the gaps" had to send you
  *backwards* a step. Step 2 now keeps what changes the decode (vehicle
  identity, reference data, known ECU types, known signal definitions); the
  vehicle specifications, bindings, actuation, limits, tuning and safety model
  sit under the completeness list that names them, and "Fill in the gaps" is a
  scroll rather than a navigation.
- **Every gap links to the control that closes it.** "How to find it" explains
  the measurement; beside it now sits "Fill it in" or, for a CarState field,
  "Bind it" — which opens the facts block and creates the binding row with the
  field already selected, since a binding has no control of its own until one
  is made. Knowing the procedure and then hunting for the box was most of what
  made these tedious. Requirements with no field of their own — the two
  computed specs, and cruise buttons — get no link rather than one that goes
  nowhere.
- **A later completeness level can no longer look closer to done than an
  earlier one.** The levels are cumulative — steering needs everything reading
  needs, and nine more — so "Steer 10/25" drew a longer bar than "Read 3/16"
  while being the further of the two from usable. The counts are unchanged,
  since they answer "what does this level need?" honestly; a level still gated
  by an earlier one now says so ("after Read the car") and draws its bar
  striped rather than solid. Striped rather than dimmed: lowering the opacity
  would have taken the text back under the contrast floor.
- The facts block was styled to sit *inside* a white card — its own border,
  12px corners and a mint header band — so once it became a card on step 3 it
  showed a tinted seam across its top edge against the card's 18px radius.
- Step 3's "← Change inputs" is now "← Vehicle details", which is what step 2
  still holds now that the port-facing facts have left it.
- **Step 3 says what it is waiting for.** Pressing "Decode this drive" moves
  you to step 3, so a run that then failed left you on a panel reading "Ready
  when you are" over a radar graphic and a coverage dial showing "—", with its
  only button sending you back a step. The failure itself had gone by in a
  toast. It now names the blocker — the run that failed, and why; no capture;
  incomplete vehicle details — and carries the action for it, including
  decoding from the panel itself rather than from the previous step.
- **A project whose analysis failed could not be decoded again.** The message
  and signal dropdowns are filled from the decoded inventory, so with no
  analysis every saved CarState binding read back blank; rebuilding the form
  then threw, and running the decoder goes through that same code — so the
  retry was refused by bindings the user could not even see options for, and
  would have been overwritten with blanks had it succeeded.
- **Torque Pro logs load as a reference.** It is the logger most drivers
  already have, and its export failed on four counts: the timebase column is
  called `Device Time` and holds `02-Aug-2026 11:34:40.210` rather than
  seconds; a reading it does not have is written `-`, and the loader treated
  any unparseable cell as the end of the row, so a 181-row log came back as
  "no usable rows"; `G(x)`, `G(y)`, `G(z)` and `G(calibrated)` all reduced to
  one channel named `G`; and `Speed (OBD)(km/h)` needed the unit read off the
  second bracket. A gap is now a gap in one channel rather than the loss of a
  row, and `ReferenceSeries.present` records which samples were measured, so
  the values filled in to keep channels rectangular never reach a correlation.

### Changed

- Published accuracy re-measured against current code: **24/46 (52%)** signals
  fully exact, **32/46 (70%)** boundaries. The previous 20/32 (62%) was correct
  when written — the synthetic car has since grown from 32 signals to 46.
- The Opel Corsa column was removed from the README. Its capture is gone and its
  figures came from a superseded process, so they cannot be reproduced or
  corrected.
- Per-message analysis runs in worker processes. A caller must guard its entry
  point with `if __name__ == "__main__":`, or pass `workers=1`.


### Verified

- **The UI is now rendered in a real browser on every CI run.** Every panel, at
  1280px and 560px, checked for console errors, sideways overflow, a panel that
  came out empty, and any `<select>` too narrow for the widest option it
  offers. Nothing else in the suite lays the page out, and every defect fixed
  in this release was invisible to it. The check found one more while being
  written: the CarState binding row still clipped `D_STEER_ANGLE_DEG_DT` at
  three columns, and is now two. A missing browser is a skip locally and a
  failure in CI, so it cannot quietly stop running.

- opendbc's own car test suite runs against both a read-only and a control port
  installed into a fresh checkout: **271 passed, 4861 subtests**.
  `test_fw_query_timing` is deselected — it looks each brand up in a hardcoded
  reference-time table upstream, so it fails for any new brand.
- Alfa Giulia, re-measured: 76 messages, 31 counters, 29 checksums all
  CRC-8/SAE-J1850, **261,836 / 261,836** frames reproduced, in 54 seconds.
- Kia Soul EV, re-measured: the steering-angle layout is still byte-identical to
  the human-written DBC.
