# Project Cooper — Deployment Guide

Two pieces, two machines: **Cooper** (the robot, 192.168.68.54) runs the
**control API** (`cooper_panel_server.py`, port 8080 — it must live where
ROS runs), and **optimus** (Ubuntu PC, 192.168.68.51) hosts only the
**webpage** (Apache, port 8080). The browser loads the page from optimus
and sends every command straight to Cooper's API.

## A. Cooper (the robot) — the control API

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

### 2. Install the API as a service (no sudo needed on Cooper)

```bash
cd ~/cooper
./deploy/install_cooper_service_nosudo.sh --pin 2468                # on-demand
./deploy/install_cooper_service_nosudo.sh --pin 2468 --idle-exit 0  # never auto-stops
./deploy/install_cooper_service_nosudo.sh --pin 2468 --cron         # force cron fallback
```

(With root available, `sudo ./deploy/install_cooper_service.sh` offers
the same modes as a system service.)

### 3. Service modes

Replace `2468` with the real panel PIN. Modes:

| Mode | Behaviour |
|---|---|
| **On-demand** (default) | Nothing runs until a panel connects to the API port — opening the panel page starts the control API automatically in ~1–2 s. Stops itself after 30 min with no panels open (never while a panel is open or a show is running). The panel's red-bar **🔄 Restart server** button or its 3 s polling revives it instantly. |
| `--idle-exit 0` | Same on-demand start, but no auto-stop — recommended for event days. |
| `--always-on` | Starts at boot, restarts on crash, never stops. |

Switching modes later = re-run the script with different flags.

The no-sudo installer prefers **systemd user units** (`~/.config/systemd/user`, checked with
`systemctl --user status cooper-panel.socket`), keeping on-demand socket
activation. Start-at-boot without a login needs lingering — the script
tries to enable it and prints the one admin command
(`sudo loginctl enable-linger <user>`) if it can't; robots that
auto-login their user work without it. **If `agi` has no sudo rights and
no admin is available** (the command answers "not allowed to execute"),
use `--cron` instead: cron entries run with no login and no admin needed
(always-on with auto-restart, log in `~/cooper-panel.log`); the
installer disables the systemd units first so the two don't fight over
the port. Cron mode installs **two entries**: `@reboot` (start at boot)
and an **every-minute watchdog** that revives the API within 60 s if it
is ever not running — this also covers systems that kill a user's
background processes at logout. The same cron fallback is used
automatically where user systemd is unavailable. For a quick one-off
start with no install at all:

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

### 3a. Show audio: the mic-switch sequence (on by default)

On some SDK builds, muting the assistant's microphone silences the
speaker too — the show would talk and dance with no sound. The show
therefore runs the vendor workaround by default, via
`/aimdk_5Fmsgs/srv/SetMicSourceRequest`: **mute → switch to the external
mic → unmute for the performance** (audio plays; the assistant hears
only the idle external mic) **→ switch back to the built-in mic →
re-mute at the end**. The request's source field is auto-detected
(AimDK stream ids: 1 = onboard, 2 = external); override with
`--mic-source-field` / `--mic-external` / `--mic-internal`, or disable
the whole workaround with `--mic-source-service ""`. Every switch
failure degrades gracefully to the plain mute behavior. To confirm the
diagnosis by hand: `python3 x2_showroom_demo.py --no-mute` — if that
show has sound, the mute is what silences the audio.

### Updating with no SSH (preferred)

In the panel: **🩺 Diagnose → ⬆ Update & restart** — the robot pulls
the latest code from GitHub and restarts its own API; the watchdog
(cron or systemd timer) revives it within seconds. **♻ Restart API**
does the restart alone. Re-copy the webpage to optimus separately when
the panel itself changed (the version tag turns amber if you forget).

### Restarting the API after an update (manual, over SSH)

**Cron mode** (Cooper's current setup — `systemctl` no longer manages it;
"Unit not loaded" from systemctl is normal here):

```bash
pkill -f cooper_panel_server.py    # wrapper restarts it within 5 s
```

If the API is fully down, the every-minute watchdog revives it by
itself; or re-run the installer with `--cron` to fix everything at once.

**Systemd on-demand mode:**

