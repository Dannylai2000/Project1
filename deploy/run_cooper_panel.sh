#!/usr/bin/env bash
# Keep the Cooper control API running WITHOUT root: start it, and restart it
# if it ever exits. Used by the cron fallback of the no-sudo installer, and
# handy for a quick manual start:
#
#     nohup ./deploy/run_cooper_panel.sh >> ~/cooper-panel.log 2>&1 &
#
# PIN and port come from deploy/.panel_env (written by the installer) or the
# COOPER_PANEL_PIN / COOPER_PANEL_PORT environment variables.
#
# No "set -u" here: ROS's own setup.bash references unset variables
# (e.g. AMENT_TRACE_SETUP_FILES), which under nounset kills this script
# at the source line — the API then never starts from cron/@reboot.

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR" || exit 1

[ -f "$APP_DIR/deploy/.panel_env" ] && . "$APP_DIR/deploy/.panel_env"
PORT="${COOPER_PANEL_PORT:-8080}"
export COOPER_PANEL_PIN="${COOPER_PANEL_PIN:-}"

# shellcheck disable=SC1091
source /opt/ros/humble/setup.bash
# AimDK overlay: the installer detects its location per robot and stores it
# in .panel_env (COOPER_AIMDK_SETUP); the classic path is the fallback.
# shellcheck disable=SC1091
source "${COOPER_AIMDK_SETUP:-$HOME/aimdk/install/setup.bash}"
# Optional extra workspace: create deploy/extra_setup.bash with its source line.
# shellcheck disable=SC1091
[ -f "$APP_DIR/deploy/extra_setup.bash" ] && . "$APP_DIR/deploy/extra_setup.bash"

while true; do
  python3 cooper_panel_server.py --port "$PORT"
  echo "$(date): cooper_panel_server exited (code $?) — restarting in 5 s" >&2
  sleep 5
done
