"""Set up and run the Discord music bot from this folder: no installer needed. Started by start.cmd (Windows)
or start.sh (Linux).

    start                     set up whatever is missing, then run the bot (restarted if it crashes)
    start --setup-only        set up, but don't start the bot
    start --autostart on      also start the bot automatically (--autostart off undoes it):
                                Windows: when you log in.  Linux: as a systemd user service.

Uses only Python's standard library. On Windows it runs on the regular, signed Python from python.org, so it works
where Smart App Control blocks unsigned installers.
"""

import argparse
import getpass
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

WINDOWS = os.name == "nt"
ROOT = Path(__file__).resolve().parent
START = "start.cmd" if WINDOWS else "./start.sh"
VENV = ROOT / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")
ENV_FILE = ROOT / ".env"
LOCK = ROOT / ".bot.pid"
WIN_STARTUP = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup" / "Discord Music Bot.cmd"
LINUX_UNIT = "discord-music-bot-portable.service"
LINUX_UNIT_FILE = Path.home() / ".config/systemd/user" / LINUX_UNIT


def step(text: str):
    print(f"\n==> {text}", flush=True)


def fail(text: str):
    print(f"\nError: {text}", flush=True)
    sys.exit(1)


def ask_yes(question: str) -> bool:
    return input(f"{question} [Y/n] ").strip().lower() not in ("n", "no")


def ask_secret(prompt: str) -> str:
    # getpass reads the keyboard directly; when input is redirected (e.g. a script), fall back to input().
    return getpass.getpass(prompt) if sys.stdin.isatty() else input(prompt)


# --- Python environment -----------------------------------------------------------------------------------------

def venv_works() -> bool:
    """.venv exists and has pip (a failed creation can leave one without it)."""
    return VENV_PY.exists() and subprocess.run([str(VENV_PY), "-m", "pip", "--version"],
                                               capture_output=True).returncode == 0


def setup_python():
    if sys.version_info < (3, 10):
        fail(f"Python 3.10 or newer is needed (this is {sys.version.split()[0]}).")
    if not venv_works():
        step("Creating the Python environment (.venv)")
        shutil.rmtree(VENV, ignore_errors=True)  # e.g. a half-made one, or one copied from another system
        created = subprocess.run([sys.executable, "-m", "venv", str(VENV)]).returncode == 0 and venv_works()
        if not created:
            shutil.rmtree(VENV, ignore_errors=True)  # don't leave a broken one behind for the next try
            fail("Couldn't create it." + ("" if WINDOWS else " On Debian/Ubuntu: sudo apt install python3-venv"))
    requirements = ROOT / "requirements.txt"
    stamp = VENV / ".requirements.sha256"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    if not stamp.exists() or stamp.read_text().strip() != digest:
        step("Installing the bot's Python packages (a minute or two the first time)")
        result = subprocess.run([str(VENV_PY), "-m", "pip", "install", "-q", "--disable-pip-version-check",
                                 "--upgrade", "-r", str(requirements)])
        if result.returncode != 0:
            fail("Installing the packages failed (see above).")
        stamp.write_text(digest)  # next start skips this until requirements.txt changes


# --- Bot token ---------------------------------------------------------------------------------------------------

def read_env() -> dict[str, str]:
    source = ENV_FILE if ENV_FILE.exists() else ROOT / ".env.example"
    values = {}
    for line in source.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def has_token() -> bool:
    return len(read_env().get("DISCORD_TOKEN", "")) >= 50


def setup_token():
    if has_token():
        return
    step("Bot token")
    print("Get it from https://discord.com/developers/applications -> your app -> Bot -> Reset Token.")
    token = ask_secret("Paste the bot token (hidden while typing): ").strip()
    if len(token) < 50:
        fail(f"That doesn't look like a bot token (too short). Run {START} again to retry.")
    kept = [line for line in (ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else [])
            if not line.startswith("DISCORD_TOKEN=")]
    ENV_FILE.write_text("\n".join(kept + [f"DISCORD_TOKEN={token}"]) + "\n", encoding="utf-8")
    if not WINDOWS:
        ENV_FILE.chmod(0o600)  # only you can read the token
    print("Saved in .env in this folder. Keep it private: anyone with the token controls your bot.")


# --- FFmpeg and Deno ---------------------------------------------------------------------------------------------

def find_tool(name: str) -> str | None:
    """On PATH, in ~/.deno/bin, or where winget installs it (a window opened before installing doesn't see it)."""
    if found := shutil.which(name):
        return found
    home_bin = Path.home() / f".{name}" / "bin" / (f"{name}.exe" if WINDOWS else name)
    if home_bin.is_file():
        return str(home_bin)
    if WINDOWS:
        winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet"
        for candidate in [winget / "Links" / f"{name}.exe", *(winget / "Packages").glob(f"**/{name}.exe")]:
            if candidate.is_file():
                return str(candidate)
    return None


def install_with_winget(name: str, package: str):
    if not shutil.which("winget"):
        fail(f"Install {name} and run {START} again (winget, which would do it, isn't available here).")
    if not ask_yes("Install it with winget now?"):
        fail(f"Install it with:  winget install {package}")
    subprocess.run(["winget", "install", "--id", package, "-e", "--accept-source-agreements",
                    "--accept-package-agreements", "--disable-interactivity"])


