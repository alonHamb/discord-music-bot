"""Find the playing song on other music services, for /playing.

No accounts or API keys: Apple Music through Apple's public iTunes search API and Deezer through its public search
API. A result only counts if its title and artist match the song (and its length, when known), so a wrong song is
never linked. Spotify has no keyless search, so it gets an exact link only when the song came from a Spotify link,
and a search link otherwise.
"""

import re
from urllib.parse import quote

import aiohttp

from matching import core_title, normalize

TIMEOUT = aiohttp.ClientTimeout(total=8)
YOUTUBE_ID_RE = re.compile(r"(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/|youtube\.com/shorts/)([\w-]{11})")
TITLE_NOISE_RE = re.compile(r"[\(\[][^\)\]]*[\)\]]|\b(?:official|lyrics?|audio|video|hd|4k|mv)\b", re.I)


def split_title(title: str) -> tuple[str, str]:
    """'Artist - Song (Official Video)' -> ('Artist', 'Song'); ('', title) when there's no 'Artist - ' part."""
    clean = TITLE_NOISE_RE.sub(" ", title)
    parts = re.split(r"\s+[-–—|]\s+", clean, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return "", clean.strip()


def search_text(artist: str, song: str) -> str:
    """What to search for: 'Song (feat. X) - Remastered 2011' by Artist -> 'Artist Song'."""
    bare = re.sub(r"[\(\[].*?[\)\]]", " ", song)
    bare = re.split(r"\s+-\s+", bare)[0]
    bare = re.split(r"\b(?:feat|ft|featuring)\b\.?", bare, flags=re.I)[0]
    return re.sub(r"\s+", " ", f"{artist} {bare.strip() or song}").strip()


def _same_song(found_artist: str, found_title: str, found_secs: int | None,
               artist: str, song: str, full_title: str, duration: int | None, tolerance: int) -> bool:
    if core_title(found_title) != core_title(song):
        return False
    fa = normalize(found_artist)
    if artist:
        a = normalize(artist)
        if not fa or (fa not in a and a not in fa):
            return False
    elif not fa or fa not in normalize(full_title):  # no artist known: it has to appear in the title
        return False
    if duration and found_secs and abs(duration - found_secs) > tolerance:
        return False
    return True


async def _apple(session, artist, song, full_title, duration, tolerance) -> str | None:
    params = {"term": search_text(artist, song), "entity": "song", "limit": "15", "country": "US"}
    async with session.get("https://itunes.apple.com/search", params=params) as r:
        r.raise_for_status()
        results = (await r.json(content_type=None)).get("results", [])
    for d in results:
        secs = round(d["trackTimeMillis"] / 1000) if d.get("trackTimeMillis") else None
        if _same_song(d.get("artistName", ""), d.get("trackName", ""), secs, artist, song, full_title, duration, tolerance):
            return d.get("trackViewUrl", "").split("?uo=")[0].split("&uo=")[0] or None
    return None


async def _deezer(session, artist, song, full_title, duration, tolerance) -> str | None:
    async with session.get("https://api.deezer.com/search", params={"q": search_text(artist, song), "limit": "15"}) as r:
        r.raise_for_status()
        results = (await r.json(content_type=None)).get("data", [])
    for d in results:
        if _same_song((d.get("artist") or {}).get("name", ""), d.get("title", ""), d.get("duration"),
                      artist, song, full_title, duration, tolerance):
            return d.get("link")
    return None


async def find_links(title: str, play_url: str, link: str | None, artist: str | None, song: str | None,
                     duration: int | None) -> dict[str, str]:
    """{service name: URL} for the song, in display order. `link` is its Spotify/Apple Music page, if it came from one;
    `play_url` is what's actually playing (a YouTube or SoundCloud page)."""
    from_store = bool(song)  # Spotify/Apple Music give exact titles and lengths; a video's length can differ a bit
    if not song:
        artist, song = split_title(title)
    artist = artist or ""
    tolerance = 10 if from_store else 45

    links: dict[str, str] = {}
    if m := YOUTUBE_ID_RE.search(play_url):
        links["YouTube"] = f"https://www.youtube.com/watch?v={m.group(1)}"
        links["YouTube Music"] = f"https://music.youtube.com/watch?v={m.group(1)}"
    if "soundcloud.com" in play_url:
        links["SoundCloud"] = play_url

    spotify = link if link and "spotify.com" in link else None
    apple = link if link and "music.apple.com" in link else None
    deezer = None
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        args = (session, artist, song, title, duration, tolerance)
        for name, lookup in (("apple", _apple), ("deezer", _deezer)):
            if name == "apple" and apple:
                continue
            try:
                found = await lookup(*args)
            except Exception as e:  # a slow or broken service just means no link for it
                print(f"{name} lookup failed for {title!r}: {e!r}")
                continue
            if name == "apple":
                apple = found
            else:
                deezer = found

    links["Spotify" if spotify else "Spotify (search)"] = spotify or (
        "https://open.spotify.com/search/" + quote(search_text(artist, song)))
    if apple:
        links["Apple Music"] = apple
    if deezer:
        links["Deezer"] = deezer
    return links
