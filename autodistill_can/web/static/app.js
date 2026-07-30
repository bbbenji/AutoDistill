"use strict";

const apiToken = document.querySelector('meta[name="api-token"]').content;
const stageOrder = ["source", "details", "analysis", "port"];
//: The sidebar's wording for each stage, so the project list can say where a
//: project got to in the same words rather than printing the internal key.
const stageLabels = {
  source: "Get CAN data",
  details: "Describe the car",
  analysis: "Decode traffic",
  port: "Build the port",
};
const helpCopy = {
  capture: `
    <p>Park first and connect your CAN hardware. Then make one ordinary, legal
    drive on a familiar route. A useful recording is usually 15–30 minutes.</p>
    <h3>Include, when safe</h3>
    <ul>
      <li>Several complete stops and gentle starts.</li>
      <li>Low, medium, and steady road speeds.</li>
      <li>Left and right turns with different steering angles.</li>
      <li>Brake pedal, accelerator, indicators, and cruise buttons.</li>
      <li>A few seconds parked before and after the drive.</li>
    </ul>
    <h3>Avoid</h3>
    <ul>
      <li>Testing controls, wiring, or diagnostic tools while moving.</li>
      <li>Recording only a parked, idling car—the values will not vary enough.</li>
      <li>Sharing raw captures publicly without checking for VIN and location data.</li>
    </ul>`,
  reference: `
    <p>A reference file is a timestamped CSV containing measurements whose
    meanings are already known. AutoDistill compares each recovered CAN field
    with these channels to discover names, scale factors, offsets, and units.</p>
    <h3>Useful channels</h3>
    <ul>
      <li>GPS speed in km/h or m/s.</li>
      <li>Steering-wheel angle from a logger or scan tool.</li>
      <li>Brake, accelerator, wheel speed, and indicator state.</li>
      <li>OBD-II PIDs captured at the same time as the CAN log.</li>
    </ul>
    <h3>CSV shape</h3>
    <p>Use one time column plus one column per measurement. Put units in the
    heading when possible: <code>time,speed_kph [km/h],steer_angle_deg [deg]</code>.</p>
    <h3>Torque Pro</h3>
    <p>A <strong>Torque Pro</strong> track log works as it comes: its
    <code>trackLog-….csv</code> export is understood directly, including its
    wall-clock timestamps, its <code>-</code> for a reading it does not have,
    and units written as <code>Speed (OBD)(km/h)</code>. Record it on the same
    drive as the CAN capture so the two share a clock.</p>
    <p>Being an OBD-II logger decides what it can name for you: vehicle speed,
    engine RPM, accelerator pedal, coolant temperature and engine load are all
    there, and speed alone identifies the wheel-speed and vEgo fields that a
    port needs first. <strong>Steering angle is not a standard OBD-II PID</strong>,
    so it is not in the export and the steering fields have to be identified
    another way — unless your car exposes one through a manufacturer PID, which
    Torque can be told to log.</p>`,
  hardware: `
    <p>You need something that turns the car's CAN bus into USB, and somewhere
    to plug it in. Neither is difficult, but getting the connection point wrong
    is the most common reason a capture comes back empty.</p>
    <h3>What to use</h3>
    <ul>
      <li><strong>A comma panda</strong> is the path of least resistance: it is
      what openpilot itself uses, it reads several buses at once, and
      AutoDistill drives it directly with the source <code>panda:</code>. A red
      panda also handles CAN FD.</li>
      <li><strong>Any Linux-supported USB-CAN adaptor</strong> works too. It
      appears as <code>can0</code> and you use the source
      <code>socketcan:can0</code>. One interface normally means one bus, so
      reading the car and the camera at once needs two.</li>
    </ul>
    <h3>Where to plug it in</h3>
    <p>The <strong>OBD-II port</strong> is the 16-pin trapezoid under the
    dashboard on the driver's side. On CAN cars, pin 6 is CAN-High and pin 14
    is CAN-Low; pin 16 is permanent +12V and pins 4 and 5 are ground.</p>
    <p><strong>The catch:</strong> on most cars built since roughly the
    mid-2010s that port sits behind a gateway that only forwards diagnostic
    traffic. You will see replies to your own queries but almost none of the
    car talking to itself. The symptom is unmistakable — a handful of
    <code>0x7xx</code> addresses and nothing else.</p>
    <p>When that happens, the bus you actually want is the one the
    <strong>forward-facing ADAS camera</strong> is on, behind the windscreen.
    That is where openpilot's car harnesses connect, and a harness for your
    model is the safest way to reach it: it plugs in between the camera and the
    car without cutting anything.</p>
    <h3>Two things that catch people out</h3>
    <ul>
      <li><strong>Do not enable termination.</strong> A CAN bus already has a
      120Ω resistor at each end; you are joining in the middle as an extra
      listener. If your adaptor has a termination switch or jumper, leave it
      off. With the ignition off, a healthy bus reads about 60Ω between CAN-H
      and CAN-L — that is the two existing resistors in parallel, and a good
      check that you are on a real bus before you power anything up.</li>
      <li><strong>Bitrate has to match.</strong> 500 kbit/s is right for almost
      every powertrain bus; comfort and body buses are often 125 kbit/s. Wrong
      bitrate means no frames at all, or a flood of errors.</li>
    </ul>
    <h3>Check it works before driving anywhere</h3>
    <p>Record ten seconds while parked with the ignition on, and watch the
    frame counter. Hundreds or thousands of frames means you are connected.
    Zero means you are not, and no amount of driving will change that.</p>
    <p>Never operate a laptop while driving. Start the recording, then drive,
    or bring a passenger.</p>`,
  firmware: `
    <p>Firmware versions are how openpilot recognises most cars built in the
    last decade. The message set alone often cannot tell a 2020 from a 2021, or
    one trim from another; the version strings burned into the ECUs can.</p>
    <h3>What the file actually is</h3>
    <p>Nothing special: an ordinary CAN capture, in the same candump format as
    your drive, that happens to contain a diagnostic question-and-answer.
    AutoDistill reassembles the ISO-TP replies out of it and pulls the version
    strings from them. There is no separate format to produce.</p>
    <h3>Three ways to get one</h3>
    <ul>
      <li><strong>Let AutoDistill ask.</strong> The guarded probe on the decode
      step sends the requests itself and keeps the capture, so there is nothing
      to attach afterwards. It needs a SocketCAN interface that can transmit —
      its own walkthrough covers the setup.</li>
      <li><strong>Record while openpilot asks.</strong> openpilot queries
      firmware every time it starts. Capture the bus, power the device up, and
      the whole exchange is in the file.</li>
      <li><strong>Record while a scan tool asks.</strong> Any garage scan tool
      or OBD app that reads ECU information will do: start the capture first,
      then run the scan, and stop the capture afterwards.</li>
    </ul>
    <h3>A gatewayed port still works for this</h3>
    <p>If the OBD-II port turned out to be gatewayed and useless for capturing
    the car itself, it is still the right place for this one. Forwarding
    diagnostics is precisely what that gateway is for — so you can collect
    firmware from the OBD-II port even when the drive capture has to come from
    a harness behind the windscreen.</p>
    <h3>If the file has nothing in it</h3>
    <p>AutoDistill reports how many firmware responses it found. Zero usually
    means the capture was taken before or after the exchange rather than during
    it, or that the queries went to a bus the capture was not on.</p>`,
  probe: `
    <p>This is the one step that <strong>transmits</strong>. Everything else
    AutoDistill does is passive listening; this asks each ECU to identify
    itself, using the same read-only diagnostic request a garage scan tool
    sends. It cannot write to an ECU, clear a fault, or unlock anything.</p>
    <h3>Before you start</h3>
    <ul>
      <li>Only on a car you own or are authorised to work on.</li>
      <li>Stationary, in park, parking brake on, nobody driving it.</li>
      <li>Ignition on so the ECUs are awake. Leaving the engine running is
      fine and avoids flattening the battery.</li>
    </ul>
    <h3>The hardware difference</h3>
    <p>Because it sends, the probe needs a SocketCAN interface with
    transmission enabled — a passive <code>panda:</code> capture cannot do it,
    and neither can an interface still in listen-only mode. If you set the
    interface up for capture, you have to reconfigure it:</p>
    <pre><code>sudo ip link set can0 down
sudo ip link set can0 type can bitrate 500000 listen-only off
sudo ip link set can0 up</code></pre>
    <p>Forgetting the <code>listen-only off</code> is the usual reason a probe
    returns nothing: the requests are never actually put on the wire.</p>
    <h3>Which bus</h3>
    <p>Diagnostics normally live on the same powertrain bus you captured from.
    If the OBD-II port is gatewayed, that port is still the right place for
    <em>this</em> step — a gateway forwards diagnostics, which is exactly what
    this is.</p>
    <h3>If nothing answers</h3>
    <p>Ignition off, wrong bus, wrong bitrate, still listen-only, or the
    manufacturer uses non-standard addresses. The last is common; the standard
    sweep covers the usual OBD/UDS blocks, and specific addresses can be added
    if a service document names them.</p>`,
  specs: `
    <p>Three numbers, none of which are on the bus.</p>
    <h3>Mass</h3>
    <p>Kerb mass in kilograms, from the specifications page of the owner's
    manual or the manufacturer's brochure for your exact trim. The sticker in
    the door jamb usually gives <em>gross vehicle weight</em>, which is a much
    larger number and the wrong one. A public weighbridge with a full tank is
    the direct measurement.</p>
    <h3>Wheelbase</h3>
    <p>Published for every car, and measurable with a tape from front hub
    centre to rear hub centre. Measure both sides and average.</p>
    <h3>Steering ratio</h3>
    <p>Sometimes published as "14.3:1". To measure it: park with the wheels
    straight on a smooth surface, turn the steering wheel exactly one full
    turn, and measure how far the road wheel moved with an angle gauge held
    against the wheel face. The ratio is 360 divided by that angle. openpilot
    refines this while driving, so a close value is enough to start.</p>
    <h3>Package / trim label</h3>
    <p>Free text, shown in openpilot's supported-car list. Name the trim or
    the option pack that has the lane-keep hardware, e.g. "All" or
    "Safety Sense 2.0".</p>`,
  ecus: `
    <p>Each line maps a diagnostic <em>request</em> address to what kind of
    module answers on it. openpilot uses this to fingerprint the car by
    firmware version.</p>
    <h3>Where the addresses come from</h3>
    <p>Run the guarded firmware probe on the analysis step. Every address that
    answers appears in the results; those are the ones to identify.</p>
    <h3>Working out which module is which</h3>
    <ul>
      <li><code>0x7E0</code> is the engine ECM on almost every car, by OBD-II
      convention, and <code>0x7E1</code> is usually the transmission.</li>
      <li>The firmware string itself often contains a part number you can
      search, or a recognisable abbreviation such as EPS or ABS.</li>
      <li>Check an existing port for the same manufacturer in opendbc: its
      <code>FW_VERSIONS</code> table maps the same addresses, and OEMs reuse
      them across models and years.</li>
      <li>Manufacturer service documentation lists module diagnostic
      addresses directly.</li>
    </ul>
    <p>A wrong guess is worse than leaving it out, so omit any you are not
    sure about.</p>`,
  bindings: `
    <p>These say which recovered signal is which openpilot field. Nothing in
    a signal's name or position can tell you that a bit is the brake switch,
    so this is measurement, not analysis.</p>
    <h3>The method for almost all of them</h3>
    <ol>
      <li>Park safely with the engine running.</li>
      <li>Record a short capture doing <em>one</em> thing repeatedly, with a
      couple of seconds between each — press the brake five times, say.</li>
      <li>Run the decoder and look for the field that changed in step. A bit
      that toggles exactly five times is the one.</li>
    </ol>
    <p>Doing one action per capture is what makes this quick. Doing five at
    once leaves you unable to tell which field belongs to which.</p>
    <h3>Checking the sense</h3>
    <p>Many cars report the opposite of what openpilot wants — seatbelt
    <em>latched</em> rather than unlatched. Use the invert transform rather
    than assuming.</p>
    <h3>Gears</h3>
    <p>Foot on the brake, move the selector through every position in turn,
    pausing in each, and note the raw value at each stop. Enter them as
    <code>0=park 1=reverse 3=drive</code>.</p>
    <h3>Units</h3>
    <p>openpilot works in m/s. A speed field is nearly always km/h or mph, so
    pick the matching transform. Switching the dashboard between units while
    capturing tells you which one the bus carries.</p>`,
  actuation: `
    <p><strong>This is the part that requires bench work.</strong> Everything
    else here is observation; this is a message you intend to transmit at a
    car, and a mistake is expensive.</p>
    <h3>Start with what already exists</h3>
    <p>Check opendbc for a port from the same manufacturer. Command messages
    are shared across model years and whole platforms, and a sibling port may
    already name yours, with a reviewed safety model to go with it.</p>
    <h3>Finding it yourself</h3>
    <ol>
      <li>Capture with the stock lane-keep camera connected, on both the
      vehicle bus and the camera bus.</li>
      <li>Drive with the factory lane-keep active so it steers the car.</li>
      <li>Look for a message that only the camera sends, whose payload tracks
      the steering the car applies to itself.</li>
      <li>Confirm it on a bench, with the car unable to move, before it is
      ever sent in anger.</li>
    </ol>
    <h3>Signal values</h3>
    <p>Each line says which signal gets which computed quantity.
    <code>apply_torque</code> is the rate-limited steering command,
    <code>lat_active</code> is 1 while openpilot is steering, and a plain
    number is a constant the message always carries. The message and every
    signal in it must be ones this capture actually contains.</p>`,
  limits: `
    <p>These bound what openpilot may command. Every one of them can be read
    off the factory system rather than guessed: the stock lane-keep is a
    working example of what this car accepts.</p>
    <h3>How to read them off</h3>
    <ol>
      <li>Capture the factory lane-keep working hard — a tight motorway curve
      is ideal.</li>
      <li>Decode that capture with the generated DBC.</li>
      <li><strong>Max steer command</strong> is the largest magnitude the
      command signal ever reaches. The analysis report lists each signal's
      observed range, so this is a lookup.</li>
      <li><strong>Ramp up / down</strong> are the largest increase and
      decrease between consecutive frames of that signal.</li>
      <li><strong>Driver override torque</strong>: resist the wheel gradually
      while the stock system steers, and note the driver torque at which it
      backs off.</li>
      <li><strong>Actuator delay</strong> is the lag between the command and
      the steering angle responding — typically 0.1 to 0.4 seconds.</li>
    </ol>
    <p>Never exceed what the factory system commands. It is the one bound you
    know the car tolerates.</p>
    <h3>Tuning</h3>
    <p>Copying a comparable opendbc platform's values as a starting point is
    normal and expected. Torque tuning is then fitted from logs of lateral
    acceleration against steering torque over varied driving.</p>`,
  safety: `
    <p>openpilot's safety layer is C code in <code>opendbc/safety/</code>. It
    sits below everything AutoDistill generates and rejects any message
    outside the limits it enforces. It is the thing that actually stops a bad
    command reaching the car.</p>
    <h3>Naming a model</h3>
    <p>Look in <code>opendbc/safety/modes/</code> for your manufacturer. If a
    mode exists and already bounds exactly the messages you intend to send,
    name it here.</p>
    <h3>If one does not exist</h3>
    <p>Writing it is the real work of a port. It must reject anything outside
    the limits, come with tests, and be reviewed by people who know the
    platform. <strong>Naming a model here does not create one</strong>, and a
    name that does not exist will simply fail to load.</p>
    <p>Whatever you enter, the generated port is still marked
    <code>safe_for_control: false</code>. That is not a score to beat — it
    records that AutoDistill has not driven your car and cannot vouch for
    any of this.</p>`,
  general: `
    <p>AutoDistill turns a recorded CAN bus conversation into a read-only
    starting point for an openpilot/OpenDBC car port.</p>
    <ol>
      <li>Record or upload CAN traffic.</li>
      <li>Add synchronized known measurements when available.</li>
      <li>Recover fields, counters, checksums, and fingerprints.</li>
      <li>Review every explicit human task and export the bootstrap port.</li>
    </ol>
    <h3>Important safety boundary</h3>
    <p>The generated controller sends nothing. AutoDistill cannot infer safe
    steering commands, vehicle limits, or a safety model by watching traffic.
    Those require human engineering, review, and closed-course validation.</p>`,
};

