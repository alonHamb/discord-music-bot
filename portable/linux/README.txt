Discord Music Bot - portable version for Linux (no package or installer)
=========================================================================

1. Install Python 3.10+ and FFmpeg if you don't have them. On Debian, Ubuntu or
   Raspberry Pi OS (64-bit):
     sudo apt install python3 python3-venv ffmpeg

2. In this folder, run:
     ./start.sh
   The first time, it sets up the bot in this folder (a minute or two), asks for your
   bot token, and offers to install Deno into ~/.deno. Then the bot runs in that
   terminal; press Ctrl+C to stop it. If it crashes, it restarts by itself.

   Get the token at https://discord.com/developers/applications -> your app -> Bot ->
   Reset Token. It's saved in the .env file in this folder (only you can read it).

3. Optional: keep it running in the background, also after you log out of the terminal:
     ./start.sh --autostart on
   It then runs as the systemd user service discord-music-bot-portable: it starts when you
   log in and restarts if it stops. To also start it at boot without logging in, run once:
     sudo loginctl enable-linger $USER
   Live log:  journalctl --user -u discord-music-bot-portable -f
   Undo:      ./start.sh --autostart off

Moving or deleting this folder: run "./start.sh --autostart off" first if you turned it on.

Only run one copy of the bot: two copies with the same token both answer commands.
