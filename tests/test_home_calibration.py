"""Region-only saves and legacy enrollment preservation through the real UI."""
import time

import pytest

from spica.config.home import HomeConfig
from spica.home.models import VisionObservation
from spica.home.profile import load_profile, save_profile
from test_home import Camera, profile


@pytest.mark.parametrize('existing', [False, True])
def test_calibration_saves_regions_without_requiring_or_erasing_faces(tmp_path, monkeypatch, existing):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    ui = pytest.importorskip('ui.home_calibration')
    app = ui.QApplication.instance() or ui.QApplication([])
    camera = Camera()
    monkeypatch.setattr(ui, 'HomeCamera', lambda *a, **k: camera)
    warnings = []
    monkeypatch.setattr(ui.QMessageBox, 'warning', lambda *a: warnings.append(a[-1]))
    if existing:
        save_profile(tmp_path, profile())
    config = HomeConfig(camera_device='camera', data_directory=str(tmp_path))
    window = ui.HomeCalibration(config)
    try:
        window.preview.regions = dict(desk=(.55,0,1,1), bed=(0,0,.4,1))
        if existing:
            window.register()  # Cancelling a new attempt must preserve the old data.
            window.register()
        camera.packet = (VisionObservation('camera',1,time.time(),time.monotonic()), None, None, (1920,1080))
        window.tick()
        window.save()
        assert not warnings
        saved = load_profile(tmp_path, 'camera')
        assert saved.desk == (.55,0,1,1)
        assert saved.embeddings == (profile().embeddings if existing else [])
        assert camera.closed
    finally:
        window.close()
        app.processEvents()


def test_calibration_reports_live_regions_overlap_and_stale_frames(tmp_path, monkeypatch):
    from spica.home.models import Person
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    ui = pytest.importorskip('ui.home_calibration')
    app = ui.QApplication.instance() or ui.QApplication([])
    camera = Camera()
    monkeypatch.setattr(ui, 'HomeCamera', lambda *a, **k: camera)
    window = ui.HomeCalibration(HomeConfig(camera_device='camera', data_directory=str(tmp_path)))
    try:
        window.preview.regions = dict(desk=(.5,0,1,1), bed=(0,0,.4,1))
        observation = VisionObservation('camera', 1, time.time(), time.monotonic(),
                                        people=(Person((.6,.2,.2,.5), .9),))
        camera.packet = (observation, None, None, (1920,1080))
        window.tick()
        assert '桌区有人' in window.status.text()
        window.preview.regions['bed'] = (.5,0,1,1)
        assert '重叠' in window.region_status(observation)
        assert '无法判断' in window.region_status(observation)
        window._last_frame = time.monotonic() - 10
        camera.packet = None
        window.tick()
        assert '无法判断' in window.status.text()
        window.reset_regions()
        assert not window.preview.regions
    finally:
        window.close()
        app.processEvents()


def test_shared_calibration_waits_for_core_save_and_does_not_write_from_ui(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    ui = pytest.importorskip('ui.home_calibration')
    from ui.workers.home_preview_camera import HomePreviewCamera
    app = ui.QApplication.instance() or ui.QApplication([])
    saving, accepted = threading.Event(), threading.Event()
    calls, saved = [], []
    def operation(action, session_id, **options):
        calls.append(action)
        if action == 'save':
            assert options['profile_data']['desk'] == (.5, 0, 1, 1)
            saving.set()
            assert accepted.wait(3)
            return {'saved': True}
        if action == 'end':
            return {'active': False, 'detail': 'Home 已恢复'}
        return {'active': True, 'packet': (VisionObservation('camera', 1, time.time(), time.monotonic()),
                                          None, None, (1920, 1080))}
    config = HomeConfig(camera_device='camera', data_directory=str(tmp_path))
    camera = HomePreviewCamera(SimpleNamespace(home_camera_preview=operation), config)
    window = ui.HomeCalibration(config, camera=camera)
    window.saved.connect(lambda: saved.append(True))
    window.show()
    try:
        deadline = time.monotonic()+3
        while window._size is None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        window.preview.regions = dict(desk=(.5, 0, 1, 1), bed=(0, 0, .4, 1))
        window.save()
        assert saving.wait(2)
        assert not saved and window.isVisible() and not window.save_button.isEnabled()
        assert not (tmp_path / 'profile.json').exists()
        accepted.set()
        while window.isVisible() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        assert saved == [True] and not window.isVisible()
        assert calls[0] == 'begin' and calls[-1] == 'end'
    finally:
        accepted.set()
        camera.close()
        camera.wait(3000)
        window.close()
        app.processEvents()
