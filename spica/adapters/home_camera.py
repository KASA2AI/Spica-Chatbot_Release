"""One demand-driven camera process with bounded native-library shutdown."""
import hashlib
import multiprocessing
from pathlib import Path
import pickle
import signal
import sys
import threading
import time

from spica.home.models import VisionObservation


def _platform(value=None):
    from spica.config.schema import fold_platform
    return value or fold_platform('auto', sys.platform)


def list_camera_devices(effective_platform=None):
    """Enumerate stable capture identities without acquiring the camera."""
    effective_platform = _platform(effective_platform)
    if effective_platform == 'windows':
        from spica.adapters.windows_home_camera import list_devices
        return list_devices()
    if effective_platform != 'linux':
        return []
    result, seen = [], set()
    candidates = [*sorted(Path('/dev/v4l/by-id').glob('*')),
                  *sorted(Path('/dev/v4l/by-path').glob('*')), *sorted(Path('/dev').glob('video*'))]
    for path in candidates:
        try:
            resolved = path.resolve(strict=True)
            if resolved in seen:
                continue
            seen.add(resolved)
            label_file = Path('/sys/class/video4linux') / resolved.name / 'name'
            label = label_file.read_text().strip() if label_file.is_file() else resolved.name
            result.append({'id': str(path), 'name': label})
        except OSError:
            continue
    return result


def camera_device_present(device, effective_platform=None):
    if not device:
        return False
    if _platform(effective_platform) == 'windows':
        return any(item['id'].casefold() == device.casefold() for item in list_camera_devices('windows'))
    return Path(device).exists()


class _FrameMailbox:
    """One bounded shared slot; a dead writer can never block the reader.

    multiprocessing.Queue.get_nowait can block on a partially written pipe
    packet after a worker crash. The reader here never waits for a writer's
    lock, and the whole slot is discarded when its process is reaped.
    """
    def __init__(self, context, capacity=512 * 1024):
        self.buffer = context.RawArray("B", capacity)
        self.length = context.RawValue("i", 0)
        self.lock = context.Lock()

    def put(self, packet):
        data = pickle.dumps(packet, protocol=5)
        if len(data) > len(self.buffer):
            raise ValueError("Home preview exceeds the bounded mailbox")
        if not self.lock.acquire(timeout=.05):
            return
        try:
            self.length.value = 0
            memoryview(self.buffer).cast("B")[:len(data)] = data
            self.length.value = len(data)
        finally:
            self.lock.release()

    def take(self):
        if not self.lock.acquire(False):
            return None
        try:
            length = self.length.value
            if not length:
                return None
            data = bytes(memoryview(self.buffer).cast("B")[:length])
            self.length.value = 0
        finally:
            self.lock.release()
        return pickle.loads(data)


