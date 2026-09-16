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
# Prerequisite — passwordless SSH from optimus to the robot (once):
#   ssh-keygen            # if this user has no key yet
#   ssh-copy-id agi@192.168.68.113
set -euo pipefail

ROBOT_IP="${1:?usage: sudo $0 <robot-ip> [robot-user]}"
ROBOT_USER="${2:-agi}"
RUN_AS="${SUDO_USER:-root}"
UNIT_NAME="cooper-session-keeper-${ROBOT_IP//./-}.service"
UNIT="/etc/systemd/system/$UNIT_NAME"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0 $ROBOT_IP $ROBOT_USER" >&2
  exit 1
fi

# Refuse to install a keeper that can't connect — check key auth first.
if ! sudo -u "$RUN_AS" ssh -o BatchMode=yes -o ConnectTimeout=5 \
       -o StrictHostKeyChecking=accept-new \
       "$ROBOT_USER@$ROBOT_IP" true 2>/dev/null; then
  echo "ERROR: passwordless SSH from user '$RUN_AS' to $ROBOT_USER@$ROBOT_IP failed." >&2
  echo "Set it up first (as $RUN_AS, no sudo):" >&2
  echo "  ssh-keygen        # only if you have no key yet" >&2
  echo "  ssh-copy-id $ROBOT_USER@$ROBOT_IP" >&2
  exit 1
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
