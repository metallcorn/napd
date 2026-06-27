#!/usr/bin/env bash
# Install napd as a systemd --user service. No root, no system files touched.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

chmod +x "$HERE/napd.py" "$HERE/napctl"

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
cp "$HERE/napd.service" "$UNIT_DIR/napd.service"

systemctl --user daemon-reload
systemctl --user enable --now napd.service

echo
echo "napd installed and started (OBSERVE mode — nothing is throttled)."
echo "  status:   $HERE/napctl"
echo "  logs:     journalctl --user -u napd -f"
echo "  stop:     systemctl --user stop napd"
echo "  remove:   $HERE/uninstall.sh"
