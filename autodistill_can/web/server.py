"""Local HTTP service that guides users through the AutoDistill pipeline.

The service deliberately binds to loopback and has no cloud integration. CAN
captures can contain VINs, locations, driving habits, and proprietary traffic,
so keeping them on the machine doing the analysis is the safest default.
"""

from __future__ import annotations

import hmac
import importlib.util
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..manual import (
    ACTUATION_VALUES,
    CARSTATE_FIELDS,
    CARSTATE_TARGETS,
    CARSTATE_TRANSFORMS,
    ECU_TYPES,
    ESSENTIAL_ECU_TYPES,
    SAFETY_MODELS,
    VehicleInfo,
    load_vehicle_info,
    write_vehicle_info,
)
from ..requirements import GEAR_SHIFTERS, STOCK_CAPTURE_ANSWERS

__all__ = ["create_server", "run_server"]

_MAX_JSON = 1 * 1024 * 1024
_MAX_UPLOAD = 8 * 1024 * 1024 * 1024
_PROJECT_ID = re.compile(r"^[a-f0-9]{32}$")
_BRAND = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_INTERFACE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_UPLOAD_KINDS = {"capture", "extra_capture", "reference", "firmware"}
#: How many captures may be merged into one analysis. A limit only so a
#: stuck loop cannot fill a disk; nothing about the merge itself cares.
_MAX_EXTRA_CAPTURES = 12
_DOWNLOADS = {
    "analysis": ("analysis.json", "application/json"),
    "dbc": ("recovered.dbc", "text/plain"),
    "port": ("port.zip", "application/zip"),
    "firmware": ("firmware.txt", "text/plain"),
    "firmware_capture": ("firmware.log", "text/plain"),
    "capture": ("capture.log", "text/plain"),
    "reference": ("reference.csv", "text/csv"),
    "vehicle_info": ("vehicle-info.json", "application/json"),
}


def _now() -> float:
    return round(time.time(), 3)


