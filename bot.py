"""A small Discord music bot: slash commands, per-server queues, playback via yt-dlp + FFmpeg.

Plays YouTube and SoundCloud directly; Spotify and Apple Music songs are read from their links (streaming_links.py)
and matched to the best YouTube upload (matching.py).
"""

import asyncio
import json
import os
import queue
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import discord
import yt_dlp
from discord import app_commands
from dotenv import load_dotenv

import matching
import platform_links
import streaming_links

load_dotenv(os.getenv("BOT_ENV_FILE"))  # the Windows installer points this at its settings file; default: .env
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional: a server whose old server-only command copies get cleared


def find_exe(name: str) -> str | None:
    """Find a program on PATH, in ~/.deno/bin (Linux setup.sh), or where winget installs it on Windows."""
    if found := shutil.which(name):
        return found
    if (home_bin := Path.home() / f".{name}" / "bin" / name).is_file():
        return str(home_bin)
    winget = Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "WinGet"
    for candidate in [winget / "Links" / f"{name}.exe", *(winget / "Packages").glob(f"**/{name}.exe")]:
        if candidate.is_file():
            return str(candidate)
    return None


FFMPEG = os.getenv("FFMPEG_PATH") or find_exe("ffmpeg")
DENO = os.getenv("DENO_PATH") or find_exe("deno")

MAX_PLAYLIST = 200  # most tracks one /play can add from a playlist

YDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,  # a video link that's also in a playlist plays just that video
    "extract_flat": "in_playlist",  # list playlist/search entries without resolving each one (fast)
    "quiet": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "js_runtimes": {"deno": {"path": DENO}},  # YouTube needs a JS runtime to unlock formats
}


PREFETCH_LEAD = 25  # seconds before a song ends to start preparing the next one
PREFETCH_POLL = 2  # how often (seconds) to check whether the playing song is that close to its end
# Play every song at about the same loudness (EBU R128 loudness normalization, as streaming services do); /volume
# then applies on top. -16 LUFS leaves headroom so volumes above 100% don't distort right away. Measured: songs
# that differed by ~20 dB end up within ~4 dB, for ~2% of a CPU core while playing.
NORMALIZE_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"


def open_stream(url: str, fast: bool = True) -> subprocess.Popen:
    """Have yt-dlp download the audio to stdout. YouTube blocks FFmpeg fetching stream URLs directly (403).

    fast skips YouTube's TV client: YouTube often attaches an ad to it, and yt-dlp then sits through the ad in
    silence ("Sleeping 4 seconds as required by the site"). That cuts the time to first audio from ~8 s to ~2 s.
    """
    cmd = [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-playlist", "-f", "bestaudio/best", "-o", "-"]
    if DENO:
        cmd += ["--js-runtimes", f"deno:{DENO}"]
    if fast:
        cmd += ["--extractor-args", "youtube:player_client=default,-tv_samsung"]
    return subprocess.Popen(cmd + [url], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


class PipedFFmpegAudio(discord.FFmpegPCMAudio):
    """FFmpeg reading yt-dlp's output directly, like `yt-dlp | ffmpeg`.

    discord.py's own pipe=True copies the data through a helper thread, which crashes with a traceback whenever a
    song is stopped or skipped mid-copy (it writes to an FFmpeg that cleanup() already removed).
    """

    def __init__(self, downloader: subprocess.Popen, **kwargs):
        self._downloader = downloader
        super().__init__("pipe:0", **kwargs)  # pipe=False: no helper thread; stdin is swapped in below

    def _spawn_process(self, args, **kwargs):
        kwargs["stdin"] = self._downloader.stdout
        return super()._spawn_process(args, **kwargs)


class BufferedAudio(discord.AudioSource):
    """Decodes ahead in its own thread, keeping up to `ahead` 20 ms frames ready.

    discord.py starts its 20 ms send clock the moment play() is called. If the first audio then takes seconds to
    arrive (yt-dlp start-up), or the download stalls mid-song, it sends the whole backlog at once and Discord plays
    it sped up. So playback only starts once audio is buffered (wait_ready), and the buffer rides out short stalls.
    """

    def __init__(self, inner: discord.AudioSource, ahead: int = 250, start: float = 0):
        self._inner = inner
        self.start = start  # where in the song the audio begins (seconds), when resuming partway through
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=ahead)
        self._stop = threading.Event()
        self._ended = threading.Event()
        self.frames_in = 0  # decoded so far
        self.frames_out = 0  # handed to Discord so far
        threading.Thread(target=self._fill, daemon=True, name="audio-buffer").start()

    @property
    def played_seconds(self) -> float:
        return self.start + self.frames_out * 0.02

    @property
    def failed(self) -> bool:
        """The stream ended without producing any audio (e.g. yt-dlp couldn't get the song)."""
        return self._ended.is_set() and self.frames_in == 0

    def _fill(self):
        while not self._stop.is_set():
            try:
                data = self._inner.read()
            except Exception:  # the stream was cleaned up under us (song skipped)
                data = b""
            if data:
                self.frames_in += 1
            while not self._stop.is_set():
                try:
                    self._frames.put(data, timeout=0.5)
                    break
                except queue.Full:
                    continue
            if not data:
                self._ended.set()
                return

    def wait_ready(self, frames: int = 50, timeout: float = 60):
        """Block until `frames` (default 1 s) of audio are ready, the stream has ended, or `timeout` passes."""
        deadline = time.monotonic() + timeout
        while self._frames.qsize() < frames and not self._ended.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)

    def read(self) -> bytes:
        try:
            data = self._frames.get(timeout=30)  # buffer ran dry: wait, as an unbuffered stream would
        except queue.Empty:
            return b""  # nothing for 30 s: give up on this song
        if data:
            self.frames_out += 1
        return data

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        self._stop.set()
        self._inner.cleanup()


