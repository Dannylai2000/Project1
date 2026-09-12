# Project Cooper — Deployment Guide

Two pieces run in the show suite. `optimus` (Ubuntu PC, 192.168.68.51)
hosts the **webpage** (Apache, port 8080) and — recommended — the
**control API** (`cooper_panel_server.py`, port 8081), which talks to
Cooper over ROS2/DDS. Nothing has to be installed on Cooper itself; the
robot only needs to be on the same network. Running the API on Cooper is
still supported as a fallback (see A.6).

## A. The control API — on optimus (recommended)

Running the API on optimus avoids Cooper's no-sudo/lingering hassles:
you have root there, and the API server is an ordinary ROS2 node that
reaches the robot over the network.

**Prerequisite**: optimus needs ROS 2 Humble and the AimDK workspace
(same two `source` lines used on the robot). Verify it can see Cooper's
services before installing:

```bash
source /opt/ros/humble/setup.bash && source ~/aimdk/install/setup.bash
ros2 service list | grep -i aimdk        # must list Cooper's services
```

If that lists nothing, the DDS link to the robot isn't up (check that
optimus and Cooper are on the same subnet and share the ROS_DOMAIN_ID) —
fix that first, or fall back to running the API on Cooper (A.6).

### 1. Get or update the code

On optimus:

```bash
git clone https://github.com/Dannylai2000/Project1.git ~/cooper   # first time
# or, if already deployed:
cd ~/cooper && git pull
```

### 2. Install the API as a service — port 8081 (Apache owns 8080)

```bash
cd ~/cooper
sudo ./deploy/install_cooper_service.sh --pin 2468 --port 8081                # on-demand
sudo ./deploy/install_cooper_service.sh --pin 2468 --port 8081 --idle-exit 0  # never auto-stops
sudo ./deploy/install_cooper_service.sh --pin 2468 --port 8081 --always-on    # runs from boot
```

The panel's default address is exactly this setup: the page's own host,
port 8081 — so a freshly opened panel connects with no typing.

### 3. Service modes

Replace `2468` with the real panel PIN. Modes:

| Mode | Behaviour |
|---|---|
| **On-demand** (default) | Nothing runs until a panel connects to the API port — opening the panel page starts the control API automatically in ~1–2 s. Stops itself after 30 min with no panels open (never while a panel is open or a show is running). The panel's red-bar **🔄 Restart server** button or its 3 s polling revives it instantly. |
| `--idle-exit 0` | Same on-demand start, but no auto-stop — recommended for event days. |
| `--always-on` | Starts at boot, restarts on crash, never stops. |

Switching modes later = re-run the script with different flags.

**No sudo (e.g. when running the API on Cooper instead — see A.6)?** Use
the no-root installer — same options, runs entirely under your own user
account:

```bash
./deploy/install_cooper_service_nosudo.sh --pin 2468                # on-demand
./deploy/install_cooper_service_nosudo.sh --pin 2468 --idle-exit 0  # never auto-stops
./deploy/install_cooper_service_nosudo.sh --pin 2468 --cron         # force cron fallback
```

It prefers **systemd user units** (`~/.config/systemd/user`, checked with
`systemctl --user status cooper-panel.socket`), keeping on-demand socket
activation. Start-at-boot without a login needs lingering — the script
tries to enable it and prints the one admin command
(`sudo loginctl enable-linger <user>`) if it can't; robots that
auto-login their user work without it. Where user systemd is unavailable
it falls back to a **cron @reboot** entry running
`deploy/run_cooper_panel.sh` (always-on, auto-restarts on crash, log in
`~/cooper-panel.log`). For a quick one-off start with no install at all:

```bash
nohup ./deploy/run_cooper_panel.sh >> ~/cooper-panel.log 2>&1 &
```

The PIN/port are kept in `deploy/.panel_env` (private to your user).

Status and logs:

```bash
systemctl status cooper-panel.socket    # on-demand mode
systemctl status cooper-panel.service   # always-on mode
journalctl -u cooper-panel -f           # live logs
```

The show script (`x2_showroom_demo.py`) is launched by the panel server — no
separate deployment. Runtime files (`cooper_panel_config.json` for the shared
shortlist, `cooper_show_timing.json` for diagnostics) are created next to the
server automatically.

