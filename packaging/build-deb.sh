#!/usr/bin/env bash
# Build dist/discord-music-bot_<version>_all.deb from the bot's source files.
# Needs a Debian/Ubuntu system (for dpkg-deb). Usage:  bash packaging/build-deb.sh [version]
set -euo pipefail

VERSION="${1:-1.0.0}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PKG="$STAGE/discord-music-bot"

command -v dpkg-deb >/dev/null || { echo "dpkg-deb not found: build this on Debian or Ubuntu." >&2; exit 1; }

# Package skeleton (control files, service, settings, the discord-music-bot command)...
cp -r "$ROOT/packaging/deb" "$PKG"
sed -i "s/@VERSION@/$VERSION/" "$PKG/DEBIAN/control"
# ...plus the bot itself: every .py file in the bot folder, and requirements.txt.
mkdir -p "$PKG/opt/discord-music-bot"
cp "$ROOT"/*.py "$ROOT/requirements.txt" "$PKG/opt/discord-music-bot/"

# Files may come from Windows: force Unix line endings and proper permissions.
find "$PKG" -type f -exec sed -i 's/\r$//' {} +
find "$PKG" -type d -exec chmod 755 {} +
find "$PKG" -type f -exec chmod 644 {} +
chmod 755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/prerm" "$PKG/DEBIAN/postrm" "$PKG/usr/bin/discord-music-bot"
chmod 600 "$PKG/etc/discord-music-bot/bot.env"

mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/discord-music-bot_${VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$PKG" "$OUT" >/dev/null
echo "Built $OUT ($(du -h "$OUT" | cut -f1))"