class Stream:
    """A song being downloaded and decoded (yt-dlp -> FFmpeg -> buffer), ready to hand to Discord."""

    def __init__(self, url: str, fast: bool = True, start: float = 0):
        self.downloader = open_stream(url, fast)
        # Starting partway (resuming after a restart): FFmpeg reads the song from the beginning and drops the audio
        # before `start`, since a pipe can't seek.
        skip = f"-ss {start:.2f} " if start else ""
        self.audio = BufferedAudio(PipedFFmpegAudio(self.downloader, executable=FFMPEG, before_options="-loglevel error",
                                                    options=f"-vn {skip}-af {NORMALIZE_FILTER}"), start=start)

    def close(self):
        self.audio.cleanup()
        self.downloader.kill()

    @classmethod
    async def open(cls, track: "Track") -> "Stream":
        """Match the song if needed, start it, and wait until 1 s of audio is ready."""
        if track.needs_match:
            await track.start_matching()
        start = track.resume_at
        stream = cls(track.page_url, start=start)
        await asyncio.to_thread(stream.audio.wait_ready)
        if stream.audio.failed:  # the fast way got nothing (YouTube hiccup): retry the usual, slower way
            stream.close()
            print(f"Fast start failed for {track.title!r}; retrying the slower way.")
            stream = cls(track.page_url, fast=False, start=start)
            await asyncio.to_thread(stream.audio.wait_ready)
        return stream


def close_when_done(task: asyncio.Task):
    """Close the Stream a preparing task produces, whenever it finishes (it can't be cancelled midway safely)."""
    def done(t: asyncio.Task):
        if not t.cancelled() and t.exception() is None:
            t.result().close()
    task.add_done_callback(done)


@dataclass
class Track:
    title: str
    page_url: str  # what yt-dlp plays: a URL, or a "ytsearch1:..." fallback for Spotify/Apple Music songs
    duration: int | None
    requested_by: str
    link: str | None = None  # the song's page on Spotify/Apple Music, shown instead of the search
    artist: str | None = None  # Spotify/Apple Music songs: matched to the best YouTube video before playing
    song: str | None = None
    _match: asyncio.Task | None = field(default=None, repr=False, compare=False)
    links: dict[str, str] | None = field(default=None, repr=False, compare=False)  # /playing's links, once looked up
    resume_at: float = field(default=0, repr=False, compare=False)  # start here (seconds): continuing after a restart

    @property
    def web_url(self) -> str | None:
        return self.link or (self.page_url if self.page_url.startswith("http") else None)

    @property
    def needs_match(self) -> bool:
        return self.song is not None and not self.page_url.startswith("http")

    def start_matching(self) -> asyncio.Task:
        """Find the best YouTube video for this song (once; later calls share the same lookup)."""
        if self._match is None:
            self._match = asyncio.create_task(self._find_match())
        return self._match

    async def _find_match(self):
        try:
            found = await matching.best_match(self.artist or "", self.song, self.duration, YDL_OPTIONS)
        except Exception as e:
            print(f"Matching failed for {self.title!r}: {e!r}")
            found = None
        if found:
            self.page_url = found[0]  # otherwise the "ytsearch1:" fallback plays YouTube's top result

    @property
    def length(self) -> str:
        return clock(self.duration) if self.duration else "?:??"


def songs(n: int) -> str:
    return f"{n} song" if n == 1 else f"{n} songs"


def clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


class GuildPlayer:
    def __init__(self):
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.volume = 1.0
        self.loop = "off"  # "off", "track" or "queue"
        self.skipping = False  # set by /skip so a looped track moves on
        self.replaying = False  # set by /replay so the current song plays again from the start
        self.prefetched: tuple[Track, asyncio.Task] | None = None  # the next song, being prepared ahead of time
        self.channel: discord.abc.Messageable | None = None  # where songs were last queued from: "Now playing" goes here
        self.announced: Track | None = None  # the last song announced, so a replay or looped song isn't announced again
        self.resume_paused = False  # continuing a song that was paused before a restart: start it paused

    def reset(self):
        # Clearing current first keeps the after-callback from looping it back in.
        self.queue.clear()
        self.current = None
        self.discard_prefetch()

    def peek_next(self) -> Track | None:
        """The song that will play after the current one, if nothing changes."""
        if self.loop == "track" and self.current:
            return self.current
        if self.queue:
            return self.queue[0]
        return self.current if self.loop == "queue" else None

    def take_prefetch(self, track: Track) -> asyncio.Task | None:
        """The prepared stream for `track`, if that's what was prepared; anything else is thrown away."""
        if self.prefetched and self.prefetched[0] is track:
            task, self.prefetched = self.prefetched[1], None
            return task
        self.discard_prefetch()
        return None

    def discard_prefetch(self):
        if self.prefetched:
            close_when_done(self.prefetched[1])
            self.prefetched = None


