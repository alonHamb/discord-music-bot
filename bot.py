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
import streaming_links

load_dotenv(os.getenv("BOT_ENV_FILE"))  # the Windows installer points this at its settings file; default: .env
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional: syncs commands to one server instantly


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

    def __init__(self, inner: discord.AudioSource, ahead: int = 250):
        self._inner = inner
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=ahead)
        self._stop = threading.Event()
        self._ended = threading.Event()
        self.frames_in = 0  # decoded so far
        self.frames_out = 0  # handed to Discord so far
        threading.Thread(target=self._fill, daemon=True, name="audio-buffer").start()

    @property
    def played_seconds(self) -> float:
        return self.frames_out * 0.02

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

    def __init__(self, url: str, fast: bool = True):
        self.downloader = open_stream(url, fast)
        self.audio = BufferedAudio(PipedFFmpegAudio(self.downloader, executable=FFMPEG,
                                                    before_options="-loglevel error", options="-vn"))

    def close(self):
        self.audio.cleanup()
        self.downloader.kill()

    @classmethod
    async def open(cls, track: "Track") -> "Stream":
        """Match the song if needed, start it, and wait until 1 s of audio is ready."""
        if track.needs_match:
            await track.start_matching()
        stream = cls(track.page_url)
        await asyncio.to_thread(stream.audio.wait_ready)
        if stream.audio.failed:  # the fast way got nothing (YouTube hiccup): retry the usual, slower way
            stream.close()
            print(f"Fast start failed for {track.title!r}; retrying the slower way.")
            stream = cls(track.page_url, fast=False)
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
        if not self.duration:
            return "?:??"
        m, s = divmod(int(self.duration), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


class GuildPlayer:
    def __init__(self):
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.volume = 1.0
        self.loop = "off"  # "off", "track" or "queue"
        self.skipping = False  # set by /skip so a looped track moves on
        self.prefetched: tuple[Track, asyncio.Task] | None = None  # the next song, being prepared ahead of time

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

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    def player(self, guild_id: int) -> GuildPlayer:
        return self.players.setdefault(guild_id, GuildPlayer())

    def play_next(self, guild: discord.Guild):
        player = self.player(guild.id)
        finished, player.current = player.current, None
        if finished:
            if player.loop == "track" and not player.skipping:
                player.queue.appendleft(finished)
            elif player.loop == "queue":
                player.queue.append(finished)
        player.skipping = False

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
        asyncio.create_task(self._prefetch_near_end(guild, track, stream.audio))

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


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id {bot.user.id})")
    print(f"ffmpeg: {FFMPEG or 'NOT FOUND'} | deno: {DENO or 'NOT FOUND'}")


@bot.tree.error
async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Always answer, so a crash never leaves Discord stuck on "thinking…".
    cause = getattr(error, "original", error)
    print(f"Error in /{interaction.command.name if interaction.command else '?'}: {cause!r}")
    msg = f"⚠️ Something went wrong: `{cause}`"
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
    channel = interaction.user.voice.channel
    me = interaction.guild.me
    if not channel.permissions_for(me).speak:
        await interaction.followup.send(
            f"I don't have the **Speak** permission in {channel.mention}. "
            "Give my role Speak in the channel or server settings, then try again."
        )
        return None
    vc = interaction.guild.voice_client
    if vc is None:
        vc = await channel.connect(self_deaf=True)
    elif vc.channel != channel:
        await vc.move_to(channel)
    if isinstance(channel, discord.StageChannel):
        try:
            await me.edit(suppress=False)  # become a speaker on the stage
        except discord.HTTPException:
            await interaction.followup.send("I'm only a listener on this stage. Invite me to speak, or use a normal voice channel.")
    if me.voice and me.voice.mute:
        await interaction.followup.send("Heads up: I'm **server-muted**. Right-click me and untick Server Mute so you can hear me.")
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
    await enqueue(interaction, vc, tracks, playlist)


