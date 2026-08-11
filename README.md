# Project1
Test

## AgiBot X2 — Showroom Demo (`x2_showroom_demo.py`)

Refined show sequence for the real robot (ROS2 / AimDK). Changes vs. the
original script:

- **Listening is turned off** at the start via the AimDK `SetMute` service
  (`/aimdk_5Fmsgs/srv/SetMute`), so the built-in assistant stops hearing and
  answering the audience during the show. The mic **stays muted after the
  show** by default; use `--unmute-after` to restore listening at the end,
  or `--no-mute` to skip mic control entirely.
- **Faster greeting**: the greeting speech starts immediately and the wave
  runs *during* it instead of before it.
- **No pause before the dance**: the LinkCraft dance resource is prefetched
  in a background thread during the greeting, so step 3 only sends the
  execute request.
- **Faster ending**: the thank-you speech overlaps the heart gesture, and
  the goodbye speech overlaps the blow kiss, with the wave right after.
- Post-speech grace reduced from 0.5s to 0.2s.

```bash
source /opt/ros/humble/setup.bash && source ~/aimdk/install/setup.bash
python3 x2_showroom_demo.py                 # muted show, stays muted
python3 x2_showroom_demo.py --unmute-after  # restore listening afterwards
python3 x2_showroom_demo.py --no-mute       # don't touch the mic
```

## AgiBot X2 — Listening Toggle (simulated)

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
