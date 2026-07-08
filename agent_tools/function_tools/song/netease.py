from __future__ import annotations

import logging
import re
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from agent_tools.function_tools.song.models import NeteaseSong, SongRequest


logger = logging.getLogger(__name__)


def search_best_song(request: SongRequest, limit: int = 20) -> NeteaseSong:
    cloudsearch = _cloudsearch_api()
    result = cloudsearch.GetSearchResult(
        keyword=request.search_keyword(),
        stype=cloudsearch.SONG,
        limit=limit,
        offset=0,
    )
    songs = result.get("result", {}).get("songs", [])
    if not songs:
        raise RuntimeError(f"没有搜到歌曲：{request.search_keyword()}")

    candidates = [_song_from_raw(song, request) for song in songs if song.get("id")]
    if not candidates:
        raise RuntimeError("搜索结果里没有可用的歌曲 ID。")
    return max(candidates, key=lambda item: item.score)


def get_audio_url(song_id: str, bitrate: int = 320000) -> str:
    _ensure_saved_pyncm_session_loaded()
    apis = _pyncm_apis()
    audio = apis.track.GetTrackAudio(int(song_id), bitrate=bitrate)
    data = audio.get("data", [])
    url = data[0].get("url") if data else None
    if not url:
        raise RuntimeError("没有拿到可播放 URL，可能是版权、地区、登录或接口限制。")
    return str(url)


def download_audio(url: str, output_path: Path, timeout_sec: int = 60, user_agent: str | None = None) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent or "Mozilla/5.0 Spica-Chatbot SongTool"},
    )
    tmp_path = output_path.with_suffix(output_path.suffix + ".part")
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:
        with tmp_path.open("wb") as file:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                file.write(chunk)
    tmp_path.replace(output_path)
    return output_path


def extension_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    suffix = Path(path).suffix.lower()
    if re.fullmatch(r"\.(mp3|m4a|flac|wav|aac|ogg)", suffix):
        return suffix
    return ".mp3"


def _song_from_raw(raw: dict[str, Any], request: SongRequest) -> NeteaseSong:
    artists = [str(item.get("name") or "") for item in (raw.get("ar") or raw.get("artists") or [])]
    title = str(raw.get("name") or "")
    song = NeteaseSong(
        song_id=str(raw.get("id")),
        title=title,
        artists=[artist for artist in artists if artist],
        album=str((raw.get("al") or raw.get("album") or {}).get("name") or ""),
        raw=raw,
    )
    song.score = _score_song(song, request)
    return song


def _score_song(song: NeteaseSong, request: SongRequest) -> float:
    query_score = _ratio(_normalize(request.search_keyword()), _normalize(f"{song.title} {song.artist_text}"))
    title_score = _ratio(_normalize(request.title or request.query), _normalize(song.title))
    artist_score = 0.0
    if request.artist:
        artist_score = _ratio(_normalize(request.artist), _normalize(song.artist_text))
    exact_bonus = 0.0
    if request.title and _normalize(request.title) == _normalize(song.title):
        exact_bonus += 0.2
    if request.artist and _normalize(request.artist) in _normalize(song.artist_text):
        exact_bonus += 0.15
    return query_score * 0.35 + title_score * 0.45 + artist_score * 0.2 + exact_bonus


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def _ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.92
    return SequenceMatcher(None, left, right).ratio()


def _ensure_saved_pyncm_session_loaded(session_path: Path | None = None) -> bool:
    try:
        import pyncm
    except Exception:
        return False

    current_session = pyncm.GetCurrentSession()
    if current_session.logged_in:
        return True

    path = session_path or _default_pyncm_session_path()
    if not path.exists():
        return False
    try:
        loaded_session = pyncm.LoadSessionFromString(path.read_text(encoding="utf-8").strip())
        pyncm.SetCurrentSession(loaded_session)
    except Exception:  # pragma: no cover - depends on local pyncm save format
        logger.warning("无法加载 pyncm 登录态文件：%s", path)
        return False
    return bool(pyncm.GetCurrentSession().logged_in)


def _default_pyncm_session_path() -> Path:
    return Path.home() / ".pyncm"


def _pyncm_apis() -> Any:
    try:
        from pyncm import apis
    except Exception as exc:  # pragma: no cover - depends on local runtime deps
        raise RuntimeError("缺少 pyncm，请先在运行环境安装 pyncm。") from exc
    return apis


def _cloudsearch_api() -> Any:
    try:
        from pyncm.apis import cloudsearch
    except Exception as exc:  # pragma: no cover - depends on local runtime deps
        raise RuntimeError("缺少 pyncm，请先在运行环境安装 pyncm。") from exc
    return cloudsearch
