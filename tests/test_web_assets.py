"""The three static files that make up the UI have to agree with each other.

app.js reaches into the page by id and by data attribute. Nothing checks those
names at build time — a renamed id in index.html is a silent null dereference
in the browser and a button that quietly does nothing. These read the real
files and assert every hook the script uses is actually in the markup.
"""

from __future__ import annotations

import re

from autodistill_can.web import server

STATIC = server.resources.files("autodistill_can.web").joinpath("static")
HTML = STATIC.joinpath("index.html").read_text()
SCRIPT = STATIC.joinpath("app.js").read_text()
STYLES = STATIC.joinpath("app.css").read_text()

_MARKUP_IDS = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', HTML))
_SCRIPT_IDS = set(re.findall(r'byId\("([A-Za-z0-9_-]+)"\)', SCRIPT))
_SCRIPT_IDS |= set(re.findall(r'getElementById\("([A-Za-z0-9_-]+)"\)', SCRIPT))


def test_every_id_the_script_reaches_for_exists_in_the_markup():
    assert len(_SCRIPT_IDS) > 30, "id extraction looks broken, not thorough"
    assert _SCRIPT_IDS <= _MARKUP_IDS


def test_every_data_hook_the_script_queries_exists_in_the_markup():
    hooks = set(re.findall(r'querySelectorAll?\("\[(data-[a-z-]+)\]"\)', SCRIPT))
    hooks |= set(re.findall(r'all\("\[(data-[a-z-]+)\]"\)', SCRIPT))
    assert hooks, "no data hooks found to check"
    for hook in hooks:
        assert f"{hook}=" in HTML, f"app.js queries [{hook}], nothing declares it"


def test_project_rows_are_styled_by_the_stylesheet():
    # The project list is built entirely in JavaScript, so its class names have
    # no presence in index.html to keep them honest. Deleting a project is a
    # destructive action behind an unstyled button, which is worth pinning.
    for name in (
        "project-item", "project-main", "project-open",
        "project-delete", "project-confirm", "confirm-keep", "confirm-delete",
    ):
        assert f'"{name}"' in SCRIPT or f"{name} " in SCRIPT, f"{name} unused"
        assert f".{name}" in STYLES, f".{name} has no styling"


def test_guided_mode_points_at_controls_that_exist():
    """Guided mode names a control to highlight for each step.

    Those names are in JavaScript and the controls are in HTML, so a renamed
    id would leave the guide walking someone to a highlight that never
    appears -- the one thing a hand-holding mode must not do.
    """
    tasks = SCRIPT[SCRIPT.index("function guideTasks()"):]
    tasks = tasks[:tasks.index("\nfunction ")]
    targets = set(re.findall(r'target: "([A-Za-z0-9_-]+)"', tasks))
    steps = set(re.findall(r'step: "([a-z]+)"', tasks))
    assert len(targets) > 5, "guide target extraction looks broken"
    assert targets <= _MARKUP_IDS | {
        name for name in re.findall(r'class="([a-z-]+)"', HTML)
    }
    assert steps <= set(re.findall(r'data-panel="([a-z]+)"', HTML))


def test_every_help_link_has_something_to_show():
    # `data-help-topic` buttons are the walkthroughs for facts a person has to
    # go and obtain. A button opening an empty modal is worse than no button.
    topics = set(re.findall(r'data-help-topic="([a-z]+)"', HTML))
    assert topics, "no help links found"
    for topic in topics:
        assert re.search(rf"^  {topic}: `", SCRIPT, re.M), f"no help copy for {topic}"
        # And its own heading: one shared title across every walkthrough made
        # the modal look like it had opened the wrong page.
        assert re.search(rf'^  {topic}: "', SCRIPT, re.M), f"no help title for {topic}"


def test_the_shell_only_ships_placeholders_the_server_replaces():
    placeholders = set(re.findall(r"__[A-Z_]+__", HTML))
    assert placeholders == {"__API_TOKEN__", "__APP_VERSION__"}


def test_a_scale_binding_can_be_reloaded_into_the_form():
    """One input serves both `threshold` and `scale`.

    Populating it from `threshold` alone meant every scale binding came back
    empty, and the next edit anywhere on the page failed to save with "needs a
    scale" -- losing the whole form, not just that row.
    """
    row = SCRIPT[SCRIPT.index("function addBindingRow"):]
    row = row[:row.index("\nfunction ")]
    assert 'data.threshold ?? data.scale ?? ""' in row


def test_a_binding_offset_survives_the_form():
    # `offset` has no field of its own, so it has to be carried explicitly or
    # it is silently dropped the first time the form is saved.
    assert "row.dataset.offset" in SCRIPT