async def fetch_tracks(query: str, requester: str) -> tuple[list[Track], str | None]:
    """Resolve a URL or search into tracks. Returns (tracks, playlist title or None)."""
    if streaming_links.is_supported(query):
        # Spotify/Apple Music can't be streamed: keep each song's name and find it on YouTube when it plays.
        songs, collection = await streaming_links.resolve(query)
        tracks = [
            Track(title=s.search, page_url=f"ytsearch1:{s.search} audio", duration=s.duration, requested_by=requester,
                  link=s.link, artist=s.artist, song=s.title)
            for s in songs[:MAX_PLAYLIST]
        ]
        return tracks, collection

    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        info = await asyncio.to_thread(ydl.extract_info, query, download=False)

    playlist = None
    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        if info.get("extractor_key", "").startswith("YoutubeSearch") or not query.startswith(("http://", "https://")):
            entries = entries[:1]  # a search: take the top result
        else:
            playlist = info.get("title") or "playlist"
            entries = entries[:MAX_PLAYLIST]
            if any(not e.get("title") for e in entries):
                # SoundCloud's quick listing can omit titles: look each track up fully (slower, but rare).
                full_opts = dict(YDL_OPTIONS, extract_flat=False, playlistend=MAX_PLAYLIST)
                with yt_dlp.YoutubeDL(full_opts) as ydl:
                    full = await asyncio.to_thread(ydl.extract_info, query, download=False)
                entries = [e for e in full.get("entries") or [] if e]
    else:
        entries = [info]

    tracks = [
        Track(
            title=e.get("title") or "Unknown",
            page_url=e.get("webpage_url") or e.get("url") or query,
            duration=e.get("duration"),
            requested_by=requester,
        )
        for e in entries
    ]
    if not tracks:
        raise ValueError("no results")
    return tracks, playlist


PLAYLISTS_FILE = Path(os.getenv("PLAYLISTS_FILE") or Path(__file__).with_name("playlists.json"))  # .deb sets it
MAX_SAVED_TRACKS = 500


