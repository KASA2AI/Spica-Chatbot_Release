"""GptSovitsV2ProDriver contract (LOCAL_RUNTIME_PLAN cut 2, A2, CI-pure).

Injected FAKE callables -- NO vendored model / GPU / model_imports (§6.5). Pins:
- synthesize_chunks returns an iterator of (sample_rate, ndarray);
- load caches by path (change_gpt_weights called once; again only on a new path);
- cwd is restored even when the vendored call raises (the protected-pushd residual);
- service.py no longer does the vendored sys.path / pushd / inference_webui import.
"""

import os
import unittest
from pathlib import Path

import numpy as np

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

    def test_cwd_restored_when_vendored_call_raises(self):
        def boom(**kwargs):
            raise RuntimeError("vendored boom")
            yield  # noqa: unreachable -- make it a generator

        drv, _ = _driver(self.root, get_tts=boom)
        before = os.getcwd()
        with self.assertRaises(RuntimeError):
            list(drv.synthesize_chunks(text="x"))
        self.assertEqual(os.getcwd(), before)  # protected pushd restored cwd

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
