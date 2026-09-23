"""Find which control area a preset motion accepts.

The McPresetMotion enum lists motion IDs but not which McControlArea each
one expects, and a wrong area is rejected exactly like an unsupported
motion (code=1 state=FAILURE). This probe tries one motion across the
candidate areas and reports the first one the robot accepts — the robot
then performs the gesture, which is the proof.

Run on the robot, standing, out of the native app's manual control, and
NOT right after a dance (no Stable-Stand retry here, on purpose):

    python3 x2_probe_area.py --motion 3001
    python3 x2_probe_area.py --motion 2001 --areas 8,0,3

Exit codes: 0 = an area was accepted (printed), 2 = every area rejected
(the motion is unsupported by this firmware, or the robot is in a mode
that refuses preset motions), 1 = service unavailable.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from contextlib import suppress

import rclpy
from aimdk_msgs.srv import SetMcPresetMotion
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from x2_action import DEFAULT_PRESET_MOTION_SVC, accepted, send_motion

# 0=NONE first (whole-body interaction moves), then both/right/left
# hands, head, waist — see McControlArea.
DEFAULT_AREAS = "0,3,2,1,4,8"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Try a preset motion across control areas and report "
                    "the first one the robot accepts"
    )
    parser.add_argument("--motion", type=int, required=True)
    parser.add_argument("--areas", default=DEFAULT_AREAS,
                        help=f"comma-separated candidates (default {DEFAULT_AREAS})")
    parser.add_argument("--service", default=DEFAULT_PRESET_MOTION_SVC)
    parser.add_argument("--gap", type=float, default=2.0,
                        help="seconds between rejected attempts")
    parser.add_argument("--wait", type=float, default=5.0,
                        help="seconds to wait for the service")
    args = parser.parse_args()
    areas = [int(a) for a in args.areas.split(",") if a.strip() != ""]

    rclpy.init()
    node = Node("x2_probe_area")
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    threading.Thread(target=executor.spin, daemon=True).start()
    try:
        client = node.create_client(SetMcPresetMotion, args.service)
        if not client.wait_for_service(timeout_sec=args.wait):
            print(f"SetMcPresetMotion service not available at {args.service}",
                  file=sys.stderr)
            return 1
        for area in areas:
            code, state = send_motion(node, client, args.motion, area)
            if accepted(code, state):
                print(f"MOTION {args.motion}: AREA {area} ACCEPTED — "
                      "the robot performs it now")
                return 0
            print(f"motion {args.motion} area {area}: rejected "
                  f"(code={code} state={state})")
            time.sleep(args.gap)
        print(f"MOTION {args.motion}: every area rejected — unsupported by "
              "this firmware, or the robot is refusing preset motions "
              "(sitting / manual control / dance mode?)", file=sys.stderr)
        return 2
    finally:
        executor.shutdown()
        with suppress(Exception):
            node.destroy_node()
        with suppress(Exception):
            rclpy.shutdown()


if __name__ == "__main__":
    sys.exit(main())
