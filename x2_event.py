"""Play the pre-configured special-event moment and exit.

Standalone companion to the Cooper Control Panel: the panel server invokes
this script when ▶ Play event is pressed. The sequence is

    opening gesture → event message (TTS) → middle gesture → closing gesture

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
from aimdk_msgs.srv import PlayTts, SetMcPresetMotion
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from x2_action import (DEFAULT_PRESET_MOTION_SVC, DEFAULT_SET_MC_ACTION_SVC,
                       accepted, send_motion, stand_default)

DEFAULT_TTS_SERVICE = "/aimdk_5Fmsgs/srv/PlayTts"

# The opening gesture is launched just before the speech so it plays while
# the message starts (the same feel as the show's wave-during-greeting).
OPENING_LEAD_S = 1.0

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


def speak(node: Node, tts_client, text: str, trim_s: float = 0.0) -> bool:
    """Speak `text` and wait out its TEXT-BASED duration estimate.

    `trim_s` returns that many seconds early, so the caller can start
    the closing gesture while the last words still play. The engine's
    own estimated_duration is only printed for reference — it overshoots
    too much to time the sequence with.
    """
    req = PlayTts.Request()
    req.tts_req.text = text
    req.tts_req.domain = "x2-event"
    req.tts_req.trace_id = f"event-{uuid.uuid4()}"
    req.tts_req.is_interrupted = True
    req.tts_req.priority_weight = 0
    req.tts_req.priority_level.value = 6

    print(f"Speaking: {text[:80]}")
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
    wait_s = max(0.0, speech_seconds(text) - max(0.0, trim_s))
    print(f"PlayTts accepted — waiting {wait_s:.1f}s from the text length "
          f"(engine claims {engine_s:.1f}s, ignored)")
    time.sleep(wait_s)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Play the pre-configured event: opening gesture → "
                    "message → middle gesture → closing gesture"
    )
    parser.add_argument("--opening-motion", type=int, default=None)
    parser.add_argument("--opening-area", type=int, default=None)
    parser.add_argument("--message", default="")
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
    if not opening and not message and not middle and not closing:
        print("nothing to play: give --opening-motion/--opening-area, "
              "--message, --middle-motion/--middle-area and/or "
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
        if opening:
            ok &= run_gesture(node, motion_client, args.mc_action_service,
                              opening[0], opening[1], args.stand_settle)
            time.sleep(OPENING_LEAD_S if message else GESTURE_PLAY_S)
        pause = min(max(args.message_pause, MIN_MESSAGE_PAUSE_S),
                    MAX_MESSAGE_PAUSE_S)
        # The pause offset times the FIRST gesture after the message
        # (the middle one when set, else the closing one).
        after_msg = middle or closing
        if message:
            # Negative pause = start that gesture this many seconds
            # before the estimated end of the message (the gesture's own
            # ~1-2 s command latency eats part of the overlap).
            trim = -pause if (after_msg and pause < 0) else 0.0
            ok &= speak(node, tts_client, message, trim_s=trim)
            if after_msg and pause > 0:
                time.sleep(pause)
            elif not after_msg:
                # No gesture follows: linger briefly so the mic stays
                # muted through any speech tail the estimate missed.
                time.sleep(2.0)
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