let systemStatus = null;
let project = null;
let currentStep = "source";
let lastProjectStage = null;
let pollTimer = null;
let brandWasEdited = false;
let lastErrorMessage = "";
let manualProjectId = null;
let saveTimer = null;

const byId = (id) => document.getElementById(id);
const all = (selector) => [...document.querySelectorAll(selector)];
const rowsIn = (id, selector) => [...byId(id).querySelectorAll(selector)];

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-AutoDistill-Token", apiToken);
  if (options.json !== undefined) {
    headers.set("Content-Type", "application/json");
    options.body = JSON.stringify(options.json);
  }
  const response = await fetch(path, {...options, headers});
  let payload;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    payload = await response.json();
  } else {
    payload = await response.text();
  }
  if (!response.ok) {
    // Only the API answers in JSON. Anything else is Python's own error page,
    // and pasting its HTML into a toast tells the reader nothing. A 501 has
    // one cause worth naming: this page is newer than the process serving it,
    // because static files are re-read per request while the code is not.
    let message = payload && payload.error ? payload.error : "";
    if (!message) {
      message = response.status === 501
        ? "The running AutoDistill server is older than this page. Stop it with"
          + " Ctrl+C and run `autodistill-can ui` again."
        : `Request failed (${response.status} ${response.statusText}).`;
    }
    throw new Error(message);
  }
  return payload;
}

function toast(message, type = "success") {
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.textContent = message;
  byId("toast-stack").append(item);
  window.setTimeout(() => item.remove(), 5000);
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
}

function slugBrand(value) {
  let slug = value.toLowerCase().trim()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_+|_+$/g, "");
  if (!/^[a-z]/.test(slug)) slug = `car_${slug}`;
  return (slug || "mycar").slice(0, 40);
}

function lineValues(id) {
  return byId(id).value.split(/\r?\n/)
    .map((value) => value.trim())
    .filter(Boolean);
}

function optionalNumber(id) {
  const value = byId(id).value.trim();
  if (!value) return null;
  const number = Number(value);
  if (!Number.isFinite(number)) throw new Error(`${id} must be a number.`);
  return number;
}

function makeSignalField(labelText, className, control) {
  const label = document.createElement("label");
  label.className = `field ${className || ""}`.trim();
  const caption = document.createElement("span");
  caption.textContent = labelText;
  label.append(caption, control);
  return label;
}

function signalInput(type, value = "") {
  const input = document.createElement("input");
  input.type = type;
  input.value = value ?? "";
  return input;
}

function addSignalRow(data = {}) {
  const row = document.createElement("div");
  row.className = "manual-signal-row";

  const bus = signalInput("number", data.bus ?? 0);
  bus.className = "manual-signal-bus";
  bus.min = "0";
  bus.max = "15";
  const address = signalInput("text", data.address || "");
  address.className = "manual-signal-address";
  address.placeholder = "0x123";
  const start = signalInput("number", data.start_bit ?? "");
  start.className = "manual-signal-start";
  start.min = "0";
  start.max = "511";
  const length = signalInput("number", data.length ?? "");
  length.className = "manual-signal-length";
  length.min = "1";
  length.max = "64";
  const name = signalInput("text", data.name || "");
  name.className = "manual-signal-name";
  name.placeholder = "VEHICLE_SPEED";
  const unit = signalInput("text", data.unit || "");
  unit.className = "manual-signal-unit";
  unit.placeholder = "km/h";
  const scale = signalInput("number", data.scale ?? "");
  scale.className = "manual-signal-scale";
  scale.step = "any";
  const offset = signalInput("number", data.offset ?? "");
  offset.className = "manual-signal-offset";
  offset.step = "any";
  const byteOrder = selectOf(
    "manual-signal-order",
    [["", "Recovered"], ["big", "Big endian"], ["little", "Little endian"]],
    data.byte_order || "",
  );
  const signed = selectOf(
    "manual-signal-signed",
    [["", "Recovered"], ["false", "Unsigned"], ["true", "Signed"]],
    data.signed === undefined || data.signed === null ? "" : String(data.signed),
  );
  const target = document.createElement("select");
  target.className = "manual-signal-target";
  const automatic = document.createElement("option");
  automatic.value = "";
  automatic.textContent = "Infer CarState field from name";
  target.append(automatic);
  (systemStatus?.carstate_targets || []).forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    target.append(option);
  });
  target.value = data.carstate_target || "";

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "remove-signal";
  remove.textContent = "×";
  remove.setAttribute("aria-label", "Remove known signal");
  remove.addEventListener("click", () => {
    row.remove();
    queueDetailsSave();
  });

  row.append(
    makeSignalField("Bus", "", bus),
    makeSignalField("CAN address", "", address),
    makeSignalField("Start bit", "", start),
    makeSignalField("Length", "", length),
    makeSignalField("Signal name", "signal-name", name),
    makeSignalField("Unit", "", unit),
    makeSignalField("Scale", "", scale),
    makeSignalField("Offset", "", offset),
    makeSignalField("Byte order", "", byteOrder),
    makeSignalField("Signedness", "", signed),
    makeSignalField("Openpilot CarState field", "signal-target", target),
    remove,
  );
  byId("manual-signal-list").append(row);
}

const CARSTATE_TRANSFORMS = [
  "identity", "bool", "invert_bool", "threshold", "equals", "scale",
  "kph_to_ms", "mph_to_ms", "rad_to_deg", "percent", "gear_map",
];

let inventory = [];
let inventoryStamp = "";
// Deliberately not "" -- that is a real state (nothing left to do), and
// starting equal to it meant the first render skipped its own update and the
// panel kept the placeholder text from the markup.
let guideStepKey = null;

function selectOf(className, options, value) {
  const select = document.createElement("select");
  select.className = className;
  options.forEach(([optionValue, label]) => {
    const option = document.createElement("option");
    option.value = optionValue;
    option.textContent = label;
    select.append(option);
  });
  select.value = value ?? "";
  return select;
}

function messageLabel(message) {
  // A generated name already carries the address -- "MSG_0AA · bus 0 · 0xAA"
  // spends a third of the select on a repeat of itself, and the useful part
  // was what got clipped. A name that does not carry it still shows it.
  const hex = message.address_hex || "";
  const stem = hex.replace(/^0x/i, "").toUpperCase();
  const base = `${message.name} · bus ${message.bus}`;
  return stem && String(message.name || "").toUpperCase().includes(stem)
    ? base
    : `${base} · ${hex}`;
}

// Keyed on the numeric address, never the printed one: "0x0AA" and "0xAA" are
// the same message, and matching on the text would silently drop a binding
// whose file happened to pad it.
function messageKey(message) {
  return `${message.bus}:${message.address}`;
}

function bindingKey(bus, address) {
  return `${Number(bus) || 0}:${Number(address)}`;
}

