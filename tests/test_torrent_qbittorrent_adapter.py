"""Phase 2: qBittorrent adapter -- fully mocked HTTP, no local qbt."""

from __future__ import annotations

import os

import pytest

from spica.adapters.torrent.qbittorrent import QBittorrentClient, _extract_btih_40hex
from spica.ports.torrent_client import TorrentCancelResult, TorrentClientError

_HEX = "fe2aafd45d8b9e077b22968a8c65b91d4a25cadf"
_MAGNET = f"magnet:?xt=urn:btih:{_HEX}&dn=whatever&tr=udp://x"
_BASE32 = "MFRGGZDFMZTWQ2LKNNWG23TPOBYXE43U"          # 32-char base32 btih
_TORRENT_HASH = "14299d250e3e00abb954b9a6020f5546fce5ba8f"
_TORRENT_PAYLOAD = (
    b"d8:announce32:https://tracker.example/announce"
    b"13:announce-listll32:https://tracker.example/announcee"
    b"l35:udp://tracker.example:6969/announceee"
    b"4:infod6:lengthi4e4:name7:ep1.mkvee"
)
_WEBSEED_PAYLOAD = (
    b"d8:announce32:https://tracker.example/announce"
    b"8:url-list24:http://127.0.0.1/private"
    b"4:infod6:lengthi4e4:name7:ep1.mkvee"
)


class FakeResp:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    def __init__(self, info_tasks=None, require_login=False, login_text="Ok.",
                 login_status=200, app_version="v5.2.3", mutation_status=200):
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict | None]] = []
        self.posts: list[tuple[str, dict | None]] = []
        self.file_uploads: list[dict | None] = []
        self._info = info_tasks or []
        self._require_login = require_login
        self._login_text = login_text
        self._login_status = login_status
        self._app_version = app_version
        self._mutation_status = mutation_status
        self._authed = False

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append((url, params))
        if "app/version" in url:
            return FakeResp(200, text=self._app_version)
        if "torrents/info" in url:
            return FakeResp(200, json_data=self._info)
        return FakeResp(404)

    def post(self, url, data=None, timeout=None, **kw):
        self.calls.append((url, data))
        self.posts.append((url, data))
        self.file_uploads.append(kw.get("files"))
        if "auth/login" in url:
            self._authed = self._login_status < 300 and self._login_text != "Fails."
            return FakeResp(self._login_status, text=self._login_text)
        if self._require_login and not self._authed:
            return FakeResp(403)
        if "torrents/pause" in url:
            for task in self._info:
                if float(task.get("progress", 0.0) or 0.0) < 1.0:
                    task["state"] = "pausedDL"
        elif "torrents/stop" in url:
            for task in self._info:
                if float(task.get("progress", 0.0) or 0.0) < 1.0:
                    task["state"] = "stoppedDL"
        elif "torrents/delete" in url:
            self._info = []
        return FakeResp(self._mutation_status, text="Ok.")

    def add_data(self):
        for url, data in self.posts:
            if "torrents/add" in url:
                return data
        return None

    def deletes(self):
        return [d for url, d in self.posts if "torrents/delete" in url]


def _client(save_dir, session, **kw):
    return QBittorrentClient("http://127.0.0.1:8080", str(save_dir),
                             session=session, **kw)


# -- magnet validation (review #4) -------------------------------------------

def test_add_magnet_accepts_40hex_starts_download(tmp_path):
    sess = FakeSession()
    task = _client(tmp_path, sess).add_magnet(_MAGNET)
    assert task == _HEX                              # lowercase 40-hex task_id
    data = sess.add_data()
    assert data["urls"] == _MAGNET
    assert data["category"] == "spica-anime"
    assert data["paused"] == "false"                # actually starts (review #1)


def test_add_magnet_never_paused_true(tmp_path):
    sess = FakeSession()
    _client(tmp_path, sess).add_magnet(_MAGNET)
    assert sess.add_data()["paused"] != "true"


