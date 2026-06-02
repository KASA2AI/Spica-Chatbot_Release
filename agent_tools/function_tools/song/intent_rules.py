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
    "摇滚歌",
    "摇滚音乐",
    "流行",
    "流行歌",
    "流行音乐",
    "爵士",
    "爵士歌",
    "爵士音乐",
    "民谣",
    "民谣歌",
    "民谣音乐",
    "古风",
    "古风歌",
    "古风音乐",
    "电子",
    "电子音乐",
    "纯音乐",
    "日语歌",
    "日文歌",
    "日语音乐",
    "英文歌",
    "英语歌",
    "英文音乐",
    "中文歌",
    "粤语歌",
    "动漫歌",
    "动漫音乐",
    "游戏音乐",
}

_AMBIGUOUS_FOLLOWUP_PATTERNS = (
    r"^(随便|随便一首|都行|都可以)$",
    r"^(他的|她的|它的)?代表作$",
    r"^那首.*(火|红|热门|经典).*$",
    r"^(伤感|开心|治愈|安静|轻松|热血|燃|舒缓|温柔|浪漫).*的?$",
    r"^不要太(吵|闹|快).*$",
    r"^适合.*的?$",
)

_PLAIN_CHAT_FOLLOWUP_PATTERNS = (
    r"^(你|妳|我们|咱们).*(刚刚|刚才|之前|说到|聊到|讲到|继续聊|继续说).*$",
    r".*(建议|解释|安慰|问题|方案|原因|代码|报错|视频|录音|下一段|上一段).*",
    r".*(讲讲|说说|聊聊|分析|解释一下).*$",
    r".*(什么|怎么|为什么|哪里|哪了|哪儿).*$",
)

_NON_SONG_OBJECT_KEYWORDS = (
    "建议",
    "解释",
    "安慰",
    "问题",
    "方案",
    "答案",
    "意见",
    "视频",
    "录音",
    "下一段",
    "上一段",
    "这段",
    "那段",
    "音频",
    "播客",
    "有声书",
)


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

    if current_state in {
        SongState.INTENT_CONFIRMING,
        SongState.PREPARING,
        SongState.PLAYING,
        SongState.PAUSED,
        SongState.READY,
    }:
        if _matches_any(normalized, (r"^(别唱了|不要唱了|不听了|取消|算了|停掉|stop)$",)):
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
        if _looks_like_non_song_object(song_text):
            return _intent(SongAction.REJECT, 0.95, text, reason="non_song_object")
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
        if _looks_like_non_song_object(song_text):
            return _intent(SongAction.REJECT, 0.95, text, reason="non_song_object")
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
        if _looks_like_non_song_object(object_text):
            return _intent(SongAction.REJECT, 0.95, text, reason="non_song_object")
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
    normalized = normalize_song_text(text)

    if current_state == SongState.INTENT_CONFIRMING:
        if not normalized:
            return _intent(SongAction.NONE, 0.0, text)
        if _matches_any(normalized, (r"^(不听了|取消|算了|别唱了|不要唱了)$",)):
            return _intent(SongAction.CANCEL, 0.98, text, reason="followup_cancel")
        if any(re.search(pattern, normalized, flags=re.I) for pattern in _REJECT_PATTERNS):
            return _intent(SongAction.REJECT, 0.96, text, reason="negative_song_query")

        song_text = _extract_followup_song_text(normalized)
        if song_text is not None:
            if _looks_like_non_song_object(song_text) or _looks_like_plain_chat_followup(song_text):
                return _intent(SongAction.REJECT, 0.94, text, reason="non_song_followup")
            if (
                not song_text
                or _looks_like_generic_object(song_text)
                or _looks_like_artist_or_style_request(song_text)
                or _needs_llm_followup(song_text)
            ):
                return _intent(SongAction.NONE, 0.0, text, reason="ambiguous_song_followup")
            artist, title = _split_artist_title(song_text)
            return _intent(
                SongAction.SING,
                0.93,
                text,
                query=_build_query(title, artist, song_text),
                title=title,
                artist=artist,
                reason="intent_confirming_song_title",
            )

        if _looks_like_plain_chat_followup(normalized) or _looks_like_non_song_object(normalized):
            return _intent(SongAction.REJECT, 0.94, text, reason="plain_chat_followup")
        if _needs_llm_followup(normalized):
            return _intent(SongAction.NONE, 0.0, text, reason="ambiguous_song_followup")
        return _intent(SongAction.NONE, 0.0, text)

    has_selection_context = current_state == SongState.CANDIDATE_SELECTING or bool(
        getattr(context, "pending_candidates", None) or getattr(context, "candidate_options", None)
    )
    if not has_selection_context:
        return _intent(SongAction.NONE, 0.0, text)

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


