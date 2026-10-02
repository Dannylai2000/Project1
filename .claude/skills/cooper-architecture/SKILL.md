---
name: cooper-architecture
description: System map of Project Cooper — what runs where (robots, optimus, BumbleBee), every file's role, the state stores, and the REST API surface. Load before changing any component or answering "how does X fit together".
---

# Cooper architecture

Diagram: `docs/architecture.svg`. Full API tables: `API.md`.

## Machines

| Machine | Role |
|---|---|
| Primary robot 192.168.68.115 | AgiBot X2 Ultra; runs the control API; repo at `~agi/cooper` |
| Secondary robot 192.168.68.113 | Identical stack, own config/keys |
| optimus 192.168.68.51 (Ubuntu PC, user `showsuit`) | Apache :8080 serving the static page ONLY; hosts the SSH session keepers |
| BumbleBee (HP EliteBook 830 G10) | Backup webserver, same Apache setup |

**Firm rule**: webservers are static — one HTML file, no code. The
browser talks straight to the robot's API (HTTP JSON + `X-Pin` header,
`/api/status` polled every 3 s). A robot must hold everything it needs
to perform locally.

## Repo files

| File | Role |
|---|---|
| `cooper_panel_server.py` | The robot-side API (stdlib HTTP + rclpy). `SERVER_VERSION` at top. Classes: `CooperPanelNode` (AimDK bridge, mic/mute/volume state + 30 s truth-sync watcher), `ShowRunner`, `EventRunner`, `PanelConfig`, `MessagesStore`, `LibraryWatcher`, `make_handler` |
| `cooper_control_panel.html` | The whole panel UI, one file. `PANEL_VERSION` in the single `<script>`. Cards: server bar, Audio (Hearing line, speaker, volume), Actions, Event, Performance (gear radios, show), Personalize Messages, Diagnose modal, Settings |
| `x2_showroom_demo.py` | Full show: greeting+wave → intro → dance → STAND_DEFAULT → thank-you+heart → goodbye |
| `x2_action.py` | One preset motion with automatic Stable-Stand retry |
| `x2_event.py` | Event: opening → message (TTS) → middle → closing; BT warm-up primer + lead silence; signed `--message-pause` |
| `x2_probe_area.py` / `x2_probe_emoji.py` / `x2_probe_audio.py` | Field probes: motion×area sweep, emoji sweep, audio-state readbacks |
| `deploy/install_cooper_service_nosudo.sh` | Robot install: systemd user socket + `cooper-watchdog.timer` (cron fallback); auto-detects the AimDK overlay; writes `deploy/.panel_env` |
| `deploy/run_cooper_panel.sh` | Cron-mode wrapper (NO `set -u` — ROS setup.bash breaks under it) |
| `deploy/install_session_keeper_on_optimus.sh` | Per-robot `ssh -N` keeper units with a restricted dedicated key; `--remove <ip>` |
| `deploy/uninstall_cooper.sh` | Full robot decommission |

## State stores (per robot, next to the server)

- `cooper_panel_config.json` — dance shortlist, per-song play times
  (999 = full song), show dance key, seen songs, **event** (opening /
  message / middle / closing / pause / bt_speaker).
- `cooper_messages.json` — message groups (guestName, greetAM/PM,
  introMsg, thankYouMsg, goodbyeMsg) + active group.
- Browser `localStorage` (per web ORIGIN — optimus and BumbleBee are
  separate): robot IPs, PIN, active robot, diag toggle, and the
  **mirror queue** of event/message copies pending for an offline robot.

The panel mirrors event and message saves to BOTH robots; failed copies
queue in the browser and retry every 30 s and on connect.

## API surface (detail in API.md)

GET: `/api/status` (version, listening, speaker, volume, mic_geared,
mic_source, show_running, event_running, library, show_timing),
`/api/health`, `/api/dances`, `/api/shortlist`, `/api/messages`,
`/api/event`, `/api/actions`.
POST (PIN): `/api/listening`, `/api/speaker`, `/api/volume`,
`/api/gear_up`, `/api/dance`, `/api/action`, `/api/show`,
`/api/event_config`, `/api/event` (auto-gears-up first),
`/api/shortlist`, `/api/messages`, `/api/songs_seen`, `/api/restart`,
`/api/update`.

## Self-healing layers

1. systemd user socket activation (`cooper-panel.socket`) + watchdog
   timer (reset-failed + re-arm, every 60 s); cron `@reboot` + anchored
   `pgrep` watchdog on legacy installs.
2. Session keepers on optimus: user services die at SSH logout
   (lingering refused), so optimus holds `ssh -N` sessions with a
   dedicated key restricted to `command="sleep infinity"`, one unit per
   robot IP.
3. Panel: 3 s polling revives socket-activated servers; dance-list
   auto-retry while AimDK boots; ♻ Restart / ⬆ Update buttons
   (`os._exit` + watchdog revival; `git pull --ff-only`).