def test_vehicle_info_can_be_loaded_into_the_manual_form():
    assert "vehicle-info-file" in _MARKUP_IDS
    assert "vehicle-info-attached" in _MARKUP_IDS
    assert "async function importVehicleInfo(file)" in SCRIPT
    assert "manual_info: manualInfo" in SCRIPT
    assert "populateManualInfo(project.manual_info)" in SCRIPT


def test_manual_signal_encoding_survives_the_form():
    for field in ("manual-signal-order", "manual-signal-signed"):
        assert field in SCRIPT
    assert "item.byte_order = optional.byte_order" in SCRIPT
    assert 'item.signed = optional.signed === "true"' in SCRIPT


def test_every_manual_section_counts_as_manual_info():
    body = SCRIPT[SCRIPT.index("function hasManualInfo(info)"):]
    body = body[:body.index("\nfunction ", 1)]
    for section in ("carstate", "actuation", "limits", "tuning", "safety"):
        assert f"info.{section}" in body
    payload = SCRIPT[SCRIPT.index("function bindingsPayload"):]
    payload = payload[:payload.index("\nfunction ")]
    assert "item.offset = Number(row.dataset.offset)" in payload


def test_the_guide_updates_on_its_very_first_render():
    """The step key must not start equal to a state it can legitimately reach.

    Guarding the live region on "did the step change" is right, but seeding the
    guard with "" -- which is also what "nothing left to do" produces -- meant a
    project with no outstanding work kept the placeholder text from the markup
    and never said anything.
    """
    assert re.search(r"^let guideStepKey = null;", SCRIPT, re.M)


def test_the_guide_knows_the_facts_that_decide_a_usable_port():
    # Each of these is a state that silently produces a worse port: firmware
    # that openpilot cannot fingerprint on, bindings that replay to impossible
    # values, and a control port no one can be told which harness to buy for.
    for key in ("identify-ecu", "fix-bindings", "harness"):
        assert f'key: "{key}"' in SCRIPT, key


def _palette() -> dict[str, str]:
    """The :root custom properties, as the browser would resolve them."""
    root = STYLES[STYLES.index(":root {"):]
    root = root[:root.index("\n}")]
    return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;", root))


def _relative_luminance(colour: str) -> float:
    channels = []
    for pair in (colour[1:3], colour[3:5], colour[5:7]):
        value = int(pair, 16) / 255
        channels.append(
            value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
        )
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(foreground: str, background: str) -> float:
    high, low = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)),
        reverse=True,
    )
    return (high + 0.05) / (low + 0.05)


def test_the_palette_is_legible_on_the_surfaces_it_is_used_on():
    """Every text pairing in the palette has to clear WCAG AA's 4.5:1.

    None of this text is large -- the hints under fields and the badges are
    9-11px -- so the 3:1 allowance for large text never applies. Three of these
    pairings once failed: --faint was 2.9:1 on white while carrying the copy
    that explains what each field wants, and --amber was under 3.3:1 in *both*
    directions, as badge text on --amber-soft and as the fill behind white text
    on .button-warning.
    """
    palette = _palette()
    pairings = [
        ("--ink", "--surface"),
        ("--muted", "--surface"),
        ("--muted", "--surface-2"),
        ("--muted", "--paper"),
        ("--faint", "--surface"),
        ("--faint", "--surface-2"),
        ("--green", "--surface"),
        ("--green-on-dark", "--dark"),
        ("--amber", "--amber-soft"),
        ("--red", "--red-soft"),
        ("--blue", "--surface"),
    ]
    for ink, surface in pairings:
        ratio = _contrast(palette[ink], palette[surface])
        assert ratio >= 4.5, f"{ink} on {surface} is {ratio:.2f}:1"

    # .button-warning puts white on the amber fill rather than beside it.
    ratio = _contrast("#ffffff", palette["--amber"])
    assert ratio >= 4.5, f"white on --amber is {ratio:.2f}:1"


def test_links_on_the_dark_sidebar_do_not_use_the_light_surface_green():
    """--green is mixed for white cards and reaches only 2.7:1 on the sidebar.

    The guide panel lives there and its "how do I do this?" link is the one
    piece of the hand-holding mode that explains the step, so it is the last
    thing that should be hard to read.
    """
    palette = _palette()
    assert _contrast(palette["--green"], palette["--dark"]) < 4.5, (
        "--green now passes on dark; this test's premise needs revisiting"
    )
    assert ".guide .text-button" in STYLES
    block = STYLES[STYLES.index(".guide .text-button"):]
    assert "var(--green-on-dark)" in block[:block.index("}")]
