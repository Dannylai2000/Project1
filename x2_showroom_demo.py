"""AgiBot X2 sequential introduction script — no mic input.

Runs a fixed sequence once on startup:
  0. Mute microphones (stop the built-in assistant listening/answering)
  1. Greeting (spoken WHILE waving — no wait before speech starts)
  2. Self-introduction (dance resource is prefetched in the background)
  3. LinkCraft dance (APT 32s) — starts immediately, resource already cached
  4. Bow, Clap
  5. Thank you (spoken WHILE making the heart gesture)
  6. Goodbye (spoken WHILE blowing a kiss and waving)

The microphone stays muted after the show so the robot does not react to
surrounding conversation. Pass --unmute-after to restore listening when
the sequence ends, or --no-mute to skip mic control entirely.

Run on the robot:
    source /opt/ros/humble/setup.bash && source ~/aimdk/install/setup.bash
    python3 x2_showroom_demo.py
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
import uuid
from contextlib import suppress
from typing import Callable, Optional

import rclpy
from aimdk_msgs.msg import CommonState, McActionCommand, McControlArea, McPresetMotion, RequestHeader
from aimdk_msgs.srv import ExecuteActionResource, GetRobotResources, PlayTts, SetMcAction, SetMcPresetMotion
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

# SetMute mutes/unmutes the robot's microphone array (AimDK interaction module).
# Guarded import: older aimdk_msgs builds may not ship it.
try:
    from aimdk_msgs.srv import SetMute
except ImportError:  # pragma: no cover
    SetMute = None


LOGGER = logging.getLogger("x2_intro_sequence")

DEFAULT_TTS_SERVICE        = "/aimdk_5Fmsgs/srv/PlayTts"
DEFAULT_GET_RESOURCES_SVC  = "/aimdk_5Fmsgs/srv/GetRobotResources"
DEFAULT_EXECUTE_ACTION_SVC = "/aimdk_5Fmsgs/srv/ExecuteActionResource"
DEFAULT_PRESET_MOTION_SVC  = "/aimdk_5Fmsgs/srv/SetMcPresetMotion"
DEFAULT_SET_MC_ACTION_SVC  = "/aimdk_5Fmsgs/srv/SetMcAction"
DEFAULT_SET_MUTE_SVC       = "/aimdk_5Fmsgs/srv/SetMute"

# ── Dance (step 3) ────────────────────────────────────────────────────────────
DANCE_RESOURCE_KEY = "linkcraft_resource_onnx_01KYPD7XFJ6HA7NKHY0RE6DN9K"  # APT 32
#DANCE_RESOURCE_KEY = "linkcraft_resource_onnx_01KYXBBE96QHBV3W025107HQ20" # Smooth Criminal 66
DANCE_DURATION_S   = 30.0

# ── Final preset motion (step 5) ──────────────────────────────────────────────
# Heart gesture (both hands): motion=1007, area=3
FINAL_MOTION_ID     = 1007
FINAL_AREA_ID       = 3
FINAL_MOTION_WAIT_S = 1.0

# ── Sequence texts ────────────────────────────────────────────────────────────
GREETING_TEXT  = "Thank You Caden for remembering me! Hello everyone! It is wonderful to be here with you today.:q"
INTRO_TEXT     = (
    " My name is Cooper. I am the One Comcentre Ambassador, a AI-Enabled humanoid robot. "
    " I may still be in training, but I'm ready for the future."
    " Before I begin, please give me at least 2 meters distance. Here is my best move. I hope you enjoy the show!"
)
THANK_YOU_TEXT = "Thank you so much for watching! It was a true pleasure performing for you."
GOODBY_TEXT = "See you in One Comcentre for the future. Bye!"

POST_TTS_GRACE_S = 0.2


class IntroSequenceNode(Node):
    def __init__(
        self,
        tts_service: str,
        mute_service: str,
        mute_enabled: bool,
        unmute_after: bool,
    ) -> None:
        super().__init__("x2_intro_sequence")

        self._shutdown_event = threading.Event()
        self._cbg = MutuallyExclusiveCallbackGroup()
        self._mute_enabled = mute_enabled
        self._unmute_after = unmute_after

        # ── PlayTts ────────────────────────────────────────────────────────
        self._tts = self.create_client(PlayTts, tts_service, callback_group=self._cbg)
        self.get_logger().info(f"waiting for {tts_service} ...")
        if not self._tts.wait_for_service(timeout_sec=10.0):
            raise RuntimeError(f"PlayTts service {tts_service!r} not available")
        self.get_logger().info("PlayTts ready")

        # ── SetMute (microphone on/off) ────────────────────────────────────
        self._set_mute = None
        if SetMute is not None:
            self._set_mute = self.create_client(
                SetMute, mute_service, callback_group=self._cbg
            )
        elif mute_enabled:
            LOGGER.warning(
                "aimdk_msgs.srv.SetMute not available in this SDK build — "
                "cannot control microphone; the assistant may keep listening"
            )

        # ── LinkCraft service clients ──────────────────────────────────────
        self._get_resources = self.create_client(
            GetRobotResources, DEFAULT_GET_RESOURCES_SVC, callback_group=self._cbg
        )
        self._exec_action = self.create_client(
            ExecuteActionResource, DEFAULT_EXECUTE_ACTION_SVC, callback_group=self._cbg
        )

        # ── Preset motion + stand clients ─────────────────────────────────
        self._preset_motion = self.create_client(
            SetMcPresetMotion, DEFAULT_PRESET_MOTION_SVC, callback_group=self._cbg
        )
        self._set_mc_action = self.create_client(
            SetMcAction, DEFAULT_SET_MC_ACTION_SVC, callback_group=self._cbg
        )

        # Prefetch the dance resource while the robot is still greeting, so
        # step 3 starts the dance without a GetRobotResources round-trip.
        self._dance_resource = None
        self._dance_ready = threading.Event()
        threading.Thread(
            target=self._prefetch_dance_resource, name="dance-prefetch", daemon=True
        ).start()

        threading.Thread(
            target=self._run_sequence, name="intro-sequence", daemon=True
        ).start()
        self.get_logger().info("Sequence thread started")

    # ── helpers ────────────────────────────────────────────────────────────

    def _wait(self, future, timeout: float):
        done = threading.Event()
        future.add_done_callback(lambda _: done.set())
        return future.result() if done.wait(timeout) else None

    def _stamp(self, req) -> None:
        now = self.get_clock().now()
        req.header.header.stamp.sec     = now.nanoseconds // 1_000_000_000
        req.header.header.stamp.nanosec = now.nanoseconds % 1_000_000_000

    # ── Microphone mute (listening on/off) ─────────────────────────────────

    def _set_listening(self, listen: bool) -> bool:
        """Turn the microphone array on (listen=True) or off (listen=False).

        Muting stops the built-in voice assistant from hearing surrounding
        conversation and answering during the show.
        """
        if self._set_mute is None:
            return False

        if not self._set_mute.wait_for_service(timeout_sec=3.0):
            LOGGER.warning("SetMute service not available — cannot change listening state")
            return False

        req = SetMute.Request()
        with suppress(Exception):
            self._stamp(req)
        with suppress(Exception):
            req.header.stamp = self.get_clock().now().to_msg()
        req.is_mute = not listen

        response = None
        for i in range(8):
            future = self._set_mute.call_async(req)
            done = threading.Event()
            future.add_done_callback(lambda _: done.set())
            if done.wait(0.5) and future.done():
                response = future.result()
                break
            LOGGER.info("SetMute retry [%d]", i)

        if response is None:
            LOGGER.error("SetMute timed out — listening state unchanged")
            return False

        LOGGER.info("Microphone %s", "UNMUTED (listening)" if listen else "MUTED (not listening)")
        return True

    # ── TTS ────────────────────────────────────────────────────────────────

    def _start_speech(self, text: str) -> float:
        """Send the TTS request and return the estimated speech duration.

        Returns 0.0 if the request failed (sequence continues regardless).
        """
        req = PlayTts.Request()
        req.tts_req.text             = text
        req.tts_req.domain           = "x2-intro-sequence"
        req.tts_req.trace_id         = f"intro-{uuid.uuid4()}"
        req.tts_req.is_interrupted   = True
        req.tts_req.priority_weight  = 0
        req.tts_req.priority_level.value = 6

        LOGGER.info("Speaking: %s", text[:80])
        response = self._wait(self._tts.call_async(req), 10.0)

        if response is None:
            LOGGER.error("PlayTts timed out — continuing sequence")
            return 0.0
        if not response.tts_resp.is_success:
            LOGGER.error("PlayTts rejected: %s — continuing sequence",
                         response.tts_resp.error_message)
            return 0.0

        duration_s = float(response.tts_resp.estimated_duration) / 1000.0
        if duration_s <= 0.0:
            duration_s = max(2.0, len(text) / 12.0)
            LOGGER.info("PlayTts accepted (estimated ~%.2fs from text length)", duration_s)
        else:
            LOGGER.info("PlayTts accepted (~%.2fs)", duration_s)
        return duration_s

    def _speak(self, text: str, during: Optional[Callable[[], None]] = None) -> None:
        """Speak `text`; optionally run `during()` while the speech plays.

        Motions launched via `during` overlap the speech instead of adding
        their own dead time to the sequence.
        """
        started = time.monotonic()
        duration_s = self._start_speech(text)

        if during is not None:
            during()

        remaining = duration_s + POST_TTS_GRACE_S - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)

    # ── LinkCraft dance ────────────────────────────────────────────────────

    def _prefetch_dance_resource(self) -> None:
        """Fetch the LinkCraft dance resource in the background at startup."""
        try:
            if not self._get_resources.wait_for_service(timeout_sec=15.0):
                LOGGER.warning("GetRobotResources not available during prefetch")
                return
            req = GetRobotResources.Request()
            self._stamp(req)
            resp = self._wait(self._get_resources.call_async(req), 30.0)
            if resp is None:
                LOGGER.warning("Prefetch of LinkCraft resources timed out")
                return
            for r in resp.robot_resources:
                if r.resource_key == DANCE_RESOURCE_KEY:
                    self._dance_resource = r
                    LOGGER.info("Dance resource prefetched: %s", r.resource_key)
                    return
            LOGGER.warning("Resource key %r not found during prefetch", DANCE_RESOURCE_KEY)
        except Exception:
            LOGGER.exception("Dance resource prefetch failed")
        finally:
            self._dance_ready.set()

    def _fetch_dance_resource(self, resource_key: str):
        """Blocking fallback fetch, used only if the prefetch didn't deliver."""
        while not self._get_resources.wait_for_service(timeout_sec=1.0):
            if not rclpy.ok():
                return None
            LOGGER.info("GetRobotResources not available, waiting...")

        req = GetRobotResources.Request()
        self._stamp(req)
        resp = self._wait(self._get_resources.call_async(req), 30.0)
        if resp is None:
            LOGGER.error("Timed out fetching LinkCraft resources")
            return None
        for r in resp.robot_resources:
            if r.resource_key == resource_key:
                return r
        LOGGER.error("Resource key %r not found", resource_key)
        return None

    def _run_linkcraft_action(self, resource_key: str, wait_duration_s: float) -> bool:
        # Use the prefetched resource when available; fall back to a live fetch.
        self._dance_ready.wait(timeout=20.0)
        resource = self._dance_resource
        if resource is None:
            resource = self._fetch_dance_resource(resource_key)
        if resource is None:
            LOGGER.error("No dance resource — skipping dance")
            return False

        while not self._exec_action.wait_for_service(timeout_sec=1.0):
            if not rclpy.ok():
                return False
            LOGGER.info("ExecuteActionResource not available, waiting...")

        req2 = ExecuteActionResource.Request()
        self._stamp(req2)
        req2.resource_key     = resource.resource_key
        req2.resource_version = resource.current_version.version
        req2.slaves = []
        req2.meta = (
            '{"resource_type": "BODY_MONTION"}'
            if "onnx" in req2.resource_key.lower()
            else '{"resource_type": "ARM_MONTION"}'
        )

        self.get_logger().info(
            f"Dance request: key={req2.resource_key} version={req2.resource_version} meta={req2.meta}"
        )

        resp2 = self._wait(self._exec_action.call_async(req2), 30.0)
        if resp2 is None:
            LOGGER.error("Timed out starting dance — skipping")
            return False

        try:
            code = int(resp2.header.header.code)
            msg  = str(resp2.header.message or "").strip()
            self.get_logger().info(f"Dance response: code={code} message={msg}")
        except (AttributeError, TypeError, ValueError):
            code, msg = 0, ""

        if code not in (None, 0) or any(w in msg.lower() for w in ("fail", "error", "reject")):
            LOGGER.error("Dance rejected (code=%s): %s", code, msg)
            return False

        LOGGER.info("Dance started; waiting %.0fs", wait_duration_s)
        time.sleep(wait_duration_s)
        return True

    # ── Stand default (required before every preset motion) ───────────────

    def _stand_default(self) -> bool:
        """Switch the robot to Stable Stand mode before a preset motion."""
        while not self._set_mc_action.wait_for_service(timeout_sec=1.0):
            if not rclpy.ok():
                return False
            LOGGER.info("SetMcAction not available, waiting...")

        req = SetMcAction.Request()
        req.header = RequestHeader()
        req.source = "node"
        cmd = McActionCommand()
        cmd.action_desc = "STAND_DEFAULT"
        req.command = cmd

        for i in range(8):
            req.header.stamp = self.get_clock().now().to_msg()
            future = self._set_mc_action.call_async(req)
            done = threading.Event()
            future.add_done_callback(lambda _: done.set())
            if done.wait(0.5) and future.done():
                break
            LOGGER.info("SetMcAction retry [%d]", i)

        if not future.done():
            LOGGER.error("SetMcAction (STAND_DEFAULT) timed out")
            return False

        resp = future.result()
        if resp is None:
            LOGGER.error("SetMcAction returned no response")
            return False

        state = int(resp.response.status.value)
        if state not in (CommonState.SUCCESS, CommonState.RUNNING):
            LOGGER.error("SetMcAction (STAND_DEFAULT) rejected: state=%s", state)
            return False

        self.get_logger().info("STAND_DEFAULT accepted; waiting 8s to settle")
        time.sleep(8.0)
        return True

    # ── Preset motion ──────────────────────────────────────────────────────

    def _run_preset_motion(self, motion_id: int, area_id: int, wait_s: float) -> bool:
        """Send a SetMcPresetMotion request."""
        req = SetMcPresetMotion.Request()
        req.header         = RequestHeader()
        req.motion         = McPresetMotion()
        req.area           = McControlArea()
        req.motion.value   = int(motion_id)
        req.area.value     = int(area_id)
        req.interrupt      = False
        req.ani_path       = ""
        req.play_timestamp = 0

        self.get_logger().info(
            f"Preset motion request: motion={motion_id} area={area_id}"
        )

        # Retry loop — mirrors 6.1.4 example (remote peer not handled well by ROS)
        response = None
        for i in range(8):
            req.header.stamp = self.get_clock().now().to_msg()
            future = self._preset_motion.call_async(req)
            done = threading.Event()
            future.add_done_callback(lambda _: done.set())
            if done.wait(0.25) and future.done():
                response = future.result()
                break
            LOGGER.info("Preset motion retry [%d]", i)

        if response is None:
            LOGGER.error("SetMcPresetMotion timed out — skipping")
            return False

        code    = int(response.response.header.code)
        state   = int(response.response.state.value)
        task_id = int(response.response.task_id)

        self.get_logger().info(
            f"Preset motion response: code={code} task_id={task_id} state={state}"
        )

        if code == 0 or state in (CommonState.SUCCESS, CommonState.RUNNING):
            if wait_s > 0:
                LOGGER.info("Preset motion accepted; waiting %.1fs", wait_s)
                time.sleep(wait_s)
            return True

        LOGGER.error("Preset motion rejected: code=%s state=%s task_id=%s", code, state, task_id)
        return False

    # ── Sequence ───────────────────────────────────────────────────────────

    def _run_sequence(self) -> None:
        try:
            if self._mute_enabled:
                self.get_logger().info("=== STEP 0: MUTE MIC (stop listening) ===")
                self._set_listening(False)

            # Greeting speech starts immediately; the wave overlaps it.
            self.get_logger().info("=== STEP 1: GREETING + WAVE ===")
            self._speak(
                GREETING_TEXT,
                during=lambda: self._run_preset_motion(1002, 2, 0.0),
            )

            self.get_logger().info("=== STEP 2: SELF-INTRODUCTION ===")
            self._speak(INTRO_TEXT)

            self.get_logger().info("=== STEP 3: DANCE ===")
            self._run_linkcraft_action(DANCE_RESOURCE_KEY, DANCE_DURATION_S)

            self.get_logger().info("=== STEP 3a: Bow ===")
            self._run_preset_motion(3001, 11, FINAL_MOTION_WAIT_S)

            self.get_logger().info("=== STEP 3b: Clap ===")
            self._run_preset_motion(3017, 11, FINAL_MOTION_WAIT_S)

            # Thank-you speech and heart gesture play together.
            self.get_logger().info("=== STEP 4: THANK YOU + HEART ===")
            self._speak(
                THANK_YOU_TEXT,
                during=lambda: self._run_preset_motion(
                    FINAL_MOTION_ID, FINAL_AREA_ID, 0.0
                ),
            )

            # Goodbye speech overlaps the blow kiss; wave follows right after.
            self.get_logger().info("=== STEP 5: GOODBYE + BLOW KISS ===")
            self._speak(
                GOODBY_TEXT,
                during=lambda: self._run_preset_motion(1004, 2, 0.0),
            )

            self.get_logger().info("=== STEP 6: WAVE GOODBYE ===")
            self._run_preset_motion(3031, 11, 2.0)

            self.get_logger().info("=== Sequence complete ===")
        except Exception:
            LOGGER.exception("Sequence failed unexpectedly")
        finally:
            if self._mute_enabled and self._unmute_after:
                self.get_logger().info("Restoring listening (unmute)")
                self._set_listening(True)
            self._shutdown_event.set()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AgiBot X2 sequential introduction — runs once, mic muted"
    )
    parser.add_argument("--tts-service", default=DEFAULT_TTS_SERVICE)
    parser.add_argument("--mute-service", default=DEFAULT_SET_MUTE_SVC,
                        help="SetMute service name for microphone control")
    parser.add_argument("--no-mute", action="store_true",
                        help="do not touch the microphone (leave listening as-is)")
    parser.add_argument("--unmute-after", action="store_true",
                        help="restore listening when the sequence finishes "
                             "(default: stay muted)")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    rclpy.init()
    node = IntroSequenceNode(
        tts_service=args.tts_service,
        mute_service=args.mute_service,
        mute_enabled=not args.no_mute,
        unmute_after=args.unmute_after,
    )
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    try:
        while rclpy.ok() and not node._shutdown_event.is_set():
            executor.spin_once(timeout_sec=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        with suppress(Exception):
            node.destroy_node()
        with suppress(Exception):
            rclpy.shutdown()


if __name__ == "__main__":
    main()