def _safe_filename(value: str | None, fallback: str) -> str:
    value = Path(value or "").name
    cleaned = re.sub(r"[^A-Za-z0-9_. -]", "_", value).strip(" .")
    return cleaned[:120] or fallback


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _inventory(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Messages and signal names, for the dropdowns that bind CarState fields.

    Trimmed hard: the full report carries bit positions, confidences and
    checksum working for every field, and the browser only needs enough to
    name one.
    """
    messages = []
    for message in report.get("messages", []):
        names = [
            signal.get("name")
            for signal in message.get("signals", [])
            if signal.get("name")
        ]
        for mode_signals in (message.get("multiplexed") or {}).values():
            names += [s.get("name") for s in mode_signals if s.get("name")]
        if not names:
            continue
        messages.append({
            "bus": message.get("bus", 0),
            "address": message.get("address"),
            "address_hex": message.get("address_hex"),
            "name": message.get("name") or message.get("address_hex"),
            "signals": sorted(set(names)),
        })
    return messages


#: A full stop only ends a sentence when a space and a capital follow it, which
#: keeps "carstate.py" and "vehicle_info.json" in one piece.
_SENTENCE = re.compile(r"^(.+?[.!?])\s+(?=[A-Z0-9])(.*)$", re.S)


def _as_review_item(text: str) -> dict[str, str]:
    """Split one human task into a heading and the reason behind it.

    These are rendered as a bold title over small grey detail. Numbering them
    "Human task 3" put a counter where the instruction belongs and demoted the
    only useful text; the first sentence is the action, the rest is why.
    """
    match = _SENTENCE.match(text)
    return {
        "level": "required",
        "title": match.group(1) if match else text,
        "detail": match.group(2) if match else "",
    }


def _summary(report_path: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    report = json.loads(report_path.read_text())
    messages = report.get("messages", [])
    fingerprint = report.get("fingerprint", {})
    correlations = report.get("correlations", [])
    signals = [signal for message in messages for signal in message.get("signals", [])]
    counters = sum("counter" in message for message in messages)
    checksums = [message["checksum"] for message in messages if "checksum" in message]
    buses = sorted(
        {
            int(message.get("bus", 0))
            for message in messages
        }
    )
    firmware = fingerprint.get("firmware", [])
    unknown = sum(
        signal.get("name", "").startswith("SIG_")
        for signal in signals
    )
    uncertain = sum(bool(item.get("underdetermined")) for item in checksums)
    result = {
        "messages": len(messages),
        "signals": len(signals),
        "named_signals": len(correlations),
        "unknown_signals": unknown,
        "counters": counters,
        "checksums": len(checksums),
        "uncertain_checksums": uncertain,
        "firmware_responses": len(firmware),
        "buses": buses,
        "coverage": report.get("coverage", {}),
    }
    # First, and above everything else: these say the recording itself limits
    # every result below them, so they have to be read before the results are.
    review: list[dict[str, str]] = [
        {
            "level": "required" if item["level"] == "blocking" else "important",
            "title": item["title"],
            "detail": item["detail"],
        }
        for item in report.get("capture", [])
    ]
    if not correlations:
        review.append({
            "level": "important",
            "title": "Record matching reference data",
            "detail": (
                "The fields are located, but their real-world meanings cannot be "
                "named without synchronized GPS, OBD-II, or sensor measurements."
            ),
        })
    if not firmware:
        review.append({
            "level": "recommended",
            "title": "Collect ECU firmware versions",
            "detail": (
                "Firmware fingerprints help openpilot distinguish this exact "
                "platform. Use the guarded firmware-probe step while parked."
            ),
        })
    if uncertain:
        review.append({
            "level": "important",
            "title": "Validate underdetermined checksums",
            "detail": (
                f"{uncertain} checksum solution(s) fit this capture but need a "
                "longer, more varied drive before they can be trusted."
            ),
        })
    if unknown:
        review.append({
            "level": "review",
            "title": "Name remaining signals",
            "detail": (
                f"{unknown} recovered fields still have placeholder names. "
                "Compare them in Cabana with known vehicle actions."
            ),
        })
    review.append({
        "level": "required",
        "title": "Human safety engineering remains required",
        "detail": (
            "AutoDistill generates a read-only, dashcam-only port. Vehicle "
            "dynamics, actuation, limits, and the safety model must be measured, "
            "implemented, and reviewed before any control work."
        ),
    })
    return result, review


class ProjectStore:
    """Own project files, state, and background AutoDistill processes."""

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancel_requested: set[str] = set()

    def _dir(self, project_id: str) -> Path:
        if not _PROJECT_ID.fullmatch(project_id):
            raise ValueError("invalid project id")
        path = (self.root / project_id).resolve()
        if path.parent != self.root:
            raise ValueError("invalid project path")
        return path

    def _state_path(self, project_id: str) -> Path:
        return self._dir(project_id) / "project.json"

    def _load_unlocked(self, project_id: str) -> dict[str, Any]:
        path = self._state_path(project_id)
        if not path.is_file():
            raise FileNotFoundError("project not found")
        state = json.loads(path.read_text())
        state["busy"] = (
            state.get("status") == "running" or project_id in self._processes
        )
        return state

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        state["updated_at"] = _now()
        saved = {key: value for key, value in state.items() if key != "busy"}
        path = self._state_path(state["id"])
        if not path.parent.is_dir():
            # The project was deleted while a worker was still running. Its
            # result has nowhere to go, and recreating the directory here
            # would resurrect a project the user asked us to forget.
            return
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(saved, indent=2) + "\n")
        os.replace(temporary, path)

    def create(self, name: str = "My vehicle") -> dict[str, Any]:
        project_id = uuid.uuid4().hex
        directory = self._dir(project_id)
        directory.mkdir(mode=0o700)
        state: dict[str, Any] = {
            "id": project_id,
            "name": str(name).strip()[:80] or "My vehicle",
            "created_at": _now(),
            "updated_at": _now(),
            "stage": "source",
            "status": "ready",
            "status_message": "Choose a demo, upload a log, or capture from hardware.",
            "files": {},
            "vehicle": {
                "name": "",
                "brand": "",
                "bus": 0,
            },
            # The step-2 form as it is being filled in, saved on every edit.
            # `vehicle` holds only values that survived validation at analysis
            # time; this holds whatever the user has typed so far.
            "form": {
                "name": "",
                "brand": "",
                "bus": 0,
                "min_frames": 40,
                "strict": False,
            },
            "analysis": None,
            "manual_info": VehicleInfo().to_dict(),
            "review": [],
            "port": None,
            "activity": [],
        }
        with self._lock:
            self._save_unlocked(state)
        return self.get(project_id)

    def list(self) -> list[dict[str, Any]]:
        projects = []
        for path in self.root.iterdir():
            if not path.is_dir() or not _PROJECT_ID.fullmatch(path.name):
                continue
            try:
                projects.append(self.get(path.name))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        projects.sort(key=lambda item: item.get("updated_at", 0), reverse=True)
        return projects

    def get(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            return _json_clone(self._load_unlocked(project_id))

    def drop_extra_capture(self, project_id: str, stored_as: str) -> dict[str, Any]:
        """Remove one merged capture, and the file behind it.

        A capture merged by mistake -- the wrong drive, or the same one twice --
        would otherwise be permanent, and re-running the analysis would keep
        including it.
        """
        state = self.get(project_id)
        if state["busy"]:
            raise RuntimeError("wait for the current task to finish")
        existing = state.get("files", {}).get("extra_captures") or []
        if not any(item["stored_as"] == stored_as for item in existing):
            raise FileNotFoundError("no such merged capture")
        # Names are generated here, never taken from the request, but this is
        # the one place a request chooses a path -- so check it stayed a name.
        if stored_as != Path(stored_as).name:
            raise ValueError("invalid capture reference")
        (self._dir(project_id) / stored_as).unlink(missing_ok=True)

        def update(current: dict[str, Any]) -> None:
            current["files"]["extra_captures"] = [
                item for item in current["files"].get("extra_captures", [])
                if item["stored_as"] != stored_as
            ]
            current["status_message"] = "Merged capture removed."
            # Its contents are baked into both, so neither still describes the
            # captures now attached.
            current["analysis"] = None
            current["port"] = None
            current["review"] = []

        return self._mutate(project_id, update)

    def delete(self, project_id: str) -> dict[str, Any]:
        """Delete a project and every file stored inside it.

        A running task does not block deletion. Getting rid of a capture that
        is wedging the app is exactly when someone reaches for delete, so the
        worker is stopped first rather than being a reason to refuse.
        """
        directory = self._dir(project_id)
        with self._lock:
            if not self._state_path(project_id).is_file():
                raise FileNotFoundError("project not found")
            try:
                name = self._load_unlocked(project_id).get("name", "")
            except (OSError, ValueError, json.JSONDecodeError):
                # A project too damaged to read is one of the few a person
                # can do nothing with except delete it.
                name = ""
            process = self._processes.get(project_id)
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        with self._lock:
            self._processes.pop(project_id, None)
            self._cancel_requested.discard(project_id)
            shutil.rmtree(directory, ignore_errors=True)
        return {"deleted": project_id, "name": name}

    def save_details(
        self, project_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist the vehicle form and manual facts as they are typed.

        Someone copying wheelbase and steering ratio out of a service manual
        should not lose that work to a page reload, and until now nothing
        stored it until analysis started. This is a draft: half-typed values
        are kept verbatim, and the real validation still happens when the
        decoder runs.
        """
        vehicle_info = load_vehicle_info(payload.get("manual_info") or {})
        try:
            bus = int(payload.get("bus", 0))
        except (TypeError, ValueError):
            bus = 0
        try:
            min_frames = int(payload.get("min_frames", 40))
        except (TypeError, ValueError):
            min_frames = 40
        form = {
            "name": str(payload.get("name", "")).strip()[:100],
            "brand": str(payload.get("brand", "")).strip().lower()[:40],
            "bus": min(max(bus, 0), 15),
            "min_frames": min(max(min_frames, 5), 100000),
            "strict": bool(payload.get("strict")),
        }

        def update(current: dict[str, Any]) -> None:
            current["form"] = form
            current["manual_info"] = vehicle_info.to_dict()

        return self._mutate(project_id, update)

    def _mutate(
        self, project_id: str, function: Callable[[dict[str, Any]], None]
    ) -> dict[str, Any]:
        with self._lock:
            state = self._load_unlocked(project_id)
            function(state)
            self._save_unlocked(state)
            return _json_clone(state)

    def _file_info(self, path: Path, display_name: str) -> dict[str, Any]:
        return {
            "name": display_name,
            "size": path.stat().st_size,
            "modified_at": _now(),
        }

    def upload(
        self,
        project_id: str,
        kind: str,
        source,
        *,
        length: int,
        filename: str | None,
    ) -> dict[str, Any]:
        if kind not in _UPLOAD_KINDS:
            raise ValueError(
                "upload kind must be capture, extra_capture, reference, "
                "or firmware"
            )
        if length <= 0:
            raise ValueError("the uploaded file is empty")
        if length > _MAX_UPLOAD:
            raise ValueError("upload is larger than the 8 GiB local safety limit")
        state = self.get(project_id)
        if state["busy"]:
            raise RuntimeError("wait for the current task to finish before uploading")
        directory = self._dir(project_id)
        display_name = _safe_filename(filename, f"{kind}.log")
        if kind == "capture":
            suffix = ".csv" if Path(display_name).suffix.lower() == ".csv" else ".log"
            target = directory / f"capture{suffix}"
            alternate = directory / ("capture.log" if suffix == ".csv" else "capture.csv")
        elif kind == "extra_capture":
            if not state.get("files", {}).get("capture"):
                raise ValueError("add the first capture before merging others onto it")
            existing = state.get("files", {}).get("extra_captures") or []
            if len(existing) >= _MAX_EXTRA_CAPTURES:
                raise ValueError(
                    f"at most {_MAX_EXTRA_CAPTURES} extra captures can be merged"
                )
            suffix = ".csv" if Path(display_name).suffix.lower() == ".csv" else ".log"
            target = directory / f"extra-{secrets.token_hex(6)}{suffix}"
            alternate = None
        elif kind == "reference":
            target = directory / "reference.csv"
            alternate = None
        else:
            target = directory / "firmware.log"
            alternate = None
        temporary = directory / f".{target.name}.{secrets.token_hex(5)}.upload"
        remaining = length
        with temporary.open("wb") as out:
            while remaining:
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    temporary.unlink(missing_ok=True)
                    raise ValueError("upload ended before Content-Length bytes arrived")
                out.write(block)
                remaining -= len(block)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, target)
        if alternate is not None:
            alternate.unlink(missing_ok=True)

        def update(current: dict[str, Any]) -> None:
            entry = {
                **self._file_info(target, display_name),
                "stored_as": target.name,
            }
            if kind == "extra_capture":
                current["files"].setdefault("extra_captures", []).append(entry)
            else:
                current["files"][kind] = entry
            current["stage"] = "details"
            current["status"] = "ready"
            current["status_message"] = f"{display_name} is ready."
            current["analysis"] = None
            current.pop("analysis_options", None)
            current["port"] = None
            current["review"] = []

        return self._mutate(project_id, update)

    def start_demo(self, project_id: str) -> dict[str, Any]:
        directory = self._dir(project_id)
        vehicle_info_path = directory / "vehicle-info.json"
        args = [
            "synth",
            "--duration", "18",
            "--output", str(directory / "capture.log"),
            "--reference", str(directory / "reference.csv"),
            "--truth", str(directory / "truth.json"),
            # Complete enough that decoding and porting this project needs no
            # further input: the demo is meant to show the whole pipeline,
            # including the human-supplied facts that finish a port, not only
            # the read-only half a bare capture can produce on its own. This
            # is safe only because the capture is synthetic -- see
            # autodistill_can.demo, which this refuses to run against
            # anything else.
            "--vehicle-info-out", str(vehicle_info_path),
            "--quiet",
        ]

        def done(state: dict[str, Any]) -> None:
            state["name"] = "Demo vehicle"
            state["files"]["capture"] = {
                **self._file_info(directory / "capture.log", "demo-drive.log"),
                "stored_as": "capture.log",
            }
            state["files"]["reference"] = {
                **self._file_info(directory / "reference.csv", "demo-reference.csv"),
                "stored_as": "reference.csv",
            }
            state["vehicle"] = {
                "name": "AUTODISTILL DEMO CAR",
                "brand": "autodistill_demo",
                "bus": 0,
            }
            # Keep the form the user sees in step 2 in step with what the
            # demo just decided on their behalf.
            state["form"] = {**state.get("form", {}), **state["vehicle"]}
            state["manual_info"] = load_vehicle_info(
                json.loads(vehicle_info_path.read_text())
            ).to_dict()
            state["stage"] = "details"
            state["status_message"] = (
                "Demo data is ready, with complete facts attached so this "
                "project can reach a full read+steer+drive port -- see them "
                "under ‘Facts AutoDistill cannot observe’."
            )
            # A project that already had a real capture analysed and ported
            # must not keep showing that stale result once the demo capture
            # has replaced it underneath.
            state["analysis"] = None
            state.pop("analysis_options", None)
            state["port"] = None
            state["review"] = []

        return self._start(project_id, "Creating demo drive", args, done)

    def start_capture(
        self, project_id: str, *, source: str, duration: float
    ) -> dict[str, Any]:
        if not (
            re.fullmatch(r"panda(?::[0-9]+)?", source)
            or re.fullmatch(r"socketcan:[A-Za-z0-9_.-]+", source)
        ):
            raise ValueError("source must look like panda:, panda:1, or socketcan:can0")
        if not 5 <= duration <= 3600:
            raise ValueError("capture duration must be between 5 and 3600 seconds")
        directory = self._dir(project_id)
        target = directory / "capture.log"
        args = [
            "capture", source, "--duration", str(duration),
            "--output", str(target), "--quiet",
        ]

        def done(state: dict[str, Any]) -> None:
            state["files"]["capture"] = {
                **self._file_info(target, f"{source.replace(':', '-')}-capture.log"),
                "stored_as": target.name,
            }
            state["stage"] = "details"
            state["status_message"] = "Hardware capture completed."
            state["analysis"] = None
            state.pop("analysis_options", None)
            state["port"] = None
            state["review"] = []

        return self._start(project_id, "Recording CAN traffic", args, done)

    def start_probe(
        self,
        project_id: str,
        *,
        interface: str,
        authorised: bool,
        parked: bool,
    ) -> dict[str, Any]:
        if not authorised or not parked:
            raise ValueError(
                "firmware probing requires ownership/authorisation and a parked car"
            )
        if not _INTERFACE.fullmatch(interface) or ":" in interface:
            raise ValueError("invalid SocketCAN interface name")
        directory = self._dir(project_id)
        output = directory / "firmware.txt"
        capture = directory / "firmware.log"
        args = [
            "probe", "--interface", interface, "--i-own-this-vehicle",
            "--output", str(output), "--capture", str(capture),
        ]

        def done(state: dict[str, Any]) -> None:
            state["files"]["firmware"] = {
                **self._file_info(capture, "firmware.log"),
                "stored_as": capture.name,
            }
            state["analysis"] = None
            state.pop("analysis_options", None)
            state["port"] = None
            state["review"] = []
            state["stage"] = "details"
            state["status_message"] = (
                "Firmware probe completed. Rerun analysis to include its responses."
            )

        return self._start(project_id, "Querying ECU firmware", args, done)

    def _capture_path(self, state: dict[str, Any]) -> Path:
        item = state.get("files", {}).get("capture")
        if not item:
            raise ValueError("add a CAN capture before continuing")
        path = self._dir(state["id"]) / item["stored_as"]
        if not path.is_file():
            raise ValueError("the CAN capture is missing from this project")
        return path

    def _vehicle(self, payload: dict[str, Any]) -> dict[str, Any]:
        brand = str(payload.get("brand", "")).strip().lower()
        name = str(payload.get("name", "")).strip()
        try:
            bus = int(payload.get("bus", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("bus must be a number") from exc
        if not _BRAND.fullmatch(brand):
            raise ValueError(
                "brand must start with a letter and contain only lowercase "
                "letters, numbers, and underscores"
            )
        if not name or len(name) > 100:
            raise ValueError("vehicle name is required and must be under 100 characters")
        if not 0 <= bus <= 15:
            raise ValueError("bus must be between 0 and 15")
        return {"brand": brand, "name": name, "bus": bus}

    def start_analysis(
        self, project_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        state = self.get(project_id)
        capture = self._capture_path(state)
        vehicle = self._vehicle(payload)
        manual_info = payload.get("manual_info", state.get("manual_info"))
        if manual_info is None:
            manual_info = state.get("manual_info")
        vehicle_info = load_vehicle_info(manual_info)
        try:
            min_frames = int(payload.get("min_frames", 40))
        except (TypeError, ValueError) as exc:
            raise ValueError("minimum frames must be a number") from exc
        if not 5 <= min_frames <= 100000:
            raise ValueError("minimum frames must be between 5 and 100000")
        directory = self._dir(project_id)
        report = directory / "analysis.json"
        dbc = directory / "recovered.dbc"
        vehicle_info_path = directory / "vehicle-info.json"
        args = [
            "analyse", str(capture), "--format", "json", "--output", str(report),
            "--dbc", str(dbc), "--name", vehicle["name"],
            "--brand", vehicle["brand"], "--bus", str(vehicle["bus"]),
            "--min-frames", str(min_frames), "--quiet",
        ]
        # Laid end to end after the first, never interleaved: see
        # frame.concatenate for what interleaving does to a message's cadence.
        for item in state.get("files", {}).get("extra_captures") or []:
            args.extend(["--add-log", str(directory / item["stored_as"])])
        reference = state.get("files", {}).get("reference")
        if reference:
            args.extend(["--reference", str(directory / reference["stored_as"])])
        firmware = state.get("files", {}).get("firmware")
        if firmware:
            args.extend(["--firmware-log", str(directory / firmware["stored_as"])])
        if bool(payload.get("strict")):
            args.append("--strict")
        if vehicle_info.has_content:
            args.extend(["--vehicle-info", str(vehicle_info_path)])

        def starting(current: dict[str, Any]) -> None:
            # Persist the form before analysis starts. If a signal override is
            # rejected against the recovered layout, the user's research is
            # still present when they reload the project and correct it.
            if vehicle_info.has_content:
                write_vehicle_info(vehicle_info, vehicle_info_path)
            else:
                vehicle_info_path.unlink(missing_ok=True)
            current["manual_info"] = vehicle_info.to_dict()

        def done(current: dict[str, Any]) -> None:
            analysis, review = _summary(report)
            current["name"] = vehicle["name"]
            current["vehicle"] = vehicle
            current["form"] = {**current.get("form", {}), **vehicle}
            current["analysis"] = analysis
            current["analysis_options"] = {
                "min_frames": min_frames,
                "strict": bool(payload.get("strict")),
            }
            # manual_info is not re-written here: `starting` already persisted
            # it before the (possibly long) analysis run, and re-applying that
            # same pre-run snapshot now would silently discard any edits made
            # through save_details while analysis was in progress.
            current["review"] = review
            current["port"] = None
            current["stage"] = "analysis"
            current["status_message"] = (
                f"Analysis found {analysis['messages']} messages and "
                f"{analysis['signals']} candidate signals."
            )

        return self._start(
            project_id, "Analysing CAN traffic", args, done,
            on_start=starting,
        )

    def start_port(self, project_id: str) -> dict[str, Any]:
        state = self.get(project_id)
        if not state.get("analysis"):
            raise ValueError("run analysis before generating a port")
        capture = self._capture_path(state)
        vehicle = state["vehicle"]
        directory = self._dir(project_id)
        port = directory / "port"
        args = [
            "port", str(capture), "--output", str(port),
            "--name", vehicle["name"], "--brand", vehicle["brand"],
            "--bus", str(vehicle["bus"]), "--force", "--quiet",
        ]
        options = state.get("analysis_options", {})
        args.extend(["--min-frames", str(options.get("min_frames", 40))])
        if options.get("strict"):
            args.append("--strict")
        # Laid end to end after the first, never interleaved: see
        # frame.concatenate for what interleaving does to a message's cadence.
        for item in state.get("files", {}).get("extra_captures") or []:
            args.extend(["--add-log", str(directory / item["stored_as"])])
        reference = state.get("files", {}).get("reference")
        if reference:
            args.extend(["--reference", str(directory / reference["stored_as"])])
        firmware = state.get("files", {}).get("firmware")
        if firmware:
            args.extend(["--firmware-log", str(directory / firmware["stored_as"])])
        # Rewrite from the saved form rather than trusting the file the
        # analysis step left behind: bindings and actuation facts are usually
        # added *after* seeing what the decoder found, and they have to reach
        # the port without forcing a full re-analysis first.
        vehicle_info_path = directory / "vehicle-info.json"
        info = load_vehicle_info(state.get("manual_info"))
        if info.has_content:
            write_vehicle_info(info, vehicle_info_path)
            args.extend(["--vehicle-info", str(vehicle_info_path)])
        else:
            vehicle_info_path.unlink(missing_ok=True)

        def done(current: dict[str, Any]) -> None:
            archive = directory / "port.zip"
            temporary_base = directory / ".port-archive"
            temporary = Path(
                shutil.make_archive(
                    str(temporary_base), "zip", root_dir=directory, base_dir="port"
                )
            )
            os.replace(temporary, archive)
            manifest = json.loads((port / "port_status.json").read_text())
            current["port"] = {
                "mode": manifest["mode"],
                "safe_for_control": manifest["safe_for_control"],
                # Whether the capture, replayed through the bindings, produced
                # values a real car could. Separate from the checklist: a
                # complete port can still be a wrong one.
                "validation": manifest.get("validation", {}),
                "evidence": manifest["evidence"],
                "manual_input": manifest.get("manual_input", {}),
                "human_required": manifest["human_required"],
                "size": archive.stat().st_size,
            }
            current["review"] = [
                _as_review_item(item) for item in manifest["human_required"]
            ]
            current["stage"] = "port"
            current["status_message"] = (
                "Read-only port generated. Download it or install it into OpenDBC."
                if manifest["mode"] == "read_only" else
                "Control port generated -- never safe_for_control regardless. "
                "Download it or install it into OpenDBC."
            )

        return self._start(project_id, "Generating read-only port", args, done)

    def start_install(
        self,
        project_id: str,
        *,
        opendbc_dir: str,
        confirmed: bool,
        force: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("installation requires explicit confirmation")
        state = self.get(project_id)
        if not state.get("port"):
            raise ValueError("generate a port before installing it")
        target = Path(opendbc_dir).expanduser()
        if not target.is_absolute():
            raise ValueError("OpenDBC path must be absolute")
        target = target.resolve()
        if not (target / "opendbc" / "car").is_dir():
            raise ValueError("that directory does not look like an OpenDBC checkout")
        args = [
            "install", str(self._dir(project_id) / "port"), str(target), "--quiet"
        ]
        if force:
            args.append("--force")

        def done(current: dict[str, Any]) -> None:
            current["status_message"] = f"Port installed into {target}."
            current["installed_to"] = str(target)

        return self._start(project_id, "Installing into OpenDBC", args, done)

    def _start(
        self,
        project_id: str,
        label: str,
        cli_args: list[str],
        on_success: Callable[[dict[str, Any]], None],
        *,
        on_start: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._load_unlocked(project_id)
            if state.get("status") == "running" or project_id in self._processes:
                raise RuntimeError("this project already has a task running")
            if on_start is not None:
                on_start(state)
            state["status"] = "running"
            state["status_message"] = label
            state["activity"].append({
                "label": label,
                "started_at": _now(),
                "status": "running",
            })
            state["activity"] = state["activity"][-20:]
            self._save_unlocked(state)

        thread = threading.Thread(
            target=self._run,
            args=(project_id, label, cli_args, on_success),
            name=f"autodistill-{project_id[:8]}",
            daemon=True,
        )
        thread.start()
        return self.get(project_id)

    def _run(
        self,
        project_id: str,
        label: str,
        cli_args: list[str],
        on_success: Callable[[dict[str, Any]], None],
    ) -> None:
        command = [sys.executable, "-m", "autodistill_can.cli", *cli_args]
        directory = self._dir(project_id)
        try:
            process = subprocess.Popen(
                command,
                cwd=directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            with self._lock:
                self._processes[project_id] = process
                # A cancel or a delete can land in the gap between starting
                # the thread and the child existing, and neither could have
                # signalled a process that had not been created yet.
                if project_id in self._cancel_requested or not directory.is_dir():
                    self._cancel_requested.discard(project_id)
                    process.terminate()
            stdout, stderr = process.communicate()
            returncode = process.returncode
        except OSError as exc:
            stdout, stderr, returncode = "", str(exc), 1
        with self._lock:
            self._processes.pop(project_id, None)
            self._cancel_requested.discard(project_id)
            try:
                state = self._load_unlocked(project_id)
            except (OSError, ValueError, json.JSONDecodeError):
                return
            activity = state["activity"][-1]
            activity["finished_at"] = _now()
            activity["log"] = (stderr or stdout).strip()[-12000:]
            if returncode == 0:
                try:
                    on_success(state)
                    state["status"] = "ready"
                    activity["status"] = "complete"
                except Exception as exc:  # noqa: BLE001 - this thread's last chance
                    # to record a terminal status: anything left uncaught here
                    # leaves the project stuck at "running" forever, since
                    # nothing else will call `_save_unlocked` for it.
                    state["status"] = "error"
                    state["status_message"] = f"{label} output could not be read: {exc}"
                    activity["status"] = "error"
                    activity["log"] = str(exc)
            elif returncode < 0:
                state["status"] = "ready"
                state["status_message"] = f"{label} was cancelled."
                activity["status"] = "cancelled"
            else:
                message = (stderr or stdout).strip()
                state["status"] = "error"
                state["status_message"] = message or f"{label} failed."
                activity["status"] = "error"
            self._save_unlocked(state)

    def cancel(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            process = self._processes.get(project_id)
            if process is None:
                state = self._load_unlocked(project_id)
                if state.get("status") != "running":
                    raise ValueError("there is no running task to cancel")
                self._cancel_requested.add(project_id)
            else:
                process.terminate()
        return self.get(project_id)

    def inventory(self, project_id: str) -> dict[str, Any]:
        """Messages and signals the last analysis found, for binding by hand."""
        report = self._dir(project_id) / "analysis.json"
        if not report.is_file():
            raise FileNotFoundError("run the decoder before binding signals")
        return {"messages": _inventory(json.loads(report.read_text()))}

    def download(self, project_id: str, artifact: str) -> tuple[Path, str]:
        if artifact not in _DOWNLOADS:
            raise ValueError("unknown artifact")
        filename, content_type = _DOWNLOADS[artifact]
        path = self._dir(project_id) / filename
        if artifact == "capture":
            state = self.get(project_id)
            item = state.get("files", {}).get("capture")
            if item:
                path = self._dir(project_id) / item["stored_as"]
        if not path.is_file():
            raise FileNotFoundError("artifact is not ready")
        return path, content_type


class AutoDistillServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        store: ProjectStore,
        token: str,
    ):
        super().__init__(address, AutoDistillHandler)
        self.store = store
        self.api_token = token


class AutoDistillHandler(BaseHTTPRequestHandler):
    server: AutoDistillServer
    protocol_version = "HTTP/1.1"
    server_version = "AutoDistillUI"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(
            f"[AutoDistill UI] {self.address_string()} - {format % args}\n"
        )

    def _headers(
        self,
        status: int,
        content_type: str,
        length: int,
        *,
        download_name: str | None = None,
        cache: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header(
            "Cache-Control",
            "public, max-age=3600" if cache else "no-store",
        )
        if download_name:
            quoted = urllib.parse.quote(download_name)
            self.send_header(
                "Content-Disposition", f"attachment; filename*=UTF-8''{quoted}"
            )
        self.end_headers()

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        self._headers(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def _error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status)

    def _token_valid(self) -> bool:
        supplied = self.headers.get("X-AutoDistill-Token", "")
        return hmac.compare_digest(supplied, self.server.api_token)

    def _host_valid(self) -> bool:
        host = self.headers.get("Host", "")
        try:
            hostname = urllib.parse.urlsplit(f"//{host}").hostname
        except ValueError:
            return False
        return hostname in {"127.0.0.1", "localhost", "::1"}

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > _MAX_JSON:
            raise ValueError("JSON body must be between 1 byte and 1 MiB")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("request body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request JSON must be an object")
        return payload

    def _segments(self) -> list[str]:
        path = urllib.parse.urlsplit(self.path).path
        return [urllib.parse.unquote(item) for item in path.split("/") if item]

    def do_GET(self) -> None:
        if not self._host_valid():
            self._error(HTTPStatus.FORBIDDEN, "invalid host")
            return
        try:
            self._do_get()
        except FileNotFoundError as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc))
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))

    def do_HEAD(self) -> None:
        """Return metadata for the shell and static assets without a body."""
        if not self._host_valid():
            self._headers(HTTPStatus.FORBIDDEN, "text/plain", 0)
            return
        segments = self._segments()
        if not segments:
            template = (
                resources.files("autodistill_can.web")
                .joinpath("static", "index.html")
                .read_text()
            )
            length = len(
                template.replace("__API_TOKEN__", self.server.api_token)
                .replace("__APP_VERSION__", __version__)
                .encode()
            )
            self._headers(HTTPStatus.OK, "text/html; charset=utf-8", length)
            return
        if (
            segments[0] == "static"
            and len(segments) == 2
            and re.fullmatch(r"[A-Za-z0-9_.-]+", segments[1])
        ):
            asset = resources.files("autodistill_can.web").joinpath(
                "static", segments[1]
            )
            if asset.is_file():
                content_type = (
                    mimetypes.guess_type(segments[1])[0]
                    or "application/octet-stream"
                )
                if (
                    content_type.startswith("text/")
                    or content_type == "application/javascript"
                ):
                    content_type += "; charset=utf-8"
                self._headers(
                    HTTPStatus.OK,
                    content_type,
                    len(asset.read_bytes()),
                    cache=True,
                )
                return
        self._headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", 0)

    def _do_get(self) -> None:
        segments = self._segments()
        if segments and segments[0] == "api" and not self._token_valid():
            self._error(HTTPStatus.FORBIDDEN, "invalid local API token")
            return
        if not segments:
            template = (
                resources.files("autodistill_can.web")
                .joinpath("static", "index.html")
                .read_text()
            )
            data = (
                template.replace("__API_TOKEN__", self.server.api_token)
                .replace("__APP_VERSION__", __version__)
                .encode()
            )
            self._headers(
                HTTPStatus.OK, "text/html; charset=utf-8", len(data)
            )
            self.wfile.write(data)
            return
        if segments[0] == "static" and len(segments) == 2:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", segments[1]):
                raise FileNotFoundError("static asset not found")
            asset = resources.files("autodistill_can.web").joinpath(
                "static", segments[1]
            )
            if not asset.is_file():
                raise FileNotFoundError("static asset not found")
            data = asset.read_bytes()
            content_type = mimetypes.guess_type(segments[1])[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type == "application/javascript":
                content_type += "; charset=utf-8"
            self._headers(HTTPStatus.OK, content_type, len(data), cache=True)
            self.wfile.write(data)
            return
        if segments == ["api", "status"]:
            self._send_json(_system_status(self.server.store))
            return
        if segments == ["api", "projects"]:
            self._send_json({"projects": self.server.store.list()})
            return
        if len(segments) == 3 and segments[:2] == ["api", "projects"]:
            self._send_json(self.server.store.get(segments[2]))
            return
        if (
            len(segments) == 4
            and segments[:2] == ["api", "projects"]
            and segments[3] == "inventory"
        ):
            self._send_json(self.server.store.inventory(segments[2]))
            return
        if (
            len(segments) == 5
            and segments[:2] == ["api", "projects"]
            and segments[3] == "download"
        ):
            path, content_type = self.server.store.download(
                segments[2], segments[4]
            )
            self._headers(
                HTTPStatus.OK,
                content_type,
                path.stat().st_size,
                download_name=path.name,
            )
            with path.open("rb") as fh:
                shutil.copyfileobj(fh, self.wfile, length=1024 * 1024)
            return
        raise FileNotFoundError("page not found")

    def do_DELETE(self) -> None:
        if not self._host_valid():
            self._error(HTTPStatus.FORBIDDEN, "invalid host")
            return
        if not self._token_valid():
            self._error(HTTPStatus.FORBIDDEN, "invalid local API token")
            return
        try:
            segments = self._segments()
            if len(segments) != 3 or segments[:2] != ["api", "projects"]:
                raise FileNotFoundError("API route not found")
            self._send_json(self.server.store.delete(segments[2]))
        except FileNotFoundError as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc))
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))

    def do_POST(self) -> None:
        if not self._host_valid():
            self._error(HTTPStatus.FORBIDDEN, "invalid host")
            return
        if not self._token_valid():
            self._error(HTTPStatus.FORBIDDEN, "invalid local API token")
            return
        try:
            self._do_post()
        except FileNotFoundError as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc))
        except RuntimeError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))

    def _do_post(self) -> None:
        segments = self._segments()
        if segments == ["api", "projects"]:
            payload = self._read_json()
            self._send_json(
                self.server.store.create(str(payload.get("name", "My vehicle"))),
                HTTPStatus.CREATED,
            )
            return
        if len(segments) < 4 or segments[:2] != ["api", "projects"]:
            raise FileNotFoundError("API route not found")
        project_id, action = segments[2], segments[3]
        if action == "upload" and len(segments) == 5:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid Content-Length") from exc
            state = self.server.store.upload(
                project_id,
                segments[4],
                self.rfile,
                length=length,
                filename=urllib.parse.unquote(self.headers.get("X-Filename", "")),
            )
            self._send_json(state)
            return
        payload = self._read_json()
        store = self.server.store
        if action == "details":
            # A save, not a job: it answers 200 with the stored state.
            self._send_json(store.save_details(project_id, payload))
            return
        if action == "demo":
            state = store.start_demo(project_id)
        elif action == "capture":
            state = store.start_capture(
                project_id,
                source=str(payload.get("source", "")),
                duration=float(payload.get("duration", 0)),
            )
        elif action == "probe":
            state = store.start_probe(
                project_id,
                interface=str(payload.get("interface", "can0")),
                authorised=bool(payload.get("authorised")),
                parked=bool(payload.get("parked")),
            )
        elif action == "analyse":
            state = store.start_analysis(project_id, payload)
        elif action == "port":
            state = store.start_port(project_id)
        elif action == "install":
            state = store.start_install(
                project_id,
                opendbc_dir=str(payload.get("opendbc_dir", "")),
                confirmed=bool(payload.get("confirmed")),
                force=bool(payload.get("force")),
            )
        elif action == "cancel":
            state = store.cancel(project_id)
        elif action == "drop_extra_capture":
            self._send_json(store.drop_extra_capture(
                project_id, str(payload.get("stored_as", ""))
            ))
            return
        else:
            raise FileNotFoundError("API action not found")
        self._send_json(state, HTTPStatus.ACCEPTED)


