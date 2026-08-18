# Project Cooper
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

## Cooper Control Panel (`cooper_panel_server.py` + `cooper_control_panel.html`)

Browser control panel for Cooper. The server runs **on the robot** and
bridges HTTP to the AimDK ROS2 services; the webpage works from any phone,
tablet, or laptop on the same network.

Features:

- **Web server availability bar** — a status strip at the top of the main
  screen shows the selected setup, Cooper's address, and whether the
  server is **Online** (green) or **OFFLINE** (red). While offline all
  control buttons are disabled, and switching setups in ⚙ Settings
  re-checks the new address immediately.
- **❓ Help** — a button in the header opens a built-in user guide
  explaining the purpose and usage of every field: connection setups,
  IP/PIN, listening, show controls, message fields, the shortlist,
  new-song alerts, diagnostics, and troubleshooting. Works offline (the
  guide is part of the page).
- **Dance picker** — lists every LinkCraft resource live from Cooper's
  library, play any of them on demand, or run the **full show** with the
  selected dance.
- **Actions** — one-tap gesture buttons on the main screen: shake hand,
  heart sign (both hands), right-hand goodbye, and blow kiss (preset
  motions via `SetMcPresetMotion`). The registry lives in `ACTIONS` at
  the top of `cooper_panel_server.py`, so adding a gesture or fixing a
  motion ID is a one-line edit. Blocked while a show is running.
  ⚠️ Verify the shake-hand motion ID (1001) against the AimDK
  preset-motion table on the robot — the other three IDs come from the
  working show script.
- **Listening mode switch** — Listening ON / OFF buttons (mic mute via
  `SetMute`), with the current state shown in the panel.
- **Cooper IP address field** — in the ⚙ settings panel; defaults to the
  host serving the page and is saved in the browser (localStorage), so if
  Cooper's IP changes just type the new one and press Connect.
- **PIN protection** — all control actions (listening, dance, show)
  require a PIN when the server is started with one. The page asks for it
  in ⚙ settings and remembers it.
- **Live status** — the page polls Cooper every 3 seconds: connection dot,
  listening state, and show-in-progress (buttons lock while a show runs).
  The server also mirrors the show's mic behaviour, so the listening
  switch stays truthful when the show mutes/unmutes the robot.
- **Personalized messages** — the "Personalize messages" card holds a
  tenant/guest name, separate morning and afternoon welcome messages, the
  self-introduction spoken before the dance, and a goodbye message.
  Cooper picks AM vs PM by its own clock at show time,
  and `{name}` in any message is replaced with the entered name (blank =
  "everyone"). All texts are freely editable and saved in the browser;
  blank fields fall back to the script's built-in lines.
- **Dance shortlist** — ⚙ Settings lists every song in Cooper's LinkCraft
  library with checkboxes; ticked songs are the only ones shown in the
  main dance list (none ticked = show every song). The shortlist is
  stored **on Cooper** (`cooper_panel_config.json` next to the server),
  so every device shares the same list; changing it requires the PIN.
  Tick all / Clear all buttons included.
- **Performance diagnostics** — a toggle in ⚙ Settings. When on, every
  control action logs a timing breakdown in a diagnostics card: total
  (click → Cooper's acknowledgment), network (browser ↔ Cooper), server
  processing, and robot acknowledgment (the ROS service round-trip),
  color-coded green/amber/red. A flow diagram in the card shows the last
  measurement on each leg, and for the full show a live timeline tracks
  **launch → first speech → dance → complete** (in ms from the click),
  reported by the show script itself through a timing file
  (`cooper_show_timing.json`). The connection status also shows a live
  ping. Off by default so daily users never see it.

  Where the measured legs live:

  ```mermaid
  sequenceDiagram
      participant B as Browser (panel page)
      participant W as Web server on Cooper<br>(cooper_panel_server.py)
      participant R as Robot services (AimDK ROS2)
      participant S as Show script<br>(x2_showroom_demo.py)

      B->>W: POST /api/listening | /api/dance | /api/show
      Note over B,W: network (browser round-trip minus server time)
      Note over W: server processing (service wait + lookup)
      W->>R: ROS service call (SetMute / ExecuteActionResource)
      Note over W,R: robot ack — Cooper's controller accepts the command
      R-->>W: acknowledgment
      W-->>B: JSON result + timing breakdown

      W->>S: /api/show only — launch show subprocess (launch ms)
      S->>R: PlayTts greeting
      Note over S,R: first speech — greeting audio accepted
      S->>R: ExecuteActionResource dance
      Note over S,R: dance started
      S-->>W: milestones via cooper_show_timing.json
      W-->>B: /api/status → show_timing (ms from click)
  ```
- **New-song alerts** — Cooper's server re-checks the LinkCraft library in
  the background (every 5 min, `--library-poll` to change) and compares it
  with a shared "seen" list. When the LinkCraft cloud pushes new songs,
  every panel shows a "✨ N new dance songs" banner with a Review button;
  in the shortlist the new songs are sorted to the top with a NEW badge.
  "Mark NEW as seen" (PIN) clears the alert for all devices at once.

The control API (`cooper_panel_server.py`) always runs on Cooper:

```bash
source /opt/ros/humble/setup.bash && source ~/aimdk/install/setup.bash
python3 cooper_panel_server.py --port 8080 --pin 2468
```

The **webpage** can be hosted three ways, and the ⚙ Settings dropdown
("Where is Cooper being used?") switches between them with one selection:

| Profile | Page hosted by | Cooper address |
|---|---|---|
| Show suite — One Comcentre | Apache on `optimus` (192.168.68.51:8080) | fixed 192.168.68.54:8080 |
| Cooper's built-in webserver | `cooper_panel_server.py` itself | automatic (the serving host) |
| Outside event — portable | notebook Apache / opened from file | typed once, remembered |

For the Apache hosts (optimus or the portable notebook), just copy
`cooper_control_panel.html` (renamed `index.html` if you like) and
optionally `cooper_icon.png` into the web root, e.g.
`/var/www/html/` on Ubuntu. The page talks to Cooper's API directly from
the browser, so Apache needs no extra modules or proxy setup.

REST API (used by the page, also handy for scripting):

| Endpoint | Method | Body | Purpose |
|---|---|---|---|
| `/api/status` | GET | — | server + listening state |
| `/api/dances` | GET | — | LinkCraft dance list |
| `/api/dance` | POST | `{"key": "..."}` | play one dance |
| `/api/listening` | POST | `{"listen": true\|false}` | mic on/off |
| `/api/show` | POST | `{"dance_key": "...", "unmute_after": false}` | run the full show |

The show script also accepts the dance directly:

```bash
python3 x2_showroom_demo.py --dance-key linkcraft_resource_onnx_... --dance-duration 30
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
