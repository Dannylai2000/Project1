"""Read the robot's REAL audio state: GetAudioStatus, GetMute, GetMicSource.

The ros2 CLI cannot call GetAudioStatus on the show-suite build (the .idl
is missing from the overlay's share/ tree), but the generated Python
classes may still exist in the overlay's dist-packages — this probe finds
out, and if so prints the full responses. Run it twice: once while Cooper
converses normally, once while it is deaf (panel says in-built, native
app says external) — the field that differs is our truth-teller.

    python3 x2_probe_audio.py
"""

from __future__ import annotations

import sys
import threading
import time
from contextlib import suppress

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

import aimdk_msgs.srv as srv_mod

SERVICES = {
    "GetAudioStatus":      "/aimdk_5Fmsgs/srv/GetAudioStatus",
    "GetMute":             "/aimdk_5Fmsgs/srv/GetMute",
    "GetVolume":           "/aimdk_5Fmsgs/srv/GetVolume",
    "GetMicSourceRequest": "/aimdk_5Fmsgs/srv/GetMicSourceRequest",
}


def main() -> int:
    names = [n for n in dir(srv_mod) if not n.startswith("_")]
    interesting = [n for n in names if any(
        k in n for k in ("Audio", "Agent", "Mute", "Mic", "Focus", "Volume"))]
    print("Audio-related types in this overlay's PYTHON package:")
    print(" ", ", ".join(sorted(interesting)) or "(none)")

    rclpy.init()
    node = Node("x2_probe_audio")
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    threading.Thread(target=executor.spin, daemon=True).start()
    try:
        for name, service in SERVICES.items():
            print(f"===== {name} =====")
            srv_type = getattr(srv_mod, name, None)
            if srv_type is None:
                print("  not in the Python package — cannot call")
                continue
            client = node.create_client(srv_type, service)
            if not client.wait_for_service(timeout_sec=3.0):
                print(f"  service {service} not available")
                continue
            req = srv_type.Request()
            with suppress(Exception):
                req.header.header.stamp = node.get_clock().now().to_msg()
            with suppress(Exception):
                req.request.header.stamp = node.get_clock().now().to_msg()
            future = client.call_async(req)
            done = threading.Event()
            future.add_done_callback(lambda _: done.set())
            if done.wait(5.0) and future.done():
                print(" ", future.result())
            else:
                print("  no reply within 5 s")
            time.sleep(0.3)
        return 0
    finally:
        executor.shutdown()
        with suppress(Exception):
            node.destroy_node()
        with suppress(Exception):
            rclpy.shutdown()


if __name__ == "__main__":
    sys.exit(main())
