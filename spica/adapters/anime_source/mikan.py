"""Mikan RSS anime source adapter (Phase 2) -- selected torrent materialization.

Implements ``AnimeSourcePort``. ``search`` hits ``RSS/Search?searchstr=..`` on the
configured base_urls (official mikanani.me first), parses the XML with
``xml.etree`` (no brittle string slicing), and builds a magnet from the 40-hex
infohash embedded in the enclosure/Home-Episode URL. Only after one candidate
is selected, ``materialize`` fetches its same-origin, hash-bound ``.torrent``
without redirects, validates its exact infohash and tracker boundary, and hands
the verified bytes onward. Arbitrary item URLs are never passed to qBittorrent.

Qt-free (CLAUDE.md #1). No os.getenv -- base_urls/timeout are constructor args.
"""

from __future__ import annotations

import base64
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

from spica.anime.models import AnimeCandidate, AnimeResource
from spica.anime.resolver import parse_source_title
from spica.anime.torrent_metadata import (
    MAX_TORRENT_BYTES,
    TorrentMetadataError,
    inspect_torrent,
    validate_public_trackers,
)
from spica.ports.anime_source import AnimeSourceError

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
# The 40-hex token in Mikan's Download/Episode URLs IS the BitTorrent v1 infohash
# (verified in Phase 0). Strictly 40 hex: search can expose a safe locator, then
# materialize verifies the selected torrent bytes against the same hash.
_INFOHASH_RE = re.compile(r"([0-9a-fA-F]{40})")
# search-quality §2.3: mikan's server-side keyword match is punctuation-sensitive
# ("Re从零开始的异世界生活" -> 0 hits; "从零开始的异世界生活" -> 94). On a 0-candidate
# result we retry ONCE with the longest contiguous CJK run of the query.
_CJK_RUN_RE = re.compile(r"[一-鿿]+")
_TORRENT_PATH_RE = re.compile(
    r"(?:^|/)Download/[^/]+/([0-9a-fA-F]{40})\.torrent$")


def _longest_cjk_run(text: str) -> str:
    runs = _CJK_RUN_RE.findall(text)
    return max(runs, key=len) if runs else ""