def _capture(config, profile, output, stop, preview, raw_preview=False, capture_enabled=None, models_ready=None,
             effective_platform=None):
    # Spawn keeps native libraries and capture threads outside the shared core.
    from multiprocessing.connection import wait
    parent = multiprocessing.parent_process()
    if parent is not None:
        def parent_exited():
            wait([parent.sentinel])
            signal.raise_signal(signal.SIGTERM)
        threading.Thread(target=parent_exited, name="home-parent-lifetime", daemon=True).start()
    import cv2
    from spica.adapters.config_platform import current_platform_capabilities

    effective_platform = _platform(effective_platform)

    cap = vision = None
    device_lock = None
    reader = None
    reader_error = [None]
    try:
        if effective_platform == 'windows':
            from spica.adapters.windows_home_camera import contain_camera_worker
            contain_camera_worker()
        directory = Path(config.data_directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        device_lock = (directory / "camera.lock").open("a+b")
        if not current_platform_capabilities().file_lock.try_acquire(device_lock.fileno()):
            raise RuntimeError('camera_already_owned')
        if not raw_preview:
            from spica.adapters.home_vision import HomeVision
            vision = HomeVision(config, profile, enable_faces=True if preview else None)
        if models_ready is not None:
            models_ready.set()
        while capture_enabled is not None and not capture_enabled.is_set():
            if stop.wait(.1):
                return
        if effective_platform == 'windows':
            from spica.adapters.windows_home_camera import WindowsCameraCapture
            cap = WindowsCameraCapture(config)
        elif effective_platform == 'linux':
            cap = cv2.VideoCapture(config.camera_device, cv2.CAP_V4L2)
        else:
            raise RuntimeError('camera_platform_unsupported')
        if not cap.isOpened():
            raise RuntimeError("camera_open_failed")
        if effective_platform == 'linux':
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.camera_width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.camera_height)
            cap.set(cv2.CAP_PROP_FPS, config.camera_fps)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        current, mutex = [None], threading.Lock()

        def read_frames():
            sequence, previous = 0, None
            while not stop.is_set():
                ok, frame = cap.read()
                captured_mono, captured_at = time.monotonic(), time.time()
                if not ok:
                    if not stop.is_set() and not cap.isOpened():
                        raise RuntimeError(getattr(cap, 'error', '') or 'camera_disconnected')
                    stop.wait(.05)
                    continue
                digest = hashlib.blake2s(frame).digest()
                if digest == previous:
                    # Exact repeated buffers are not new evidence or a heartbeat.
                    continue
                previous = digest
                sequence += 1
                with mutex:
                    current[0] = (sequence, frame, captured_at, captured_mono)

        def read():
            try:
                read_frames()
            except Exception as exc:
                reader_error[0] = exc
                stop.set()

        reader = threading.Thread(target=read, name="home-frame-reader", daemon=True)
        reader.start()
        last_sequence = -1
        while not stop.wait(config.inference_interval_seconds):
            with mutex:
                packet = current[0]
            if packet is None or packet[0] == last_sequence:
                continue
            sequence, frame, captured_at, captured_mono = packet
            last_sequence = sequence
            if time.monotonic()-captured_mono > config.frame_ttl_seconds:
                continue
            if raw_preview:
                observation = VisionObservation(config.camera_device, sequence, captured_at, captured_mono)
                embedding = None
            else:
                observation, embedding = vision.infer(frame, sequence, captured_at, captured_mono,
                                                       registration=preview)
            image = None
            if raw_preview:
                encoded, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
                if encoded:
                    image = jpg.tobytes()
            elif preview:
                shown = frame.copy()
                h, w = shown.shape[:2]
                for person in observation.people:
                    x, y, bw, bh = person.box
                    color = (0, 220, 0) if person.identity == "owner" else (0, 180, 255)
                    cv2.rectangle(shown, (int(x*w), int(y*h)), (int((x+bw)*w), int((y+bh)*h)), color, 2)
                    cv2.circle(shown, (int((x+bw/2)*w), int((y+bh*.35)*h)), 7, color, -1)
                shown = cv2.resize(shown, (960, int(960*h/w)))
                encoded, jpg = cv2.imencode(".jpg", shown, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if encoded:
                    image = jpg.tobytes()
            output.put((observation, image, embedding, (frame.shape[1], frame.shape[0])))
        if reader_error[0] is not None:
            raise reader_error[0]
    except Exception as exc:
        output.put((VisionObservation(config.camera_device, 0, time.time(), time.monotonic(),
                                           error=f"{type(exc).__name__}: {exc}"), None, None, None))
    finally:
        stop.set()
        try:
            if vision is not None:
                vision.close()
        finally:
            if cap is not None and effective_platform == 'windows':
                cap.request_stop()
            if reader is not None:
                reader.join(.5)
            # Never release VideoCapture concurrently with a blocked native read.
            # The parent terminates this process if the driver did not unblock.
            if reader is not None and reader.is_alive():
                reader.join()
            try:
                if cap is not None:
                    cap.release()
            finally:
                if device_lock is not None:
                    device_lock.close()


class HomeCamera:
    def __init__(self, config, profile, *, preview=False, raw_preview=False, context=None, clock=time.monotonic,
                 effective_platform=None):
        self.config, self.profile, self.preview = config, profile, preview
        self.raw_preview = raw_preview
        self.effective_platform = _platform(effective_platform)
        self._context = context or multiprocessing.get_context("spawn")
        self._clock = clock
        self.reasons = set()
        self.generation = 0
        self._process = self._output = self._stop = None
        self._last_frame = self._retry_at = 0
        self._closed = False
        self._prepare_models = False
        self._capture_enabled = self._models_ready = None
        self.error = ""

    @property
    def running(self):
        return self._process is not None and self._process.is_alive()

    def require(self, reason, needed):
        if self._closed:
            return
        if needed:
            self.reasons.add(reason)
        else:
            self.reasons.discard(reason)

    def prepare_models(self, needed):
        self._prepare_models = bool(needed) and not self._closed

    def readiness(self):
        from spica.adapters.home_vision import model_specs
        missing = [name for name, _, _ in model_specs(self.config)
                   if not (Path(self.config.model_directory)/name).is_file()]
        present = camera_device_present(self.config.camera_device, self.effective_platform)
        return dict(device_present=present,
                    calibrated=self.profile is not None, missing_models=missing, error=self.error,
                    models_ready=bool(self.running and self._models_ready is not None and self._models_ready.is_set()),
                    capturing=self.capturing)

    def reset_after_resume(self):
        self._release()
        self._retry_at = 0

    def configure_preview(self, config, profile, *, preview=False, raw_preview=False):
        """Switch modes only after the sole capture process has been reaped."""
        if self._closed:
            raise RuntimeError('Home 相机已停止')
        self._release()
        self.config, self.profile = config, profile
        self.preview, self.raw_preview = preview, raw_preview
        self.reasons.clear()
        self._prepare_models = False
        self._retry_at = 0
        self.error = ''

    @property
    def capturing(self):
        return self.running and self._capture_enabled is not None and self._capture_enabled.is_set()

    def poll(self):
        if self._closed:
            return None
        now = self._clock()
        if not self.reasons and (not self._prepare_models
                or self._capture_enabled is not None and self._capture_enabled.is_set()):
            self._release()
        if not self.reasons and not self._prepare_models:
            return None
        if self._process is None and now >= self._retry_at:
            capacity = max(512 * 1024, self.config.camera_width*self.config.camera_height*3+65536) if self.raw_preview else 512 * 1024
            self._output = _FrameMailbox(self._context, capacity)
            self._stop = self._context.Event()
            self._capture_enabled, self._models_ready = self._context.Event(), self._context.Event()
            self._process = self._context.Process(target=_capture,
                args=(self.config, self.profile, self._output, self._stop, self.preview,
                      self.raw_preview, self._capture_enabled, self._models_ready, self.effective_platform),
                name="spica-home-camera", daemon=True)
            try:
                self._process.start()
            except Exception:
                self._release()
                raise
            self.error = ''
            self.generation += 1
            self._last_frame = now
        if self._process is None:
            return None
        if self.reasons and not self._capture_enabled.is_set():
            self._capture_enabled.set()
            self._last_frame = now
        packet = self._output.take()
        if packet is not None:
            self._last_frame = packet[0].captured_mono
            self.error = packet[0].error
        waiting_models_only = not self._capture_enabled.is_set() and self._models_ready.is_set()
        if not self.running or not waiting_models_only and now-self._last_frame > self.config.camera_watchdog_seconds:
            self.error = self.error or "camera_no_fresh_frames"
            self._release()
            self._retry_at = now + 5
            # Once failure is known, even the worker's last successful packet
            # must not extend owner identity or authorize a desk-visit action.
            packet = (VisionObservation(self.config.camera_device, 0, time.time(), now,
                      error=self.error), None, None, None)
        return packet

    def _release(self):
        process = self._process
        if process is None:
            return
        self._stop.set()
        if process.pid is not None:
            process.join(.8)
            if process.is_alive():
                process.terminate()
                process.join(.8)
            if process.is_alive():
                process.kill()
                process.join(.8)
            if process.is_alive():
                raise RuntimeError("Home camera process could not be reaped")
        process.close()
        self._process = self._output = self._stop = None
        self._capture_enabled = self._models_ready = None

    def close(self):
        self._closed = True
        self.reasons.clear()
        self._release()
