"""W4-b Windows GPU preload: injectable unit tests (run on Linux, everything
Windows-only is mocked).

Pins the gate-1-validated design (docs/windows_w4_probe.md §5.3):
- ORT's CUDA/TRT EPs resolve cudnn64_9.dll / nvinfer_10.dll via standard
  LoadLibrary search, which does NOT include wheel dirs -> the preload must
  register candidate dirs (os.add_dll_directory) AND force-load the core DLLs
  (ctypes.WinDLL), with handles AND DLL objects kept alive at module level;
- package dirs are located via importlib.util.find_spec WITHOUT importing
  (never `import torch` just to fix a search path);
- order: cuDNN before nvinfer (nvinfer's own deps must already resolve);
- best-effort: a missing package / failing load skips, never raises;
- the Linux route is untouched: on a non-nt host the main entrypoints never
  call the Windows helper.
"""

import ctypes
import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

import agent_tools.function_tools.screen.backends.rapidocr as backend
import spica.local_runtime.ocr.rapidocr_trt_runtime as trt_runtime


@pytest.fixture
def clean_state():
    """Snapshot + clear the module-level keep-alive lists and once-guards."""
    saved = (
        backend._CUDA_PRELOADED, list(backend._WIN_DLL_DIR_HANDLES), list(backend._WIN_LOADED_DLLS),
        trt_runtime._LIBS_PRELOADED, list(trt_runtime._WIN_DLL_DIR_HANDLES), list(trt_runtime._WIN_LOADED_DLLS),
    )
    backend._WIN_DLL_DIR_HANDLES.clear()
    backend._WIN_LOADED_DLLS.clear()
    trt_runtime._WIN_DLL_DIR_HANDLES.clear()
    trt_runtime._WIN_LOADED_DLLS.clear()
    yield
    backend._CUDA_PRELOADED = saved[0]
    backend._WIN_DLL_DIR_HANDLES[:] = saved[1]
    backend._WIN_LOADED_DLLS[:] = saved[2]
    trt_runtime._LIBS_PRELOADED = saved[3]
    trt_runtime._WIN_DLL_DIR_HANDLES[:] = saved[4]
    trt_runtime._WIN_LOADED_DLLS[:] = saved[5]


def _gpu_tree(tmp_path, *, cudnn_in=("torch",), with_trt=True):
    """Fake site-packages: torch/lib + ctranslate2 + nvidia/*/bin + tensorrt_libs."""
    packages = {}
    torch_dir = tmp_path / "torch"
    (torch_dir / "lib").mkdir(parents=True)
    (torch_dir / "__init__.py").write_text("")
    if "torch" in cudnn_in:
        (torch_dir / "lib" / "cudnn64_9.dll").write_bytes(b"x")
    packages["torch"] = {"dir": torch_dir}

    ct2_dir = tmp_path / "ctranslate2"
    ct2_dir.mkdir()
    (ct2_dir / "__init__.py").write_text("")
    if "ct2" in cudnn_in:
        (ct2_dir / "cudnn64_9.dll").write_bytes(b"x")
    packages["ctranslate2"] = {"dir": ct2_dir}

    nvidia_root = tmp_path / "nvidia"
    cudnn_bin = nvidia_root / "cudnn" / "bin"
    cudnn_bin.mkdir(parents=True)
    if "nvidia" in cudnn_in:
        (cudnn_bin / "cudnn64_9.dll").write_bytes(b"x")
    packages["nvidia"] = {"dir": nvidia_root, "namespace": True}

    if with_trt:
        trt_dir = tmp_path / "tensorrt_libs"
        trt_dir.mkdir()
        (trt_dir / "__init__.py").write_text("")
        for name in ("nvinfer_10.dll", "nvinfer_plugin_10.dll", "nvonnxparser_10.dll"):
            (trt_dir / name).write_bytes(b"x")
        packages["tensorrt_libs"] = {"dir": trt_dir}
    return packages


class _WinMocks:
    """Recording fakes for os.add_dll_directory + ctypes.WinDLL (absent on Linux)."""

    def __init__(self, fail_dlls=()):
        self.registered_dirs = []
        self.loaded_dlls = []
        self.fail_dlls = set(fail_dlls)

    def add_dll_directory(self, path):
        self.registered_dirs.append(path)
        return SimpleNamespace(kind="dll-dir-handle", path=path)

    def win_dll(self, path):
        name = os.path.basename(path)
        if name in self.fail_dlls:
            raise OSError(f"load failed: {name}")
        self.loaded_dlls.append(path)
        return SimpleNamespace(kind="loaded-dll", path=path)