```bash
systemctl --user stop cooper-panel.service && systemctl --user restart cooper-panel.socket
```

Stopping only the socket leaves an already-running service alive
("Socket service already active, refusing"); always stop the service
too. If units ended up failed:
`systemctl --user reset-failed cooper-panel.service cooper-panel.socket`.

When the panel shows OFFLINE, press its **🩺 Diagnose** button first: it
works out whether Cooper is unreachable (power/Wi-Fi/wrong IP) or up with
the socket not armed (typical after a reboot without lingering), and
shows these commands ready to copy.

### 3b. After every update: check the version tag

The panel shows a version tag top-right (e.g. `v2026.09.12-1`), bumped
on every change; the API reports its own via `/api/status`. After
deploying, hard-reload the page (Ctrl-Shift-R / long-press reload on the
iPad) and check the tag:

- Shows the **new version** → the page deployed.
- Shows the **old version** → the copy to optimus didn't happen or the
  browser cached the page — re-copy and hard-reload.
- **Amber "page ≠ API"** → the two sides are on different builds:
  update whichever lags (`git pull` + restart the service on Cooper, or
  re-copy the HTML on optimus).

### 4. First-run verification

Open the panel page (from optimus, or `cooper_control_panel.html` straight
from a folder) and check, in order:

1. The **server status bar turns green** ("… control API … Online"). Red
   with the wrong address showing? Fix it in ⚙ Settings — Cooper's API is
   192.168.68.54 port 8080 in the show suite.
2. In ⚙ Settings, the **song list loads with readable names** and you set
   the **Show dance**; the Performance card shows the choice. If songs
   still show as IDs, the API logged one line
   (`journalctl --user -u cooper-panel.service | grep "LinkCraft resource fields"`)
   dumping the resource's fields — it reveals which field carries the
   real name on this SDK build.
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
6. Panel unreachable from *other* devices but fine on Cooper? Open the
   robot's firewall: `sudo ufw allow 8080` (needs an admin on Cooper).

### 5. Optional: secondary (backup) robot

To have a second X2 ready to continue the performance if the first one
fails, repeat steps 1–2 on the backup robot (same repo, same installer —
use the **same PIN** so devices don't need a second code). Then, on each
panel device, enter the backup robot's IP in ⚙ Settings →
**Secondary robot IP address**. When the active robot's status bar goes
red, press **🤖 Use backup robot** on the bar (or use the Active robot
selector) to carry on with the other robot.

Note: the shortlist, play times, show dance, and message groups are
stored on each robot, so configure them once per robot.

### 3c. Robot goes offline when you log out? The session keeper

If the panel is green only while someone is SSH'd into the robot, that
robot's user services stop at logout (no lingering, no auto-login, and
cron ignoring user jobs — all three normal boot paths closed). Fix it
from optimus, which is always on: hold one permanent SSH session to the
robot so its user services stay armed. On optimus:

```bash
cd ~/Project1                          # wherever this repo is on optimus
sudo ./deploy/install_session_keeper_on_optimus.sh 192.168.68.113 agi --password <robot pw>
```

The `--password` is used once to install a **dedicated, restricted
keeper key** on the robot (idle-session-only: it cannot run commands,
open a shell, or forward ports even if it leaks) and is never stored.

Install **one keeper per robot** — they coexist, and the standby robot
must stay armed too or failover would land on an offline robot. The
keeper is independent of the panel's Active-robot selector. When a
robot's IP changes:

```bash
sudo ./deploy/install_session_keeper_on_optimus.sh --remove <old-ip>
sudo ./deploy/install_session_keeper_on_optimus.sh <new-ip> agi --password <pw>
```

The keeper reconnects automatically after reboots (either machine) and
Wi-Fi drops, so the robot's API is armed whenever the robot is up —
this also makes the panel go green sooner after the robot boots. One
`sudo loginctl enable-linger agi` by an admin ON THE ROBOT makes the
keeper unnecessary, if that ever becomes available.

## B. Webservers — hosting the page (optimus + any backup machine)

