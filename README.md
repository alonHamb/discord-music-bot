# Discord music bot

Plays YouTube, SoundCloud, Spotify and Apple Music links (Spotify/Apple songs are matched to YouTube) with a queue,
loop, shuffle and saved playlists. Type `/help` in Discord for the command list.

## Building installers

After changing the code, build new installers with `build.ps1` (PowerShell, in this folder):

```
.\build.ps1                    # asks: Windows, Linux or both, and the version (suggests the next one)
.\build.ps1 windows 1.0.1      # Windows Setup.exe only
.\build.ps1 linux 1.0.1        # Linux .deb only
.\build.ps1 all 1.0.1          # both
```

They land in `dist/`. Always use a **higher version** than what's installed, or the upgrade does nothing; the
last version built is kept in `VERSION`. Rebuilding the same version overwrites its installer. If `dist/` has
installers of older versions for the platforms you're building, the script lists them and asks whether to delete
them once the new build succeeds (Enter = yes; add `-Yes` to skip the question). A failed build deletes nothing. Every `.py` file in this folder plus `requirements.txt` goes in, so new
files and new packages are picked up automatically. The Windows build needs Inno Setup
(`winget install JRSoftware.InnoSetup`); the Linux build needs WSL with Debian or Ubuntu (`-WslDistro Ubuntu` to
pick another one). Test your change on Windows first (`.venv\Scripts\python.exe bot.py`) before building.

## Install on Windows

Run `dist\discord-music-bot-<version>-windows-setup.exe` (needs admin). It asks for the bot token, then downloads
its own private Python, FFmpeg and Deno into the install folder (a few minutes; needs internet) and runs the bot in
the background from boot, with no window, as Windows' restricted LOCAL SERVICE account. It restarts the bot if it
crashes.

Stop the development copy (`python bot.py`) while the installed one runs: two copies with the same token both
answer commands.

| | |
|---|---|
| Set or change the token | Start menu → Discord Music Bot → Set bot token, or `discord-music-bot token` |
| Live log | Start menu → View live log, or `discord-music-bot logs` |
| Running? | Start menu → Bot status, or `discord-music-bot status` |
| Restart | Start menu → Restart bot, or `discord-music-bot restart` |
| Songs stopped loading | `discord-music-bot update-ytdlp` |
| Stop / start | Start menu → Stop bot / Start bot, or `discord-music-bot stop` / `start` (a stopped bot starts again with Windows) |
| Don't start with Windows | Start menu → Disable bot, or `discord-music-bot disable` (Enable bot / `enable` undoes it) |
| Upgrade | run the newer Setup.exe: it says which version is installed and, once you agree, fully uninstalls it before installing the new one (keeps the token and playlists; downloads Python, FFmpeg and Deno again, a few minutes) |
| Uninstall | Settings → Apps → Discord Music Bot (asks whether to keep the token and playlists) |

Commands that change something ask for admin permission. The Start menu entries also show up when you search
Windows for "bot". In a terminal that was already open before installing, `discord-music-bot` isn't found yet:
open a new terminal, or use the full path `"C:\Program Files\Discord Music Bot\discord-music-bot.cmd"`. Where things live: the program in
`C:\Program Files\Discord Music Bot`; token, playlists and logs in `C:\ProgramData\Discord Music Bot`
(the token file is readable only by administrators and the bot). If setup fails, its log is
`C:\ProgramData\Discord Music Bot\install.log`; running the installer again retries.

## Install as a package (Debian, Ubuntu, Raspberry Pi OS 64-bit)

The easiest way. Copy `dist/discord-music-bot_1.0.0_all.deb` to the Linux machine, then:

```
sudo apt install --no-install-recommends ./discord-music-bot_1.0.0_all.deb
sudo discord-music-bot token
```

The first command installs Python and FFmpeg if needed, plus the bot's packages and Deno (needs internet, about 1–2
minutes). The second asks for your bot token and starts the bot; it then starts automatically on boot and restarts
if it crashes. Leaving out `--no-install-recommends` also works but installs ~270 extra packages the bot doesn't use.

Stop the bot on your Windows PC once it's running on Linux: two copies with the same token both answer commands.

| | |
|---|---|
| All commands | `discord-music-bot` |
| Live log | `sudo discord-music-bot logs` |
| Running? | `discord-music-bot status` |
| Restart | `sudo discord-music-bot restart` |
| Songs stopped loading | `sudo discord-music-bot update-ytdlp` |
| Upgrade | `sudo apt install ./discord-music-bot_<new version>_all.deb` (keeps token and playlists) |
| Uninstall, keep settings | `sudo apt remove discord-music-bot` |
| Uninstall everything | `sudo apt purge discord-music-bot` (also deletes the token and saved playlists) |

Where things live: settings and token in `/etc/discord-music-bot/bot.env` (root only), saved playlists in
`/var/lib/discord-music-bot/playlists.json`, the program in `/opt/discord-music-bot`. The bot runs as its own
locked-down `discord-music-bot` user that can only write to its playlists folder.

## Run it on a Linux machine with the setup script

For Fedora and Arch, or if you'd rather not use a package. Works on 64-bit Debian, Ubuntu, Raspberry Pi OS
(64-bit), Fedora and Arch. Needs about 1 GB of disk and 512 MB+ of RAM.

1. **Copy the bot to the Linux machine.** Only these files are needed:
   `bot.py`, `matching.py`, `streaming_links.py`, `requirements.txt`, `setup.sh`, plus `.env` (your token) and
   `playlists.json` (saved playlists) if you have them. Don't copy `.venv`. From Windows PowerShell, for example:

   ```
   ssh you@linux-machine "mkdir -p ~/discord-bot"
   scp bot.py matching.py streaming_links.py requirements.txt setup.sh .env you@linux-machine:~/discord-bot/
   ```

2. **Run the setup script on the Linux machine**, as your normal user (it asks for your sudo password):

   ```
   cd ~/discord-bot
   bash setup.sh
   ```

   It installs Python, FFmpeg and Deno, sets up the bot's packages, asks for the bot token if there's no `.env`,
   and installs a `discord-music-bot` service that starts on boot and restarts the bot if it crashes.

3. **Stop the bot on your Windows PC** (Ctrl+C in its terminal). If two copies run with the same token, both
   answer commands and things break.

### Day to day

| | |
|---|---|
| Live log | `sudo journalctl -u discord-music-bot -f` |
| Restart | `sudo systemctl restart discord-music-bot` |
| Stop / start | `sudo systemctl stop discord-music-bot` / `start` |
| Update the bot | copy the new `.py` files over, then run `bash setup.sh` again |
| Update yt-dlp only (if songs stop loading) | `.venv/bin/pip install -U yt-dlp && sudo systemctl restart discord-music-bot` |

`bash setup.sh --no-service` installs everything without the service; then run `.venv/bin/python bot.py` yourself.

### Troubleshooting

- **`$'\r': command not found`** when running the script: the file got Windows line endings. Fix with
  `sed -i 's/\r$//' setup.sh` and run it again.
- **"Sign in to confirm you're not a bot"** in the log: YouTube is blocking this internet connection. This mostly
  happens on cloud servers, not home connections.
- **32-bit Raspberry Pi OS** isn't supported. Install the 64-bit version.
