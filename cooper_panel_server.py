"""Cooper Control Panel — HTTP bridge to the AgiBot X2 (ROS2 / AimDK).

Runs ON the robot (or any machine on the same ROS2 domain) and exposes a
small REST API plus the control panel webpage, so Cooper can be driven
from any browser on the network:

    GET  /                  → cooper_control_panel.html
    GET  /api/status        → server + listening state
    GET  /api/dances        → LinkCraft dance resources (live from the robot)
    POST /api/dance         → {"key": "..."} start that dance now
    POST /api/listening     → {"listen": true|false} unmute / mute the mic
    POST /api/show          → {"dance_key": "...", "unmute_after": false}
                              run the full showroom sequence as a subprocess

Standard library only — no Flask/aiohttp needed on the robot.

Run on the robot:
    source /opt/ros/humble/setup.bash && source ~/aimdk/install/setup.bash
    python3 cooper_panel_server.py --port 8080

Then browse to http://<cooper-ip>:8080 — or open
cooper_control_panel.html anywhere and type Cooper's IP into the panel.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import subprocess
import sys
import threading
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


LOGGER = logging.getLogger("cooper_panel")

DEFAULT_GET_RESOURCES_SVC  = "/aimdk_5Fmsgs/srv/GetRobotResources"
DEFAULT_EXECUTE_ACTION_SVC = "/aimdk_5Fmsgs/srv/ExecuteActionResource"
DEFAULT_SET_MUTE_SVC       = "/aimdk_5Fmsgs/srv/SetMute"

PANEL_HTML_FILE = Path(__file__).resolve().parent / "cooper_control_panel.html"
SHOW_SCRIPT     = Path(__file__).resolve().parent / "x2_showroom_demo.py"


class CooperPanelNode(Node):
    """ROS2 side of the panel: talks to the AimDK services."""

    def __init__(self, mute_service: str) -> None:
        super().__init__("cooper_panel")
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

        # Last listening state we set (None until first change from the panel).
        self.listening_state: bool | None = None
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
        """Start a LinkCraft dance by resource key."""
        with self._lock:
            resource = self._resource_cache.get(resource_key)
        if resource is None:
            # Cache miss (server restarted, stale page) — refresh and retry.
            self.list_dances()
            with self._lock:
                resource = self._resource_cache.get(resource_key)
        if resource is None:
            raise RuntimeError(f"dance resource {resource_key!r} not found on robot")

        if not self._exec_action.wait_for_service(timeout_sec=5.0):
            raise RuntimeError("ExecuteActionResource service not available")

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

        resp = self._wait(self._exec_action.call_async(req), 20.0)
        if resp is None:
            raise RuntimeError("ExecuteActionResource timed out")

        code, msg = 0, ""
        with suppress(AttributeError, TypeError, ValueError):
            code = int(resp.header.header.code)
            msg  = str(resp.header.message or "").strip()
        if code not in (None, 0) or any(w in msg.lower() for w in ("fail", "error", "reject")):
            raise RuntimeError(f"dance rejected (code={code}): {msg}")
        return {"code": code, "message": msg}

    def set_listening(self, listen: bool) -> None:
        """Unmute (listen=True) or mute (listen=False) Cooper's microphones."""
        if self._set_mute is None:
            raise RuntimeError("SetMute not available in this aimdk_msgs build")
        if not self._set_mute.wait_for_service(timeout_sec=5.0):
            raise RuntimeError("SetMute service not available")

        req = SetMute.Request()
        self._stamp(req)
        req.is_mute = not listen

        response = None
        for _ in range(8):
            future = self._set_mute.call_async(req)
            done = threading.Event()
            future.add_done_callback(lambda _: done.set())
            if done.wait(0.5) and future.done():
                response = future.result()
                break
        if response is None:
            raise RuntimeError("SetMute timed out")
        self.listening_state = listen
        LOGGER.info("Microphone %s", "UNMUTED (listening)" if listen else "MUTED")


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

    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def start(self, dance_key: str | None, unmute_after: bool) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise RuntimeError("a show is already running")
            cmd = [sys.executable, str(SHOW_SCRIPT)]
            if dance_key:
                cmd += ["--dance-key", dance_key]
            if unmute_after:
                cmd += ["--unmute-after"]
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


def make_handler(node: CooperPanelNode, shows: ShowRunner, pin: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CooperPanel/1.0"

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
                return self._serve_panel()
            if self.path == "/api/status":
                return self._send_json({
                    "ok": True,
                    "listening": node.listening_state,
                    "show_running": shows.running(),
                    "pin_required": bool(pin),
                })
            if self.path == "/api/dances":
                try:
                    return self._send_json({"ok": True, "dances": node.list_dances()})
                except Exception as exc:
                    return self._send_json({"ok": False, "error": str(exc)}, 502)
            return self._send_json({"ok": False, "error": "not found"}, 404)

        def do_POST(self):
            if not self._pin_ok():
                return self._send_json({"ok": False, "error": "invalid PIN"}, 401)
            body = self._read_json()
            try:
                if self.path == "/api/dance":
                    key = str(body.get("key") or "")
                    if not key:
                        return self._send_json({"ok": False, "error": "missing 'key'"}, 400)
                    result = node.start_dance(key)
                    return self._send_json({"ok": True, **result})

                if self.path == "/api/listening":
                    if "listen" not in body:
                        return self._send_json({"ok": False, "error": "missing 'listen'"}, 400)
                    listen = bool(body["listen"])
                    node.set_listening(listen)
                    return self._send_json({"ok": True, "listening": listen})

                if self.path == "/api/show":
                    shows.start(
                        dance_key=str(body.get("dance_key") or "") or None,
                        unmute_after=bool(body.get("unmute_after", False)),
                    )
                    return self._send_json({"ok": True, "show_running": True})

                return self._send_json({"ok": False, "error": "not found"}, 404)
            except Exception as exc:
                LOGGER.exception("POST %s failed", self.path)
                return self._send_json({"ok": False, "error": str(exc)}, 502)

        def _serve_panel(self):
            try:
                body = PANEL_HTML_FILE.read_bytes()
            except OSError:
                body = b"<h1>Cooper Panel</h1><p>cooper_control_panel.html not found next to the server script.</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Cooper Control Panel server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--mute-service", default=DEFAULT_SET_MUTE_SVC)
    parser.add_argument("--pin", default=os.getenv("COOPER_PANEL_PIN", ""),
                        help="PIN required for all control actions "
                             "(env COOPER_PANEL_PIN; empty = no PIN)")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    rclpy.init()
    node = CooperPanelNode(mute_service=args.mute_service)
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, name="ros-spin", daemon=True)
    spin_thread.start()

    shows = ShowRunner(node)
    server = ThreadingHTTPServer(
        (args.bind, args.port), make_handler(node, shows, args.pin)
    )
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
