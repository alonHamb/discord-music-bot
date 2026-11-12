#!/usr/bin/env bash
# Run the bot from this folder for development: no package, no installer.
#
#   ./run.sh               set up whatever is missing, then run the bot here (Ctrl+C stops it)
#   ./run.sh --setup-only  set up, but don't start the bot
#
# Sets up the Python environment in .venv (packages reinstalled only when requirements.txt changes), the bot token
# in .env, and Deno; checks for FFmpeg. Also checks that no other copy of the bot is running: two copies with the
# same token both answer commands.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
SETUP_ONLY=0
[[ "${1:-}" == "--setup-only" ]] && SETUP_ONLY=1

step() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }
ask_yes() { local a; read -rp "$1 [Y/n] " a; [[ ! "$a" =~ ^([nN]|[nN][oO])$ ]]; }
ask_no()  { local a; read -rp "$1 [y/N] " a; [[ "$a" =~ ^([yY]|[yY][eE][sS])$ ]]; }

# --- Python environment ------------------------------------------------------------------------------------------
if [[ ! -x .venv/bin/python ]]; then
    step "Creating the Python environment (.venv)"
    command -v python3 >/dev/null || die "Python 3 isn't installed. On Debian/Ubuntu: sudo apt install python3 python3-venv"
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10 or newer is needed (have $(python3 --version))."
    rm -rf .venv  # e.g. a Windows .venv copied over
    python3 -m venv .venv || die "Couldn't create it. On Debian/Ubuntu: sudo apt install python3-venv"
fi

hash="$(sha256sum requirements.txt | cut -d' ' -f1)"
if [[ "$(cat .venv/.requirements.sha256 2>/dev/null)" != "$hash" ]]; then
    step "Installing the bot's Python packages"
    .venv/bin/python -m pip install -q --disable-pip-version-check --upgrade -r requirements.txt
    echo "$hash" > .venv/.requirements.sha256  # next run skips this until requirements.txt changes
fi

# --- Bot token -----------------------------------------------------------------------------------------------------
[[ -f .env ]] || cp .env.example .env
token="$(sed -n 's/^DISCORD_TOKEN=//p' .env | tr -d '\r')"
if (( ${#token} < 50 )); then
    step "Bot token"
    echo "Get it from https://discord.com/developers/applications -> your app -> Bot -> Reset Token."
    read -rsp "Paste the bot token (hidden while typing): " token; echo
    (( ${#token} >= 50 )) || die "That doesn't look like a bot token (too short)."
    read -rp "Your server ID for instant command updates (optional, press Enter to skip): " guild
    { grep -vE '^(DISCORD_TOKEN|GUILD_ID)=' .env || true; printf 'DISCORD_TOKEN=%s\nGUILD_ID=%s\n' "$token" "$guild"; } > .env.new
    mv .env.new .env
    echo "Saved in .env (Git ignores this file, so the token stays private)."
fi
chmod 600 .env  # only you can read the token

# --- FFmpeg and Deno: bot.py finds them on PATH or in ~/.deno/bin ---------------------------------------------------
command -v ffmpeg >/dev/null || die "FFmpeg is missing. Install it with your package manager, e.g.: sudo apt install ffmpeg"
if ! command -v deno >/dev/null && [[ ! -x "$HOME/.deno/bin/deno" ]]; then
    warn "Deno is missing (YouTube needs it)."
    ask_yes "Install it into ~/.deno now?" || die "Install it from https://docs.deno.com/runtime/getting_started/installation/"
    command -v unzip >/dev/null || die "The Deno installer needs unzip: sudo apt install unzip"
    echo "Installing Deno..."
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL="$HOME/.deno" sh -s -- -y --no-modify-path >/dev/null 2>&1
    [[ -x "$HOME/.deno/bin/deno" ]] || die "Installing Deno failed."
fi

if (( SETUP_ONLY )); then step "Set up. Start the bot with:  ./run.sh"; exit 0; fi

# --- Other copies of the bot ---------------------------------------------------------------------------------------
if systemctl is-active --quiet discord-music-bot 2>/dev/null; then
    warn "The installed bot (discord-music-bot service) is running with the same token. Two copies both answer commands."
    echo "Stop it with:  sudo discord-music-bot stop"
    ask_no "Run this copy anyway?" || exit 1
fi
others="$(pgrep -f "$ROOT/.venv/bin/python.* bot.py|^\.venv/bin/python bot.py" || true)"
if [[ -n "$others" ]]; then
    warn "Another copy of the bot is already running from this folder (process $(echo $others))."
    ask_yes "Stop it and start this one?" || exit 1
    kill $others 2>/dev/null || true
fi

# --- Run ------------------------------------------------------------------------------------------------------------
step "Starting the bot (Ctrl+C to stop)"
export PYTHONUNBUFFERED=1  # show its output immediately, also when it's redirected to a file
exec .venv/bin/python bot.py
