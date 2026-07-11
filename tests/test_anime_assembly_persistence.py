"""Phase 4: anime assembly persistence -- library/pending JSON, host closures.

Covers: repo-root path anchoring, episode-key reverse parse (incl. failure),
corrupt-file quarantine, atomic write, the pending sidecar lifecycle
(emit -> note_task_id -> register erases), pre-registration validation
(containment + media extension + folder-result handling), mark_played
persistence, restart load, and the F8 busy seam wired through install().
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_tools.function_tools.screen.schema import ScreenToolError
from spica.anime import watch_flow
from spica.anime.coordinator import CoordinatorResult, MATCHED
from spica.anime.library import AnimeLibrary, LibraryEntry
from spica.anime.models import AnimeResource
from spica.config.schema import AnimeConfig
from spica.core.anime_events import AnimeCancelRequestEvent
from spica.host.assemblies import anime as anime_assembly
from spica.host.assemblies.anime import (
    _REPO_ROOT,
    load_library,
    load_pending,
    parse_episode_key,
    resolve_data_path,
    resolve_media_file,
)
from spica.plugins.registry import CapabilityRegistry


# -- episode-key reverse parse -------------------------------------------------

def test_parse_episode_key_roundtrip():
    assert parse_episode_key("无职转生|s3|e1") == ("无职转生", 3, 1)


def test_parse_episode_key_title_containing_marker():
    # greedy title + end anchor: the RIGHTMOST |sN|eM wins
    assert parse_episode_key("怪番|s2|e3|s1|e5") == ("怪番|s2|e3", 1, 5)


@pytest.mark.parametrize("bad", ["", "无职转生", "无职转生|s3", "无职转生|sX|e1",
                                 "无职转生|s3|e", "s3|e1"])
def test_parse_episode_key_failure_returns_none(bad):
    assert parse_episode_key(bad) is None


# -- data-path anchoring -------------------------------------------------------

def test_resolve_data_path_relative_anchors_at_repo_root():
    p = resolve_data_path("data/anime/library.json")
    assert p.is_absolute()
    assert p == _REPO_ROOT / "data" / "anime" / "library.json"


def test_containment_root_must_be_resolved_for_dotdot(tmp_path):
    # The assembly hands resolve_media_file a `.resolve()`d download_root. Without
    # that, a `..`-containing (or symlinked) root makes is_relative_to compare
    # unnormalized parts, so a file GENUINELY inside is mis-flagged "outside".
    d = tmp_path / "generated_anime"
    d.mkdir()
    f = d / "ep.mkv"
    f.write_bytes(b"x")
    dotdot_root = tmp_path / "x" / ".." / "generated_anime"   # same dir, unnormalized
    with pytest.raises(ValueError):                          # unresolved root -> false "outside"
        resolve_media_file(f, dotdot_root)
    assert resolve_media_file(f, dotdot_root.resolve()) == f.resolve()   # resolved -> contained


def test_resolve_data_path_absolute_and_home(tmp_path):
    assert resolve_data_path(str(tmp_path / "x.json")) == tmp_path / "x.json"
    assert resolve_data_path("~/x.json") == Path("~/x.json").expanduser()


# -- load: missing / valid / corrupt -------------------------------------------

def _entry(key="无职转生|s3|e1", played=False, path="/dl/ep1.mkv"):
    return LibraryEntry(episode_key=key, title="无职转生", season=3, episode=1,
                        file_path=path, size_bytes=7, source="mikan",
                        added_at="2026-07-07T00:00:00+00:00", played=played)


def test_load_library_missing_returns_empty(tmp_path):
    lib = load_library(tmp_path / "nope.json")
    assert lib.to_json() == []


def test_load_library_valid_roundtrip(tmp_path):
    p = tmp_path / "library.json"
    p.write_text(json.dumps([_entry().__dict__]), encoding="utf-8")
    lib = load_library(p)
    assert lib.find("无职转生|s3|e1") is not None


@pytest.mark.parametrize("payload", ["{not json", '{"a": 1}', '[{"bad": "entry"}]'])
def test_load_library_corrupt_quarantines_and_starts_empty(tmp_path, payload):
    p = tmp_path / "library.json"
    p.write_text(payload, encoding="utf-8")
    lib = load_library(p)
    assert lib.to_json() == []
    assert not p.exists()                       # moved aside, never clobbered
    quarantined = list(tmp_path.glob("library.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == payload


def test_load_pending_corrupt_quarantines(tmp_path):
    p = tmp_path / "pending.json"
    p.write_text("[1, 2, {broken", encoding="utf-8")
    assert load_pending(p) == []
    assert list(tmp_path.glob("pending.json.corrupt-*"))


# -- install wiring fixture ----------------------------------------------------

class FakePlayer:
    def __init__(self):
        self.played: list[str] = []

    def play_file(self, p):
        self.played.append(p)


def _host(tmp_path, *, enabled=True, mikan_base_urls=None, bilibili_spaces=None):
    dl = tmp_path / "dl"
    dl.mkdir(exist_ok=True)
    extra = {}
    if mikan_base_urls is not None:
        extra["mikan_base_urls"] = mikan_base_urls
    if bilibili_spaces is not None:
        extra["bilibili_spaces"] = bilibili_spaces
    h = SimpleNamespace()
    h.config = SimpleNamespace(anime=AnimeConfig(
        enabled=enabled,
        download_dir=str(dl),
        library_file=str(tmp_path / "store" / "library.json"),
        cookies_file=str(tmp_path / "cookies.txt"),
        **extra,
    ))
    h.secrets = SimpleNamespace(bilibili_cookie=None, qbittorrent_password=None)
    h.registry = CapabilityRegistry()
    h._anime_sink = lambda ev: h.sunk.append(ev)
    h.sunk = []
    return h


def _install(h, **kw):
    anime_assembly.install(h, sources=[], torrent=object(), player=FakePlayer(),
                           **kw)


def _tool_run(h):
    handler = h.registry.tool_handler("watch_anime")
    assert handler is not None
    return handler


def _media_file(h, name="ep1.mkv", size=64):
    f = Path(h.config.anime.download_dir) / name
    f.write_bytes(b"x" * size)
    return f


def _resource(key="无职转生|s3|e1"):
    return AnimeResource(episode_key=key, source="mikan",
                         locator="magnet:?xt=urn:btih:" + "a" * 40,
                         display_title="无职转生 第三季", size_bytes=700)


def _request_download(h, monkeypatch, key="无职转生|s3|e1"):
    """Drive the real closure through resolve->emit; returns the request_id."""
    monkeypatch.setattr(watch_flow, "resolve_episode",
                        lambda *a, **k: CoordinatorResult(MATCHED,
                                                          resource=_resource(key)))
    out = _tool_run(h)(query="无职转生第三季第一集", episode=None,
                       use_recent_unplayed=None)
    assert out["status"] == "downloading"
    return out["request_id"]


# -- pending sidecar lifecycle ---------------------------------------------------

def test_emit_records_pending_with_explicit_fields(tmp_path, monkeypatch):
    h = _host(tmp_path)
    _install(h)
    rid = _request_download(h, monkeypatch)
    assert len(h.sunk) == 1                       # event still reached the sink
    pending_file = tmp_path / "store" / "pending.json"
    [rec] = json.loads(pending_file.read_text(encoding="utf-8"))
    assert rec["request_id"] == rid
    assert rec["episode_key"] == "无职转生|s3|e1"
    assert (rec["title"], rec["season"], rec["episode"]) == ("无职转生 第三季", 3, 1)
    assert rec["task_id"] is None


def test_note_task_id_persists(tmp_path, monkeypatch):
    h = _host(tmp_path)
    _install(h)
    rid = _request_download(h, monkeypatch)
    h.anime_note_task_id(rid, "a" * 40)
    [rec] = json.loads((tmp_path / "store" / "pending.json").read_text("utf-8"))
    assert rec["task_id"] == "a" * 40
    assert h.anime_list_pending()[0]["task_id"] == "a" * 40


def test_register_erases_pending_and_persists_library(tmp_path, monkeypatch):
    h = _host(tmp_path)
    _install(h)
    rid = _request_download(h, monkeypatch)
    f = _media_file(h)
    entry = h.anime_register_download(rid, "无职转生|s3|e1", str(f))
    assert entry.title == "无职转生 第三季"       # explicit pending fields, not key parse
    assert (entry.season, entry.episode) == (3, 1)
    assert entry.size_bytes == 64
    assert json.loads((tmp_path / "store" / "pending.json").read_text("utf-8")) == []
    [saved] = json.loads((tmp_path / "store" / "library.json").read_text("utf-8"))
    assert saved["episode_key"] == "无职转生|s3|e1"
    assert saved["file_path"] == str(f)
    # atomic write leaves no tmp file behind
    assert not list((tmp_path / "store").glob("*.tmp"))


def test_register_without_pending_falls_back_to_key_parse(tmp_path):
    h = _host(tmp_path)
    _install(h)
    f = _media_file(h)
    entry = h.anime_register_download("ghost", "无职转生|s3|e1", str(f))
    assert (entry.title, entry.season, entry.episode) == ("无职转生", 3, 1)


def test_register_unparseable_key_uses_placeholders(tmp_path):
    h = _host(tmp_path)
    _install(h)
    f = _media_file(h)
    entry = h.anime_register_download("ghost", "not-a-key", str(f))
    assert (entry.title, entry.season, entry.episode) == ("not-a-key", 1, 0)


# -- pre-registration validation (review) ----------------------------------------

def test_register_rejects_path_outside_download_dir(tmp_path):
    h = _host(tmp_path)
    _install(h)
    outside = tmp_path / "evil.mkv"
    outside.write_bytes(b"x")
    with pytest.raises(ValueError):
        h.anime_register_download("r", "无职转生|s3|e1", str(outside))
    assert not (tmp_path / "store" / "library.json").exists()   # nothing registered


def test_register_rejects_non_media_extension(tmp_path):
    h = _host(tmp_path)
    _install(h)
    bad = Path(h.config.anime.download_dir) / "evil.desktop"
    bad.write_bytes(b"x")
    with pytest.raises(ValueError):
        h.anime_register_download("r", "无职转生|s3|e1", str(bad))
    part = Path(h.config.anime.download_dir) / "ep.mkv.part"
    part.write_bytes(b"x")
    with pytest.raises(ValueError):
        h.anime_register_download("r", "无职转生|s3|e1", str(part))


def test_register_folder_result_picks_largest_media_file(tmp_path):
    h = _host(tmp_path)
    _install(h)
    folder = Path(h.config.anime.download_dir) / "ep1"
    folder.mkdir()
    (folder / "sample.mkv").write_bytes(b"x" * 4)
    big = folder / "main.mkv"
    big.write_bytes(b"x" * 128)
    (folder / "readme.txt").write_bytes(b"x" * 999)   # non-media never wins
    entry = h.anime_register_download("r", "无职转生|s3|e1", str(folder))
    assert entry.file_path == str(big)


def test_register_folder_without_media_rejected(tmp_path):
    h = _host(tmp_path)
    _install(h)
    folder = Path(h.config.anime.download_dir) / "ep1"
    folder.mkdir()
    (folder / "evil.sh").write_bytes(b"x")
    with pytest.raises(ValueError):
        h.anime_register_download("r", "无职转生|s3|e1", str(folder))


# -- mark_played / is_played / restart load ---------------------------------------

def test_mark_played_persists_and_is_played(tmp_path):
    h = _host(tmp_path)
    _install(h)
    f = _media_file(h)
    h.anime_register_download("r", "无职转生|s3|e1", str(f))
    assert h.anime_is_played("无职转生|s3|e1") is False
    h.anime_mark_played("无职转生|s3|e1")
    assert h.anime_is_played("无职转生|s3|e1") is True
    [saved] = json.loads((tmp_path / "store" / "library.json").read_text("utf-8"))
    assert saved["played"] is True


def test_reregister_played_episode_preserves_played_state(tmp_path):
    h = _host(tmp_path)
    _install(h)
    f = _media_file(h)
    h.anime_register_download("first", "无职转生|s3|e1", str(f))
    h.anime_mark_played("无职转生|s3|e1")

    entry = h.anime_register_download(
        "later", "无职转生|s3|e1", str(f))

    assert entry.played is True
    assert h.anime_is_played("无职转生|s3|e1") is True
    [saved] = json.loads((tmp_path / "store" / "library.json").read_text("utf-8"))
    assert saved["played"] is True


def test_restart_loads_persisted_library(tmp_path):
    h1 = _host(tmp_path)
    _install(h1)
    f = _media_file(h1)
    h1.anime_register_download("r", "无职转生|s3|e1", str(f))
    # a fresh install on the same config picks the entries back up (startup load)
    h2 = _host(tmp_path)
    _install(h2)                                   # no injected library
    assert h2.anime_library.find("无职转生|s3|e1") is not None


def test_drop_pending_persists(tmp_path, monkeypatch):
    h = _host(tmp_path)
    _install(h)
    rid = _request_download(h, monkeypatch)
    h.anime_drop_pending(rid)
    assert h.anime_list_pending() == []
    assert json.loads((tmp_path / "store" / "pending.json").read_text("utf-8")) == []


# -- F8 busy seam through install --------------------------------------------------

def test_in_flight_seam_reports_busy(tmp_path, monkeypatch):
    h = _host(tmp_path)
    _install(h)
    h._anime_in_flight = lambda: {"progress": 0.42, "title": "无职转生 第三季"}
    monkeypatch.setattr(watch_flow, "resolve_episode",
                        lambda *a, **k: pytest.fail("busy gate sits before resolve"))
    with pytest.raises(ScreenToolError) as ei:
        _tool_run(h)(query="无职转生第三季第一集", episode=None,
                     use_recent_unplayed=None)
    assert ei.value.code == "ANIME_DOWNLOAD_BUSY"
    assert "42" in ei.value.message


def test_in_flight_seam_absent_attr_is_tolerated(tmp_path, monkeypatch):
    # a host without the seam attribute (fake hosts / pre-Phase-4) never crashes
    h = _host(tmp_path)
    _install(h)
    assert not hasattr(h, "_anime_in_flight")
    rid = _request_download(h, monkeypatch)        # resolves fine -> downloading
    assert rid


# -- user-requested stop tool -------------------------------------------------

def test_cancel_tool_submits_trusted_active_request_even_when_anime_disabled(tmp_path):
    h = _host(tmp_path, enabled=False)
    h._anime_in_flight = lambda: {
        "request_id": "REQ1", "title": "幼女战记 第二季", "progress": 0.0,
    }
    _install(h)

    names = {schema.get("name") for schema in h.registry.tool_schemas()}
    assert "cancel_anime_download" in names
    handler = h.registry.tool_handler("cancel_anime_download")
    assert handler is not None
    assert handler() == {
        "status": "submitted", "request_id": "REQ1", "title": "幼女战记 第二季",
    }
    assert h.sunk == [AnimeCancelRequestEvent(
        request_id="REQ1", title="幼女战记 第二季")]
    assert h.registry.tool_effect("cancel_anime_download") == "act"
    assert h.registry.tool_chainable("cancel_anime_download") is False
    assert h.registry.tool_intent_gated("cancel_anime_download") is False


@pytest.mark.parametrize(
    ("sink", "state"),
    [
        (None, {"request_id": "REQ1", "title": "幼女战记"}),
        (lambda ev: None, None),
        (lambda ev: None, {"request_id": "", "title": "幼女战记"}),
    ],
)
def test_cancel_tool_is_only_supplied_with_sink_and_valid_active_request(
        tmp_path, sink, state):
    h = _host(tmp_path)
    h._anime_sink = sink
    h._anime_in_flight = lambda: state
    _install(h)

    names = {schema.get("name") for schema in h.registry.tool_schemas()}
    assert "cancel_anime_download" not in names


def test_forced_cancel_call_without_offer_fails_closed(tmp_path):
    h = _host(tmp_path)
    h._anime_in_flight = lambda: {"request_id": "REQ1", "title": "幼女战记"}
    _install(h)
    handler = h.registry.tool_handler("cancel_anime_download")
    assert handler is not None

    with pytest.raises(ScreenToolError) as caught:
        handler()

    assert caught.value.code == "ANIME_CANCEL_REQUEST_STALE"
    assert h.sunk == []


def test_cancel_offer_a_then_missing_fails_stale_without_emit(tmp_path):
    h = _host(tmp_path)
    state = {"request_id": "A", "title": "第一集"}
    h._anime_in_flight = lambda: dict(state) if state else None
    _install(h)
    assert "cancel_anime_download" in {
        schema.get("name") for schema in h.registry.tool_schemas()
    }
    handler = h.registry.tool_handler("cancel_anime_download")
    assert handler is not None
    state.clear()

    with pytest.raises(ScreenToolError) as caught:
        handler()

    assert caught.value.code == "ANIME_CANCEL_REQUEST_STALE"
    assert h.sunk == []


def test_cancel_offer_a_then_active_b_never_redirects(tmp_path):
    h = _host(tmp_path)
    state = {"request_id": "A", "title": "第一集"}
    h._anime_in_flight = lambda: dict(state)
    _install(h)
    h.registry.tool_schemas()
    handler = h.registry.tool_handler("cancel_anime_download")
    assert handler is not None
    state.update(request_id="B", title="第二集")

    with pytest.raises(ScreenToolError) as caught:
        handler()

    assert caught.value.code == "ANIME_CANCEL_REQUEST_STALE"
    assert h.sunk == []


def test_cancel_offer_is_one_shot_and_duplicate_is_stale(tmp_path):
    h = _host(tmp_path)
    h._anime_in_flight = lambda: {"request_id": "A", "title": "第一集"}
    _install(h)
    h.registry.tool_schemas()
    handler = h.registry.tool_handler("cancel_anime_download")
    assert handler is not None

    assert handler()["request_id"] == "A"
    with pytest.raises(ScreenToolError) as caught:
        handler()

    assert caught.value.code == "ANIME_CANCEL_REQUEST_STALE"
    assert h.sunk == [AnimeCancelRequestEvent(request_id="A", title="第一集")]


def test_cancel_sink_failure_is_a_tool_error_not_a_false_ack(tmp_path):
    h = _host(tmp_path)
    h._anime_in_flight = lambda: {"request_id": "REQ1", "title": "幼女战记"}

    def broken_sink(event):
        raise RuntimeError("UI bridge closed")

    h._anime_sink = broken_sink
    _install(h)
    h.registry.tool_schemas()
    handler = h.registry.tool_handler("cancel_anime_download")
    assert handler is not None

    with pytest.raises(ScreenToolError) as caught:
        handler()

    assert caught.value.code == "ANIME_CANCEL_SUBMIT_FAILED"

    # The failed hand-off consumed the offer.  Replacing the sink does not let
    # a retry fall back to whatever task happens to be current now.
    h._anime_sink = h.sunk.append
    with pytest.raises(ScreenToolError) as retried:
        handler()
    assert retried.value.code == "ANIME_CANCEL_REQUEST_STALE"
    assert h.sunk == []


def test_cancel_sink_disappearing_after_offer_consumes_identity(tmp_path):
    h = _host(tmp_path)
    h._anime_in_flight = lambda: {
        "request_id": "REQ1", "title": "幼女战记"}
    _install(h)
    h.registry.tool_schemas()
    handler = h.registry.tool_handler("cancel_anime_download")
    assert handler is not None
    h._anime_sink = None

    with pytest.raises(ScreenToolError) as caught:
        handler()

    assert caught.value.code == "ANIME_UI_NOT_READY"
    h._anime_sink = h.sunk.append
    with pytest.raises(ScreenToolError) as retry:
        handler()
    assert retry.value.code == "ANIME_CANCEL_REQUEST_STALE"
    assert h.sunk == []


def test_declined_offer_is_cleared_before_next_capability_generation(tmp_path):
    h = _host(tmp_path)
    state = {"request_id": "A", "title": "第一集"}
    h._anime_in_flight = lambda: dict(state) if state else None
    _install(h)
    handler = h.registry.tool_handler("cancel_anime_download")
    assert handler is not None

    # Turn A offered the tool but the model declined it.
    h.registry.tool_schemas()
    # The next turn has no active task. Availability must clear A before it
    # returns False; a forced call after this generation is stale.
    state.clear()
    assert "cancel_anime_download" not in {
        schema.get("name") for schema in h.registry.tool_schemas()
    }
    # Restore the exact old live ID without another capability generation.  If
    # the unavailable generation failed to clear TLS, this forced call would
    # incorrectly consume the abandoned A offer and emit an event.
    state.update(request_id="A", title="第一集")
    with pytest.raises(ScreenToolError) as stale:
        handler()
    assert stale.value.code == "ANIME_CANCEL_REQUEST_STALE"
    assert h.sunk == []

    # A later genuine B offer binds B, never revives A.
    state.update(request_id="B", title="第二集")
    h.registry.tool_schemas()
    assert handler()["request_id"] == "B"
    assert h.sunk == [AnimeCancelRequestEvent(request_id="B", title="第二集")]


def test_cancel_request_id_is_compared_as_an_opaque_token(tmp_path):
    opaque_id = "  request-id-with-significant-space  "
    h = _host(tmp_path)
    h._anime_in_flight = lambda: {
        "request_id": opaque_id,
        "title": "第一集",
    }
    _install(h)

    h.registry.tool_schemas()
    handler = h.registry.tool_handler("cancel_anime_download")
    assert handler is not None

    assert handler()["request_id"] == opaque_id
    assert h.sunk == [AnimeCancelRequestEvent(
        request_id=opaque_id, title="第一集")]


# -- A2: empty source lists must not crash startup (P2-6) ----------------------

def test_install_empty_mikan_urls_disabled_does_not_crash(tmp_path):
    # install() runs UNCONDITIONALLY in AppHost.initialize, even when anime is
    # disabled; an empty mikan_base_urls must skip that source, never raise (P2-6).
    h = _host(tmp_path, enabled=False, mikan_base_urls=[])
    anime_assembly.install(h)                      # real sources built -> no crash


def test_install_both_source_lists_empty_enabled_resolves_source_error(tmp_path):
    # both lists empty + enabled: watch_anime still registers but every resolve
    # returns a STABLE ANIME_SOURCE_ERROR (no sources), never a startup crash.
    h = _host(tmp_path, enabled=True, mikan_base_urls=[], bilibili_spaces=[])
    anime_assembly.install(h)
    handler = h.registry.tool_handler("watch_anime")
    assert handler is not None
    with pytest.raises(ScreenToolError) as ei:
        handler(query="无职转生第三季第一集", episode=None, use_recent_unplayed=None)
    assert ei.value.code == "ANIME_SOURCE_ERROR"
