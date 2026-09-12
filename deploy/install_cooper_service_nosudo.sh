#!/usr/bin/env bash
# Install the Cooper control API on the robot WITHOUT root/sudo.
#
#   ./deploy/install_cooper_service_nosudo.sh --pin 2468               # on-demand
#   ./deploy/install_cooper_service_nosudo.sh --pin 2468 --idle-exit 0 # never auto-stops
#   ./deploy/install_cooper_service_nosudo.sh --pin 2468 --cron        # force cron fallback
#
# Prefers systemd USER units (~/.config/systemd/user — no root needed),
# keeping on-demand socket activation. If user systemd is unavailable,
# falls back to a cron @reboot entry that runs deploy/run_cooper_panel.sh
# (always-on with auto-restart).
#
# Note on boot start with user systemd: services normally start when the
# user logs in. For start-at-boot without login, lingering must be enabled;
# the script tries "loginctl enable-linger" and tells you if an admin has
# to run it once. Robots that auto-login their user don't need lingering.
set -euo pipefail

PIN=""
PORT=8080
IDLE_EXIT=30
FORCE_CRON=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pin) PIN="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --idle-exit) IDLE_EXIT="$2"; shift 2 ;;
    --cron) FORCE_CRON=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ROS_SETUP="/opt/ros/humble/setup.bash"
AIMDK_SETUP="$HOME/aimdk/install/setup.bash"

echo "App dir: $APP_DIR"
echo "Port:    $PORT"

# Store PIN/port for the wrapper script (cron mode); private to this user.
printf 'COOPER_PANEL_PIN=%q\nCOOPER_PANEL_PORT=%q\n' "$PIN" "$PORT" \
  > "$APP_DIR/deploy/.panel_env"
chmod 600 "$APP_DIR/deploy/.panel_env"
chmod +x "$APP_DIR/deploy/run_cooper_panel.sh"

have_user_systemd() {
  command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}

if [[ $FORCE_CRON -eq 0 ]] && have_user_systemd; then
  echo "Mode:    systemd --user (on-demand, idle-exit ${IDLE_EXIT} min)"
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"

  IDLE_ARG=""
  [[ "$IDLE_EXIT" != "0" ]] && IDLE_ARG=" --idle-exit $IDLE_EXIT"

  cat > "$UNIT_DIR/cooper-panel.service" <<EOF
[Unit]
Description=Cooper Control Panel API

[Service]
WorkingDirectory=$APP_DIR
Environment=COOPER_PANEL_PIN=$PIN
ExecStart=/bin/bash -lc 'source $ROS_SETUP && source $AIMDK_SETUP && if [ -f $APP_DIR/deploy/extra_setup.bash ]; then source $APP_DIR/deploy/extra_setup.bash; fi; exec python3 cooper_panel_server.py --port $PORT$IDLE_ARG'

[Install]
WantedBy=default.target
EOF

  cat > "$UNIT_DIR/cooper-panel.socket" <<EOF
[Unit]
Description=Cooper Control Panel socket (starts the API on first connection)

[Socket]
ListenStream=$PORT

[Install]
WantedBy=sockets.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable --now cooper-panel.socket
  echo
  echo "Done. The API starts on the first connection to port $PORT."
  if loginctl enable-linger "$USER" >/dev/null 2>&1; then
    echo "Lingering enabled: it will also arm at every boot, no login needed."
  else
    echo "NOTE: could not enable lingering (needs admin once):"
    echo "        sudo loginctl enable-linger $USER"
    echo "      Until then the socket arms when this user logs in — robots"
    echo "      that auto-login their user are fine as-is."
  fi
else
  echo "Mode:    cron @reboot fallback (always-on with auto-restart)"
  command -v crontab >/dev/null 2>&1 || {
    echo "ERROR: neither user systemd nor crontab is available." >&2
    echo "Start manually instead:" >&2
    echo "  nohup $APP_DIR/deploy/run_cooper_panel.sh >> \$HOME/cooper-panel.log 2>&1 &" >&2
    exit 1
  }
  CRON_LINE="@reboot /bin/bash $APP_DIR/deploy/run_cooper_panel.sh >> \$HOME/cooper-panel.log 2>&1"
  ( crontab -l 2>/dev/null | grep -v "run_cooper_panel.sh" ; echo "$CRON_LINE" ) | crontab -
  echo "Cron entry installed. Starting the API now..."
  if ! pgrep -f "cooper_panel_server.py --port $PORT" >/dev/null 2>&1; then
    nohup /bin/bash "$APP_DIR/deploy/run_cooper_panel.sh" >> "$HOME/cooper-panel.log" 2>&1 &
    echo "Started (log: ~/cooper-panel.log)."
  else
    echo "Already running."
  fi
fi
