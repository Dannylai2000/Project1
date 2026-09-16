#!/usr/bin/env bash
# Run ON OPTIMUS (with sudo). Keeps a permanent SSH login session open to a
# robot, which keeps the robot's user-level services (the Cooper API socket
# and self-heal timer) armed at all times.
#
# Why: without lingering, a robot's user services stop the moment the last
# login ends — the panel goes red when you log out. On robots where
# "loginctl enable-linger" is refused and cron ignores user jobs, this
# keeper is the no-admin-on-the-robot fix: optimus (always on) simply stays
# "logged in" to the robot, reconnecting automatically after reboots and
# network drops.
#
#   sudo ./deploy/install_session_keeper_on_optimus.sh 192.168.68.113 agi
#
# Key-based SSH to the robot is required. Either set it up yourself first
# (ssh-keygen + ssh-copy-id agi@<robot-ip>), or pass the robot user's
# password ONCE with --password <pw> and the installer copies the key for
# you (needs the sshpass package; installed automatically when missing).
# The password is used only for that one key copy — nothing stores it.
set -euo pipefail

ROBOT_IP=""
ROBOT_USER="agi"
PASSWORD=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --password) PASSWORD="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)
      if [[ -z "$ROBOT_IP" ]]; then ROBOT_IP="$1"
      else ROBOT_USER="$1"; fi
      shift ;;
  esac
done
[[ -n "$ROBOT_IP" ]] || { echo "usage: sudo $0 <robot-ip> [robot-user] [--password <pw>]" >&2; exit 1; }

RUN_AS="${SUDO_USER:-root}"
RUN_AS_HOME="$(getent passwd "$RUN_AS" | cut -d: -f6)"
UNIT_NAME="cooper-session-keeper-${ROBOT_IP//./-}.service"
UNIT="/etc/systemd/system/$UNIT_NAME"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0 $ROBOT_IP $ROBOT_USER" >&2
  exit 1
fi

key_auth_ok() {
  sudo -u "$RUN_AS" ssh -o BatchMode=yes -o ConnectTimeout=5 \
    -o StrictHostKeyChecking=accept-new \
    "$ROBOT_USER@$ROBOT_IP" true 2>/dev/null
}

if ! key_auth_ok; then
  if [[ -z "$PASSWORD" ]]; then
    echo "ERROR: passwordless SSH from user '$RUN_AS' to $ROBOT_USER@$ROBOT_IP failed." >&2
    echo "Either set it up first (as $RUN_AS, no sudo):" >&2
    echo "  ssh-keygen        # only if you have no key yet" >&2
    echo "  ssh-copy-id $ROBOT_USER@$ROBOT_IP" >&2
    echo "or re-run with the robot password once:" >&2
    echo "  sudo $0 $ROBOT_IP $ROBOT_USER --password <pw>" >&2
    exit 1
  fi
  echo "Setting up key-based SSH using the provided password (one time)..."
  if ! command -v sshpass >/dev/null 2>&1; then
    apt-get install -y sshpass >/dev/null 2>&1 || {
      echo "ERROR: sshpass is not installed and could not be installed." >&2
      echo "Install it (sudo apt-get install sshpass) or run ssh-copy-id" >&2
      echo "manually, then re-run this installer without --password." >&2
      exit 1
    }
  fi
  if [[ ! -f "$RUN_AS_HOME/.ssh/id_ed25519" && ! -f "$RUN_AS_HOME/.ssh/id_rsa" ]]; then
    sudo -u "$RUN_AS" ssh-keygen -t ed25519 -N "" \
      -f "$RUN_AS_HOME/.ssh/id_ed25519" >/dev/null
  fi
  sudo -u "$RUN_AS" sshpass -p "$PASSWORD" ssh-copy-id \
    -o StrictHostKeyChecking=accept-new "$ROBOT_USER@$ROBOT_IP" >/dev/null
  key_auth_ok || {
    echo "ERROR: key copy appeared to succeed but key login still fails." >&2
    exit 1
  }
  echo "Key installed — the password is no longer needed."
fi

cat > "$UNIT" <<EOF
[Unit]
Description=Hold a login session on $ROBOT_USER@$ROBOT_IP so the robot's user services stay armed
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_AS
ExecStart=/usr/bin/ssh -N \\
  -o BatchMode=yes \\
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \\
  -o ConnectTimeout=10 \\
  -o StrictHostKeyChecking=accept-new \\
  $ROBOT_USER@$ROBOT_IP
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$UNIT_NAME"
echo
echo "Done. optimus now holds a login session on $ROBOT_USER@$ROBOT_IP,"
echo "keeping the robot's Cooper services armed at all times. It reconnects"
echo "by itself after optimus boots, the robot reboots, or Wi-Fi drops."
echo "Check:   systemctl status $UNIT_NAME"
echo "Remove:  sudo systemctl disable --now $UNIT_NAME && sudo rm $UNIT"
