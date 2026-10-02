---
name: agibot-x2-field-notes
description: Field-calibrated AgiBot X2 / AimDK knowledge that is in no official doc — mic stream IDs, lying readbacks, mute/speaker coupling, TTS timing, Bluetooth clipping, verified motion and emoji tables, SDK-vs-firmware gaps. Load before writing or debugging ANY code that talks to the robot.
---

# AgiBot X2 field notes (hard-won — do not re-learn these)

Everything below was measured on the show-suite robots (firmware as of
2026-10). When something contradicts the SDK docs, the robot wins.

## Microphone / audio routing

- **Streams: 0 = built-in mic array; 1 AND 2 = external.** The native
  iPad app writes 0 for "Built-in". Setting 1 as in-built was the
  historic deaf-after-show bug: Cooper accepts it, the readback agrees,
  and the robot hears nothing.
- `SetMicSourceRequest` (field `audio_stream_id`): the robot re-routes
  **only on a value change**, and the router takes seconds to apply.
  Pattern: switch to the opposite stream, wait `MIC_TOGGLE_GAP_S` (4 s),
  switch to the target, wait 2 s before unmuting. Commanding too fast
  leaves the SETTING right and the ROUTING wrong.
- `GetMicSourceRequest` **echoes the stored setting, not the actual
  routing**. Only the native app (and behavior) shows the truth.
- **`SetMute(true)` also pulls the speaker down.** Mute-then-speak flows
  must re-assert `SetVolume` immediately after the mute or TTS is
  silent. Mute = `is_mute`; speaker mute is `SetVolume 0`.
- Gear-up convention: performance = external stream 2, UNMUTED (muting
  kills show audio), volume-0-wrapped switches so the robot's "switched
  microphone" announcement is silent. Normal = stream 0 + clear mute.
- `GetMute` / `GetVolume` ARE callable (feed the 30 s state watcher);
  `GetAudioStatus` is not (see SDK gap).

## TTS

- `PlayTts` request: `tts_req.text/domain/trace_id/is_interrupted/`
  `priority_weight/priority_level.value(6)`. Response
  `estimated_duration` is unreliable: often 0, else overshoots ~9 s on
  a 563-char message. **Time speech from text**: ~14 Latin chars/s,
  ~4 CJK chars/s, 2 s floor (`speech_seconds` in x2_event.py).
- **Bluetooth speakers swallow stream starts** (first words cut). Fix:
  "." TTS primer ~2 s ahead + ~1 s leading punctuation silence
  (", , , ") on the message. Only when BT is in use (event
  `bt_speaker` flag) — the robot cannot report the active output.

## Preset motions (SetMcPresetMotion)

Area is a bitmask: 1 left hand, 2 right hand, 3 both, 4 head, 8 waist.
Rejection signature: `code=1 state=2` (FAILURE). **Verified working**:

| Motion/area | Gesture |
|---|---|
| 1002/2, 1002/1 | Wave right / left |
| 1003/2 | Handshake |
| 1004/2 | Blow kiss |
| 1007/3 | Heart (both hands) — NOT in the enum, works anyway |
| 1001/2 | Raise hand |
| 1008/3 | Clap |
| 1009/2 | Fist bump |
| 1013/2 | Salute |

**Rejected by this firmware in every area** (enum newer than motion
library): 2001 turn-wave, all 3xxx interaction presets (bow, thumbs-up,
peace, heart-overhead, hug, photo poses, …), 4001/4002 head moves.
Re-probe after robot software updates: `python3 x2_probe_area.py
--motion <id>`.

- **After a LinkCraft dance the controller refuses preset motions**
  until `SetMcAction` with `action_desc="STAND_DEFAULT"` + settle
  (~3 s; x2_action.py auto-retries with 8 s).
- Robot `CommonState`: SUCCESS=1, FAILURE=2, ABORTED=3, TIMEOUT=4,
  INVALID=5, IN_MANUAL=6, NOT_READY=100, PENDING=200, CREATED=300,
  RUNNING=400. (The dev stub harness uses RUNNING=2 — different!)
  "Accepted" = code==0 or state in (SUCCESS, RUNNING).

## Face emojis (PlayEmoji)

Request fields: **`emotion_id`** (uint8) + **`mode`** (1 once, 2 loop)
— NOT `emoji_id`/`loop` (those spellings silently set nothing → every
call played emotion 0/UNKNOWN for weeks). Enum: 1 blink, 10/11 calm,
20 game, 30–33 cute, 40/50 eye close/open, 60 bored, 70 abnormal,
80 sleepy, 90 happy, 100/101 very happy, 110 sad, 120 sympathy,
130 confused, 140 shock, 150 act-cute, 160 serious, 170 thinking,
180/190 angry, 200/210 adore, 220 charge. Which ones the face app
renders is UNVERIFIED — sweep with `x2_probe_emoji.py` while watching
the face.

## LinkCraft dances (ExecuteActionResource / GetRobotResources)

- Resource keys are **per robot** — a key from one robot fails on the
  other. The panel stores the show dance per robot and validates
  against the live library before launching.
- Play time: per-song seconds, 999 = full song (measured duration when
  available, 33 s default). Audio files live in a private container —
  unreachable from the host, so duration measuring often fails.

## SDK vs firmware gap

A robot software update renamed `~/aimdk` → `~/aimdk.bak~`; the overlay
predates the firmware. **Live on the wire but uncallable** (no .idl, no
Python class): `GetAudioStatus`, `GetAgentPropertiesRequest`,
`GetPlayDevice`, `ConnectBluetooth`/`DisConnectBluetooth`/
`CtrlScanBluetooth`, `msg/PlayDeviceChange`. The definitions live inside
AgiBot's containers, not on the host. Fix: obtain the matching AimDK_X2
SDK from AgiBot, install at `~/aimdk`, re-run the installer (it prefers
`~/aimdk`; current path is in `deploy/.panel_env` →
`COOPER_AIMDK_SETUP`). Unlocks: speaker-output auto-detect/select, true
mic-routing verification, conversation-agent properties.

## Shell / environment on the robots

- Always `source /opt/ros/humble/setup.bash && source <overlay>` before
  `ros2`/`python3` — "Unknown package 'aimdk_msgs'" means an unsourced
  shell, not a missing service.
- ROS setup.bash references unset vars → **never `set -u`** in wrappers.
- rclpy teardown can print `terminate called…/Aborted` at process exit —
  harmless if results already printed.
- Stdout of robot subprocesses is block-buffered → run them with
  `python3 -u` or journal timestamps all land at exit.
