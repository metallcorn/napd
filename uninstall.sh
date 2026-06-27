#!/usr/bin/env bash
# Remove napd cleanly. Releases any cgroup caps and unloads the KWin script
# (the daemon does this on SIGTERM, before we delete the unit).
set -euo pipefail

systemctl --user stop napd.service 2>/dev/null || true
systemctl --user disable napd.service 2>/dev/null || true

# belt-and-suspenders: make sure the KWin script is gone
qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.unloadScript napd-focus 2>/dev/null || true

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
rm -f "$UNIT_DIR/napd.service"
systemctl --user daemon-reload

echo "napd removed. No system files were ever touched."