class PlaylistStore:
    """Saved playlists, per server, kept in playlists.json: {guild_id: {key: {name, owner_id, tracks: [...]}}}."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict[str, dict]] = json.loads(path.read_text("utf-8")) if path.exists() else {}

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(self.path)  # atomic, so a crash mid-write can't corrupt the file

    def guild(self, guild_id: int) -> dict[str, dict]:
        return self.data.setdefault(str(guild_id), {})

    def get(self, guild_id: int, name: str) -> dict | None:
        return self.guild(guild_id).get(name.strip().lower())


playlists = PlaylistStore(PLAYLISTS_FILE)


def track_to_dict(t: Track) -> dict:
    return {"title": t.title, "url": t.page_url, "duration": t.duration, "link": t.link, "artist": t.artist, "song": t.song}


def track_from_dict(d: dict, requester: str) -> Track:
    artist, song = d.get("artist"), d.get("song")
    if song is None and d["url"].startswith("ytsearch1:") and " - " in d["title"]:
        artist, song = d["title"].split(" - ", 1)  # saved before smart matching existed: "Artist - Title"
    return Track(title=d["title"], page_url=d["url"], duration=d.get("duration"), requested_by=requester,
                 link=d.get("link"), artist=artist, song=song)


class MusicBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.players: dict[int, GuildPlayer] = {}
        self.shutting_down = False
        self.restored = False

    async def setup_hook(self):
        # Register the commands globally, for every server the bot is in (Discord applies changes within moments).
        await self.tree.sync()
        if GUILD_ID:
            # Older versions also registered a copy just in this server, so members saw every command twice.
            # Clear that copy (a no-op once it's gone).
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.clear_commands(guild=guild)
            await self.tree.sync(guild=guild)
        # Save what's playing when asked to stop (systemd, Ctrl+C), so the next start continues where it left off.
        for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), lambda *_: self.loop.call_soon_threadsafe(self.shut_down))

    def shut_down(self):
        if self.shutting_down:
            return
        save_state()  # first: closing the connection ends the songs
        self.shutting_down = True
        print("Shutting down; saved what was playing.")
        asyncio.create_task(self.close())

    def player(self, guild_id: int) -> GuildPlayer:
        return self.players.setdefault(guild_id, GuildPlayer())

    def play_next(self, guild: discord.Guild):
        if self.shutting_down:  # the song stopped because we're shutting down: keep the queue as it was saved
            return
        player = self.player(guild.id)
        finished, player.current = player.current, None
        if finished:
            if player.replaying:
                player.queue.appendleft(finished)
            elif player.loop == "track" and not player.skipping:
                player.queue.appendleft(finished)
            elif player.loop == "queue":
                player.queue.append(finished)
        player.skipping = player.replaying = False

        vc = guild.voice_client
        if not vc or not player.queue:
            player.current = None
            return
        track = player.current = player.queue.popleft()
        if player.queue and player.queue[0].needs_match:
            player.queue[0].start_matching()  # get the next song ready while this one plays
        asyncio.create_task(self._prepare_and_play(guild, track))

    async def _prepare_and_play(self, guild: discord.Guild, track: Track):
        player = self.player(guild.id)

        def still_wanted() -> bool:  # skip, /stop or /leave may have happened while we were getting ready
            vc = guild.voice_client
            return player.current is track and vc is not None and not vc.is_playing() and not vc.is_paused()

        stream = None
        prepared = player.take_prefetch(track)
        if prepared:  # got ready while the previous song played: starts with no gap
            try:
                stream = await prepared
            except Exception as e:
                print(f"Preparing {track.title!r} ahead failed ({e!r}); starting it now instead.")
        if stream is None:
            stream = await Stream.open(track)
        if not still_wanted():
            stream.close()
            return

        def after(err):
            stream.downloader.kill()  # stop downloading when the song ends or is skipped
            if err:
                print(f"Playback error: {err!r}")
            self.loop.call_soon_threadsafe(self.play_next, guild)

        guild.voice_client.play(discord.PCMVolumeTransformer(stream.audio, volume=player.volume), after=after)
        track.resume_at = 0  # a replay or loop starts from the beginning
        if player.resume_paused:
            player.resume_paused = False
            guild.voice_client.pause()
        asyncio.create_task(self._prefetch_near_end(guild, track, stream.audio))
        if player.announced is not track:
            player.announced = track
            asyncio.create_task(self._announce(player, track))

    async def _announce(self, player: GuildPlayer, track: Track):
        if not player.channel:
            return
        try:
            await player.channel.send(now_playing_text(track), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            print(f"Couldn't announce {track.title!r}: {e!r}")

    async def _prefetch_near_end(self, guild: discord.Guild, track: Track, audio: BufferedAudio):
        """When the playing song is about to end, start preparing the next one so it follows with no gap."""
        player = self.player(guild.id)
        while player.current is track and guild.voice_client and (guild.voice_client.is_playing() or guild.voice_client.is_paused()):
            remaining = (track.duration or 0) - audio.played_seconds  # unknown length: prepare right away
            if remaining <= PREFETCH_LEAD:
                upcoming = player.peek_next()
                if upcoming and not player.prefetched:
                    player.prefetched = (upcoming, asyncio.create_task(Stream.open(upcoming)))
                return
            await asyncio.sleep(PREFETCH_POLL)


bot = MusicBot()


# What's playing in each server, saved every few seconds and on shutdown, so a restart (or a crash) continues
# the same songs from the same spot. Kept next to playlists.json.
STATE_FILE = PLAYLISTS_FILE.with_name("state.json")
STATE_SAVE_EVERY = 5  # seconds
STATE_MAX_AGE = 3600  # seconds: after a longer break, start fresh instead of suddenly playing again
last_saved_state: str | None = None


def playback_state() -> dict:
    guilds = {}
    for guild in bot.guilds:
        player, vc = bot.players.get(guild.id), guild.voice_client
        if not player or not player.current or not vc or not vc.channel:
            continue
        source = vc.source
        audio = source.original if isinstance(source, discord.PCMVolumeTransformer) else None
        playing = isinstance(audio, BufferedAudio) and (vc.is_playing() or vc.is_paused())
        guilds[str(guild.id)] = {
            "voice_channel": vc.channel.id,
            "text_channel": getattr(player.channel, "id", None),
            "position": round(audio.played_seconds if playing else player.current.resume_at, 2),
            "paused": vc.is_paused() or (not playing and player.resume_paused),
            "loop": player.loop,
            "volume": player.volume,
            "tracks": [dict(track_to_dict(t), requested_by=t.requested_by) for t in [player.current, *player.queue]],
        }
    return guilds


def save_state():
    global last_saved_state
    text = json.dumps(playback_state(), ensure_ascii=False)
    if text == last_saved_state:
        return
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"saved_at": time.time(), "guilds": json.loads(text)}, indent=1, ensure_ascii=False), "utf-8")
        tmp.replace(STATE_FILE)
        last_saved_state = text
    except OSError as e:
        print(f"Couldn't save the playback state to {STATE_FILE}: {e!r}")


async def keep_saving_state():
    while not bot.shutting_down:
        save_state()
        await asyncio.sleep(STATE_SAVE_EVERY)


async def restore_state():
    """Rejoin each voice channel that was playing and continue the song where it was, with the rest of the queue."""
    try:
        saved = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
    except (OSError, ValueError) as e:
        print(f"Couldn't read the saved playback state ({e!r}); starting fresh.")
        return
    if time.time() - saved.get("saved_at", 0) > STATE_MAX_AGE:
        return
    for guild_id, st in saved.get("guilds", {}).items():
        guild = bot.get_guild(int(guild_id))
        channel = guild.get_channel(st["voice_channel"]) if guild else None
        if not channel or not any(not m.bot for m in channel.members):
            continue  # gone, or nobody's listening anymore
        try:
            tracks = [track_from_dict(d, d.get("requested_by") or "someone") for d in st["tracks"]]
            if not tracks:
                continue
            text_channel = guild.get_channel(st["text_channel"]) if st.get("text_channel") else None

            async def send(msg: str, text_channel=text_channel):
                if text_channel:
                    await text_channel.send(msg)

            if await join_voice(channel, send) is None:
                continue
            player = bot.player(guild.id)
            player.queue = deque(tracks)
            player.loop, player.volume, player.channel = st.get("loop", "off"), st.get("volume", 1.0), text_channel
            player.resume_paused = st.get("paused", False)
            tracks[0].resume_at = max(0.0, st.get("position", 0) - 1)  # a second of overlap, so nothing is missed
            player.announced = tracks[0]  # it's continuing, not starting: no "Now playing" message
            bot.play_next(guild)
            print(f"Continuing {tracks[0].title!r} at {clock(tracks[0].resume_at)} in {guild.name} "
                  f"({len(tracks) - 1} more queued).")
        except Exception as e:
            print(f"Couldn't continue playing in {guild.name}: {e!r}")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id {bot.user.id})")
    print(f"ffmpeg: {FFMPEG or 'NOT FOUND'} | deno: {DENO or 'NOT FOUND'}")
    if not bot.restored:  # on_ready also runs after every reconnect: restore only once
        bot.restored = True
        await restore_state()
        asyncio.create_task(keep_saving_state())


@bot.tree.error
async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Always answer, so a crash never leaves Discord stuck on "thinking…".
    cause = getattr(error, "original", error)
    print(f"Error in /{interaction.command.name if interaction.command else '?'}: {cause!r}")
    msg = f"Something went wrong: `{cause}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass  # interaction expired or another bot instance already answered it


async def ensure_voice(interaction: discord.Interaction) -> discord.VoiceClient | None:
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("Join a voice channel first.")
        return None
    return await join_voice(interaction.user.voice.channel, interaction.followup.send)


async def connect_voice(channel: discord.VoiceChannel | discord.StageChannel) -> discord.VoiceClient | None:
    """Connect to `channel`, trying a second time if Discord's voice server doesn't answer.
    Returns None if both attempts time out."""
    for attempt in (1, 2):
        try:
            return await channel.connect(self_deaf=True)
        except asyncio.TimeoutError:
            print(f"Voice connect timed out (attempt {attempt}) in guild {channel.guild.id}")
            # Clear any half-made connection so the retry (or the next /play) starts clean.
            leftover = channel.guild.voice_client
            if leftover is not None:
                try:
                    await leftover.disconnect(force=True)
                except Exception:
                    pass
    return None


async def join_voice(channel: discord.VoiceChannel | discord.StageChannel, send) -> discord.VoiceClient | None:
    """Connect to (or move to) `channel`. `send` reports problems back to whoever asked."""
    guild = channel.guild
    me = guild.me
    if not channel.permissions_for(me).speak:
        await send(
            f"I don't have the **Speak** permission in {channel.mention}. "
            "Give my role Speak in the channel or server settings, then try again."
        )
        return None
    vc = guild.voice_client
    if vc is None:
        vc = await connect_voice(channel)
        if vc is None:
            await send("Couldn't connect to voice (Discord didn't respond). Try again in a moment.")
            return None
    elif vc.channel != channel:
        await vc.move_to(channel)
    if isinstance(channel, discord.StageChannel):
        try:
            await me.edit(suppress=False)  # become a speaker on the stage
        except discord.HTTPException:
            await send("I'm only a listener on this stage. Invite me to speak, or use a normal voice channel.")
    if me.voice and me.voice.mute:
        await send("Heads up: I'm **server-muted**. Right-click me and untick Server Mute so you can hear me.")
    return vc


@bot.tree.command(description="Play a song/playlist link (YouTube, SoundCloud, Spotify, Apple Music) or search by name")
@app_commands.describe(query="Song/playlist link or search terms")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    vc = await ensure_voice(interaction)
    if vc is None:
        return
    try:
        tracks, playlist = await fetch_tracks(query, interaction.user.display_name)
    except Exception as e:
        await interaction.followup.send(f"Couldn't load that: `{e}`")
        return
    await enqueue(interaction.guild, interaction.followup.send, tracks, playlist, interaction.channel)


async def enqueue(guild: discord.Guild, send, tracks: list[Track], playlist: str | None,
                  channel: discord.abc.Messageable | None):
    """Add tracks to the queue, start playing if idle, and report back with `send`.
    Later songs are announced in `channel` as they start."""
    player = bot.player(guild.id)
    player.queue.extend(tracks)
    if channel:
        player.channel = channel
    starting = player.current is None  # a song being matched counts as playing
    if starting:
        bot.play_next(guild)
        player.announced = player.current  # this reply already says it's playing
    elif player.queue[0].needs_match:
        player.queue[0].start_matching()  # these may be next: get them ready now

    # Each thing gets its own message: what was added, then (if it's starting) the song now playing.
    if playlist:
        await send(f"Added **{songs(len(tracks))}** from **{playlist}**.")
    elif not starting:
        await send(f"Queued **{tracks[0].title}** ({tracks[0].length}) at position {len(player.queue)}.")
    if starting and player.current:
        if player.current.needs_match:  # wait (briefly) for its YouTube video, so the message can show it
            await asyncio.wait({player.current.start_matching()}, timeout=10)
        await send(now_playing_text(player.current))


# These do the work behind both the slash commands and the nickname commands, and return the reply.
def do_skip(guild: discord.Guild) -> str:
    vc = guild.voice_client
    player = bot.player(guild.id)
    if not vc or not player.current:
        return "Nothing is playing."
    player.skipping = True
    if vc.is_playing() or vc.is_paused():
        vc.stop()  # triggers play_next via the after-callback
    else:
        bot.play_next(guild)  # the song was still being matched and hadn't started
    return "Skipped the song."


def do_replay(guild: discord.Guild) -> str:
    vc = guild.voice_client
    player = bot.player(guild.id)
    if not vc or not player.current:
        return "Nothing is playing."
    if not (vc.is_playing() or vc.is_paused()):
        return "The song is still starting, so it's already at the beginning."
    player.replaying = True
    vc.stop()  # play_next puts the song back at the front of the queue
    return f"Starting **{player.current.title}** over."


def do_pause(guild: discord.Guild) -> str:
    vc = guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        return "Paused."
    return "Nothing is playing."


def do_resume(guild: discord.Guild) -> str:
    vc = guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        return "Resumed."
    return "Nothing is paused."


def do_stop(guild: discord.Guild) -> str:
    bot.player(guild.id).reset()
    if guild.voice_client:
        guild.voice_client.stop()
    return "Stopped playback and cleared the queue."


def do_shuffle(guild: discord.Guild) -> str:
    player = bot.player(guild.id)
    if len(player.queue) < 2:
        return "The queue needs at least two songs to shuffle."
    tracks = list(player.queue)
    random.shuffle(tracks)
    player.queue = deque(tracks)
    return f"Shuffled {len(tracks)} songs."


def do_volume(guild: discord.Guild, level: int) -> str:
    player = bot.player(guild.id)
    player.volume = level / 100
    vc = guild.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = player.volume
    return f"Volume set to {level}%."


async def do_leave(guild: discord.Guild) -> str:
    vc = guild.voice_client
    if not vc:
        return "I'm not in a voice channel."
    bot.player(guild.id).reset()
    await vc.disconnect()
    return "Goodbye!"


@bot.tree.command(description="Skip the current song")
async def skip(interaction: discord.Interaction):
    await interaction.response.send_message(do_skip(interaction.guild))


@bot.tree.command(description="Start the current song over from the beginning")
async def replay(interaction: discord.Interaction):
    await interaction.response.send_message(do_replay(interaction.guild))


@bot.tree.command(description="Pause playback")
async def pause(interaction: discord.Interaction):
    await interaction.response.send_message(do_pause(interaction.guild))


@bot.tree.command(description="Resume playback")
async def resume(interaction: discord.Interaction):
    await interaction.response.send_message(do_resume(interaction.guild))


@bot.tree.command(description="Stop playback and clear the queue")
async def stop(interaction: discord.Interaction):
    await interaction.response.send_message(do_stop(interaction.guild))


@bot.tree.command(description="Shuffle the upcoming songs")
async def shuffle(interaction: discord.Interaction):
    await interaction.response.send_message(do_shuffle(interaction.guild))


LOOP_LABELS = {"off": "Looping is off", "track": "Looping the current song", "queue": "Looping the whole queue"}


@bot.tree.command(description="Loop the current song or the whole queue")
@app_commands.describe(mode="What to repeat")
@app_commands.choices(mode=[
    app_commands.Choice(name="Current song", value="track"),
    app_commands.Choice(name="Whole queue", value="queue"),
    app_commands.Choice(name="Off", value="off"),
])
async def loop(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    bot.player(interaction.guild.id).loop = mode.value
    await interaction.response.send_message(LOOP_LABELS[mode.value] + ".")


@bot.tree.command(name="queue", description="Show the song queue")
async def show_queue(interaction: discord.Interaction):
    player = bot.player(interaction.guild.id)
    if not player.current and not player.queue:
        return await interaction.response.send_message("The queue is empty.")
    lines = []
    if player.current:
        lines.append(f"**Now:** {player.current.title} ({player.current.length})")
    for i, t in enumerate(list(player.queue)[:10], 1):
        lines.append(f"`{i}.` {t.title} ({t.length}) — {t.requested_by}")
    if len(player.queue) > 10:
        lines.append(f"...and {len(player.queue) - 10} more.")
    if player.loop != "off":
        lines.append(LOOP_LABELS[player.loop])
    await interaction.response.send_message("\n".join(lines))


def youtube_url(t: Track) -> str | None:
    """The YouTube video being played, if it is one (Spotify/Apple Music songs once matched)."""
    m = platform_links.YOUTUBE_ID_RE.search(t.page_url)
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else None


def now_playing_text(t: Track) -> str:
    """The song line shown by /nowplaying and when a song starts. The YouTube link is left bare so Discord shows
    the video's player under the message; other links stay in <> so they add no preview of their own."""
    msg = f"**{t.title}** ({t.length}), requested by {t.requested_by}"
    yt = youtube_url(t)
    if t.web_url and (not yt or t.link):
        msg += f"\n<{t.web_url}>"
    if yt:
        msg += f"\n{yt}"
    return msg