def _looks_like_non_song_object(text: str) -> bool:
    cleaned = _clean_song_text(text)
    if not cleaned:
        return False
    if cleaned in _STYLE_KEYWORDS:
        return False
    return any(keyword in cleaned for keyword in _NON_SONG_OBJECT_KEYWORDS)


def extract_pending_song_hint(intent: SongIntent) -> tuple[str | None, str | None, str | None]:
    raw_query = _clean_song_text(intent.query or intent.title or intent.artist or "")
    artist = _clean_song_text(intent.artist or "")
    style = None

    inferred_artist, inferred_style = _extract_artist_style_hint(raw_query)
    if not artist:
        artist = inferred_artist or ""
    style = inferred_style

    return (raw_query or None), (artist or None), (style or None)


def update_pending_song_hint_from_intent(context: SongContext | None, intent: SongIntent) -> None:
    if context is None:
        return
    raw_query, artist, style = extract_pending_song_hint(intent)
    context.pending_song_raw_query = raw_query
    context.pending_song_artist = artist
    context.pending_song_style = style


def _extract_followup_song_text(text: str) -> str | None:
    explicit_patterns = (
        r"^(?:我(?:还|也|再|就)?想听|我(?:还|也|再|就)?要听|(?:还|也|再|就)?想听)\s*(?P<song>.+)$",
        r"^(?:来一首|请唱|唱一下|唱一首|播放)\s*(?P<song>.+)$",
    )
    for pattern in explicit_patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return _clean_song_text(match.group("song"))
    if _needs_llm_followup(text):
        return _clean_song_text(text)
    if _looks_like_plain_chat_followup(text):
        return None
    return _clean_song_text(text)


def _needs_llm_followup(text: str) -> bool:
    cleaned = _clean_song_text(text)
    if not cleaned:
        return False
    if cleaned in _WEAK_OBJECTS:
        return True
    return _matches_any(cleaned, _AMBIGUOUS_FOLLOWUP_PATTERNS)


def _looks_like_plain_chat_followup(text: str) -> bool:
    cleaned = _clean_song_text(text)
    if not cleaned:
        return False
    return _matches_any(cleaned, _PLAIN_CHAT_FOLLOWUP_PATTERNS)


def _extract_artist_style_hint(text: str) -> tuple[str | None, str | None]:
    cleaned = _clean_song_text(text)
    if not cleaned:
        return None, None
    if cleaned in _STYLE_KEYWORDS:
        return None, cleaned

    possessive = re.match(r"^(.+?)\s*的\s*(?:歌|歌曲|音乐)$", cleaned)
    if possessive:
        value = _clean_song_text(possessive.group(1))
        if value in _STYLE_KEYWORDS:
            return None, value
        return (value or None), None

    for suffix in ("歌曲", "音乐", "风格"):
        if cleaned.endswith(suffix):
            value = _clean_song_text(cleaned[: -len(suffix)])
            if not value:
                return None, None
            if value in _STYLE_KEYWORDS or suffix == "风格":
                return None, value
            return value, None

    return cleaned, None
