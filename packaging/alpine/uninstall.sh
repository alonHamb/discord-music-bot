#!/bin/sh
# Remove the Discord music bot from Alpine Linux. Keeps the token and saved playlists unless run with --purge.
set -e
[ "$(id -u)" -eq 0 ] || { echo "Run this as root."; exit 1; }
SERVICE=discord-music-bot

rc-service "$SERVICE" stop >/dev/null 2>&1 || true
rc-update del "$SERVICE" default >/dev/null 2>&1 || true
rm -f /etc/init.d/$SERVICE /usr/bin/discord-music-bot
rm -rf /opt/discord-music-bot
if [ "$1" = "--purge" ]; then
    rm -rf /var/lib/discord-music-bot /etc/discord-music-bot /var/log/discord-music-bot /var/log/discord-music-bot-install.log
    deluser "$SERVICE" >/dev/null 2>&1 || true
    delgroup "$SERVICE" >/dev/null 2>&1 || true
    echo "Removed the bot, its settings, playlists and logs."
else
    echo "Removed the bot. Kept the token (/etc/discord-music-bot) and playlists (/var/lib/discord-music-bot);"
    echo "run with --purge to delete those too."
fi
echo "(Python, FFmpeg and Node.js/Deno stay installed; remove them with apk del if nothing else needs them.)"