def test_add_torrent_bytes_uploads_verified_payload_and_starts(tmp_path):
    sess = FakeSession()
    task = _client(tmp_path, sess).add_torrent_bytes(
        _TORRENT_PAYLOAD, expected_infohash=_TORRENT_HASH)

    assert task == _TORRENT_HASH
    data = sess.add_data()
    assert data["category"] == "spica-anime"
    assert data["paused"] == "false"
    assert data["savepath"] == str(tmp_path.resolve())
    upload = next(files for files in sess.file_uploads if files is not None)
    assert upload["torrents"][1] == _TORRENT_PAYLOAD


def test_add_torrent_bytes_rejects_hash_mismatch_before_qbt(tmp_path):
    sess = FakeSession()

    with pytest.raises(TorrentClientError) as exc:
        _client(tmp_path, sess).add_torrent_bytes(
            _TORRENT_PAYLOAD, expected_infohash="0" * 40)

    assert exc.value.code == "HASH_MISMATCH"
    assert sess.add_data() is None


def test_add_torrent_bytes_rejects_malformed_payload_before_qbt(tmp_path):
    sess = FakeSession()

    with pytest.raises(TorrentClientError) as exc:
        _client(tmp_path, sess).add_torrent_bytes(
            b"not bencode", expected_infohash=_TORRENT_HASH)

    assert exc.value.code == "BAD_TORRENT"
    assert sess.add_data() is None


def test_add_torrent_bytes_rejects_webseed_before_qbt(tmp_path):
    sess = FakeSession()

    with pytest.raises(TorrentClientError) as exc:
        _client(tmp_path, sess).add_torrent_bytes(
            _WEBSEED_PAYLOAD, expected_infohash=_TORRENT_HASH)

    assert exc.value.code == "BAD_TORRENT"
    assert sess.add_data() is None


@pytest.mark.parametrize("bad", [
    "https://mikanani.me/Download/x/abc.torrent",   # http torrent URL
    "http://evil/x.torrent",
    "/tmp/local.torrent",                            # file path
    "file:///tmp/x.torrent",
    "not a magnet",
    f"magnet:?xt=urn:btih:{_BASE32}",                # base32 -> rejected (v1.1)
    "magnet:?xt=urn:btih:tooshort",
    "magnet:?dn=only-name",                          # no btih
])
def test_add_magnet_rejects_non_40hex(tmp_path, bad):
    sess = FakeSession()
    with pytest.raises(TorrentClientError) as ei:
        _client(tmp_path, sess).add_magnet(bad)
    assert ei.value.code == "BAD_MAGNET"
    assert sess.add_data() is None                   # nothing sent to qbt


def test_add_magnet_fails_body_raises(tmp_path):
    # F7: qbt answers 200 + "Fails." when the add is rejected -> must raise,
    # never return a fake task_id the poller would wait on forever.
    class FailsSession(FakeSession):
        def post(self, url, data=None, timeout=None, **kw):
            self.calls.append((url, data))
            self.posts.append((url, data))
            if "auth/login" in url:
                return FakeResp(200, text="Ok.")
            return FakeResp(200, text="Fails.")

    with pytest.raises(TorrentClientError) as ei:
        _client(tmp_path, FailsSession()).add_magnet(_MAGNET)
    assert ei.value.code == "ADD_FAILED"


class _FailsAddSession(FakeSession):
    """qbt rejects every add with 200 + 'Fails.' (a duplicate infohash)."""

    def post(self, url, data=None, timeout=None, **kw):
        self.calls.append((url, data))
        self.posts.append((url, data))
        if "auth/login" in url:
            return FakeResp(200, text="Ok.")
        return FakeResp(200, text="Fails.")


def test_add_magnet_duplicate_reuses_task_in_category(tmp_path):
    # P2-3: "Fails." on a duplicate add is idempotent when THIS btih already
    # lives in our category -> reuse the running task, never a dead-end.
    sess = _FailsAddSession(info_tasks=[
        {"hash": _HEX, "state": "downloading", "progress": 0.3}])
    task = _client(tmp_path, sess).add_magnet(_MAGNET)
    assert task == _HEX                              # reused, not ADD_FAILED
    # it verified reuse via the category-filtered info endpoint
    info_call = next(p for u, p in sess.calls if "torrents/info" in u)
    assert info_call == {"category": "spica-anime"}


