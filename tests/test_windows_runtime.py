"""Regressions at the Python entry and vendored Windows text frontend seams."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEXT_ROOT = ROOT / "artifacts/tts_slim/base/GPT_SoVITS/text"


def test_python_entry_anchors_resources_before_starting_ui(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    import webui_qt
    from ui import qt_overlay

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(webui_qt, "_check_linux_qt_xcb_dependency", lambda: True)
    monkeypatch.setattr(webui_qt, "_configure_linux_alsa_plugins", lambda: None)
    monkeypatch.setattr(webui_qt, "_configure_linux_input_method", lambda: None)
    seen = []
    monkeypatch.setattr(qt_overlay, "load_secrets", lambda: None)
    monkeypatch.setattr(qt_overlay, "QApplication", lambda _args: seen.append(Path.cwd()) or SimpleNamespace(
        setQuitOnLastWindowClosed=lambda _value: None, exec=lambda: 7,
    ))
    monkeypatch.setattr(qt_overlay, "OverlayWindow", lambda: SimpleNamespace(
        show=lambda: None, _restart_requested=False,
    ))
    assert webui_qt.main() == 7
    assert seen == [ROOT]


def test_japanese_dictionary_survives_cwd_restore_on_windows(tmp_path, monkeypatch):
    text_dir = tmp_path / "桌宠 资源" / "text"
    user_dir = text_dir / "ja_userdic"
    user_dir.mkdir(parents=True)
    (user_dir / "userdict.csv").write_text("紗凪", encoding="utf-8")
    source = text_dir / "japanese.py"
    source.write_bytes((TEXT_ROOT / "japanese.py").read_bytes())
    dictionary = tmp_path / "用户 Python" / "dictionary"
    dictionary.mkdir(parents=True)
    compiled, loaded = [], []

    def compile_dictionary(csv, binary):
        compiled.append(csv)
        Path(binary).write_bytes(b"dictionary")

    jtalk = SimpleNamespace(
        OPEN_JTALK_DICT_DIR=str(dictionary).encode("utf-8"),
        mecab_dict_index=compile_dictionary,
        update_global_jtalk_with_user_dict=loaded.append,
    )
    win_os = ModuleType("os")
    win_os.__dict__.update(vars(os))
    win_os.name = "nt"
    monkeypatch.chdir(text_dir.parent)
    monkeypatch.setitem(sys.modules, "os", win_os)
    monkeypatch.setitem(sys.modules, "pyopenjtalk", jtalk)
    monkeypatch.setitem(sys.modules, "text.symbols", SimpleNamespace(punctuation=["!", "?", ".", ","]))
    spec = importlib.util.spec_from_file_location("windows_japanese", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    assert jtalk.OPEN_JTALK_DICT_DIR == str(dictionary).encode("utf-8")
    assert compiled == [str(user_dir / "userdict.csv")]
    assert loaded == [str(user_dir / "user.dict")]
    assert Path(loaded[0]).is_file()


def test_chinese_phonemes_work_without_compiled_jieba(tmp_path):
    pytest.importorskip("jieba")
    pytest.importorskip("pypinyin")
    pytest.importorskip("cn2an")
    # A fresh process avoids polluting the application's top-level `text` imports.
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", """
import sys
sys.path.insert(0, sys.argv[1])
sys.modules['jieba_fast'] = None
from text.chinese import g2p, text_normalize
text = text_normalize('你好，今天一起聊天吧。')
phones, word2ph = g2p(text)
assert phones and len(word2ph) == len(text)
assert sum(word2ph) == len(phones)
""", str(TEXT_ROOT.parent)],
        cwd=tmp_path, capture_output=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stderr
