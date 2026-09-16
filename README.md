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
- **Ending**: after the dance, Stable Stand (required before preset
  motions), then the both-hands heart playing WHILE the thank-you and
  goodbye are spoken.
- **Skip the dance** — a checkbox in the Performance card runs a
  talk-and-gestures-only show (wave + welcome, introduction,
  heart + thank-you, goodbye): no dance, no song configuration needed,
  no Stable-Stand wait.
- **Face emoji**: the eye open/close (blink) expression is played on
  Cooper's face at each show phase — welcome, dance, and closing — via
  the AimDK `PlayEmoji` service. Fire-and-forget: a missing service only
  logs a warning. `--emoji-id N` selects the expression (check the AimDK
  emoji table for the blink id on your SDK build); `--emoji-id -1`
  disables it.
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
- **Architecture & API reference** — see **`API.md`** for the full
  picture: how the iPad talks to the page on optimus, the page to the
  robot's API middleware, and the middleware to the AgiBot AimDK
  (ROS2) SDK, plus every REST endpoint with request/response shapes.
- **❓ Help** — a button in the header opens a built-in user guide
  explaining the purpose and usage of every field: connection setups,
  IP/PIN, listening, show controls, message fields, the shortlist,
  new-song alerts, diagnostics, and troubleshooting. Works offline (the
  guide is part of the page).
- **Dance picker** — lists every LinkCraft resource live from Cooper's
  library, play any of them on demand, or run the **full show** with the
  selected dance.
- **Actions** — a dropdown of gestures plus a **▶ Execute** button:
  shake hand, heart sign (both hands), right-hand goodbye, left-hand
  wave, and blow kiss. Each
  execution runs the standalone **`x2_action.py`** program (also usable
  by hand: `python3 x2_action.py --motion 1002 --area 2`). Blocked while
  a show runs. IDs follow the AimDK preset-motion table (1001 raise,
  1002 wave, 1003 handshake, 1004 airkiss). ⚠️ Left-hand wave uses
  area 1 — verify the left-arm area id on the robot (right arm is 2).
- **Show dance** — chosen in ⚙ Settings (config `show_dance`, stored on
  the connected robot); ▶ Run full show and Play dance only use it, and
  the Performance card displays the current choice. Settings also shows
  the selected **LinkCraft ID** — every robot's LinkCraft library has
  different IDs, so pick the show dance once per robot (primary and
  secondary). A robot with no dance configured refuses to start the
  show with a clear message instead of trying another robot's ID.
- **Secondary (backup) robot** — ⚙ Settings holds a second X2's IP
  address and an Active robot selector; if the active robot fails, the
  red status bar offers "🤖 Use backup robot" to continue the
  performance on the other one. Both robots run the same panel server;
  each keeps its own shortlist/play-time config.
- **Per-dance play time** — the seconds box beside each song in the
  shortlist sets how long the full show waits during that dance: type
  the song's length so it plays in full (blank = 33 s, 0 = don't
  wait). 999 = full song via the measured audio length — works only on
  builds where the resource's audio file is reachable on disk; on the
  current robots the proxy keeps it in a private container, so type
  the length instead. Stored on the robot
  (`dance_times` in `cooper_panel_config.json`), shared by all devices.
- **Audio controls** — Microphone On / Off radios (mic mute via
  `SetMute`), Speaker On / Muted radios, and a **volume slider** (0–100,
  via `SetVolume`). Muted = volume 0; "Speaker On" restores the last
  non-zero level set (initially `--speaker-volume`, default 70). The
  speaker and volume stay usable during a show for silent rehearsals.
- **Cooper IP address field** — in ⚙ Settings; the control API (ROS)
  runs on Cooper, so the default is 192.168.68.54 port 8080 (the page
  itself is hosted on optimus). Saved in the browser, and an invalid
  saved address is discarded automatically. At an event, type Cooper's
  IP on that network.
- **PIN protection** — all control actions (listening, dance, show)
  require a PIN when the server is started with one. The page asks for it
  in ⚙ settings and remembers it.
- **🎭 Gear up for performance** — a MIC-mode radio in the Performance
  card: gearing up sets the speaker to 0, switches to the external MIC,
  waits 5 s, then restores the volume to 70% — Cooper is then ready to
  perform with sound while ignoring the audience, and Run full show
  skips its own MIC switching. "Normal" reverses it. The mode is stored
  on the robot, so every panel shows the true state; an un-geared show
  still switches MICs automatically as a fallback.
