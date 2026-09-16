"""Run one AgiBot X2 preset motion (gesture) and exit.

Standalone companion to the Cooper Control Panel: the panel server invokes
this script for each Action executed from the panel, so gesture execution
lives in its own Python program and can also be tested by hand:

    python3 x2_action.py --motion 1002 --area 2      # right-hand wave

If the controller rejects the motion (it stays in dance mode after a
LinkCraft dance and refuses preset motions), the script automatically
switches the robot to Stable Stand (SetMcAction STAND_DEFAULT), waits for
it to settle, and retries once.

Exit codes: 0 = accepted by the robot, 1 = service unavailable / bad args,
2 = rejected or timed out (even after the Stable-Stand retry).
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from contextlib import suppress

import rclpy
from aimdk_msgs.msg import (CommonState, McActionCommand, McControlArea,
                            McPresetMotion, RequestHeader)
from aimdk_msgs.srv import SetMcAction, SetMcPresetMotion
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

DEFAULT_PRESET_MOTION_SVC = "/aimdk_5Fmsgs/srv/SetMcPresetMotion"
DEFAULT_SET_MC_ACTION_SVC = "/aimdk_5Fmsgs/srv/SetMcAction"


def _call(node: Node, client, req, tries: int = 8):
    """Call with the AimDK example's retry loop; the response or None."""
    for _ in range(tries):
        with suppress(Exception):
            req.header.stamp = node.get_clock().now().to_msg()
        future = client.call_async(req)
        done = threading.Event()
        future.add_done_callback(lambda _: done.set())
        if done.wait(0.25) and future.done():
            return future.result()
    return None


def send_motion(node: Node, client, motion: int, area: int):
    """Send the preset motion; (code, state) or (None, None) on timeout."""
    req = SetMcPresetMotion.Request()
    req.header         = RequestHeader()
    req.motion         = McPresetMotion()
    req.area           = McControlArea()
    req.motion.value   = int(motion)
    req.area.value     = int(area)
    req.interrupt      = False
    req.ani_path       = ""
    req.play_timestamp = 0
    response = _call(node, client, req)
    if response is None:
        return None, None
    return (int(response.response.header.code),
            int(response.response.state.value))


def accepted(code, state) -> bool:
    return code is not None and (
        code == 0 or state in (CommonState.SUCCESS, CommonState.RUNNING))


def stand_default(node: Node, service: str, settle_s: float) -> bool:
    """Switch to Stable Stand so preset motions are accepted again."""
    client = node.create_client(SetMcAction, service)
    if not client.wait_for_service(timeout_sec=5.0):
        print("SetMcAction service not available — cannot switch to "
              "Stable Stand", file=sys.stderr)
        return False
    req = SetMcAction.Request()
    req.header = RequestHeader()
    req.source = "node"
    cmd = McActionCommand()
    cmd.action_desc = "STAND_DEFAULT"
    req.command = cmd
    response = _call(node, client, req)
    if response is None:
        print("SetMcAction (STAND_DEFAULT) timed out", file=sys.stderr)
        return False
    state = int(response.response.status.value)
    if state not in (CommonState.SUCCESS, CommonState.RUNNING):
        print(f"STAND_DEFAULT rejected (state={state})", file=sys.stderr)
        return False
    print(f"Stable Stand accepted — settling {settle_s:.0f}s before retry")
    time.sleep(settle_s)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one AgiBot X2 preset motion (gesture) and exit"
    )
    parser.add_argument("--motion", type=int, required=True, help="preset motion id")
    parser.add_argument("--area", type=int, required=True, help="control area id")
    parser.add_argument("--service", default=DEFAULT_PRESET_MOTION_SVC)
    parser.add_argument("--mc-action-service", default=DEFAULT_SET_MC_ACTION_SVC)
    parser.add_argument("--stand-settle", type=float, default=8.0,
                        help="seconds Stable Stand needs before the retry")
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

        code, state = send_motion(node, client, args.motion, args.area)
        if accepted(code, state):
            print(f"motion {args.motion}/{args.area} accepted")
            return 0
        if code is None:
            print("SetMcPresetMotion timed out", file=sys.stderr)
            return 2

        # Rejected — usual cause: the controller is still in dance mode
        # after a LinkCraft dance. Go to Stable Stand and try once more.
        print(f"motion rejected (code={code} state={state}) — switching to "
              "Stable Stand and retrying")
        if stand_default(node, args.mc_action_service, args.stand_settle):
            code, state = send_motion(node, client, args.motion, args.area)
            if accepted(code, state):
                print(f"motion {args.motion}/{args.area} accepted after "
                      "Stable Stand")
                return 0
        print(f"motion rejected (code={code} state={state}) even after "
              "Stable Stand — is the robot standing and out of the native "
              "app's manual control?", file=sys.stderr)
        return 2
    finally:
        executor.shutdown()
        with suppress(Exception):
            node.destroy_node()
        with suppress(Exception):
            rclpy.shutdown()


if __name__ == "__main__":
    sys.exit(main())