function addBindingRow(data = {}) {
  const row = document.createElement("div");
  row.className = "manual-signal-row binding-row";

  const target = selectOf(
    "binding-target",
    (systemStatus?.carstate_fields || []).map((field) => [field, field]),
    data.target || "",
  );
  const message = selectOf("binding-message", [], "");
  const signal = selectOf("binding-signal", [["", "Signal…"]], data.signal || "");
  const transform = selectOf(
    "binding-transform",
    CARSTATE_TRANSFORMS.map((name) => [name, name]),
    data.transform || "identity",
  );
  // One box serves both `threshold` and `scale`, so it has to load from
  // whichever the saved binding used. Reading it back always expects a value,
  // so populating it from `threshold` alone made every scale binding fail to
  // save the next time anything on the page was edited.
  const threshold = signalInput("number", data.threshold ?? data.scale ?? "");
  threshold.className = "binding-threshold";
  threshold.step = "any";
  // `offset` has no field of its own. Carry it so a binding that has one
  // survives a round trip through the form instead of being silently dropped.
  if (data.offset !== undefined && data.offset !== null) {
    row.dataset.offset = String(data.offset);
  }
  if (data.note) row.dataset.note = data.note;
  const gears = signalInput("text", Object.entries(data.gear_map || {})
    .map(([raw, gear]) => `${raw}=${gear}`).join(" "));
  gears.className = "binding-gears";
  gears.placeholder = "0=park 1=reverse 3=drive";

  // The signal list depends on the message, so it is repopulated whenever the
  // message changes rather than listing every signal in the car at once. Both
  // lists are rebuildable because the inventory arrives after the first paint
  // and after every re-run of the decoder.
  const option = (value, label) => {
    const element = document.createElement("option");
    element.value = value;
    element.textContent = label;
    return element;
  };
  const fillSignals = (selected) => {
    const chosen = inventory.find((m) => messageKey(m) === message.value);
    signal.replaceChildren(
      option("", chosen ? "Signal…" : "Run the decoder first"),
      ...(chosen?.signals || []).map((name) => option(name, name)),
    );
    if (selected) signal.value = selected;
  };
  const fillMessages = (selected) => {
    message.replaceChildren(
      option("", inventory.length ? "Message…" : "Run the decoder first"),
      ...inventory.map((m) => option(messageKey(m), messageLabel(m))),
    );
    if (selected) message.value = selected;
  };
  // Remember what this row is *meant* to point at, not just what the select
  // currently shows. Saved bindings are restored before the message list has
  // arrived, and assigning to a <select> with no matching option is silently
  // dropped -- so the intent has to outlive the empty dropdown.
  let wantMessage = data.address === undefined ? "" : bindingKey(data.bus, data.address);
  let wantSignal = data.signal || "";
  message.addEventListener("change", () => {
    wantMessage = message.value;
    wantSignal = "";
    fillSignals();
  });
  signal.addEventListener("change", () => { wantSignal = signal.value; });
  row.refreshOptions = () => {
    fillMessages(wantMessage);
    fillSignals(wantSignal);
  };
  row.refreshOptions();

  const showExtras = () => {
    threshold.parentElement.classList.toggle(
      "hidden", !["threshold", "equals", "scale"].includes(transform.value),
    );
    gears.parentElement.classList.toggle("hidden", transform.value !== "gear_map");
  };
  transform.addEventListener("change", showExtras);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "remove-signal";
  remove.textContent = "×";
  remove.setAttribute("aria-label", "Remove binding");
  remove.addEventListener("click", () => {
    row.remove();
    queueDetailsSave();
  });

  row.append(
    makeSignalField("openpilot field", "signal-target", target),
    makeSignalField("Message", "signal-name", message),
    makeSignalField("Signal", "", signal),
    makeSignalField("Transform", "", transform),
    makeSignalField("Threshold / scale", "", threshold),
    makeSignalField("Gear map", "binding-gears-field", gears),
    remove,
  );
  showExtras();
  byId("binding-list").append(row);
  return row;
}

function bindingsPayload() {
  // The message and signal dropdowns are filled from the decoded inventory, so
  // before anything has been decoded they have no options and every saved
  // binding reads back blank. Rebuilding from the form here would both destroy
  // those bindings and throw -- and since running the decoder goes through
  // this function, a project that had bindings and then lost its analysis
  // (a re-run that failed) could not be decoded again at all.
  if (!inventory.length) {
    return (project?.manual_info?.carstate || []).map((item) => ({...item}));
  }

  const bindings = [];
  rowsIn("binding-list", ".binding-row").forEach((row, index) => {
    const value = (selector) => row.querySelector(selector).value.trim();
    const target = value(".binding-target");
    const message = value(".binding-message");
    const signal = value(".binding-signal");
    if (!target && !message && !signal) return;
    if (!target || !message || !signal) {
      throw new Error(`Binding ${index + 1} needs a field, a message, and a signal.`);
    }
    const [bus, address] = message.split(":");
    const transform = value(".binding-transform") || "identity";
    const item = {
      target, bus: Number(bus), address: Number(address), signal, transform,
    };
    const threshold = value(".binding-threshold");
    if (["threshold", "equals"].includes(transform)) {
      if (threshold === "") throw new Error(`Binding ${index + 1} needs a threshold.`);
      item.threshold = Number(threshold);
    } else if (transform === "scale") {
      if (threshold === "") throw new Error(`Binding ${index + 1} needs a scale.`);
      item.scale = Number(threshold);
      if (row.dataset.offset !== undefined) {
        item.offset = Number(row.dataset.offset);
      }
    }
    if (transform === "gear_map") {
      const map = {};
      value(".binding-gears").split(/[\s,]+/).filter(Boolean).forEach((pair) => {
        const [raw, gear] = pair.split("=");
        if (!gear) throw new Error(`Binding ${index + 1}: gear map needs 0=park pairs.`);
        map[raw] = gear;
      });
      if (!Object.keys(map).length) {
        throw new Error(`Binding ${index + 1} needs a gear map.`);
      }
      item.gear_map = map;
    }
    if (row.dataset.note) item.note = row.dataset.note;
    bindings.push(item);
  });
  return bindings;
}

function commandPayload(prefix, purpose) {
  const text = (id) => byId(`${prefix}-${id}`).value.trim();
  const address = text("address");
  const signalText = text("signals");
  if (!address && !signalText) return null;
  if (!address || !signalText) {
    throw new Error(`The ${purpose} command needs both an address and signals.`);
  }
  const signals = {};
  signalText.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
    .forEach((line) => {
      const match = line.match(/^([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(\S+)$/);
      if (!match) {
        throw new Error(`The ${purpose} command has a bad line: “${line}”.`);
      }
      const raw = match[2];
      signals[match[1]] = Number.isFinite(Number(raw)) ? Number(raw) : raw;
    });
  const message = {
    bus: Number(text("bus") || 0),
    address,
    frequency_hz: Number(text("frequency") || 100),
    signals,
  };
  if (text("message")) message.message = text("message");
  if (text("counter")) message.counter_signal = text("counter");
  if (text("checksum")) message.checksum_signal = text("checksum");
  return message;
}

function limitsPayload() {
  const limits = {};
  const put = (key, id) => {
    const raw = byId(id).value.trim();
    if (raw !== "") limits[key] = Number(raw);
  };
  put("steer_max", "limit-steer-max");
  put("steer_delta_up", "limit-delta-up");
  put("steer_delta_down", "limit-delta-down");
  put("steer_driver_allowance", "limit-driver-allowance");
  put("steer_actuator_delay", "limit-actuator-delay");
  put("steer_limit_timer", "limit-steer-timer");
  put("accel_min", "limit-accel-min");
  put("accel_max", "limit-accel-max");
  return limits;
}

function tuningPayload() {
  const tuning = {};
  const kind = byId("tune-lat-kind").value;
  if (kind) {
    const lateral = {kind};
    const accel = byId("tune-lat-accel").value.trim();
    const friction = byId("tune-lat-friction").value.trim();
    if (accel !== "") lateral.max_lateral_accel = Number(accel);
    if (friction !== "") lateral.friction = Number(friction);
    tuning.lateral = lateral;
  }
  const gain = byId("tune-long-gains").value.trim();
  if (gain) {
    const ki = Number(gain);
    if (!Number.isFinite(ki)) {
      throw new Error("Longitudinal gain must be a number.");
    }
    // kp/kpBP moved under a `deprecated` group in opendbc's own schema and no
    // current brand's interface sets it; only ki is wired into generated code.
    tuning.longitudinal = {ki};
  }
  return tuning;
}

function hasManualInfo(info) {
  if (!info) return false;
  const specs = info.vehicle_specs || {};
  const engineering = info.engineering || {};
  return Object.values(specs).some((value) => value !== null && value !== "")
    || Object.keys(info.ecu_types || {}).length > 0
    || (info.signals || []).length > 0
    || (info.carstate || []).length > 0
    || Object.keys(info.actuation || {}).length > 0
    || Object.keys(info.limits || {}).length > 0
    || Object.keys(info.tuning || {}).length > 0
    || Object.keys(info.safety || {}).length > 0
    || Object.values(engineering).some((items) => (items || []).length > 0);
}

function populateManualInfo(info) {
  const data = info || {};
  const fileStatus = byId("vehicle-info-attached");
  fileStatus.textContent = hasManualInfo(data)
    ? "Saved manual facts loaded"
    : "Nothing loaded";
  fileStatus.classList.toggle("ready", hasManualInfo(data));
  const specs = data.vehicle_specs || {};
  byId("manual-mass").value = specs.mass_kg ?? "";
  byId("manual-wheelbase").value = specs.wheelbase_m ?? "";
  byId("manual-steer-ratio").value = specs.steer_ratio ?? "";
  byId("manual-package").value = specs.docs_package ?? "";
  byId("manual-harness").value = specs.harness ?? "";
  byId("manual-ecus").value = Object.entries(data.ecu_types || {})
    .map(([address, ecu]) => `${address} = ${ecu}`)
    .join("\n");
  const engineering = data.engineering || {};
  byId("manual-sources").value = (engineering.sources || []).join("\n");
  byId("manual-actuation").value = (engineering.actuation_notes || []).join("\n");
  byId("manual-safety").value = (engineering.safety_notes || []).join("\n");
  byId("manual-signal-list").replaceChildren();
  (data.signals || []).forEach(addSignalRow);

  byId("binding-list").replaceChildren();
  (data.carstate || []).forEach(addBindingRow);

  const actuation = data.actuation || {};
  populateCommand("lat", actuation.lateral);
  populateCommand("long", actuation.longitudinal);

  const limits = data.limits || {};
  const put = (id, value) => { byId(id).value = value ?? ""; };
  put("limit-steer-max", limits.steer_max);
  put("limit-delta-up", limits.steer_delta_up);
  put("limit-delta-down", limits.steer_delta_down);
  put("limit-driver-allowance", limits.steer_driver_allowance);
  put("limit-actuator-delay", limits.steer_actuator_delay);
  put("limit-steer-timer", limits.steer_limit_timer);
  put("limit-accel-min", limits.accel_min);
  put("limit-accel-max", limits.accel_max);

  const tuning = data.tuning || {};
  put("tune-lat-kind", tuning.lateral?.kind);
  put("tune-lat-accel", tuning.lateral?.max_lateral_accel);
  put("tune-lat-friction", tuning.lateral?.friction);
  byId("tune-long-gains").value = tuning.longitudinal?.ki ?? "";
  put("safety-model", data.safety?.model);
  put("safety-param", data.safety?.param);
}

function populateCommand(prefix, message) {
  const put = (id, value) => { byId(`${prefix}-${id}`).value = value ?? ""; };
  put("bus", message?.bus);
  put("address", message?.address);
  put("message", message?.message);
  put("frequency", message?.frequency_hz);
  put("counter", message?.counter_signal);
  put("checksum", message?.checksum_signal);
  byId(`${prefix}-signals`).value = Object.entries(message?.signals || {})
    .map(([name, value]) => `${name} = ${value}`)
    .join("\n");
}

function manualPayload() {
  // Several schema fields are intentionally not shown in the compact form.
  // Keep them when a complete vehicle-info.json was imported; editing one
  // visible field must not silently strip advanced facts from the file.
  const retained = JSON.parse(JSON.stringify(project?.manual_info || {}));
  const ecuTypes = {};
  lineValues("manual-ecus").forEach((line, index) => {
    const match = line.match(/^([^=,\s]+)\s*(?:=|,|\s)\s*([A-Za-z][A-Za-z0-9]*)$/);
    if (!match) {
      throw new Error(`ECU line ${index + 1} must look like 0x7E0 = engine.`);
    }
    if (systemStatus?.ecu_types && !systemStatus.ecu_types.includes(match[2])) {
      throw new Error(`Unknown OpenDBC ECU type “${match[2]}”.`);
    }
    if (Object.hasOwn(ecuTypes, match[1])) {
      throw new Error(`ECU address “${match[1]}” appears more than once.`);
    }
    ecuTypes[match[1]] = match[2];
  });

  const signals = [];
  // Scoped to its own list: CarState binding rows share this styling class,
  // and a document-wide query would try to read override fields out of them.
  rowsIn("manual-signal-list", ".manual-signal-row").forEach((row, index) => {
    const value = (selector) => row.querySelector(selector).value.trim();
    const address = value(".manual-signal-address");
    const name = value(".manual-signal-name");
    const start = value(".manual-signal-start");
    const meaningful = address || name || start;
    if (!meaningful) return;
    if (!address || !name || start === "") {
      throw new Error(`Known signal ${index + 1} needs address, start bit, and name.`);
    }
    const item = {
      bus: Number(value(".manual-signal-bus")),
      address,
      start_bit: Number(start),
      name,
    };
    const optional = {
      length: value(".manual-signal-length"),
      unit: value(".manual-signal-unit"),
      scale: value(".manual-signal-scale"),
      offset: value(".manual-signal-offset"),
      byte_order: value(".manual-signal-order"),
      signed: value(".manual-signal-signed"),
      carstate_target: value(".manual-signal-target"),
    };
    if (optional.length !== "") item.length = Number(optional.length);
    if (optional.unit) item.unit = optional.unit;
    if (optional.scale !== "") item.scale = Number(optional.scale);
    if (optional.offset !== "") item.offset = Number(optional.offset);
    if (optional.byte_order) item.byte_order = optional.byte_order;
    if (optional.signed !== "") item.signed = optional.signed === "true";
    if (optional.carstate_target) item.carstate_target = optional.carstate_target;
    signals.push(item);
  });

  const actuation = {};
  const lateral = commandPayload("lat", "steering");
  const longitudinal = commandPayload("long", "acceleration");
  if (lateral) actuation.lateral = {...(retained.actuation?.lateral || {}), ...lateral};
  if (longitudinal) {
    actuation.longitudinal = {
      ...(retained.actuation?.longitudinal || {}), ...longitudinal,
    };
  }

  const visibleLimits = [
    "steer_max", "steer_delta_up", "steer_delta_down",
    "steer_driver_allowance", "steer_actuator_delay", "steer_limit_timer",
    "accel_min", "accel_max",
  ];
  const limits = {...(retained.limits || {})};
  visibleLimits.forEach((key) => { delete limits[key]; });
  Object.assign(limits, limitsPayload());

  const enteredTuning = tuningPayload();
  const tuning = {};
  if (enteredTuning.lateral) {
    tuning.lateral = {...(retained.tuning?.lateral || {})};
    ["kind", "max_lateral_accel", "friction"].forEach((key) => {
      delete tuning.lateral[key];
    });
    Object.assign(tuning.lateral, enteredTuning.lateral);
  }
  if (enteredTuning.longitudinal) {
    tuning.longitudinal = {
      ...(retained.tuning?.longitudinal || {}), ...enteredTuning.longitudinal,
    };
  }

  const payload = {
    ...retained,
    schema_version: 1,
    vehicle_specs: {
      ...(retained.vehicle_specs || {}),
      mass_kg: optionalNumber("manual-mass"),
      wheelbase_m: optionalNumber("manual-wheelbase"),
      steer_ratio: optionalNumber("manual-steer-ratio"),
      docs_package: byId("manual-package").value.trim() || null,
      harness: byId("manual-harness").value.trim() || null,
    },
    ecu_types: ecuTypes,
    signals,
    carstate: bindingsPayload(),
    actuation,
    limits,
    tuning,
    engineering: {
      actuation_notes: lineValues("manual-actuation"),
      safety_notes: lineValues("manual-safety"),
      sources: lineValues("manual-sources"),
    },
  };
  const safetyModel = byId("safety-model").value.trim();
  if (safetyModel) {
    payload.safety = {
      model: safetyModel,
      param: Number(byId("safety-param").value.trim() || 0),
    };
  } else {
    delete payload.safety;
  }
  return payload;
}

function setStep(step, {force = false} = {}) {
  if (!stageOrder.includes(step)) return;
  if (!force && project) {
    if (step === "details" && !project.files.capture) {
      toast("Add a CAN log first.", "error");
      step = "source";
    }
    if (step === "analysis" && !project.analysis) {
      toast("Describe the vehicle and run analysis first.", "error");
      step = project.files.capture ? "details" : "source";
    }
    if (step === "port" && !project.port) {
      toast("Generate a port from the analysis first.", "error");
      step = project.analysis ? "analysis" : "details";
    }
  }
  currentStep = step;
  all("[data-panel]").forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.panel === step);
  });
  all("[data-step]").forEach((button) => {
    const active = button.dataset.step === step;
    button.classList.toggle("active", active);
    // Which of the four steps you are on is otherwise carried only by colour
    // and a lime bar, neither of which reaches a screen reader.
    if (active) {
      button.setAttribute("aria-current", "step");
    } else {
      button.removeAttribute("aria-current");
    }
  });
  document.querySelector(".main").scrollTo({top: 0, behavior: "smooth"});
}

