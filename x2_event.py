"""Play the pre-configured special-event moment and exit.

Standalone companion to the Cooper Control Panel: the panel server invokes
this script when ▶ Play event is pressed. The sequence is

    opening gesture → event message (TTS) → dance → middle gesture → closing gesture

and every part is optional — the panel stores which gestures and text to
use in cooper_panel_config.json. It can also be tested by hand:

    python3 x2_event.py --opening-motion 1002 --opening-area 2 \
        --message "Welcome to One Comcentre!" \
        --closing-motion 1007 --closing-area 3

Gestures reuse x2_action.py's logic, including the automatic Stable-Stand
retry when the controller rejects a preset motion.

Exit codes: 0 = every requested part accepted, 1 = a service is
unavailable / bad args, 2 = one or more parts were rejected (the rest of
the sequence still runs).
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import uuid
from contextlib import suppress

import rclpy
from aimdk_msgs.srv import ExecuteActionResource, GetRobotResources, PlayTts, SetMcPresetMotion
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from x2_action import (DEFAULT_PRESET_MOTION_SVC, DEFAULT_SET_MC_ACTION_SVC,
                       accepted, send_motion, stand_default)

DEFAULT_TTS_SERVICE = "/aimdk_5Fmsgs/srv/PlayTts"

# The opening gesture is launched just before the speech so it plays while
# the message starts (the same feel as the show's wave-during-greeting).
OPENING_LEAD_S = 1.0

# Bluetooth speakers sleep their audio link and swallow the first words
# of a message (field report 2026-10-02: the first three words were cut
# off on the external BT speaker). A tiny primer utterance is sent this
# long before the real message to wake the link; it overlaps the opening
# gesture so the event barely slows down. "." renders as (near) silence
# on most TTS engines — if the cutoff persists, the primer never made a
# sound: set an audible --warmup-text instead.
AUDIO_WARMUP_TEXT = "."
AUDIO_WARMUP_S = 2.0

# Second layer of protection: even with the primer, a Bluetooth link can
# still swallow a fraction of a second at stream start (field report:
# "Hi" was still lost). Leading punctuation renders as ~1 s of silence
# at the head of the message stream, so what gets swallowed is silence,
# not the first word.
MESSAGE_LEAD_SILENCE = ", , , "

# When there is no message, or after the closing gesture, give a motion
# this long to play out before the program (and the panel's "event
# playing" state) ends.
GESTURE_PLAY_S = 4.0

# Gap between the middle gesture and the closing one — long enough for
# the middle motion to play out, short enough to keep the finale tight.
MIDDLE_PLAY_S = 6.0

# The TTS engine's estimated_duration overshoots badly (measured ~9 s too
# long on the show-suite message), and the overshoot scales with the text —
# so it is IGNORED for timing. The speech time is estimated from the text
# itself instead (speech_seconds below), and the panel-configured
# --message-pause says when the closing gesture starts relative to the
# estimated end of the message: NEGATIVE = that many seconds before the
# end (the gesture overlaps the last words — accounting for the ~1-2 s a
# commanded motion takes to become visible), positive = pause after it.
MIN_MESSAGE_PAUSE_S = -10.0
DEFAULT_MESSAGE_PAUSE_S = -3.0
MAX_MESSAGE_PAUSE_S = 30.0

# Speaking rates for the text-based estimate. Latin rate CALIBRATED on the
# show-suite robot: a 563-char message took ~41 s to speak (journal,
# 2026-09-23), i.e. ~13.7 chars/s; 14 errs slightly fast on purpose — an
# estimate that runs short just starts the gesture on the last words, and
# the panel's Pause field keeps full control, while one that runs long
# adds dead air no pause setting can remove.
LATIN_CHARS_PER_S = 14.0
CJK_CHARS_PER_S = 4.0


def speech_seconds(text: str) -> float:
    """Estimated speaking time of `text`, from its length and script."""
    cjk = sum(1 for ch in text if ch >= "⺀")
    other = len(text) - cjk
    return max(2.0, other / LATIN_CHARS_PER_S + cjk / CJK_CHARS_PER_S)


def run_gesture(node: Node, client, mc_action_service: str,
                motion: int, area: int, settle_s: float) -> bool:
    """One preset motion with x2_action.py's Stable-Stand retry."""
    code, state = send_motion(node, client, motion, area)
    if accepted(code, state):
        print(f"gesture {motion}/{area} accepted")
        return True
    if code is None:
        print("SetMcPresetMotion timed out", file=sys.stderr)
        return False
    print(f"gesture rejected (code={code} state={state}) — switching to "
          "Stable Stand and retrying")
    if stand_default(node, mc_action_service, settle_s):
        code, state = send_motion(node, client, motion, area)
        if accepted(code, state):
            print(f"gesture {motion}/{area} accepted after Stable Stand")
            return True
    print(f"gesture {motion}/{area} rejected (code={code} state={state}) "
          "even after Stable Stand", file=sys.stderr)
    return False