@bot.tree.command(description="Show the current song")
async def nowplaying(interaction: discord.Interaction):
    t = bot.player(interaction.guild.id).current
    if not t:
        return await interaction.response.send_message("Nothing is playing.")
    await interaction.response.send_message(now_playing_text(t))


@bot.tree.command(description="Show the current song with links to it on YouTube, Spotify, Apple Music and more")
async def playing(interaction: discord.Interaction):
    t = bot.player(interaction.guild.id).current
    if not t:
        return await interaction.response.send_message("Nothing is playing.")
    await interaction.response.defer(thinking=True)
    if t.needs_match:  # a Spotify/Apple Music song still being matched: its YouTube video is part of the answer
        await asyncio.wait({t.start_matching()}, timeout=10)
    links = t.links or await platform_links.find_links(t.title, t.page_url, t.link, t.artist, t.song, t.duration)
    if not t.needs_match:  # once matched, the links don't change: look them up only once per song
        t.links = links

    vc = interaction.guild.voice_client
    source = vc.source if vc else None
    audio = source.original if isinstance(source, discord.PCMVolumeTransformer) else None
    position = f"{clock(audio.played_seconds)} / " if isinstance(audio, BufferedAudio) else ""
    paused = " (paused)" if vc and vc.is_paused() else ""
    lines = [f"**{t.title}**", f"{position}{t.length}{paused}, requested by {t.requested_by}"]
    if links:
        # Only the YouTube link is left bare, so Discord shows the video's player under the message.
        lines.append(" · ".join(f"[{name}]({url})" if name == "YouTube" else f"[{name}](<{url}>)"
                                for name, url in links.items()))
    await interaction.followup.send("\n".join(lines))


