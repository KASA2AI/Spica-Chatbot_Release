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
- ``status`` operates only within category ``spica-anime``. ``cancel`` first
  proves category+exact-hash ownership, then uses a version-specific bounded
  freeze/re-read protocol before delete; post-delete exact-hash properties
  checks prevent recategorization from masquerading as disappearance.

Qt-free (CLAUDE.md #1). No os.getenv -- url / save_dir / credentials are
constructor args; the default session sets ``trust_env = False``.
"""

from __future__ import annotations

import math
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from spica.anime.models import DownloadStatus
from spica.anime.torrent_metadata import (
    TorrentMetadataError,
    inspect_torrent,
    validate_public_trackers,
)
from spica.ports.torrent_client import (
    TorrentCancelOutcome,
    TorrentCancelResult,
    TorrentClientError,
)

_CATEGORY = "spica-anime"
_BTIH_XT_RE = re.compile(r"^urn:btih:([0-9a-fA-F]{40})$")
_BTIH_HEX_RE = re.compile(r"^[0-9a-fA-F]{40}$")
# qbt states that mean "download finished" (seeding / paused-after-complete).
_DONE_STATES = frozenset({
    "uploading", "pausedUP", "stoppedUP", "stalledUP", "forcedUP", "queuedUP", "checkingUP",
})
_ERROR_STATES = frozenset({"error", "missingFiles"})
_FREEZE_POLL_ATTEMPTS = 5
_DELETE_POLL_ATTEMPTS = 5
_MUTATION_POLL_SECONDS = 0.05


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
                 timeout: float = 10,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._base = base_url.rstrip("/")
        # pinned absolute path handed to the qbt daemon (~ won't expand remotely)
        self._save_dir = str(Path(save_dir).expanduser().resolve())
        self._category = _CATEGORY
        self._username = username
        self._password = password
        self._http = session if session is not None else _default_session(self._base)
        self._timeout = timeout
        self._sleep = sleep
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

    def cancel(self, task_id: str) -> TorrentCancelOutcome:
        """Best-available fail-closed cancellation for qBT 4.1-4.6 and 5.x.

        This is intentionally not described as a transaction/CAS: an external
        client can still resume, recategorize, or remove/re-add the same hash
        between our final read and delete, qBT may finish in flight, and task
        disappearance does not prove synchronous disk unlink completion. Within
        this adapter, however, completion always wins every read before delete.
        """
        task_hash = str(task_id or "").lower()
        if _BTIH_HEX_RE.fullmatch(task_hash) is None:
            raise TorrentClientError("BAD_TASK_ID", "invalid torrent hash")

        initial = self._category_task(task_hash)
        if initial is None:
            return self._owned_task_missing(task_hash)
        if _task_is_complete(initial):
            return _completed_cancel_outcome(initial)

        freeze_endpoint, frozen_dl_state = self._resolve_cancel_protocol()
        self._post(f"torrents/{freeze_endpoint}", data={"hashes": task_hash})

        candidate: dict[str, Any] | None = None
        candidate_kind: str | None = None
        stable_error: tuple[str, float] | None = None
        stable_error_reads = 0
        for attempt in range(_FREEZE_POLL_ATTEMPTS):
            observed = self._category_task(task_hash)
            if observed is None:
                return self._owned_task_missing(task_hash)
            if _task_is_complete(observed):
                return _completed_cancel_outcome(observed)
            state = str(observed.get("state", ""))
            progress = _strict_task_progress(observed)
            if progress is None:
                stable_error = None
                stable_error_reads = 0
            elif state == frozen_dl_state and progress < 1.0:
                candidate = observed
                candidate_kind = "frozen"
                break
            elif state in _ERROR_STATES and progress < 1.0:
                signature = (state, progress)
                if signature == stable_error:
                    stable_error_reads += 1
                else:
                    stable_error = signature
                    stable_error_reads = 1
                if stable_error_reads >= 2:
                    candidate = observed
                    candidate_kind = "stable_error"
                    break
            else:
                stable_error = None
                stable_error_reads = 0
            if attempt + 1 < _FREEZE_POLL_ATTEMPTS:
                self._sleep(_MUTATION_POLL_SECONDS)
        if candidate is None:
            raise TorrentClientError(
                "CANCEL_NOT_FROZEN",
                "qBittorrent did not confirm a stable frozen incomplete state",
            )

        # Latest exact category read immediately before delete. For error states
        # this is also the required stable re-read after freeze succeeded.
        final = self._category_task(task_hash)
        if final is None:
            return self._owned_task_missing(task_hash)
        if _task_is_complete(final):
            return _completed_cancel_outcome(final)
        final_state = str(final.get("state", ""))
        final_progress = _strict_task_progress(final)
        if final_progress is None:
            raise TorrentClientError(
                "CANCEL_NOT_FROZEN",
                "torrent progress was missing or invalid before delete",
            )
        frozen_incomplete = (
            candidate_kind == "frozen"
            and final_state == frozen_dl_state
            and final_progress < 1.0
        )
        stable_incomplete_error = (
            candidate_kind == "stable_error"
            and stable_error is not None
            and (final_state, final_progress) == stable_error
            and final_state in _ERROR_STATES
            and final_progress < 1.0
        )
        if not (frozen_incomplete or stable_incomplete_error):
            raise TorrentClientError(
                "CANCEL_NOT_FROZEN",
                "torrent changed before delete; refusing destructive mutation",
            )

        self._post(
            "torrents/delete",
            data={"hashes": task_hash, "deleteFiles": "true"},
        )
        # A 200/204 only acknowledges the command.  The exact-hash properties
        # endpoint is global (not category-filtered), so recategorizing the task
        # cannot masquerade as deletion.  A documented 404 is the sole task-
        # disappearance proof supported by qBT 4.1-4.6 and 5.x.
        for attempt in range(_DELETE_POLL_ATTEMPTS):
            if not self._exact_task_exists(task_hash):
                return TorrentCancelOutcome(TorrentCancelResult.CANCELLED)
            if attempt + 1 < _DELETE_POLL_ATTEMPTS:
                self._sleep(_MUTATION_POLL_SECONDS)
        raise TorrentClientError(
            "CANCEL_UNCONFIRMED",
            "delete was accepted but the category task still exists",
        )

    # -- internals -----------------------------------------------------------

    def _category_tasks(self) -> list[dict]:
        resp = self._get("torrents/info", params={"category": self._category})
        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise TorrentClientError("API_ERROR", f"bad info json: {e}")
        return data if isinstance(data, list) else []

    def _category_task(self, task_hash: str) -> dict[str, Any] | None:
        # v4.1.0 did not guarantee torrents/info?hashes=. Pull only our category
        # and perform the exact hash match locally for the full supported range.
        return next((
            task for task in self._category_tasks()
            if str(task.get("hash", "")).lower() == task_hash
        ), None)

    def _exact_task_exists(self, task_hash: str) -> bool:
        response = self._request_accepting(
            "get",
            "torrents/properties",
            accepted_statuses=(200, 404),
            params={"hash": task_hash},
        )
        return getattr(response, "status_code", 200) != 404

    def _owned_task_missing(self, task_hash: str) -> TorrentCancelOutcome:
        """Disambiguate external removal from loss of category ownership."""
        if self._exact_task_exists(task_hash):
            raise TorrentClientError(
                "CANCEL_OWNER_LOST",
                "torrent left the spica-anime category; refusing mutation",
            )
        return TorrentCancelOutcome(TorrentCancelResult.MISSING)

    def _resolve_cancel_protocol(self) -> tuple[str, str]:
        response = self._get("app/version")
        version = (getattr(response, "text", "") or "").strip()
        match = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.\d+.*)?", version)
        if match is None:
            raise TorrentClientError(
                "UNSUPPORTED_VERSION", f"unparseable qBittorrent version: {version!r}")
        major, minor = int(match.group(1)), int(match.group(2))
        if major == 4 and 1 <= minor <= 6:
            protocol = ("pause", "pausedDL")
        elif major == 5:
            protocol = ("stop", "stoppedDL")
        else:
            raise TorrentClientError(
                "UNSUPPORTED_VERSION", f"unsupported qBittorrent version: {version}")
        return protocol

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
        return self._request_accepting(
            method, path, accepted_statuses=(200, 204), **kw)

    def _request_accepting(
        self,
        method: str,
        path: str,
        *,
        accepted_statuses: tuple[int, ...],
        **kw: Any,
    ) -> Any:
        resp = self._raw(method, path, **kw)
        if getattr(resp, "status_code", 200) == 403:
            # 403 always means (re)authenticate -- an expired SID must trigger a
            # re-login + single replay, never a hard failure (F4, plan P1-10).
            # At most one re-login per request: a replayed 403 falls through.
            self._authed = False
            self._login()
            resp = self._raw(method, path, **kw)
        code = getattr(resp, "status_code", 200)
        if code not in accepted_statuses:
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
    progress = _task_progress(t)
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
    try:
        last_activity_at = float(t.get("last_activity"))
    except (TypeError, ValueError, OverflowError):
        last_activity_at = None
    if last_activity_at is not None and (
            last_activity_at <= 0 or not math.isfinite(last_activity_at)):
        last_activity_at = None
    return DownloadStatus(
        task_id=str(t.get("hash", "")).lower(), state=mapped, progress=progress,
        save_path=t.get("content_path") or t.get("save_path"),
        error=state if mapped == "error" else None,
        last_activity_at=last_activity_at,
    )


def _task_progress(task: dict[str, Any]) -> float:
    try:
        value = float(task.get("progress", 0.0) or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return value if math.isfinite(value) and value >= 0.0 else 0.0


def _strict_task_progress(task: dict[str, Any]) -> float | None:
    """Parse progress for destructive decisions; malformed data is unknown.

    Display/status mapping may safely render malformed progress as zero, but a
    delete path must never turn missing or invalid evidence into confirmation
    that a task is incomplete.
    """
    if "progress" not in task or isinstance(task["progress"], bool):
        return None
    try:
        value = float(task["progress"])
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0.0 else None


def _task_is_complete(task: dict[str, Any]) -> bool:
    state = str(task.get("state", ""))
    return _task_progress(task) >= 1.0 or state in _DONE_STATES or state.endswith("UP")


def _completed_cancel_outcome(task: dict[str, Any]) -> TorrentCancelOutcome:
    save_path = task.get("content_path") or task.get("save_path")
    return TorrentCancelOutcome(
        TorrentCancelResult.ALREADY_COMPLETED,
        str(save_path) if save_path is not None else None,
    )