function updateStepState() {
  const complete = {
    source: Boolean(project?.files?.capture),
    details: Boolean(project?.analysis),
    analysis: Boolean(project?.port),
    port: false,
  };
  all("[data-step]").forEach((button) => {
    button.classList.toggle("complete", complete[button.dataset.step]);
  });
}

/* Extra captures, merged onto the end of the first.

   One drive rarely exercises everything: indicators, reverse and blind-spot
   only appear in a recording where someone used them, an underdetermined
   checksum needs more variety than one drive gave, and the stock lane-keep
   drive that answers the control-path facts is a separate outing from the
   general one. Merging is laying them end to end, which keeps each message's
   real cadence -- interleaving would halve every period. */
function renderExtraCaptures() {
  const container = byId("extra-captures");
  container.replaceChildren();
  if (!project.files?.capture) return;

  const extras = project.files.extra_captures || [];
  extras.forEach((item) => {
    const row = document.createElement("div");
    row.className = "extra-capture";
    const copy = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = item.name;
    const small = document.createElement("small");
    small.textContent = `${formatBytes(item.size)} · merged after the first capture`;
    copy.append(strong, small);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "text-button extra-capture-remove";
    remove.textContent = "Remove";
    remove.setAttribute("aria-label", `Remove ${item.name}`);
    remove.addEventListener("click", async () => {
      try {
        project = await api(`/api/projects/${project.id}/drop_extra_capture`, {
          method: "POST",
          json: {stored_as: item.stored_as},
        });
        renderProject({allowNavigation: false});
        toast(`${item.name} removed. Run the decoder again.`, "success");
      } catch (error) {
        toast(error.message, "error");
      }
    });
    row.append(copy, remove);
    container.append(row);
  });

  const add = document.createElement("label");
  add.className = "text-button file-link extra-capture-add";
  add.append(document.createTextNode(
    extras.length ? "Merge another capture" : "Merge another capture of this car",
  ));
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".log,.txt,.csv,.gz,.bz2,.xz,.zst,.rlog";
  input.hidden = true;
  input.addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (file) upload("extra_capture", file);
    event.target.value = "";
  });
  add.append(input);
  const hint = document.createElement("small");
  hint.className = "extra-capture-hint";
  hint.textContent = extras.length
    ? `${extras.length} extra capture${extras.length > 1 ? "s" : ""} will be `
      + "laid end to end after the first."
    : "A second drive that used the indicators, reverse or the stock lane-keep "
      + "finds fields the first one could not.";
  container.append(add, hint);
}

function updateFileSummary() {
  const capture = project.files.capture;
  const summary = byId("capture-summary");
  const strong = summary.querySelector("strong");
  const small = summary.querySelector("small");
  if (capture) {
    strong.textContent = capture.name;
    small.textContent = `${formatBytes(capture.size)} · stored in this local project`;
  } else {
    strong.textContent = "No capture yet";
    small.textContent = "Return to step 1 to add one.";
  }
  // "Replace" is the wrong word for a row that has nothing in it.
  byId("capture-action").textContent = capture ? "Replace" : "Add one";
  const reference = project.files.reference;
  byId("reference-attached").textContent = reference
    ? `${reference.name} · ${formatBytes(reference.size)}`
    : "Nothing attached";
  byId("reference-attached").classList.toggle("ready", Boolean(reference));
  const firmware = project.files.firmware;
  byId("firmware-attached").textContent = firmware
    ? `${firmware.name} · ${formatBytes(firmware.size)}`
    : "Nothing attached";
  byId("firmware-attached").classList.toggle("ready", Boolean(firmware));
}

function evidenceRows(analysis) {
  return [
    ["ID", "CAN message map", `${analysis.messages} unique addresses across bus ${analysis.buses.join(", ") || "—"}`, analysis.messages],
    ["SG", "Candidate signal layout", "Bit positions, widths, byte order, signedness, scale, and confidence", analysis.signals],
    ["↗", "Reference correlations", "Fields matched to independently measured channels", analysis.named_signals],
    ["#", "Rolling counters", "Sequences used to verify message freshness", analysis.counters],
    ["✓", "Checksum formulas", `${analysis.uncertain_checksums} solution(s) still underdetermined`, analysis.checksums],
    ["FW", "Firmware fingerprint", "ECU diagnostic responses collected", analysis.firmware_responses],
  ];
}

function renderEvidence(analysis) {
  const container = byId("evidence-list");
  container.replaceChildren();
  evidenceRows(analysis).forEach(([icon, title, detail, value]) => {
    const row = document.createElement("div");
    row.className = "evidence-row";
    const mark = document.createElement("span");
    mark.className = "row-icon";
    mark.textContent = icon;
    const copy = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = title;
    const small = document.createElement("small");
    small.textContent = detail;
    copy.append(strong, small);
    const number = document.createElement("span");
    number.className = "row-value";
    number.textContent = String(value);
    row.append(mark, copy, number);
    container.append(row);
  });
}

function renderReview(container, items) {
  container.replaceChildren();
  (items || []).forEach((item, index) => {
    const row = document.createElement("div");
    row.className = "review-row";
    row.dataset.level = item.level || "review";
    const mark = document.createElement("span");
    mark.className = "review-dot";
    mark.textContent = item.level === "required" ? "!" : String(index + 1);
    const copy = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = item.title;
    copy.append(strong);
    // An empty <small> still occupies its line-height, leaving rows with no
    // detail looking padded out of line with the rest.
    if (item.detail) {
      const small = document.createElement("small");
      small.textContent = item.detail;
      copy.append(small);
    }
    row.append(mark, copy);
    container.append(row);
  });
}

// --------------------------------------------------------------------------
// Guided mode
// --------------------------------------------------------------------------

/**
 * The whole job, as a list of tasks, each answered from the project's actual
 * state. Deriving it rather than scripting a tour means the guide cannot get
 * out of step with what has really been done -- including work done before
 * guided mode was ever switched on, or in a previous session.
 */
