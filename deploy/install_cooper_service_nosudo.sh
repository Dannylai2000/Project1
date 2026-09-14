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

  # Self-heal timer: every minute, clear any failed state (e.g. the
  # "start-limit-hit" crash-loop lockout) and re-arm the socket if it died —
  # the systemd-mode equivalent of the cron watchdog. No SSH needed.
  cat > "$UNIT_DIR/cooper-watchdog.service" <<EOF
[Unit]
Description=Cooper panel self-heal (clear failed state, re-arm the socket)

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'systemctl --user reset-failed cooper-panel.service cooper-panel.socket 2>/dev/null || true; systemctl --user is-active --quiet cooper-panel.socket || systemctl --user restart cooper-panel.socket'
EOF

  cat > "$UNIT_DIR/cooper-watchdog.timer" <<EOF
[Unit]
Description=Run the Cooper panel self-heal every minute

[Timer]
OnBootSec=45
OnUnitActiveSec=60

[Install]
WantedBy=timers.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable --now cooper-panel.socket
  systemctl --user enable --now cooper-watchdog.timer
  echo
  echo "Done. The API starts on the first connection to port $PORT."
  echo "Self-heal timer armed: failed units are reset and the socket is"
  echo "re-armed automatically every minute."
  if loginctl enable-linger "$USER" >/dev/null 2>&1; then
    echo "Lingering enabled: it will also arm at every boot, no login needed."
  else
    echo "NOTE: could not enable lingering (needs admin once):"
    echo "        sudo loginctl enable-linger $USER"
    echo "      Until then the socket arms when this user logs in — robots"
    echo "      that auto-login their user are fine as-is."
    echo "      No admin available? Re-run this installer with --cron:"
    echo "      cron @reboot needs no login and no admin (always-on mode)."
  fi
else
  echo "Mode:    cron @reboot fallback (always-on with auto-restart)"
  # Hand port $PORT over: an armed user-systemd socket from a previous
  # install would block the cron-run server ("address already in use").
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now cooper-panel.socket cooper-panel.service \
      >/dev/null 2>&1 || true
  fi
  command -v crontab >/dev/null 2>&1 || {
    echo "ERROR: neither user systemd nor crontab is available." >&2
    echo "Start manually instead:" >&2
    echo "  nohup $APP_DIR/deploy/run_cooper_panel.sh >> \$HOME/cooper-panel.log 2>&1 &" >&2
    exit 1
  }
  CRON_BOOT="@reboot /bin/bash $APP_DIR/deploy/run_cooper_panel.sh >> \$HOME/cooper-panel.log 2>&1"
  # Every-minute watchdog: revives the API within 60 s if it is ever not
  # running — covers crashes AND systems that kill a user's background
  # processes at logout (which also kills the nohup start below). The
  # pattern is anchored to end-of-cmdline: a real wrapper's command line
  # ENDS with the script path, while the watchdog's own cron shell (which
  # also contains the path, in its restart half) continues with the log
  # redirect — so the watchdog never matches itself.
  CRON_WATCH="* * * * * pgrep -f \"[r]un_cooper_panel.sh\$\" >/dev/null 2>&1 || /bin/bash $APP_DIR/deploy/run_cooper_panel.sh >> \$HOME/cooper-panel.log 2>&1"
  # Build the new crontab in a temp file. Every step tolerates "no crontab
  # yet" and "no other lines" — with set -e, a bare pipeline here used to
  # kill the whole script on machines that never had a crontab.
  TMP_CRON="$(mktemp)"
  ( crontab -l 2>/dev/null || true ) | grep -v "run_cooper_panel.sh" > "$TMP_CRON" || true
  printf '%s\n%s\n' "$CRON_BOOT" "$CRON_WATCH" >> "$TMP_CRON"
  crontab "$TMP_CRON"
  rm -f "$TMP_CRON"
  crontab -l | grep -q "run_cooper_panel.sh" || {
    echo "ERROR: cron entry did not stick — check 'crontab -l'." >&2
    exit 1
  }
  echo "Cron entries installed (@reboot + every-minute watchdog)."
  echo "Starting the API now..."
  if ! pgrep -f "cooper_panel_server.py --port $PORT" >/dev/null 2>&1; then
    nohup /bin/bash "$APP_DIR/deploy/run_cooper_panel.sh" >> "$HOME/cooper-panel.log" 2>&1 &
    echo "Started (log: ~/cooper-panel.log)."
  else
    echo "Already running."
  fi
fi
