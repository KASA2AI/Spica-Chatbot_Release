from __future__ import annotations

import re

from agent_tools.function_tools.song.models import SongRequest


_REJECT_PATTERNS = (
    r"你(会|能|可以).*唱歌(吗|么|\?)?$",
    r"(你|spica|斯皮卡).*喜欢听什么",
    r"这首歌.*(讲了什么|讲什么|什么意思|表达什么)",
    r"这歌.*(讲了什么|讲什么|什么意思|表达什么)",
)

_COMMAND_PATTERNS = (
    r"^(?:spica|斯皮卡)\s*(?:唱歌|唱一首|唱首|唱一下|来一首)\s*(?P<song>.+)$",
    r"^(?:请|麻烦)?\s*(?:spica|斯皮卡)?\s*唱一首\s*(?P<song>.+)$",
    r"^(?:请|麻烦)?\s*(?:spica|斯皮卡)?\s*唱首\s*(?P<song>.+)$",
    r"^(?:请|麻烦)?\s*(?:spica|斯皮卡)?\s*唱一下\s*(?P<song>.+)$",
    r"^(?:spica|斯皮卡)?\s*(?:能|可以|可不可以|能不能|能否).*?唱一首\s*(?P<song>.+?)(?:吗|嘛|么|不|\?)?$",
    r"^来一首\s*(?P<song>.+)$",
    r"^换一首\s*(?P<song>.+)$",
)

_TAIL_RE = re.compile(r"(?:可以吗|行吗|好吗|好不好|可以不|吗|嘛|么|吧|呗|呀|啊|呢|[?？!！。,.，、])+$")


def parse_song_request(user_text: str) -> SongRequest | None:
    text = _compact(user_text)
    if not text:
        return None
    if any(re.search(pattern, text, flags=re.I) for pattern in _REJECT_PATTERNS):
        return None

    song_text = ""
    for pattern in _COMMAND_PATTERNS:
        match = re.search(pattern, text, flags=re.I)
        if match:
            song_text = _clean_song_text(match.group("song"))
            break
    if not song_text:
        return None
    if _looks_like_non_song_question(song_text):
        return None

    artist, title = _split_artist_title(song_text)
    query = " ".join(part for part in (title, artist) if part) or song_text
    return SongRequest(
        query=query,
        title=title or None,
        artist=artist or None,
        user_text=user_text,
    )


def _compact(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _clean_song_text(text: str) -> str:
    text = _compact(text)
    text = text.strip("「」『』“”\"' ")
    previous = None
    while previous != text:
        previous = text
        text = _TAIL_RE.sub("", text).strip()
    return text.strip("「」『』“”\"' ")


def _split_artist_title(song_text: str) -> tuple[str | None, str | None]:
    song_text = _clean_song_text(song_text)
    if not song_text:
        return None, None

    dash_match = re.match(r"^(.+?)\s*[-–—]\s*(.+)$", song_text)
    if dash_match:
        left = _clean_song_text(dash_match.group(1))
        right = _clean_song_text(dash_match.group(2))
        return (left or None), (right or None)

    possessive_match = re.match(r"^(.+?)\s*的\s*(.+)$", song_text)
    if possessive_match:
        artist = _clean_song_text(possessive_match.group(1))
        title = _clean_song_text(possessive_match.group(2))
        if artist and title:
            return artist, title
    return None, song_text


def _looks_like_non_song_question(song_text: str) -> bool:
    return song_text in {"歌", "歌曲", "音乐"} or "什么歌" in song_text
