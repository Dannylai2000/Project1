#!/usr/bin/env bash
# Install the Cooper Control Panel webserver on the robot as a systemd unit.
#
#   sudo ./deploy/install_cooper_service.sh --pin 2468              # on-demand (default)
#   sudo ./deploy/install_cooper_service.sh --pin 2468 --always-on  # run at boot, always
#
# On-demand mode (systemd socket activation): nothing runs until the panel
# connects to port 8080 — selecting a setup in the panel, or opening the page
# served by Cooper, starts the Python webserver automatically. After
# IDLE_EXIT minutes without requests it stops itself; the next connection
# starts it again. Always-on mode starts the server at boot and keeps it
# running (Restart=on-failure).
set -euo pipefail

MODE="on-demand"
PIN=""
PORT=8080
IDLE_EXIT=30
ROS_SETUP="/opt/ros/humble/setup.bash"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --always-on) MODE="always-on"; shift ;;
    --on-demand) MODE="on-demand"; shift ;;
    --pin) PIN="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --idle-exit) IDLE_EXIT="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0 ..." >&2
  exit 1
fi

RUN_USER="${SUDO_USER:-root}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AIMDK_SETUP="$RUN_HOME/aimdk/install/setup.bash"

echo "Mode:      $MODE"
echo "User:      $RUN_USER"
echo "App dir:   $APP_DIR"
echo "Port:      $PORT"
[[ "$MODE" == "on-demand" ]] && echo "Idle exit: ${IDLE_EXIT} min"

IDLE_ARG=""
[[ "$MODE" == "on-demand" ]] && IDLE_ARG=" --idle-exit $IDLE_EXIT"

cat > /etc/systemd/system/cooper-panel.service <<EOF
[Unit]
Description=Cooper Control Panel
After=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
Environment=COOPER_PANEL_PIN=$PIN
ExecStart=/bin/bash -lc 'source $ROS_SETUP && source $AIMDK_SETUP && exec python3 cooper_panel_server.py --port $PORT$IDLE_ARG'
$([[ "$MODE" == "always-on" ]] && echo "Restart=on-failure
RestartSec=5")

[Install]
WantedBy=multi-user.target
EOF

if [[ "$MODE" == "on-demand" ]]; then
  cat > /etc/systemd/system/cooper-panel.socket <<EOF
[Unit]
Description=Cooper Control Panel socket (starts the server on first connection)

[Socket]
ListenStream=$PORT

[Install]
WantedBy=sockets.target
EOF
  systemctl daemon-reload
  systemctl disable cooper-panel.service >/dev/null 2>&1 || true
  systemctl stop cooper-panel.service >/dev/null 2>&1 || true
  systemctl enable --now cooper-panel.socket
  echo
  echo "Done. The webserver is NOT running now — it starts automatically on"
  echo "the first connection to port $PORT (e.g. when a panel selects Cooper's"
  echo "setup) and stops itself after ${IDLE_EXIT} min without requests."
else
  systemctl daemon-reload
  systemctl disable --now cooper-panel.socket >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/cooper-panel.socket
  systemctl enable --now cooper-panel.service
  echo
  echo "Done. The webserver is running and will start at every boot."
fi
