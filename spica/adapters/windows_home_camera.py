"""DirectShow capture by stable device moniker inside the existing Home worker.

FFmpeg is already a desktop dependency. Using its exact alternative device name
avoids persisting an enumeration index or selecting a different identically named
camera. The worker joins a kill-on-close Job before starting any native child.
"""
from collections import deque
import multiprocessing
import re
import shutil
import subprocess
import threading

_WORKER_JOB = None


def contain_camera_worker():
    global _WORKER_JOB
    if _WORKER_JOB is not None:
        return
    if multiprocessing.parent_process() is None:
        raise RuntimeError("Windows capture requires the isolated Home camera worker")
    import win32api
    import win32job

    job = win32job.CreateJobObject(None, '')
    try:
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
    except BaseException:
        job.Close()
        raise
    # Deliberately retained for the worker's lifetime. A forced worker exit also
    # closes this last handle and terminates FFmpeg; there is no orphan window.
    _WORKER_JOB = job


def parse_devices(text):
    result, name, seen = [], None, set()
    for line in text.splitlines():
        device = re.search(r'\] "([^"]+)" \((video|audio|none)\)', line)
        if device:
            name = device[1] if device[2] == 'video' else None
            continue
        alternative = re.search(r'Alternative name "([^"]+)"', line)
        if name is not None and alternative and alternative[1].startswith('@device_'):
            identity = alternative[1]
            if identity.casefold() not in seen:
                result.append({'id': identity, 'name': name})
                seen.add(identity.casefold())
            name = None
    return result


def list_devices(*, run=subprocess.run):
    executable = shutil.which('ffmpeg')
    if not executable:
        return []
    # DirectShow enumeration conventionally exits nonzero after listing devices.
    result = run([executable, '-hide_banner', '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy'],
        capture_output=True, encoding='utf-8', errors='replace', timeout=5,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
    return parse_devices(result.stderr)


def select_capture_rate(options, width, height, requested):
    rates = []
    pattern = r'vcodec=mjpeg\s+min s=(\d+)x(\d+) fps=([\d.]+) max s=(\d+)x(\d+) fps=([\d.]+)'
    for match in re.finditer(pattern, options):
        low_w, low_h, low_rate, high_w, high_h, high_rate = match.groups()
        if (int(low_w), int(low_h), int(high_w), int(high_h)) != (width, height, width, height):
            continue
        rate = max(float(low_rate), requested)
        if rate <= float(high_rate):
            rates.append(rate)
    if not rates:
        raise RuntimeError('configured camera size/frame rate has no supported MJPEG capture mode')
    return min(rates)


class WindowsCameraCapture:
    def __init__(self, config):
        self.width, self.height = config.camera_width, config.camera_height
        candidates = [item for item in list_devices()
                      if item['id'].casefold() == config.camera_device.casefold()]
        if len(candidates) != 1:
            raise RuntimeError('configured Windows camera is missing or ambiguous')
        executable = shutil.which('ffmpeg')
        source = 'video=' + candidates[0]['id']
        options = subprocess.run([executable, '-hide_banner', '-list_options', 'true', '-f', 'dshow', '-i', source],
            capture_output=True, encoding='utf-8', errors='replace', timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        capture_rate = select_capture_rate(options.stderr, self.width, self.height, config.camera_fps)
        command = [executable, '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-rtbufsize', '32M', '-f', 'dshow', '-video_size', f'{self.width}x{self.height}',
            '-framerate', f'{capture_rate:g}', '-vcodec', 'mjpeg',
            '-i', source, '-an', '-vf', f'fps={config.camera_fps}',
            '-pix_fmt', 'bgr24', '-f', 'rawvideo', 'pipe:1']
        self._process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0, creationflags=subprocess.CREATE_NO_WINDOW)
        self._errors = deque(maxlen=8)
        self._reader = threading.Thread(target=self._drain_errors, name='home-camera-diagnostics', daemon=True)
        self._reader.start()

    def _drain_errors(self):
        while chunk := self._process.stderr.read(4096):
            self._errors.append(chunk)

    @property
    def error(self):
        return b''.join(self._errors).decode('utf-8', errors='replace')[-2048:]

    def isOpened(self):
        return self._process.poll() is None

    def read(self):
        import numpy as np

        size = self.width * self.height * 3
        data = bytearray(size)
        offset = 0
        while offset < size:
            read = self._process.stdout.readinto(memoryview(data)[offset:])
            if not read:
                return False, None
            offset += read
        return True, np.frombuffer(data, dtype=np.uint8).reshape(self.height, self.width, 3)

    def request_stop(self):
        if self._process.poll() is None:
            self._process.terminate()

    def release(self):
        self.request_stop()
        try:
            self._process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=1)
        self._reader.join(1)
        self._process.stdout.close()
        self._process.stderr.close()