def test_add_magnet_duplicate_not_in_category_raises(tmp_path):
    # "Fails." with the btih NOT among our category tasks is a real rejection
    sess = _FailsAddSession(info_tasks=[
        {"hash": "0" * 40, "state": "downloading", "progress": 0.1}])
    with pytest.raises(TorrentClientError) as ei:
        _client(tmp_path, sess).add_magnet(_MAGNET)
    assert ei.value.code == "ADD_FAILED"


def test_add_torrent_bytes_duplicate_reuses_task_in_category(tmp_path):
    sess = _FailsAddSession(info_tasks=[
        {"hash": _TORRENT_HASH, "state": "metaDL", "progress": 0.0}])

    task = _client(tmp_path, sess).add_torrent_bytes(
        _TORRENT_PAYLOAD, expected_infohash=_TORRENT_HASH)

    assert task == _TORRENT_HASH
    info_call = next(p for u, p in sess.calls if "torrents/info" in u)
    assert info_call == {"category": "spica-anime"}


def test_add_torrent_bytes_duplicate_outside_category_raises(tmp_path):
    sess = _FailsAddSession(info_tasks=[
        {"hash": "0" * 40, "state": "downloading", "progress": 0.1}])

    with pytest.raises(TorrentClientError) as exc:
        _client(tmp_path, sess).add_torrent_bytes(
            _TORRENT_PAYLOAD, expected_infohash=_TORRENT_HASH)

    assert exc.value.code == "ADD_FAILED"


def test_extract_btih_helper():
    assert _extract_btih_40hex(_MAGNET) == _HEX
    assert _extract_btih_40hex(f"magnet:?xt=urn:btih:{_BASE32}") is None
    assert _extract_btih_40hex("https://x/y.torrent") is None


# -- save_dir pinned / absolute ----------------------------------------------

def test_save_dir_pinned_absolute(tmp_path):
    sess = FakeSession()
    _client(tmp_path / "sub", sess).add_magnet(_MAGNET)   # save_dir not in the API
    savepath = sess.add_data()["savepath"]
    assert savepath == str((tmp_path / "sub").resolve())
    assert os.path.isabs(savepath)                        # absolute (Windows-safe: not startswith "/")


# -- category scope (P2-20) --------------------------------------------------

def test_status_reads_only_our_category(tmp_path):
    sess = FakeSession(info_tasks=[
        {"hash": _HEX.upper(), "state": "downloading", "progress": 0.5}])
    st = _client(tmp_path, sess).status(_HEX)
    assert st.state == "downloading"
    assert st.progress == 0.5
    # info was queried with our category filter
    info_call = next(p for u, p in sess.calls if "torrents/info" in u)
    assert info_call == {"category": "spica-anime"}


def test_status_preserves_metadata_fetching_state(tmp_path):
    sess = FakeSession(info_tasks=[
        {"hash": _HEX, "state": "metaDL", "progress": 0.0}])

    status = _client(tmp_path, sess).status(_HEX)

    assert status.state == "metadata"
    assert status.progress == 0.0


def test_status_exposes_qbt_last_activity_unix_epoch_seconds(tmp_path):
    sess = FakeSession(info_tasks=[
        {"hash": _HEX, "state": "stalledDL", "progress": 0.25,
         "last_activity": 1_721_234_567}])

    status = _client(tmp_path, sess).status(_HEX)

    assert status.last_activity_at == 1_721_234_567.0


@pytest.mark.parametrize(
    "last_activity",
    [
        0,
        -1,
        "not-an-epoch",
        None,
        float("nan"),
        float("inf"),
        10 ** 400,
    ],
)
def test_status_ignores_invalid_qbt_last_activity(tmp_path, last_activity):
    sess = FakeSession(info_tasks=[
        {"hash": _HEX, "state": "stalledDL", "progress": 0.25,
         "last_activity": last_activity}])

    status = _client(tmp_path, sess).status(_HEX)

    assert status.last_activity_at is None


