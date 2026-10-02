---
name: cooper-troubleshoot
description: Runbook for Project Cooper field problems — panel offline, robot deaf after a show, silent TTS, boot persistence, gesture rejections, version mismatches. Maps each symptom to its known cause and the exact commands/probes to confirm. Load when diagnosing any operator report.
---

# Cooper troubleshooting runbook

First stop is always the panel's 🩺 **Diagnose** (11 live checks) and
the version chip (amber = page/API mismatch → deploy the lagging side).
On the robot, the journal is the source of truth:

```bash
journalctl --user -u cooper-panel.service --since "30 min ago" --no-pager | grep -viE "RTPS|DDS|SubscriberImpl|PublisherImpl|DataReader"
```

Shell prerequisite for ros2/python on a robot:
`source /opt/ros/humble/setup.bash && source <overlay>` — overlay path
is in `grep COOPER_AIMDK_SETUP ~/cooper/deploy/.panel_env`
(`~/aimdk.bak~/...` on robots whose update renamed the workspace).
"Unknown package 'aimdk_msgs'" = unsourced shell, nothing more.

## Panel shows robot OFFLINE

1. `curl -s http://127.0.0.1:8080/api/status | head -c 200` on the robot.
2. Empty with socket `active (listening)` → the service dies on start:
   `journalctl --user -u cooper-panel.service -n 30`. Classic causes:
   `ModuleNotFoundError: aimdk_msgs` (AimDK overlay moved — re-run the
   installer, it auto-detects; check `.panel_env`), or port held by an
   orphan (`pgrep -af cooper_panel_server`, `kill -9` if SIGTERM ignored).
3. "Unit not loaded" from systemctl → units not installed on this robot
   (or cron-mode robot): `ls ~/.config/systemd/user/`, `crontab -l`.
   Reinstall: `cd ~/cooper && ./deploy/install_cooper_service_nosudo.sh
   --pin <pin>`.
4. Robot goes offline when SSH logs out → session keeper missing for
   this IP: on optimus `systemctl status cooper-session-keeper-*` and
   install per DEPLOYMENT §3c. (User services die at logout; lingering
   is refused on these robots.)
5. Only one webserver's page fails → per-origin localStorage: enter the
   robot IPs + PIN once in ⚙ Settings on that origin.

## Robot deaf (won't converse) after a performance

Historic root cause is FIXED (built-in mic is stream 0, not 1) — if it
recurs:
1. Panel Hearing line should read "listening"; MIC mode line
   "normal · robot: in-built MIC" (⚠ = setting/gear mismatch).
2. Check the native iPad app's microphone screen — it shows ACTUAL
   routing; `GetMicSourceRequest` only echoes the setting.
3. `python3 x2_probe_audio.py` healthy vs deaf: `mic_source` must be 0
   when conversing. 1 or 2 while "normal" → the toggle lost the race:
   journal for `Mic-source switch attempt` / `verified` lines.
4. Selecting Normal in MIC mode is the universal restore (switches to
   stream 0 AND clears any mute).

## TTS plays silently

- Something muted before speaking: `SetMute(true)` pulls the speaker
  down → the flow must re-assert `SetVolume` right after muting (the
  show and the event both do; copy that pattern).
- `GetVolume` readback (`x2_probe_audio.py`) shows the real level.
- Speaker deliberately Muted in the panel = silent rehearsal, honored
  everywhere.

## First words cut off

Bluetooth speaker stream-start swallowing. The event's "external
Bluetooth speaker" checkbox enables the primer + silent lead-in; it is
per-event config, mirrored to both robots. The full show has no primer
yet — known gap if shows run over BT.

## Gesture rejected (`code=1 state=2`)

- Right after a dance → STAND_DEFAULT not settled (x2_action retries
  automatically; standalone calls need `--stand-settle`).
- Otherwise: wrong control area or firmware lacks the motion — sweep
  with `python3 x2_probe_area.py --motion <id>`; all-areas-rejected =
  unsupported (the 2001/3xxx/4xxx presets are known-unsupported).
- Everything rejected including Stable Stand → robot-side controller
  fault; reboot the robot (has happened once).

## Event problems

- "No sound": check `/api/event` → empty `message` means a save
  mirrored blank text; journal `Starting event:` line shows the exact
  args the run used.
- "Not copied to the other robot": the copy is QUEUED in that browser
  and auto-delivers when the robot comes online; one extra 💾 Save with
  both robots up also settles it.
- Play is slow to start from Normal: by design — auto gear-up (~15 s)
  runs first so the audience can't trigger the assistant mid-event.

## Probes (in the repo, run on the robot, sourced shell)

| Probe | Question it answers |
|---|---|
| `x2_probe_area.py --motion N` | Which control area (if any) this firmware accepts for motion N |
| `x2_probe_emoji.py` | Which emotion IDs the face renders (watch the face; prints success/state per ID) |
| `x2_probe_audio.py` | Real mute/volume/mic-source readbacks — run healthy vs broken and diff |
