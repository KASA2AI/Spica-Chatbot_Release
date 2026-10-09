"""Unpublished drafts use the real dialogue pipeline, without premature memory."""
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from test_proactive_turn import _build_engine, _ChatCompletionsAPI
from spica.core.prepared_speech import PreparedSpeech
from spica.runtime.jobs import InlineJobRunner
from spica.ports.memory import MemoryScope


def prepare(tmp_path):
    calls = []
    engine = _build_engine(SimpleNamespace(base_url='https://api.deepseek.com/v1',
        chat=_ChatCompletionsAPI(calls)), tmp_path)
    engine.config.tts.daily_enabled = False
    draft = PreparedSpeech(engine, InlineJobRunner(), '准备一句简短告别。', ttl_seconds=30,
        want_audio=False, source='home.farewell', is_current=lambda: True)
    draft.start()
    return engine, draft, calls


def test_preparation_does_not_write_or_execute_tools_then_delivery_commits(tmp_path):
    engine, draft, calls = prepare(tmp_path)
    scope = MemoryScope('spica', 'owner', 'default')
    assert draft.ready and not draft.error
    assert engine.deps.memory.evidence(scope) == []
    assert calls and all(not kwargs.get('tools') for _, kwargs in calls)
    directory = Path(draft._directory.name)
    draft.bind('presented')
    events = list(draft.replay('presented', threading.Event()))
    assert events[-1]['event'] == 'done'
    assert engine.deps.memory.evidence(scope) == []
    draft.settle(delivered=True, request_id='presented')
    rows = engine.deps.memory.evidence(scope)
    assert {'system_event', 'assistant_generated', 'playback'} <= {row['kind'] for row in rows}
    assert all(row['turn_id'] == 'presented' for row in rows)
    assert draft.closed and not directory.exists()


def test_withdrawal_rejects_bind_and_cancellation_waits_for_player_release(tmp_path, monkeypatch):
    engine, draft, _ = prepare(tmp_path)
    original = engine.deps.memory.context_is_current
    monkeypatch.setattr(engine.deps.memory, 'context_is_current', lambda *_: False)
    with pytest.raises(RuntimeError, match='no longer available'):
        draft.bind('late')
    monkeypatch.setattr(engine.deps.memory, 'context_is_current', original)
    draft.bind('active')
    directory = Path(draft._directory.name)
    draft.cancel()
    assert directory.exists() and not draft.closed
    with pytest.raises(RuntimeError, match='context changed'):
        list(draft.replay('active', threading.Event()))
    draft.settle(delivered=False)
    assert draft.closed and not directory.exists()
    assert not engine.deps.memory.evidence(MemoryScope('spica', 'owner', 'default'))
