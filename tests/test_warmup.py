"""Phase 6E: AppHost.warmup (host-orchestrated startup warmup, Qt-free)."""

import unittest
from types import SimpleNamespace

from spica.host.app_host import AppHost


def _host(tts):
    host = AppHost()
    host.chat_engine = SimpleNamespace(model="gpt-x")  # conversation_surface
    host.tts_adapter = tts
    return host


def _run(tts):
    events: list[tuple[str, str]] = []
    _host(tts).warmup(lambda stage, message: events.append((stage, message)))
    return events


class _TTS:
    name = "gsv"

    def __init__(self, config, results):
        self._config = config
        self._results = list(results)
        self.calls: list[str] = []

    def public_config(self):
        return self._config

    def warmup(self, emotion, synthesize):
        self.calls.append(emotion)
        return self._results.pop(0)


class WarmupTest(unittest.TestCase):
    def test_no_warmup_capability_reports_ready(self):
        events = _run(SimpleNamespace(name="dummy"))  # no public_config / warmup
        self.assertEqual(events[0][0], "initializing")
        self.assertIn("gpt-x", events[0][1])
        self.assertEqual(events[-1][0], "ready")
        self.assertIn("无需启动预热", events[-1][1])

    def test_warmup_disabled(self):
        events = _run(_TTS({"warmup_on_startup": False}, []))
        self.assertEqual(events[-1][0], "ready")
        self.assertIn("已关闭", events[-1][1])

    def test_warmup_success(self):
        tts = _TTS({"warmup_on_startup": True, "warmup_emotion": "happy"}, [{"ok": True, "duration_ms": 12}])
        events: list[tuple[str, str]] = []
        host = _host(tts)
        host.warmup(lambda stage, message: events.append((stage, message)))
        self.assertEqual(tts.calls, ["happy"])
        self.assertEqual([s for s, _ in events], ["initializing", "initializing", "ready"])
        self.assertIn("就绪", events[-1][1])

    def test_warmup_multiple_emotions(self):
        tts = _TTS(
            {"warmup_on_startup": True, "warmup_emotions": ["happy", "sad"]},
            [{"ok": True, "duration_ms": 5}, {"ok": True, "duration_ms": 7}],
        )
        events: list[tuple[str, str]] = []
        _host(tts).warmup(lambda stage, message: events.append((stage, message)))
        self.assertEqual(tts.calls, ["happy", "sad"])
        self.assertEqual(events[-1][0], "ready")

    def test_warmup_failure_reports_error(self):
        events = _run(_TTS({"warmup_on_startup": True, "warmup_emotion": "happy"},
                           [{"ok": False, "error": "boom"}]))
        self.assertEqual(events[-1][0], "error")
        self.assertIn("boom", events[-1][1])


if __name__ == "__main__":
    unittest.main()


def test_daily_silent_does_not_warm_tts_or_change_stt_preference():
    from spica.config.schema import AppConfig, TtsConfig
    calls = []
    host = _host(_TTS({"warmup_on_startup": True}, []))
    host.config = AppConfig(tts=TtsConfig(daily_enabled=False))
    host.stt_adapter = SimpleNamespace(warmup=lambda: calls.append("stt") or {"ok": True})
    host.config.stt.warmup_on_startup = True
    host.warmup(lambda *_: None)
    assert host.tts_adapter.calls == [] and calls == ["stt"]
