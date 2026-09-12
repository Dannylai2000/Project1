# Project Cooper — Deployment Guide

How to deploy and run everything on **Cooper** (the AgiBot X2 Ultra ambassador
robot at One Comcentre), plus the optional page hosts. Only Cooper runs code —
`optimus` and the portable notebook just host the webpage.

## A. Cooper (the robot)

### 1. Get or update the code

SSH into Cooper, then:

```bash
git clone https://github.com/Dannylai2000/Project1.git ~/cooper   # first time
# or, if already deployed:
cd ~/cooper && git pull
```

No internet on Cooper? Clone on a laptop and copy instead:

```bash
scp -r Project1 <user>@192.168.68.54:~/cooper
```

### 2. Optional: page icon

Drop a photo of Cooper in as `~/cooper/cooper_icon.png` — it becomes the
browser-tab icon automatically (a built-in Cooper icon is the fallback).

### 3. Install the webserver as a service — one command

```bash
cd ~/cooper
sudo ./deploy/install_cooper_service.sh --pin 2468                 # on-demand (default)
sudo ./deploy/install_cooper_service.sh --pin 2468 --idle-exit 0   # on-demand, never auto-stops
sudo ./deploy/install_cooper_service.sh --pin 2468 --always-on     # classic: runs from boot
```

Replace `2468` with the real panel PIN. Modes:

| Mode | Behaviour |
|---|---|
| **On-demand** (default) | Nothing runs until a panel connects to port 8080 — opening the panel page starts the control API automatically in ~1–2 s. Stops itself after 30 min with no panels open (never while a panel is open or a show is running). The panel's red-bar **🔄 Restart server** button or its 3 s polling revives it instantly. |
| `--idle-exit 0` | Same on-demand start, but no auto-stop — recommended for event days. |
| `--always-on` | Starts at boot, restarts on crash, never stops. |

Switching modes later = re-run the script with different flags.

**No sudo on Cooper?** Use the no-root installer instead — same options,
runs entirely under your own user account:

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

1. The **server status bar turns green** ("Cooper server … Online").
2. The **dance list loads with readable song names** — raw resource keys mean
   the display-name field needs mapping in `cooper_panel_server.py`.
3. The **Microphone On/Off radios** work — this exercises the `SetMute`
   service; if it errors, check the request field name:
   `ros2 interface show aimdk_msgs/srv/SetMute`. Then the **Speaker
   On/Muted radios and the volume slider** — these exercise `SetVolume`
   (muted = volume 0; On restores the last non-zero level, initially
   `--speaker-volume`, default 70).
4. Each **Action button** (🤝 🫶 👋 🖐️ 😘) performs the right gesture
   (handshake = preset motion 1003; left-hand wave uses area 1 — verify
   the left-arm area id).
5. **Run a full show** with Performance diagnostics on: the mic mutes, the
   timeline fills in (launch → first speech → dance → goodbye → complete),
   and the face plays the eye open/close emoji. A different expression means
   the blink id differs on this SDK build — set it with `--emoji-id N`
   (or change `DEFAULT_EMOJI_ID` in `x2_showroom_demo.py`).
6. Panel unreachable from *other* devices but fine on Cooper? Open the
   firewall: `sudo ufw allow 8080`.

### 5. Optional: secondary (backup) robot

To have a second X2 ready to continue the performance if the first one
fails, repeat steps 1–4 on the backup robot (same repo, same installer —
use the **same PIN** so devices don't need a second code). Then, on each
panel device, open ⚙ Settings and enter the backup robot's IP in
**Secondary robot IP address**. When the active robot's status bar goes
red, press **🤖 Use backup robot** on the bar (or use the Active robot
selector) to carry on with the other robot.

Note: the shortlist, per-dance play times, and new-song alerts are stored
on each robot, so configure them once per robot.

## B. optimus — the webserver hosting the page

The panel webpage is hosted ONLY on the Ubuntu PC `optimus`
(192.168.68.51, Apache on port 8080) — Cooper runs the control API, not
the page:

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