- **One-command uninstall** — `./deploy/uninstall_cooper.sh` removes
  everything from a robot before returning or repurposing it:
  processes, cron entries, systemd user units (incl. the PIN),
  lingering, logs, and the code directory itself, with a final
  verification. See DEPLOYMENT.md section D.
- **Self-healing & no-SSH maintenance** — each robot heals itself: a
  cron watchdog (cron installs) or a systemd user timer (socket
  installs) checks every minute, clears failed states, and revives the
  API after crashes, logouts, or crash-loop lockouts. Routine
  operations moved into the panel's 🩺 Diagnose modal: **♻ Restart
  API** and **⬆ Update & restart** (the robot runs `git pull` on
  itself and restarts — deploys without anyone SSHing in). Both are
  PIN-protected and refused while a show is running.
- **🩺 Diagnose button** — one tap on the server status bar. API online:
  runs a health check on Cooper itself (AimDK services, start-after-reboot
  persistence, program files, PIN) with a plain-language fix for anything
  amber/red. API offline: tells apart "Cooper unreachable (power/Wi-Fi/IP)"
  from "Cooper up but the API socket not armed (reboot without lingering)"
  and shows the exact SSH commands to fix it, with a copy button.
- **Live status** — the page polls Cooper every 3 seconds: connection dot,
  listening state, and show-in-progress (buttons lock while a show runs).
  The server also mirrors the show's mic behaviour, so the listening
  switch stays truthful when the show mutes/unmutes the robot.
- **Personalized messages** — the "Personalize messages" card holds a
  tenant/guest name, separate morning and afternoon welcome messages, the
  self-introduction, a thank-you (after the dance), and a goodbye —
  organized into named **message groups** (e.g. "Show suite", "Event"),
  each a full independent set. Users create/rename/delete groups; the
  active group's messages are the ones the show speaks, so switching
  occasion is one dropdown selection. **Groups are stored on the
  connected robot** (`cooper_messages.json`, one set per robot), so every
  device sees and edits the same texts, and the show always speaks the
  robot's stored active group no matter which device pressed the button
  (a browser's old local copy is migrated to the robot automatically).
  Cooper picks AM vs PM by its own clock at show time,
  and `{name}` in any message is replaced with the entered name (blank =
  "everyone"). **All fields are blank by default: blank = Cooper speaks
  its built-in message from `x2_showroom_demo.py` on the robot** — the
  single source of truth for default texts. Type in a field only to
  override that one message for the group.
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

### Install as a service (one command, two modes)

```bash
sudo ./deploy/install_cooper_service.sh --pin 2468               # on-demand
sudo ./deploy/install_cooper_service.sh --pin 2468 --always-on   # at boot
```

- **On-demand** (default, systemd socket activation): the webserver is
  NOT running until something connects to port 8080 — opening the panel
  page starts it automatically within a second or two. After 30 minutes without
  requests (`--idle-exit` to change) it stops itself; the next
  connection starts it again — the panel's red OFFLINE bar has a
  **🔄 Restart server** button for exactly this, and its 3 s polling
  also revives the server on its own. An open panel counts as activity,
  so the server never stops while anyone is using it. For events where
  no auto-stop is wanted at all, install with `--idle-exit 0`:
  starts on first use, then runs until Cooper shuts down.
- **Always-on**: starts at boot and stays up (`Restart=on-failure`).

The server also supports these directly: `--idle-exit N` (minutes,
0 = run forever) and systemd's `LISTEN_FDS` socket activation.

The **webpage** is hosted on `optimus` (Apache at 192.168.68.51:8080) —
Cooper serves the control API only. Copy `cooper_control_panel.html`
(renamed `index.html` if you like) and optionally the icon as
`favicon.png` into `/var/www/html/`. The page talks to Cooper's API
directly from the browser, so Apache needs no extra modules or proxy
setup. At an outside event, open the same HTML file straight from a
folder — no webserver needed — and type Cooper's event IP in ⚙ Settings.

REST API (used by the page, also handy for scripting):

| Endpoint | Method | Body | Purpose |
|---|---|---|---|
| `/api/status` | GET | — | server + mic/speaker/show state |
| `/api/dances` | GET | — | LinkCraft dance list |
| `/api/dance` | POST | `{"key": "..."}` | play one dance |
| `/api/listening` | POST | `{"listen": true\|false}` | mic on/off |
| `/api/speaker` | POST | `{"on": true\|false}` | speaker on/muted |
| `/api/volume` | POST | `{"level": 0-100}` | speaker volume |
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
