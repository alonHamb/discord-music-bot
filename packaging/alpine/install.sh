#!/bin/sh
# Install or upgrade the Discord music bot on Alpine Linux, as the OpenRC service discord-music-bot.
# Run it as root from the extracted folder:  ./install.sh
# Upgrading (running a newer version's install.sh) keeps the token and saved playlists.
set -e

HERE=$(cd "$(dirname "$0")" && pwd)
APP=/opt/discord-music-bot
DATA=/var/lib/discord-music-bot
CONF_DIR=/etc/discord-music-bot
ENV_FILE=$CONF_DIR/bot.env
LOG_DIR=/var/log/discord-music-bot
INSTALL_LOG=/var/log/discord-music-bot-install.log
SERVICE=discord-music-bot

step() { printf 'discord-music-bot: %s\n' "$*"; }
quiet() {
    "$@" >>"$INSTALL_LOG" 2>&1 && return 0
    tail -n 20 "$INSTALL_LOG"
    step "FAILED: $*  (full output: $INSTALL_LOG)"
    exit 1
}

[ "$(id -u)" -eq 0 ] || { echo "Run this as root (or with: doas ./install.sh / sudo ./install.sh)."; exit 1; }
[ -f /etc/alpine-release ] || { echo "This installer is for Alpine Linux. On Debian/Ubuntu use the .deb package."; exit 1; }
: >"$INSTALL_LOG"

# --- System packages ---------------------------------------------------------------------------------------------
# FFmpeg and Node.js are in Alpine's community repository, which a fresh install often has commented out.
if grep -Eq '^#\s*http.*/community\s*$' /etc/apk/repositories && ! grep -Eq '^[^#].*/community\s*$' /etc/apk/repositories; then
    step "enabling Alpine's community repository (FFmpeg and Node.js are there)"
    sed -i -E 's|^#\s*(http.*/community)\s*$|\1|' /etc/apk/repositories
fi
step "installing system packages (Python, FFmpeg, ...)"
quiet apk update
quiet apk add python3 ffmpeg curl ca-certificates libsodium libffi
# YouTube needs a JavaScript runtime. Deno where Alpine has it (64-bit), otherwise Node.js (e.g. 32-bit x86).
if apk search -x deno 2>/dev/null | grep -q '^deno-'; then
    quiet apk add deno
else
    quiet apk add nodejs
fi

# --- User, folders, settings -------------------------------------------------------------------------------------
getent group "$SERVICE" >/dev/null || addgroup -S "$SERVICE"
getent passwd "$SERVICE" >/dev/null || adduser -S -D -H -h "$DATA" -s /sbin/nologin -G "$SERVICE" "$SERVICE"
mkdir -p "$APP" "$DATA" "$CONF_DIR" "$LOG_DIR"
chown "$SERVICE:$SERVICE" "$DATA" "$LOG_DIR"
chmod 750 "$DATA" "$LOG_DIR"
[ -f "$ENV_FILE" ] || cp "$HERE/bot.env" "$ENV_FILE"
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"  # the token: root only (the service passes it to the bot as an environment variable)

# --- The bot's files ---------------------------------------------------------------------------------------------
if rc-service "$SERVICE" status >/dev/null 2>&1; then
    step "stopping the running bot"
    rc-service "$SERVICE" stop >/dev/null 2>&1 || true
fi
rm -f "$APP"/*.py
cp "$HERE"/app/*.py "$HERE/app/requirements.txt" "$HERE/VERSION" "$APP/"
cp "$HERE/uninstall.sh" "$APP/uninstall.sh"
cp "$HERE/discord-music-bot.initd" /etc/init.d/$SERVICE
cp "$HERE/discord-music-bot" /usr/bin/discord-music-bot
chmod 644 "$APP"/*.py "$APP/requirements.txt" "$APP/VERSION"
chmod 755 "$APP/uninstall.sh" /etc/init.d/$SERVICE /usr/bin/discord-music-bot

# --- Python environment ------------------------------------------------------------------------------------------
step "setting up the Python environment (downloads packages; can take a few minutes)"
[ -x "$APP/venv/bin/python" ] || quiet python3 -m venv "$APP/venv"
quiet "$APP/venv/bin/python" -m pip install --upgrade pip
pip_install() { SODIUM_INSTALL=system "$APP/venv/bin/python" -m pip install --upgrade "$@" >>"$INSTALL_LOG" 2>&1; }
# curl-cffi (optional, only helps some sites) has no build for every CPU (e.g. 32-bit x86) and can't be compiled
# here: check first, and leave it out where there's none.
reqs="$APP/requirements.txt"
rm -rf /tmp/dmb-cffi-check
if ! "$APP/venv/bin/python" -m pip download -q --only-binary=:all: --no-deps -d /tmp/dmb-cffi-check curl-cffi \
        >>"$INSTALL_LOG" 2>&1; then
    sed 's/yt-dlp\[default,curl-cffi\]/yt-dlp[default]/' "$APP/requirements.txt" >"$APP/requirements-nocffi.txt"
    reqs="$APP/requirements-nocffi.txt"
    step "(curl-cffi isn't available for this CPU; the bot works without it)"
fi
rm -rf /tmp/dmb-cffi-check
# Prebuilt packages first. Some have none for every Alpine CPU type (e.g. 32-bit x86): then compile them with
# temporary build tools.
if ! pip_install --only-binary=:all: -r "$reqs"; then
    step "compiling some Python packages for this CPU (can take 10+ minutes on a slow machine)"
    quiet apk add -t .discord-music-bot-build build-base python3-dev libffi-dev libsodium-dev
    pip_install -r "$reqs" || {
        apk del -q .discord-music-bot-build >/dev/null 2>&1 || true
        tail -n 30 "$INSTALL_LOG"; step "FAILED installing the Python packages (full output: $INSTALL_LOG)"; exit 1
    }
    apk del -q .discord-music-bot-build >/dev/null 2>&1 || true
fi
yt_ver=$("$APP/venv/bin/python" -m yt_dlp --version)
step "yt-dlp $yt_ver"

step "checking the bot loads"
quiet sh -c "cd '$APP' && '$APP/venv/bin/python' -c 'import bot'"

# --- Start --------------------------------------------------------------------------------------------------------
rc-update add "$SERVICE" default >/dev/null 2>&1 || true
if grep -Eq '^DISCORD_TOKEN=[^[:space:]]{50,}' "$ENV_FILE"; then
    rc-service "$SERVICE" restart >/dev/null 2>&1 || rc-service "$SERVICE" start
    step "installed $(cat "$APP/VERSION") and running. Live log: discord-music-bot logs"
else
    step "installed $(cat "$APP/VERSION"). Set your bot token to start it:  discord-music-bot token"
fi
exit 0
