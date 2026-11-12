"""Read song lists from Spotify and Apple Music links.

Neither service lets bots stream its audio, so we only take each song's title, artist and length from the link;
the bot then plays the song from YouTube. No accounts or API keys needed: Spotify's public embed pages and Apple's
iTunes lookup API are used.
"""

import json
import re
from dataclasses import dataclass

import aiohttp

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
TIMEOUT = aiohttp.ClientTimeout(total=20)

SPOTIFY_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z-]+/)?(track|album|playlist|artist)/([A-Za-z0-9]+)")
APPLE_RE = re.compile(r"music\.apple\.com/([a-z]{2})/(album|song|playlist)/(?:[^/?#]+/)?([^/?#]+)")
APPLE_SONG_ID_RE = re.compile(r"/song/(?:[^/?#]+/)?(\d+)")


@dataclass
class LinkedSong:
    title: str
    artist: str
    duration: int | None  # seconds
    link: str  # the song's page on the original service

    @property
    def search(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else self.title


def is_supported(url: str) -> bool:
    return bool(SPOTIFY_RE.search(url) or APPLE_RE.search(url))


async def resolve(url: str) -> tuple[list[LinkedSong], str | None]:
    """Return (songs, collection name or None for a single song). Raises ValueError for unusable links."""
    async with aiohttp.ClientSession(headers=HEADERS, timeout=TIMEOUT) as session:
        if m := SPOTIFY_RE.search(url):
            return await _spotify(session, *m.groups())
        if m := APPLE_RE.search(url):
            return await _apple(session, url, *m.groups())
    raise ValueError("not a Spotify or Apple Music link")


async def _get_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url) as r:
        if r.status == 404:
            raise ValueError("that link doesn't exist (or is private)")
        r.raise_for_status()
        return await r.text()


# --- Spotify ---------------------------------------------------------------------------------------------------

async def _spotify(session, kind: str, item_id: str) -> tuple[list[LinkedSong], str | None]:
    html = await _get_text(session, f"https://open.spotify.com/embed/{kind}/{item_id}")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    try:
        entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ValueError("couldn't read that Spotify link. Check it's correct and public") from None

    if kind == "track":
        artist = ", ".join(a["name"] for a in entity.get("artists") or [])
        return [LinkedSong(entity["name"], artist, _secs(entity.get("duration")), f"https://open.spotify.com/track/{item_id}")], None

    songs = [
        LinkedSong(
            t["title"],
            t.get("subtitle", "").replace(" ", " "),
            _secs(t.get("duration")),
            "https://open.spotify.com/track/" + t["uri"].rsplit(":", 1)[-1],
        )
        for t in entity.get("trackList") or []
        if t.get("uri", "").startswith("spotify:track:")  # skip podcast episodes
    ]
    if not songs:
        raise ValueError("that Spotify link has no songs")
    return songs, entity.get("name") or f"Spotify {kind}"


# --- Apple Music ------------------------------------------------------------------------------------------------

async def _itunes_lookup(session, country: str, ids: list[str], entity: str | None = None) -> list[dict]:
    params = {"id": ",".join(ids), "country": country, "limit": "200"}
    if entity:
        params["entity"] = entity
    async with session.get("https://itunes.apple.com/lookup", params=params) as r:
        r.raise_for_status()
        return json.loads(await r.text())["results"]


def _apple_song(d: dict) -> LinkedSong:
    return LinkedSong(d["trackName"], d.get("artistName", ""), _secs(d.get("trackTimeMillis")), d.get("trackViewUrl", "").split("&uo=")[0])


async def _apple(session, url: str, country: str, kind: str, item_id: str) -> tuple[list[LinkedSong], str | None]:
    if kind == "album" and (song := re.search(r"[?&]i=(\d+)", url)):
        kind, item_id = "song", song.group(1)  # an album link pointing at one song

    if kind == "song":
        results = [r for r in await _itunes_lookup(session, country, [item_id]) if r.get("wrapperType") == "track"]
        if not results:
            raise ValueError("couldn't find that Apple Music song")
        return [_apple_song(results[0])], None

    if kind == "album":
        results = await _itunes_lookup(session, country, [item_id], entity="song")
        album = next((r for r in results if r.get("wrapperType") == "collection"), {})
        songs = [_apple_song(r) for r in results if r.get("wrapperType") == "track"]
        if not songs:
            raise ValueError("couldn't find that Apple Music album")
        return songs, album.get("collectionName") or "Apple Music album"

    # Playlist: the public page lists the songs (without artists), so look their IDs up for full details.
    html = await _get_text(session, url)
    blocks = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
    playlist = next((d for d in map(json.loads, blocks) if d.get("@type") == "MusicPlaylist"), None)
    if not playlist:
        raise ValueError("couldn't read that Apple Music playlist (is it public?)")
    ids = [m.group(1) for t in playlist.get("track") or [] if (m := APPLE_SONG_ID_RE.search(t.get("url", "")))]
    found: dict[str, dict] = {}
    for i in range(0, len(ids), 150):
        for r in await _itunes_lookup(session, country, ids[i:i + 150]):
            found[str(r.get("trackId"))] = r
    songs = [_apple_song(found[i]) for i in ids if i in found]
    if not songs:
        raise ValueError("that Apple Music playlist has no songs")
    return songs, playlist.get("name") or "Apple Music playlist"


def _secs(ms) -> int | None:
    return round(ms / 1000) if ms else None
