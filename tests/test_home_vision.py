"""Home GPU migration: preserve geometry and fail closed with bounded owners."""
from types import SimpleNamespace

import numpy as np
import pytest

from spica.adapters.home_vision import HomeVision
from spica.config.home import HomeConfig
from spica.home.perception import RoomLocator, DeskVisit
from spica.home.models import Person
from spica.home.profile import HomeProfile
from spica.local_runtime.tensorrt import TensorRTEngine


def test_person_only_yolo26_unpads_boxes_without_hiding_overlapping_people():
    import cv2
    vision = HomeVision.__new__(HomeVision)
    vision.cv, vision.np, vision.config = cv2, np, HomeConfig()
    output = np.zeros((1, 100, 6), np.float32)
    output[0, 0] = (100, 200, 300, 450, .9, 0)
    output[0, 1] = (110, 210, 310, 450, .8, 0)
    received = []
    def infer(blob):
        received.append(blob)
        return {"output0": output}
    vision.people = SimpleNamespace(infer=infer)
    frame = np.full((1080, 1920, 3), (10, 20, 30), dtype=np.uint8)
    boxes = vision._people(frame)
    assert len(boxes) == 2
    assert boxes[0].box == pytest.approx((300/1920, 180/1080, 600/1920, 750/1080))
    np.testing.assert_allclose(received[0][0, :, 0, 0], np.full(3, 114/255), rtol=1e-6)
    np.testing.assert_allclose(received[0][0, :, 140, 0], [30/255, 20/255, 10/255], rtol=1e-6)
    output[0, 0, 5] = 1
    with pytest.raises(ValueError, match="only the person"):
        vision._people(frame)


def test_low_score_real_detections_continue_tracks_without_creating_people():
    import cv2

    config = HomeConfig(camera_device='camera')
    profile = HomeProfile(camera_device='camera', image_size=(1920, 1080),
                          desk=(.5, 0, 1, 1), bed=(0, 0, .4, 1))
    vision = HomeVision.__new__(HomeVision)
    vision.cv, vision.np, vision.config, vision.profile = cv2, np, config, profile
    vision.faces, vision.face_error = None, ''
    vision._appearance = lambda *_: np.eye(1, 512, dtype=np.float32).tobytes()
    predictions = np.zeros((1, 100, 6), np.float32)
    vision.people = SimpleNamespace(infer=lambda _: {'output0': predictions})
    image = np.full((1080, 1920, 3), 100, np.uint8)
    locator = RoomLocator(config, profile)

    def observe(at, score, x=100, *, duplicate=False):
        predictions.fill(0)
        predictions[0, 0] = (x, 200, x+100, 400, score, 0)
        if duplicate:
            predictions[0, 1] = (x, 200, x+101, 400, .4, 0)
        frame, _ = vision.infer(image, int(at*10)+1, 1000+at, at)
        room = locator.observe(frame, at)
        return room

    # An unassociated weak desk candidate must not create H1 occupancy.
    unseen = observe(0, .4, x=400)
    assert not unseen.people and unseen.desk_occupied is None
    for at in np.arange(.5, 6.5, .5):
        room = observe(float(at), .9)
    identity = room.people[0].track_id
    assert identity is not None
    # A weak duplicate must not make the strong, already matched body ambiguous.
    assert [p.track_id for p in observe(6.5, .9, duplicate=True).people] == [identity]
    # Keep the existing low-score continuity fix for H1 and preview tracking;
    # H2's region decision no longer depends on maintaining this identity.
    for at in np.arange(7, 10.5, .5):
        room = observe(float(at), .4)
        assert [p.track_id for p in room.people] == [identity]
        assert room.people[0].location == 'bed'
    observe(10.5, .9, x=200)
    observe(11, .9, x=300)
    for at in np.arange(11.5, 16.5, .5):
        room = observe(float(at), .9, x=370)
    assert room.people[0].track_id == identity and room.people[0].location == 'desk'


def test_weak_duplicate_cannot_bypass_a_strong_persons_conflicting_face():
    import cv2
    config = HomeConfig(camera_device='camera')
    owner = np.eye(1, 128, dtype=np.float32).reshape(-1)
    profile = HomeProfile(camera_device='camera', image_size=(1920, 1080),
        desk=(.5, 0, 1, 1), bed=(0, 0, .4, 1), embeddings=[owner.tolist()]*3)
    vision = HomeVision.__new__(HomeVision)
    vision.cv, vision.np, vision.config, vision.profile = cv2, np, config, profile
    vision.enrolled = np.asarray(profile.embeddings)
    vision.faces, vision.face_error = object(), ''
    vision._faces = lambda _: np.array([[425, 240, 90, 100]+[0.]*10+[.99]], np.float32)
    vision._align_crop = lambda *_: np.zeros((112, 112, 3), np.uint8)
    face = [owner]
    vision.recognizer = SimpleNamespace(infer=lambda _: {'feature': face[0]})
    body = np.eye(1, 512, dtype=np.float32).reshape(-1)
    vision._appearance = lambda *_: body.tobytes()
    vision._people = lambda _: [Person((.2, .2, .2, .6), confidence=.9)]
    image = np.full((1080, 1920, 3), 100, np.uint8)
    locator = RoomLocator(config, profile)
    frame, _ = vision.infer(image, 1, 1001, 1)
    original = locator.observe(frame, 1).people[0]
    assert original.identity == 'owner'
    face[0] = np.roll(owner, 1)
    body[0], body[1] = .8, .6
    vision._people = lambda _: [Person((.2, .2, .2, .6), confidence=.9),
                               Person((.201, .2, .2, .6), confidence=.4)]
    frame, _ = vision.infer(image, 2, 1001.5, 1.5)
    result = locator.observe(frame, 1.5)
    assert original.track_id not in [person.track_id for person in result.people]
    assert [person.identity for person in result.people] == ['other']
    assert frame.people[1].association_error


