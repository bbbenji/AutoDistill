"""Load the real UI in a real browser and check it does not come out broken.

Everything else about the web UI is checked without rendering it: the server is
exercised over HTTP, and ``test_web_assets`` pins the ids, data hooks and
palette that the three static files agree on. None of that can see a select
that clips the option identifying it, a stylesheet rule that never matches, or
a layout that overflows the window -- those are only visible once a browser has
laid the page out, and each of those has shipped here at least once.

The page is driven with a stubbed ``fetch`` rather than a live server, because
the server already has its own tests and what is under test here is the
rendering. The data the stub serves is not invented: it comes from a real
capture put through the real analysis and port pipeline, then through the
server's own serialisers, so the shapes are the ones the browser really gets.

Chrome writes its findings into ``document.title`` and the DOM is dumped, which
needs nothing but the browser binary. Skipped when there is no browser.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from autodistill_can.manual import (
    load_vehicle_info,
)
from autodistill_can.web import server

STATIC = Path(str(server.resources.files("autodistill_can.web").joinpath("static")))

_BROWSERS = (
    "google-chrome-stable", "google-chrome", "chromium", "chromium-browser",
    "chrome",
)

#: Widths worth checking: the full three-column desktop layout, and the
#: narrowest phone the stylesheet claims to support (`html { min-width: 320px }`
#: with the last breakpoint at 620px).
_WIDTHS = (1280, 560)

_PANELS = ("source", "details", "analysis", "port")


def _browser() -> str:
    for name in _BROWSERS:
        found = shutil.which(name)
        if found:
            return found
    # Skipping is right on a laptop with no browser, but a skip is also how a
    # check quietly stops running forever. CI sets this so a missing browser is
    # a failure there rather than eight silent passes.
    if os.environ.get("AUTODISTILL_REQUIRE_BROWSER"):
        raise AssertionError(
            "AUTODISTILL_REQUIRE_BROWSER is set but no browser was found; "
            f"looked for {', '.join(_BROWSERS)}"
        )
    pytest.skip("no Chrome/Chromium binary to render with")


@pytest.fixture(scope="module")
def fixture_project(tmp_path_factory) -> dict:
    """A project payload built by the real pipeline and the real serialisers."""
    work = tmp_path_factory.mktemp("render")
    run = [
        ["synth", "-d", "8", "-o", "drive.log", "--reference", "ref.csv", "-q"],
        ["analyse", "drive.log", "--reference", "ref.csv",
         "-f", "json", "-o", "report.json", "-q"],
    ]
    for args in run:
        subprocess.run(
            ["autodistill-can", *args], cwd=work, check=True,
            capture_output=True, text=True,
        )

    # The binding editor is the densest set of controls in the UI and the one
    # whose selects carry the longest text, so a fixture without bindings
    # leaves the part most likely to clip unrendered. These are the same facts
    # CI builds its control port from.
    facts = Path(__file__).resolve().parent.parent / "tools" / "control_facts.py"
    subprocess.run(
        [sys.executable, str(facts), "drive.log", "ref.csv", "facts.json"],
        cwd=work, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["autodistill-can", "port", "drive.log", "--reference", "ref.csv",
         "--vehicle-info", "facts.json", "--brand", "rendercar",
         "--name", "RENDER CAR 2021", "-o", "port", "-q"],
        cwd=work, check=True, capture_output=True, text=True,
    )

    manual_info = load_vehicle_info(
        json.loads((work / "facts.json").read_text())
    ).to_dict()
    analysis, review = server._summary(work / "report.json")
    manifest = json.loads((work / "port" / "port_status.json").read_text())
    report = json.loads((work / "report.json").read_text())

    return {
        "id": "0" * 32,
        "name": "Render Car 2021",
        "stage": "port",
        "status": "ready",
        "updated_at": 1754000000,
        "files": {"capture": {"name": "drive.log", "size": 153092728},
                  "reference": {"name": "ref.csv", "size": 82311}},
        "vehicle": {"name": "Render Car 2021", "brand": "rendercar", "bus": 0},
        "form": {"name": "Render Car 2021", "brand": "rendercar", "bus": 0},
        "analysis": analysis,
        "review": [*review, *(
            server._as_review_item(item) for item in manifest["human_required"]
        )],
        "port": {
            "mode": manifest["mode"],
            "safe_for_control": manifest["safe_for_control"],
            "validation": manifest.get("validation", {}),
            "evidence": manifest["evidence"],
            "manual_input": manifest.get("manual_input", {}),
            "human_required": manifest["human_required"],
            "size": 48213,
        },
        "manual_info": manual_info,
        "status_message": "",
        "activity": [],
        "installed_to": None,
        "_inventory": server._inventory(report),
    }


# Collects what only a laid-out page can tell us. Runs after the panel has been
# switched to, and hands back a JSON verdict through the document title.
_PROBE = r"""
<script>
window.__errors = [];
window.addEventListener("error", (e) => window.__errors.push(String(e.message)));
window.addEventListener("unhandledrejection",
  (e) => window.__errors.push("unhandled rejection: " + e.reason));
