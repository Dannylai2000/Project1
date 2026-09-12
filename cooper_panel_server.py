"""Cooper control API — HTTP bridge to the AgiBot X2 (ROS2 / AimDK).

Runs ON the robot and exposes the REST API the Cooper Control Panel
webpage (hosted on optimus) talks to:

    GET  /api/status        → server + mic/speaker/show state
    GET  /api/dances        → LinkCraft dance resources (live from the robot)
    GET  /api/actions       → one-tap gestures
    GET  /api/shortlist     → shared shortlist + per-dance play times
    POST /api/dance         → {"key": "..."} start that dance now
    POST /api/listening     → {"listen": true|false} mic on / off
    POST /api/speaker       → {"on": true|false} speaker on / muted
    POST /api/action        → {"action": "..."} run a gesture
    POST /api/shortlist     → save shortlist / play times
    POST /api/songs_seen    → acknowledge new songs
    POST /api/show          → run the full showroom sequence

Standard library only — no Flask/aiohttp needed on the robot.

Run on the robot:
    source /opt/ros/humble/setup.bash && source ~/aimdk/install/setup.bash
    python3 cooper_panel_server.py --port 8080
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import rclpy
from aimdk_msgs.srv import ExecuteActionResource, GetRobotResources
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

try:
    from aimdk_msgs.srv import SetMute
except ImportError:  # pragma: no cover
    SetMute = None

# SetVolume drives the speaker (0-100); muting = volume 0.
try:
    from aimdk_msgs.srv import SetVolume
except ImportError:  # pragma: no cover
    SetVolume = None


LOGGER = logging.getLogger("cooper_panel")

DEFAULT_GET_RESOURCES_SVC  = "/aimdk_5Fmsgs/srv/GetRobotResources"
DEFAULT_EXECUTE_ACTION_SVC = "/aimdk_5Fmsgs/srv/ExecuteActionResource"
DEFAULT_SET_MUTE_SVC       = "/aimdk_5Fmsgs/srv/SetMute"
DEFAULT_SET_VOLUME_SVC     = "/aimdk_5Fmsgs/srv/SetVolume"

# One-tap gestures for the panel's Actions card. Motion/area IDs follow the
# AimDK preset-motion table (1001 raise, 1002 wave, 1003 handshake,
# 1004 airkiss); heart, wave, and blow kiss match the working show script.
ACTIONS = {
    "shake_hand":   {"label": "Shake hand",         "emoji": "🤝", "motion": 1003, "area": 2},
    "heart":        {"label": "Heart sign",          "emoji": "🫶", "motion": 1007, "area": 3},
    "wave_goodbye": {"label": "Right-hand goodbye",  "emoji": "👋", "motion": 1002, "area": 2},
    "wave_left":    {"label": "Left-hand wave",      "emoji": "🖐️", "motion": 1002, "area": 1},
    "blow_kiss":    {"label": "Blow kiss",           "emoji": "😘", "motion": 1004, "area": 2},
}

SHOW_SCRIPT     = Path(__file__).resolve().parent / "x2_showroom_demo.py"

# Gestures run in their own program, invoked per Action press.
ACTION_SCRIPT   = Path(__file__).resolve().parent / "x2_action.py"

# Shared panel settings (dance shortlist + play times), one file for all
# devices, stored next to the server on Cooper.
CONFIG_FILE     = Path(__file__).resolve().parent / "cooper_panel_config.json"

# Milestones written by the running show script (first speech, dance start),
# read back for the panel's performance diagnostics.
TIMING_FILE     = Path(__file__).resolve().parent / "cooper_show_timing.json"


class CooperPanelNode(Node):
    """ROS2 side of the panel: talks to the AimDK services."""

    def __init__(self, mute_service: str, speaker_volume: int = 70) -> None:
        super().__init__("cooper_panel")
        self._speaker_volume = max(1, min(100, int(speaker_volume)))
        self._cbg = MutuallyExclusiveCallbackGroup()
        self._lock = threading.Lock()

        self._get_resources = self.create_client(
            GetRobotResources, DEFAULT_GET_RESOURCES_SVC, callback_group=self._cbg
        )
        self._exec_action = self.create_client(
            ExecuteActionResource, DEFAULT_EXECUTE_ACTION_SVC, callback_group=self._cbg
        )
        self._set_mute = None
        if SetMute is not None:
            self._set_mute = self.create_client(
                SetMute, mute_service, callback_group=self._cbg
            )
        self._set_volume = None
        if SetVolume is not None:
            self._set_volume = self.create_client(
                SetVolume, DEFAULT_SET_VOLUME_SVC, callback_group=self._cbg
            )

        # Last listening/speaker/volume states we set (None until first change).
        self.listening_state: bool | None = None
        self.speaker_state: bool | None = None
        self.volume_state: int | None = None
        # Cache of resource_key -> resource, refreshed by list_dances().
        self._resource_cache: dict = {}

    # ── helpers ────────────────────────────────────────────────────────────

    def _wait(self, future, timeout: float):
        done = threading.Event()
        future.add_done_callback(lambda _: done.set())
        return future.result() if done.wait(timeout) else None

    def _stamp(self, req) -> None:
        with suppress(Exception):
            now = self.get_clock().now()
            req.header.header.stamp.sec     = now.nanoseconds // 1_000_000_000
            req.header.header.stamp.nanosec = now.nanoseconds % 1_000_000_000
        with suppress(Exception):
            req.header.stamp = self.get_clock().now().to_msg()

    # ── API operations ─────────────────────────────────────────────────────

    def list_dances(self) -> list[dict]:
        """Return LinkCraft resources as [{key, name, version}, ...]."""
        if not self._get_resources.wait_for_service(timeout_sec=5.0):
            raise RuntimeError("GetRobotResources service not available")

        req = GetRobotResources.Request()
        self._stamp(req)
        resp = self._wait(self._get_resources.call_async(req), 20.0)
        if resp is None:
            raise RuntimeError("GetRobotResources timed out")

        dances = []
        with self._lock:
            self._resource_cache.clear()
            for r in resp.robot_resources:
                key = str(r.resource_key)
                if "linkcraft" not in key.lower():
                    continue
                self._resource_cache[key] = r
                name = ""
                for attr in ("resource_name", "name", "display_name", "description"):
                    value = getattr(r, attr, "")
                    if value:
                        name = str(value)
                        break
                version = ""
                with suppress(Exception):
                    version = str(r.current_version.version)
                dances.append({"key": key, "name": name or key, "version": version})
        return dances

    def start_dance(self, resource_key: str) -> dict:
        """Start a LinkCraft dance by resource key. Includes timing breakdown."""
        timing: dict = {}
        t0 = time.perf_counter()
        with self._lock:
            resource = self._resource_cache.get(resource_key)
        if resource is None:
            # Cache miss (server restarted, stale page) — refresh and retry.
            self.list_dances()
            with self._lock:
                resource = self._resource_cache.get(resource_key)
        if resource is None:
            raise RuntimeError(f"dance resource {resource_key!r} not found on robot")
        timing["library_lookup_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        t0 = time.perf_counter()
        if not self._exec_action.wait_for_service(timeout_sec=5.0):
            raise RuntimeError("ExecuteActionResource service not available")
        timing["service_wait_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        req = ExecuteActionResource.Request()
        self._stamp(req)
        req.resource_key     = resource.resource_key
        req.resource_version = resource.current_version.version
        req.slaves = []
        req.meta = (
            '{"resource_type": "BODY_MONTION"}'
            if "onnx" in resource_key.lower()
            else '{"resource_type": "ARM_MONTION"}'
        )

        t0 = time.perf_counter()
        resp = self._wait(self._exec_action.call_async(req), 20.0)
        if resp is None:
            raise RuntimeError("ExecuteActionResource timed out")
        timing["robot_ack_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        code, msg = 0, ""
        with suppress(AttributeError, TypeError, ValueError):
            code = int(resp.header.header.code)
            msg  = str(resp.header.message or "").strip()
        if code not in (None, 0) or any(w in msg.lower() for w in ("fail", "error", "reject")):
            raise RuntimeError(f"dance rejected (code={code}): {msg}")
        return {"code": code, "message": msg, "timing": timing}

    def set_listening(self, listen: bool) -> dict:
        """Unmute (listen=True) or mute (listen=False) Cooper's microphones.

        Returns a timing breakdown for the performance diagnostics.
        """
        timing: dict = {}
        if self._set_mute is None:
            raise RuntimeError("SetMute not available in this aimdk_msgs build")
        t0 = time.perf_counter()
        if not self._set_mute.wait_for_service(timeout_sec=5.0):
            raise RuntimeError("SetMute service not available")
        timing["service_wait_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        req = SetMute.Request()
        self._stamp(req)
        req.is_mute = not listen

        response = None
        t0 = time.perf_counter()
        for _ in range(8):
            future = self._set_mute.call_async(req)
            done = threading.Event()
            future.add_done_callback(lambda _: done.set())
            if done.wait(0.5) and future.done():
                response = future.result()
                break
        if response is None:
            raise RuntimeError("SetMute timed out")
        timing["robot_ack_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self.listening_state = listen
        LOGGER.info("Microphone %s", "UNMUTED (listening)" if listen else "MUTED")
        return timing

    def _send_volume(self, volume: int) -> dict:
        """Send a SetVolume request (0-100). Returns a timing breakdown."""
        timing: dict = {}
        if self._set_volume is None:
            raise RuntimeError("SetVolume not available in this aimdk_msgs build")
        t0 = time.perf_counter()
        if not self._set_volume.wait_for_service(timeout_sec=5.0):
            raise RuntimeError("SetVolume service not available")
        timing["service_wait_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        req = SetVolume.Request()
        self._stamp(req)
        # Field names vary between SDK builds — set whichever exists.
        for holder in (req, getattr(req, "volume_req", None)):
            if holder is None:
                continue
            for field in ("audio_volume", "volume"):
                if hasattr(holder, field):
                    setattr(holder, field, int(volume))

        response = None
        t0 = time.perf_counter()
        for _ in range(8):
            future = self._set_volume.call_async(req)
            done = threading.Event()
            future.add_done_callback(lambda _: done.set())
            if done.wait(0.5) and future.done():
                response = future.result()
                break
        if response is None:
            raise RuntimeError("SetVolume timed out")
        timing["robot_ack_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return timing

    def set_speaker(self, on: bool) -> dict:
        """Speaker on (restore volume) or muted (volume 0), via SetVolume."""
        volume = self._speaker_volume if on else 0
        timing = self._send_volume(volume)
        self.speaker_state = on
        self.volume_state = volume
        LOGGER.info("Speaker %s", f"ON (volume {volume})" if on else "MUTED (volume 0)")
        return timing

    def set_volume(self, level: int) -> dict:
        """Set the speaker volume directly (0-100); 0 counts as muted."""
        level = max(0, min(100, int(level)))
        timing = self._send_volume(level)
        self.volume_state = level
        self.speaker_state = level > 0
        if level > 0:
            self._speaker_volume = level  # what a later "Speaker On" restores
        LOGGER.info("Speaker volume set to %d", level)
        return timing


class ShowRunner:
    """Launches the full showroom sequence as a subprocess (one at a time).

    Mirrors the show's microphone behaviour into node.listening_state so the
    panel status stays truthful: the show mutes at start, and unmutes at the
    end only when unmute_after is requested.
    """

    def __init__(self, node: CooperPanelNode) -> None:
        self._node = node
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._last_requested: float | None = None

    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def start(
        self,
        dance_key: str | None,
        unmute_after: bool,
        greeting: str | None = None,
        intro: str | None = None,
        goodbye: str | None = None,
        dance_duration: float | None = None,
        volume: int | None = None,
    ) -> dict:
        t0 = time.perf_counter()
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise RuntimeError("a show is already running")
            cmd = [sys.executable, str(SHOW_SCRIPT)]
            if dance_key:
                cmd += ["--dance-key", dance_key]
            if unmute_after:
                cmd += ["--unmute-after"]
            if greeting:
                cmd += ["--greeting-text", greeting]
            if intro:
                cmd += ["--intro-text", intro]
            if goodbye:
                cmd += ["--goodbye-text", goodbye]
            if dance_duration is not None:
                cmd += ["--dance-duration", str(dance_duration)]
            if volume is not None:
                cmd += ["--volume", str(int(volume))]
            cmd += ["--timing-file", str(TIMING_FILE)]
            with suppress(OSError):
                TIMING_FILE.unlink()
            self._last_requested = time.time()
            LOGGER.info("Starting show: %s", " ".join(cmd))
            self._proc = subprocess.Popen(cmd)
            proc = self._proc

        # The show's first step mutes the microphones.
        self._node.listening_state = False

        def watch() -> None:
            proc.wait()
            if unmute_after:
                self._node.listening_state = True
            LOGGER.info("Show finished (exit code %s)", proc.returncode)

        threading.Thread(target=watch, name="show-watch", daemon=True).start()
        return {"launch_ms": round((time.perf_counter() - t0) * 1000, 1)}

    def show_timing(self) -> dict | None:
        """Milestones of the latest show, in ms from the panel's click."""
        with self._lock:
            requested = self._last_requested
        if requested is None:
            return None
        try:
            events = json.loads(TIMING_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        out: dict = {"requested_at": requested}
        for event in ("script_start", "first_speech", "dance_started",
                      "goodbye_speech", "show_complete"):
            t = events.get(event)
            if isinstance(t, (int, float)) and t >= requested:
                out[f"{event}_ms"] = round((t - requested) * 1000, 1)
        return out if len(out) > 1 else None


class PanelConfig:
    """Panel settings shared by all devices, persisted as a JSON file."""

    MAX_KEYS = 500
    MAX_KEY_LEN = 200

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def get_shortlist(self) -> list[str]:
        with self._lock:
            try:
                data = json.loads(self._path.read_text())
                items = data.get("shortlist", [])
            except (OSError, json.JSONDecodeError):
                return []
        return [str(k) for k in items if isinstance(k, str)]

    def set_shortlist(self, keys) -> list[str]:
        if not isinstance(keys, list):
            raise ValueError("shortlist must be a list of resource keys")
        cleaned = []
        for k in keys[: self.MAX_KEYS]:
            k = str(k).strip()[: self.MAX_KEY_LEN]
            if k and k not in cleaned:
                cleaned.append(k)
        with self._lock:
            try:
                data = json.loads(self._path.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
            data["shortlist"] = cleaned
            self._path.write_text(json.dumps(data, indent=2))
        LOGGER.info("Shortlist saved: %d song(s)", len(cleaned))
        return cleaned

    MAX_DANCE_TIME_S = 600.0

    # Two user-customizable gesture buttons (label + preset motion/area ids).
    DEFAULT_CUSTOM_ACTIONS = [
        {"label": "Right-hand wave", "motion": 1002, "area": 2},
        {"label": "Both-hands heart", "motion": 1007, "area": 3},
    ]

    def get_custom_actions(self) -> list[dict]:
        with self._lock:
            try:
                data = json.loads(self._path.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
        stored = data.get("custom_actions")
        out = []
        for i, default in enumerate(self.DEFAULT_CUSTOM_ACTIONS):
            entry = dict(default)
            if isinstance(stored, list) and i < len(stored) and isinstance(stored[i], dict):
                with suppress(TypeError, ValueError):
                    entry = {
                        "label": str(stored[i].get("label") or default["label"])[:40],
                        "motion": int(stored[i].get("motion", default["motion"])),
                        "area": int(stored[i].get("area", default["area"])),
                    }
            out.append(entry)
        return out

    def set_custom_actions(self, actions) -> list[dict]:
        if not isinstance(actions, list) or len(actions) != len(self.DEFAULT_CUSTOM_ACTIONS):
            raise ValueError(f"expected a list of {len(self.DEFAULT_CUSTOM_ACTIONS)} actions")
        cleaned = []
        for i, a in enumerate(actions):
            if not isinstance(a, dict):
                raise ValueError("each action must be an object")
            default = self.DEFAULT_CUSTOM_ACTIONS[i]
            label = str(a.get("label") or default["label"]).strip()[:40] or default["label"]
            try:
                motion = max(0, min(9999, int(a.get("motion", default["motion"]))))
                area = max(0, min(99, int(a.get("area", default["area"]))))
            except (TypeError, ValueError):
                raise ValueError("motion and area must be numbers")
            cleaned.append({"label": label, "motion": motion, "area": area})
        with self._lock:
            try:
                data = json.loads(self._path.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
            data["custom_actions"] = cleaned
            self._path.write_text(json.dumps(data, indent=2))
        LOGGER.info("Custom action buttons saved: %s",
                    ", ".join(a["label"] for a in cleaned))
        return cleaned

    def get_dance_times(self) -> dict:
        """Per-song play time in seconds ({} entries mean the show default)."""
        with self._lock:
            try:
                data = json.loads(self._path.read_text())
            except (OSError, json.JSONDecodeError):
                return {}
        times = data.get("dance_times", {})
        if not isinstance(times, dict):
            return {}
        out = {}
        for k, v in times.items():
            with suppress(TypeError, ValueError):
                out[str(k)] = float(v)
        return out

    def set_dance_times(self, times) -> dict:
        if not isinstance(times, dict):
            raise ValueError("times must be an object of {resource_key: seconds}")
        cleaned = {}
        for k, v in list(times.items())[: self.MAX_KEYS]:
            k = str(k).strip()[: self.MAX_KEY_LEN]
            if not k:
                continue
            with suppress(TypeError, ValueError):
                cleaned[k] = min(max(float(v), 0.0), self.MAX_DANCE_TIME_S)
        with self._lock:
            try:
                data = json.loads(self._path.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
            data["dance_times"] = cleaned
            self._path.write_text(json.dumps(data, indent=2))
        LOGGER.info("Dance play times saved for %d song(s)", len(cleaned))
        return cleaned

    def get_show_dance(self) -> str:
        """Resource key of the dance the full show uses ("" = script default)."""
        with self._lock:
            try:
                data = json.loads(self._path.read_text())
            except (OSError, json.JSONDecodeError):
                return ""
        return str(data.get("show_dance") or "")[: self.MAX_KEY_LEN]

    def set_show_dance(self, key) -> str:
        key = str(key or "").strip()[: self.MAX_KEY_LEN]
        with self._lock:
            try:
                data = json.loads(self._path.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
            data["show_dance"] = key
            self._path.write_text(json.dumps(data, indent=2))
        LOGGER.info("Show dance set to %r", key or "(script default)")
        return key

    def get_seen_songs(self) -> list[str] | None:
        """Song keys every device has already been shown. None = never set."""
        with self._lock:
            try:
                data = json.loads(self._path.read_text())
            except (OSError, json.JSONDecodeError):
                return None
        items = data.get("seen_songs")
        if items is None:
            return None
        return [str(k) for k in items if isinstance(k, str)]

    def set_seen_songs(self, keys) -> None:
        cleaned = sorted({
            str(k).strip()[: self.MAX_KEY_LEN]
            for k in list(keys)[: self.MAX_KEYS]
            if str(k).strip()
        })
        with self._lock:
            try:
                data = json.loads(self._path.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
            data["seen_songs"] = cleaned
            self._path.write_text(json.dumps(data, indent=2))


class LibraryWatcher:
    """Keeps a cached copy of the dance library and flags new arrivals.

    LinkCraft can push new songs to Cooper at any time. A background poll
    compares the library against the shared 'seen' list, so every device's
    status polling can surface "new songs" without anyone pressing refresh.
    """

    def __init__(self, node: CooperPanelNode, config: PanelConfig, poll_s: float) -> None:
        self._node = node
        self._config = config
        self._poll_s = poll_s
        self._lock = threading.Lock()
        self._dances: list[dict] = []
        self._new_count = 0

    def refresh(self) -> list[dict]:
        """Fetch the library live and annotate each song with is_new."""
        dances = self._node.list_dances()
        keys = [d["key"] for d in dances]
        seen = self._config.get_seen_songs()
        if seen is None:
            # First ever run: current library is the baseline, nothing is new.
            self._config.set_seen_songs(keys)
            seen = keys
        seen_set = set(seen)
        for d in dances:
            d["is_new"] = d["key"] not in seen_set
        with self._lock:
            self._dances = dances
            self._new_count = sum(1 for d in dances if d["is_new"])
            if self._new_count:
                LOGGER.info("LinkCraft library: %d song(s), %d NEW",
                            len(dances), self._new_count)
        return dances

    def counts(self) -> tuple[int, int]:
        with self._lock:
            return len(self._dances), self._new_count

    def mark_all_seen(self) -> int:
        """Acknowledge every currently known song; clears the NEW flags."""
        with self._lock:
            keys = [d["key"] for d in self._dances]
            for d in self._dances:
                d["is_new"] = False
            self._new_count = 0
        self._config.set_seen_songs(keys)
        LOGGER.info("Marked %d song(s) as seen", len(keys))
        return len(keys)

    def start_polling(self) -> None:
        def loop() -> None:
            time.sleep(5.0)  # give ROS services a moment on startup
            while True:
                try:
                    self.refresh()
                except Exception:
                    LOGGER.debug("Library poll failed", exc_info=True)
                time.sleep(self._poll_s)

        threading.Thread(target=loop, name="library-poll", daemon=True).start()


MAX_MESSAGE_LEN = 500


def run_action(motion: int, area: int) -> dict:
    """Execute one gesture via the standalone x2_action.py program."""
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(ACTION_SCRIPT),
         "--motion", str(int(motion)), "--area", str(int(area))],
        capture_output=True, text=True, timeout=30,
    )
    timing = {"action_ms": round((time.perf_counter() - t0) * 1000, 1)}
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip()
                  or f"exit code {proc.returncode}")
        raise RuntimeError(detail.splitlines()[-1])
    return timing


def resolve_messages(body: dict) -> tuple[str | None, str | None, str | None]:
    """Build the greeting/intro/goodbye text from the panel's message settings.

    - The AM greeting is used before 12:00 (Cooper's clock), PM after; if
      only one is filled in, it is used all day.
    - "{name}" in any message is replaced with the tenant/guest name
      (falls back to "everyone").
    - Empty fields mean the show script's built-in default text is used.
    """
    name = str(body.get("name") or "").strip() or "everyone"

    def clean(field: str) -> str:
        return str(body.get(field) or "").strip()[:MAX_MESSAGE_LEN]

    am, pm = clean("greeting_am"), clean("greeting_pm")
    intro, goodbye = clean("intro"), clean("goodbye")
    greeting = (am if time.localtime().tm_hour < 12 else pm) or am or pm

    sub = lambda text: text.replace("{name}", name) if text else None
    return sub(greeting), sub(intro), sub(goodbye)


def make_handler(node: CooperPanelNode, shows: ShowRunner, pin: str,
                 config: PanelConfig, library: LibraryWatcher,
                 last_activity: list):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CooperPanel/1.0"

        def parse_request(self):
            last_activity[0] = time.time()  # any HTTP request counts as activity
            return super().parse_request()

        def _pin_ok(self) -> bool:
            """Control endpoints require the panel PIN via the X-Pin header."""
            if not pin:
                return True
            supplied = self.headers.get("X-Pin") or ""
            return hmac.compare_digest(supplied, pin)

        # ── plumbing ───────────────────────────────────────────────────────
        def _send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode() or "{}")
            except json.JSONDecodeError:
                return {}

        def log_message(self, fmt, *args):  # route access logs through logging
            LOGGER.debug("%s - %s", self.address_string(), fmt % args)

        def do_OPTIONS(self):  # CORS preflight
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Pin")
            self.end_headers()

        # ── routes ─────────────────────────────────────────────────────────
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                # The webpage is hosted on optimus; Cooper serves the API only.
                return self._send_json({
                    "ok": True,
                    "service": "Cooper control API",
                    "panel": "open the Cooper Control Panel page hosted on optimus",
                })
            if self.path == "/api/status":
                library_size, new_songs = library.counts()
                return self._send_json({
                    "ok": True,
                    "listening": node.listening_state,
                    "speaker": node.speaker_state,
                    "volume": node.volume_state,
                    "show_running": shows.running(),
                    "pin_required": bool(pin),
                    "library_size": library_size,
                    "new_songs": new_songs,
                    "show_timing": shows.show_timing(),
                })
            if self.path == "/api/dances":
                try:
                    return self._send_json({"ok": True, "dances": library.refresh()})
                except Exception as exc:
                    return self._send_json({"ok": False, "error": str(exc)}, 502)
            if self.path == "/api/shortlist":
                return self._send_json({"ok": True,
                                        "shortlist": config.get_shortlist(),
                                        "times": config.get_dance_times(),
                                        "show_dance": config.get_show_dance()})
            if self.path == "/api/actions":
                actions = [
                    {"key": k, "label": a["label"], "emoji": a["emoji"]}
                    for k, a in ACTIONS.items()
                ]
                for i, a in enumerate(config.get_custom_actions(), start=1):
                    actions.append({"key": f"custom{i}", "label": a["label"],
                                    "emoji": "⭐", "custom": True,
                                    "motion": a["motion"], "area": a["area"]})
                return self._send_json({"ok": True, "actions": actions})
            return self._send_json({"ok": False, "error": "not found"}, 404)

        def do_POST(self):
            if not self._pin_ok():
                return self._send_json({"ok": False, "error": "invalid PIN"}, 401)
            body = self._read_json()
            t_start = time.perf_counter()

            def server_ms() -> float:
                return round((time.perf_counter() - t_start) * 1000, 1)

            try:
                if self.path == "/api/dance":
                    # No key in the request = play the configured show dance.
                    key = str(body.get("key") or "") or config.get_show_dance()
                    if not key:
                        return self._send_json(
                            {"ok": False,
                             "error": "no show dance configured — pick one in Settings"}, 400)
                    result = node.start_dance(key)
                    timing = result.pop("timing", {})
                    timing["server_total_ms"] = server_ms()
                    return self._send_json({"ok": True, **result, "timing": timing})

                if self.path == "/api/listening":
                    if "listen" not in body:
                        return self._send_json({"ok": False, "error": "missing 'listen'"}, 400)
                    listen = bool(body["listen"])
                    timing = node.set_listening(listen)
                    timing["server_total_ms"] = server_ms()
                    return self._send_json({"ok": True, "listening": listen, "timing": timing})

                if self.path == "/api/speaker":
                    if "on" not in body:
                        return self._send_json({"ok": False, "error": "missing 'on'"}, 400)
                    on = bool(body["on"])
                    timing = node.set_speaker(on)
                    timing["server_total_ms"] = server_ms()
                    return self._send_json({"ok": True, "speaker": on,
                                            "volume": node.volume_state, "timing": timing})

                if self.path == "/api/volume":
                    if "level" not in body:
                        return self._send_json({"ok": False, "error": "missing 'level'"}, 400)
                    try:
                        level = int(body["level"])
                    except (TypeError, ValueError):
                        return self._send_json({"ok": False, "error": "level must be 0-100"}, 400)
                    timing = node.set_volume(level)
                    timing["server_total_ms"] = server_ms()
                    return self._send_json({"ok": True, "volume": node.volume_state,
                                            "speaker": node.speaker_state, "timing": timing})

                if self.path == "/api/custom_actions":
                    saved = config.set_custom_actions(body.get("actions"))
                    return self._send_json({"ok": True, "actions": saved})

                if self.path == "/api/shortlist":
                    result = {}
                    if "shortlist" in body:
                        result["shortlist"] = config.set_shortlist(body.get("shortlist"))
                    if "times" in body:
                        result["times"] = config.set_dance_times(body.get("times"))
                    if "show_dance" in body:
                        result["show_dance"] = config.set_show_dance(body.get("show_dance"))
                    if not result:
                        return self._send_json(
                            {"ok": False,
                             "error": "missing 'shortlist', 'times' or 'show_dance'"}, 400)
                    return self._send_json({"ok": True, **result})

                if self.path == "/api/songs_seen":
                    return self._send_json({"ok": True, "seen": library.mark_all_seen()})

                if self.path == "/api/action":
                    key = str(body.get("action") or "")
                    action = ACTIONS.get(key)
                    if action is None and key.startswith("custom"):
                        customs = config.get_custom_actions()
                        with suppress(TypeError, ValueError, IndexError):
                            idx = int(key[6:]) - 1
                            if 0 <= idx < len(customs):
                                action = customs[idx]
                    if action is None:
                        return self._send_json({"ok": False, "error": "unknown action"}, 400)
                    if shows.running():
                        return self._send_json(
                            {"ok": False, "error": "a show is running — wait for it to finish"}, 409)
                    # Gestures execute in their own program (x2_action.py).
                    timing = run_action(action["motion"], action["area"])
                    timing["server_total_ms"] = server_ms()
                    return self._send_json({"ok": True, "action": action["label"],
                                            "timing": timing})

                if self.path == "/api/show":
                    greeting, intro, goodbye = resolve_messages(body)
                    dance_key = (str(body.get("dance_key") or "")
                                 or config.get_show_dance() or None)
                    # Per-song play time from the shared config (None = default).
                    dance_duration = None
                    if dance_key:
                        dance_duration = config.get_dance_times().get(dance_key)
                    # Guarantee the show is audible — unless the speaker was
                    # deliberately muted in the panel (silent rehearsal).
                    if node.speaker_state is False:
                        volume = -1  # leave the mute in place
                    else:
                        volume = node._speaker_volume
                    timing = shows.start(
                        dance_key=dance_key,
                        unmute_after=bool(body.get("unmute_after", False)),
                        greeting=greeting,
                        intro=intro,
                        goodbye=goodbye,
                        dance_duration=dance_duration,
                        volume=volume,
                    )
                    if volume >= 0:
                        node.speaker_state = True
                        node.volume_state = volume
                    timing["server_total_ms"] = server_ms()
                    return self._send_json({"ok": True, "show_running": True,
                                            "timing": timing})

                return self._send_json({"ok": False, "error": "not found"}, 404)
            except Exception as exc:
                LOGGER.exception("POST %s failed", self.path)
                return self._send_json({"ok": False, "error": str(exc)}, 502)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Cooper Control Panel server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--mute-service", default=DEFAULT_SET_MUTE_SVC)
    parser.add_argument("--speaker-volume", type=int, default=70,
                        help="volume (1-100) restored when the speaker is "
                             "switched back on after a mute")
    parser.add_argument("--pin", default=os.getenv("COOPER_PANEL_PIN", ""),
                        help="PIN required for all control actions "
                             "(env COOPER_PANEL_PIN; empty = no PIN)")
    parser.add_argument("--library-poll", type=float, default=300.0,
                        help="seconds between background checks of the "
                             "LinkCraft library for new songs")
    parser.add_argument("--idle-exit", type=float, default=0.0,
                        help="exit after N minutes with no HTTP requests "
                             "(0 = run forever). With systemd socket "
                             "activation the next connection restarts the "
                             "server on demand")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    rclpy.init()
    node = CooperPanelNode(mute_service=args.mute_service,
                           speaker_volume=args.speaker_volume)
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, name="ros-spin", daemon=True)
    spin_thread.start()

    shows = ShowRunner(node)
    config = PanelConfig(CONFIG_FILE)
    library = LibraryWatcher(node, config, args.library_poll)
    library.start_polling()

    last_activity = [time.time()]
    handler = make_handler(node, shows, args.pin, config, library, last_activity)

    # systemd socket activation: inherit the already-listening socket (fd 3)
    # so the server only runs while someone is actually using the panel.
    if int(os.environ.get("LISTEN_FDS", "0")) >= 1:
        server = ThreadingHTTPServer(
            (args.bind, args.port), handler, bind_and_activate=False
        )
        server.socket = socket.socket(fileno=3)
        LOGGER.info("Started on demand via systemd socket activation")
    else:
        server = ThreadingHTTPServer((args.bind, args.port), handler)

    if args.idle_exit > 0:
        def idle_watch() -> None:
            while True:
                time.sleep(5.0)
                idle_s = time.time() - last_activity[0]
                if idle_s > args.idle_exit * 60 and not shows.running():
                    LOGGER.info("No requests for %.0f min — exiting "
                                "(next connection starts the server again)",
                                args.idle_exit)
                    server.shutdown()
                    return
        threading.Thread(target=idle_watch, name="idle-watch", daemon=True).start()

    LOGGER.info("Cooper Control Panel at http://%s:%d/ (PIN %s)",
                args.bind, args.port, "enabled" if args.pin else "DISABLED")
    if not args.pin:
        LOGGER.warning("No PIN set — anyone on the network can control Cooper. "
                       "Start with --pin <code> or COOPER_PANEL_PIN.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        executor.shutdown()
        with suppress(Exception):
            node.destroy_node()
        with suppress(Exception):
            rclpy.shutdown()


if __name__ == "__main__":
    main()