def warm_up_audio(node: Node, tts_client, text: str) -> None:
    """Fire a tiny TTS primer to wake a sleeping (Bluetooth) audio link.

    Best-effort and fast: failures never block the event.
    """
    with suppress(Exception):
        req = PlayTts.Request()
        req.tts_req.text = text
        req.tts_req.domain = "x2-event"
        req.tts_req.trace_id = f"event-warmup-{uuid.uuid4()}"
        req.tts_req.is_interrupted = True
        req.tts_req.priority_weight = 0
        req.tts_req.priority_level.value = 6
        future = tts_client.call_async(req)
        done = threading.Event()
        future.add_done_callback(lambda _: done.set())
        done.wait(3.0)
        print("audio warm-up primer sent")


def speak(node: Node, tts_client, text: str, trim_s: float = 0.0,
          lead_silence: bool = False) -> bool:
    """Speak `text` and wait out its TEXT-BASED duration estimate.

    `trim_s` returns that many seconds early, so the caller can start
    the closing gesture while the last words still play. `lead_silence`
    prepends ~1 s of silence for Bluetooth speakers (see
    MESSAGE_LEAD_SILENCE). The engine's own estimated_duration is only
    printed for reference — it overshoots too much to time with.
    """
    padded = (MESSAGE_LEAD_SILENCE if lead_silence else "") + text
    req = PlayTts.Request()
    req.tts_req.text = padded
    req.tts_req.domain = "x2-event"
    req.tts_req.trace_id = f"event-{uuid.uuid4()}"
    req.tts_req.is_interrupted = True
    req.tts_req.priority_weight = 0
    req.tts_req.priority_level.value = 6

    print(("Speaking (with silent lead-in): " if lead_silence
           else "Speaking: ") + text[:80])
    future = tts_client.call_async(req)
    done = threading.Event()
    future.add_done_callback(lambda _: done.set())
    if not done.wait(10.0) or not future.done():
        print("PlayTts timed out", file=sys.stderr)
        return False
    response = future.result()
    if not response.tts_resp.is_success:
        print(f"PlayTts rejected: {response.tts_resp.error_message}",
              file=sys.stderr)
        return False

    engine_s = float(response.tts_resp.estimated_duration) / 1000.0
    wait_s = max(0.0, speech_seconds(padded) - max(0.0, trim_s))
    print(f"PlayTts accepted — waiting {wait_s:.1f}s from the text length "
          f"(engine claims {engine_s:.1f}s, ignored)")
    time.sleep(wait_s)
    return True


