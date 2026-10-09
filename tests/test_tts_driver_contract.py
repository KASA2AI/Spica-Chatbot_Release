"""GptSovitsV2ProDriver contract (LOCAL_RUNTIME_PLAN cut 2, A2, CI-pure).

Injected FAKE callables -- NO vendored model / GPU / model_imports (§6.5). Pins:
- synthesize_chunks returns an iterator of (sample_rate, ndarray);
- load caches by path (change_gpt_weights called once; again only on a new path);
- cwd is restored even when the vendored call raises (the protected-pushd residual);
- service.py no longer does the vendored sys.path / pushd / inference_webui import.
"""

import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pytest

from spica.local_runtime.tts.driver import GptSovitsV2ProDriver


def _fake_get_tts_wav_factory(pieces):
    def fake(**kwargs):
        for sr, audio in pieces:
            yield sr, audio
    return fake


class _Recorder:
    def __init__(self):
        self.gpt_calls = []
        self.sovits_calls = []

    def change_gpt(self, *, gpt_path):
        self.gpt_calls.append(gpt_path)

    def change_sovits(self, *, sovits_path, prompt_language, text_language):
        self.sovits_calls.append((sovits_path, prompt_language, text_language))
        yield  # the real change_sovits_weights is a generator (UI updates); driver drains it


def _driver(tmp_root, rec=None, get_tts=None):
    rec = rec or _Recorder()
    return GptSovitsV2ProDriver(
        tmp_root,
        i18n=lambda x: x,
        change_gpt_weights=rec.change_gpt,
        change_sovits_weights=rec.change_sovits,
        get_tts_wav=get_tts or _fake_get_tts_wav_factory([(32000, np.zeros(8, dtype=np.int16))]),
    ), rec


