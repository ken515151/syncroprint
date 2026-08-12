#!/bin/bash
# SyncroPrint installer for Linux Mint / Ubuntu. Run as root from the repo root.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo bash packaging/install.sh" >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_DIR=/usr/lib/syncroprint
DESKTOP_USER="${SUDO_USER:-}"

echo "== Installing dependencies =="
apt-get update -qq
apt-get install -y -qq python3 python3-requests python3-websockets \
    python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 cups-client

echo "== Creating service user =="
if ! id syncroprint &>/dev/null; then
    useradd --system --home-dir /var/lib/syncroprint --shell /usr/sbin/nologin syncroprint
fi
usermod -aG lp syncroprint

echo "== Installing files =="
mkdir -p "$LIB_DIR" /etc/syncroprint /var/lib/syncroprint/spool
cp -r "$REPO_DIR/syncroprintd" "$LIB_DIR/"
cp -r "$REPO_DIR/applet" "$LIB_DIR/"
chown -R root:root "$LIB_DIR"

if [[ ! -f /etc/syncroprint/config.json ]]; then
    cp "$REPO_DIR/packaging/config.example.json" /etc/syncroprint/config.json
fi
# The daemon owns its config (the applet writes it via the daemon's control
# socket, so the daemon user needs write access); group syncroprint may read.
chown -R syncroprint:syncroprint /etc/syncroprint
chmod 0750 /etc/syncroprint
chmod 0640 /etc/syncroprint/config.json
chown -R syncroprint:syncroprint /var/lib/syncroprint

echo "== Installing systemd unit =="
cp "$REPO_DIR/packaging/syncroprintd.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable syncroprintd.service

echo "== Desktop integration =="
if [[ -n "$DESKTOP_USER" && "$DESKTOP_USER" != "root" ]]; then
    usermod -aG syncroprint "$DESKTOP_USER"
    AUTOSTART_DIR="$(getent passwd "$DESKTOP_USER" | cut -d: -f6)/.config/autostart"
    mkdir -p "$AUTOSTART_DIR"
    cp "$REPO_DIR/packaging/syncroprint-applet.desktop" "$AUTOSTART_DIR/"
    chown "$DESKTOP_USER": "$AUTOSTART_DIR/syncroprint-applet.desktop"
    echo "Applet autostart installed for $DESKTOP_USER (group change needs re-login)"
fi

echo
echo "Done. Next steps:"
echo "  1. systemctl start syncroprintd"
echo "  2. Log out and back in (group membership), then click the tray icon"
echo "  3. Settings… → enter subdomain + AutoPrinter App Center token → Test connection → Save"
echo "  4. Settings… → Printers + Events tabs: pick queues and enable document types"
echo "  (journalctl -u syncroprintd -f to watch it connect)"