A webserver's ONLY job here is serving one static file:
`cooper_control_panel.html`. The robots run the control APIs; the page
in the browser talks straight to a robot. No Apache modules, proxies,
PHP, or databases — any machine with Apache (or nginx, or even
`python3 -m http.server`) can host it. The show suite uses `optimus`
(192.168.68.51, Apache on 8080); a backup/event webserver (e.g. a
notebook) is set up the same way.

### B1. Setting up a webserver from scratch (after `apt install apache2`)

Steps for a fresh machine — optimus and the backup notebook alike:

**1. Get the repo** (also enables one-line updates later):

```bash
sudo apt-get install -y git        # if missing
cd ~
git clone https://github.com/Dannylai2000/Project1.git
```

No internet? Copy it from a machine that has it:
`scp -r showsuit@192.168.68.51:~/Project1 ~/Project1`

**2. (Optional) serve on port 8080 like optimus** — Apache defaults to
port 80. Matching optimus keeps every device's bookmark pattern the
same (`http://<server-ip>:8080`); skipping this step just means the
URL has no `:8080`:

```bash
sudo sed -i 's/^Listen 80$/Listen 8080/' /etc/apache2/ports.conf
sudo sed -i 's/<VirtualHost \*:80>/<VirtualHost *:8080>/' /etc/apache2/sites-available/000-default.conf
sudo systemctl restart apache2
```

**3. Install the page** (this replaces Apache's default "It works" page):

```bash
cd ~/Project1
sudo cp cooper_control_panel.html /var/www/html/index.html
sudo cp cooper_icon.png /var/www/html/favicon.png    # optional tab icon
```

**4. Test** from a phone/iPad on the same network:
`http://<server-ip>:8080` (or without `:8080` if step 2 was skipped) —
the panel should load, and after entering the robot IP + PIN in
⚙ Settings, go green.

**5. Updating the page later** — the one easy-to-forget step whenever
the panel changes (the amber "page ≠ API" version tag is the reminder):

```bash
cd ~/Project1 && git pull && sudo cp cooper_control_panel.html /var/www/html/index.html
```

Run it on EVERY webserver (optimus and the notebook) — each hosts its
own copy of the page.

### B2. Notes for the backup / event webserver

- **The page is identical everywhere** — panels served by optimus and
  by the notebook control the same robots, and everything that matters
  (shortlist, show dance, play times, messages, MIC state) lives ON
  the robots, so all devices see the same state regardless of which
  webserver served them the page.
- **Browser settings are per web address**: the browser stores the
  robot IPs, PIN, and Active-robot choice separately for
  `http://192.168.68.51:8080` and `http://<notebook-ip>:8080`. The
  first time a device opens the panel from the NEW webserver, enter
  the robot IP and PIN once in ⚙ Settings — they stick from then on.
- **Events without optimus**: the notebook can also take over the
  session-keeper duty so the robot's services stay armed at the venue.
  The same installer works on any Ubuntu machine with sudo:

  ```bash
  cd ~/Project1
  sudo ./deploy/install_session_keeper_on_optimus.sh <robot-event-ip> agi --password <pw>
  ```

  (Remove it after the event with `--remove <robot-event-ip>` if the
  robot's event IP won't be reused.) Also install the robot's service
  with `--idle-exit 0` for event days so there is no idle timer.
- **No webserver at all** still works: copy
  `cooper_control_panel.html` onto the tablet/notebook and open it
  straight from a folder in the browser; type the robot's event IP in
  ⚙ Settings.

## C. Uninstall / decommission a robot

Before returning or repurposing a robot, remove everything the panel
installed with one command (no sudo needed — everything is user-level):

```bash
cd ~/cooper && ./deploy/uninstall_cooper.sh
```

It stops the API and any running show, removes the cron entries and the
systemd user units (which contain the PIN), disables lingering, deletes
the logs, verifies nothing is left, and finally deletes `~/cooper`
itself — including the runtime files (`cooper_panel_config.json`,
`cooper_messages.json`, `deploy/.panel_env`). Use `--yes` to skip the
confirmation, `--keep-code` to clean up services but keep the code.

The panel never modifies the robot's system (it was installed without
sudo), so beyond this cleanup, a factory reset is the manufacturer's
own procedure. Remember also to remove the robot's IP from ⚙ Settings
on the panel devices, and to forget Wi-Fi / delete SSH keys per your
own handover checklist.

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
