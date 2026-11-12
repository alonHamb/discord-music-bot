#!/usr/bin/env bash
# Set up the Discord music bot on Linux and run it as a systemd service (starts on boot, restarts on crash).
#
# Usage, from the bot's folder, as your normal user (not root; it asks for sudo when needed):
#   bash setup.sh               install everything and start the service
#   bash setup.sh --no-service  install everything, but don't create the service
#
# Safe to run again: it updates the packages, keeps your .env and playlists, and restarts the bot.
# Supports Debian / Ubuntu / Raspberry Pi OS (64-bit), Fedora and Arch.

set -euo pipefail

SERVICE=discord-music-bot
BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WANT_SERVICE=1
[[ "${1:-}" == "--no-service" ]] && WANT_SERVICE=0

LOG="$BOT_DIR/setup.log"
: > "$LOG"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }
# Run a noisy command with its output going to setup.log; show the end of the log if it fails.
quiet() {
    "$@" >>"$LOG" 2>&1 && return
    echo; tail -n 25 "$LOG"
    die "This step failed: $*  (full output in $LOG)"
}

# --- Checks ------------------------------------------------------------------------------------------------------

[[ $EUID -eq 0 ]] && die "Run this as your normal user, not root or sudo. It asks for sudo itself when needed."
for f in bot.py matching.py streaming_links.py requirements.txt; do
    [[ -f "$BOT_DIR/$f" ]] || die "$f is missing. Copy the whole bot folder here first."
done

case "$(uname -m)" in
    x86_64|amd64|aarch64|arm64) ;;
    armv7l|armv6l) die "32-bit ARM isn't supported (no builds of Deno and some Python packages). On a Raspberry Pi, install the 64-bit Raspberry Pi OS." ;;
    *) warn "Untested CPU type $(uname -m); continuing anyway." ;;
esac

# --- System packages ---------------------------------------------------------------------------------------------

echo "This installs system packages, so sudo may ask for your password."
sudo true || die "sudo is needed to install packages."  # not "sudo -v": that wants a password even with NOPASSWD

say "Installing system packages (Python, FFmpeg, curl, unzip); this can take a few minutes"
if command -v apt-get >/dev/null; then
    quiet sudo apt-get update
    # No "recommended" extras: they'd add a C++ compiler, GTK and fonts that the bot never uses.
    quiet sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 python3-venv ffmpeg curl unzip ca-certificates
elif command -v dnf >/dev/null; then
    # Fedora's own repos ship "ffmpeg-free"; the full ffmpeg (RPM Fusion) works too.
    command -v ffmpeg >/dev/null || sudo dnf install -y ffmpeg-free >>"$LOG" 2>&1 || quiet sudo dnf install -y ffmpeg
    quiet sudo dnf install -y python3 curl unzip
elif command -v pacman >/dev/null; then
    quiet sudo pacman -Sy --needed --noconfirm python ffmpeg curl unzip deno
else
    warn "Unknown package manager. Make sure python3 (3.10+), ffmpeg, curl and unzip are installed."
fi

command -v ffmpeg >/dev/null || die "FFmpeg didn't install. Install it with your package manager and run this again."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || die "Python 3.10 or newer is needed; this system has $(python3 --version 2>&1). Upgrade the OS or install a newer Python."

# --- Deno (YouTube needs a JavaScript runtime) -------------------------------------------------------------------

if command -v deno >/dev/null; then
    say "Deno already installed: $(deno --version | sed -n 1p)"
elif [[ -x "$HOME/.deno/bin/deno" ]]; then
    say "Updating Deno"
    "$HOME/.deno/bin/deno" upgrade -q >>"$LOG" 2>&1 || warn "Deno upgrade failed; keeping the current version."
else
    say "Installing Deno into ~/.deno"
    quiet sh -c 'curl -fsSL https://deno.land/install.sh | DENO_INSTALL="$HOME/.deno" sh -s -- -y --no-modify-path'
    [[ -x "$HOME/.deno/bin/deno" ]] || die "Deno didn't install. See https://docs.deno.com/runtime/getting_started/installation/"
fi

# --- Python environment ------------------------------------------------------------------------------------------

say "Creating the Python environment and installing the bot's packages"
cd "$BOT_DIR"
if [[ -d .venv && ! -x .venv/bin/python ]]; then
    warn "Removing a .venv that isn't a Linux environment (probably copied over from Windows)."
    rm -rf .venv
fi
[[ -d .venv ]] || quiet python3 -m venv .venv
quiet .venv/bin/python -m pip install --upgrade pip
quiet .venv/bin/python -m pip install --upgrade -r requirements.txt
.venv/bin/python -c "import bot" || die "The bot failed to load; see the error above."

# --- Bot token ---------------------------------------------------------------------------------------------------

token_ok() { [[ -f .env ]] && grep -Eq '^DISCORD_TOKEN=[^[:space:]]{50,}' .env && ! grep -q 'paste-your' .env; }

if token_ok; then
    say "Using the bot token already in .env"
else
    say "Bot token"
    echo "Get it from https://discord.com/developers/applications -> your app -> Bot -> Reset Token."
    read -rsp "Paste the bot token (hidden while typing): " token; echo
    [[ ${#token} -ge 50 ]] || die "That doesn't look like a bot token (too short). Run this script again to retry."
    read -rp "Your server ID for instant command updates (optional, press Enter to skip): " guild_id
    printf 'DISCORD_TOKEN=%s\nGUILD_ID=%s\n' "$token" "$guild_id" > .env
fi
chmod 600 .env  # only you can read the token

# --- systemd service ---------------------------------------------------------------------------------------------

if [[ $WANT_SERVICE -eq 0 ]]; then
    say "Done. Start the bot with:  cd \"$BOT_DIR\" && .venv/bin/python bot.py"
    exit 0
fi
if ! command -v systemctl >/dev/null || [[ ! -d /run/systemd/system ]]; then
    warn "systemd isn't running here, so the bot can't start automatically."
    say "Done. Start the bot with:  cd \"$BOT_DIR\" && .venv/bin/python bot.py"
    exit 0
fi

say "Installing the $SERVICE service (starts on boot, restarts if it crashes)"
sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<EOF
[Unit]
Description=Discord music bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/.venv/bin/python bot.py
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null 2>&1
started_at="$(date '+%Y-%m-%d %H:%M:%S')"
sudo systemctl restart "$SERVICE"

say "Checking that the bot logs in (usually a few seconds; up to a minute right after a restart)"
recent_log() { sudo journalctl -u "$SERVICE" --since "$started_at" --no-pager -q 2>/dev/null || true; }
for _ in $(seq 1 90); do
    if recent_log | grep -q "Logged in as"; then
        recent_log | grep -E "Logged in as|ffmpeg:" | sed 's/^.*\]: //'
        cat <<EOF

The bot is running and will start automatically on boot.

  Live log:   sudo journalctl -u $SERVICE -f
  Restart:    sudo systemctl restart $SERVICE
  Stop:       sudo systemctl stop $SERVICE
  Status:     systemctl status $SERVICE
  Update:     copy the new bot files here, then run  bash setup.sh  again

Make sure the bot isn't also running on another computer, or both will answer commands.
EOF
        exit 0
    fi
    if recent_log | grep -qE "LoginFailure|Improper token"; then
        die "Discord rejected the token. Fix DISCORD_TOKEN in $BOT_DIR/.env, then: sudo systemctl restart $SERVICE"
    fi
    sleep 1
done
warn "The bot hasn't logged in yet. Check the log with:  sudo journalctl -u $SERVICE -e"
