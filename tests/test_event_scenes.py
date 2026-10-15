"""Exercise real scene storage and HTTP routes without loading the robot SDK."""
import ast
import hmac
import json
import logging
import threading
import time
from contextlib import suppress
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def test_scene_presets_and_playback(tmp_path):
    # Load only SDK-independent definitions; hardware calls are mocked below.
    source = Path(__file__).resolve().parents[1] / "cooper_panel_server.py"
    tree = ast.parse(source.read_text())
    definitions = [item for item in tree.body
                   if isinstance(item, (ast.ClassDef, ast.FunctionDef))
                   and item.name in ("PanelConfig", "make_handler")]
    namespace = dict(globals(), LOGGER=logging.getLogger("scene-test"),
                     ACTIONS={"wave": {"motion": 1002, "area": 2}})
    code = "from __future__ import annotations\n" + ast.unparse(
        ast.Module(body=definitions, type_ignores=[]))
    exec(compile(code, str(source), "exec"), namespace)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"event": {"message": "Existing intro"},
                                       "show_dance": "tai-chi"}))
    config = namespace["PanelConfig"](config_path)
    node = Mock(mic_geared=False)
    shows = Mock()
    shows.running.return_value = False
    events = Mock()
    events.running.return_value = False
    events.start.side_effect = lambda *args, **kwargs: {"launch_ms": 1}
    handler = namespace["make_handler"](
        node, shows, "test-pin", config, Mock(), Mock(), [0], events)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def request(path, body=None, pin="test-pin"):
        conn = HTTPConnection(*server.server_address, timeout=5)
        conn.request("GET" if body is None else "POST", path,
                     None if body is None else json.dumps(body),
                     {"X-Pin": pin, "Content-Type": "application/json"})
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    try:
        assert request("/api/event_scene1")[1]["message"] == "Existing intro"
        assert request("/api/event_scene2")[1]["message"] == ""
        assert request("/api/event_scene2", {})[0] == 400
        assert request("/api/event_config_scene2", {"message": "Answer"}, pin="bad")[0] == 401
        assert request("/api/event_config_scene2", {"opening": "unknown"})[0] == 400
        assert request("/api/event_config_scene2", {"message": "Prepared answer", "pause": 99})[0] == 200
        assert request("/api/event_config_scene1", {"opening": "wave", "message": "Hello"})[0] == 200
        # A fresh store proves both presets survive restart without losing other settings.
        restored = namespace["PanelConfig"](config_path)
        assert restored.get_event()["message"] == "Hello"
        assert restored.get_event("2")["message"] == "Prepared answer"
        assert restored.get_event("2")["pause"] == 30
        assert restored.get_show_dance() == "tai-chi"
        for path, message in [("/api/event_scene1", "Hello"),
                              ("/api/event_scene2", "Prepared answer"),
                              ("/api/event", "Hello")]:
            assert request(path, {})[0] == 200
            assert events.start.call_args.args[1] == message
        node.gear_up.assert_called_with(True)
        shows.running.return_value = True
        assert request("/api/event_scene2", {})[0] == 409
        shows.running.return_value = False
        events.running.return_value = True
        assert request("/api/event_scene1", {})[0] == 409
        events.running.return_value = False
        assert request("/api/event_config", {"message": "Legacy edit"})[0] == 200
        assert request("/api/event_scene1")[1]["message"] == "Legacy edit"
        assert request("/api/event_scene2")[1]["message"] == "Prepared answer"
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
