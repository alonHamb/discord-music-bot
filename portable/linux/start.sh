#!/usr/bin/env bash
# Set up (first time) and run the Discord music bot from this folder. No package or installer needed.
#   ./start.sh                  run the bot
#   ./start.sh --setup-only     only set up
#   ./start.sh --autostart on   run it as a systemd user service (--autostart off undoes it)
cd "$(dirname "$(readlink -f "$0")")" || exit 1

for py in python3 python; do
    if command -v "$py" >/dev/null && "$py" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        exec "$py" launcher.py "$@"
    fi
done
echo "Python 3.10 or newer is needed. On Debian/Ubuntu/Raspberry Pi OS:"
echo "  sudo apt install python3 python3-venv ffmpeg"
exit 1