def _origin(url: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        port = parsed.port
    except ValueError:
        return None
    if (scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password):
        return None
    if (scheme, port) in {("http", 80), ("https", 443)}:
        port = None
    return scheme, parsed.hostname.rstrip(".").lower(), port


def _default_session() -> Any:
    import requests  # lazy: tests inject a fake session and never import this
    s = requests.Session()
    s.trust_env = False                      # no env proxy (review #5)
    s.headers.update({"User-Agent": _UA})
    return s


def _read_torrent_body(resp: Any) -> bytes:
    """Read at most ``MAX_TORRENT_BYTES`` without buffering an unbounded body."""
    headers = getattr(resp, "headers", {}) or {}
    raw_length = headers.get("Content-Length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except (TypeError, ValueError):
            content_length = None
        if content_length is not None and content_length > MAX_TORRENT_BYTES:
            raise TorrentMetadataError("torrent payload exceeds size limit")

    iter_content = getattr(resp, "iter_content", None)
    if callable(iter_content):
        chunks: list[bytes] = []
        size = 0
        for chunk in iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            if not isinstance(chunk, bytes):
                raise TorrentMetadataError("torrent response is not bytes")
            size += len(chunk)
            if size > MAX_TORRENT_BYTES:
                raise TorrentMetadataError("torrent payload exceeds size limit")
            chunks.append(chunk)
        return b"".join(chunks)

    content = getattr(resp, "content", b"")
    if not isinstance(content, bytes):
        raise TorrentMetadataError("torrent response is not bytes")
    if len(content) > MAX_TORRENT_BYTES:
        raise TorrentMetadataError("torrent payload exceeds size limit")
    return content


class MikanRssSource:
    name = "mikan"

    def __init__(self, base_urls: list[str], *, session: Any = None,
                 timeout: float = 15, clock: Any = None) -> None:
        if not base_urls:
            raise ValueError("base_urls must be non-empty")
        self._base_urls = [b.rstrip("/") for b in base_urls]
        self._http = session if session is not None else _default_session()
        self._timeout = timeout
        self._clock = clock if clock is not None else time.monotonic
        self._deadline_at: float | None = None

    def _budget_timeout(self) -> float:
        """Remaining-budget gate before EVERY HTTP request (F6): raises TIMEOUT
        once the per-search deadline is exhausted, else caps the request timeout.
        Must be called OUTSIDE any broad try/except that would swallow it."""
        if self._deadline_at is None:
            return self._timeout
        remaining = self._deadline_at - self._clock()
        if remaining <= 0:
            raise AnimeSourceError("TIMEOUT", "search deadline exceeded")
        return min(self._timeout, remaining)

    def search(self, title_query: str, *,
               deadline: float | None = None) -> list[AnimeCandidate]:
        self._deadline_at = (None if deadline is None
                             else self._clock() + deadline)
        cands = self._search_once(title_query)
        # 0-candidate fallback (§2.3): retry ONCE with the longest CJK run when the
        # server was REACHABLE but matched nothing (a raise -- unreachable / bad
        # XML -- propagates from _search_once and never reaches here). The retry
        # shares the same per-search deadline (_budget_timeout still gates it).
        if not cands:
            fallback = _longest_cjk_run(title_query)
            if fallback and fallback != title_query:
                retried = self._search_once(fallback)
                if retried:
                    return retried
        return cands

    def _search_once(self, title_query: str) -> list[AnimeCandidate]:
        last_net_err: Any = None
        parse_err: Any = None
        for base in self._base_urls:
            t = self._budget_timeout()           # raises TIMEOUT when spent (F6)
            url = f"{base}/RSS/Search?searchstr={urllib.parse.quote(title_query)}"
            try:
                resp = self._http.get(
                    url, timeout=t, allow_redirects=False)
            except Exception as e:  # noqa: BLE001 -- network down for this base
                last_net_err = e
                continue
            if getattr(resp, "status_code", 200) != 200:
                last_net_err = f"HTTP {resp.status_code}"
                continue
            try:
                return self._parse(resp.text, source_origin=base)
            except ET.ParseError as e:
                # a mirror serving an HTML error page -> this base failed; try
                # the next one instead of aborting the whole search (review #3)
                parse_err = e
                continue
        # nothing succeeded: prefer PARSE_ERROR if a base returned bad XML,
        # else all were network/HTTP failures -> SOURCE_UNREACHABLE.
        if parse_err is not None:
            raise AnimeSourceError("PARSE_ERROR", str(parse_err))
        raise AnimeSourceError("SOURCE_UNREACHABLE",
                               f"all base_urls failed: {last_net_err}")

    def _parse(
        self, xml_text: str, *, source_origin: str = "",
    ) -> list[AnimeCandidate]:
        root = ET.fromstring(xml_text)
        cands: list[AnimeCandidate] = []
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            enc = item.find("enclosure")
            enc_url = enc.get("url") if enc is not None else ""
            length = enc.get("length") if enc is not None else None
            link = item.findtext("link") or ""
            ih = self._infohash(enc_url) or self._infohash(link)
            if not ih:
                continue  # no infohash -> can't build a magnet, skip
            parsed = parse_source_title(title)
            if parsed.is_batch:
                continue  # multi-episode torrent -> filtered (D11, port contract)
            magnet = (f"magnet:?xt=urn:btih:{ih.lower()}"
                      f"&dn={urllib.parse.quote(title)}")
            cands.append(AnimeCandidate(
                source=self.name, locator=magnet, parsed=parsed,
                size_bytes=int(length) if length and length.isdigit() else None,
                display_title=title, materialize_url=enc_url,
                materialize_origin=source_origin,
            ))
        return cands

    @staticmethod
    def _infohash(url: str | None) -> str | None:
        m = _INFOHASH_RE.search(url or "")
        return m.group(1) if m else None

    def _torrent_url_is_allowed(
        self,
        url: str,
        expected_infohash: str,
        source_origin: str,
    ) -> bool:
        try:
            target = urllib.parse.urlsplit(url)
        except ValueError:
            return False
        if (target.scheme.lower() not in {"http", "https"}
                or not target.hostname or target.username or target.password
                or target.query or target.fragment):
            return False
        target_origin = _origin(url)
        rss_origin = _origin(source_origin)
        allowed_origins = {
            parsed for base in self._base_urls
            if (parsed := _origin(base)) is not None
        }
        match = _TORRENT_PATH_RE.search(target.path)
        return bool(
            rss_origin in allowed_origins
            and target_origin == rss_origin
            and match is not None
            and match.group(1).lower() == expected_infohash.lower()
        )

    def materialize(self, candidate: AnimeCandidate) -> AnimeResource:
        if not candidate.locator.startswith("magnet:?"):
            raise AnimeSourceError("BAD_CANDIDATE", "not a magnet locator")
        if not candidate.materialize_url:
            raise AnimeSourceError("BAD_CANDIDATE", "missing torrent materialize URL")
        if not candidate.materialize_origin:
            raise AnimeSourceError("BAD_CANDIDATE", "missing RSS source origin")
        expected_infohash = self._infohash(candidate.locator)
        if (expected_infohash is None
                or not self._torrent_url_is_allowed(
                    candidate.materialize_url, expected_infohash,
                    candidate.materialize_origin)):
            raise AnimeSourceError(
                "UNSAFE_TORRENT_URL", "torrent URL is outside configured Mikan origins")
        timeout = self._budget_timeout()
        try:
            resp = self._http.get(
                candidate.materialize_url, timeout=timeout,
                allow_redirects=False, stream=True)
        except Exception as e:  # noqa: BLE001 -- source boundary
            raise AnimeSourceError("SOURCE_UNREACHABLE", str(e)) from e
        try:
            if getattr(resp, "status_code", 200) != 200:
                raise AnimeSourceError(
                    "SOURCE_UNREACHABLE", f"HTTP {resp.status_code}")
            try:
                payload = _read_torrent_body(resp)
            except TorrentMetadataError as exc:
                raise AnimeSourceError("BAD_TORRENT", str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 -- streaming network boundary
                raise AnimeSourceError("SOURCE_UNREACHABLE", str(exc)) from exc
        finally:
            close = getattr(resp, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 -- cleanup must not mask result
                    pass
        try:
            metadata = inspect_torrent(payload)
        except TorrentMetadataError as exc:
            raise AnimeSourceError("BAD_TORRENT", str(exc)) from exc
        try:
            validate_public_trackers(metadata.trackers)
        except TorrentMetadataError as exc:
            raise AnimeSourceError("UNSAFE_TRACKER", str(exc)) from exc
        if metadata.infohash != expected_infohash:
            raise AnimeSourceError(
                "HASH_MISMATCH",
                f"torrent infohash {metadata.infohash} != {expected_infohash}",
            )
        # episode_key is a placeholder: the coordinator is the single generation
        # point and overwrites it with the query-derived canonical key (F2).
        return AnimeResource(
            episode_key="", source=self.name, locator=candidate.locator,
            display_title=candidate.display_title, size_bytes=candidate.size_bytes,
            torrent_payload_b64=base64.b64encode(payload).decode("ascii"),
        )