### 4. First-run verification

Open the panel page (from optimus, or `cooper_control_panel.html` straight
from a folder) and check, in order:

1. The **server status bar turns green** ("… control API … Online"). Red
   with the wrong address showing? Fix it in ⚙ Settings — the API is the
   page's own host on port 8081 in the recommended setup.
2. In ⚙ Settings, the **song list loads with readable names** and you set
   the **Show dance**; the Performance card shows the choice.
3. The **Microphone On/Off radios** work — this exercises the `SetMute`
   service; if it errors, check the request field name:
   `ros2 interface show aimdk_msgs/srv/SetMute`. Then the **Speaker
   On/Muted radios and the volume slider** — these exercise `SetVolume`
   (muted = volume 0; On restores the last non-zero level, initially
   `--speaker-volume`, default 70). Note both stay disabled ("unknown")
   while the status bar is red — fix connectivity first.
4. Pick each **Action** in the dropdown and press ▶ Execute
   (handshake = preset motion 1003; left-hand wave uses area 1 — verify
   the left-arm area id). Gestures run via the standalone `x2_action.py`,
   which can also be tested by hand:
   `python3 x2_action.py --motion 1002 --area 2`.
5. **Run a full show** with Performance diagnostics on: the mic mutes, the
   timeline fills in (launch → first speech → dance → goodbye → complete),
   and the face plays the eye open/close emoji. A different expression means
   the blink id differs on this SDK build — set it with `--emoji-id N`
   (or change `DEFAULT_EMOJI_ID` in `x2_showroom_demo.py`).
6. Panel unreachable from *other* devices but fine locally? Open the
   firewall on the API host: `sudo ufw allow 8081` (or 8080 on Cooper).

### 5. Optional: secondary (backup) robot

To have a second X2 ready to continue the performance if the first one
fails, run a second control API pointed at that robot (its own host or
the robot itself), then on each panel device enter its address in
⚙ Settings → **Secondary robot IP address**. When the active robot's
status bar goes red, press **🤖 Use backup robot** on the bar (or use the
Active robot selector) to carry on with the other robot. Use the same
PIN so devices don't need a second code.

Note: the shortlist, play times, show dance, and custom actions are
stored next to each API server, so configure them once per API.

### 6. Fallback: running the API on Cooper instead

If optimus cannot reach Cooper's ROS2 services, install the API on the
robot exactly as in steps 1–3 but with the **no-sudo installer** and the
default port 8080, then set the panel's ⚙ Settings address to
192.168.68.54 port 8080.

## B. optimus — the webserver hosting the page

The panel webpage is hosted ONLY on the Ubuntu PC `optimus`
(192.168.68.51, Apache on port 8080), alongside the control API on
port 8081:

```bash
sudo cp cooper_control_panel.html /var/www/html/index.html
sudo cp cooper_icon.png /var/www/html/favicon.png    # optional tab icon
```

Staff browse `http://192.168.68.51:8080`; the panel talks directly to
Cooper's API at 192.168.68.54:8080 (the default address in ⚙ Settings).
No Apache modules or proxy configuration needed.

> Re-copy `cooper_control_panel.html` whenever panel updates are pulled —
> this is the one easy-to-forget step.

## C. Events — no webserver needed

Take a copy of `cooper_control_panel.html` on the notebook or tablet and
open it straight from a folder in the browser. In ⚙ Settings type
Cooper's event IP and press Connect; back at the show suite, change it
back to 192.168.68.54.

For events, install Cooper's service with `--idle-exit 0` so there is no idle
timer while waiting for the performance slot.

## D. Show-day checklist

1. Power Cooper on; wait for boot.
2. Open the panel → the status bar goes green by itself (on-demand start).
   Red bar? Press **🔄 Restart server**; still red → check Cooper's power and
   Wi-Fi.
3. Enter the PIN (once per device), curate the dance shortlist, set the
   tenant name and messages.
4. New songs pushed from LinkCraft show a ✨ banner — Review, tick the ones
   to use, then **Mark NEW as seen**.
5. Press **▶ Run full show**. The ❓ Help button in the panel covers
   everything else for staff.