def test_status_completed_mapping(tmp_path):
    sess = FakeSession(info_tasks=[
        {"hash": _HEX, "state": "pausedUP", "progress": 1.0,
         "content_path": "/dl/ep.mkv"}])
    st = _client(tmp_path, sess).status(_HEX)
    assert st.state == "completed"
    assert st.save_path == "/dl/ep.mkv"


def test_cancel_only_deletes_in_category(tmp_path):
    sess = FakeSession(info_tasks=[{"hash": _HEX, "state": "downloading",
                                    "progress": 0.1}])
    outcome = _client(tmp_path, sess).cancel(_HEX)
    assert outcome.result is TorrentCancelResult.CANCELLED
    assert sess.deletes() == [{"hashes": _HEX, "deleteFiles": "true"}]


def test_cancel_refuses_task_outside_category(tmp_path):
    # a user's manual torrent is NOT in our category -> refuse, never delete
    sess = FakeSession(info_tasks=[{"hash": _HEX, "state": "downloading",
                                    "progress": 0.1}])
    other = "0" * 40
    outcome = _client(tmp_path, sess).cancel(other)
    assert outcome.result is TorrentCancelResult.MISSING
    assert sess.deletes() == []                    # nothing deleted


def test_cancel_freeze_recheck_completed_never_deletes_data(tmp_path):
    class CompletesWhileStoppingSession(FakeSession):
        def __init__(self):
            super().__init__()
            self.snapshots = [
                [{"hash": _HEX, "state": "downloading", "progress": 0.99}],
                [{
                    "hash": _HEX,
                    "state": "stoppedUP",
                    "progress": 1.0,
                    "content_path": "/dl/ep.mkv",
                }],
            ]

        def get(self, url, params=None, timeout=None, **kw):
            self.calls.append((url, params))
            if "app/version" in url:
                return FakeResp(200, text="v5.2.3")
            if "torrents/info" in url:
                snapshot = self.snapshots.pop(0)
                return FakeResp(200, json_data=snapshot)
            return FakeResp(404)

    session = CompletesWhileStoppingSession()

    outcome = _client(tmp_path, session).cancel(_HEX)

    assert outcome.result.value == "already_completed"
    assert outcome.save_path == "/dl/ep.mkv"
    assert any("torrents/stop" in url for url, _ in session.posts)
    assert session.deletes() == []


