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
| **On-demand** (default) | Nothing runs until a panel connects to port 8080 — selecting a setup in the panel starts the webserver automatically in ~1–2 s. Stops itself after 30 min with no panels open (never while a panel is open or a show is running). The panel's red-bar **🔄 Restart server** button or its 3 s polling revives it instantly. |
| `--idle-exit 0` | Same on-demand start, but no auto-stop — recommended for event days. |
| `--always-on` | Starts at boot, restarts on crash, never stops. |

Switching modes later = re-run the script with different flags.

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

Browse to `http://192.168.68.54:8080` from any device and check, in order:

1. The **server status bar turns green** ("Cooper server … Online").
2. The **dance list loads with readable song names** — raw resource keys mean
   the display-name field needs mapping in `cooper_panel_server.py`.
3. The **listening toggle** works — this exercises the `SetMute` service. If
   it errors, check the request field name:
   `ros2 interface show aimdk_msgs/srv/SetMute`.
4. Each **Action button** (🤝 🫶 👋 😘) performs the right gesture
   (handshake = preset motion 1003).
5. **Run a full show** with Performance diagnostics on: the mic mutes, the
   timeline fills in (launch → first speech → dance → goodbye → complete),
   and the face plays the eye open/close emoji. A different expression means
   the blink id differs on this SDK build — set it with `--emoji-id N`
   (or change `DEFAULT_EMOJI_ID` in `x2_showroom_demo.py`).
6. Panel unreachable from *other* devices but fine on Cooper? Open the
   firewall: `sudo ufw allow 8080`.

## B. optimus — show-suite Apache (optional page host)

On the Ubuntu PC `optimus` (192.168.68.51, Apache on port 8080):

```bash
sudo cp cooper_control_panel.html /var/www/html/index.html
sudo cp cooper_icon.png /var/www/html/    # optional
```

Staff browse `http://192.168.68.51:8080`; the panel defaults to the
**Show suite** profile and talks directly to Cooper at 192.168.68.54:8080.
No Apache modules or proxy configuration needed.

> Re-copy `cooper_control_panel.html` whenever panel updates are pulled —
> this is the one easy-to-forget step.

## C. Portable notebook — events (optional)

Same two-file copy into its Apache web root, or skip Apache entirely and open
`cooper_control_panel.html` straight from a folder. In ⚙ Settings pick
**"Outside event — portable"** and type Cooper's event IP once — it is
remembered separately from the show-suite settings.

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