@bot.tree.command(description="Set the volume (0-200, default 100)")
@app_commands.describe(level="Volume percent; above 100 boosts quiet songs")
async def volume(interaction: discord.Interaction, level: app_commands.Range[int, 0, 200]):
    await interaction.response.send_message(do_volume(interaction.guild, level))


@bot.tree.command(description="Disconnect the bot from voice")
async def leave(interaction: discord.Interaction):
    await interaction.response.send_message(await do_leave(interaction.guild))


playlist_group = app_commands.Group(name="playlist", description="Saved playlists for this server")


async def playlist_name_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    names = sorted(p["name"] for p in playlists.guild(interaction.guild_id).values())
    return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n.lower()][:25]


async def playlist_position_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    pl = playlists.get(interaction.guild_id, interaction.namespace.name or "")
    if not pl:
        return []
    choices = [(i, f"{i}. {t['title']}") for i, t in enumerate(pl["tracks"], 1)]
    return [app_commands.Choice(name=label[:100], value=i) for i, label in choices if current.lower() in label.lower()][:25]


async def missing_playlist(interaction: discord.Interaction, name: str):
    msg = f"There's no playlist called **{name}**. See them all with `/playlist list`."
    if interaction.response.is_done():
        await interaction.followup.send(msg)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


@playlist_group.command(name="create", description="Create a new, empty playlist")
@app_commands.describe(name="Playlist name")
async def playlist_create(interaction: discord.Interaction, name: app_commands.Range[str, 1, 50]):
    name = name.strip()
    if playlists.get(interaction.guild_id, name):
        return await interaction.response.send_message(f"A playlist called **{name}** already exists.", ephemeral=True)
    playlists.guild(interaction.guild_id)[name.lower()] = {"name": name, "owner_id": interaction.user.id, "tracks": []}
    playlists.save()
    await interaction.response.send_message(f"Created the playlist **{name}**. Add songs to it with `/playlist add`.")