def test_cancel_final_pre_delete_read_completed_never_deletes_data(tmp_path):
    down = [{"hash": _HEX, "state": "downloading", "progress": 0.99}]
    frozen = [{"hash": _HEX, "state": "stoppedDL", "progress": 0.99}]
    completed = [{
        "hash": _HEX,
        "state": "stoppedUP",
        "progress": 1.0,
        "content_path": "/dl/ep.mkv",
    }]
    session = _ScriptedCancelSession([down, frozen, completed])

    outcome = _client(
        tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert outcome.result is TorrentCancelResult.ALREADY_COMPLETED
    assert outcome.save_path == "/dl/ep.mkv"
    assert session.deletes() == []


class _ScriptedCancelSession(FakeSession):
    """Exact qBT snapshots for freeze/re-read/delete confirmation tests."""

    def __init__(
        self,
        snapshots,
        *,
        app_version="v5.2.3",
        mutation_status=200,
        property_statuses=None,
    ):
        super().__init__(
            app_version=app_version, mutation_status=mutation_status)
        self.snapshots = list(snapshots)
        self.property_statuses = list(property_statuses or [404])

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append((url, params))
        if "app/version" in url:
            return FakeResp(200, text=self._app_version)
        if "torrents/properties" in url:
            status = (
                self.property_statuses.pop(0)
                if len(self.property_statuses) > 1
                else self.property_statuses[0]
            )
            return FakeResp(status, json_data={} if status == 200 else None)
        if "torrents/info" in url:
            snapshot = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
            return FakeResp(200, json_data=snapshot)
        return FakeResp(404)

    def post(self, url, data=None, timeout=None, **kw):
        self.calls.append((url, data))
        self.posts.append((url, data))
        self.file_uploads.append(kw.get("files"))
        return FakeResp(self._mutation_status, text="")


@pytest.mark.parametrize(
    ("version", "freeze_endpoint", "frozen_state"),
    [
        ("v4.1.0", "pause", "pausedDL"),
        ("v4.6.7", "pause", "pausedDL"),
        ("v5.0.0", "stop", "stoppedDL"),
        ("v5.2.3", "stop", "stoppedDL"),
    ],
)
def test_cancel_dispatches_version_specific_freeze_only(
        tmp_path, version, freeze_endpoint, frozen_state):
    frozen = [{"hash": _HEX, "state": frozen_state, "progress": 0.4}]
    session = _ScriptedCancelSession([
        [{"hash": _HEX, "state": "downloading", "progress": 0.4}],
        frozen,
        frozen,
        [],
    ], app_version=version)

    outcome = _client(tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert outcome.result is TorrentCancelResult.CANCELLED
    mutation_paths = [url for url, _ in session.posts]
    assert any(f"torrents/{freeze_endpoint}" in url for url in mutation_paths)
    forbidden = "stop" if freeze_endpoint == "pause" else "pause"
    assert not any(f"torrents/{forbidden}" in url for url in mutation_paths)


@pytest.mark.parametrize("version", ["", "not-a-version", "v4.0.5", "v4.7.0", "v6.0.0"])
def test_cancel_unknown_version_is_zero_mutation(tmp_path, version):
    session = _ScriptedCancelSession([
        [{"hash": _HEX, "state": "downloading", "progress": 0.4}],
    ], app_version=version)

    with pytest.raises(TorrentClientError) as caught:
        _client(tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert caught.value.code == "UNSUPPORTED_VERSION"
    assert session.posts == []


def test_cancel_polls_until_v5_reports_stopped_dl(tmp_path):
    down = [{"hash": _HEX, "state": "downloading", "progress": 0.4}]
    checking = [{"hash": _HEX, "state": "checkingDL", "progress": 0.4}]
    frozen = [{"hash": _HEX, "state": "stoppedDL", "progress": 0.4}]
    session = _ScriptedCancelSession([down, down, checking, frozen, frozen, []])

    outcome = _client(tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert outcome.result is TorrentCancelResult.CANCELLED
    info_calls = [params for url, params in session.calls if "torrents/info" in url]
    assert len(info_calls) == 5
    # v4.1 compatibility: never assume the optional hashes filter exists.
    assert info_calls == [{"category": "spica-anime"}] * 5
    properties_call = next(
        params for url, params in session.calls
        if "torrents/properties" in url)
    assert properties_call == {"hash": _HEX}


def test_cancel_v4_1_reads_category_then_matches_hash_locally(tmp_path):
    down = [{"hash": _HEX, "state": "downloading", "progress": 0.4}]
    frozen = [{"hash": _HEX, "state": "pausedDL", "progress": 0.4}]
    session = _ScriptedCancelSession(
        [down, frozen, frozen], app_version="v4.1.0")

    outcome = _client(
        tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert outcome.result is TorrentCancelResult.CANCELLED
    info_calls = [
        params for url, params in session.calls if "torrents/info" in url]
    assert info_calls
    assert all(params == {"category": "spica-anime"} for params in info_calls)


def test_cancel_mutations_target_only_exact_owned_hash(tmp_path):
    other_hash = "0" * 40
    down = [
        {"hash": _HEX, "state": "downloading", "progress": 0.4},
        {"hash": other_hash, "state": "downloading", "progress": 0.2},
    ]
    frozen = [
        {"hash": _HEX, "state": "stoppedDL", "progress": 0.4},
        {"hash": other_hash, "state": "downloading", "progress": 0.2},
    ]
    session = _ScriptedCancelSession([down, frozen, frozen])

    outcome = _client(
        tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert outcome.result is TorrentCancelResult.CANCELLED
    freeze_data = next(
        data for url, data in session.posts if "torrents/stop" in url)
    delete_data = next(
        data for url, data in session.posts if "torrents/delete" in url)
    assert freeze_data == {"hashes": _HEX}
    assert delete_data["hashes"] == _HEX
    assert other_hash not in (freeze_data["hashes"], delete_data["hashes"])


@pytest.mark.parametrize(
    "state",
    ["uploading", "pausedUP", "stoppedUP", "queuedUP", "checkingUP", "forcedUP"],
)
def test_cancel_all_up_family_states_preserve_completed_files(tmp_path, state):
    session = _ScriptedCancelSession([[{
        "hash": _HEX, "state": state, "progress": 0.8,
        "content_path": "/dl/ep.mkv",
    }]])

    outcome = _client(tmp_path, session).cancel(_HEX)

    assert outcome.result is TorrentCancelResult.ALREADY_COMPLETED
    assert outcome.save_path == "/dl/ep.mkv"
    assert session.posts == []


def test_cancel_deletes_stable_incomplete_error_only_after_freeze(tmp_path):
    error = [{"hash": _HEX, "state": "error", "progress": 0.4}]
    session = _ScriptedCancelSession([error, error, error, error, []])

    outcome = _client(tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert outcome.result is TorrentCancelResult.CANCELLED
    assert len(session.deletes()) == 1


@pytest.mark.parametrize("state", ["checkingDL", "moving", "unknown"])
def test_cancel_ambiguous_post_freeze_states_fail_closed(tmp_path, state):
    initial = [{"hash": _HEX, "state": "downloading", "progress": 0.4}]
    ambiguous = [{"hash": _HEX, "state": state, "progress": 0.4}]
    session = _ScriptedCancelSession([initial] + [ambiguous] * 6)

    with pytest.raises(TorrentClientError) as caught:
        _client(tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert caught.value.code == "CANCEL_NOT_FROZEN"
    assert session.deletes() == []


def test_cancel_unstable_error_fails_closed_without_delete(tmp_path):
    snapshots = [[{
        "hash": _HEX,
        "state": "error",
        "progress": progress,
    }] for progress in (0.1, 0.2, 0.1, 0.2, 0.1, 0.2)]
    session = _ScriptedCancelSession(snapshots)

    with pytest.raises(TorrentClientError) as caught:
        _client(tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert caught.value.code == "CANCEL_NOT_FROZEN"
    assert session.deletes() == []


def test_cancel_nonconsecutive_error_signature_never_authorizes_delete(tmp_path):
    down = [{"hash": _HEX, "state": "downloading", "progress": 0.4}]
    error = [{"hash": _HEX, "state": "error", "progress": 0.4}]
    frozen = [{"hash": _HEX, "state": "stoppedDL", "progress": 0.4}]
    session = _ScriptedCancelSession([down, error, frozen, error])

    with pytest.raises(TorrentClientError) as caught:
        _client(tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert caught.value.code == "CANCEL_NOT_FROZEN"
    assert session.deletes() == []


@pytest.mark.parametrize(
    "raw_progress",
    ["__missing__", None, "garbage", float("nan"), float("inf"), -1, 10 ** 400],
    ids=["missing", "none", "text", "nan", "inf", "negative", "overflow"],
)
def test_cancel_invalid_progress_never_authorizes_delete(
        tmp_path, raw_progress):
    task = {"hash": _HEX, "state": "stoppedDL"}
    if raw_progress != "__missing__":
        task["progress"] = raw_progress
    snapshot = [task]
    session = _ScriptedCancelSession([snapshot] * 7)

    with pytest.raises(TorrentClientError) as caught:
        _client(tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert caught.value.code == "CANCEL_NOT_FROZEN"
    assert session.deletes() == []


def test_cancel_recategorized_task_is_not_confirmed_disappeared(tmp_path):
    down = [{"hash": _HEX, "state": "downloading", "progress": 0.4}]
    frozen = [{"hash": _HEX, "state": "stoppedDL", "progress": 0.4}]
    session = _ScriptedCancelSession(
        [down, frozen, frozen, []],
        property_statuses=[200] * 5,
    )

    with pytest.raises(TorrentClientError) as caught:
        _client(tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert caught.value.code == "CANCEL_UNCONFIRMED"
    assert len(session.deletes()) == 1


@pytest.mark.parametrize(
    "snapshots",
    [
        [
            [{"hash": _HEX, "state": "downloading", "progress": 0.4}],
            [],
        ],
        [
            [{"hash": _HEX, "state": "downloading", "progress": 0.4}],
            [{"hash": _HEX, "state": "stoppedDL", "progress": 0.4}],
            [],
        ],
    ],
    ids=["during-freeze", "before-delete"],
)
def test_cancel_owner_lost_from_category_fails_closed_when_hash_still_exists(
        tmp_path, snapshots):
    session = _ScriptedCancelSession(
        snapshots,
        property_statuses=[200],
    )

    with pytest.raises(TorrentClientError) as caught:
        _client(tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert caught.value.code == "CANCEL_OWNER_LOST"
    assert session.deletes() == []


def test_cancel_initial_category_miss_checks_global_exact_hash(tmp_path):
    session = _ScriptedCancelSession(
        [[]],
        property_statuses=[200],
    )

    with pytest.raises(TorrentClientError) as caught:
        _client(tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert caught.value.code == "CANCEL_OWNER_LOST"
    assert session.posts == []
    properties_call = next(
        params for url, params in session.calls
        if "torrents/properties" in url)
    assert properties_call == {"hash": _HEX}


def test_cancel_initial_category_and_global_miss_returns_missing(tmp_path):
    session = _ScriptedCancelSession(
        [[]],
        property_statuses=[404],
    )

    outcome = _client(
        tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert outcome.result is TorrentCancelResult.MISSING
    assert session.posts == []
    assert any("torrents/properties" in url for url, _ in session.calls)


def test_cancel_owned_task_truly_removed_during_freeze_returns_missing(tmp_path):
    down = [{"hash": _HEX, "state": "downloading", "progress": 0.4}]
    session = _ScriptedCancelSession(
        [down, []],
        property_statuses=[404],
    )

    outcome = _client(
        tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert outcome.result is TorrentCancelResult.MISSING
    assert session.deletes() == []


@pytest.mark.parametrize("mutation_status", [200, 204])
def test_cancel_requires_post_delete_disappearance_not_http_ack(
        tmp_path, mutation_status):
    down = [{"hash": _HEX, "state": "downloading", "progress": 0.4}]
    frozen = [{"hash": _HEX, "state": "stoppedDL", "progress": 0.4}]
    session = _ScriptedCancelSession(
        [down] + [frozen] * 8,
        mutation_status=mutation_status,
        property_statuses=[200] * 5,
    )

    with pytest.raises(TorrentClientError) as caught:
        _client(tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert caught.value.code == "CANCEL_UNCONFIRMED"
    assert len(session.deletes()) == 1


def test_cancel_accepts_204_only_after_eventual_exact_hash_disappearance(
        tmp_path):
    down = [{"hash": _HEX, "state": "downloading", "progress": 0.4}]
    frozen = [{"hash": _HEX, "state": "stoppedDL", "progress": 0.4}]
    session = _ScriptedCancelSession(
        [down, frozen, frozen],
        mutation_status=204,
        property_statuses=[200, 200, 404],
    )

    outcome = _client(
        tmp_path, session, sleep=lambda _seconds: None).cancel(_HEX)

    assert outcome.result is TorrentCancelResult.CANCELLED
    property_calls = [
        params for url, params in session.calls
        if "torrents/properties" in url]
    assert property_calls == [{"hash": _HEX}] * 3


# -- auth --------------------------------------------------------------------

def test_relogin_on_sid_expiry_replays_request(tmp_path):
    # F4: after a successful login, a later 403 (SID expired) must trigger ONE
    # re-login + replay instead of API_ERROR (plan P1-10: reconnect, not fail).
    class ExpiringSession:
        def __init__(self):
            self.authed = False
            self.logins = 0

        def get(self, url, params=None, timeout=None, **kw):
            if not self.authed:
                return FakeResp(403)
            if "torrents/info" in url:
                return FakeResp(200, json_data=[
                    {"hash": _HEX, "state": "downloading", "progress": 0.5}])
            return FakeResp(404)

        def post(self, url, data=None, timeout=None, **kw):
            if "auth/login" in url:
                self.logins += 1
                self.authed = True
                return FakeResp(200, text="Ok.")
            if not self.authed:
                return FakeResp(403)
            return FakeResp(200, text="Ok.")

    sess = ExpiringSession()
    client = _client(tmp_path, sess, username="admin", password="pw")
    assert client.status(_HEX).state == "downloading"   # first: lazy login
    assert sess.logins == 1
    sess.authed = False                                 # SID expires server-side
    st = client.status(_HEX)                            # must re-login + replay
    assert st.state == "downloading"
    assert sess.logins == 2


def test_lazy_login_on_403(tmp_path):
    sess = FakeSession(require_login=True)
    _client(tmp_path, sess, username="admin", password="pw").add_magnet(_MAGNET)
    assert any("auth/login" in u for u, _ in sess.posts)
    assert sess.add_data()["urls"] == _MAGNET      # retried after login


def test_status_task_not_found(tmp_path):
    sess = FakeSession(info_tasks=[])
    with pytest.raises(TorrentClientError) as ei:
        _client(tmp_path, sess).status(_HEX)
    assert ei.value.code == "TASK_NOT_FOUND"


def test_add_magnet_subfolder_nests_savepath_within_save_dir(tmp_path):
    from pathlib import Path
    sess = FakeSession()
    _client(tmp_path, sess).add_magnet(_MAGNET, subfolder="无职转生 第三季")
    savepath = sess.add_data()["savepath"]
    assert savepath.endswith("无职转生 第三季")
    assert Path(savepath).is_relative_to(Path(str(tmp_path)).resolve())   # still inside save_dir


def test_add_magnet_without_subfolder_uses_base_save_dir(tmp_path):
    from pathlib import Path
    sess = FakeSession()
    _client(tmp_path, sess).add_magnet(_MAGNET)
    assert Path(sess.add_data()["savepath"]) == Path(str(tmp_path)).resolve()


def test_add_magnet_rejects_traversal_subfolder(tmp_path):
    sess = FakeSession()
    with pytest.raises(TorrentClientError) as ei:
        _client(tmp_path, sess).add_magnet(_MAGNET, subfolder="../../etc")
    assert ei.value.code == "UNSAFE_PATH"


def test_login_accepts_qbittorrent_5x_204(tmp_path):
    # qBittorrent 5.x returns HTTP 204 (empty body) on a successful login; the
    # 4.x-only `text == "Ok."` check wrongly rejected it (W4-b §6.3 real machine,
    # qBittorrent 5.2.3). Login must succeed -> the magnet add goes through.
    sess = FakeSession(require_login=True, login_status=204, login_text="")
    _client(tmp_path, sess, username="admin", password="pw").add_magnet(_MAGNET)
    assert any("auth/login" in u for u, _ in sess.posts)
    assert sess.add_data()["urls"] == _MAGNET


def test_login_rejects_bad_credentials_fails_body(tmp_path):
    # qBittorrent returns 200 "Fails." on wrong credentials -- must still be AUTH_FAILED.
    sess = FakeSession(require_login=True, login_status=200, login_text="Fails.")
    with pytest.raises(TorrentClientError) as ei:
        _client(tmp_path, sess, username="admin", password="bad").add_magnet(_MAGNET)
    assert ei.value.code == "AUTH_FAILED"