function guideTasks() {
  if (!project) return [];
  const files = project.files || {};
  const form = project.form || {};
  const vehicle = project.vehicle || {};
  const analysis = project.analysis;
  const coverage = analysis?.coverage;
  const missing = (coverage?.items || []).filter(
    (item) => item.status === "missing" && item.mandatory,
  );
  const missingRead = missing.filter((item) => item.need === "read");

  const info = project.manual_info || {};
  const essential = systemStatus?.essential_ecu_types || ["engine", "eps", "abs"];
  const hasEssentialEcu = Object.values(info.ecu_types || {})
    .some((type) => essential.includes(type));
  const validation = project.port?.validation;
  const impossible = (validation?.findings || [])
    .filter((finding) => finding.level === "error");
  const declaresControl = Boolean(
    (info.actuation || {}).lateral || (info.actuation || {}).longitudinal,
  );

  return [
    {
      key: "capture",
      title: "Get some CAN data",
      detail: "Start with the demo if you have no car to hand — it runs the "
        + "whole workflow on a fake drive in about twenty seconds.",
      why: "Everything else reads from this recording.",
      step: "source",
      target: "demo-button",
      action: "Take me there",
      help: "hardware",
      helpLabel: "What hardware do I need?",
      done: Boolean(files.capture),
    },
    {
      key: "reference",
      title: "Attach a reference recording",
      detail: "A CSV of GPS speed, OBD-II values, or anything measured with "
        + "timestamps. Without it AutoDistill can find the fields but cannot "
        + "tell you what any of them mean.",
      why: "This single file is the difference between a list of anonymous "
        + "numbers and a working CarState.",
      step: "details",
      target: "reference-upload",
      action: "Attach one",
      optional: true,
      done: Boolean(files.reference),
    },
    {
      key: "name",
      title: "Name the car",
      detail: "A human-readable name and a lowercase package name, which "
        + "becomes the opendbc/car/<brand>/ directory.",
      why: "The generated package is named after these.",
      step: "details",
      target: "vehicle-name",
      action: "Fill them in",
      done: Boolean((form.name || vehicle.name) && (form.brand || vehicle.brand)),
    },
    {
      key: "analyse",
      title: "Run the decoder",
      detail: "This finds the fields, the counters, and the checksums, and "
        + "verifies each checksum against every frame you recorded.",
      why: "Until this runs there is nothing to bind signals to.",
      step: "details",
      target: "analyse-button",
      action: "Run it",
      done: Boolean(analysis),
    },
    {
      key: "firmware",
      title: "Collect ECU firmware versions",
      detail: "A guarded, read-only diagnostic probe, done while parked. "
        + "openpilot fingerprints most cars by firmware.",
      why: "Without it your car may be indistinguishable from a similar one.",
      step: "analysis",
      target: "probe-card",
      action: "Open the probe",
      help: "probe",
      helpLabel: "How do I set the hardware up for this?",
      optional: true,
      done: Boolean(analysis && analysis.firmware_responses > 0),
    },
    {
      key: "identify-ecu",
      title: "Identify one of the modules that answered",
      detail: "Collecting firmware is only half of it. openpilot fingerprints "
        + `on ${essential.slice(0, 3).join(", ")} and a couple of others; a `
        + "table of unidentified modules is left out entirely, because it "
        + "would match every car in opendbc rather than yours.",
      why: "Without one identified, your firmware is recorded but never used.",
      step: "details",
      target: "manual-ecus",
      action: "Identify one",
      help: "ecus",
      helpLabel: "How do I tell which module is which?",
      optional: true,
      // Firmware answered, but nothing openpilot can fingerprint on.
      done: hasEssentialEcu,
      blocked: !(analysis && analysis.firmware_responses > 0),
    },
    {
      key: "bind",
      title: missingRead.length
        ? `Bind ${missingRead.length} more CarState field${missingRead.length > 1 ? "s" : ""}`
        : "Bind the CarState fields",
      detail: missingRead.length
        ? `Next up: ${missingRead[0].title.toLowerCase()}. ${missingRead[0].how}`
        : "Tell openpilot which recovered signal is which field.",
      why: "openpilot will not engage without these, and no analysis can "
        + "work them out for you.",
      step: "analysis",
      target: "binding-list",
      action: "Bind them",
      done: Boolean(coverage?.read?.complete),
      blocked: !analysis,
    },
    {
      key: "port",
      title: "Generate the port",
      detail: "Writes the whole opendbc package: values, fingerprints, "
        + "CarState, DBCs, and the verified checksum code.",
      why: "This is the thing you came for.",
      step: "analysis",
      target: "generate-button",
      action: "Generate it",
      done: Boolean(project.port),
      blocked: !analysis,
    },
    {
      key: "fix-bindings",
      title: impossible.length === 0
        ? "Bindings produce believable values"
        : impossible.length === 1
          ? "One binding cannot be right"
          : `${impossible.length} bindings cannot be right`,
      detail: impossible.length
        ? `${impossible[0].target}: ${impossible[0].message}`
        : "Replaying your capture through the port found impossible values.",
      why: "These were checked against your own recording, so they are wrong "
        + "regardless of what the car does. The port will import and run "
        + "anyway, which is what makes them worth catching here.",
      step: "port",
      target: "validation-card",
      action: "Show me",
      // Only ever appears when there is something real to fix. Requires the
      // port, or "no impossible values" is vacuously true and a fresh project
      // shows this ticked off before anything has been validated at all.
      done: Boolean(project.port) && impossible.length === 0,
      blocked: !project.port,
    },
    {
      key: "harness",
      title: "Name the comma harness",
      detail: "Which harness fits this car. openpilot's own docs test refuses "
        + "a supported platform without one, and it is how anyone else knows "
        + "what to buy.",
      why: "A control port cannot be upstreamed without it.",
      step: "analysis",
      target: "manual-harness",
      action: "Name it",
      optional: true,
      done: Boolean((info.vehicle_specs || {}).harness),
      // Only matters once the port actually controls something.
      blocked: !declaresControl,
    },
    {
      key: "control",
      title: "Add the control facts",
      detail: "Which message steers the car, what limits are safe, and which "
        + "opendbc safety model applies. All of it from your own bench work.",
      why: "Supplying these turns the inert controller into a working one. "
        + "The port is still never marked safe for control.",
      step: "analysis",
      target: "lat-address",
      action: "Fill them in",
      optional: true,
      done: Boolean(coverage?.lateral?.complete),
      blocked: !analysis,
    },
    {
      key: "install",
      title: "Install it into opendbc",
      detail: "Copies the package into a local opendbc checkout and registers "
        + "it in the central platform list.",
      why: "Then you can construct the interface and check it reads the car.",
      step: "port",
      target: "opendbc-path",
      action: "Install it",
      optional: true,
      done: Boolean(project.installed_to),
      blocked: !project.port,
    },
  ];
}

function guideCurrent(tasks) {
  const skipped = JSON.parse(localStorage.getItem("autodistill-skipped") || "[]");
  return tasks.find(
    (task) => !task.done && !task.blocked && !skipped.includes(task.key),
  );
}

function spotlight(target) {
  const element = typeof target === "string" ? byId(target) : target;
  if (!element) return;
  element.scrollIntoView({behavior: "smooth", block: "center"});
  // A brief ring rather than a permanent marker: it answers "which one?" and
  // then gets out of the way.
  element.classList.remove("spotlight");
  void element.offsetWidth;  // restart the animation if it is already running
  element.classList.add("spotlight");
  window.setTimeout(() => element.classList.remove("spotlight"), 2600);
  if (element.tagName === "INPUT" || element.tagName === "BUTTON") {
    element.focus({preventScroll: true});
  }
}

function renderGuide() {
  const on = localStorage.getItem("autodistill-guide") !== "off";
  byId("guide").classList.toggle("hidden", !on);
  byId("guide-toggle").classList.toggle("active", on);
  byId("guide-toggle").setAttribute("aria-pressed", String(on));
  if (!on || !project) return;

  const tasks = guideTasks();
  const required = tasks.filter((task) => !task.optional);
  const done = required.filter((task) => task.done).length;
  byId("guide-bar").style.width =
    `${required.length ? (done / required.length) * 100 : 0}%`;
  byId("guide-count").textContent =
    `${done} of ${required.length} essential steps done`;

  const current = guideCurrent(tasks);
  // The body is a live region, and this runs on every poll while a job is
  // running. Rewriting identical text would have a screen reader announce the
  // same instruction every 900ms, so only touch it when the step changes.
  const changed = (current?.key || "") !== guideStepKey;
  guideStepKey = current?.key || "";
  byId("guide-body").classList.toggle("hidden", !current);
  if (current && changed) {
    byId("guide-title").textContent = current.title;
    byId("guide-detail").textContent = current.detail;
    byId("guide-why").textContent = current.why;
    const action = byId("guide-action");
    action.textContent = current.action;
    action.classList.remove("hidden");
    action.onclick = () => {
      setStep(current.step, {force: true});
      window.setTimeout(() => spotlight(current.target), 260);
    };
    // Steps needing hardware get the walkthrough for it in the panel itself:
    // "connect a panda" is not a useful instruction to someone who has never
    // seen one.
    const help = byId("guide-help");
    help.classList.toggle("hidden", !current.help);
    if (current.help) {
      help.textContent = current.helpLabel;
      help.onclick = () => openHelp(current.help);
    }
    const skip = byId("guide-skip");
    skip.classList.toggle("hidden", !current.optional);
    skip.onclick = () => {
      const skipped = JSON.parse(
        localStorage.getItem("autodistill-skipped") || "[]",
      );
      skipped.push(current.key);
      localStorage.setItem("autodistill-skipped", JSON.stringify(skipped));
      renderGuide();
    };
  } else if (!current && changed) {
    // Ending on "nothing left" would imply the port is finished, which is the
    // one impression this tool must never leave. Name what only a person can
    // do instead.
    byId("guide-title").textContent = "Everything AutoDistill can do is done";
    byId("guide-detail").textContent =
      "What remains cannot be automated: check each signal against the car in "
      + "Cabana, bench-test any message you intend to send, write and get "
      + "review for the safety model in opendbc/safety, and test on a closed "
      + "course before anywhere else.";
    byId("guide-why").textContent =
      "The port is marked safe_for_control: false and stays that way. That is "
      + "not a box left unticked -- it records that nobody has driven this yet.";
    byId("guide-action").classList.add("hidden");
    byId("guide-skip").classList.add("hidden");
    byId("guide-help").classList.add("hidden");
    byId("guide-body").classList.remove("hidden");
  }

  const list = byId("guide-list");
  list.replaceChildren(...tasks.map((task) => {
    const row = document.createElement("li");
    const item = document.createElement("button");
    item.type = "button";
    item.className = [
      "guide-item",
      task.done ? "done" : "",
      task === current ? "current" : "",
      task.optional ? "optional" : "",
    ].filter(Boolean).join(" ");
    item.textContent = task.title;
    item.disabled = Boolean(task.blocked) && !task.done;
    if (task === current) item.setAttribute("aria-current", "step");
    item.addEventListener("click", () => {
      setStep(task.step, {force: true});
      window.setTimeout(() => spotlight(task.target), 260);
    });
    row.append(item);
    return row;
  }));
}

const COVERAGE_LEVELS = [
  ["read", "Read the car", "Dashcam and logging only"],
  ["lateral", "Steer the car", "Adds a steering controller"],
  ["longitudinal", "Drive the car", "Adds gas and brake"],
];

//: Which control defines each requirement. Every `ret.*` key is a CarState
//: binding instead, which has no fixed control of its own -- the row is made
//: on demand with the field already chosen.
//:
//: Deliberately partial. `specs.centerToFront` and `specs.tireStiffnessFactor`
//: are computed rather than typed, and cruise buttons have no field of their
//: own yet, so those rows get no link rather than one that goes nowhere.
const COVERAGE_INPUTS = {
  "specs.mass": "manual-mass",
  "specs.wheelbase": "manual-wheelbase",
  "specs.steerRatio": "manual-steer-ratio",
  "specs.harness": "manual-harness",
  "control.lateral": "lat-address",
  "control.longitudinal": "long-address",
  "limits.steer_max": "limit-steer-max",
  "limits.steer_delta_up": "limit-delta-up",
  "limits.steer_delta_down": "limit-delta-down",
  "limits.steer_driver_allowance": "limit-driver-allowance",
  "limits.steer_actuator_delay": "limit-actuator-delay",
  "tuning.lateral": "tune-lat-kind",
  "tuning.longitudinal": "tune-long-gains",
  "safety.model": "safety-model",
};

