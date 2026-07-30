"""The guided local UI serves real projects without weakening safety gates."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from autodistill_can.cli import build_parser
from autodistill_can.web.server import ProjectStore, create_server


@pytest.fixture
def web_server(tmp_path):
    server = create_server(port=0, workspace=tmp_path, token="test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}", server
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def _request(url, *, method="GET", payload=None, token="test-token", headers=None):
    body = None
    request_headers = dict(headers or {})
    if token is not None:
        request_headers["X-AutoDistill-Token"] = token
    if payload is not None:
        body = json.dumps(payload).encode()
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=body, headers=request_headers, method=method
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        content_type = response.headers.get_content_type()
        data = response.read()
        if content_type == "application/json":
            return response.status, json.loads(data)
        return response.status, data


def _wait_for_store(store, project_id, timeout=20):
    deadline = time.monotonic() + timeout
    result = store.get(project_id)
    while result["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        result = store.get(project_id)
    return result


def test_ui_command_parses_without_starting_server():
    args = build_parser().parse_args([
        "ui", "--port", "0", "--workspace", "/tmp/autodistill-ui",
        "--no-browser",
    ])
    assert args.command == "ui"
    assert args.port == 0
    assert args.no_browser


def test_ui_shell_and_project_api(web_server):
    base, _ = web_server
    head = urllib.request.Request(base + "/static/app.css", method="HEAD")
    with urllib.request.urlopen(head, timeout=5) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "text/css"
        assert int(response.headers["Content-Length"]) > 1000
        assert response.read() == b""

    status, page = _request(base + "/", token=None)
    assert status == 200
    assert b"AutoDistill" in page
    assert b'test-token' in page
    assert b"/static/app.js" in page

    status, system = _request(base + "/api/status")
    assert status == 200
    assert "engine" in system["ecu_types"]
    assert "ret.vEgoRaw" in system["carstate_targets"]

    status, project = _request(
        base + "/api/projects",
        method="POST",
        payload={"name": "Test vehicle"},
    )
    assert status == 201
    assert project["name"] == "Test vehicle"
    assert project["stage"] == "source"


def test_streaming_upload_and_token_gate(web_server):
    base, _ = web_server
    _, project = _request(
        base + "/api/projects", method="POST", payload={"name": "Upload test"}
    )
    capture = b"(0.0) can0 100#0000000000000000\n"
    request = urllib.request.Request(
        f"{base}/api/projects/{project['id']}/upload/capture",
        data=capture,
        method="POST",
        headers={
            "X-AutoDistill-Token": "test-token",
            "Content-Type": "application/octet-stream",
            "X-Filename": "drive.log",
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        uploaded = json.loads(response.read())
    assert uploaded["files"]["capture"]["name"] == "drive.log"
    assert uploaded["files"]["capture"]["size"] == len(capture)
    assert uploaded["stage"] == "details"

    bad = urllib.request.Request(
        base + "/api/projects",
        data=b'{"name":"blocked"}',
        method="POST",
        headers={
            "X-AutoDistill-Token": "wrong-token",
            "Content-Type": "application/json",
        },
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(bad, timeout=5)
    assert exc.value.code == 403

    rebound = urllib.request.Request(
        base + "/api/status",
        headers={
            "Host": "attacker.example",
            "X-AutoDistill-Token": "test-token",
        },
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(rebound, timeout=5)
    assert exc.value.code == 403


def test_the_step_two_form_survives_a_reload(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create("Draft")
    saved = store.save_details(project["id"], {
        "name": "  2021 Hyundai Ioniq 5 ",
        "brand": "HYUNDAI",
        "bus": 2,
        "min_frames": 80,
        "strict": True,
        "manual_info": {
            "schema_version": 1,
            "vehicle_specs": {"mass_kg": 1845, "wheelbase_m": 2.766},
        },
    })
    assert saved["form"]["name"] == "2021 Hyundai Ioniq 5"
    assert saved["form"]["brand"] == "hyundai"

    # Reloading the page is a fresh read from disk, which is what was lost.
    reloaded = ProjectStore(tmp_path).get(project["id"])
    assert reloaded["form"]["bus"] == 2
    assert reloaded["form"]["min_frames"] == 80
    assert reloaded["form"]["strict"] is True
    assert reloaded["manual_info"]["vehicle_specs"]["mass_kg"] == 1845.0
    assert reloaded["manual_info"]["vehicle_specs"]["wheelbase_m"] == 2.766

    # Saving a draft must not look like progress through the workflow.
    assert reloaded["analysis"] is None
    assert reloaded["port"] is None
    assert reloaded["stage"] == "source"
    assert reloaded["vehicle"] == {"name": "", "brand": "", "bus": 0}


def test_draft_details_are_clamped_not_rejected(tmp_path):
    # Half-typed values are normal while a form is being filled in; they are
    # validated properly when analysis starts, not while someone is typing.
    store = ProjectStore(tmp_path)
    project = store.create("Draft")
    saved = store.save_details(project["id"], {
        "name": "x" * 400, "brand": "", "bus": 99, "min_frames": "nonsense",
    })
    assert len(saved["form"]["name"]) == 100
    assert saved["form"]["bus"] == 15
    assert saved["form"]["min_frames"] == 40
    with pytest.raises(ValueError):
        store.save_details(project["id"], {"manual_info": {"schema_version": 7}})


def test_details_save_over_http(web_server):
    base, _ = web_server
    _, project = _request(
        base + "/api/projects", method="POST", payload={"name": "Form"}
    )
    status, saved = _request(
        f"{base}/api/projects/{project['id']}/details",
        method="POST",
        payload={"name": "OPEL CORSA", "brand": "corsa", "bus": 0},
    )
    assert status == 200
    assert saved["form"]["name"] == "OPEL CORSA"
    _, fetched = _request(f"{base}/api/projects/{project['id']}")
    assert fetched["form"]["brand"] == "corsa"


def test_delete_removes_the_project_and_its_files(tmp_path):
    store = ProjectStore(tmp_path)
    keep = store.create("Keep me")
    doomed = store.create("Delete me")
    directory = tmp_path / doomed["id"]
    (directory / "capture.log").write_text("(0.0) can0 100#0000000000000000\n")

    assert store.delete(doomed["id"])["name"] == "Delete me"
    assert not directory.exists()
    assert [item["id"] for item in store.list()] == [keep["id"]]
    with pytest.raises(FileNotFoundError):
        store.get(doomed["id"])
    with pytest.raises(FileNotFoundError):
        store.delete(doomed["id"])
    with pytest.raises(ValueError):
        store.delete("../../etc")


def test_a_corrupt_project_can_still_be_deleted(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create("Half-written state")
    (tmp_path / project["id"] / "project.json").write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        store.get(project["id"])
    assert store.delete(project["id"])["deleted"] == project["id"]
    assert not (tmp_path / project["id"]).exists()


def test_delete_stops_a_running_task_and_stays_deleted(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create("Wedged capture")
    store.start_demo(project["id"])
    store.delete(project["id"])
    directory = tmp_path / project["id"]
    assert not directory.exists()
    # The worker has to notice it is writing into a project that no longer
    # exists. If it recreated the state file, the deleted project would
    # reappear in the sidebar the next time the list was fetched.
    time.sleep(2)
    assert not directory.exists()
    assert store.list() == []


def test_delete_over_http_is_guarded(web_server):
    base, server = web_server
    _, project = _request(
        base + "/api/projects", method="POST", payload={"name": "HTTP delete"}
    )

    for token in (None, "wrong-token"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _request(
                f"{base}/api/projects/{project['id']}", method="DELETE", token=token
            )
        assert exc.value.code == 403

    with pytest.raises(urllib.error.HTTPError) as exc:
        _request(f"{base}/api/projects/{'0' * 32}", method="DELETE")
    assert exc.value.code == 404

    status, payload = _request(
        f"{base}/api/projects/{project['id']}", method="DELETE"
    )
    assert status == 200
    assert payload["deleted"] == project["id"]
    _, listed = _request(base + "/api/projects")
    assert listed["projects"] == []
    assert not (server.store.root / project["id"]).exists()


def test_demo_job_uses_real_cli(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create("Demo")
    running = store.start_demo(project["id"])
    assert running["status"] == "running"
    result = _wait_for_store(store, project["id"])
    assert result["status"] == "ready", result["status_message"]
    assert result["files"]["capture"]["size"] > 1000
    assert result["files"]["reference"]["size"] > 100
    assert result["vehicle"]["brand"] == "autodistill_demo"


def test_demo_project_reaches_a_complete_control_port(tmp_path):
    """The demo is meant to show the whole pipeline working, not just the

    read-only half. Loading it should need no further input to reach full
    read+steer+drive coverage and a port whose controller actually builds and
    sends both a lateral and a longitudinal command -- `safe_for_control`
    stays false regardless, since nothing ever changes that.
    """
    store = ProjectStore(tmp_path)
    project = store.create("Complete demo")
    store.start_demo(project["id"])
    demo = _wait_for_store(store, project["id"])
    assert demo["status"] == "ready", demo["status_message"]
    assert demo["manual_info"]["actuation"]["lateral"]
    assert demo["manual_info"]["actuation"]["longitudinal"]

    # No manual_info in the payload: this is the ordinary "decode" request,
    # which must fall back to what the demo already attached.
    store.start_analysis(project["id"], {
        "name": demo["vehicle"]["name"], "brand": demo["vehicle"]["brand"],
        "bus": 0, "min_frames": 40, "strict": False,
    })
    analysed = _wait_for_store(store, project["id"])
    assert analysed["status"] == "ready", analysed["status_message"]
    coverage = analysed["analysis"]["coverage"]
    assert coverage["read"]["complete"] is True
    assert coverage["lateral"]["complete"] is True
    assert coverage["longitudinal"]["complete"] is True

    store.start_port(project["id"])
    ported = _wait_for_store(store, project["id"])
    assert ported["status"] == "ready", ported["status_message"]
    manifest = json.loads(
        (tmp_path / project["id"] / "port" / "port_status.json").read_text()
    )
    assert manifest["mode"] == "control"
    assert manifest["controls"]["lateral"] is True
    assert manifest["controls"]["longitudinal"] is True
    assert manifest["safe_for_control"] is False
    controller = (
        tmp_path / project["id"] / "port" / "carcontroller.py"
    ).read_text()
    assert controller.count("can_sends.append") == 2


def test_reloading_demo_data_clears_a_stale_analysis(tmp_path):
    """Loading the demo onto a project with a real result already on it.

    ``upload`` and ``start_probe`` both clear ``analysis``/``port``/``review``
    when the capture underneath them changes; ``start_demo`` used to skip that,
    so a project that had already been analysed and ported kept showing that
    stale result -- and serving its stale port.zip -- once the demo capture
    silently replaced the real one.
    """
    store = ProjectStore(tmp_path)
    project = store.create("Stale demo")
    store.start_demo(project["id"])
    assert _wait_for_store(store, project["id"])["status"] == "ready"
    store.start_analysis(project["id"], {
        "name": "FIRST CAR", "brand": "firstcar", "bus": 0,
        "min_frames": 40, "strict": False,
    })
    analysed = _wait_for_store(store, project["id"])
    assert analysed["status"] == "ready", analysed["status_message"]
    assert analysed["analysis"] is not None

    store.start_demo(project["id"])
    reloaded = _wait_for_store(store, project["id"])
    assert reloaded["status"] == "ready", reloaded["status_message"]
    assert reloaded["analysis"] is None
    assert reloaded["port"] is None
    assert reloaded["review"] == []


def test_incomplete_control_facts_entered_after_analysis_remain_inert(tmp_path):
    """The order people actually work in.

    Nobody can bind a CarState field or name a steering message before seeing
    what the decoder found. So facts arrive after analysis, and must reach the
    port without forcing a full re-analysis first.
    """
    store = ProjectStore(tmp_path)
    project = store.create("Control")
    store.start_demo(project["id"])
    assert _wait_for_store(store, project["id"])["status"] == "ready"
    store.start_analysis(project["id"], {
        "name": "CONTROL CAR", "brand": "controlcar", "bus": 0,
        "min_frames": 40, "strict": False,
    })
    analysed = _wait_for_store(store, project["id"])
    assert analysed["status"] == "ready", analysed["status_message"]
    assert analysed["analysis"]["coverage"]["read"]["total"] > 0

    # Pick a real message to command, exactly as the UI's dropdowns would.
    inventory = store.inventory(project["id"])["messages"]
    report = json.loads((tmp_path / project["id"] / "analysis.json").read_text())
    commandable = next(
        m for m in report["messages"]
        if "checksum" in m and "counter" in m
        and any(s["kind"] not in ("counter", "checksum") for s in m["signals"])
    )
    payload_signal = next(
        s["name"] for s in commandable["signals"]
        if s["kind"] not in ("counter", "checksum")
    )
    assert any(m["address"] == commandable["address"] for m in inventory)

    store.save_details(project["id"], {
        "name": "CONTROL CAR", "brand": "controlcar", "bus": 0,
        "manual_info": {
            "schema_version": 1,
            "vehicle_specs": {"mass_kg": 1845, "wheelbase_m": 2.7,
                              "steer_ratio": 14.0},
            "actuation": {"lateral": {
                "bus": commandable["bus"],
                "address": commandable["address"],
                "message": commandable["name"],
                "frequency_hz": 100,
                "signals": {payload_signal: "apply_torque"},
                "counter_signal": "COUNTER",
                "checksum_signal": "CHECKSUM",
            }},
            "limits": {"steer_max": 384, "steer_delta_up": 3,
                       "steer_delta_down": 7, "steer_driver_allowance": 50,
                       "steer_actuator_delay": 0.1},
            "tuning": {"lateral": {"kind": "torque", "max_lateral_accel": 2.5,
                                   "friction": 0.1}},
            "safety": {"model": "hyundai"},
        },
    })

    store.start_port(project["id"])
    ported = _wait_for_store(store, project["id"])
    assert ported["status"] == "ready", ported["status_message"]
    package = tmp_path / project["id"] / "port"
    controller = (package / "carcontroller.py").read_text()
    # These facts describe the outgoing frame, but the mandatory CarState and
    # harness checklist is still incomplete. Preserve the declaration without
    # quietly turning an incomplete port into a live controller.
    assert "can_sends.append" not in controller
    assert "SafetyModel.noOutput" in (package / "interface.py").read_text()
    assert "STEER_MAX = 384" in (package / "values.py").read_text()
    manifest = json.loads((package / "port_status.json").read_text())
    assert manifest["controls"]["declared_lateral"] is True
    assert manifest["controls"]["lateral"] is False
    # However complete it is, this is still not a port anyone has driven.
    assert ported["port"]["safe_for_control"] is False


def test_web_workflow_preserves_manual_facts_in_analysis_and_port(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create("Manual facts")
    store.start_demo(project["id"])
    demo = _wait_for_store(store, project["id"])
    assert demo["status"] == "ready", demo["status_message"]

    manual_info = {
        "schema_version": 1,
        "vehicle_specs": {
            "mass_kg": 1810,
            "wheelbase_m": 2.75,
            "steer_ratio": 14.6,
            "docs_package": "All",
        },
        "ecu_types": {"0x7E0": "engine"},
        "signals": [],
        "engineering": {
            "actuation_notes": ["Command format requires bench work"],
            "safety_notes": ["Do not enable on public roads"],
            "sources": ["Service manual, vehicle specification page"],
        },
    }
    running = store.start_analysis(project["id"], {
        "name": "MANUAL TEST CAR",
        "brand": "manual_test",
        "bus": 0,
        "min_frames": 40,
        "strict": False,
        "manual_info": manual_info,
    })
    # The form is saved before the worker starts, so even a later analysis
    # failure cannot erase the user's measurements and research notes.
    assert running["manual_info"]["vehicle_specs"]["mass_kg"] == 1810.0
    analysed = _wait_for_store(store, project["id"])
    assert analysed["status"] == "ready", analysed["status_message"]
    assert analysed["manual_info"]["vehicle_specs"]["mass_kg"] == 1810.0
    saved = tmp_path / project["id"] / "vehicle-info.json"
    assert json.loads(saved.read_text())["ecu_types"] == {"0x7E0": "engine"}

    store.start_port(project["id"])
    ported = _wait_for_store(store, project["id"])
    assert ported["status"] == "ready", ported["status_message"]
    assert ported["port"]["manual_input"]["provided"] is True
    assert ported["port"]["safe_for_control"] is False
    package = tmp_path / project["id"] / "port"
    assert (package / "vehicle_info.json").is_file()
    assert "mass=1810.0" in (package / "values.py").read_text()
    assert "SafetyModel.noOutput" in (package / "interface.py").read_text()


# --------------------------------------------------------------------------
# Merging additional captures


def _synth(path, seconds=20):
    import subprocess

    subprocess.run(
        ["autodistill-can", "synth", "-d", str(seconds), "-o", str(path), "-q"],
        check=True, capture_output=True,
    )
    return path


def test_extra_captures_are_merged_into_the_run(tmp_path):
    """A second drive has to reach the command line, or it is stored and ignored.

    One drive rarely exercises everything, so extra captures exist to widen
    what the analysis has seen. They are laid end to end after the first --
    `--add-log` -- never interleaved.
    """
    store = ProjectStore(tmp_path)
    project = store.create("Merge Test")
    pid = project["id"]
    first = _synth(tmp_path / "first.log")
    second = _synth(tmp_path / "second.log")

    with first.open("rb") as fh:
        store.upload(pid, "capture", fh, length=first.stat().st_size,
                     filename="first.log")
    with second.open("rb") as fh:
        state = store.upload(pid, "extra_capture", fh,
                             length=second.stat().st_size, filename="second.log")

    extras = state["files"]["extra_captures"]
    assert [item["name"] for item in extras] == ["second.log"]

    state = store.start_analysis(
        pid, {"name": "Merge Test", "brand": "mtest", "bus": 0}
    )
    _wait_for_store(store, pid, timeout=180)
    state = store.get(pid)
    assert state["status"] == "ready", state.get("status_message")

    report = json.loads((store._dir(pid) / "analysis.json").read_text())
    frames = sum(message["frames"] for message in report["messages"])

    # Both captures, not one: a single 20s synth drive is nowhere near this.
    single = ProjectStore(tmp_path / "solo")
    solo = single.create("Solo")["id"]
    with first.open("rb") as fh:
        single.upload(solo, "capture", fh, length=first.stat().st_size,
                      filename="first.log")
    single.start_analysis(solo, {"name": "Solo", "brand": "solo", "bus": 0})
    _wait_for_store(single, solo, timeout=180)
    solo_report = json.loads((single._dir(solo) / "analysis.json").read_text())
    solo_frames = sum(m["frames"] for m in solo_report["messages"])
    assert frames > solo_frames * 1.8


def test_an_extra_capture_needs_a_first_one(tmp_path):
    store = ProjectStore(tmp_path)
    pid = store.create("No Base")["id"]
    extra = _synth(tmp_path / "extra.log")
    with extra.open("rb") as fh:
        with pytest.raises(ValueError, match="first capture"):
            store.upload(pid, "extra_capture", fh,
                         length=extra.stat().st_size, filename="extra.log")


def test_dropping_an_extra_capture_removes_it_and_the_results(tmp_path):
    """Its contents are baked into the analysis, so both have to go."""
    store = ProjectStore(tmp_path)
    pid = store.create("Drop Test")["id"]
    first = _synth(tmp_path / "one.log")
    second = _synth(tmp_path / "two.log")
    with first.open("rb") as fh:
        store.upload(pid, "capture", fh, length=first.stat().st_size,
                     filename="one.log")
    with second.open("rb") as fh:
        state = store.upload(pid, "extra_capture", fh,
                             length=second.stat().st_size, filename="two.log")

    stored_as = state["files"]["extra_captures"][0]["stored_as"]
    assert (store._dir(pid) / stored_as).exists()

    state = store.drop_extra_capture(pid, stored_as)
    assert state["files"]["extra_captures"] == []
    assert state["analysis"] is None
    assert not (store._dir(pid) / stored_as).exists()


def test_dropping_rejects_a_path_that_is_not_one_of_its_own(tmp_path):
    store = ProjectStore(tmp_path)
    pid = store.create("Guard")["id"]
    with pytest.raises(FileNotFoundError):
        store.drop_extra_capture(pid, "../../etc/passwd")
