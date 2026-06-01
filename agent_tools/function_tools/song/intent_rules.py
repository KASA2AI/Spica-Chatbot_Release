from __future__ import annotations

import re

from agent_tools.function_tools.song.intent import SongAction, SongContext, SongIntent, SongState


_TAIL_RE = re.compile(r"(?:可以吗|行吗|好吗|好不好|可以不|吗|嘛|么|吧|呗|呀|啊|呢|[?？!！。,.，、])+$")

_REJECT_PATTERNS = (
    r"你(会|能|可以).*唱歌(?:吗|么)?[?？]?$",
    r"唱歌功能怎么用",
    r"唱歌怎么触发",
    r"这首歌.*(讲了什么|讲什么|什么意思|表达什么)",
    r"这歌.*(讲了什么|讲什么|什么意思|表达什么)",
    r"歌词是什么",
    r"帮我写一首歌",
    r"帮我写歌词",
    r"唱歌模型怎么训练",
    r"rvc\s*是什么",
    r"gpt-?sovits\s*是什么",
)

_COMMAND_PATTERNS = (
    r"^(?:spica|斯皮卡)\s*(?:唱歌|唱一首|唱首|唱一下|来一首)\s*(?P<song>.+)$",
    r"^(?:请|麻烦)?\s*(?:spica|斯皮卡)?\s*唱一首\s*(?P<song>.+)$",
    r"^(?:请|麻烦)?\s*(?:spica|斯皮卡)?\s*唱首\s*(?P<song>.+)$",
    r"^(?:请|麻烦)?\s*(?:spica|斯皮卡)?\s*唱一下\s*(?P<song>.+)$",
    r"^来一首\s*(?P<song>.+)$",
    r"^播放\s*(?P<song>.+)$",
    r"^用(?:你的|spica的|斯皮卡的)?声音\s*(?:唱|cover|翻唱)\s*(?P<song>.+)$",
    r"^用\s*(?:spica|斯皮卡)\s*的声音\s*(?:唱|cover|翻唱)\s*(?P<song>.+)$",
    r"^我想听你唱\s*(?P<song>.+)$",
    r"^帮我唱一首\s*(?P<song>.+)$",
    r"^帮我唱一下\s*(?P<song>.+)$",
)

_LISTEN_PATTERNS = (
    r"^(?:(?:spica|斯皮卡)[,，、\s]*)?(?:我(?:还|也|再|就)?想听|我(?:还|也|再|就)?要听|(?:还|也|再|就)?想听)\s*(?P<song>.+)$",
    r"^.*?[，,。.!！?？、\s](?:我(?:还|也|再|就)?想听|我(?:还|也|再|就)?要听|(?:还|也|再|就)?想听)\s*(?P<song>.+)$",
)

_SEARCH_PATTERNS = (
    r"^(?:来点|来一些)\s*(?P<object>.+)$",
    r"^(?:唱|唱一下|唱一首|来一首)\s*(?P<object>(?:.+?)(?:的歌|歌曲|音乐|风格))$",
)

_WEAK_OBJECTS = {"歌", "歌曲", "音乐", "一首歌", "随便", "随便一首", "什么歌"}
_STYLE_KEYWORDS = {
    "摇滚",
    "流行",
    "爵士",
    "民谣",
    "古风",
    "电子",
    "纯音乐",
    "日语歌",
    "英文歌",
    "中文歌",
    "粤语歌",
    "动漫歌",
    "游戏音乐",
}


def normalize_song_text(text: str) -> str:
    text = (text or "").strip()
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)
    return text


def parse_song_control_intent(
    text: str,
    state: SongState,
    context: SongContext | None = None,
) -> SongIntent:
    del context
    normalized = normalize_song_text(text)
    current_state = _coerce_state(state)
    if not normalized:
        return _intent(SongAction.NONE, 0.0, text)

    if current_state in {SongState.PREPARING, SongState.PLAYING, SongState.PAUSED, SongState.READY}:
        if _matches_any(normalized, (r"^(别唱了|不要唱了|取消|算了|停掉|stop)$",)):
            return _intent(SongAction.CANCEL, 0.98, text, reason="control_cancel")

    if current_state in {SongState.PLAYING, SongState.PREPARING}:
        if _matches_any(normalized, (r"^(暂停|暂停一下|停一下|等一下|先停|pause)$",)):
            return _intent(SongAction.PAUSE, 0.98, text, reason="control_pause")
    if current_state == SongState.PLAYING:
        if _matches_any(normalized, (r"^(重来|重新唱|从头来|再唱一遍)$",)):
            return _intent(SongAction.RESTART, 0.94, text, reason="control_restart")
        change = re.match(r"^(换成|改唱)\s*(?P<song>.+)$", normalized)
        if change:
            song_text = _clean_song_text(change.group("song"))
            artist, title = _split_artist_title(song_text)
            return _intent(
                SongAction.CHANGE,
                0.96,
                text,
                query=_build_query(title, artist, song_text),
                title=title,
                artist=artist,
                reason="control_change_with_song",
            )
        if _matches_any(normalized, (r"^(换一首|下一首)$",)):
            return _intent(
                SongAction.CHANGE,
                0.94,
                text,
                reason="control_change",
                needs_confirmation=True,
            )

    if current_state in {SongState.PAUSED, SongState.READY}:
        if _matches_any(normalized, (r"^(继续|继续唱|接着唱|可以继续了|resume)$",)):
            return _intent(SongAction.RESUME, 0.98, text, reason="control_resume")

    return _intent(SongAction.NONE, 0.0, text)


