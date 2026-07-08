"""W4-b Windows GPU preload: injectable unit tests (run on Linux, everything
Windows-only is mocked).

Pins the design validated by the W4-b §6.3 real-machine smoke
(docs/windows_w4b_smoke.md):
- ORT's CUDA/TRT EPs resolve the cuDNN / nvinfer DLLs via the standard
  LoadLibrary search, which does NOT include wheel dirs -> the preload must
  register candidate dirs (os.add_dll_directory) AND force-load the DLLs
  (ctypes.WinDLL), with handles AND DLL objects kept alive at module level;
- **cuDNN 9 is a dispatcher (cudnn64_9.dll) + backend DLLs it dlopens on demand;
  that internal load ignores add_dll_directory, so the WHOLE family must be
  force-loaded -- loading only the dispatcher aborts the EP with 0xC0000409.**
  The `test_..._loads_whole_cudnn_family_...` cases are the regression guard for
  exactly the bug the smoke caught (a plain unit mock alone missed it, so the
  guard asserts the CONTRACT: every present family member is force-loaded);
- package dirs are located via importlib.util.find_spec WITHOUT importing
  (never `import torch` just to fix a search path);
- best-effort: a missing / failing member is skipped, never raises;
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

CUDNN_FAMILY = backend._WIN_CUDNN_DLLS  # dispatcher + 7 backends; the two modules agree
NVINFER_DLLS = trt_runtime._WIN_TRT_DLLS


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


def _gpu_tree(tmp_path, *, cudnn_in=("torch",), cudnn_family=True, with_trt=True):
    """Fake site-packages: torch/lib + ctranslate2 + nvidia/*/bin + tensorrt_libs.
    ``cudnn_family`` writes the whole dispatcher+backend family into each cuDNN
    dir (default) vs. just the dispatcher (for the dir-fallthrough case)."""
    def _write_cudnn(directory):
        names = CUDNN_FAMILY if cudnn_family else ("cudnn64_9.dll",)
        for name in names:
            (directory / name).write_bytes(b"x")

    packages = {}
    torch_dir = tmp_path / "torch"
    (torch_dir / "lib").mkdir(parents=True)
    (torch_dir / "__init__.py").write_text("")
    if "torch" in cudnn_in:
        _write_cudnn(torch_dir / "lib")
    packages["torch"] = {"dir": torch_dir}

    ct2_dir = tmp_path / "ctranslate2"
    ct2_dir.mkdir()
    (ct2_dir / "__init__.py").write_text("")
    if "ct2" in cudnn_in:
        _write_cudnn(ct2_dir)
    packages["ctranslate2"] = {"dir": ct2_dir}

    nvidia_root = tmp_path / "nvidia"
    cudnn_bin = nvidia_root / "cudnn" / "bin"
    cudnn_bin.mkdir(parents=True)
    if "nvidia" in cudnn_in:
        _write_cudnn(cudnn_bin)
    packages["nvidia"] = {"dir": nvidia_root, "namespace": True}

    if with_trt:
        trt_dir = tmp_path / "tensorrt_libs"
        trt_dir.mkdir()
        (trt_dir / "__init__.py").write_text("")
        for name in NVINFER_DLLS:
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

    def loaded_names(self):
        return [os.path.basename(p) for p in self.loaded_dlls]


def _fake_find_spec(packages):
    def fake_find_spec(name, *args, **kwargs):
        info = packages.get(name)
        if info is None:
            return None
        if info.get("namespace"):
            return SimpleNamespace(origin=None, submodule_search_locations=[str(info["dir"])])
        return SimpleNamespace(
            origin=str(info["dir"] / "__init__.py"), submodule_search_locations=None
        )
    return fake_find_spec


@pytest.fixture
def win(monkeypatch, tmp_path, clean_state):
    """Wire the fake gpu tree + Windows API mocks; returns (mocks, packages)."""
    packages = _gpu_tree(tmp_path)
    mocks = _WinMocks()
    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec(packages))
    monkeypatch.setattr(os, "add_dll_directory", mocks.add_dll_directory, raising=False)
    monkeypatch.setattr(ctypes, "WinDLL", mocks.win_dll, raising=False)
    return mocks, packages


def _wire(monkeypatch, packages, mocks):
    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec(packages))
    monkeypatch.setattr(os, "add_dll_directory", mocks.add_dll_directory, raising=False)
    monkeypatch.setattr(ctypes, "WinDLL", mocks.win_dll, raising=False)


class TestBackendCudaPreloadWindows:
    def test_registers_candidate_dirs(self, win, tmp_path):
        mocks, _ = win
        backend._preload_cuda_libraries_windows()
        registered = [os.path.normpath(p) for p in mocks.registered_dirs]
        assert os.path.normpath(str(tmp_path / "nvidia" / "cudnn" / "bin")) in registered
        assert os.path.normpath(str(tmp_path / "torch" / "lib")) in registered
        assert os.path.normpath(str(tmp_path / "ctranslate2")) in registered

    def test_loads_whole_cudnn_family_not_just_dispatcher(self, win):
        # THE regression guard (W4-b §6.3): loading only cudnn64_9.dll aborted the
        # EP with 0xC0000409 -- every present family member must go resident.
        mocks, _ = win
        backend._preload_cuda_libraries_windows()
        loaded = mocks.loaded_names()
        for member in CUDNN_FAMILY:
            assert member in loaded, f"{member} was not force-loaded (dispatcher-only regression?)"
        # exactly the family, each once (from the first dir carrying the dispatcher)
        assert sorted(loaded) == sorted(CUDNN_FAMILY)

    def test_handles_and_dll_objects_kept_alive_at_module_level(self, win):
        backend._preload_cuda_libraries_windows()
        assert backend._WIN_DLL_DIR_HANDLES
        assert all(h.kind == "dll-dir-handle" for h in backend._WIN_DLL_DIR_HANDLES)
        assert len(backend._WIN_LOADED_DLLS) == len(CUDNN_FAMILY)
        assert all(d.kind == "loaded-dll" for d in backend._WIN_LOADED_DLLS)

    def test_partial_family_member_failure_is_best_effort(self, monkeypatch, tmp_path, clean_state):
        # One backend DLL won't load -> the rest still go resident (never raise).
        packages = _gpu_tree(tmp_path)
        mocks = _WinMocks(fail_dlls={"cudnn_ops64_9.dll"})
        _wire(monkeypatch, packages, mocks)
        backend._preload_cuda_libraries_windows()
        loaded = mocks.loaded_names()
        assert "cudnn_ops64_9.dll" not in loaded
        assert "cudnn64_9.dll" in loaded and "cudnn_graph64_9.dll" in loaded
        assert len(loaded) == len(CUDNN_FAMILY) - 1

    def test_missing_packages_are_silent(self, monkeypatch, clean_state):
        monkeypatch.setattr(importlib.util, "find_spec", lambda *a, **k: None)
        mocks = _WinMocks()
        monkeypatch.setattr(os, "add_dll_directory", mocks.add_dll_directory, raising=False)
        monkeypatch.setattr(ctypes, "WinDLL", mocks.win_dll, raising=False)
        backend._preload_cuda_libraries_windows()  # must not raise
        assert mocks.registered_dirs == []
        assert mocks.loaded_dlls == []

    def test_dir_without_dispatcher_falls_through(self, monkeypatch, tmp_path, clean_state):
        # torch dispatcher fails to load; the dir carries only the dispatcher, so
        # nothing loads there and the ct2 copy (dispatcher-only) wins next.
        packages = _gpu_tree(tmp_path, cudnn_in=("torch", "ct2"), cudnn_family=False)
        mocks = _WinMocks(fail_dlls={"cudnn64_9.dll"})
        # only the torch copy should fail; make ct2's copy loadable by path check
        torch_copy = str(tmp_path / "torch" / "lib" / "cudnn64_9.dll")

        def flaky(path):
            if path == torch_copy:
                raise OSError("bad torch copy")
            mocks.loaded_dlls.append(path)
            return SimpleNamespace(kind="loaded-dll", path=path)

        monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec(packages))
        monkeypatch.setattr(os, "add_dll_directory", mocks.add_dll_directory, raising=False)
        monkeypatch.setattr(ctypes, "WinDLL", flaky, raising=False)
        backend._preload_cuda_libraries_windows()
        assert mocks.loaded_dlls == [str(tmp_path / "ctranslate2" / "cudnn64_9.dll")]

    def test_linux_entrypoint_never_calls_windows_helper(self, monkeypatch, clean_state):
        assert os.name != "nt", "this pin is meaningful on the Linux dev machine only"
        calls = []
        monkeypatch.setattr(backend, "_preload_cuda_libraries_windows", lambda: calls.append(1))
        monkeypatch.setattr(backend, "_CUDA_PRELOADED", False)
        monkeypatch.setitem(sys.modules, "nvidia", None)  # Linux route inert
        backend._preload_cuda_libraries()
        assert calls == []  # the fork never routed a posix host into the nt helper


class TestTrtRuntimePreloadWindows:
    def test_loads_whole_cudnn_family_then_all_nvinfer(self, win, tmp_path):
        mocks, _ = win
        trt_runtime._preload_inference_libs_windows()
        names = mocks.loaded_names()
        for member in CUDNN_FAMILY:
            assert member in names, f"{member} not force-loaded (dispatcher-only regression?)"
        for dll in NVINFER_DLLS:
            assert dll in names
        # ORDER: the whole cuDNN family resident BEFORE nvinfer (nvinfer's deps).
        assert max(names.index(m) for m in CUDNN_FAMILY) < names.index("nvinfer_10.dll")
        registered = [os.path.normpath(p) for p in mocks.registered_dirs]
        assert os.path.normpath(str(tmp_path / "tensorrt_libs")) in registered

    def test_keep_alive_lists_populated(self, win):
        trt_runtime._preload_inference_libs_windows()
        assert trt_runtime._WIN_DLL_DIR_HANDLES
        assert len(trt_runtime._WIN_LOADED_DLLS) == len(CUDNN_FAMILY) + len(NVINFER_DLLS)

    def test_missing_tensorrt_libs_still_does_cuda_family(self, monkeypatch, tmp_path, clean_state):
        packages = _gpu_tree(tmp_path, with_trt=False)
        mocks = _WinMocks()
        _wire(monkeypatch, packages, mocks)
        trt_runtime._preload_inference_libs_windows()  # must not raise
        names = mocks.loaded_names()
        for member in CUDNN_FAMILY:
            assert member in names  # CUDA half still fully done
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
