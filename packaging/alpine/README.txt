Discord music bot for Alpine Linux
==================================

Install (as root, needs internet):

    tar -xzf discord-music-bot-<version>-alpine.tar.gz
    cd discord-music-bot-<version>-alpine
    ./install.sh
    discord-music-bot token

install.sh installs Python, FFmpeg and a JavaScript runtime for YouTube (Deno where Alpine has it, otherwise
Node.js) with apk, enabling Alpine's community repository if needed. It sets up the bot in /opt/discord-music-bot
and runs it as the OpenRC service "discord-music-bot", which starts on boot and restarts if it crashes.
On CPUs without prebuilt Python packages (e.g. 32-bit x86) it compiles a few of them, which can take a while.

Upgrade: extract the newer version and run its ./install.sh. The token and saved playlists are kept.

Manage it with:  discord-music-bot   (token, logs, status, restart, stop, start, disable, enable,
                                      update-ytdlp, uninstall, version)

Files:
    /etc/discord-music-bot/bot.env         settings (the token)
    /var/lib/discord-music-bot/            saved playlists and playback state
    /var/log/discord-music-bot/bot.log     the bot's log