@playlist_group.command(name="add", description="Add a song (or a YouTube playlist) to a saved playlist")
@app_commands.describe(name="Playlist to add to", song="URL or search terms; leave empty to add the song playing now")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def playlist_add(interaction: discord.Interaction, name: str, song: str | None = None):
    pl = playlists.get(interaction.guild_id, name)
    if not pl:
        return await missing_playlist(interaction, name)
    if song is None:
        current = bot.player(interaction.guild_id).current
        if not current:
            return await interaction.response.send_message("Nothing is playing. Give me a song to add.", ephemeral=True)
        tracks, source = [current], None
        await interaction.response.defer()
    else:
        await interaction.response.defer()
        try:
            tracks, source = await fetch_tracks(song, interaction.user.display_name)
        except Exception as e:
            return await interaction.followup.send(f"Couldn't load that: `{e}`")

    room = MAX_SAVED_TRACKS - len(pl["tracks"])
    if room <= 0:
        return await interaction.followup.send(f"**{pl['name']}** is full (it can hold {MAX_SAVED_TRACKS} songs).")
    tracks = tracks[:room]
    pl["tracks"].extend(track_to_dict(t) for t in tracks)
    playlists.save()
    if source or len(tracks) > 1:
        await interaction.followup.send(f"Added **{songs(len(tracks))}** to **{pl['name']}**. It now has {songs(len(pl['tracks']))}.")
    else:
        await interaction.followup.send(f"Added **{tracks[0].title}** to **{pl['name']}** at position {len(pl['tracks'])}.")


@playlist_group.command(name="remove", description="Remove a song from a saved playlist")
@app_commands.describe(name="Playlist to remove from", position="The song to remove (start typing its name or number)")
@app_commands.autocomplete(name=playlist_name_autocomplete, position=playlist_position_autocomplete)
async def playlist_remove(interaction: discord.Interaction, name: str, position: int):
    pl = playlists.get(interaction.guild_id, name)
    if not pl:
        return await missing_playlist(interaction, name)
    if not pl["tracks"]:
        return await interaction.response.send_message(f"**{pl['name']}** is empty.", ephemeral=True)
    if not 1 <= position <= len(pl["tracks"]):
        return await interaction.response.send_message(
            f"**{pl['name']}** has {songs(len(pl['tracks']))}. Pick a number from 1 to {len(pl['tracks'])}.", ephemeral=True
        )
    removed = pl["tracks"].pop(position - 1)
    playlists.save()
    await interaction.response.send_message(f"Removed **{removed['title']}** from **{pl['name']}**.")


@playlist_group.command(name="play", description="Queue a saved playlist")
@app_commands.describe(name="Playlist to play", shuffle="Shuffle the songs first")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def playlist_play(interaction: discord.Interaction, name: str, shuffle: bool = False):
    pl = playlists.get(interaction.guild_id, name)
    if not pl:
        return await missing_playlist(interaction, name)
    if not pl["tracks"]:
        return await interaction.response.send_message(f"**{pl['name']}** is empty. Add songs to it with `/playlist add`.", ephemeral=True)
    await interaction.response.defer()
    vc = await ensure_voice(interaction)
    if vc is None:
        return
    tracks = [track_from_dict(d, interaction.user.display_name) for d in pl["tracks"]]
    if shuffle:
        random.shuffle(tracks)
    await enqueue(interaction.guild, interaction.followup.send, tracks, pl["name"], interaction.channel)


