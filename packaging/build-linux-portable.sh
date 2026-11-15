#!/usr/bin/env bash
# Build dist/discord-music-bot-<version>-linux-portable.tar.gz: one folder with the bot, launcher.py and start.sh.
# Made on Linux (WSL) so start.sh keeps its executable permission. Usage:  bash packaging/build-linux-portable.sh <version>
set -euo pipefail

VERSION="${1:?usage: build-linux-portable.sh <version>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="discord-music-bot-${VERSION}-linux-portable"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
DIR="$STAGE/$NAME"

mkdir -p "$DIR"
cp "$ROOT"/*.py "$ROOT/requirements.txt" "$ROOT/.env.example" "$ROOT/portable/launcher.py" "$ROOT"/portable/linux/* "$DIR/"
printf '%s' "$VERSION" > "$DIR/VERSION"
find "$DIR" -type f -exec sed -i 's/\r$//' {} +  # files may come from Windows
find "$DIR" -type f -exec chmod 644 {} +  # (a plain * would skip .env.example)
chmod 755 "$DIR/start.sh" "$DIR/launcher.py"

mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/$NAME.tar.gz"
tar -czf "$OUT" --owner=0 --group=0 --numeric-owner -C "$STAGE" "$NAME"
echo "Built $OUT ($(du -h "$OUT" | cut -f1))"
