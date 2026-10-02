# Project Cooper — AgiBot X2 Ultra control system (One Comcentre show suite)

Control system for two AgiBot X2 Ultra humanoid robots ("Cooper"). The
**robots** run the REST API (`cooper_panel_server.py`, port 8080, ROS 2 +
AimDK); the **webservers** (optimus 192.168.68.51 and the BumbleBee
notebook, Apache :8080) host ONLY the static page
`cooper_control_panel.html`. The browser talks directly to the robot's
API. This architecture is a firm decision — never move logic onto the
webservers.

- Primary robot: 192.168.68.115 · Secondary: 192.168.68.113 · robot user
  `agi`, repo clone at `~/cooper` on each robot.
- Architecture & full API reference: `API.md` (+ `docs/architecture.svg`).
- Install / self-heal / session keepers / webserver setup: `DEPLOYMENT.md`.

## Non-negotiable conventions

1. **Version lockstep**: every behavior change bumps `SERVER_VERSION`
   (cooper_panel_server.py) and `PANEL_VERSION` (cooper_control_panel.html)
   to the same `YYYY.MM.DD-N` string. The panel shows an amber chip on
   mismatch — that is the deployment check.
2. **Git flow**: develop on branch `claude/agibot-x2-listening-toggle-7ddhsq`,
   commit, push, PR to `master`, merge, then reset the branch onto
   `origin/master` (force-with-lease). Never stack on merged history.
3. **Line endings**: LF only (`.gitattributes` enforces; CRLF once broke
   every shebang on optimus).
4. The robots deploy via the panel's 🩺 Diagnose → ⬆ Update & restart
   (`git pull --ff-only` + restart). Webservers deploy via `git pull` +
   copying the page into the Apache root.

## Before touching robot-facing code

Load the skills — they hold hard-won field knowledge that is NOT in the
AgiBot docs and was expensive to learn (wrong mic stream IDs, lying
readbacks, mute pulling the speaker down, Bluetooth clipping, …):

- `cooper-architecture` — system map, files, state stores, API surface.
- `agibot-x2-field-notes` — AimDK quirks ledger + verified enum tables.
- `cooper-dev-workflow` — how to change, test (stub harness), version,
  ship and deploy.
- `cooper-troubleshoot` — runbook for offline panels, deaf robot, silent
  audio, boot persistence, probes.

For any substantial robot/panel question, the `cooper-expert` agent
(.claude/agents/) carries the same context for delegation.
