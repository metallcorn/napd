#!/usr/bin/env bash
# Build napd_<version>_all.deb with just dpkg-deb (no debhelper needed).
#   ./packaging/build-deb.sh [version]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-0.2.0}"
PKG="$(mktemp -d)/napd"
trap 'rm -rf "$(dirname "$PKG")"' EXIT

# payload
install -Dm0755 "$ROOT/napctl"                 "$PKG/usr/bin/napctl"
install -Dm0755 "$ROOT/napd.py"                "$PKG/usr/lib/napd/napd.py"
install -Dm0644 "$ROOT/napd-focus.js"          "$PKG/usr/lib/napd/napd-focus.js"
install -Dm0644 "$ROOT/packaging/napd.service" "$PKG/usr/lib/systemd/user/napd.service"
install -Dm0644 "$ROOT/README.md"              "$PKG/usr/share/doc/napd/README.md"
install -Dm0644 "$ROOT/INTERFACE.md"           "$PKG/usr/share/doc/napd/INTERFACE.md"
install -Dm0644 "$ROOT/LICENSE"                "$PKG/usr/share/doc/napd/copyright"

# control metadata
mkdir -p "$PKG/DEBIAN"
sed "s/@VERSION@/$VERSION/" "$ROOT/packaging/control.in" > "$PKG/DEBIAN/control"
printf 'Installed-Size: %s\n' "$(du -ks "$PKG/usr" | cut -f1)" >> "$PKG/DEBIAN/control"
install -m0755 "$ROOT/packaging/postinst" "$PKG/DEBIAN/postinst"
install -m0755 "$ROOT/packaging/prerm"    "$PKG/DEBIAN/prerm"

OUT="$ROOT/napd_${VERSION}_all.deb"
fakeroot dpkg-deb --build --root-owner-group "$PKG" "$OUT"
echo
echo "built: $OUT"
