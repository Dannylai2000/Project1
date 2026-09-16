"""Cooper control API — HTTP bridge to the AgiBot X2 (ROS2 / AimDK).

Runs ON the robot and exposes the REST API the Cooper Control Panel
webpage (hosted on optimus) talks to:

    GET  /api/status        → server + mic/speaker/show state
    GET  /api/dances        → LinkCraft dance resources (live from the robot)
    GET  /api/actions       → one-tap gestures
    GET  /api/shortlist     → shared shortlist + per-dance play times
    GET  /api/messages      → this robot's message groups (shared by devices)
    POST /api/dance         → {"key": "..."} start that dance now
    POST /api/listening     → {"listen": true|false} mic on / off
    POST /api/speaker       → {"on": true|false} speaker on / muted
    POST /api/action        → {"action": "..."} run a gesture
    POST /api/shortlist     → save shortlist / play times
    POST /api/messages      → save the message groups on this robot
    POST /api/songs_seen    → acknowledge new songs
    POST /api/show          → run the full showroom sequence
    POST /api/restart       → restart the API (watchdog/socket revives it)
    POST /api/update        → git pull on the robot, then restart

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

# Dynamic service loading (types vary by SDK build).
try:
    from rosidl_runtime_py.utilities import get_service
except ImportError:  # pragma: no cover
    get_service = None


LOGGER = logging.getLogger("cooper_panel")

# Bumped on every change, in lockstep with PANEL_VERSION in
# cooper_control_panel.html. The panel shows both and flags a mismatch,
# so a half-deployed update is visible at a glance.
SERVER_VERSION = "2026.09.16-4"

# For the health report's uptime figure.
SERVER_STARTED = time.time()

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

# Personalized message groups — a dedicated file PER ROBOT, so every panel
# device reads and edits the same texts (browser storage used to drift).
MESSAGES_FILE   = Path(__file__).resolve().parent / "cooper_messages.json"

# Milestones written by the running show script (first speech, dance start),
# read back for the panel's performance diagnostics.
TIMING_FILE     = Path(__file__).resolve().parent / "cooper_show_timing.json"


# Field names that plausibly carry a human-readable LinkCraft song name,
# tried in order on the resource itself, its nested sub-messages, and any
# JSON payloads found in string fields.
NAME_FIELD_HINTS = ("resource_name", "display_name", "song_name", "name",
                    "title", "alias", "label", "nick_name", "nickname",
                    "description", "remark")


def _message_fields(msg) -> list[str]:
    """Field names of a ROS message ([] for plain values)."""
    with suppress(Exception):
        return list(msg.get_fields_and_field_types().keys())
    return [s.lstrip("_") for s in getattr(msg, "__slots__", [])]


def _looks_like_name(value, key: str) -> bool:
    """True when a string reads like a human title, not another machine ID."""
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v or v == key:
        return False
    if v.lower().startswith(("linkcraft_", "resource_", "urn:", "uuid")):
        return False
    # 20+ chars of unbroken letters/digits/underscores = another machine ID.
    if len(v) >= 20 and " " not in v and all(c.isalnum() or c == "_" for c in v):
        return False
    return True


def _name_from_json(text: str, key: str) -> str:
    """Dig a name/title out of a JSON payload carried in a string field."""
    if not text or text[0] not in "{[":
        return ""
    with suppress(Exception):
        queue = [json.loads(text)]
        while queue:
            item = queue.pop(0)
            if isinstance(item, dict):
                for hint in NAME_FIELD_HINTS:
                    v = item.get(hint)
                    if _looks_like_name(v, key):
                        return v.strip()
                queue.extend(item.values())
            elif isinstance(item, list):
                queue.extend(item)
    return ""


def resource_display_name(resource, key: str) -> str:
    """Best-effort human-readable name for a LinkCraft resource.

    Checks likely name fields on the resource itself, then one level of
    nested sub-messages, then inside any JSON payloads (e.g. a meta field).
    Returns "" when nothing readable is found (caller falls back to the key).
    """
    holders = [resource]
    for field in _message_fields(resource):
        value = getattr(resource, field, None)
        if value is not None and _message_fields(value):
            holders.append(value)
    for holder in holders:
        for hint in NAME_FIELD_HINTS:
            v = getattr(holder, hint, None)
            if _looks_like_name(v, key):
                return v.strip()
    for holder in holders:
        for field in _message_fields(holder):
            v = getattr(holder, field, None)
            if isinstance(v, str):
                found = _name_from_json(v.strip(), key)
                if found:
                    return found
    return ""


def describe_message_fields(msg, depth: int = 1) -> dict:
    """{field: value preview} for a ROS message, `depth` nesting levels."""
    out = {}
    for field in _message_fields(msg):
        v = getattr(msg, field, None)
        if _message_fields(v) and depth > 0:
            out[field] = describe_message_fields(v, depth - 1)
        elif isinstance(v, str):
            out[field] = v[:120]
        elif isinstance(v, (bool, int, float)):
            out[field] = v
        elif isinstance(v, (list, tuple)):
            preview = []
            for item in list(v)[:2]:
                if _message_fields(item) and depth > 0:
                    preview.append(describe_message_fields(item, depth - 1))
                elif isinstance(item, str):
                    preview.append(item[:80])
                elif isinstance(item, (bool, int, float)):
                    preview.append(item)
                else:
                    preview.append(type(item).__name__)
            if len(v) > 2:
                preview.append(f"… +{len(v) - 2} more")
            out[field] = preview
        else:
            out[field] = type(v).__name__
    return out


def _duration_from_value(fname: str, v) -> float | None:
    """Seconds when a field name/value plausibly carries a song duration."""
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
        return None
    low = fname.lower()
    if "stamp" in low or "date" in low or "time_out" in low:
        return None
    if not ("duration" in low or "play_time" in low or "playtime" in low
            or low in ("length", "seconds", "secs", "time_length")):
        return None
    val = float(v)
    if "ms" in low or "milli" in low or val > 1000:
        val /= 1000.0
    return val if 3.0 <= val <= 3600.0 else None


AUDIO_EXTS = (".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac")


def _wav_duration(path: Path) -> float | None:
    import wave
    with suppress(Exception):
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            if rate > 0:
                return w.getnframes() / rate
    return None


def _mp3_duration(path: Path) -> float | None:
    """CBR estimate from the first MPEG frame header — close enough for
    the show to wait out the whole song."""
    with suppress(Exception):
        data = path.read_bytes()
        size = len(data)
        i = 0
        if data[:3] == b"ID3":                     # skip the ID3v2 tag
            i = 10 + (((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14)
                      | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F))
        while i < size - 4:
            if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
                ver   = (data[i + 1] >> 3) & 0x03  # 3=MPEG1, 2=MPEG2
                layer = (data[i + 1] >> 1) & 0x03  # 1=Layer III
                bidx  = (data[i + 2] >> 4) & 0x0F
                if layer == 1 and 0 < bidx < 15:
                    kbps = ((0, 32, 40, 48, 56, 64, 80, 96, 112, 128,
                             160, 192, 224, 256, 320) if ver == 3 else
                            (0, 8, 16, 24, 32, 40, 48, 56, 64, 80,
                             96, 112, 128, 144, 160))[bidx]
                    if kbps:
                        return (size - i) * 8.0 / (kbps * 1000.0)
            i += 1
    return None


def audio_file_duration(path: Path) -> float | None:
    ext = path.suffix.lower()
    if ext == ".wav":
        return _wav_duration(path)
    if ext == ".mp3":
        return _mp3_duration(path)
    return None


def files_duration_s(file_paths) -> float | None:
    """Song duration measured from the resource's audio files ON DISK.

    LinkCraft's metadata carries no duration, but its `files` entries point
    into /agibot/.../resources/ on the robot — the audio lives there and the
    API runs on the same machine, so read it directly.
    """
    candidates: list[Path] = []
    for raw in list(file_paths or [])[:5]:
        p = Path(str(raw))
        with suppress(OSError):
            if p.is_dir():
                for child in sorted(p.rglob("*"))[:200]:
                    if child.suffix.lower() in AUDIO_EXTS and child.is_file():
                        candidates.append(child)
            elif p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                candidates.append(p)
    for c in candidates:
        d = audio_file_duration(c)
        if d is not None and 3.0 <= d <= 3600.0:
            LOGGER.info("Song duration from %s: %.1fs", c.name, d)
            return round(d, 1)
    return None


def resource_duration_s(resource) -> float | None:
    """Best-effort song duration (seconds) from a LinkCraft resource.

    Searched on the resource, nested sub-messages, entries of list fields
    (e.g. current_version.files), and JSON payloads in string fields.
    None when this SDK build simply doesn't report one.
    """
    holders = [resource]
    for field in _message_fields(resource):
        v = getattr(resource, field, None)
        if v is None:
            continue
        if _message_fields(v):
            holders.append(v)
            for f2 in _message_fields(v):
                v2 = getattr(v, f2, None)
                if isinstance(v2, (list, tuple)):
                    holders += [x for x in list(v2)[:10] if _message_fields(x)]
        elif isinstance(v, (list, tuple)):
            holders += [x for x in list(v)[:10] if _message_fields(x)]
    for holder in holders:
        for field in _message_fields(holder):
            d = _duration_from_value(field, getattr(holder, field, None))
            if d is not None:
                return round(d, 1)
    for holder in holders:
        for field in _message_fields(holder):
            v = getattr(holder, field, None)
            if isinstance(v, str) and v[:1] in "{[":
                with suppress(Exception):
                    queue = [json.loads(v)]
                    while queue:
                        item = queue.pop(0)
                        if isinstance(item, dict):
                            for k, vv in item.items():
                                d = _duration_from_value(k, vv)
                                if d is not None:
                                    return round(d, 1)
                            queue.extend(item.values())
                        elif isinstance(item, list):
                            queue.extend(item)
    return None


class CooperPanelNode(Node):
    """ROS2 side of the panel: talks to the AimDK services."""

    def __init__(self, mute_service: str, speaker_volume: int = 70,
                 mic_source_service: str = "", mic_internal: int = 1,
                 mic_external: int = 2) -> None:
        super().__init__("cooper_panel")
        self._speaker_volume = max(1, min(100, int(speaker_volume)))
        self._mic_source_service = mic_source_service
        self._mic_internal = int(mic_internal)
        self._mic_external = int(mic_external)
        # True after a manual "Gear up for performance" (external mic active).
        self.mic_geared = False
        # Last VERIFIED mic source id (GetMicSourceRequest); None = unknown.
        self.mic_source_state: int | None = None
        # This state dies with the process, and the API restarts routinely
        # (idle-exit, logout, panel-driven update) — recover the truth from
        # the robot itself so the panel's MIC mode stays honest.
        threading.Thread(target=self._recover_mic_state,
                         name="mic-state-recover", daemon=True).start()
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
        # One-time journal dump of a LinkCraft resource's fields (diagnosis).
        self._resource_fields_logged = False
        self._audio_probe_logged = False
        # (key, version) -> measured song duration in seconds (or None).
        self._duration_cache: dict = {}

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
                if not self._resource_fields_logged:
                    # One-time field dump (two levels deep, list previews) so
                    # the journal shows where this SDK build keeps the song
                    # name and — if reported at all — the song duration.
                    self._resource_fields_logged = True
                    LOGGER.info("LinkCraft resource fields for %s: %s",
                                key, describe_message_fields(r, depth=2))
                # On this SDK build the LinkCraft song name (e.g. 太极) is
                # current_version.name — confirmed from the journal dump.
                # Fall back to the generic search on builds shaped otherwise.
                name = ""
                with suppress(Exception):
                    name = str(r.current_version.name).strip()
                if not name or name == key:
                    name = resource_display_name(r, key)
                version = ""
                with suppress(Exception):
                    version = str(r.current_version.version)
                dur = resource_duration_s(r)
                if dur is None:
                    # Metadata carries no duration on this SDK — measure the
                    # audio file the resource's `files` entries point at.
                    ck = (key, version)
                    if ck in self._duration_cache:
                        dur = self._duration_cache[ck]
                    else:
                        paths = []
                        with suppress(Exception):
                            paths = [str(f) for f in r.current_version.files]
                        dur = files_duration_s(paths)
                        if dur is None and paths and not self._audio_probe_logged:
                            # One-time look inside the resource dir so the
                            # journal shows what is actually there.
                            self._audio_probe_logged = True
                            listing: list = []
                            with suppress(Exception):
                                p = Path(paths[0])
                                if p.is_dir():
                                    listing = [c.name for c in
                                               sorted(p.iterdir())[:20]]
                                else:
                                    listing = [paths[0] + (" (file)" if p.exists()
                                                           else " (missing)")]
                            LOGGER.info("Audio probe found no duration for %s;"
                                        " files=%s contents=%s",
                                        key, paths[:3], listing)
                        self._duration_cache[ck] = dur
                dances.append({"key": key, "name": name or key,
                               "version": version, "duration": dur})
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

    def _resolve_service(self, name: str):
        """(srv_type, ready client) for a service in the graph, or None."""
        if not name or get_service is None:
            return None
        srv_type = None
        for svc, types in self.get_service_names_and_types():
            if svc == name and types:
                with suppress(Exception):
                    srv_type = get_service(types[0])
                break
        if srv_type is None:
            return None
        client = self.create_client(srv_type, name, callback_group=self._cbg)
        if not client.wait_for_service(timeout_sec=2.0):
            return None
        return srv_type, client

    def _call_service(self, client, req):
        """Call with retries; the response, or None on timeout."""
        for _ in range(8):
            future = client.call_async(req)
            done = threading.Event()
            future.add_done_callback(lambda _: done.set())
            if done.wait(0.5) and future.done():
                return future.result()
        return None

    @staticmethod
    def _apply_source_field(holder, value: int) -> str | None:
        """Set the mic-source id on a message; the field name used, or None."""
        fields = {}
        with suppress(Exception):
            fields = dict(holder.get_fields_and_field_types())
        for cand in ("audio_stream_id", "mic_source", "source", "stream_id",
                     "audio_source", "channel", "type", "value", "id"):
            if cand in fields:
                with suppress(Exception):
                    setattr(holder, cand, int(value))
                    return cand
        for fname, ftype in fields.items():
            if any(k in fname.lower() for k in ("source", "stream", "mic")) \
                    and "int" in ftype:
                with suppress(Exception):
                    setattr(holder, fname, int(value))
                    return fname
        return None

    @staticmethod
    def _read_source_field(msg) -> int | None:
        """Dig the current mic-source id out of a response (nested too)."""
        holders = [msg]
        with suppress(Exception):
            for fname in msg.get_fields_and_field_types():
                sub = getattr(msg, fname, None)
                if hasattr(sub, "get_fields_and_field_types"):
                    holders.append(sub)
        for holder in holders:
            fields = {}
            with suppress(Exception):
                fields = dict(holder.get_fields_and_field_types())
            for cand in ("audio_stream_id", "mic_source", "source",
                         "stream_id", "audio_source"):
                if cand in fields:
                    v = getattr(holder, cand, None)
                    if isinstance(v, int) and not isinstance(v, bool):
                        return v
            for fname, ftype in fields.items():
                if any(k in fname.lower() for k in ("source", "stream", "mic")) \
                        and "int" in ftype:
                    v = getattr(holder, fname, None)
                    if isinstance(v, int) and not isinstance(v, bool):
                        return v
        return None

    def _recover_mic_state(self) -> None:
        """At startup, read which mic is REALLY active and adopt it.

        A restart used to reset mic_geared to False while the robot was
        still on the external mic — the MIC-mode radio then showed
        "normal", and selecting Normal was a no-op (already selected),
        leaving no way back to the in-built mic from the panel.
        """
        time.sleep(3.0)  # let DDS discovery populate the service graph
        with suppress(Exception):
            source = self._get_mic_source()
            if source is not None:
                self.mic_geared = (source == self._mic_external)
                LOGGER.info("Mic state recovered at startup: source=%d "
                            "(%s), geared=%s", source,
                            "external" if self.mic_geared else "in-built",
                            self.mic_geared)

    def _get_mic_source(self) -> int | None:
        """Ask the robot which mic is REALLY active (GetMicSourceRequest)."""
        name = ""
        if "SetMicSource" in self._mic_source_service:
            name = self._mic_source_service.replace("SetMicSource", "GetMicSource")
        resolved = self._resolve_service(name)
        if resolved is None:
            return None
        srv_type, client = resolved
        req = srv_type.Request()
        self._stamp(req)
        response = self._call_service(client, req)
        if response is None:
            return None
        source = self._read_source_field(response)
        if source is not None:
            self.mic_source_state = source
        return source

    def _call_mic_source(self, target: int) -> bool:
        """Switch Cooper's mic stream to `target` (1 = built-in, 2 = external)
        and VERIFY the robot really changed.

        The native app showed the switch not applying even though the service
        answered, so success now means: response received, no error flagged in
        it, and (when GetMicSourceRequest is available) a read-back confirming
        the new source. One automatic retry on a failed verification.
        """
        resolved = self._resolve_service(self._mic_source_service)
        if resolved is None:
            LOGGER.warning("Mic-source service %s not found / not responding",
                           self._mic_source_service)
            return False
        srv_type, client = resolved

        for attempt in (1, 2):
            req = srv_type.Request()
            self._stamp(req)
            applied = self._apply_source_field(req, target)
            if applied is None:
                # The id may live one level down (nested sub-message).
                with suppress(Exception):
                    for fname in req.get_fields_and_field_types():
                        sub = getattr(req, fname, None)
                        if hasattr(sub, "get_fields_and_field_types"):
                            inner = self._apply_source_field(sub, target)
                            if inner:
                                applied = f"{fname}.{inner}"
                                break
            if applied is None:
                LOGGER.warning("No usable source field on %s request",
                               self._mic_source_service)
                return False

            response = self._call_service(client, req)
            if response is None:
                LOGGER.warning("Mic-source switch timed out (attempt %d)", attempt)
                continue
            LOGGER.info("Mic-source switch attempt %d: %s=%d, response=%r",
                        attempt, applied, int(target), response)

            # An error flagged in the response = not applied.
            flagged = False
            with suppress(Exception):
                for fname, ftype in response.get_fields_and_field_types().items():
                    v = getattr(response, fname, None)
                    if fname == "success" and v is False:
                        flagged = True
                    if "err" in fname.lower() and isinstance(v, int) \
                            and not isinstance(v, bool) and v != 0:
                        flagged = True
            if flagged:
                LOGGER.warning("Mic-source switch rejected by the robot: %r",
                               response)
                continue

            time.sleep(0.5)  # let the switch settle before reading back
            actual = self._get_mic_source()
            if actual is None:
                # No Get service to verify with — trust the response.
                self.mic_source_state = int(target)
                return True
            if actual == int(target):
                LOGGER.info("Mic source verified: %d", actual)
                return True
            LOGGER.warning("Mic source did NOT change: wanted %d, robot "
                           "reports %d (attempt %d)", int(target), actual,
                           attempt)
        return False

    def _normalize_mic_source(self) -> None:
        """Silently switch back to the built-in mic before enabling listening.

        A show or a gear-up leaves the external (idle) mic selected; unmuting
        on it would leave the assistant deaf. The switch is done at volume 0
        so the robot's own switch announcement is not heard. Best-effort —
        any failure is logged and the unmute proceeds regardless.
        """
        if not self._mic_source_service or get_service is None:
            return
        try:
            with suppress(Exception):
                self._send_volume(0)  # silence the switch announcement
            if self._call_mic_source(self._mic_internal):
                self.mic_geared = False
        except Exception:
            LOGGER.exception("Mic normalization failed")
        finally:
            with suppress(Exception):
                self._send_volume(self._speaker_volume)
                self.volume_state = self._speaker_volume
                self.speaker_state = True

    def gear_up(self, external: bool, settle_s: float = 5.0) -> dict:
        """Manual performance prep (the panel's Gear-up radio).

        Gear up (external=True): volume 0 → switch to the external mic →
        wait `settle_s` → volume back to the show level. Cooper is then
        ready to perform with sound and effectively deaf to the audience.
        Normal (external=False): the same sequence back to the built-in mic.
        """
        if not self._mic_source_service or get_service is None:
            raise RuntimeError("mic-source service not configured on this build")
        timing: dict = {}
        t0 = time.perf_counter()
        with suppress(Exception):
            self._send_volume(0)  # silence the switch announcement
        target = self._mic_external if external else self._mic_internal
        try:
            if not self._call_mic_source(target):
                actual = self.mic_source_state
                detail = ""
                if actual is not None and actual != target:
                    detail = (" — the robot still reports the "
                              + ("external" if actual == self._mic_external
                                 else "in-built") + " mic")
                raise RuntimeError("mic-source switch did not apply"
                                   + detail + " (details in Cooper's journal)")
            time.sleep(max(0.0, settle_s))
        finally:
            with suppress(Exception):
                self._send_volume(self._speaker_volume)
                self.volume_state = self._speaker_volume
                self.speaker_state = True
        self.mic_geared = bool(external)
        timing["gear_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return timing

    def set_listening(self, listen: bool) -> dict:
        """Unmute (listen=True) or mute (listen=False) Cooper's microphones.

        Returns a timing breakdown for the performance diagnostics.
        """
        if listen:
            # Make sure the built-in mic is active before listening resumes.
            self._normalize_mic_source()
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

    def __init__(self, node: CooperPanelNode, extra_args: list[str] | None = None) -> None:
        self._node = node
        self._extra_args = list(extra_args or [])
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
        thank_you: str | None = None,
        goodbye: str | None = None,
        dance_duration: float | None = None,
        volume: int | None = None,
        geared: bool = False,
        skip_dance: bool = False,
    ) -> dict:
        t0 = time.perf_counter()
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise RuntimeError("a show is already running")
            cmd = [sys.executable, str(SHOW_SCRIPT)]
            if skip_dance:
                cmd += ["--no-dance"]
            if dance_key:
                cmd += ["--dance-key", dance_key]
            if unmute_after:
                cmd += ["--unmute-after"]
            if greeting:
                cmd += ["--greeting-text", greeting]
            if intro:
                cmd += ["--intro-text", intro]
            if thank_you:
                cmd += ["--thank-you-text", thank_you]
            if goodbye:
                cmd += ["--goodbye-text", goodbye]
            if dance_duration is not None:
                cmd += ["--dance-duration", str(dance_duration)]
            if volume is not None:
                cmd += ["--volume", str(int(volume))]
            cmd += self._extra_args
            if geared:
                # Cooper was geared up manually: already on the external mic
                # with the speaker up. The show skips its own mute/mic-switch
                # sequence entirely (the later flags override _extra_args).
                cmd += ["--no-mute", "--mic-source-service", ""]
            cmd += ["--timing-file", str(TIMING_FILE)]
            with suppress(OSError):
                TIMING_FILE.unlink()
            self._last_requested = time.time()
            LOGGER.info("Starting show: %s", " ".join(cmd))
            self._proc = subprocess.Popen(cmd)
            proc = self._proc

        # The show's first step mutes the microphones — unless Cooper was
        # geared up manually, in which case the show leaves the mic alone.
        if not geared:
            self._node.listening_state = False

        def watch() -> None:
            proc.wait()
            if unmute_after and not geared:
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

    # 999 in a seconds box is the "play the full song" sentinel.
    MAX_DANCE_TIME_S = 999.0

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

    def duration_for(self, key: str) -> float | None:
        """Cached song duration in seconds, when LinkCraft reports one."""
        with self._lock:
            for d in self._dances:
                if d["key"] == key:
                    return d.get("duration")
        return None

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

# Field keys of one message group (panel and store share this shape).
MSG_FIELD_KEYS = ("guestName", "greetAM", "greetPM", "introMsg",
                  "thankYouMsg", "goodbyeMsg")


class MessagesStore:
    """Per-robot store for the personalized message groups.

    One JSON file next to the server: {"active": name, "groups": {name:
    {guestName, greetAM, greetPM, introMsg, thankYouMsg, goodbyeMsg}}}.
    Empty fields mean "speak the show script's built-in text".
    """

    MAX_GROUPS = 20
    MAX_NAME = 50

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _clean(self, active, groups) -> dict:
        clean: dict = {}
        for name, g in list((groups or {}).items())[: self.MAX_GROUPS]:
            if not isinstance(g, dict):
                continue
            nm = str(name).strip()[: self.MAX_NAME]
            if not nm:
                continue
            clean[nm] = {k: str(g.get(k) or "")[:MAX_MESSAGE_LEN]
                         for k in MSG_FIELD_KEYS}
        act = str(active or "").strip()[: self.MAX_NAME]
        if act not in clean and clean:
            act = next(iter(clean))
        return {"active": act if clean else "", "groups": clean}

    def get(self) -> dict:
        with self._lock:
            try:
                data = json.loads(self._path.read_text())
            except (OSError, json.JSONDecodeError):
                return {"active": "", "groups": {}}
        return self._clean(data.get("active"), data.get("groups"))

    def set(self, active, groups) -> dict:
        cleaned = self._clean(active, groups)
        with self._lock:
            self._path.write_text(
                json.dumps(cleaned, ensure_ascii=False, indent=2))
        return cleaned


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


def resolve_messages(body: dict, messages: MessagesStore):
    """(greeting, intro, thank_you, goodbye) texts for the show.

    Explicit texts in the request body win (older panel pages send them);
    otherwise the robot's stored ACTIVE message group is used, so every
    device launches the show with the same texts.

    - The AM greeting is used before 12:00 (Cooper's clock), PM after; if
      only one is filled in, it is used all day.
    - "{name}" in any message is replaced with the tenant/guest name
      (falls back to "everyone").
    - Empty everywhere = None = the show script's built-in text.
    """
    stored = messages.get()
    group = (stored["groups"] or {}).get(stored["active"], {})

    def pick(body_key: str, group_key: str) -> str:
        v = str(body.get(body_key) or "").strip()
        if not v:
            v = str(group.get(group_key) or "").strip()
        return v[:MAX_MESSAGE_LEN]

    name = pick("name", "guestName") or "everyone"
    am, pm = pick("greeting_am", "greetAM"), pick("greeting_pm", "greetPM")
    intro = pick("intro", "introMsg")
    thanks = pick("thank_you", "thankYouMsg")
    goodbye = pick("goodbye", "goodbyeMsg")
    greeting = (am if time.localtime().tm_hour < 12 else pm) or am or pm

    sub = lambda text: text.replace("{name}", name) if text else None
    return sub(greeting), sub(intro), sub(thanks), sub(goodbye)


def health_report(node: CooperPanelNode, config: PanelConfig, pin: str) -> list[dict]:
    """Self-diagnosis behind GET /api/health (the panel's 🩺 Diagnose button).

    Each check: {id, label, ok, detail, fix} with ok True (pass),
    False (problem) or None (warning / informational).
    """
    checks: list[dict] = []

    def add(cid: str, label: str, ok, detail: str = "", fix: str = "") -> None:
        checks.append({"id": cid, "label": label, "ok": ok,
                       "detail": detail, "fix": fix})

    on_demand = int(os.environ.get("LISTEN_FDS", "0")) >= 1
    up_min = int((time.time() - SERVER_STARTED) // 60)
    add("api", "Control API process", True,
        f"v{SERVER_VERSION} · up {up_min} min · " +
        ("started on demand (systemd socket)" if on_demand
         else "running standalone (manual start or cron)"))

    # Will the API arm itself again after Cooper reboots?
    user = ""
    with suppress(Exception):
        import getpass
        user = getpass.getuser()
    linger = bool(user) and (Path("/var/lib/systemd/linger") / user).exists()
    has_cron = False
    with suppress(Exception):
        out = subprocess.run(["crontab", "-l"], capture_output=True,
                             text=True, timeout=5)
        has_cron = "run_cooper_panel.sh" in (out.stdout or "")
    if linger:
        add("boot", "Start after reboot", True,
            "lingering is enabled — the API socket arms at every boot")
    elif has_cron:
        add("boot", "Start after reboot", True,
            "cron @reboot entry found — the API starts at every boot")
    else:
        add("boot", "Start after reboot", None,
            f"the API only arms when {user or 'the robot user'} logs in on "
            "Cooper — after a reboot the panel stays offline until then "
            "(fine if Cooper auto-logs-in at boot)",
            "one-time admin command on Cooper: "
            f"sudo loginctl enable-linger {user or '<user>'} — or, with no "
            "admin available, re-run the installer with --cron (cron needs "
            "no login and no admin)")

    # AimDK ROS services the panel depends on.
    for cid, label, client in (
            ("svc_mute",    "Microphone service (SetMute)",         node._set_mute),
            ("svc_volume",  "Speaker service (SetVolume)",          node._set_volume),
            ("svc_library", "Dance library (GetRobotResources)",    node._get_resources),
            ("svc_dance",   "Dance runner (ExecuteActionResource)", node._exec_action)):
        if client is None:
            add(cid, label, False, "service type missing in this build",
                "rebuild the aimdk workspace (colcon build) and restart the API")
            continue
        ready = False
        with suppress(Exception):
            ready = bool(client.service_is_ready())
        add(cid, label, ready, "responding" if ready else "not available right now",
            "" if ready else "wait for the robot software to finish booting; "
            "if it stays red, rebuild the aimdk workspace (colcon build) and "
            "restart the API")

    # Mic-source switch used by the show-audio workaround.
    svc = node._mic_source_service
    if svc:
        present = False
        with suppress(Exception):
            present = any(n == svc
                          for n, _ in self_service_names(node))
        add("svc_mic_source", "Mic-source switch (show audio)",
            True if present else None,
            "responding" if present else f"{svc} not found",
            "" if present else "the show falls back to plain mute — the "
            "performance may have no sound")

    # Files the panel needs on disk.
    missing = [p.name for p in (SHOW_SCRIPT, ACTION_SCRIPT) if not p.exists()]
    add("files", "Show & gesture programs", not missing,
        "x2_showroom_demo.py and x2_action.py present" if not missing
        else "missing: " + ", ".join(missing),
        "" if not missing else "run git pull in ~/cooper on Cooper")

    writable = os.access(CONFIG_FILE.parent, os.W_OK)
    add("config", "Shared settings storage", True if writable else None,
        "writable" if writable
        else f"{CONFIG_FILE.parent} is not writable — shortlist and "
             "show-dance changes will not save")

    sd = config.get_show_dance()
    add("show_dance", "Show dance selected", True if sd else None,
        sd if sd else "no dance chosen yet",
        "" if sd else "pick one in ⚙ Settings → song list")

    add("pin", "PIN protection", True if pin else None,
        "enabled" if pin else "DISABLED — anyone on the network can control Cooper",
        "" if pin else "reinstall the service with --pin <code>")

    return checks


def self_service_names(node):
    """The ROS service graph as (name, types) pairs; [] on any failure."""
    with suppress(Exception):
        return node.get_service_names_and_types()
    return []


def make_handler(node: CooperPanelNode, shows: ShowRunner, pin: str,
                 config: PanelConfig, library: LibraryWatcher,
                 messages: MessagesStore, last_activity: list):
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
                    "version": SERVER_VERSION,
                    "listening": node.listening_state,
                    "speaker": node.speaker_state,
                    "volume": node.volume_state,
                    "mic_geared": node.mic_geared,
                    "mic_source": node.mic_source_state,
                    "show_running": shows.running(),
                    "pin_required": bool(pin),
                    "library_size": library_size,
                    "new_songs": new_songs,
                    "show_timing": shows.show_timing(),
                })
            if self.path == "/api/health":
                try:
                    return self._send_json({"ok": True, "version": SERVER_VERSION,
                                            "checks": health_report(node, config, pin)})
                except Exception as exc:
                    LOGGER.exception("health check failed")
                    return self._send_json({"ok": False, "error": str(exc)}, 502)
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
            if self.path == "/api/messages":
                return self._send_json({"ok": True, **messages.get()})
            if self.path == "/api/actions":
                actions = [
                    {"key": k, "label": a["label"], "emoji": a["emoji"]}
                    for k, a in ACTIONS.items()
                ]
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

                if self.path == "/api/restart":
                    # Exit the process: the systemd socket / cron wrapper /
                    # watchdog brings the API back within seconds, running
                    # whatever code is on disk. No SSH needed.
                    if shows.running():
                        return self._send_json(
                            {"ok": False,
                             "error": "a show is running — wait for it to finish"}, 409)
                    LOGGER.info("Restart requested from the panel")
                    threading.Timer(0.6, lambda: os._exit(0)).start()
                    return self._send_json({"ok": True, "restarting": True})

                if self.path == "/api/update":
                    # git pull on the robot, then restart — the panel's
                    # "Update & restart" button; replaces the manual SSH.
                    if shows.running():
                        return self._send_json(
                            {"ok": False,
                             "error": "a show is running — wait for it to finish"}, 409)
                    repo = Path(__file__).resolve().parent
                    try:
                        proc = subprocess.run(
                            ["git", "-C", str(repo), "pull", "--ff-only"],
                            capture_output=True, text=True, timeout=90)
                    except subprocess.TimeoutExpired:
                        return self._send_json(
                            {"ok": False, "error": "git pull timed out — is "
                             "the robot's internet connection up?"}, 502)
                    out = (proc.stdout + "\n" + proc.stderr).strip()
                    LOGGER.info("Panel-triggered update: rc=%s output=%s",
                                proc.returncode, out[-400:])
                    if proc.returncode != 0:
                        return self._send_json(
                            {"ok": False,
                             "error": "git pull failed: " + out[-400:]}, 502)
                    updated = "Already up to date" not in out
                    if updated:
                        threading.Timer(0.8, lambda: os._exit(0)).start()
                    return self._send_json({"ok": True, "updated": updated,
                                            "restarting": updated,
                                            "output": out[-800:]})

                if self.path == "/api/gear_up":
                    if shows.running():
                        return self._send_json(
                            {"ok": False,
                             "error": "a show is running — wait for it to finish"}, 409)
                    external = bool(body.get("external", True))
                    timing = node.gear_up(external)
                    timing["server_total_ms"] = server_ms()
                    return self._send_json({"ok": True, "mic_geared": node.mic_geared,
                                            "volume": node.volume_state,
                                            "timing": timing})

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

                if self.path == "/api/messages":
                    if "groups" not in body:
                        return self._send_json(
                            {"ok": False, "error": "missing 'groups'"}, 400)
                    saved = messages.set(body.get("active"), body.get("groups"))
                    return self._send_json({"ok": True, **saved})

                if self.path == "/api/songs_seen":
                    return self._send_json({"ok": True, "seen": library.mark_all_seen()})

                if self.path == "/api/action":
                    key = str(body.get("action") or "")
                    action = ACTIONS.get(key)
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
                    greeting, intro, thank_you, goodbye = \
                        resolve_messages(body, messages)
                    skip_dance = bool(body.get("skip_dance", False))
                    dance_key = (str(body.get("dance_key") or "")
                                 or config.get_show_dance() or None)
                    if skip_dance:
                        dance_key = None       # talk-only show: no dance needed
                    elif not dance_key:
                        # Never fall back to the show script's built-in dance
                        # key: LinkCraft IDs differ per robot, so a key baked
                        # in for one robot fails on another.
                        return self._send_json(
                            {"ok": False,
                             "error": "no show dance configured on this robot "
                                      "— pick one in ⚙ Settings (each robot "
                                      "has its own LinkCraft IDs)"}, 400)
                    dance_duration = None
                    if dance_key:
                        # Same trap, second form: a dance key copied over
                        # from another robot. Verify it against THIS robot's
                        # library before launching, so the show fails loudly
                        # here rather than silently skipping the dance.
                        library_keys = None
                        with suppress(Exception):
                            library_keys = {d["key"] for d in library.refresh()}
                        if library_keys is not None and dance_key not in library_keys:
                            return self._send_json(
                                {"ok": False,
                                 "error": f"show dance {dance_key[-16:]}… is not "
                                          "in this robot's LinkCraft library — it "
                                          "belongs to the other robot. Pick this "
                                          "robot's own song in ⚙ Settings"}, 400)
                        # Per-song play time: 999 = "play the full song"
                        # sentinel; a number = that many seconds; blank = the
                        # measured song length; unmeasurable = 33 s default.
                        dance_duration = config.get_dance_times().get(dance_key)
                        if dance_duration is not None and int(dance_duration) == 999:
                            dance_duration = None
                        if dance_duration is None:
                            dance_duration = library.duration_for(dance_key)
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
                        thank_you=thank_you,
                        goodbye=goodbye,
                        dance_duration=dance_duration,
                        volume=volume,
                        geared=node.mic_geared,
                        skip_dance=skip_dance,
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
    parser.add_argument("--mic-source-service",
                        default="/aimdk_5Fmsgs/srv/SetMicSourceRequest",
                        help="mic source switch service, forwarded to every "
                             "show: mute → external mic → unmute → perform → "
                             "built-in mic → mute ('' disables)")
    parser.add_argument("--mic-source-field", default="audio_stream_id")
    parser.add_argument("--mic-external", type=int, default=2)
    parser.add_argument("--mic-internal", type=int, default=1)
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
                           speaker_volume=args.speaker_volume,
                           mic_source_service=args.mic_source_service,
                           mic_internal=args.mic_internal,
                           mic_external=args.mic_external)
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, name="ros-spin", daemon=True)
    spin_thread.start()

    show_extra: list[str] = []
    if args.mic_source_service:
        show_extra += ["--mic-source-service", args.mic_source_service,
                       "--mic-source-field", args.mic_source_field,
                       "--mic-external", str(args.mic_external),
                       "--mic-internal", str(args.mic_internal)]
    shows = ShowRunner(node, extra_args=show_extra)
    config = PanelConfig(CONFIG_FILE)
    library = LibraryWatcher(node, config, args.library_poll)
    library.start_polling()
    messages = MessagesStore(MESSAGES_FILE)

    last_activity = [time.time()]
    handler = make_handler(node, shows, args.pin, config, library, messages,
                           last_activity)

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

    LOGGER.info("Cooper control API v%s at http://%s:%d/ (PIN %s)",
                SERVER_VERSION, args.bind, args.port,
                "enabled" if args.pin else "DISABLED")
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
