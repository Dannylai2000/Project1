"""Probe Cooper's face emojis (PlayEmoji) one ID at a time.

Motions report acceptance, but an emoji's only real proof is the face
display — so this steps through IDs with a pause between them while you
WATCH COOPER'S FACE, and prints whatever the service replies for each.

First see what the SDK defines (names live in the enum, if there is one):

    ros2 interface list | grep -i emoji
    ros2 interface show aimdk_msgs/srv/PlayEmoji

Then probe, noting which IDs visibly change the face:

    python3 x2_probe_emoji.py --ids 1-30
    python3 x2_probe_emoji.py --ids 1,2,5,7 --gap 6

Request field names vary between SDK builds (emoji_id / id, optional
loop), so every known spelling is set — same trick the show script uses.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from contextlib import suppress

import rclpy
from aimdk_msgs.srv import PlayEmoji
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

DEFAULT_PLAY_EMOJI_SVC = "/aimdk_5Fmsgs/srv/PlayEmoji"


def parse_ids(spec: str) -> list[int]:
    """"1-5,8,12-14" → [1, 2, 3, 4, 5, 8, 12, 13, 14]."""
    ids: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            ids.extend(range(int(lo), int(hi) + 1))
        else:
            ids.append(int(part))
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Step through PlayEmoji IDs while you watch the face"
    )
    parser.add_argument("--ids",
                        default="1,10,11,20,30,31,32,33,40,50,60,70,80,90,"
                                "100,101,110,120,130,140,150,160,170,180,"
                                "190,200,210,220",
                        help="IDs to try: ranges and lists, e.g. 1-30 or "
                             "1,2,7 (default = every ID in the PlayEmoji "
                             "emotion enum)")
    parser.add_argument("--service", default=DEFAULT_PLAY_EMOJI_SVC)
    parser.add_argument("--gap", type=float, default=4.0,
                        help="seconds to watch the face between IDs")
    parser.add_argument("--loop", action="store_true",
                        help="set the request's loop flag (the show uses it "
                             "for the blink; off here so each plays once)")
    parser.add_argument("--wait", type=float, default=5.0,
                        help="seconds to wait for the service")
    args = parser.parse_args()
    ids = parse_ids(args.ids)

    rclpy.init()
    node = Node("x2_probe_emoji")
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    threading.Thread(target=executor.spin, daemon=True).start()
    try:
        client = node.create_client(PlayEmoji, args.service)
        if not client.wait_for_service(timeout_sec=args.wait):
            print(f"PlayEmoji service not available at {args.service}",
                  file=sys.stderr)
            return 1
        print(f"Probing {len(ids)} emoji ID(s) — WATCH COOPER'S FACE and "
              "note which IDs change it.")
        for emoji_id in ids:
            req = PlayEmoji.Request()
            with suppress(Exception):
                req.header.stamp = node.get_clock().now().to_msg()
            for holder in (req, getattr(req, "emoji_req", None)):
                if holder is None:
                    continue
                # This SDK build uses emotion_id + mode (1 once / 2 loop);
                # older spellings kept as fallback.
                for field in ("emotion_id", "emoji_id", "id"):
                    if hasattr(holder, field):
                        setattr(holder, field, int(emoji_id))
                if hasattr(holder, "mode"):
                    holder.mode = 2 if args.loop else 1
                if hasattr(holder, "loop"):
                    holder.loop = bool(args.loop)

            future = client.call_async(req)
            done = threading.Event()
            future.add_done_callback(lambda _: done.set())
            if done.wait(3.0) and future.done():
                reply = str(future.result()).replace("\n", " ")
                print(f"emoji {emoji_id}: reply {reply[:160]}")
            else:
                print(f"emoji {emoji_id}: no reply within 3 s")
            time.sleep(args.gap)
        print("Done. The working list = the IDs that visibly changed the "
              "face (a polite reply for an unknown ID is common).")
        return 0
    finally:
        executor.shutdown()
        with suppress(Exception):
            node.destroy_node()
        with suppress(Exception):
            rclpy.shutdown()


if __name__ == "__main__":
    sys.exit(main())