def _system_status(store: ProjectStore) -> dict[str, Any]:
    can_interfaces = []
    net = Path("/sys/class/net")
    if net.is_dir():
        for interface in net.iterdir():
            try:
                if (interface / "type").read_text().strip() == "280":
                    can_interfaces.append(interface.name)
            except OSError:
                continue
    return {
        "app": "AutoDistill",
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "workspace": str(store.root),
        "privacy": "All files stay on this machine.",
        "panda_ready": importlib.util.find_spec("usb1") is not None,
        "socketcan_ready": sys.platform.startswith("linux"),
        "can_interfaces": sorted(can_interfaces),
        "ecu_types": list(ECU_TYPES),
        # The subset openpilot will actually fingerprint on. Identifying only
        # non-essential modules leaves FW_VERSIONS empty, so the guided panel
        # needs to know the difference to ask for the right thing.
        "essential_ecu_types": list(ESSENTIAL_ECU_TYPES),
        "carstate_targets": list(CARSTATE_TARGETS),
        # Everything a person can bind or declare, so the browser offers the
        # same vocabulary the validator will accept.
        "carstate_fields": list(CARSTATE_FIELDS),
        "carstate_transforms": list(CARSTATE_TRANSFORMS),
        "actuation_values": list(ACTUATION_VALUES),
        "safety_models": list(SAFETY_MODELS),
        "gear_shifters": list(GEAR_SHIFTERS),
        "stock_capture_answers": list(STOCK_CAPTURE_ANSWERS),
    }


def create_server(
    *,
    port: int = 8765,
    workspace: Path | str | None = None,
    token: str | None = None,
) -> AutoDistillServer:
    """Create a loopback-only UI server; useful for CLI startup and tests."""
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    root = workspace or (Path.home() / ".autodistill" / "projects")
    return AutoDistillServer(
        ("127.0.0.1", port),
        ProjectStore(root),
        token or secrets.token_urlsafe(32),
    )


def run_server(
    *,
    port: int = 8765,
    workspace: Path | str | None = None,
    open_browser: bool = True,
) -> None:
    """Run the local UI until interrupted."""
    server = create_server(port=port, workspace=workspace)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"AutoDistill UI: {url}", file=sys.stderr)
    print(f"Projects stay local in {server.store.root}", file=sys.stderr)
    print(
        'New to this? Leave "Guide me" on -- it walks you through the whole '
        "port,\none step at a time, and shows what to do next.",
        file=sys.stderr,
    )
    if open_browser:
        threading.Timer(0.35, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nAutoDistill UI stopped.", file=sys.stderr)
    finally:
        server.server_close()
