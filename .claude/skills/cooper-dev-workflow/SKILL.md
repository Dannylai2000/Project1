---
name: cooper-dev-workflow
description: How to change, test, version and ship Project Cooper code — version lockstep, the stub test harness, panel JS checking, the branch/PR flow, and deployment to robots and webservers. Load before making any code change in this repo.
---

# Cooper development workflow

## 1. Versioning (every behavior change)

Bump BOTH to the same `YYYY.MM.DD-N` string:
- `SERVER_VERSION` in `cooper_panel_server.py`
- `const PANEL_VERSION` in `cooper_control_panel.html`

The panel's chip turns amber on mismatch — that is how operators see a
half-deployed update. Docs-only changes don't bump; anything the robot
or page executes does (even script-only changes, for deploy visibility).

## 2. Testing without a robot (stub harness)

No robot is reachable from dev. Test the server with fake `rclpy` +
`aimdk_msgs` modules on `PYTHONPATH`:

- Stubs live in the session scratchpad (`stubs/rclpy`, `stubs/aimdk_msgs`).
  If missing, recreate: `rclpy` with `init/shutdown/ok`, a `Node` whose
  `create_client` returns a client where `wait_for_service()` → False
  and `call_async` raises; `aimdk_msgs.srv` with empty `Request` classes
  for every service the code imports; `aimdk_msgs.msg` with
  `CommonState` (**stub uses SUCCESS=1, RUNNING=2 — the real robot uses
  RUNNING=400; never copy stub enums into tests of robot semantics**),
  `McActionCommand`, `McControlArea`, `McPresetMotion`, `RequestHeader`.
- Copy the server + x2 scripts into a scratch dir (never run the server
  in the repo — it writes `cooper_panel_config.json` beside itself),
  then: `PYTHONPATH=<stubs> python3 cooper_panel_server.py --port 809X
  --pin 1234 --idle-exit 0 &` and exercise endpoints with `curl`
  (`-H "X-Pin: 1234"` for POSTs). Check clamps, 400/401/409 paths, and
  the `Starting event/show:` journal lines for correct subprocess args.
- Compile check: `python3 -m py_compile cooper_panel_server.py x2_*.py`.
- Panel JS: extract every `<script>` block to a file and `node --check`
  it.
- Run multi-command tests from script FILES, not heredocs — a `pkill`
  pattern inside a heredoc matches the tool shell itself (exit 144).

## 3. Branch / PR flow

Work happens on `claude/agibot-x2-listening-toggle-7ddhsq`:

1. Commit with a clear message; push
   (`git push -u origin <branch> --force-with-lease`).
2. Open a PR to `master`, merge it.
3. Resync: `git fetch origin master && git checkout -B <branch>
   origin/master && git push -u origin <branch> --force-with-lease`.
   Never stack new commits on already-merged history.

## 4. Deployment

- **Robots**: panel 🩺 Diagnose → ⬆ Update & restart (runs
  `git pull --ff-only` and restarts; the watchdog/socket revives the
  API). Or by hand: `cd ~/cooper && git pull --ff-only` + restart.
  Verify the chip shows the new version with no amber warning.
- **Webservers** (optimus + BumbleBee): `git pull` in the clone, copy
  `cooper_control_panel.html` into the Apache root (per DEPLOYMENT.md
  §B). Both must be updated or users get version-mismatch chips.
- Fresh robot: `./deploy/install_cooper_service_nosudo.sh --pin <pin>`
  (auto-detects the AimDK overlay; `--aimdk-setup` overrides;
  `--cron` fallback), then the session keeper for its IP on optimus.

## 5. Design rules learned the hard way

- Webservers stay static; state stays on the robots; the panel mirrors
  shared content (event, messages) to both robots and queues failed
  copies in browser localStorage.
- Shared-content saves must give per-robot feedback ("✓ saved on BOTH
  robots" / "⚠ queued for <addr>") — silent loss is the cardinal sin.
- Old page ↔ new API (and vice versa) must degrade politely: guard new
  status fields (`typeof s.x === "boolean"`), keep old endpoints alive,
  show "needs API update on Cooper" instead of broken controls.
- Numeric inputs on the iPad are select dropdowns when negative or odd
  values are needed (no minus key on the number pad).
- Robot-facing constants that came from measurement (mic gaps, speech
  rates, stream IDs) carry a comment saying WHAT was measured and WHEN —
  they look arbitrary otherwise and get "simplified" away.
- New SDK capabilities get a probe script first, a feature second.
