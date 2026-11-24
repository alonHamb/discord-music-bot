#!/usr/bin/env bash
# Build dist/discord-music-bot-<version>-alpine.tar.gz: the bot plus install.sh, which sets it up as an OpenRC
# service on Alpine Linux. Made on Linux (WSL) so the scripts keep their executable permission.
# Usage:  bash packaging/build-alpine.sh <version>
set -euo pipefail

VERSION="${1:?usage: build-alpine.sh <version>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="discord-music-bot-${VERSION}-alpine"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
DIR="$STAGE/$NAME"

mkdir -p "$DIR/app"
cp "$ROOT"/*.py "$ROOT/requirements.txt" "$DIR/app/"
cp "$ROOT"/packaging/alpine/* "$DIR/"
cp "$ROOT/packaging/deb/etc/discord-music-bot/bot.env" "$DIR/bot.env"
sed -i 's/sudo discord-music-bot token/discord-music-bot token/; s/sudo systemctl restart discord-music-bot/discord-music-bot restart/' "$DIR/bot.env"
printf '%s' "$VERSION" > "$DIR/VERSION"
find "$DIR" -type f -exec sed -i 's/\r$//' {} +  # files may come from Windows
find "$DIR" -type d -exec chmod 755 {} +
find "$DIR" -type f -exec chmod 644 {} +
chmod 755 "$DIR/install.sh" "$DIR/uninstall.sh" "$DIR/discord-music-bot" "$DIR/discord-music-bot.initd"

mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/$NAME.tar.gz"
tar -czf "$OUT" --owner=0 --group=0 --numeric-owner -C "$STAGE" "$NAME"
echo "Built $OUT ($(du -h "$OUT" | cut -f1))"
