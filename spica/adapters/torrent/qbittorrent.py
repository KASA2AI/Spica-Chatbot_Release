"""qBittorrent Web API torrent client adapter (Phase 2).

Implements ``TorrentClientPort`` (whitelisted action surface, CLAUDE.md #9):
- ``add_magnet`` accepts ONLY ``magnet:?xt=urn:btih:<40hex>..`` (review #4:
  strictly 40-hex btih; base32 is v1.1). It rejects http(s) torrent URLs, file
  paths, and any non-magnet, so qbt never fetches an arbitrary URL. It STARTS the
  download (``paused=false`` -- the port has no resume, review #1). ``save_dir``
  is pinned at construction and is never a call argument.
- ``add_torrent_bytes`` accepts ONLY an in-memory, size-capped bencoded payload,
  re-validates its exact v1 infohash and direct tracker boundary, and uploads it
  as multipart data. No caller-controlled URL or local path reaches qbt.
- ``status`` / ``cancel`` operate ONLY within category ``spica-anime`` -- they
  never touch torrents the user added by hand (P2-20).

Qt-free (CLAUDE.md #1). No os.getenv -- url / save_dir / credentials are
constructor args; the default session sets ``trust_env = False``.
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Any

from spica.anime.models import DownloadStatus
from spica.anime.torrent_metadata import (
    TorrentMetadataError,
    inspect_torrent,
    validate_public_trackers,
)
from spica.ports.torrent_client import TorrentClientError

_CATEGORY = "spica-anime"
_BTIH_XT_RE = re.compile(r"^urn:btih:([0-9a-fA-F]{40})$")
_BTIH_HEX_RE = re.compile(r"^[0-9a-fA-F]{40}$")
# qbt states that mean "download finished" (seeding / paused-after-complete).
_DONE_STATES = frozenset({
    "uploading", "pausedUP", "stalledUP", "forcedUP", "queuedUP", "checkingUP",
})


def _extract_btih_40hex(magnet: str) -> str | None:
    """Return the lowercase 40-hex btih, or None if ``magnet`` is not a strict
    ``magnet:?..xt=urn:btih:<40hex>..`` (review #4). Rejects base32 / http / file."""
    if not isinstance(magnet, str) or not magnet.startswith("magnet:?"):
        return None
    params = urllib.parse.parse_qs(magnet[len("magnet:?"):])
    for xt in params.get("xt", []):
        m = _BTIH_XT_RE.match(xt)
        if m:
            return m.group(1).lower()
    return None


def _default_session(referer: str) -> Any:
    import requests  # lazy: tests inject a fake session
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"Referer": referer})
    return s