@pytest.fixture
def win(monkeypatch, tmp_path, clean_state):
    """Wire the fake gpu tree + Windows API mocks; returns (mocks, packages)."""
    packages = _gpu_tree(tmp_path)
    mocks = _WinMocks()

    def fake_find_spec(name, *args, **kwargs):
        info = packages.get(name)
        if info is None:
            return None
        if info.get("namespace"):
            return SimpleNamespace(origin=None, submodule_search_locations=[str(info["dir"])])
        return SimpleNamespace(
            origin=str(info["dir"] / "__init__.py"), submodule_search_locations=None
        )

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(os, "add_dll_directory", mocks.add_dll_directory, raising=False)
    monkeypatch.setattr(ctypes, "WinDLL", mocks.win_dll, raising=False)
    return mocks, packages


class TestBackendCudaPreloadWindows:
    def test_registers_candidate_dirs_and_loads_cudnn(self, win, tmp_path):
        mocks, _ = win
        backend._preload_cuda_libraries_windows()
        registered = [os.path.normpath(p) for p in mocks.registered_dirs]
        assert os.path.normpath(str(tmp_path / "nvidia" / "cudnn" / "bin")) in registered
        assert os.path.normpath(str(tmp_path / "torch" / "lib")) in registered
        assert os.path.normpath(str(tmp_path / "ctranslate2")) in registered
        # cudnn64_9.dll force-loaded exactly once (first dir that carries it).
        cudnn_loads = [p for p in mocks.loaded_dlls if p.endswith("cudnn64_9.dll")]
        assert len(cudnn_loads) == 1

    def test_handles_and_dll_objects_kept_alive_at_module_level(self, win):
        backend._preload_cuda_libraries_windows()
        assert backend._WIN_DLL_DIR_HANDLES  # dir handles resident
        assert all(h.kind == "dll-dir-handle" for h in backend._WIN_DLL_DIR_HANDLES)
        assert backend._WIN_LOADED_DLLS  # DLL objects resident
        assert all(d.kind == "loaded-dll" for d in backend._WIN_LOADED_DLLS)

    def test_missing_packages_are_silent(self, monkeypatch, clean_state):
        monkeypatch.setattr(importlib.util, "find_spec", lambda *a, **k: None)
        mocks = _WinMocks()
        monkeypatch.setattr(os, "add_dll_directory", mocks.add_dll_directory, raising=False)
        monkeypatch.setattr(ctypes, "WinDLL", mocks.win_dll, raising=False)
        backend._preload_cuda_libraries_windows()  # must not raise
        assert mocks.registered_dirs == []
        assert mocks.loaded_dlls == []

    def test_windll_failure_falls_through_to_next_candidate(
        self, monkeypatch, tmp_path, clean_state
    ):
        # cudnn present in BOTH torch/lib and ctranslate2; the torch copy fails to
        # load -> best-effort moves on and the ct2 copy goes resident.
        packages = _gpu_tree(tmp_path, cudnn_in=("torch", "ct2"))
        mocks = _WinMocks()
        torch_copy = str(tmp_path / "torch" / "lib" / "cudnn64_9.dll")
        original_win_dll = mocks.win_dll

        def flaky_win_dll(path):
            if path == torch_copy:
                raise OSError("simulated bad torch copy")
            return original_win_dll(path)

        def fake_find_spec(name, *a, **k):
            info = packages.get(name)
            if info is None:
                return None
            if info.get("namespace"):
                return SimpleNamespace(origin=None, submodule_search_locations=[str(info["dir"])])
            return SimpleNamespace(origin=str(info["dir"] / "__init__.py"), submodule_search_locations=None)

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
        monkeypatch.setattr(os, "add_dll_directory", mocks.add_dll_directory, raising=False)
        monkeypatch.setattr(ctypes, "WinDLL", flaky_win_dll, raising=False)
        backend._preload_cuda_libraries_windows()
        assert [p for p in mocks.loaded_dlls if p.endswith("cudnn64_9.dll")] == [
            str(tmp_path / "ctranslate2" / "cudnn64_9.dll")
        ]

    def test_linux_entrypoint_never_calls_windows_helper(self, monkeypatch, clean_state):
        assert os.name != "nt", "this pin is meaningful on the Linux dev machine only"
        calls = []
        monkeypatch.setattr(backend, "_preload_cuda_libraries_windows", lambda: calls.append(1))
        monkeypatch.setattr(backend, "_CUDA_PRELOADED", False)
        # Make the Linux route inert: sys.modules[name]=None -> `import nvidia` raises.
        monkeypatch.setitem(sys.modules, "nvidia", None)
        backend._preload_cuda_libraries()
        assert calls == []  # the fork never routed a posix host into the nt helper


