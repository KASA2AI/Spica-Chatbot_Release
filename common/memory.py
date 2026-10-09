"""Return unused native allocation buffers without unloading live models."""

import sys


def release_native_buffers() -> None:
    if sys.platform != 'linux':
        return
    try:
        import ctypes
        trim = ctypes.CDLL(None).malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        trim(0)
    except (AttributeError, OSError):
        pass  # Optional on non-glibc allocators; never a readiness failure.
