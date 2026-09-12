"""Run one AgiBot X2 preset motion (gesture) and exit.

Standalone companion to the Cooper Control Panel: the panel server invokes
this script for each Action executed from the panel, so gesture execution
lives in its own Python program and can also be tested by hand:

    python3 x2_action.py --motion 1002 --area 2      # right-hand wave

Exit codes: 0 = accepted by the robot, 1 = service unavailable / bad args,
2 = rejected or timed out.
"""

from __future__ import annotations

import argparse
import sys
import threading
from contextlib import suppress

import rclpy
from aimdk_msgs.msg import CommonState, McControlArea, McPresetMotion, RequestHeader
from aimdk_msgs.srv import SetMcPresetMotion
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

DEFAULT_PRESET_MOTION_SVC = "/aimdk_5Fmsgs/srv/SetMcPresetMotion"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one AgiBot X2 preset motion (gesture) and exit"
    )
    parser.add_argument("--motion", type=int, required=True, help="preset motion id")
    parser.add_argument("--area", type=int, required=True, help="control area id")
    parser.add_argument("--service", default=DEFAULT_PRESET_MOTION_SVC)
    parser.add_argument("--wait", type=float, default=5.0,
                        help="seconds to wait for the service")
    args = parser.parse_args()

    rclpy.init()
    node = Node("x2_action")
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    threading.Thread(target=executor.spin, daemon=True).start()
    try:
        client = node.create_client(SetMcPresetMotion, args.service)
        if not client.wait_for_service(timeout_sec=args.wait):
            print(f"SetMcPresetMotion service not available at {args.service}",
                  file=sys.stderr)
            return 1

        req = SetMcPresetMotion.Request()
        req.header         = RequestHeader()
        req.motion         = McPresetMotion()
        req.area           = McControlArea()
        req.motion.value   = int(args.motion)
        req.area.value     = int(args.area)
        req.interrupt      = False
        req.ani_path       = ""
        req.play_timestamp = 0

        # Retry loop — mirrors the AimDK example (remote peer + ROS quirks).
        response = None
        for _ in range(8):
            with suppress(Exception):
                req.header.stamp = node.get_clock().now().to_msg()
            future = client.call_async(req)
            done = threading.Event()
            future.add_done_callback(lambda _: done.set())
            if done.wait(0.25) and future.done():
                response = future.result()
                break
        if response is None:
            print("SetMcPresetMotion timed out", file=sys.stderr)
            return 2

        code  = int(response.response.header.code)
        state = int(response.response.state.value)
        if code == 0 or state in (CommonState.SUCCESS, CommonState.RUNNING):
            print(f"motion {args.motion}/{args.area} accepted")
            return 0
        print(f"motion rejected (code={code} state={state})", file=sys.stderr)
        return 2
    finally:
        executor.shutdown()
        with suppress(Exception):
            node.destroy_node()
        with suppress(Exception):
            rclpy.shutdown()


if __name__ == "__main__":
    sys.exit(main())