const realError = console.error;
console.error = (...a) => { window.__errors.push(a.join(" ")); realError(...a); };
</script>
"""

_CHECK = r"""
<script>
function textWidth(el, text) {
  const cs = getComputedStyle(el);
  const ctx = (window.__ctx ||= document.createElement("canvas").getContext("2d"));
  ctx.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
  return ctx.measureText(text).width;
}

function clippedSelects() {
  const bad = [];
  for (const sel of document.querySelectorAll("select")) {
    if (!sel.offsetParent || !sel.options.length) continue;
    const cs = getComputedStyle(sel);
    // Any option can be chosen, so the widest one is what has to fit. Chrome
    // draws the arrow inside the box, so allow for it before comparing.
    const widest = Math.max(...[...sel.options].map((o) => textWidth(sel, o.text)));
    const room = sel.clientWidth
      - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight) - ARROW;
    if (widest > room + SLACK) {
      bad.push({ id: sel.id || sel.className, need: Math.round(widest),
                 room: Math.round(room) });
    }
  }
  return bad;
}

function emptyPanel() {
  const panel = document.querySelector("[data-panel].active");
  if (!panel) return "no active panel";
  return panel.getBoundingClientRect().height > 200 ? null : "active panel is empty";
}

setTimeout(() => {
  document.title = JSON.stringify({
    errors: window.__errors,
    // A page that scrolls sideways is the classic responsive failure.
    overflow: document.documentElement.scrollWidth - window.innerWidth,
    clipped: clippedSelects(),
    empty: emptyPanel(),
    emptyCopy: [
      document.getElementById("analysis-empty-title"),
      document.getElementById("analysis-empty-detail"),
      document.getElementById("analysis-empty-action"),
    ].map((el) => (el ? el.textContent : "")).join(" | "),
    dialHidden: document.getElementById("readiness-dial")
      .classList.contains("hidden"),
    boundTargets: [...document.querySelectorAll(".binding-row .binding-target")]
      .map((el) => el.value).filter(Boolean),
    coverageHint: document.getElementById("coverage-hint")
      .classList.contains("hidden")
      ? "" : document.getElementById("coverage-hint").textContent,
    factsOpen: document.getElementById("port-facts").open,
    vehicleInfoStatus: document.getElementById("vehicle-info-attached").textContent,
    manualMass: document.getElementById("manual-mass").value,
    knownSignals: document.querySelectorAll(
      "#manual-signal-list .manual-signal-row").length,
  });
}, DELAY);
</script>
"""


def _page(project: dict, panel: str, tmp: Path, *, drive: str = "") -> Path:
    """The real index.html, wired to the real CSS and JS, with fetch stubbed.

    The status payload comes from the server's own builder rather than a copy
    of it here. A hand-written copy drifts: the browser reads a key the stub
    never sends, gets undefined, and quietly renders nothing -- which is a
    silent pass, not a failure.
    """
    status = server._system_status(server.ProjectStore(tmp / "workspace"))
    inventory = project.get("_inventory") or []
    stub = (
        "<script>\nconst stored = %s;\n"
        "window.fetch = async (u, o = {}) => {\n"
        "  const j = (b) => new Response(JSON.stringify(b),\n"
        "    {status: 200, headers: {'Content-Type': 'application/json'}});\n"
        "  if (u === '/api/status') return j(%s);\n"
        "  if (u === '/api/projects') return j({projects: [stored]});\n"
        "  if (u.endsWith('/inventory')) return j({messages: %s});\n"
        "  return j(stored);\n};\n"
        "localStorage.setItem('autodistill-guide', 'on');\n"
        "</script>"
    ) % (json.dumps(project), json.dumps(status), json.dumps(inventory))

    html = (STATIC / "index.html").read_text()
    html = html.replace('href="/static/app.css"', f'href="{STATIC / "app.css"}"')
    html = html.replace(
        '<script src="/static/app.js" defer></script>',
        _PROBE + stub + f'<script src="{STATIC / "app.js"}"></script>'
        + f'<script>setTimeout(() => {{ document.querySelector('
        f'\'[data-step="{panel}"]\').click(); {drive} }}, 300);</script>'
        + _CHECK.replace("DELAY", "1200").replace("ARROW", "18").replace("SLACK", "2"),
    )
    html = html.replace("__API_TOKEN__", "t").replace("__APP_VERSION__", "test")
    # Transitions are driven by the compositor clock, which does not advance
    # under --virtual-time-budget, so without this a screenshot or a measurement
    # can land mid-animation and report a settled layout as broken.
    html = html.replace(
        "</head>",
        "<style>*,*::before,*::after{transition:none!important;"
        "animation:none!important}</style></head>",
    )
    path = tmp / f"{panel}.html"
    path.write_text(html)
    return path


def _render(browser: str, page: Path, width: int) -> dict:
    result = subprocess.run(
        [
            browser, "--headless", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", "--virtual-time-budget=5000",
            f"--window-size={width},1400", "--dump-dom", str(page),
        ],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "LANG": "en_US.UTF-8"},
    )
    dom = result.stdout
    start = dom.find("<title>")
    end = dom.find("</title>")
    assert start != -1 and end != -1, f"page produced no verdict:\n{result.stderr[-800:]}"
    raw = dom[start + len("<title>"):end]
    raw = raw.replace("&quot;", '"').replace("&amp;", "&")
    raw = raw.replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
    return json.loads(raw)


@pytest.mark.parametrize("panel", _PANELS)
@pytest.mark.parametrize("width", _WIDTHS)
def test_panel_renders_without_error_or_overflow(
    fixture_project, tmp_path, panel, width
):
    verdict = _render(_browser(), _page(fixture_project, panel, tmp_path), width)

    assert verdict["errors"] == [], f"{panel} at {width}px logged errors"
    assert verdict["empty"] is None, f"{panel} at {width}px: {verdict['empty']}"
    # One pixel of slack for subpixel rounding in the layout.
    assert verdict["overflow"] <= 1, (
        f"{panel} at {width}px scrolls sideways by {verdict['overflow']}px"
    )
    assert verdict["clipped"] == [], (
        f"{panel} at {width}px clips option text in {verdict['clipped']}"
    )


@pytest.mark.parametrize(
    "status,has_capture,expect",
    [
        ("error", True, "did not finish"),
        ("ready", True, "Ready to decode"),
        ("ready", False, "No drive to decode"),
    ],
)
def test_the_analysis_empty_state_says_what_it_is_waiting_for(
    fixture_project, tmp_path, status, has_capture, expect
):
    """Step 3 before the decoder has run must name its blocker and act on it.

    You land here by pressing "Decode this drive", which force-navigates to
    step 3 -- so a run that then fails leaves you on this panel with no
    analysis. It used to read "Ready when you are" over a radar graphic and a
    coverage dial showing "—", with its only button sending you back a step;
    the actual failure had gone by in a toast. Every branch is exercised in a
    browser because the button's handler is only reached by clicking it: the
    first version of this screen called a function that does not exist, and
    nothing but a click would have found that.
    """
    project = dict(fixture_project)
    project["analysis"] = None
    project["port"] = None
    project["stage"] = "analysis"
    project["status"] = status
    project["status_message"] = "Analysis failed: drive.log line 44120 is bad."
    if not has_capture:
        project["files"] = {}

    page = _page(
        project, "source", tmp_path,
        drive='setStep("analysis", {force: true});'
              'setTimeout(() => document.getElementById('
              '"analysis-empty-action").click(), 200);',
    )
    verdict = _render(_browser(), page, 1280)

    assert verdict["errors"] == [], "clicking the empty-state button threw"
    assert expect in verdict["emptyCopy"], verdict["emptyCopy"]
    # A dial reading "—" over the word COVERAGE looks like a figure that failed
    # to load, so it should not be on screen at all yet.
    assert verdict["dialHidden"] is True


def test_a_missing_requirement_links_to_where_it_is_defined(
    fixture_project, tmp_path
):
    """Each gap on step 3 has to reach the control that closes it.

    "How to find it" explains the measurement; this is the other half — going
    to the box the answer is typed into. A CarState field has no box until one
    is made, so its link creates the binding row with the field already
    selected, which is the case worth pinning: a wrong key there would add a
    row bound to nothing.
    """
    project = dict(fixture_project)
    # Nothing supplied, so every requirement is listed as a gap.
    coverage = json.loads(json.dumps(project["analysis"]["coverage"]))
    for item in coverage["items"]:
        item["status"] = "missing"
    for level in ("read", "lateral", "longitudinal"):
        coverage[level] = {**coverage[level], "met": 0, "complete": False}
    project["analysis"] = {**project["analysis"], "coverage": coverage}
    project["manual_info"] = {}

    page = _page(
        project, "analysis", tmp_path,
        drive='setTimeout(() => {'
              '  const rows = [...document.querySelectorAll(".coverage-row")];'
              '  const row = rows.find((r) => r.textContent.includes('
              '    "ret.steeringAngleDeg"));'
              '  row.querySelector(".coverage-define").click();'
              '}, 400);',
    )
    verdict = _render(_browser(), page, 1280)

    assert verdict["errors"] == []
    # The row was created and points at the field the link named.
    assert verdict["boundTargets"] == ["ret.steeringAngleDeg"], verdict["boundTargets"]
    # ...and the block holding it was opened, or it would be created out of sight.
    assert verdict["factsOpen"] is True


def test_the_one_drive_hint_reaches_the_page(fixture_project, tmp_path):
    """Eight control-path facts come from a single recording, and say so.

    Each of them tells you to "capture the stock system" on its own, so the
    gap list reads as eight separate expeditions. The note is built from a
    server-supplied list, and reading the wrong variable for it fails silently
    -- an empty list just hides the note -- so it takes a browser to confirm
    it is really there.
    """
    project = dict(fixture_project)
    coverage = json.loads(json.dumps(project["analysis"]["coverage"]))
    for item in coverage["items"]:
        item["status"] = "missing"
    for level in ("read", "lateral", "longitudinal"):
        coverage[level] = {**coverage[level], "met": 0, "complete": False}
    project["analysis"] = {**project["analysis"], "coverage": coverage}

    verdict = _render(_browser(), _page(project, "analysis", tmp_path), 1280)
    assert verdict["errors"] == []
    assert "one recording" in verdict["coverageHint"], verdict["coverageHint"]


def test_vehicle_info_file_populates_the_real_form(fixture_project, tmp_path):
    project = dict(fixture_project)
    project["manual_info"] = {}
    imported = {
        "schema_version": 1,
        "vehicle_specs": {
            "mass_kg": 1655.61175, "wheelbase_m": 2.65,
            "steer_ratio": 16.88, "harness": "toyota_a",
        },
        "signals": [{
            "bus": 0, "address": "0x2E4", "start_bit": 8, "length": 16,
            "name": "STEER_TORQUE_CMD", "signed": True, "byte_order": "big",
        }],
    }
    payload = json.dumps(imported)
    drive = f'''
      const previousFetch = window.fetch;
      window.fetch = async (url, options = {{}}) => {{
        if (url.endsWith("/details") && options.body) {{
          stored.manual_info = JSON.parse(options.body).manual_info;
          return new Response(JSON.stringify(stored), {{
            status: 200, headers: {{"Content-Type": "application/json"}},
          }});
        }}
        return previousFetch(url, options);
      }};
      const transfer = new DataTransfer();
      transfer.items.add(new File([{json.dumps(payload)}], "vehicle-info.json",
                                  {{type: "application/json"}}));
      const input = document.getElementById("vehicle-info-file");
      input.files = transfer.files;
      input.dispatchEvent(new Event("change", {{bubbles: true}}));
    '''

    verdict = _render(
        _browser(), _page(project, "details", tmp_path, drive=drive), 1280
    )

    assert verdict["errors"] == []
    assert verdict["vehicleInfoStatus"] == "vehicle-info.json loaded and saved"
    assert verdict["manualMass"] == "1655.61175"
    assert verdict["knownSignals"] == 1