def install_deno_linux():
    if not ask_yes("Install it into ~/.deno now (no root needed)?"):
        fail("Install it from https://docs.deno.com/runtime/getting_started/installation/")
    for tool in ("curl", "unzip"):
        if not shutil.which(tool):
            fail(f"The Deno installer needs {tool}. On Debian/Ubuntu: sudo apt install {tool}")
    print("Installing Deno...", flush=True)
    subprocess.run("curl -fsSL https://deno.land/install.sh | sh -s -- -y --no-modify-path", shell=True,
                   env=dict(os.environ, DENO_INSTALL=str(Path.home() / ".deno")),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def setup_tools():
    if not find_tool("ffmpeg"):
        print("\nffmpeg is missing (it plays the audio).")
        if WINDOWS:
            install_with_winget("ffmpeg", "Gyan.FFmpeg")
        else:
            fail("Install it with your package manager, e.g. on Debian/Ubuntu:  sudo apt install ffmpeg")
        if not find_tool("ffmpeg"):
            fail("Installing ffmpeg failed.")
    if not find_tool("deno"):
        print("\ndeno is missing (YouTube needs it).")
        if WINDOWS:
            install_with_winget("deno", "DenoLand.Deno")
        else:
            install_deno_linux()
        if not find_tool("deno"):
            fail("Installing deno failed.")


# --- One copy at a time --------------------------------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    if WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def claim_lock():
    if LOCK.exists():
        try:
            other = int(LOCK.read_text().strip())
        except ValueError:
            other = 0
        if other and other != os.getpid() and pid_alive(other):
            fail(f"The bot is already running from this folder (process {other}). "
                 "Two copies with the same token would both answer commands.")
    LOCK.write_text(str(os.getpid()))


# --- Start automatically -----------------------------------------------------------------------------------------

def set_autostart(on: bool):
    if on and not has_token():
        fail(f"Run {START} once first, to set up the bot and its token.")
    (autostart_windows if WINDOWS else autostart_linux)(on)


def autostart_windows(on: bool):
    if on:
        WIN_STARTUP.parent.mkdir(parents=True, exist_ok=True)
        # Text mode on Windows writes "\n" as a Windows line break.
        WIN_STARTUP.write_text(f'@echo off\nstart "Discord Music Bot" /min cmd /c ""{ROOT / "start.cmd"}""\n')
        print("The bot will start (minimized) when you log in to Windows.\n(Undo with: start.cmd --autostart off)")
    else:
        WIN_STARTUP.unlink(missing_ok=True)
        print("The bot won't start at login anymore.")


def systemctl(*args: str) -> bool:
    return subprocess.run(["systemctl", "--user", *args]).returncode == 0


def autostart_linux(on: bool):
    if not shutil.which("systemctl"):
        fail("--autostart needs systemd, which this system doesn't use.")
    if on:
        LINUX_UNIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        LINUX_UNIT_FILE.write_text(
            "[Unit]\nDescription=Discord music bot (portable)\nWants=network-online.target\n"
            "After=network-online.target\n\n"
            f'[Service]\nWorkingDirectory={ROOT}\nExecStart={sys.executable} "{ROOT / "launcher.py"}"\n'
            "Environment=PYTHONUNBUFFERED=1\nRestart=on-failure\nRestartSec=10\n\n"
            "[Install]\nWantedBy=default.target\n")
        if not (systemctl("daemon-reload") and systemctl("enable", "--now", LINUX_UNIT)):
            fail("Couldn't start the service (see above).")
        print(f"The bot now runs as the systemd user service {LINUX_UNIT}: it starts when you log in and "
              f"restarts if it stops.\n  Live log:  journalctl --user -u {LINUX_UNIT} -f\n"
              f"  Undo:      ./start.sh --autostart off")
        linger = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"],
                                capture_output=True, text=True).stdout.strip()
        if linger != "yes":
            print("To also start it at boot without logging in, run once:  sudo loginctl enable-linger $USER")
    else:
        systemctl("disable", "--now", LINUX_UNIT)
        LINUX_UNIT_FILE.unlink(missing_ok=True)
        systemctl("daemon-reload")
        print("The bot doesn't start automatically anymore.")


# --- Run -------------------------------------------------------------------------------------------------------------

def run_bot():
    claim_lock()
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    for name, var in (("ffmpeg", "FFMPEG_PATH"), ("deno", "DENO_PATH")):
        env.setdefault(var, find_tool(name) or "")
    delay = 10
    try:
        while True:
            step(f"Starting the bot ({'close this window or ' if WINDOWS else ''}press Ctrl+C to stop)")
            started = time.monotonic()
            code = subprocess.call([str(VENV_PY), str(ROOT / "bot.py")], cwd=ROOT, env=env)
            # Quick repeated failures (e.g. a wrong token) back off up to 5 minutes; a long run resets that.
            delay = 10 if time.monotonic() - started > 120 else min(delay * 2, 300)
            print(f"\nThe bot stopped (exit code {code}). Restarting in {delay} seconds; Ctrl+C to quit.", flush=True)
            time.sleep(delay)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        LOCK.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Set up and run the Discord music bot from this folder.")
    parser.add_argument("--setup-only", action="store_true", help="set up, but don't start the bot")
    parser.add_argument("--autostart", choices=["on", "off"], help="start the bot automatically")
    args = parser.parse_args()
    if args.autostart:
        set_autostart(args.autostart == "on")
        return
    setup_python()
    setup_token()
    setup_tools()
    if args.setup_only:
        print(f"\nSet up. Start the bot with:  {START}")
        return
    run_bot()


if __name__ == "__main__":
    main()