class TestTrtRuntimePreloadWindows:
    def test_loads_cudnn_then_all_nvinfer_dlls(self, win, tmp_path):
        mocks, _ = win
        trt_runtime._preload_inference_libs_windows()
        names = [os.path.basename(p) for p in mocks.loaded_dlls]
        assert "cudnn64_9.dll" in names
        for dll in ("nvinfer_10.dll", "nvinfer_plugin_10.dll", "nvonnxparser_10.dll"):
            assert dll in names
        # Order: cuDNN resident BEFORE nvinfer (nvinfer's deps must already resolve).
        assert names.index("cudnn64_9.dll") < names.index("nvinfer_10.dll")
        # tensorrt_libs dir registered too.
        registered = [os.path.normpath(p) for p in mocks.registered_dirs]
        assert os.path.normpath(str(tmp_path / "tensorrt_libs")) in registered

    def test_keep_alive_lists_populated(self, win):
        trt_runtime._preload_inference_libs_windows()
        assert trt_runtime._WIN_DLL_DIR_HANDLES
        assert trt_runtime._WIN_LOADED_DLLS

    def test_missing_tensorrt_libs_still_does_cuda_part(self, monkeypatch, tmp_path, clean_state):
        packages = _gpu_tree(tmp_path, with_trt=False)
        mocks = _WinMocks()

        def fake_find_spec(name, *a, **k):
            info = packages.get(name)
            if info is None:
                return None
            if info.get("namespace"):
                return SimpleNamespace(origin=None, submodule_search_locations=[str(info["dir"])])
            return SimpleNamespace(origin=str(info["dir"] / "__init__.py"), submodule_search_locations=None)

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
        monkeypatch.setattr(os, "add_dll_directory", mocks.add_dll_directory, raising=False)
        monkeypatch.setattr(ctypes, "WinDLL", mocks.win_dll, raising=False)
        trt_runtime._preload_inference_libs_windows()  # must not raise
        names = [os.path.basename(p) for p in mocks.loaded_dlls]
        assert "cudnn64_9.dll" in names  # CUDA half still done
        assert not any(n.startswith("nvinfer") for n in names)

    def test_linux_entrypoint_never_calls_windows_helper(self, monkeypatch, clean_state):
        assert os.name != "nt"
        calls = []
        monkeypatch.setattr(
            trt_runtime, "_preload_inference_libs_windows", lambda: calls.append(1)
        )
        monkeypatch.setattr(trt_runtime, "_LIBS_PRELOADED", False)
        monkeypatch.setitem(sys.modules, "nvidia", None)
        monkeypatch.setitem(sys.modules, "tensorrt_libs", None)
        trt_runtime.preload_inference_libs()
        assert calls == []

    def test_once_guard_still_holds(self, monkeypatch, clean_state):
        # The once-per-process guard lives in the entrypoint (platform-independent);
        # a second call must be a no-op regardless of platform fork.
        monkeypatch.setattr(trt_runtime, "_LIBS_PRELOADED", False)
        monkeypatch.setitem(sys.modules, "nvidia", None)
        monkeypatch.setitem(sys.modules, "tensorrt_libs", None)
        trt_runtime.preload_inference_libs()
        assert trt_runtime._LIBS_PRELOADED is True
        calls = []
        monkeypatch.setattr(
            trt_runtime, "_preload_inference_libs_windows", lambda: calls.append(1)
        )
        trt_runtime.preload_inference_libs()  # guarded: no work at all
        assert calls == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
