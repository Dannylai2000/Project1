"""Check event persistence and sequence without requiring the robot's ROS SDK."""
import argparse
import ast
import json
import logging
from pathlib import Path
import sys
import tempfile
import threading
from contextlib import suppress
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load_parts(filename, names, namespace):
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
    tree.body = [n for n in tree.body if getattr(n, "name", None) in names
                 or isinstance(n, ast.ImportFrom) and n.module == "__future__"
                 or isinstance(n, ast.Assign) and isinstance(n.value, (ast.Constant, ast.UnaryOp))]
    exec(compile(tree, filename, "exec"), namespace)
    return namespace


class EventDanceTests(unittest.TestCase):
    def test_config_roundtrip_and_old_config(self):
        ns = load_parts("cooper_panel_server.py", {"PanelConfig"}, {
            "Path": Path, "threading": threading, "json": json,
            "LOGGER": logging.getLogger(__name__),
        })
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            config = ns["PanelConfig"](path)
            self.assertEqual(config.get_event()["dance_key"], "")
            config.set_event("", "Hello", "", "", dance_key="  song_onnx  ")
            self.assertEqual(config.get_event()["dance_key"], "song_onnx")
            config.set_event("", "Hello", "", "", dance_key="")
            self.assertEqual(config.get_event()["dance_key"], "")

    def test_dance_precedes_stand_and_middle_and_failure_stops_gestures(self):
        for succeeds in (True, False):
            with self.subTest(succeeds=succeeds):
                calls = []
                node = Mock()
                ns = load_parts("x2_event.py", {"main"}, {
                    "argparse": argparse, "sys": sys, "threading": threading,
                    "suppress": suppress, "rclpy": Mock(), "Node": Mock(return_value=node),
                    "MultiThreadedExecutor": Mock(), "SetMcPresetMotion": Mock(),
                    "DEFAULT_PRESET_MOTION_SVC": "motion", "DEFAULT_SET_MC_ACTION_SVC": "stand",
                    "time": SimpleNamespace(sleep=lambda _: None, monotonic=lambda: 0),
                    "run_dance": lambda *args: calls.append("dance") or succeeds,
                    "stand_default": lambda *args: calls.append("stand") or True,
                    "run_gesture": lambda *args: calls.append("middle") or True,
                })
                with patch.object(sys, "argv", ["event", "--dance-key", "song",
                                                "--middle-motion", "1002", "--middle-area", "2"]):
                    code = ns["main"]()
                self.assertEqual(calls, ["dance", "stand", "middle"] if succeeds else ["dance"])
                self.assertEqual(code, 0 if succeeds else 2)


if __name__ == "__main__":
    unittest.main()