async def enqueue(interaction: discord.Interaction, vc: discord.VoiceClient, tracks: list[Track], playlist: str | None):
    """Add tracks to the queue, start playing if idle, and report back."""
    player = bot.player(interaction.guild.id)
    player.queue.extend(tracks)
    starting = player.current is None  # a song being matched counts as playing
    if starting:
        bot.play_next(interaction.guild)
    elif player.queue[0].needs_match:
        player.queue[0].start_matching()  # these may be next: get them ready now

    if playlist:
        msg = f"📃 Added **{len(tracks)}** tracks from **{playlist}**."
        if starting:
            msg += f"\n▶️ Now playing **{player.current.title}** ({player.current.length})"
    elif starting:
        msg = f"▶️ Now playing **{tracks[0].title}** ({tracks[0].length})"
    else:
        msg = f"➕ Queued **{tracks[0].title}** ({tracks[0].length}) — position {len(player.queue)}"
    await interaction.followup.send(msg)


@bot.tree.command(description="Skip the current song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    player = bot.player(interaction.guild.id)
    if not vc or not player.current:
        return await interaction.response.send_message("Nothing is playing.")
    player.skipping = True
    if vc.is_playing() or vc.is_paused():
        vc.stop()  # triggers play_next via the after-callback
    else:
        bot.play_next(interaction.guild)  # the song was still being matched and hadn't started
    await interaction.response.send_message("⏭️ Skipped.")


@bot.tree.command(description="Pause playback")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        return await interaction.response.send_message("⏸️ Paused.")
    await interaction.response.send_message("Nothing is playing.")


@bot.tree.command(description="Resume playback")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        return await interaction.response.send_message("▶️ Resumed.")
    await interaction.response.send_message("Nothing is paused.")


@bot.tree.command(description="Stop playback and clear the queue")
async def stop(interaction: discord.Interaction):
    bot.player(interaction.guild.id).reset()
    vc = interaction.guild.voice_client
    if vc:
        vc.stop()
    await interaction.response.send_message("⏹️ Stopped and cleared the queue.")


@bot.tree.command(description="Shuffle the upcoming songs")
async def shuffle(interaction: discord.Interaction):
    player = bot.player(interaction.guild.id)
    if len(player.queue) < 2:
        return await interaction.response.send_message("Need at least 2 songs in the queue to shuffle.")
    tracks = list(player.queue)
    random.shuffle(tracks)
    player.queue = deque(tracks)
    await interaction.response.send_message(f"🔀 Shuffled {len(tracks)} songs.")


LOOP_LABELS = {"off": "➡️ Loop off", "track": "🔂 Looping the current song", "queue": "🔁 Looping the whole queue"}


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
        lines.append(f"…and {len(player.queue) - 10} more")
    if player.loop != "off":
        lines.append(LOOP_LABELS[player.loop])
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(description="Show the current song")
async def nowplaying(interaction: discord.Interaction):
    t = bot.player(interaction.guild.id).current
    if not t:
        return await interaction.response.send_message("Nothing is playing.")
    msg = f"🎶 **{t.title}** ({t.length}) — requested by {t.requested_by}"
    if t.web_url:
        msg += f"\n<{t.web_url}>"
    await interaction.response.send_message(msg)


@bot.tree.command(description="Set volume (0-200, default 100)")
@app_commands.describe(level="Volume percent; above 100 boosts quiet songs")
async def volume(interaction: discord.Interaction, level: app_commands.Range[int, 0, 200]):
    player = bot.player(interaction.guild.id)
    player.volume = level / 100
    vc = interaction.guild.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = player.volume
    await interaction.response.send_message(f"🔊 Volume set to {level}%.")


@bot.tree.command(description="Disconnect the bot from voice")
async def leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("I'm not in a voice channel.")
    bot.player(interaction.guild.id).reset()
    await vc.disconnect()
    await interaction.response.send_message("👋 Bye!")


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
    await interaction.response.send_message(f"📁 Created playlist **{name}**. Add songs with `/playlist add`.")


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
        return await interaction.followup.send(f"**{pl['name']}** is full ({MAX_SAVED_TRACKS} songs).")
    tracks = tracks[:room]
    pl["tracks"].extend(track_to_dict(t) for t in tracks)
    playlists.save()
    if source or len(tracks) > 1:
        await interaction.followup.send(f"➕ Added **{len(tracks)}** songs to **{pl['name']}** ({len(pl['tracks'])} total).")
    else:
        await interaction.followup.send(f"➕ Added **{tracks[0].title}** to **{pl['name']}** — position {len(pl['tracks'])}.")


@playlist_group.command(name="remove", description="Remove a song from a saved playlist")
@app_commands.describe(name="Playlist to remove from", position="The song to remove (start typing its name or number)")
@app_commands.autocomplete(name=playlist_name_autocomplete, position=playlist_position_autocomplete)
async def playlist_remove(interaction: discord.Interaction, name: str, position: int):
    pl = playlists.get(interaction.guild_id, name)
    if not pl:
        return await missing_playlist(interaction, name)
    if not 1 <= position <= len(pl["tracks"]):
        return await interaction.response.send_message(
            f"**{pl['name']}** has {len(pl['tracks'])} songs; pick a number from 1 to {len(pl['tracks'])}.", ephemeral=True
        )
    removed = pl["tracks"].pop(position - 1)
    playlists.save()
    await interaction.response.send_message(f"➖ Removed **{removed['title']}** from **{pl['name']}**.")


@playlist_group.command(name="play", description="Queue a saved playlist")
@app_commands.describe(name="Playlist to play", shuffle="Shuffle the songs first")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def playlist_play(interaction: discord.Interaction, name: str, shuffle: bool = False):
    pl = playlists.get(interaction.guild_id, name)
    if not pl:
        return await missing_playlist(interaction, name)
    if not pl["tracks"]:
        return await interaction.response.send_message(f"**{pl['name']}** is empty. Add songs with `/playlist add`.", ephemeral=True)
    await interaction.response.defer()
    vc = await ensure_voice(interaction)
    if vc is None:
        return
    tracks = [track_from_dict(d, interaction.user.display_name) for d in pl["tracks"]]
    if shuffle:
        random.shuffle(tracks)
    await enqueue(interaction, vc, tracks, pl["name"])


@playlist_group.command(name="show", description="Show the songs in a saved playlist")
@app_commands.describe(name="Playlist to show")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def playlist_show(interaction: discord.Interaction, name: str):
    pl = playlists.get(interaction.guild_id, name)
    if not pl:
        return await missing_playlist(interaction, name)
    tracks = [track_from_dict(d, "") for d in pl["tracks"]]
    lines = [f"📁 **{pl['name']}** — {len(tracks)} songs, made by <@{pl['owner_id']}>"]
    lines += [f"`{i}.` {t.title[:70]} ({t.length})" for i, t in enumerate(tracks[:20], 1)]  # stay under Discord's 2000 chars
    if len(tracks) > 20:
        lines.append(f"…and {len(tracks) - 20} more")
    if not tracks:
        lines.append("It's empty. Add songs with `/playlist add`.")
    await interaction.response.send_message("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())


@playlist_group.command(name="list", description="List this server's saved playlists")
async def playlist_list(interaction: discord.Interaction):
    pls = sorted(playlists.guild(interaction.guild_id).values(), key=lambda p: p["name"].lower())
    if not pls:
        return await interaction.response.send_message("No playlists yet. Make one with `/playlist create`.")
    lines = ["**Playlists**"] + [f"📁 **{p['name']}** — {len(p['tracks'])} songs" for p in pls]
    await interaction.response.send_message("\n".join(lines))


@playlist_group.command(name="delete", description="Delete a saved playlist (its creator or a server manager)")
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
    await interaction.response.send_message(f"🗑️ Deleted playlist **{pl['name']}**.")


bot.tree.add_command(playlist_group)


@bot.tree.command(name="help", description="List the bot's commands")
async def show_help(interaction: discord.Interaction):
    lines = []
    for c in sorted(bot.tree.get_commands(), key=lambda c: c.name):
        subs = c.commands if isinstance(c, app_commands.Group) else [c]
        lines += [f"`/{s.qualified_name}` — {s.description}" for s in subs]
    await interaction.response.send_message("**Music commands**\n" + "\n".join(lines), ephemeral=True)


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