def run_dance(node: Node, key: str, duration: float, wait: float) -> bool:
    """Execute a LinkCraft resource using the same request shape as the show."""
    def call(client, request):
        with suppress(AttributeError):
            request.header.stamp = node.get_clock().now().to_msg()
        future = client.call_async(request)
        done = threading.Event()
        future.add_done_callback(lambda _: done.set())
        if not done.wait(30.0) or not future.done():
            raise RuntimeError("LinkCraft request timed out")
        return future.result()

    clients = []
    try:
        resources = node.create_client(GetRobotResources, "/aimdk_5Fmsgs/srv/GetRobotResources")
        clients.append(resources)
        execute = node.create_client(ExecuteActionResource, "/aimdk_5Fmsgs/srv/ExecuteActionResource")
        clients.append(execute)
        if not all(c.wait_for_service(timeout_sec=wait) for c in clients):
            raise RuntimeError("LinkCraft service unavailable")
        response = call(resources, GetRobotResources.Request())
        resource = next((r for r in response.robot_resources if r.resource_key == key), None)
        if resource is None:
            raise RuntimeError("event dance not found on this robot")
        request = ExecuteActionResource.Request()
        request.resource_key = key
        request.resource_version = resource.current_version.version
        request.slaves = []
        request.meta = ('{"resource_type": "BODY_MONTION"}' if "onnx" in key.lower()
                        else '{"resource_type": "ARM_MONTION"}')
        response = call(execute, request)
        code, message = 0, ""
        with suppress(AttributeError, TypeError, ValueError):
            code = int(response.header.header.code)
            message = str(response.header.message or "")
        if code != 0 or any(w in message.lower() for w in ("fail", "error", "reject")):
            raise RuntimeError(f"event dance rejected ({code}): {message}")
        print(f"Event dance started; waiting {duration:.1f}s")
        time.sleep(max(0.0, duration))
        return True
    except Exception as exc:
        print(f"Event dance failed: {exc}", file=sys.stderr)
        return False
    finally:
        for client in clients:
            node.destroy_client(client)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Play the pre-configured event: opening gesture → "
                    "message → dance → middle gesture → closing gesture"
    )
    parser.add_argument("--opening-motion", type=int, default=None)
    parser.add_argument("--opening-area", type=int, default=None)
    parser.add_argument("--message", default="")
    parser.add_argument("--dance-key", default="")
    parser.add_argument("--dance-duration", type=float, default=33.0)
    parser.add_argument("--middle-motion", type=int, default=None)
    parser.add_argument("--middle-area", type=int, default=None)
    parser.add_argument("--closing-motion", type=int, default=None)
    parser.add_argument("--closing-area", type=int, default=None)
    parser.add_argument("--service", default=DEFAULT_PRESET_MOTION_SVC)
    parser.add_argument("--mc-action-service", default=DEFAULT_SET_MC_ACTION_SVC)
    parser.add_argument("--tts-service", default=DEFAULT_TTS_SERVICE)
    parser.add_argument("--stand-settle", type=float, default=8.0,
                        help="seconds Stable Stand needs before a retry")
    parser.add_argument("--message-pause", type=float,
                        default=DEFAULT_MESSAGE_PAUSE_S,
                        help="when the closing gesture starts, relative to "
                             "the (estimated) end of the message: negative "
                             "= that many seconds BEFORE the end (overlap, "
                             f"default {DEFAULT_MESSAGE_PAUSE_S}), positive "
                             "= pause after it")
    parser.add_argument("--bt-speaker", action="store_true",
                        help="the audio goes to an external Bluetooth "
                             "speaker: wake its link with a TTS primer "
                             "and prepend a silent lead-in to the "
                             "message (BT links swallow stream starts)")
    parser.add_argument("--warmup-text", default=AUDIO_WARMUP_TEXT,
                        help="primer utterance for --bt-speaker "
                             "('' disables the primer only)")
    parser.add_argument("--wait", type=float, default=5.0,
                        help="seconds to wait for each service")
    args = parser.parse_args()

    opening = (args.opening_motion, args.opening_area) \
        if args.opening_motion is not None and args.opening_area is not None else None
    middle = (args.middle_motion, args.middle_area) \
        if args.middle_motion is not None and args.middle_area is not None else None
    closing = (args.closing_motion, args.closing_area) \
        if args.closing_motion is not None and args.closing_area is not None else None
    message = args.message.strip()
    if not opening and not message and not args.dance_key and not middle and not closing:
        print("nothing to play: give --opening-motion/--opening-area, "
              "--message, --dance-key, --middle-motion/--middle-area and/or "
              "--closing-motion/--closing-area", file=sys.stderr)
        return 1

    rclpy.init()
    node = Node("x2_event")
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    threading.Thread(target=executor.spin, daemon=True).start()
    try:
        motion_client = None
        if opening or middle or closing:
            motion_client = node.create_client(SetMcPresetMotion, args.service)
            if not motion_client.wait_for_service(timeout_sec=args.wait):
                print(f"SetMcPresetMotion service not available at "
                      f"{args.service}", file=sys.stderr)
                return 1
        tts_client = None
        if message:
            tts_client = node.create_client(PlayTts, args.tts_service)
            if not tts_client.wait_for_service(timeout_sec=args.wait):
                print(f"PlayTts service not available at {args.tts_service}",
                      file=sys.stderr)
                return 1

        ok = True
        # Wake the audio path first (Bluetooth speakers swallow the first
        # words otherwise); the wait overlaps the opening gesture below.
        # Skipped entirely on the in-built speaker (--bt-speaker not set).
        warmup_until = 0.0
        if message and args.bt_speaker and args.warmup_text:
            warm_up_audio(node, tts_client, args.warmup_text)
            warmup_until = time.monotonic() + AUDIO_WARMUP_S
        if opening:
            ok &= run_gesture(node, motion_client, args.mc_action_service,
                              opening[0], opening[1], args.stand_settle)
            time.sleep(OPENING_LEAD_S if message else GESTURE_PLAY_S)
        pause = min(max(args.message_pause, MIN_MESSAGE_PAUSE_S),
                    MAX_MESSAGE_PAUSE_S)
        # The pause offset times the FIRST gesture after the message
        # (the middle one when set, else the closing one).
        after_msg = args.dance_key or middle or closing
        if message:
            # Negative pause = start that gesture this many seconds
            # before the estimated end of the message (the gesture's own
            # ~1-2 s command latency eats part of the overlap).
            trim = -pause if (after_msg and pause < 0) else 0.0
            remaining = warmup_until - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)  # give the primer time to open the link
            ok &= speak(node, tts_client, message, trim_s=trim,
                        lead_silence=args.bt_speaker)
            if after_msg and pause > 0:
                time.sleep(pause)
            elif not after_msg:
                # No gesture follows: linger briefly so the mic stays
                # muted through any speech tail the estimate missed.
                time.sleep(2.0)
        if args.dance_key:
            if not run_dance(node, args.dance_key, args.dance_duration, args.wait):
                return 2  # Do not launch gestures while dance state is uncertain.
            if middle or closing:
                if not stand_default(node, args.mc_action_service, args.stand_settle):
                    return 2
        if middle:
            ok &= run_gesture(node, motion_client, args.mc_action_service,
                              middle[0], middle[1], args.stand_settle)
            # Let the middle gesture play out before the closing one.
            time.sleep(MIDDLE_PLAY_S if closing else GESTURE_PLAY_S)
        if closing:
            ok &= run_gesture(node, motion_client, args.mc_action_service,
                              closing[0], closing[1], args.stand_settle)
            time.sleep(GESTURE_PLAY_S)
        print("event sequence complete" if ok
              else "event sequence complete with failures")
        return 0 if ok else 2
    finally:
        executor.shutdown()
        with suppress(Exception):
            node.destroy_node()
        with suppress(Exception):
            rclpy.shutdown()


if __name__ == "__main__":
    sys.exit(main())
