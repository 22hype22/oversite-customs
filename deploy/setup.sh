#!/usr/bin/env bash
# Oversite Customs — one-command VPS setup with IPv6 /64 rotation.
#
# This is the "real music bot" setup: it configures the server so the bot can
# send every YouTube request from a DIFFERENT IPv6 address inside your /64 block.
# No single address ever makes enough requests to trip YouTube's bot-check, and
# YouTube can't block a whole /64 (that would hit real users). Result: /play and
# /radio keep working without cookies, tokens, or proxies — set up once.
#
# USE ON: a fresh Ubuntu 22.04 / 24.04 server that has a routed IPv6 /64
#         (Hetzner Cloud gives every server a /64 — recommended, ~$4/mo).
#
# STEPS (run as root):
#   git clone https://github.com/22hype22/oversite-customs /opt/oversite-customs
#   cd /opt/oversite-customs
#   cp deploy/.env.example deploy/.env
#   nano deploy/.env          # paste your 3 secrets (from Railway → Variables)
#   sudo bash deploy/setup.sh
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE="oversite-customs"
ENV_FILE="${REPO_DIR}/deploy/.env"

if [[ $EUID -ne 0 ]]; then echo "!! Run as root:  sudo bash deploy/setup.sh"; exit 1; fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "!! Missing ${ENV_FILE}"
  echo "   Run:  cp deploy/.env.example deploy/.env  then edit it (nano deploy/.env)"
  exit 1
fi

# Load the env file (KEY=VALUE lines)
set -a; # shellcheck disable=SC1090
source "$ENV_FILE"; set +a

echo "==> Installing packages (python, ffmpeg, git)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git ffmpeg curl iproute2

# ── Detect the server's IPv6 /64 (unless you set IPV6_SUBNET yourself) ──
if [[ -z "${IPV6_SUBNET:-}" ]]; then
  GLOBAL6="$(ip -6 addr show scope global | awk '/inet6/{print $2}' | grep -v '^fe80' | head -n1 || true)"
  if [[ -n "$GLOBAL6" ]]; then
    IPV6_SUBNET="$(python3 - "$GLOBAL6" <<'PY'
import ipaddress,sys
a=sys.argv[1].split('/')[0]
print(ipaddress.IPv6Network(a+"/64",strict=False).with_prefixlen)
PY
)"
    echo "==> Auto-detected IPv6 /64: ${IPV6_SUBNET}"
  fi
fi
if [[ -z "${IPV6_SUBNET:-}" ]]; then
  echo "!! Could not find a global IPv6 /64 on this server."
  echo "   Make sure the VPS has IPv6 enabled, or set IPV6_SUBNET=... in deploy/.env"
  exit 1
fi

# ── Configure AnyIP: allow binding to any address in the /64 ──
echo "==> Enabling non-local bind + local route for ${IPV6_SUBNET}"
echo "net.ipv6.ip_nonlocal_bind=1" >/etc/sysctl.d/99-anyip.conf
sysctl --system >/dev/null

cat >/etc/systemd/system/anyip.service <<EOF
[Unit]
Description=AnyIP local route for IPv6 /64 rotation
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/sbin/ip -6 route replace local ${IPV6_SUBNET} dev lo
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now anyip.service

# ── Python venv + dependencies ──
echo "==> Installing Python dependencies…"
python3 -m venv "${REPO_DIR}/.venv"
"${REPO_DIR}/.venv/bin/pip" install --upgrade pip >/dev/null
"${REPO_DIR}/.venv/bin/pip" install -r "${REPO_DIR}/requirements.txt"

# ── systemd service for the bot ──
echo "==> Creating the bot service…"
cat >/etc/systemd/system/${SERVICE}.service <<EOF
[Unit]
Description=Oversite Customs Discord bot
After=network-online.target anyip.service
Wants=network-online.target
[Service]
Type=simple
WorkingDirectory=${REPO_DIR}
EnvironmentFile=${ENV_FILE}
Environment=IPV6_SUBNET=${IPV6_SUBNET}
ExecStart=${REPO_DIR}/.venv/bin/python ${REPO_DIR}/main.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now ${SERVICE}.service

echo ""
echo "==> DONE. The bot is running with IPv6 rotation on ${IPV6_SUBNET}"
echo "    Watch the logs:   journalctl -u ${SERVICE} -f"
echo "    Look for:         [Boot] egress IP (ipv6) = ...  <-- rotates per request"
echo "    Restart:          systemctl restart ${SERVICE}"
echo "    Update later:     cd ${REPO_DIR} && git pull && systemctl restart ${SERVICE}"
