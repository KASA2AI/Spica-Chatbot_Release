import multiprocessing
import os
import subprocess
import sys
import time

import pytest

from spica.adapters.windows_home_camera import parse_devices


def test_directshow_preserves_exact_identity_and_excludes_audio_and_untyped_devices():
    devices = parse_devices('''[dshow] "Same camera" (video)
[dshow]   Alternative name "@device_pnp_first"
[dshow] "Microphone" (audio)
[dshow]   Alternative name "@device_cm_audio"
[dshow] "Same camera" (video)
[dshow]   Alternative name "@device_pnp_second"
[dshow] "Virtual" (none)
[dshow]   Alternative name "@device_sw_virtual"
''')
    assert devices == [{'id': '@device_pnp_first', 'name': 'Same camera'},
                       {'id': '@device_pnp_second', 'name': 'Same camera'}]


def test_missing_stable_camera_never_falls_back_to_name_or_default(monkeypatch):
    from spica.adapters import windows_home_camera as module
    from spica.config.home import HomeConfig

    monkeypatch.setattr(module, 'list_devices', lambda: [{'id':'@device_pnp_other','name':'Same camera'}])
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('must not acquire another camera'))
    with pytest.raises(RuntimeError, match='missing'):
        module.WindowsCameraCapture(HomeConfig(camera_device='@device_pnp_missing'))


def test_fixed_driver_rate_can_feed_lower_requested_rate_without_changing_geometry():
    from spica.adapters.windows_home_camera import select_capture_rate
    options = ('vcodec=mjpeg  min s=1920x1080 fps=30 max s=1920x1080 fps=60.0002\n'
               'vcodec=mjpeg  min s=3840x2160 fps=14 max s=3840x2160 fps=14')
    assert select_capture_rate(options, 1920, 1080, 15) == 30
    with pytest.raises(RuntimeError, match='no supported'):
        select_capture_rate(options, 3840, 2160, 15)
    with pytest.raises(RuntimeError, match='no supported'):
        select_capture_rate(options, 1280, 720, 15)


def _camera_tree(queue):
    from spica.adapters.windows_home_camera import contain_camera_worker
    contain_camera_worker()
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                             creationflags=subprocess.CREATE_NO_WINDOW)
    queue.put(child.pid)
    time.sleep(60)


@pytest.mark.skipif(os.name != 'nt', reason='native Windows process containment')
def test_force_stopping_home_worker_also_stops_its_native_child():
    import win32api
    import win32con
    import win32event
    import win32process

    context = multiprocessing.get_context('spawn')
    queue = context.Queue()
    worker = context.Process(target=_camera_tree, args=(queue,))
    child = None
    worker.start()
    try:
        child = win32api.OpenProcess(win32con.SYNCHRONIZE | win32con.PROCESS_TERMINATE,
                                     False, queue.get(timeout=10))
        worker.terminate()
        worker.join(5)
        assert not worker.is_alive()
        assert win32event.WaitForSingleObject(child, 3000) == win32event.WAIT_OBJECT_0
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        if child is not None:
            if win32event.WaitForSingleObject(child, 0) == win32event.WAIT_TIMEOUT:
                win32process.TerminateProcess(child, 1)
            child.Close()
        worker.close()
        queue.close()
        queue.join_thread()