class TtsDriverContractTest(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd()  # a real existing dir for pushd

    def test_synthesize_chunks_yields_sr_ndarray(self):
        pieces = [(32000, np.ones(4, dtype=np.int16)), (32000, np.zeros(4, dtype=np.int16))]
        drv, _ = _driver(self.root, get_tts=_fake_get_tts_wav_factory(pieces))
        out = list(drv.synthesize_chunks(text="x"))
        self.assertEqual(len(out), 2)
        for sr, audio in out:
            self.assertEqual(sr, 32000)
            self.assertIsInstance(audio, np.ndarray)

    def test_load_caches_by_path(self):
        drv, rec = _driver(self.root)
        kw = dict(sovits_path="s.pth", prompt_language="ja", text_language="ja")
        drv.load(gpt_path="g.ckpt", **kw)
        drv.load(gpt_path="g.ckpt", **kw)  # same -> cached, not reloaded
        self.assertEqual(rec.gpt_calls, ["g.ckpt"])
        self.assertEqual(len(rec.sovits_calls), 1)
        drv.load(gpt_path="g2.ckpt", **kw)  # new gpt path -> reload
        self.assertEqual(rec.gpt_calls, ["g.ckpt", "g2.ckpt"])

    def test_load_force_reloads(self):
        drv, rec = _driver(self.root)
        kw = dict(gpt_path="g.ckpt", sovits_path="s.pth", prompt_language="ja", text_language="ja")
        drv.load(**kw)
        drv.load(force=True, **kw)
        self.assertEqual(rec.gpt_calls, ["g.ckpt", "g.ckpt"])

    def test_unload_invalidates_weights_and_allows_loading_again(self):
        drv, rec = _driver(self.root)
        kw = dict(gpt_path="g.ckpt", sovits_path="s.pth", prompt_language="ja", text_language="ja")
        drv.load(**kw)
        assert drv.ready
        generation = drv.generation
        drv.unload()
        assert not drv.ready
        assert drv.generation != generation
        drv.load(**kw)
        assert drv.ready
        self.assertEqual(rec.gpt_calls, ["g.ckpt", "g.ckpt"])

    def test_another_role_invalidates_the_previous_global_weight_cache(self):
        first, recorded = _driver(self.root)
        second, _ = _driver(self.root)
        kw = dict(gpt_path="one.ckpt", sovits_path="one.pth", prompt_language="ja", text_language="ja")
        first.load(**kw)
        second.load(**dict(kw, gpt_path="two.ckpt"))
        assert not first.ready and second.ready
        first.load(**kw)
        assert first.ready and not second.ready
        assert recorded.gpt_calls == ['one.ckpt', 'one.ckpt']

    def test_synthesize_does_not_change_cwd(self):
        # A3: synthesize_chunks must NOT pushd -- get_tts_wav runs at the ORIGINAL cwd
        # (no call-time cwd dependency on Linux). root != cwd so a pushd would show.
        seen = {}

        def fake(**kwargs):
            seen["cwd"] = os.getcwd()
            yield (32000, np.zeros(4, dtype=np.int16))

        with tempfile.TemporaryDirectory() as root:
            drv, _ = _driver(root, get_tts=fake)
            before = os.getcwd()
            list(drv.synthesize_chunks(text="x"))
            self.assertEqual(seen["cwd"], before)  # ran WITHOUT pushd to root
            self.assertEqual(os.getcwd(), before)

    def test_cwd_unchanged_when_synthesize_raises(self):
        # A3: even on a vendored exception, synthesize never touches cwd (no pushd).
        def boom(**kwargs):
            raise RuntimeError("vendored boom")
            yield  # noqa: unreachable -- make it a generator

        with tempfile.TemporaryDirectory() as root:
            drv, _ = _driver(root, get_tts=boom)
            before = os.getcwd()
            with self.assertRaises(RuntimeError):
                list(drv.synthesize_chunks(text="x"))
            self.assertEqual(os.getcwd(), before)

    def test_load_pushes_cwd_during_call(self):
        # A3 contrast: load MUST still pushd (change_*_weights' cwd-relative
        # ./weight.json) -- cwd is the root DURING the call, restored after.
        seen = {}

        class _CwdRecorder(_Recorder):
            def change_gpt(self, *, gpt_path):
                seen["gpt_cwd"] = os.getcwd()
                super().change_gpt(gpt_path=gpt_path)

        with tempfile.TemporaryDirectory() as root:
            drv, _ = _driver(root, rec=_CwdRecorder())
            before = os.getcwd()
            drv.load(gpt_path="g", sovits_path="s", prompt_language="ja", text_language="ja")
            self.assertNotEqual(seen["gpt_cwd"], before)  # load DID pushd
            self.assertEqual(seen["gpt_cwd"], os.path.realpath(root))  # ...to the root
            self.assertEqual(os.getcwd(), before)  # restored after

    def test_i18n_passthrough(self):
        drv, _ = _driver(self.root)
        self.assertEqual(drv.i18n("日文"), "日文")  # injected identity i18n

    def test_service_no_longer_does_vendored_sys_path_or_import(self):
        # A2 goal: service.py swaps the inference SOURCE only. It must no longer
        # `import os`/`import sys` (the sys.path/os.chdir glue) nor import the
        # vendored inference_webui -- all that moved to local_runtime.tts. AST-based
        # so comments/docstrings that merely mention the old glue don't false-match.
        import ast
        import inspect

        import agent_tools.tts.gptsovits.service as service

        tree = ast.parse(inspect.getsource(service))
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                bad += [f"import {a.name}" for a in node.names if a.name in ("os", "sys")]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if any(token in module for token in ("inference_webui", "GPT_SoVITS", "i18n")):
                    bad.append(f"from {module} import ...")
        self.assertEqual(bad, [], f"service.py still does vendored sys.path/import glue: {bad}")


if __name__ == "__main__":
    unittest.main()


def test_service_latest_residency_intent_releases_after_native_warmup(tmp_path, monkeypatch):
    import json
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from agent_tools.tts.gptsovits.service import GPTSoVITSTool
    from types import SimpleNamespace
    entered, release = threading.Event(), threading.Event()
    unloaded = []
    driver = SimpleNamespace(ready=False, generation=0, i18n=lambda value: value)
    def load(**_):
        entered.set()
        assert release.wait(3)
        driver.ready = True
        driver.generation += 1
    def unload():
        unloaded.append(True)
        driver.ready = False
        driver.generation += 1
    driver.load, driver.unload = load, unload
    monkeypatch.setattr('spica.local_runtime.tts.GptSovitsV2ProDriver', lambda root: driver)
    reference = tmp_path / 'ref.wav'
    reference.touch()
    path = tmp_path / 'tts.json'
    path.write_text(json.dumps(dict(gptsovits_root=str(tmp_path), output_dir=str(tmp_path/'audio'),
        gpt_model_path='g.ckpt', sovits_model_path='s.pth',
        emotions={'happy': {'ref_audio_path': str(reference), 'prompt_text': 'はい。'}})))
    tool = GPTSoVITSTool(path)
    tool.set_resident('daily', True)
    tool.set_resident('alarm', True)
    with ThreadPoolExecutor() as workers:
        pending = workers.submit(tool.warmup)
        assert entered.wait(1)
        tool.set_resident('daily', False)  # Does not wait for the native load.
        release.set()
        assert pending.result(3)['ok']
        tool.release_if_unused()
        assert tool.resource_status['ready'] and not unloaded
        entered.clear()
        release.clear()
        pending = workers.submit(tool.warmup)
        assert entered.wait(1)
        tool.set_resident('alarm', False)
        draining = workers.submit(tool.release_if_unused)
        assert not draining.done() and not unloaded
        release.set()
        assert pending.result(3)['ok']
        draining.result(3)
    assert not tool.resource_status['ready'] and unloaded


def test_reenable_while_unload_waits_for_native_lock_keeps_ready_models(tmp_path, monkeypatch):
    import json
    import threading
    from agent_tools.tts.gptsovits.service import GPTSoVITSTool
    from spica.local_runtime.tts import model_imports
    waiting = threading.Event()
    native = threading.RLock()
    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == 'unload-test':
                waiting.set()
            native.acquire()
        def __exit__(self, *_):
            native.release()
    lock = ObservedLock()
    monkeypatch.setattr(model_imports, 'INFERENCE_LOCK', lock)
    driver, _ = _driver(tmp_path)
    driver._lock = lock
    monkeypatch.setattr('spica.local_runtime.tts.GptSovitsV2ProDriver', lambda _: driver)
    (tmp_path/'ref.wav').touch()
    path = tmp_path/'tts.json'
    path.write_text(json.dumps(dict(gptsovits_root=str(tmp_path), output_dir=str(tmp_path/'audio'),
        gpt_model_path='g.ckpt', sovits_model_path='s.pth',
        emotions={'happy': {'ref_audio_path': str(tmp_path/'ref.wav'), 'prompt_text': 'はい。'}})))
    tool = GPTSoVITSTool(path)
    tool.set_resident('daily', True)
    assert tool.warmup()['ok'] and tool.resource_status['ready']
    with lock:
        tool.set_resident('daily', False)
        worker = threading.Thread(target=tool.release_if_unused, name='unload-test')
        worker.start()
        assert waiting.wait(2)
        tool.set_resident('daily', True)
    worker.join(2)
    assert not worker.is_alive() and tool.resource_status['ready']


@pytest.mark.parametrize('fail', [False, True])
def test_loaded_weights_do_not_publish_ready_before_warmup_inference(tmp_path, monkeypatch, fail):
    import json
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from agent_tools.tts.gptsovits.service import GPTSoVITSTool
    entered, release = threading.Event(), threading.Event()
    def infer(**_):
        entered.set()
        assert release.wait(3)
        if fail:
            raise RuntimeError('native warmup failed')
        yield 32000, np.zeros(8, dtype=np.int16)
    driver, _ = _driver(tmp_path, get_tts=infer)
    monkeypatch.setattr('spica.local_runtime.tts.GptSovitsV2ProDriver', lambda _: driver)
    (tmp_path/'ref.wav').touch()
    path = tmp_path/'tts.json'
    path.write_text(json.dumps(dict(gptsovits_root=str(tmp_path), output_dir=str(tmp_path/'audio'),
        gpt_model_path='g.ckpt', sovits_model_path='s.pth',
        emotions={'happy': {'ref_audio_path': str(tmp_path/'ref.wav'), 'prompt_text': 'はい。'}})))
    tool = GPTSoVITSTool(path)
    tool.set_resident('daily', True)
    with ThreadPoolExecutor() as workers:
        pending = workers.submit(tool.warmup, synthesize=True)
        try:
            assert entered.wait(1)
            assert driver.ready and not tool.resource_status['ready']
            assert tool.resource_status['state'] == 'loading' and not pending.done()
        finally:
            release.set()
        assert pending.result(3)['ok'] is not fail
    assert tool.resource_status['ready'] is not fail
    if fail:
        assert tool.resource_status['state'] == 'failed' and 'native warmup failed' in tool.resource_status['error']


def test_host_tts_shutdown_keeps_one_owner_while_native_close_drains():
    import threading
    from types import SimpleNamespace
    from spica.host.assemblies.audio import begin_tts_shutdown
    entered, release = threading.Event(), threading.Event()
    calls = []
    def close():
        calls.append(True)
        entered.set()
        release.wait(2)
    host = SimpleNamespace(tts_adapter=SimpleNamespace(close=close))
    worker = begin_tts_shutdown(host)
    try:
        assert entered.wait(1) and worker.is_alive()
        assert begin_tts_shutdown(host) is worker
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and calls == [True]