function canDefine(key) {
  return key.startsWith("ret.") || Boolean(COVERAGE_INPUTS[key]);
}

/* Take someone from "this is missing" to the box they type it into.

   The list and the fields are on the same step now, but the fields are inside
   a collapsed block a long way down it, and a CarState binding has no box
   until one is added -- so for those this makes the row, with the field
   already selected, and leaves the message and signal to be chosen. */
function defineRequirement(key) {
  const facts = byId("port-facts");
  facts.open = true;

  if (!key.startsWith("ret.")) {
    spotlight(COVERAGE_INPUTS[key]);
    return;
  }
  const existing = rowsIn("binding-list", ".binding-row").find(
    (row) => row.querySelector(".binding-target")?.value === key,
  );
  const row = existing || addBindingRow({target: key});
  spotlight(row);
  // The message is the next choice to make, and the one the row cannot guess.
  row.querySelector(".binding-message")?.focus();
  if (!existing) queueDetailsSave();
}

function renderCoverage(coverage) {
  const card = byId("coverage-card");
  card.classList.toggle("hidden", !coverage || !coverage.items);
  if (!coverage || !coverage.items) return;

  const levels = byId("coverage-levels");
  levels.replaceChildren();
  // Each level contains the one before it -- steering needs everything reading
  // needs, plus more -- so a later level can show a longer bar while being
  // further from usable: "Steer 10/25" beside "Read 3/16" reads as steering
  // being the closer of the two, when steering cannot happen at all until
  // reading is finished. The counts stay as they are, since they are the
  // honest answer to "what does this level need?"; what changes is that a
  // level still gated by an earlier one says so and is drawn as unavailable.
  let gate = "";
  COVERAGE_LEVELS.forEach(([key, title, detail]) => {
    const level = coverage[key] || {met: 0, total: 0, complete: false};
    const locked = Boolean(gate);
    const row = document.createElement("div");
    row.className = [
      "coverage-level",
      level.complete ? "complete" : "",
      locked ? "locked" : "",
    ].filter(Boolean).join(" ");
    const copy = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = title;
    const small = document.createElement("small");
    small.textContent = locked ? `${detail} · after ${gate}` : detail;
    if (!level.complete && !gate) gate = title;
    copy.append(strong, small);
    const bar = document.createElement("div");
    bar.className = "coverage-bar";
    const fill = document.createElement("i");
    const share = level.total ? (level.met / level.total) * 100 : 0;
    fill.style.width = `${share.toFixed(0)}%`;
    bar.append(fill);
    const count = document.createElement("span");
    count.className = "coverage-count";
    count.textContent = level.complete
      ? "complete" : `${level.met}/${level.total}`;
    row.append(copy, bar, count);
    levels.append(row);
  });

  const missing = coverage.items.filter(
    (item) => item.status === "missing" && item.mandatory,
  );

  // Each of these says "capture the stock system" on its own, so the list
  // reads as that many separate expeditions. It is one drive.
  const fromOneDrive = (systemStatus?.stock_capture_answers || []).filter(
    (key) => missing.some((item) => item.key === key),
  );
  const hint = byId("coverage-hint");
  hint.classList.toggle("hidden", fromOneDrive.length < 2);
  if (fromOneDrive.length >= 2) {
    hint.textContent =
      `${fromOneDrive.length} of these come from one recording: drive with the `
      + "car's own lane-keep active, capture both buses, and the command "
      + "message, its limits and its delay are all in that file.";
  }
  const container = byId("coverage-missing");
  container.replaceChildren();
  if (!missing.length) {
    const done = document.createElement("p");
    done.className = "coverage-done";
    done.textContent =
      "Every requirement is supplied. Review each one before driving anything.";
    container.append(done);
    return;
  }
  missing.forEach((item) => {
    const row = document.createElement("div");
    row.className = "coverage-row";
    const kind = document.createElement("span");
    kind.className = "coverage-kind";
    kind.textContent = item.kind;
    const copy = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = item.title;
    const small = document.createElement("small");
    small.textContent = `${item.key} — ${item.detail}`;
    copy.append(strong, small);
    // The procedure for getting this fact, one click away. Inline would make
    // the list unscannable; a link elsewhere would make it unfindable.
    const actions = document.createElement("div");
    actions.className = "coverage-actions";
    if (item.how) {
      const how = document.createElement("details");
      how.className = "coverage-how";
      const summary = document.createElement("summary");
      summary.append(
        document.createTextNode("How to find it"),
        Object.assign(document.createElement("span"), {className: "chevron"}),
      );
      const text = document.createElement("p");
      text.textContent = item.how;
      how.append(summary, text);
      actions.append(how);
    }
    // "How to find it" explains the measurement; this goes to where the answer
    // is typed. Both matter: knowing the procedure and then hunting for the
    // box is most of what made filling these in tedious.
    if (canDefine(item.key)) {
      const define = document.createElement("button");
      define.type = "button";
      define.className = "text-button coverage-define";
      define.textContent = item.key.startsWith("ret.")
        ? "Bind it →" : "Fill it in →";
      define.setAttribute("aria-label", `Define ${item.title}`);
      define.addEventListener("click", () => defineRequirement(item.key));
      actions.append(define);
    }
    if (actions.children.length) copy.append(actions);
    const need = document.createElement("span");
    need.className = "coverage-need";
    need.textContent = item.need;
    row.append(kind, copy, need);
    container.append(row);
  });
}

async function loadInventory() {
  if (!project?.analysis) {
    inventory = [];
  } else {
    try {
      const payload = await api(`/api/projects/${project.id}/inventory`);
      inventory = payload.messages || [];
    } catch (_) {
      inventory = [];  // not analysed yet; the dropdowns say so
    }
  }
  // Rebuild in place so a row someone is halfway through editing keeps its
  // selections when the message list finally arrives.
  rowsIn("binding-list", ".binding-row").forEach((row) => row.refreshOptions?.());
}

function evidenceCoverage(analysis) {
  let score = 0;
  if (analysis.messages) score += 20;
  if (analysis.signals) score += 15;
  if (analysis.named_signals) score += 25;
  if (analysis.counters) score += 10;
  if (analysis.checksums) score += 15;
  if (analysis.firmware_responses) score += 10;
  if (!analysis.uncertain_checksums) score += 5;
  return Math.min(score, 100);
}

function artifactUrl(name) {
  return `/api/projects/${project.id}/download/${name}`;
}

async function downloadArtifact(name) {
  try {
    const response = await fetch(artifactUrl(name), {
      headers: {"X-AutoDistill-Token": apiToken},
    });
    if (!response.ok) {
      let message = "Download failed.";
      try {
        const payload = await response.json();
        message = payload.error || message;
      } catch (_) {
        // A non-JSON server error still gets the generic message.
      }
      throw new Error(message);
    }
    const disposition = response.headers.get("content-disposition") || "";
    const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/);
    const filename = encoded ? decodeURIComponent(encoded[1]) : name;
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    toast(error.message, "error");
  }
}

//: What the empty step-3 screen should do when its button is pressed. Set by
//: renderAnalysis, because which action it offers depends on what is missing.
let analysisEmptyAction = null;

/* Why the decoder has not run, in the order the steps happen.

   This screen is reached by opening step 3 before decoding -- from the
   sidebar, or by coming back to a half-finished project -- and it used to say
   "Ready when you are" over a radar graphic and a dial reading "—", which
   reads as either stuck or still loading. It is neither: it is waiting on
   something the person has to do, and the only useful thing it can say is
   which one. */
function analysisBlocker() {
  if (project.status === "running") {
    return {
      title: "Decoding now",
      detail: "Working through the capture. This panel fills in by itself when "
        + "it finishes; you do not need to stay on it.",
      label: "Decoding…",
      run: null,
    };
  }
  // The most likely way to be looking at this screen at all. A run that failed
  // announced itself once, in a toast, and toasts go away -- so what was left
  // was this panel, saying nothing, on a step that cannot otherwise be opened
  // without an analysis.
  if (project.status === "error") {
    return {
      title: "The last run did not finish",
      detail: project.status_message
        || "The decoder stopped before it produced anything.",
      label: "Try again",
      run: () => runAction("analyse", vehiclePayload(), {step: "analysis"}),
    };
  }
  if (!project.files?.capture) {
    return {
      title: "No drive to decode yet",
      detail: "Step 1 is where a capture comes from: the guided demo, a log you "
        + "already have, or a passive recording from hardware.",
      label: "Get CAN data",
      run: () => setStep("source"),
    };
  }
  try {
    vehiclePayload();
  } catch (error) {
    return {
      title: "Check the vehicle details",
      detail: `${error.message} The decoder writes these into the generated `
        + "package, so they have to be set before it runs.",
      label: "Review details",
      run: () => setStep("details"),
    };
  }
  return {
    title: "Ready to decode",
    detail: `${project.files.capture.name} is loaded. Reading it finds the `
      + "messages, signals, counters and checksums. Nothing is transmitted to "
      + "the car, and nothing leaves this computer.",
    label: "Decode this drive",
    run: () => runAction("analyse", vehiclePayload(), {step: "analysis"}),
  };
}

function renderAnalysis() {
  const analysis = project.analysis;
  byId("analysis-empty").classList.toggle("hidden", Boolean(analysis));
  byId("analysis-results").classList.toggle("hidden", !analysis);
  byId("generate-button").disabled = !analysis || project.status === "running";
  byId("generate-button").title = analysis
    ? ""
    : "Decode the drive first — there is nothing to build a port from yet.";
  // A dial reading "—" over the word COVERAGE looks like a figure that failed
  // to load. There is no coverage to report until the decoder has run.
  byId("readiness-dial").classList.toggle("hidden", !analysis);
  if (!analysis) {
    byId("readiness-value").textContent = "—";
    const blocker = analysisBlocker();
    byId("analysis-empty-title").textContent = blocker.title;
    byId("analysis-empty-detail").textContent = blocker.detail;
    const action = byId("analysis-empty-action");
    action.textContent = blocker.label;
    action.disabled = !blocker.run;
    analysisEmptyAction = blocker.run;
    byId("analysis-lede").textContent =
      "Run the decoder to turn the raw traffic into messages, signals, "
      + "counters, checksums, and a fingerprint.";
    return;
  }
  byId("analysis-lede").textContent =
    `AutoDistill decoded buses ${analysis.buses.join(", ") || "—"} and separated automatic evidence from the work that still needs engineering judgment.`;
  byId("readiness-value").textContent = `${evidenceCoverage(analysis)}%`;
  byId("metric-messages").textContent = analysis.messages;
  byId("metric-signals").textContent = analysis.signals;
  byId("metric-named").textContent = analysis.named_signals;
  byId("metric-checksums").textContent = analysis.checksums;
  byId("metric-counters").textContent = analysis.counters;
  byId("metric-firmware").textContent = analysis.firmware_responses;
  renderEvidence(analysis);
  renderCoverage(analysis.coverage);
  renderReview(byId("review-list"), project.review);
  byId("probe-card").classList.toggle("hidden", analysis.firmware_responses > 0);
  byId("download-vehicle-info").classList.toggle(
    "hidden", !hasManualInfo(project.manual_info),
  );
}

function packageRows(port) {
  const evidence = port.evidence || {};
  const rows = [
    ["PY", "OpenDBC car package", "Interface, CarState, values, fingerprints, and a no-output controller"],
    ["DBC", "Generated DBC files", `${Object.keys(evidence.mapped_carstate_fields || {}).length || evidence.mapped_carstate_fields?.length || 0} CarState fields mapped`],
    ["FP", "Vehicle fingerprint", `${evidence.main_bus_fingerprint_messages || 0} main-bus messages`],
    ["CRC", "Verified integrity code", `${evidence.solved_checksums || 0} checksums and ${evidence.rolling_counters || 0} counters`],
    ["SAFE", "Safety gate", "dashcamOnly enabled, noOutput safety model, controller sends nothing"],
  ];
  if (port.manual_input?.provided) {
    rows.splice(4, 0, [
      "HUM",
      "Human-supplied knowledge",
      `${port.manual_input.signal_overrides || 0} signal overrides and ${port.manual_input.ecu_types_supplied || 0} ECU types, preserved with sources`,
    ]);
  }
  return rows;
}