@playlist_group.command(name="show", description="Show the songs in a saved playlist")
@app_commands.describe(name="Playlist to show")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def playlist_show(interaction: discord.Interaction, name: str):
    pl = playlists.get(interaction.guild_id, name)
    if not pl:
        return await missing_playlist(interaction, name)
    tracks = [track_from_dict(d, "") for d in pl["tracks"]]
    lines = [f"**{pl['name']}**: {songs(len(tracks))}, made by <@{pl['owner_id']}>"]
    lines += [f"`{i}.` {t.title[:70]} ({t.length})" for i, t in enumerate(tracks[:20], 1)]  # stay under Discord's 2000 chars
    if len(tracks) > 20:
        lines.append(f"...and {len(tracks) - 20} more.")
    if not tracks:
        lines.append("It's empty. Add songs to it with `/playlist add`.")
    await interaction.response.send_message("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())


@playlist_group.command(name="list", description="List this server's saved playlists")
async def playlist_list(interaction: discord.Interaction):
    pls = sorted(playlists.guild(interaction.guild_id).values(), key=lambda p: p["name"].lower())
    if not pls:
        return await interaction.response.send_message("There are no playlists yet. Make one with `/playlist create`.")
    lines = ["**Playlists**"] + [f"**{p['name']}**: {songs(len(p['tracks']))}" for p in pls]
    await interaction.response.send_message("\n".join(lines))


@playlist_group.command(name="delete", description="Delete a saved playlist (only its creator or a server manager can)")
@app_commands.describe(name="Playlist to delete")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def playlist_delete(interaction: discord.Interaction, name: str):
    pl = playlists.get(interaction.guild_id, name)
    if not pl:
        return await missing_playlist(interaction, name)
    if interaction.user.id != pl["owner_id"] and not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message(
            f"Only <@{pl['owner_id']}> or a server manager can delete **{pl['name']}**.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
    del playlists.guild(interaction.guild_id)[pl["name"].lower()]
    playlists.save()
    await interaction.response.send_message(f"Deleted the playlist **{pl['name']}**.")


bot.tree.add_command(playlist_group)


@bot.tree.command(name="help", description="List the bot's commands")
async def show_help(interaction: discord.Interaction):
    lines = []
    for c in sorted(bot.tree.get_commands(), key=lambda c: c.name):
        subs = c.commands if isinstance(c, app_commands.Group) else [c]
        lines += [f"`/{s.qualified_name}` — {s.description}" for s in subs]
    await interaction.response.send_message("**Music commands**\n" + "\n".join(lines), ephemeral=True)


# Nickname commands: rename the bot to "/skip", "/play lofi", "/volume 50"... and it runs the command, then puts its
# nickname back. Only people allowed to change the bot's nickname (Manage Nicknames, with a role above the bot's)
# can do this. With View Audit Log the bot also knows who renamed it: needed for "/play" when it isn't in voice yet.
NICK_PREFIX = "/"
NICK_COMMANDS = "play <song>, skip, replay, pause, resume, stop, shuffle, loop <song|queue|off>, volume <0-200>, leave"
LOOP_WORDS = {"song": "track", "track": "track", "one": "track", "queue": "queue", "all": "queue", "off": "off"}
home_nicks: dict[int, str | None] = {}  # per server: the bot's real nickname, to put back after a command


async def who_renamed_me(guild: discord.Guild) -> discord.Member | None:
    if not guild.me.guild_permissions.view_audit_log:
        return None
    for attempt in range(2):  # the audit log entry can show up a moment after the rename
        try:
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.member_update):
                recent = (discord.utils.utcnow() - entry.created_at).total_seconds() < 30
                if entry.target and entry.target.id == bot.user.id and recent:
                    return guild.get_member(entry.user.id) or entry.user
        except discord.HTTPException:
            return None
        await asyncio.sleep(1)
    return None


def nick_reply_channel(guild: discord.Guild) -> discord.abc.Messageable | None:
    """Where to answer a nickname command: the voice channel's chat if the bot is in one, else the server's main channel."""
    vc = guild.voice_client
    candidates = [vc.channel if vc else None, guild.system_channel, *guild.text_channels]
    return next((c for c in candidates if c and c.permissions_for(guild.me).send_messages), None)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if after.id != bot.user.id or after.nick == before.nick:
        return
    guild = after.guild
    if not (before.nick or "").startswith(NICK_PREFIX):
        home_nicks[guild.id] = before.nick
    if not (after.nick or "").startswith(NICK_PREFIX):
        return  # an ordinary rename (or our own reset): leave it alone

    command = after.nick[len(NICK_PREFIX):].strip()
    try:
        await guild.me.edit(nick=home_nicks.get(guild.id))
    except discord.HTTPException as e:
        print(f"Couldn't put my nickname back in {guild.name} (give me Change Nickname): {e!r}")

    renamer = await who_renamed_me(guild)
    channel = nick_reply_channel(guild)
    by = f"{renamer.display_name} via nickname" if renamer else "Nickname command"
    print(f"{by} in {guild.name}: {command!r}")

    async def send(msg: str):
        if channel:
            try:
                await channel.send(f"*{by}:* {msg}", allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    name, _, arg = command.partition(" ")
    name, arg = name.lower(), arg.strip()
    simple = {"skip": do_skip, "replay": do_replay, "pause": do_pause, "resume": do_resume,
              "stop": do_stop, "shuffle": do_shuffle}
    try:
        if name in simple:
            await send(simple[name](guild))
        elif name == "leave":
            await send(await do_leave(guild))
        elif name == "volume":
            if not arg.isdigit() or not 0 <= int(arg) <= 200:
                return await send("Give a volume from 0 to 200, for example `/volume 50`.")
            await send(do_volume(guild, int(arg)))
        elif name == "loop":
            mode = LOOP_WORDS.get(arg.lower())
            if not mode:
                return await send("What should I loop? Use `/loop song`, `/loop queue` or `/loop off`.")
            bot.player(guild.id).loop = mode
            await send(LOOP_LABELS[mode] + ".")
        elif name == "play":
            if not arg:
                return await send("What should I play? For example: `/play never gonna give you up`.")
            renamer_voice = getattr(renamer, "voice", None)
            channel_to_join = renamer_voice.channel if renamer_voice else (guild.voice_client.channel if guild.voice_client else None)
            if channel_to_join is None:
                return await send("Join a voice channel first. (If you're in one, give me **View Audit Log** so I can see who renamed me.)")
            if await join_voice(channel_to_join, send) is None:
                return
            try:
                tracks, playlist = await fetch_tracks(arg, renamer.display_name if renamer else "nickname")
            except Exception as e:
                return await send(f"Couldn't load that: `{e}`")
            await enqueue(guild, send, tracks, playlist, channel)
        else:
            await send(f"I don't know `{NICK_PREFIX}{name}`. Nickname commands: {NICK_COMMANDS}.")
    except Exception as e:
        print(f"Error in nickname command {command!r}: {e!r}")
        await send(f"Something went wrong: `{e}`")


@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    # Leave when everyone else has left the channel.
    vc = member.guild.voice_client
    if vc and before.channel == vc.channel and all(m.bot for m in vc.channel.members):
        bot.player(member.guild.id).reset()
        await vc.disconnect()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env (see .env.example)")
    if not FFMPEG:
        raise SystemExit("FFmpeg not found. Install it (winget install Gyan.FFmpeg) or set FFMPEG_PATH in .env")
    bot.run(TOKEN)
