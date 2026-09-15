#!/usr/bin/env bash
# Remove EVERYTHING the Cooper control panel installed on this robot.
#
#   ./deploy/uninstall_cooper.sh            # asks for confirmation
#   ./deploy/uninstall_cooper.sh --yes      # no questions (for scripting)
#   ./deploy/uninstall_cooper.sh --keep-code  # clean up but keep ~/cooper
#
# Everything Cooper-related is USER-LEVEL (installed without sudo), so this
# script removes it all without admin rights:
#   - running processes (API server, wrapper, show/gesture scripts)
#   - cron entries (@reboot + the every-minute watchdog)
#   - systemd user units (cooper-panel.service/.socket,
#     cooper-watchdog.service/.timer) — these contain the panel PIN
#   - the lingering flag (if it was ever enabled for this user)
#   - logs (~/cooper-panel.log, ~/cron_test.log)
#   - the code directory itself (unless --keep-code), including the
#     runtime files: cooper_panel_config.json, cooper_messages.json,
#     cooper_show_timing.json, deploy/.panel_env (PIN)
#
# The robot's SYSTEM is untouched by the panel (we never had sudo), so a
# true factory reset beyond this is the manufacturer's own procedure.
set -u

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
RUN_USER="${USER:-$(id -un)}"
ASSUME_YES=0
KEEP_CODE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y) ASSUME_YES=1; shift ;;
    --keep-code) KEEP_CODE=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

echo "This will remove the Cooper control panel from this robot:"
echo "  - stop the API and any running show"
echo "  - remove the cron entries and systemd user units (incl. the PIN)"
echo "  - disable lingering for $RUN_USER (if enabled)"
echo "  - delete the logs"
if [[ $KEEP_CODE -eq 0 ]]; then
  echo "  - DELETE the code directory: $APP_DIR"
fi
if [[ $ASSUME_YES -eq 0 ]]; then
  read -r -p "Continue? [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Aborted."; exit 1; }
fi

echo "1/6 Stopping processes..."
pkill -f run_cooper_panel.sh      2>/dev/null && sleep 1 || true
pkill -9 -f cooper_panel_server.py 2>/dev/null || true
pkill -9 -f x2_showroom_demo.py    2>/dev/null || true
pkill -9 -f x2_action.py           2>/dev/null || true

echo "2/6 Removing cron entries..."
if command -v crontab >/dev/null 2>&1; then
  REMAINING="$( (crontab -l 2>/dev/null || true) \
                | grep -v "run_cooper_panel.sh" | grep -v "cron_test" || true)"
  if [[ -n "$REMAINING" ]]; then
    printf '%s\n' "$REMAINING" | crontab -
  else
    crontab -r 2>/dev/null || true   # nothing left — drop the crontab entirely
  fi
fi

echo "3/6 Removing systemd user units..."
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now \
    cooper-watchdog.timer cooper-watchdog.service \
    cooper-panel.socket cooper-panel.service    >/dev/null 2>&1 || true
  rm -f "$UNIT_DIR/cooper-panel.service" "$UNIT_DIR/cooper-panel.socket" \
        "$UNIT_DIR/cooper-watchdog.service" "$UNIT_DIR/cooper-watchdog.timer"
  systemctl --user daemon-reload              >/dev/null 2>&1 || true
  systemctl --user reset-failed               >/dev/null 2>&1 || true
fi

echo "4/6 Disabling lingering (if it was enabled)..."
loginctl disable-linger "$RUN_USER" >/dev/null 2>&1 || true

echo "5/6 Removing logs..."
rm -f "$HOME/cooper-panel.log" "$HOME/cron_test.log"

echo "6/6 Verifying..."
LEFT=0
if pgrep -f "cooper_panel_server.py" >/dev/null 2>&1; then
  echo "  WARNING: a cooper_panel_server.py process is still running"; LEFT=1
fi
if crontab -l 2>/dev/null | grep -q "run_cooper_panel.sh"; then
  echo "  WARNING: a cron entry survived — check 'crontab -l'"; LEFT=1
fi
if ls "$UNIT_DIR"/cooper-* >/dev/null 2>&1; then
  echo "  WARNING: unit files survived in $UNIT_DIR"; LEFT=1
fi
[[ $LEFT -eq 0 ]] && echo "  Clean: no processes, cron entries, or units remain."

if [[ $KEEP_CODE -eq 1 ]]; then
  echo "Done. Code kept at $APP_DIR (runtime files and PIN still inside it —"
  echo "delete the directory yourself before handing the robot over)."
  exit 0
fi

# Delete the code directory LAST, from a command string already in memory —
# this script lives inside it, so it must not read itself after the rm.
echo "Deleting $APP_DIR ..."
cd "$HOME"
exec /bin/bash -c "rm -rf '$APP_DIR' && echo 'Done. The Cooper control panel is fully removed from this robot.'"
