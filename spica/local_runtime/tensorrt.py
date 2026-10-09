"""Fixed-shape TensorRT engines owned by the calling worker, with no CPU fallback.

Engine preparation is explicit and offline; inference never invokes a builder.
The existing manifest key binds the cache to weights, shape, precision and the
actual GPU/CUDA/TensorRT versions. Native stalls remain inside the Home process.
"""
import hashlib
from pathlib import Path
import platform
import tempfile

from spica.local_runtime.manifest import ModelManifestEntry, engine_cache_key

_LOGGER = None


def _logger(trt):
    # TensorRT retains the first logger process-wide, beyond an individual engine.
    global _LOGGER
    if _LOGGER is None:
        _LOGGER = trt.Logger(trt.Logger.WARNING)
    return _LOGGER


def _checked(result):
    status, *values = result
    if int(status):
        raise RuntimeError(f"Home CUDA operation failed: {status}")
    return values[0] if len(values) == 1 else values


def _engine_path(model, shape, precision, trt, cuda):
    _checked(cuda.cudaSetDevice(0))
    props = _checked(cuda.cudaGetDeviceProperties(0))
    info = {"os_name": platform.system(),
            "gpu_arch": f"{props.name!s}/sm_{props.major}{props.minor}",
            "tensorrt_version": trt.__version__,
            "cuda_version": _checked(cuda.cudaRuntimeGetVersion())}
    entry = ModelManifestEntry(model_id=model.stem+"-trt1", source=str(model),
        onnx=str(model), precision=precision, checksum=hashlib.sha256(model.read_bytes()).hexdigest(),
        dynamic_shapes={"input": list(shape)})
    return model.parent / "engines" / (engine_cache_key(entry, info)+".engine")


def build_engine(model, shape, *, precision="fp32"):
    """Build a local engine explicitly, limiting workspace to 1 GiB."""
    import tensorrt as trt
    from cuda.bindings import runtime as cuda

    model = Path(model)
    if precision not in ("fp32", "fp16"):
        raise ValueError("Home supports FP32 or FP16 TensorRT engines")
    target = _engine_path(model, shape, precision, trt, cuda)
    if target.exists():
        return target
    logger = _logger(trt)
    with trt.Builder(logger) as builder, builder.create_network(0) as network:
        with trt.OnnxParser(network, logger) as parser:
            if not parser.parse_from_file(str(model)):
                errors = "; ".join(str(parser.get_error(i)) for i in range(parser.num_errors))
                raise RuntimeError(f"Home ONNX parse failed: {errors}")
            if network.num_inputs != 1:
                raise ValueError("Home engines require one image input")
            network.get_input(0).shape = tuple(shape)
            with builder.create_builder_config() as config:
                config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 1 << 30)
                config.clear_flag(trt.BuilderFlag.TF32)
                if precision == "fp16":
                    config.set_flag(trt.BuilderFlag.FP16)
                serialized = builder.build_serialized_network(network, config)
                if serialized is None:
                    raise RuntimeError(f"Home TensorRT build failed: {model.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
                        temporary = Path(output.name)
                        output.write(bytes(serialized))
                    temporary.replace(target)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
    return target


class TensorRTEngine:
    """One CUDA stream and fixed buffers; close also covers partial construction."""
    def __init__(self, model, shape, *, precision="fp32"):
        import numpy as np
        import tensorrt as trt
        from cuda.bindings import runtime as cuda

        self.np, self.cuda = np, cuda
        self._runtime = self._engine = self._context = self._stream = None
        self._buffers = {}
        self._closed = False
        self._logger = _logger(trt)
        self.input_name = ""
        self.output_names = []
        self.path = _engine_path(Path(model), shape, precision, trt, cuda)
        self.precision = precision
        if not self.path.is_file():
            raise FileNotFoundError("Home TensorRT engine missing; run scripts/setup_home_models.py")
        try:
            self._runtime = trt.Runtime(self._logger)
            self._engine = self._runtime.deserialize_cuda_engine(self.path.read_bytes())
            if self._engine is None:
                raise RuntimeError(f"Home TensorRT engine unavailable: {self.path.name}")
            self._context = self._engine.create_execution_context()
            if self._context is None:
                raise RuntimeError("Home TensorRT context unavailable")
            self._stream = _checked(cuda.cudaStreamCreate())
            for i in range(self._engine.num_io_tensors):
                name = self._engine.get_tensor_name(i)
                tensor_shape = tuple(self._engine.get_tensor_shape(name))
                if any(d <= 0 for d in tensor_shape):
                    raise ValueError("Home TensorRT engine must have fixed shapes")
                host = np.empty(tensor_shape, dtype=trt.nptype(self._engine.get_tensor_dtype(name)))
                pointer = _checked(cuda.cudaMalloc(host.nbytes))
                self._buffers[name] = (host, pointer)
                if not self._context.set_tensor_address(name, int(pointer)):
                    raise RuntimeError("Home TensorRT could not bind an image buffer")
                if self._engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT:
                    if self.input_name:
                        raise ValueError("Home TensorRT expects one input")
                    self.input_name = name
                    if tensor_shape != tuple(shape):
                        raise ValueError("Home TensorRT input geometry differs")
                else:
                    self.output_names.append(name)
            if not self.input_name or not self.output_names:
                raise ValueError("Home TensorRT engine has no image input/output")
        except BaseException:
            self.close()
            raise

    def infer(self, blob):
        if self._closed:
            raise RuntimeError("Home TensorRT engine is closed")
        np, cuda = self.np, self.cuda
        host, pointer = self._buffers[self.input_name]
        if blob.shape != host.shape or blob.dtype != host.dtype:
            raise ValueError("Home TensorRT image shape or dtype differs")
        np.copyto(host, blob)
        _checked(cuda.cudaMemcpy(pointer, host.ctypes.data, host.nbytes,
                                 cuda.cudaMemcpyKind.cudaMemcpyHostToDevice))
        if not self._context.execute_async_v3(stream_handle=int(self._stream)):
            raise RuntimeError("Home TensorRT inference failed")
        _checked(cuda.cudaStreamSynchronize(self._stream))
        outputs = {}
        for name in self.output_names:
            host, pointer = self._buffers[name]
            _checked(cuda.cudaMemcpy(host.ctypes.data, pointer, host.nbytes,
                                     cuda.cudaMemcpyKind.cudaMemcpyDeviceToHost))
            if not np.isfinite(host).all():
                raise ValueError("Home TensorRT returned non-finite observations")
            outputs[name] = host
        return outputs  # Buffers stay valid until this engine's next inference.

    def close(self):
        if self._closed:
            return
        self._closed = True
        errors = []
        if self._stream is not None:
            try:
                _checked(self.cuda.cudaStreamSynchronize(self._stream))
            except Exception as exc:
                errors.append(exc)
        self._context = self._engine = self._runtime = None
        for _, pointer in self._buffers.values():
            try:
                _checked(self.cuda.cudaFree(pointer))
            except Exception as exc:
                errors.append(exc)
        self._buffers.clear()
        if self._stream is not None:
            try:
                _checked(self.cuda.cudaStreamDestroy(self._stream))
            except Exception as exc:
                errors.append(exc)
            self._stream = None
        if errors:
            raise RuntimeError("Home CUDA cleanup failed") from errors[0]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
