"""Lazy TTS dependencies without importing the real models in unit tests."""

import ast
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import Mock
import warnings

import pytest


BASE = Path(__file__).resolve().parents[1] / "artifacts/tts_slim/base"
INFERENCE = BASE / "GPT_SoVITS/inference_webui.py"


def _loading_functions():
    """Exercise the shipped functions, isolated from vendor import-time models."""
    names = {"_get_bert_models", "get_bert_inf", "_warn"}
    tree = ast.parse(INFERENCE.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    tensor = Mock()
    tensor.to.return_value = tensor
    model = Mock()
    model.half.return_value = model.to.return_value = model
    namespace = {
        "__name__": "GPT_SoVITS.inference_webui",
        "warnings": warnings,
        "tokenizer": None, "bert_model": None, "_bert_lock": Lock(),
        "bert_path": "/runtime/bert", "is_half": True, "device": "cuda",
        "AutoTokenizer": SimpleNamespace(from_pretrained=Mock(return_value=object())),
        "AutoModelForMaskedLM": SimpleNamespace(from_pretrained=Mock(return_value=model)),
        "torch": SimpleNamespace(zeros=Mock(return_value=tensor), float16="fp16", float32="fp32"),
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(INFERENCE), "exec"), namespace)
    namespace["get_bert_feature"] = lambda *_: (namespace["_get_bert_models"](), tensor)[1]
    return namespace, model


def test_japanese_and_english_skip_bert_chinese_loads_once():
    ns, model = _loading_functions()
    for language in ("all_ja", "en"):
        ns["get_bert_inf"]([1, 2], None, "sample", language)
    ns["torch"].zeros.assert_called_with((1024, 2), dtype="fp16")
    ns["AutoTokenizer"].from_pretrained.assert_not_called()
    ns["AutoModelForMaskedLM"].from_pretrained.assert_not_called()

    for _ in range(2):
        ns["get_bert_inf"]([1, 2], [1, 1], "中文", "all_zh")
    ns["AutoTokenizer"].from_pretrained.assert_called_once_with("/runtime/bert")
    ns["AutoModelForMaskedLM"].from_pretrained.assert_called_once_with("/runtime/bert")
    model.half.assert_called_once_with()
    model.to.assert_called_once_with("cuda")


def test_failed_bert_load_can_retry_without_publishing_partial_state():
    ns, model = _loading_functions()
    model.to.side_effect = [RuntimeError("device unavailable"), model]
    with pytest.raises(RuntimeError, match="device unavailable"):
        ns["_get_bert_models"]()
    assert ns["tokenizer"] is None and ns["bert_model"] is None
    tokenizer, loaded = ns["_get_bert_models"]()
    assert tokenizer is not None and loaded is model


def test_headless_import_has_no_eager_gradio_or_bert_load():
    class ImportWork(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            pass

        def visit_ClassDef(self, node):
            pass

        def visit_If(self, node):
            if ast.unparse(node.test) != "__name__ == '__main__'":
                self.generic_visit(node)

        def visit_Import(self, node):
            assert all(alias.name != "gradio" for alias in node.names)

        def visit_ImportFrom(self, node):
            assert not (node.module or "").startswith("gradio")

        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "from_pretrained":
                assert ast.unparse(node.func.value) not in {"AutoTokenizer", "AutoModelForMaskedLM"}
            self.generic_visit(node)

    for path in (INFERENCE, BASE / "tools/my_utils.py"):
        ImportWork().visit(ast.parse(path.read_text(encoding="utf-8")))
    ns, _ = _loading_functions()
    with pytest.warns(RuntimeWarning, match="missing reference"):
        ns["_warn"]("missing reference")