def test_failed_gpu_model_load_releases_previous_engines_and_never_falls_back(monkeypatch):
    from spica.local_runtime import tensorrt
    opened, closed = [], []
    def engine(model, shape, **kwargs):
        if len(opened) == 1:
            raise RuntimeError("GPU unavailable")
        opened.append(model.name)
        return SimpleNamespace(close=lambda: closed.append(model.name))
    monkeypatch.setattr(tensorrt, "TensorRTEngine", engine)
    with pytest.raises(RuntimeError, match="GPU unavailable"):
        HomeVision(HomeConfig())
    assert closed == list(reversed(opened))


def test_cuda_cleanup_attempts_all_allocations_even_when_one_free_fails():
    freed, streams = [], []
    def free(pointer):
        freed.append(pointer)
        return (1 if pointer == 11 else 0,)
    engine = TensorRTEngine.__new__(TensorRTEngine)
    engine.cuda = SimpleNamespace(cudaStreamSynchronize=lambda stream: (0,), cudaFree=free,
        cudaStreamDestroy=lambda stream: streams.append(stream) or (0,))
    engine._closed, engine._stream = False, 7
    engine._context = engine._engine = engine._runtime = object()
    engine._buffers = {"input": (None, 11), "output": (None, 12)}
    with pytest.raises(RuntimeError, match="CUDA cleanup failed"):
        engine.close()
    assert freed == [11, 12] and streams == [7]
    assert engine._context is engine._engine is engine._runtime is None
    engine.close()
    assert freed == [11, 12]


@pytest.mark.parametrize("second_face_x", [1330, 100])
def test_ambiguous_faces_do_not_block_region_occupancy(second_face_x):
    import cv2
    vision = HomeVision.__new__(HomeVision)
    vision.cv, vision.np, vision.config = cv2, np, HomeConfig()
    feature = np.array([1.]+[0.]*127, dtype=np.float32)
    vision.profile = HomeProfile(camera_device="camera", image_size=(1920, 1080),
        desk=(.5, 0, 1, 1), bed=(0, 0, .4, 1), embeddings=[feature.tolist()]*3)
    vision.enrolled = np.asarray(vision.profile.embeddings)
    vision.faces, vision.face_error = object(), ""
    vision._appearance = lambda *_: np.eye(1, 512, dtype=np.float32).tobytes()
    vision._people = lambda _: [Person((.6, .2, .2, .6))]
    faces = np.array([[1200, 250, 100, 120]+[0.]*10+[.99],
                      [second_face_x, 260, 100, 120]+[0.]*10+[.99]], dtype=np.float32)
    vision._align_crop = lambda *_: np.zeros((112, 112, 3), np.uint8)
    vision.recognizer = SimpleNamespace(infer=lambda _: {"feature": feature})
    locator, visit = RoomLocator(vision.config, vision.profile), DeskVisit(vision.config)
    frame = np.full((1080, 1920, 3), 127, np.uint8)
    locations, claims, ids = [], [], []
    for sequence, at in enumerate((1., 1.5, 2.)):
        vision._faces = lambda _, first=sequence == 0: faces[:1] if first else faces
        observation, _ = vision.infer(frame, sequence+1, 1000+at, at)
        owner = locator.observe(observation, at)
        locations.append(owner.desk_occupied)
        ids.append(owner.people[0].track_id)
        claims.append(visit.observe(owner, at))
    assert locations == [True, True, True]
    assert claims == [False, False, True]
    if second_face_x == 1330:
        assert ids[1:] == [None, None]
    else:
        assert ids == [ids[0]]*3


def test_face_models_are_optional_and_partial_failure_releases_only_their_engines(monkeypatch):
    from spica.local_runtime import tensorrt
    opened, closed = [], []
    def engine(model, shape, **kwargs):
        if 'sface' in model.name:
            raise FileNotFoundError('optional SFace missing')
        opened.append(model.name)
        return SimpleNamespace(close=lambda: closed.append(model.name))
    monkeypatch.setattr(tensorrt, 'TensorRTEngine', engine)
    with HomeVision(HomeConfig()) as vision:
        assert len(opened) == 2 and vision.faces is None
    opened.clear(); closed.clear()
    vision = HomeVision(HomeConfig(), enable_faces=True)
    assert vision.face_error and vision.faces is None
    assert closed == ['face_detection_yunet_2023mar.onnx']
    assert len(opened) == 3
    vision.close()
    assert opened[0] in closed and opened[1] in closed


def test_optional_face_inference_failure_keeps_body_regions_and_features():
    import cv2
    vision = HomeVision.__new__(HomeVision)
    vision.cv, vision.np, vision.config = cv2, np, HomeConfig()
    vision.profile, vision.faces, vision.face_error = None, object(), ''
    vision._people = lambda _: [Person((.6,.2,.2,.6))]
    feature = np.eye(1,512,dtype=np.float32).tobytes()
    vision._appearance = lambda *_: feature
    def failed(*_): raise RuntimeError('face GPU inference failed')
    vision._faces = failed
    result, enrollment = vision.infer(np.full((1080,1920,3),127,np.uint8),1,1000,1)
    assert not result.error and result.face_error
    assert result.people[0].appearance == feature and result.people[0].identity == 'unknown'
    assert enrollment is None