class QBittorrentClient:
    def __init__(self, base_url: str, save_dir: str, *, username: str | None = None,
                 password: str | None = None, session: Any = None,
                 timeout: float = 10) -> None:
        self._base = base_url.rstrip("/")
        # pinned absolute path handed to the qbt daemon (~ won't expand remotely)
        self._save_dir = str(Path(save_dir).expanduser().resolve())
        self._category = _CATEGORY
        self._username = username
        self._password = password
        self._http = session if session is not None else _default_session(self._base)
        self._timeout = timeout
        self._authed = False

    # -- port ----------------------------------------------------------------

    def add_magnet(self, magnet: str, *, subfolder: str | None = None) -> str:
        btih = _extract_btih_40hex(magnet)
        if btih is None:
            raise TorrentClientError(
                "BAD_MAGNET", "only magnet:?xt=urn:btih:<40hex> is accepted")
        resp = self._post("torrents/add", data={
            "urls": magnet,
            "category": self._category,
            "savepath": self._savepath_for(subfolder),   # base save_dir, optionally /<subfolder>
            "paused": "false",            # actually START (review #1)
        })
        # qbt answers HTTP 200 with body "Fails." when the add is rejected (F7).
        # A duplicate infohash is the common cause (P2-3): if THIS btih is already
        # a task in OUR category the add is idempotent -> reuse it, so a re-request
        # of an already-downloading episode is not a dead end. Any other "Fails."
        # (btih not in our category) stays a real ADD_FAILED. Category scope is
        # unchanged -- _category_tasks() still filters to spica-anime only.
        body = (getattr(resp, "text", "") or "").strip()
        if body != "Ok.":
            for t in self._category_tasks():
                if str(t.get("hash", "")).lower() == btih:
                    return btih            # already ours + running -> reuse
            raise TorrentClientError("ADD_FAILED", f"qbt rejected add: {body!r}")
        return btih                        # lowercase 40-hex task_id

    def add_torrent_bytes(
        self,
        payload: bytes,
        *,
        expected_infohash: str,
        subfolder: str | None = None,
    ) -> str:
        expected = str(expected_infohash or "").lower()
        if _BTIH_HEX_RE.fullmatch(expected) is None:
            raise TorrentClientError("BAD_TORRENT", "invalid expected infohash")
        try:
            metadata = inspect_torrent(payload)
            validate_public_trackers(metadata.trackers)
        except TorrentMetadataError as exc:
            raise TorrentClientError("BAD_TORRENT", str(exc)) from exc
        if metadata.infohash != expected:
            raise TorrentClientError(
                "HASH_MISMATCH",
                f"torrent infohash {metadata.infohash} != {expected}",
            )
        resp = self._post(
            "torrents/add",
            data={
                "category": self._category,
                "savepath": self._savepath_for(subfolder),
                "paused": "false",
            },
            files={
                "torrents": (
                    f"{expected}.torrent", payload, "application/x-bittorrent")
            },
        )
        body = (getattr(resp, "text", "") or "").strip()
        if body != "Ok.":
            for task in self._category_tasks():
                if str(task.get("hash", "")).lower() == expected:
                    return expected
            raise TorrentClientError(
                "ADD_FAILED", f"qbt rejected torrent payload: {body!r}")
        return expected

    def _savepath_for(self, subfolder: str | None) -> str:
        """The pinned save_dir, optionally with a per-anime ``subfolder`` under it.
        Re-validates real-path containment (defence in depth): the joined target
        must stay inside save_dir, so a subfolder can never redirect writes out of
        download_dir even if the caller passed something unsanitized (P0-4)."""
        if not subfolder:
            return self._save_dir
        base = Path(self._save_dir)
        target = (base / subfolder).resolve()
        if not target.is_relative_to(base):
            raise TorrentClientError("UNSAFE_PATH", f"subfolder escapes save_dir: {subfolder!r}")
        return str(target)

    def status(self, task_id: str) -> DownloadStatus:
        for t in self._category_tasks():
            if str(t.get("hash", "")).lower() == task_id.lower():
                return _to_status(t)
        raise TorrentClientError("TASK_NOT_FOUND", task_id)

    def cancel(self, task_id: str) -> None:
        ours = {str(t.get("hash", "")).lower() for t in self._category_tasks()}
        if task_id.lower() not in ours:
            # never delete a task outside our category (P2-20)
            raise TorrentClientError(
                "NOT_IN_CATEGORY", "refusing to cancel a non-spica-anime task")
        self._post("torrents/delete",
                   data={"hashes": task_id.lower(), "deleteFiles": "true"})

    # -- internals -----------------------------------------------------------

    def _category_tasks(self) -> list[dict]:
        resp = self._get("torrents/info", params={"category": self._category})
        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise TorrentClientError("API_ERROR", f"bad info json: {e}")
        return data if isinstance(data, list) else []

    def _login(self) -> None:
        if self._username is None and self._password is None:
            raise TorrentClientError("AUTH_FAILED", "credentials required")
        resp = self._raw("post", "auth/login",
                         data={"username": self._username or "",
                               "password": self._password or ""})
        # qBittorrent 4.x: HTTP 200 body "Ok." on success ("Fails." on bad creds);
        # qBittorrent 5.x: HTTP 204 empty body on success. Accept any 2xx that is
        # not the explicit "Fails." rejection -- keying on the 4.x "Ok." body alone
        # wrongly rejected 5.x's 204 (W4-b §6.3 real machine, qBittorrent 5.2.3).
        status = getattr(resp, "status_code", 200)
        body = (getattr(resp, "text", "") or "").strip()
        if not (200 <= status < 300) or body == "Fails.":
            raise TorrentClientError("AUTH_FAILED", "login rejected")
        self._authed = True

    def _get(self, path: str, **kw: Any) -> Any:
        return self._request("get", path, **kw)

    def _post(self, path: str, **kw: Any) -> Any:
        return self._request("post", path, **kw)

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        resp = self._raw(method, path, **kw)
        if getattr(resp, "status_code", 200) == 403:
            # 403 always means (re)authenticate -- an expired SID must trigger a
            # re-login + single replay, never a hard failure (F4, plan P1-10).
            # At most one re-login per request: a replayed 403 falls through.
            self._authed = False
            self._login()
            resp = self._raw(method, path, **kw)
        code = getattr(resp, "status_code", 200)
        if code != 200:
            raise TorrentClientError("API_ERROR", f"HTTP {code} on {path}")
        return resp

    def _raw(self, method: str, path: str, **kw: Any) -> Any:
        url = f"{self._base}/api/v2/{path}"
        try:
            return getattr(self._http, method)(url, timeout=self._timeout, **kw)
        except Exception as e:  # noqa: BLE001
            raise TorrentClientError("UNREACHABLE", str(e))


def _to_status(t: dict) -> DownloadStatus:
    state = str(t.get("state", ""))
    progress = float(t.get("progress", 0.0) or 0.0)
    if progress >= 1.0 or state in _DONE_STATES:
        mapped = "completed"
    elif "error" in state.lower() or state == "missingFiles":
        mapped = "error"
    elif state == "metaDL":
        mapped = "metadata"
    elif "stalled" in state.lower():
        mapped = "stalled"
    else:
        mapped = "downloading"
    return DownloadStatus(
        task_id=str(t.get("hash", "")).lower(), state=mapped, progress=progress,
        save_path=t.get("content_path") or t.get("save_path"),
        error=state if mapped == "error" else None,
    )
