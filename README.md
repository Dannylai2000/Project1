# Project1
Test

## AgiBot X2 — Listening Toggle

The AgiBot X2's microphones stay open by default so it can respond to the
audience. The `agibot_x2` package adds an on/off switch for that listening,
so audio is only captured when you allow it.

### Usage (CLI)

```bash
python toggle_listening.py off      # robot stops capturing audio
python toggle_listening.py on      # robot resumes listening
python toggle_listening.py toggle  # flip the current state
python toggle_listening.py status  # show the current state
```

### Usage (Python)

```python
from agibot_x2 import ListeningController

controller = ListeningController()
controller.stop_listening()   # mute the robot
controller.start_listening()  # resume listening
controller.toggle()           # flip state; returns the new state
print(controller.is_listening)
```

### Connecting to the real robot

The controller talks to hardware via the `MicrophoneBackend` interface
(`agibot_x2/listening.py`). Subclass it and implement `start_capture`,
`stop_capture`, and `is_capturing` using the AgiBot X2 audio SDK (ROS2
audio topic, ALSA device, or vendor API), then pass your backend to
`ListeningController(backend=...)`. Without a backend a simulated
microphone is used, which is handy for development and tests.

Notes:

- The state persists to `~/.agibot_x2/listening_state.json`, so a muted
  robot stays muted across restarts.
- If the state file is missing or unreadable, the controller defaults to
  **not listening** (fails private, not open).

### Tests

```bash
python -m pytest tests/
```