function renderValidation(validation) {
  const card = byId("validation-card");
  const has = validation && validation.checked_fields;
  card.classList.toggle("hidden", !has);
  if (!has) return;

  // Notes are "you did not exercise this", which is normal and not a defect.
  const problems = (validation.findings || []).filter((f) => f.level !== "note");
  card.classList.toggle("failed", !validation.ok);
  byId("validation-mark").textContent = validation.ok ? "✓" : "!";
  byId("validation-title").textContent = validation.ok
    ? "Every bound field produced believable values"
    : `${problems.length} binding${problems.length > 1 ? "s" : ""} produced values a real car cannot`;
  byId("validation-detail").textContent = validation.ok
    ? `${validation.checked_fields} fields replayed against your capture. That is `
      + "not proof they are right, only that none is provably wrong."
    : "Replayed against your own capture. Fix these before trusting the port.";

  const list = byId("validation-list");
  list.replaceChildren(...problems.map((finding) => {
    const row = document.createElement("div");
    row.className = `validation-row ${finding.level}`;
    const target = document.createElement("code");
    target.textContent = finding.target;
    const text = document.createElement("span");
    text.textContent = finding.message;
    row.append(target, text);
    return row;
  }));
}

function renderPort() {
  const port = project.port;
  byId("port-empty").classList.toggle("hidden", Boolean(port));
  byId("port-results").classList.toggle("hidden", !port);
  if (!port) return;
  renderValidation(port.validation);
  byId("port-title").textContent = `${project.vehicle.name} bootstrap is ready`;
  const container = byId("package-list");
  container.replaceChildren();
  packageRows(port).forEach(([icon, title, detail]) => {
    const row = document.createElement("div");
    row.className = "package-row";
    const mark = document.createElement("span");
    mark.className = "row-icon";
    mark.textContent = icon;
    const copy = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = title;
    const small = document.createElement("small");
    small.textContent = detail;
    copy.append(strong, small);
    row.append(mark, copy);
    container.append(row);
  });
  // Titling each of these "Human task 3" put a number where the instruction
  // should be and demoted the only useful text to small grey. The first
  // sentence is the action; whatever follows is the reason.
  const items = (port.human_required || []).map((text) => {
    // A full stop only ends a sentence when a space and a capital follow it,
    // which keeps "carstate.py" and "vehicle_info.json" in one piece.
    const split = text.match(/^(.+?[.!?])\s+(?=[A-Z0-9])(.*)$/s);
    return {
      level: "required",
      title: split ? split[1] : text,
      detail: split ? split[2] : "",
    };
  });
  renderReview(byId("port-review-list"), items);
}

function updateBusyState() {
  const busy = project.status === "running";
  byId("status-banner").classList.toggle("hidden", !busy);
  byId("status-title").textContent = busy ? project.status_message : "Ready";
  byId("status-message").textContent =
    busy ? "This page can stay open; your files remain on this machine." : "";
  byId("save-state").textContent = busy ? "Working…" : "Saved locally";
  [
    "demo-button", "capture-button", "analyse-button", "generate-button",
    "probe-button", "install-button", "new-project-button",
  ].forEach((id) => {
    const button = byId(id);
    if (button) button.disabled = busy || (id === "generate-button" && !project.analysis);
  });
  if (busy) {
    startPolling();
  } else {
    stopPolling();
  }
}

function restoreDetailsForm() {
  // `form` is the draft saved as the user types; `vehicle` is what survived
  // validation the last time analysis ran. Prefer the draft, fall back to the
  // validated values, and never overwrite the field someone is typing into.
  const draft = project.form || {};
  const vehicle = project.vehicle || {};
  const put = (id, value) => {
    if (value === undefined || value === null || value === "") return;
    if (document.activeElement === byId(id)) return;
    byId(id).value = String(value);
  };
  put("vehicle-name", draft.name || vehicle.name);
  put("vehicle-brand", draft.brand || vehicle.brand);
  if (draft.brand || vehicle.brand) brandWasEdited = true;
  put("vehicle-bus", draft.bus ?? vehicle.bus);
  put("min-frames", draft.min_frames);
  if (document.activeElement !== byId("strict-mode")) {
    byId("strict-mode").checked = Boolean(draft.strict);
  }
}

function detailsDraft() {
  return {
    name: byId("vehicle-name").value.trim(),
    brand: byId("vehicle-brand").value.trim(),
    bus: Number(byId("vehicle-bus").value),
    min_frames: Number(byId("min-frames").value),
    strict: byId("strict-mode").checked,
    manual_info: manualPayload(),
  };
}

async function saveDetails() {
  if (!project) return;
  let draft;
  try {
    draft = detailsDraft();
  } catch (error) {
    // A half-typed ECU line is normal while editing; say so quietly in the
    // status pill rather than interrupting with a toast on every keystroke.
    byId("save-state").textContent = `Not saved: ${error.message}`;
    return;
  }
  try {
    byId("save-state").textContent = "Saving…";
    const saved = await api(`/api/projects/${project.id}/details`, {
      method: "POST",
      json: draft,
    });
    // Adopt the stored state without re-rendering: the form is the source of
    // truth right now, and repainting it would fight whoever is typing.
    if (project && project.id === saved.id) project = saved;
    byId("save-state").textContent = "Saved locally";
  } catch (error) {
    byId("save-state").textContent = `Not saved: ${error.message}`;
  }
}

function queueDetailsSave() {
  window.clearTimeout(saveTimer);
  saveTimer = window.setTimeout(saveDetails, 700);
}

function renderProject({allowNavigation = true} = {}) {
  if (!project) return;
  byId("project-name").textContent = project.name;
  if (manualProjectId !== project.id) {
    populateManualInfo(project.manual_info);
    manualProjectId = project.id;
  }
  // Reload the message list when the project changes or the decoder has run
  // again, since a re-run can rename signals the bindings point at.
  const stamp = project.analysis
    ? `${project.id}:${project.analysis.messages}:${project.analysis.signals}`
    : `${project.id}:none`;
  if (stamp !== inventoryStamp) {
    inventoryStamp = stamp;
    loadInventory();
  }
  updateStepState();
  updateFileSummary();
  renderExtraCaptures();
  restoreDetailsForm();
  renderAnalysis();
  renderPort();
  updateBusyState();
  renderGuide();
  if (project.status === "error" && project.status_message !== lastErrorMessage) {
    lastErrorMessage = project.status_message;
    toast(project.status_message, "error");
  }
  if (
    allowNavigation &&
    lastProjectStage &&
    lastProjectStage !== project.stage &&
    project.status !== "running"
  ) {
    setStep(project.stage, {force: true});
  }
  lastProjectStage = project.stage;
}

async function refreshProject({allowNavigation = true} = {}) {
  if (!project) return;
  try {
    project = await api(`/api/projects/${project.id}`);
    renderProject({allowNavigation});
  } catch (error) {
    stopPolling();
    toast(error.message, "error");
  }
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = window.setInterval(() => refreshProject(), 900);
}

function stopPolling() {
  if (pollTimer) window.clearInterval(pollTimer);
  pollTimer = null;
}

async function createProject(name = "My vehicle") {
  window.clearTimeout(saveTimer);
  project = await api("/api/projects", {method: "POST", json: {name}});
  localStorage.setItem("autodistill-project", project.id);
  lastProjectStage = null;
  brandWasEdited = false;
  byId("vehicle-name").value = "";
  byId("vehicle-brand").value = "";
  renderProject({allowNavigation: false});
  setStep("source", {force: true});
  return project;
}

async function boot() {
  try {
    const [status, projectsPayload] = await Promise.all([
      api("/api/status"),
      api("/api/projects"),
    ]);
    systemStatus = status;
    renderHardwareStatus();
    renderSafetyModels();
    const projects = projectsPayload.projects || [];
    const savedId = localStorage.getItem("autodistill-project");
    project = projects.find((item) => item.id === savedId) || projects[0] || null;
    if (!project) project = await createProject();
    localStorage.setItem("autodistill-project", project.id);
    lastProjectStage = project.stage;
    renderProject({allowNavigation: false});
    setStep(openingStep(project), {force: true});
    if (project.status === "running") startPolling();
  } catch (error) {
    toast(`Could not start the local app: ${error.message}`, "error");
    byId("project-name").textContent = "Connection error";
  }
}

function renderSafetyModels() {
  const list = byId("safety-models");
  list.replaceChildren(...(systemStatus?.safety_models || []).map((name) => {
    const option = document.createElement("option");
    option.value = name;
    return option;
  }));
}

function renderHardwareStatus() {
  if (!systemStatus) return;
  const bits = [];
  if (systemStatus.panda_ready) bits.push("panda USB support ready");
  if (systemStatus.can_interfaces.length) {
    bits.push(`CAN: ${systemStatus.can_interfaces.join(", ")}`);
  } else if (systemStatus.socketcan_ready) {
    bits.push("SocketCAN supported; no CAN interface detected yet");
  }
  const element = byId("hardware-status");
  element.textContent = bits.join(" · ") || "No capture hardware detected; uploads still work.";
  element.classList.toggle(
    "ready",
    systemStatus.panda_ready || systemStatus.can_interfaces.length > 0,
  );
}

