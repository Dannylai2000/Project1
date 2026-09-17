#!/usr/bin/env bash
# Run ON OPTIMUS (with sudo). Keeps a permanent SSH login session open to a
# robot, which keeps the robot's user-level services (the Cooper API socket
# and self-heal timer) armed at all times.
#
#   sudo ./deploy/install_session_keeper_on_optimus.sh 192.168.68.113 agi --password <pw>
#   sudo ./deploy/install_session_keeper_on_optimus.sh --remove 192.168.68.113
#
# One keeper PER ROBOT (they coexist; the unit name contains the IP) — the
# standby robot must stay armed too, or failover would switch to an offline
# robot. When a robot's IP changes: --remove the old IP, install the new one.
#
# Security model:
#   - No password is ever stored. --password is used ONCE to install the
#     key, then discarded (needs sshpass; installed automatically).
#   - The keeper uses a DEDICATED key (~/.ssh/cooper_keeper_ed25519), not
#     your personal one, and it is installed on the robot with an
#     authorized_keys restriction (command="sleep infinity", no-pty, no
#     port/agent/X11 forwarding): even if the key leaks, it can only hold
#     an idle session — it cannot run commands on the robot.
set -euo pipefail

ROBOT_IP=""
ROBOT_USER="agi"
PASSWORD=""
REMOVE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --password) PASSWORD="$2"; shift 2 ;;
    --remove) REMOVE=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)
      if [[ -z "$ROBOT_IP" ]]; then ROBOT_IP="$1"
      else ROBOT_USER="$1"; fi
      shift ;;
  esac
done
[[ -n "$ROBOT_IP" ]] || {
  echo "usage: sudo $0 <robot-ip> [robot-user] [--password <pw>]" >&2
  echo "       sudo $0 --remove <robot-ip>" >&2
  exit 1
}

RUN_AS="${SUDO_USER:-root}"
RUN_AS_HOME="$(getent passwd "$RUN_AS" | cut -d: -f6)"
UNIT_NAME="cooper-session-keeper-${ROBOT_IP//./-}.service"
UNIT="/etc/systemd/system/$UNIT_NAME"
KEY="$RUN_AS_HOME/.ssh/cooper_keeper_ed25519"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0 ..." >&2
  exit 1
fi

if [[ $REMOVE -eq 1 ]]; then
  systemctl disable --now "$UNIT_NAME" >/dev/null 2>&1 || true
  rm -f "$UNIT"
  systemctl daemon-reload
  echo "Keeper for $ROBOT_IP removed. (The keeper key line can be deleted"
  echo "from ~$ROBOT_USER/.ssh/authorized_keys on that robot if it will"
  echo "never be used again.)"
  exit 0
fi

SSH_BASE=(-o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new)

# Dedicated keeper key — separate from the user's personal key so the
# on-robot restriction never affects normal SSH use.
if [[ ! -f "$KEY" ]]; then
  sudo -u "$RUN_AS" ssh-keygen -t ed25519 -N "" -f "$KEY" \
    -C "cooper-session-keeper@optimus" >/dev/null
  echo "Created the dedicated keeper key: $KEY"
fi

keeper_auth_ok() {
  # The keeper key is restricted with command="sleep infinity" on the robot,
  # so a normal "ssh ... true" probe would hang inside our own restriction
  # (the robot runs the forced command instead of `true`). Probe with -N
  # (no command) under a short timeout instead: if the connection survives
  # 5 s, authentication succeeded — auth failures exit almost immediately.
  timeout 5 sudo -u "$RUN_AS" ssh -i "$KEY" -o IdentitiesOnly=yes "${SSH_BASE[@]}" \
    -N "$ROBOT_USER@$ROBOT_IP" 2>/dev/null
  [[ $? -eq 124 ]]   # 124 = timeout killed a still-connected ssh = auth OK
}

if ! keeper_auth_ok; then
  # Install the restricted key line on the robot, via whichever channel
  # works: the user's existing key access, or --password (once).
  PUB="$(cat "$KEY.pub")"
  LINE="command=\"sleep infinity\",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding $PUB"
  REMOTE='mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && line="$(cat)" && grep -qF "$line" ~/.ssh/authorized_keys || echo "$line" >> ~/.ssh/authorized_keys'
  if printf '%s\n' "$LINE" | timeout 20 sudo -u "$RUN_AS" ssh "${SSH_BASE[@]}" \
       "$ROBOT_USER@$ROBOT_IP" "$REMOTE" 2>/dev/null; then
    echo "Keeper key installed on the robot (via your existing SSH access)."
  elif [[ -n "$PASSWORD" ]]; then
    command -v sshpass >/dev/null 2>&1 || apt-get install -y sshpass >/dev/null 2>&1 || {
      echo "ERROR: sshpass is needed for --password and could not be installed." >&2
      exit 1
    }
    # timeout: sshpass hangs forever when the robot's password prompt looks
    # different from what it expects — fail loudly instead.
    printf '%s\n' "$LINE" | timeout 30 sudo -u "$RUN_AS" sshpass -p "$PASSWORD" \
      ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new \
      -o NumberOfPasswordPrompts=1 \
      "$ROBOT_USER@$ROBOT_IP" "$REMOTE" || {
      echo "ERROR: could not install the key using the password (wrong" >&2
      echo "password, unreachable robot, or an unusual login prompt)." >&2
      echo "Check by hand from this machine:" >&2
      echo "  ping -c2 $ROBOT_IP" >&2
      echo "  ssh $ROBOT_USER@$ROBOT_IP     # does password login work?" >&2
      echo "Manual fallback, then re-run WITHOUT --password:" >&2
      echo "  ssh-copy-id -i $KEY.pub $ROBOT_USER@$ROBOT_IP" >&2
      exit 1
    }
    echo "Keeper key installed on the robot — the password is no longer needed."
  else
    echo "ERROR: cannot reach $ROBOT_USER@$ROBOT_IP with a key, and no" >&2
    echo "--password was given for the one-time key installation." >&2
    exit 1
  fi
  keeper_auth_ok || {
    echo "ERROR: keeper key installed but key login still fails." >&2
    exit 1
  }
fi

cat > "$UNIT" <<EOF
[Unit]
Description=Hold a login session on $ROBOT_USER@$ROBOT_IP so the robot's user services stay armed
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_AS
ExecStart=/usr/bin/ssh -N \\
  -i $KEY -o IdentitiesOnly=yes \\
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
echo "Done. optimus now holds a login session on $ROBOT_USER@$ROBOT_IP"
echo "using the restricted keeper key (idle session only — it cannot run"
echo "commands on the robot). Install one keeper per robot; on an IP"
echo "change: sudo $0 --remove <old-ip>, then install the new one."
echo "Check:   systemctl status $UNIT_NAME"
