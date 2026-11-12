"""Find the YouTube video that best matches a Spotify/Apple Music song.

Searches YouTube twice in parallel and scores every candidate on how closely its length matches the real song,
whether the title and artist match, whether it's an official upload, and whether it looks like the wrong version
(live, cover, sped up, ...). Length is the strongest signal: wrong versions almost always differ by many seconds.
"""

import asyncio
import math
import re
import unicodedata

import yt_dlp

# Words that mark a different version of a song. Penalized unless the real song's title has them too.
WRONG_VERSION = [
    "live", "cover", "karaoke", "instrumental", "remix", "sped up", "speed up", "slowed", "reverb", "nightcore",
    "8d", "acoustic", "mashup", "reaction", "tutorial", "lesson", "1 hour", "10 hours", "hour loop", "loop",
    "bass boosted", "extended", "piano", "guitar", "drum", "performance", "concert", "fan made", "ai cover",
    "parody", "chipmunk", "teaser", "trailer", "snippet", "preview", "behind the scenes", "interview", "shorts",
    "edit audio", "tiktok", "festival", "tour", "unplugged", "demo", "rehearsal", "medley", "megamix",
    "mix", "rework", "vip", "bootleg", "dub", "flip",
]
# Normal labels inside (...) or [...] in a video title; any other bracketed extra hints at a different version.
NORMAL_LABEL_WORDS = set(
    "official audio video music lyric lyrics visualizer visualiser hd hq 4k 8k 1080p remaster remastered remasterd "
    "explicit clean mv m v color coded colour with feat ft featuring full song single album track topic oficial "
    "letra audioofficial officialaudio officialvideo animated and the from soundtrack original version".split()
)
WRONG_VERSION_RE = [(w, re.compile(rf"\b{re.escape(w)}\b")) for w in WRONG_VERSION]
VIDEO_RE = re.compile(r"\b(?:music video|official video|lyric video|lyrics video|visualizer|4k video|hd video|video oficial)\b")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower().replace("&", " and ")
    text = re.sub(r"[’'`]", "", text)
    return re.sub(r"[^\w]+", " ", text).strip()


def core_title(title: str) -> str:
    """'Song (feat. X) - Remastered 2011' -> 'Song'."""
    core = re.sub(r"[\(\[].*?[\)\]]", " ", title)
    core = re.split(r"\s+-\s+", core)[0]
    core = re.split(r"\b(?:feat|ft|featuring)\b\.?", core, flags=re.I)[0]
    return normalize(core) or normalize(title)


def duration_score(target: int | None, actual: int | None) -> float:
    if not target or not actual:
        return 0
    diff = abs(target - actual)
    if diff <= 1:
        return 38
    if diff <= 2:
        return 35
    if diff <= 3:
        return 31
    if diff <= 5:
        return 25
    if diff <= 10:
        return 15
    if diff <= 20:
        return 0
    if diff <= 45:
        return -25
    return -60


def score(candidate: dict, artist: str, title: str, duration: int | None, rank: int) -> float:
    cand_title = normalize(candidate.get("title") or "")
    channel = normalize(candidate.get("channel") or candidate.get("uploader") or "")
    cand_words = set(cand_title.split())
    original = normalize(f"{artist} {title}")
    primary_artist = normalize(re.split(r",|&| and | x | feat\.? | ft\.? ", artist, flags=re.I)[0]) if artist else ""

    s = 0.0
    # Song title present in the video title.
    core = core_title(title)
    if core and f" {core} " in f" {cand_title} ":
        s += 40
    elif core:
        words = core.split()
        s += 40 * sum(w in cand_words for w in words) / len(words) - 10

    # Artist in the video title or the channel name; official channels get extra.
    if primary_artist:
        channel_flat = channel.replace(" ", "")
        artist_flat = primary_artist.replace(" ", "")
        if f" {primary_artist} " in f" {cand_title} " or artist_flat in channel_flat:
            s += 25
        else:
            words = primary_artist.split()
            s += 25 * sum(w in cand_words or w in channel.split() for w in words) / len(words) - 10
        if channel.endswith(" topic") and artist_flat in channel_flat:
            s += 15  # "Artist - Topic": YouTube's auto-generated official audio
        elif channel_flat in (artist_flat, artist_flat + "vevo", artist_flat + "official", "official" + artist_flat):
            s += 10
    if candidate.get("channel_is_verified"):
        s += 3

    s += duration_score(duration, candidate.get("duration"))

    for word, pattern in WRONG_VERSION_RE:
        if pattern.search(cand_title) and not pattern.search(original):
            s -= 40
    if VIDEO_RE.search(cand_title):
        s -= 4  # music/lyric videos often add intros or outros; prefer the plain audio upload

    # Bracketed extras that aren't normal labels and aren't in the real title: "(Devonshire Mix)", "(Sped Up Ver.)".
    original_words = set(original.split())
    for extra in re.findall(r"[\(\[]([^\)\]]*)[\)\]]", candidate.get("title") or ""):
        words = [w for w in normalize(extra).split() if not w.isdigit()]
        if any(w not in NORMAL_LABEL_WORDS and w not in original_words for w in words):
            s -= 12

    s += min(9.0, math.log10((candidate.get("view_count") or 0) + 1))  # popularity, gently
    s -= 0.5 * rank  # YouTube's own ranking breaks ties
    return s


def _search(query: str, count: int, ydl_options: dict) -> list[dict]:
    opts = dict(ydl_options, extract_flat=True, quiet=True, no_warnings=True)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
    return [e for e in info.get("entries") or [] if e and e.get("id")]


async def best_match(artist: str, title: str, duration: int | None, ydl_options: dict) -> tuple[str, dict] | None:
    """Return (video URL, chosen candidate) for the best-scoring YouTube video, or None if nothing was found."""
    base = f"{artist} - {title}" if artist else title
    results = await asyncio.gather(
        asyncio.to_thread(_search, base, 10, ydl_options),
        # The full title ("... - Remastered 2011") plus "audio" tends to surface the official "Artist - Topic" upload.
        asyncio.to_thread(_search, f"{base} audio", 6, ydl_options),
        return_exceptions=True,
    )
    candidates: dict[str, tuple[dict, int]] = {}
    for result in results:
        if isinstance(result, Exception):
            continue
        for rank, entry in enumerate(result):
            if entry["id"] not in candidates or rank < candidates[entry["id"]][1]:
                candidates[entry["id"]] = (entry, rank)
    if not candidates:
        return None
    best, _ = max(candidates.values(), key=lambda c: score(c[0], artist, title, duration, c[1]))
    return f"https://www.youtube.com/watch?v={best['id']}", best