async function runAction(action, payload = {}, {step = null} = {}) {
  if (!project || project.status === "running") return;
  try {
    project = await api(`/api/projects/${project.id}/${action}`, {
      method: "POST",
      json: payload,
    });
    renderProject({allowNavigation: false});
    if (step) setStep(step, {force: true});
    startPolling();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function upload(kind, file) {
  if (!file || !project || project.status === "running") return;
  try {
    byId("save-state").textContent = `Uploading ${file.name}…`;
    project = await api(`/api/projects/${project.id}/upload/${kind}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Filename": encodeURIComponent(file.name),
      },
      body: file,
    });
    renderProject({allowNavigation: false});
    toast(`${file.name} is ready.`);
    if (kind === "capture") setStep("details", {force: true});
  } catch (error) {
    toast(error.message, "error");
    byId("save-state").textContent = "Upload failed";
  }
}

async function importVehicleInfo(file) {
  if (!file || !project || project.status === "running") return;
  try {
    if (file.size > 1024 * 1024) {
      throw new Error("vehicle-info.json must be smaller than 1 MiB.");
    }
    let manualInfo;
    try {
      manualInfo = JSON.parse(await file.text());
    } catch (error) {
      throw new Error(`Could not read ${file.name} as JSON: ${error.message}`);
    }
    if (!manualInfo || Array.isArray(manualInfo) || typeof manualInfo !== "object") {
      throw new Error("vehicle-info.json must contain one JSON object.");
    }
    window.clearTimeout(saveTimer);
    byId("vehicle-info-attached").textContent = `Validating ${file.name}…`;
    project = await api(`/api/projects/${project.id}/details`, {
      method: "POST",
      json: {
        name: byId("vehicle-name").value.trim(),
        brand: byId("vehicle-brand").value.trim(),
        bus: Number(byId("vehicle-bus").value),
        min_frames: Number(byId("min-frames").value),
        strict: byId("strict-mode").checked,
        manual_info: manualInfo,
      },
    });
    populateManualInfo(project.manual_info);
    manualProjectId = project.id;
    renderProject({allowNavigation: false});
    const status = byId("vehicle-info-attached");
    status.textContent = `${file.name} loaded and saved`;
    status.classList.add("ready");
    toast(`${file.name} filled the manual port facts.`);
  } catch (error) {
    const status = byId("vehicle-info-attached");
    status.textContent = "Nothing loaded";
    status.classList.remove("ready");
    toast(error.message, "error");
  }
}

async function cancelAction() {
  try {
    project = await api(`/api/projects/${project.id}/cancel`, {
      method: "POST",
      json: {},
    });
    renderProject({allowNavigation: false});
  } catch (error) {
    toast(error.message, "error");
  }
}

function vehiclePayload() {
  const name = byId("vehicle-name").value.trim();
  const brand = byId("vehicle-brand").value.trim();
  if (!name) throw new Error("Enter the vehicle name.");
  if (!/^[a-z][a-z0-9_]{0,39}$/.test(brand)) {
    throw new Error("Brand must use lowercase letters, numbers, and underscores.");
  }
  return {
    name,
    brand,
    bus: Number(byId("vehicle-bus").value),
    min_frames: Number(byId("min-frames").value),
    strict: byId("strict-mode").checked,
    manual_info: manualPayload(),
  };
}

//: Every topic needs its own heading. One shared title across eight
//: walkthroughs made the modal look like it had opened the wrong page.
const helpTitles = {
  capture: "A good capture is varied and synchronized",
  reference: "Reference data is what gives the numbers meaning",
  hardware: "Choosing the hardware, and where it plugs in",
  probe: "Collecting ECU firmware, safely",
  firmware: "Where firmware responses come from",
  specs: "Three numbers you measure or look up",
  ecus: "Which module answers on which address",
  bindings: "Working out which signal is which field",
  actuation: "The command message, and being sure of it",
  limits: "Reading the limits off the factory system",
  safety: "The safety model is the part that is not generated",
  general: "What AutoDistill does, and where it stops",
};

function openHelp(topic = "general") {
  const known = topic in helpCopy ? topic : "general";
  byId("help-title").textContent = helpTitles[known];
  byId("help-content").innerHTML = helpCopy[known];
  byId("help-modal").classList.remove("hidden");
  byId("help-close").focus();
}

function closeModal(id) {
  byId(id).classList.add("hidden");
}

function openingStep(item) {
  if (item.port) return "port";
  if (item.analysis) return "analysis";
  return item.files.capture ? "details" : "source";
}

function switchToProject(item) {
  // Drop any pending autosave: it was queued against the project being left.
  window.clearTimeout(saveTimer);
  project = item;
  localStorage.setItem("autodistill-project", item.id);
  lastProjectStage = item.stage;
  brandWasEdited = Boolean(item.vehicle?.brand);
  renderProject({allowNavigation: false});
  setStep(openingStep(item), {force: true});
}

async function deleteProject(item) {
  try {
    await api(`/api/projects/${item.id}`, {method: "DELETE"});
    toast(`Deleted ${item.name} and its files.`);
    const projects = await refreshProjectList();
    if (project && project.id === item.id) {
      // The open project just went away, so something has to take its place.
      if (projects.length) {
        switchToProject(projects[0]);
      } else {
        await createProject();
      }
      // Re-render so the row for the newly opened project is the highlighted
      // one; the list above was built while this project still existed.
      await refreshProjectList();
    }
  } catch (error) {
    toast(error.message, "error");
  }
}

function projectRow(item) {
  const row = document.createElement("div");
  row.className = `project-item ${item.id === project?.id ? "active" : ""}`.trim();

  const open = document.createElement("button");
  open.type = "button";
  open.className = "project-open";
  const copy = document.createElement("div");
  const strong = document.createElement("strong");
  strong.textContent = item.name;
  const small = document.createElement("small");
  const date = new Date(item.updated_at * 1000);
  // Seconds in a "last touched" timestamp are noise, and the stage key is not
  // what the stage is called anywhere else in the UI.
  const when = date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  small.textContent = `${stageLabels[item.stage] || item.stage} · updated ${when}`;
  copy.append(strong, small);
  const state = document.createElement("span");
  state.textContent = item.status === "running" ? "Working" : "Open";
  open.append(copy, state);
  open.addEventListener("click", () => {
    switchToProject(item);
    closeModal("project-modal");
  });

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "project-delete";
  remove.textContent = "Delete";
  remove.setAttribute("aria-label", `Delete ${item.name}`);
  remove.addEventListener("click", () => {
    row.classList.add("confirming");
    row.querySelector(".confirm-keep").focus();
  });

  const main = document.createElement("div");
  main.className = "project-main";
  main.append(open, remove);

  // Deleting erases a capture that can be expensive or impossible to record
  // again, so it asks once, in place, naming what is about to go.
  const confirm = document.createElement("div");
  confirm.className = "project-confirm";
  const question = document.createElement("p");
  question.textContent = item.status === "running"
    ? `Stop the running task and delete ${item.name}? Its capture, analysis, and port are erased.`
    : `Delete ${item.name}? Its capture, analysis, and port are erased.`;
  const keep = document.createElement("button");
  keep.type = "button";
  keep.className = "confirm-keep";
  keep.textContent = "Keep";
  keep.addEventListener("click", () => row.classList.remove("confirming"));
  const confirmed = document.createElement("button");
  confirmed.type = "button";
  confirmed.className = "confirm-delete";
  confirmed.textContent = "Delete permanently";
  confirmed.addEventListener("click", () => {
    confirmed.disabled = true;
    deleteProject(item);
  });
  confirm.append(question, keep, confirmed);

  row.append(main, confirm);
  return row;
}

async function refreshProjectList() {
  const payload = await api("/api/projects");
  const projects = payload.projects || [];
  const list = byId("project-list");
  if (projects.length) {
    list.replaceChildren(...projects.map(projectRow));
  } else {
    // "Choose a vehicle project" over an empty box reads like a failure to
    // load rather than a workspace nobody has put anything in yet.
    const empty = document.createElement("p");
    empty.className = "empty-note";
    empty.textContent = "No vehicle projects yet. Each one keeps its own "
      + "capture, analysis, and generated port on this computer.";
    list.replaceChildren(empty);
  }
  return projects;
}

async function openProjects() {
  try {
    await refreshProjectList();
    byId("project-modal").classList.remove("hidden");
    byId("project-close").focus();
  } catch (error) {
    toast(error.message, "error");
  }
}

function bindEvents() {
  all("[data-step]").forEach((button) => {
    button.addEventListener("click", () => setStep(button.dataset.step));
  });
  all("[data-go-step]").forEach((button) => {
    button.addEventListener("click", () => setStep(button.dataset.goStep));
  });
  all("[data-help-topic]").forEach((button) => {
    button.addEventListener("click", () => openHelp(button.dataset.helpTopic));
  });
  byId("help-button").addEventListener("click", () => openHelp());
  const setGuide = (on) => {
    localStorage.setItem("autodistill-guide", on ? "on" : "off");
    renderGuide();
    if (on) toast("Guided mode on. The next step is in the sidebar.");
  };
  byId("guide-toggle").addEventListener("click", () => {
    setGuide(localStorage.getItem("autodistill-guide") === "off");
  });
  byId("guide-off").addEventListener("click", () => setGuide(false));
  byId("help-close").addEventListener("click", () => closeModal("help-modal"));
  byId("project-close").addEventListener("click", () => closeModal("project-modal"));
  byId("project-picker").addEventListener("click", openProjects);
  byId("create-project-button").addEventListener("click", async () => {
    try {
      await createProject();
      closeModal("project-modal");
      toast("New local project created.");
    } catch (error) {
      toast(error.message, "error");
    }
  });
  byId("new-project-button").addEventListener("click", async () => {
    try {
      await createProject();
      toast("New local project created.");
    } catch (error) {
      toast(error.message, "error");
    }
  });
  all(".modal-backdrop").forEach((modal) => {
    modal.addEventListener("click", (event) => {
      if (event.target === modal) modal.classList.add("hidden");
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      all(".modal-backdrop").forEach((modal) => modal.classList.add("hidden"));
    }
  });

  byId("demo-button").addEventListener("click", () => {
    runAction("demo", {}, {step: "source"});
  });
  byId("capture-file").addEventListener("change", (event) => {
    upload("capture", event.target.files[0]);
    event.target.value = "";
  });
  const dropzone = byId("capture-dropzone");
  ["dragenter", "dragover"].forEach((name) => {
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.add("dragging");
    });
  });
  ["dragleave", "drop"].forEach((name) => {
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.remove("dragging");
    });
  });
  dropzone.addEventListener("drop", (event) => {
    upload("capture", event.dataTransfer.files[0]);
  });
  byId("reference-file").addEventListener("change", (event) => {
    upload("reference", event.target.files[0]);
    event.target.value = "";
  });
  byId("vehicle-info-file").addEventListener("change", (event) => {
    importVehicleInfo(event.target.files[0]);
    event.target.value = "";
  });
  byId("firmware-file").addEventListener("change", (event) => {
    upload("firmware", event.target.files[0]);
    event.target.value = "";
  });

  all("[data-source-kind]").forEach((button) => {
    button.addEventListener("click", () => {
      all("[data-source-kind]").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      byId("capture-source").value =
        button.dataset.sourceKind === "panda" ? "panda:" : "socketcan:can0";
    });
  });
  byId("capture-button").addEventListener("click", () => {
    runAction("capture", {
      source: byId("capture-source").value.trim(),
      duration: Number(byId("capture-duration").value),
    }, {step: "source"});
  });
  byId("vehicle-name").addEventListener("input", (event) => {
    if (!brandWasEdited) byId("vehicle-brand").value = slugBrand(event.target.value);
  });
  byId("vehicle-brand").addEventListener("input", () => {
    brandWasEdited = Boolean(byId("vehicle-brand").value);
  });

  // Everything on these steps is saved as it is typed, including the signal
  // rows added later, which is why this listens on the panel rather than on
  // each field. Both panels, because the facts that finish a port live on
  // step 3 with the gap list that asks for them -- a field that saved only
  // while it sat on step 2 would silently stop saving when it moved.
  ['[data-panel="details"]', '[data-panel="analysis"]'].forEach((selector) => {
    const panel = document.querySelector(selector);
    panel.addEventListener("input", queueDetailsSave);
    panel.addEventListener("change", queueDetailsSave);
  });
  // The gaps are now listed directly above the fields that fill them, so this
  // opens and points rather than navigating anywhere. spotlight() scrolls.
  byId("fill-gaps").addEventListener("click", () => {
    byId("port-facts").open = true;
    spotlight("port-facts");
  });
  byId("analysis-empty-action").addEventListener("click", () => {
    if (!analysisEmptyAction) return;
    try {
      analysisEmptyAction();
    } catch (error) {
      toast(error.message, "error");
    }
  });
  byId("analyse-button").addEventListener("click", () => {
    try {
      runAction("analyse", vehiclePayload(), {step: "analysis"});
    } catch (error) {
      toast(error.message, "error");
    }
  });
  byId("add-signal-button").addEventListener("click", () => {
    addSignalRow();
    queueDetailsSave();
  });
  byId("add-binding-button").addEventListener("click", () => {
    addBindingRow();
    queueDetailsSave();
  });
  byId("generate-button").addEventListener("click", () => {
    runAction("port", {}, {step: "port"});
  });
  byId("probe-button").addEventListener("click", () => {
    runAction("probe", {
      interface: byId("probe-interface").value.trim(),
      authorised: byId("probe-authorised").checked,
      parked: byId("probe-parked").checked,
    }, {step: "analysis"});
  });
  byId("install-button").addEventListener("click", () => {
    runAction("install", {
      opendbc_dir: byId("opendbc-path").value.trim(),
      confirmed: byId("install-confirm").checked,
      force: false,
    }, {step: "port"});
  });
  byId("cancel-button").addEventListener("click", cancelAction);
  byId("download-analysis").addEventListener("click", (event) => {
    event.preventDefault();
    downloadArtifact("analysis");
  });
  byId("download-dbc").addEventListener("click", (event) => {
    event.preventDefault();
    downloadArtifact("dbc");
  });
  byId("download-port").addEventListener("click", (event) => {
    event.preventDefault();
    downloadArtifact("port");
  });
  byId("download-vehicle-info").addEventListener("click", (event) => {
    event.preventDefault();
    downloadArtifact("vehicle_info");
  });
}

bindEvents();
boot();
