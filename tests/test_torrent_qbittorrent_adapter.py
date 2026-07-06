"""Phase 2: qBittorrent adapter -- fully mocked HTTP, no local qbt."""

from __future__ import annotations

import pytest

from spica.adapters.torrent.qbittorrent import QBittorrentClient, _extract_btih_40hex
from spica.ports.torrent_client import TorrentClientError

_HEX = "fe2aafd45d8b9e077b22968a8c65b91d4a25cadf"
_MAGNET = f"magnet:?xt=urn:btih:{_HEX}&dn=whatever&tr=udp://x"
_BASE32 = "MFRGGZDFMZTWQ2LKNNWG23TPOBYXE43U"          # 32-char base32 btih


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
    def __init__(self, info_tasks=None, require_login=False, login_text="Ok."):
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict | None]] = []
        self.posts: list[tuple[str, dict | None]] = []
        self._info = info_tasks or []
        self._require_login = require_login
        self._login_text = login_text
        self._authed = False

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append((url, params))
        if "torrents/info" in url:
            return FakeResp(200, json_data=self._info)
        return FakeResp(404)

    def post(self, url, data=None, timeout=None, **kw):
        self.calls.append((url, data))
        self.posts.append((url, data))
        if "auth/login" in url:
            self._authed = True
            return FakeResp(200, text=self._login_text)
        if self._require_login and not self._authed:
            return FakeResp(403)
        return FakeResp(200, text="Ok.")

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
    assert savepath.startswith("/")                       # absolute


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
    _client(tmp_path, sess).cancel(_HEX)
    assert sess.deletes() == [{"hashes": _HEX, "deleteFiles": "true"}]


def test_cancel_refuses_task_outside_category(tmp_path):
    # a user's manual torrent is NOT in our category -> refuse, never delete
    sess = FakeSession(info_tasks=[{"hash": _HEX, "state": "downloading",
                                    "progress": 0.1}])
    other = "0" * 40
    with pytest.raises(TorrentClientError) as ei:
        _client(tmp_path, sess).cancel(other)
    assert ei.value.code == "NOT_IN_CATEGORY"
    assert sess.deletes() == []                    # nothing deleted


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