def parse_song_command_intent(text: str) -> SongIntent:
    normalized = normalize_song_text(text)
    if not normalized:
        return _intent(SongAction.NONE, 0.0, text)

    if any(re.search(pattern, normalized, flags=re.I) for pattern in _REJECT_PATTERNS):
        return _intent(SongAction.REJECT, 0.96, text, reason="negative_song_query")

    for pattern in _COMMAND_PATTERNS:
        match = re.search(pattern, normalized, flags=re.I)
        if not match:
            continue
        song_text = _clean_song_text(match.group("song"))
        if not song_text or _looks_like_generic_object(song_text) or _looks_like_artist_or_style_request(song_text):
            return _intent(
                SongAction.SEARCH,
                0.78 if song_text else 0.72,
                text,
                query=song_text or None,
                reason="missing_specific_song",
                needs_confirmation=True,
            )
        if _looks_like_non_song_question(song_text):
            return _intent(SongAction.REJECT, 0.92, text, reason="non_song_question")
        artist, title = _split_artist_title(song_text)
        return _intent(
            SongAction.SING,
            0.94,
            text,
            query=_build_query(title, artist, song_text),
            title=title,
            artist=artist,
            reason="explicit_song_command",
        )

    for pattern in _LISTEN_PATTERNS:
        match = re.search(pattern, normalized, flags=re.I)
        if not match:
            continue
        song_text = _clean_song_text(match.group("song"))
        if not song_text or _looks_like_generic_object(song_text) or _looks_like_artist_or_style_request(song_text):
            return _intent(
                SongAction.SEARCH,
                0.78 if song_text else 0.66,
                text,
                query=song_text or None,
                reason="listen_request_needs_song",
                needs_confirmation=True,
            )
        artist, title = _split_artist_title(song_text)
        return _intent(
            SongAction.SING,
            0.92,
            text,
            query=_build_query(title, artist, song_text),
            title=title,
            artist=artist,
            reason="explicit_listen_song",
        )

    for pattern in _SEARCH_PATTERNS:
        match = re.search(pattern, normalized, flags=re.I)
        if not match:
            continue
        object_text = _clean_song_text(match.group("object"))
        confidence = 0.66 if not object_text or _looks_like_generic_object(object_text) else 0.78
        return _intent(
            SongAction.SEARCH,
            confidence,
            text,
            query=object_text or None,
            reason="generic_music_request" if confidence < 0.7 else "artist_or_style_request",
            needs_confirmation=True,
        )

    return _intent(SongAction.NONE, 0.0, text)


def parse_song_followup_intent(
    text: str,
    state: SongState,
    context: SongContext | None = None,
) -> SongIntent:
    current_state = _coerce_state(state)
    has_selection_context = current_state == SongState.CANDIDATE_SELECTING or bool(
        getattr(context, "pending_candidates", None) or getattr(context, "candidate_options", None)
    )
    if not has_selection_context:
        return _intent(SongAction.NONE, 0.0, text)

    normalized = normalize_song_text(text)
    index_map = {
        "1": 0,
        "第一个": 0,
        "第一首": 0,
        "就第一个": 0,
        "2": 1,
        "第二个": 1,
        "第二首": 1,
        "就第二个": 1,
        "3": 2,
        "第三个": 2,
        "第三首": 2,
        "就第三个": 2,
    }
    if normalized in index_map:
        return _intent(
            SongAction.CONFIRM,
            0.95,
            text,
            candidate_index=index_map[normalized],
            reason="candidate_index",
        )
    if normalized in {"就这个", "这个"}:
        return _intent(
            SongAction.CONFIRM,
            0.90,
            text,
            candidate_index=0,
            reason="candidate_current",
        )
    return _intent(SongAction.NONE, 0.0, text)


def _coerce_state(state: SongState) -> SongState:
    if isinstance(state, SongState):
        return state
    try:
        return SongState(str(state))
    except ValueError:
        return SongState.IDLE


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.I) for pattern in patterns)


def _intent(
    action: SongAction,
    confidence: float,
    original_text: str,
    *,
    query: str | None = None,
    title: str | None = None,
    artist: str | None = None,
    candidate_index: int | None = None,
    reason: str = "",
    needs_confirmation: bool = False,
) -> SongIntent:
    return SongIntent(
        action=action,
        confidence=confidence,
        query=query,
        title=title,
        artist=artist,
        candidate_index=candidate_index,
        reason=reason,
        needs_confirmation=needs_confirmation,
        source="rule",
        original_text=original_text,
    )


def _clean_song_text(text: str) -> str:
    text = normalize_song_text(text)
    text = text.strip("「」『』《》“”\"' ")
    previous = None
    while previous != text:
        previous = text
        text = _TAIL_RE.sub("", text).strip()
    return text.strip("「」『』《》“”\"' ")


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


def _build_query(title: str | None, artist: str | None, fallback: str) -> str:
    query = " ".join(part for part in (title, artist) if part)
    return query or fallback


def _looks_like_generic_object(song_text: str) -> bool:
    text = _clean_song_text(song_text)
    return text in _WEAK_OBJECTS


def _looks_like_artist_or_style_request(song_text: str) -> bool:
    text = _clean_song_text(song_text)
    return text.endswith(("的歌", "的歌曲", "歌曲", "音乐", "风格")) or text in _STYLE_KEYWORDS


def _looks_like_non_song_question(song_text: str) -> bool:
    return song_text in {"歌", "歌曲", "音乐"} or "什么歌" in song_text
