"""The idle sampler stores timing only and cannot outlive its UI owner."""

from types import SimpleNamespace

from test_voice_wake import qapp
from ui import desktop_input_activity as module


def test_idle_sampling_is_lazy_cached_and_closed_once(qapp, monkeypatch):
    now, queries, closed, opened = [100.], [], [], []
    reader = SimpleNamespace(seconds=lambda: queries.append(True) or 30.,
                             close=lambda: closed.append(True))
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(module.QGuiApplication, 'platformName', lambda: 'xcb')
    monkeypatch.setattr(module, '_X11Idle', lambda: opened.append(True) or reader)
    owner = module.DesktopInputActivity()
    try:
        assert opened == []
        assert owner.idle_seconds() == 30.
        now[0] += .25
        assert owner.idle_seconds() == 30.25 and len(queries) == 1
        now[0] += .25
        assert owner.idle_seconds() == 30. and len(queries) == 2
        owner.shutdown()
        assert owner.idle_seconds() is None
        owner.shutdown()
        assert closed == [True]
    finally:
        owner.shutdown()
        owner.deleteLater()


def test_unknown_platform_retains_until_actual_local_input(qapp, monkeypatch):
    now = [10.]
    monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='unsupported'))
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    owner = module.DesktopInputActivity()
    try:
        assert owner.idle_seconds() is None
        event = SimpleNamespace(type=lambda: module.QEvent.Type.MouseMove, spontaneous=lambda: False)
        owner.eventFilter(None, event)
        assert owner.idle_seconds() is None, 'Synthetic UI movement cannot acknowledge a notification.'
        event.spontaneous = lambda: True
        owner.eventFilter(None, event)
        now[0] += 2
        assert owner.idle_seconds() == 2.
    finally:
        owner.shutdown()
        owner.deleteLater()
