"""Mikan RSS anime source adapter (Phase 2) -- RSS-only, magnet-only, zero SSRF.

Implements ``AnimeSourcePort``. ``search`` hits ``RSS/Search?searchstr=..`` on the
configured base_urls (official mikanani.me first), parses the XML with
``xml.etree`` (no brittle string slicing), and builds a magnet from the 40-hex
infohash embedded in the enclosure/Home-Episode URL. It NEVER downloads the
.torrent nor fetches any http(s) URL from an item (no SSRF). ``materialize`` is a
pure repackage -- no network, no side effect (the magnet is ready from search).

Qt-free (CLAUDE.md #1). No os.getenv -- base_urls/timeout are constructor args.
"""

from __future__ import annotations

import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

from spica.anime.models import AnimeCandidate, AnimeResource
from spica.anime.resolver import parse_source_title
from spica.ports.anime_source import AnimeSourceError

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
# The 40-hex token in Mikan's Download/Episode URLs IS the BitTorrent v1 infohash
# (verified in Phase 0). Strictly 40 hex -- so we build the magnet without ever
# fetching the .torrent (P0-3, magnet-only).
_INFOHASH_RE = re.compile(r"([0-9a-fA-F]{40})")


def _default_session() -> Any:
    import requests  # lazy: tests inject a fake session and never import this
    s = requests.Session()
    s.trust_env = False                      # no env proxy (review #5)
    s.headers.update({"User-Agent": _UA})
    return s


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
        last_net_err: Any = None
        parse_err: Any = None
        for base in self._base_urls:
            t = self._budget_timeout()           # raises TIMEOUT when spent (F6)
            url = f"{base}/RSS/Search?searchstr={urllib.parse.quote(title_query)}"
            try:
                resp = self._http.get(url, timeout=t)
            except Exception as e:  # noqa: BLE001 -- network down for this base
                last_net_err = e
                continue
            if getattr(resp, "status_code", 200) != 200:
                last_net_err = f"HTTP {resp.status_code}"
                continue
            try:
                return self._parse(resp.text)
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

    def _parse(self, xml_text: str) -> list[AnimeCandidate]:
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
                display_title=title,
            ))
        return cands

    @staticmethod
    def _infohash(url: str | None) -> str | None:
        m = _INFOHASH_RE.search(url or "")
        return m.group(1) if m else None

    def materialize(self, candidate: AnimeCandidate) -> AnimeResource:
        # The magnet is already built in search -> pure repackage, no network,
        # no side effect (must not start a download -- port contract).
        if not candidate.locator.startswith("magnet:?"):
            raise AnimeSourceError("BAD_CANDIDATE", "not a magnet locator")
        # episode_key is a placeholder: the coordinator is the single generation
        # point and overwrites it with the query-derived canonical key (F2).
        return AnimeResource(
            episode_key="", source=self.name, locator=candidate.locator,
            display_title=candidate.display_title, size_bytes=candidate.size_bytes,
        )
