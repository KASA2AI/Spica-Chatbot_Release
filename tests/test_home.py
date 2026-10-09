"""H1 behavior: fresh evidence, local people/regions, a single bounded camera."""
from dataclasses import replace
import json
import multiprocessing
import os
import time
from types import SimpleNamespace

import pytest
import numpy as np

from spica.adapters.home_camera import HomeCamera
from spica.adapters.home_display import HomeDisplay
from spica.adapters.home_mqtt import HomeMQTT
from spica.config.home import HomeConfig
from spica.home.models import Person, VisionObservation, SensorObservation, DisplayResult, RoomObservation
from spica.home.perception import RoomLocator, DeskVisit
from spica.home.profile import HomeProfile, load_profile, save_profile
from spica.home.runtime import HomeRuntime


def profile():
    return HomeProfile(camera_device="camera", image_size=(1920, 1080),
        desk=(.5, 0, 1, 1), bed=(0, 0, .4, 1), embeddings=[[1.]+[0.]*127]*3)


def appearance(index=0):
    value = np.zeros(512, np.float32)
    value[index] = 1
    return value.tobytes()


def frame(at=1., *, identity="owner", box=(.6, .2, .2, .6), people=None, sequence=None):
    return VisionObservation("camera", int(at*10) if sequence is None else sequence,
        1000+at, at, (Person(box, identity, appearance=appearance()),) if people is None else people)


def test_calibration_keeps_identity_local_and_rejects_another_camera(tmp_path):
    save_profile(tmp_path, profile())
    assert load_profile(tmp_path, "camera") == profile()
    regional = profile().model_copy(update={"embeddings": []})
    save_profile(tmp_path, regional)
    assert load_profile(tmp_path, "camera").embeddings == []
    with pytest.raises(ValueError, match="camera changed"):
        load_profile(tmp_path, "other-camera")
    with pytest.raises(ValueError):
        HomeProfile.model_validate({**profile().model_dump(), "desk": [0, 0, float("nan"), 1]})


def test_back_facing_people_have_independent_regions_and_continuous_tracks():
    locator = RoomLocator(HomeConfig(), profile())
    people = (Person((.1,.2,.2,.6), appearance=appearance(0)),
              Person((.6,.2,.2,.6), appearance=appearance(1)))
    first = locator.observe(frame(1, people=people), 1)
    assert first.desk_occupied is True
    assert [p.location for p in first.people] == ['bed', 'desk']
    ids = [p.track_id for p in first.people]
    for at in (2, 3, 4, 5, 6):
        current = locator.observe(frame(at, people=people[::-1]), at)
        assert [p.track_id for p in current.people] == ids[::-1]
        assert current.desk_occupied is True
        assert all(p.identity == 'unknown' for p in current.people)


def test_repeated_old_and_future_frames_never_extend_tracks():
    locator = RoomLocator(HomeConfig(), profile())
    first = locator.observe(frame(), 1).people[0].track_id
    assert locator.observe(frame(), 1.1).reason == 'stale_frame'
    assert locator.observe(frame(1.3), 1.3).people[0].track_id != first
    assert locator.observe(frame(2), 5).reason == 'stale_frame'
    assert locator.observe(frame(7), 6).reason == 'stale_frame'


def test_wake_regions_use_raw_detections_with_boundary_and_weak_candidate_uncertainty():
    locator = RoomLocator(HomeConfig(), profile())
    # No embeddings or track ID are required; multiple bed detections are valid.
    bed = Person((.1, .2, .2, .6))
    desk = Person((.6, .2, .2, .6))
    both = locator.observe(frame(1, people=(bed, bed, desk)), 1)
    assert both.bed_occupied is True and both.outside_bed_occupied is True
    assert all(person.track_id is None for person in both.people)
    # The normal H1 classification is unchanged. The H2 bed boundary has an
    # uncertainty band, so small box motion cannot immediately imply leaving.
    edge = Person((.31, .2, .2, .6))  # torso x=.41; bed ends at .40.
    boundary = locator.observe(frame(2, people=(edge,)), 2)
    assert boundary.people[0].location == 'outside'
    assert boundary.bed_occupied is None and boundary.outside_bed_occupied is None
    weak_bed = replace(bed, confidence=.4)
    uncertain = locator.observe(frame(3, people=(weak_bed, desk)), 3)
    assert uncertain.bed_occupied is None and uncertain.outside_bed_occupied is True
    weak_desk = locator.observe(frame(4, people=(replace(desk, confidence=.4),)), 4)
    assert weak_desk.outside_bed_occupied is None
    clear = locator.observe(frame(5, people=(desk,)), 5)
    assert clear.bed_occupied is False and clear.outside_bed_occupied is True
    empty = locator.observe(frame(6, people=()), 6)
    assert empty.reason == 'no_person'
    assert empty.bed_occupied is False and empty.outside_bed_occupied is False
    failed = locator.observe(replace(frame(7), error='camera_failed'), 7)
    assert failed.bed_occupied is None and failed.outside_bed_occupied is None


def test_crossing_ambiguity_does_not_erase_desk_occupancy_or_inherit_a_target():
    locator = RoomLocator(HomeConfig(), profile())
    people = tuple(Person(box, appearance=appearance()) for box in ((.50,.2,.2,.6),(.64,.2,.2,.6)))
    original = locator.observe(frame(1, people=people), 1)
    merged = locator.observe(frame(1.5, people=(Person((.57,.2,.2,.6), appearance=appearance()),)), 1.5)
    assert merged.desk_occupied is True
    assert merged.people[0].track_id is None
    recovered = locator.observe(frame(2, people=people), 2)
    assert [p.track_id for p in recovered.people] == [p.track_id for p in original.people]
    lost = locator.observe(frame(6, people=people), 6)
    assert not set(p.track_id for p in lost.people).intersection(p.track_id for p in original.people)


def test_one_wake_per_visit_unknown_and_reconnect_cannot_rearm():
    config = HomeConfig()
    locator, visit = RoomLocator(config, profile()), DeskVisit(config)
    results = [visit.observe(locator.observe(frame(at), at), at) for at in (1, 1.5, 2, 2.5)]
    assert results == [False, False, True, False]
    from spica.home.models import RoomObservation
    visit.observe(RoomObservation(), 10)
    locator.reset()
    assert not any(visit.observe(locator.observe(frame(at), at), at) for at in (11, 11.5, 12))
    for at in (13, 14, 15):
        visit.observe(locator.observe(frame(at, box=(.1,.2,.2,.6)), at), at)
    assert [visit.observe(locator.observe(frame(at), at), at) for at in (16, 16.5, 17)] == [False, False, True]


class MQTTClient:
    def __init__(self): self.published = []
    def reconnect_delay_set(self, **kwargs): pass
    def subscribe(self, topics): self.topics = topics
    def connect_async(self, *args, **kwargs): pass
    def loop_start(self): pass
    def disconnect(self): pass
    def loop_stop(self): pass
    def is_connected(self): return True
    def publish(self, topic, payload, **kwargs):
        self.published.append((topic, json.loads(payload), kwargs))
        if getattr(self, 'on_publish', None) is not None:
            self.on_publish(topic, json.loads(payload))
        return SimpleNamespace(rc=0)


@pytest.mark.parametrize('boundary', ['live', 'reconnect', 'resume'])
def test_sensor_clock_rollback_recovers_fresh_reports_without_accepting_cached_replay(boundary):
    wall, mono = [9990.], [1000.]
    client = MQTTClient()
    mqtt = HomeMQTT(HomeConfig(), client=client, clock=lambda: mono[0], wall_clock=lambda: wall[0])
    door = '0x403802fffe5a36ce'
    def deliver(topic, payload, retain=False):
        mqtt._on_message(client, None, SimpleNamespace(topic='zigbee2mqtt/'+topic,
            payload=json.dumps(payload).encode(), retain=retain))
    def metadata():
        deliver('bridge/info', {'config': {'devices': {'sensor': {
            'friendly_name': 'room_presence', 'filtered_cache': ['^occupancy$', '^illuminance$']}}}})
        deliver('bridge/devices', [dict(ieee_address=door, friendly_name='door_entry',
            interview_completed=True, definition={'model':'SNZB-04P'})], True)
    mqtt._on_connect(client, None, None, 0, None)
    metadata()
    deliver('room_presence', {'last_seen':wall[0], 'occupancy':False, 'illuminance':40})
    wall[0], mono[0] = 10000., 1010.
    old = {'last_seen':wall[0], 'occupancy':False, 'illuminance':40}
    deliver('room_presence', old)
    deliver('door_entry/home/contact', dict(ieee_address=door, contact=True, observed_at=wall[0]))
    assert {name for name, _ in mqtt.drain()} >= {'presence', 'illuminance', 'door_event'}
    wall[0] = 6400.
    if boundary == 'reconnect':
        mqtt._on_disconnect()
        mqtt._on_connect(client, None, None, 0, None)
        metadata()
    elif boundary == 'resume':
        mqtt.invalidate_observations()
    deliver('room_presence', old, True)
    deliver('room_presence', dict(last_seen=wall[0], occupancy=False, illuminance=40))  # Cache barrier.
    assert not any(name in {'presence', 'illuminance', 'door_event'} for name, _ in mqtt.drain())
    wall[0], mono[0] = 6460., 1070.
    new = dict(last_seen=wall[0], occupancy=True, illuminance=5)
    deliver('room_presence', new)
    opened = dict(ieee_address=door, contact=False, observed_at=wall[0])
    deliver('door_entry/home/contact', opened)
    values = {name: observation.value for name, observation in mqtt.drain()
              if name in {'presence', 'illuminance', 'door_event'}}
    assert values == {'presence':True, 'illuminance':5, 'door_event':False}
    deliver('room_presence', old)  # Pre-correction future timestamp.
    deliver('room_presence', new)  # Duplicate.
    deliver('door_entry/home/contact', opened, True)
    deliver('door_entry/home/contact', opened)
    assert not mqtt.drain()
    mqtt.close()


def test_light_finger_is_bound_and_uses_full_travel_hardware_return_not_toggle():
    from spica.config.home import HomeWakeConfig
    client = MQTTClient()
    device = '0xa4c1380f3cc5969d'
    reported_at = [time.time()]
    mqtt = HomeMQTT(HomeConfig(wake=HomeWakeConfig(light_on_device=device)), client=client,
                    wall_clock=lambda: reported_at[0])
    def deliver(topic, value):
        # Each synthetic device report is new even on Windows Python versions
        # whose wall clock can return the same value for adjacent calls.
        reported_at[0] += .001
        mqtt._on_message(client, None, SimpleNamespace(topic='zigbee2mqtt/'+topic,
            payload=json.dumps(dict(value, last_seen=reported_at[0]) if isinstance(value, dict) else value).encode(), retain=False))
    claims = []
    assert mqtt.press_light(lambda: claims.append(True)) == 'not_ready'
    deliver('bridge/devices', [{'ieee_address':device, 'friendly_name':device,
        'interview_completed':True, 'definition':{'model':'TS0001_fingerbot'}}])
    assert client.published[-1][0] == 'zigbee2mqtt/'+device+'/get'
    deliver('bridge/state', {'state':'online'})
    assert mqtt.configure_light_press() == 'configuration_requested'
    assert client.published[-1][1] == {'mode':'click', 'upper':0, 'lower':100}
    deliver(device, {'mode':'switch', 'upper':0, 'lower':100})
    assert mqtt.light_status()['on'] == 'not_ready'
    deliver(device, {'mode':'click', 'upper':0, 'lower':100, 'reverse':'ON', 'state':'ON'})
    client.on_publish = lambda topic, payload: deliver(device, {'state':'ON'}) if topic.endswith('/get') else None
    client.published.clear()
    deliver('bridge/state', {'state':'offline'})
    assert mqtt.press_light(lambda: claims.append(True)) == 'not_ready'
    assert not claims and not client.published
    deliver('bridge/state', {'state':'online'})
    def claimed():
        assert not any(topic.endswith('/set') for topic, _, _ in client.published)
        if not claims:
            claims.append(True)
    assert mqtt.press_light(claimed) == 'requested'
    assert claims == [True]
    assert client.published == [
        ('zigbee2mqtt/'+device+'/get', {'state':''}, {'qos':0,'retain':False}),
        ('zigbee2mqtt/'+device+'/set', {'state':'ON'}, {'qos':0,'retain':False}),
    ]
    mqtt.close()


def test_mqtt_only_new_timestamped_sensor_reports_and_reconnect_barrier():
    clock = [1000.]
    client = MQTTClient()
    mqtt = HomeMQTT(HomeConfig(), client=client, clock=lambda: clock[0], wall_clock=lambda: clock[0])
    mqtt._on_connect(client, None, None, 0, None)
    assert mqtt.drain() == [("connection", "connected")]
    info = SimpleNamespace(topic="zigbee2mqtt/bridge/info", retain=True, payload=json.dumps({
        "config": {"devices": {"ieee": {"friendly_name": "room_presence",
                                          "filtered_cache": ["^occupancy$"]}}}}).encode())
    mqtt._on_message(client, None, info)
    mqtt.drain()
    def report(stamp, *, retain=False, value=True):
        payload = {"occupancy": value, "last_seen": stamp}
        mqtt._on_message(client, None, SimpleNamespace(topic="zigbee2mqtt/room_presence",
                          retain=retain, payload=json.dumps(payload).encode()))
    for stamp, retained in ((1000, True), (999, False), (None, False), (1001, False)):
        report(stamp, retain=retained)
    assert mqtt.drain() == []
    clock[0] = 1002
    report(1001)
    event = mqtt.drain()[0][1]
    assert event.value is True and event.valid_until == 1181
    report(1001)
    report(1002, value="true")
    assert mqtt.drain() == []
    mqtt._on_disconnect()
    mqtt._on_connect(client, None, None, 0, None)
    mqtt.drain()
    mqtt._on_message(client, None, info)
    mqtt.drain()
    report(1001.5)
    assert mqtt.drain() == []
    clock[0] = 1003
    report(1003, value=False)
    assert mqtt.drain()[0][1].value is False


def test_aggregate_last_seen_cannot_refresh_cached_occupancy_without_filter():
    client = MQTTClient()
    mqtt = HomeMQTT(HomeConfig(), client=client, clock=lambda: 1002, wall_clock=lambda: 1002)
    mqtt._on_connect(client, None, None, 0, None)
    mqtt.drain()
    message = SimpleNamespace(topic="zigbee2mqtt/room_presence", retain=False,
        payload=b'{"occupancy":true,"last_seen":1002,"illumination":"bright"}')
    mqtt._on_message(client, None, message)
    assert mqtt.drain() == []
    info = SimpleNamespace(topic="zigbee2mqtt/bridge/info", retain=True, payload=json.dumps({
        "config": {"devices": {"ieee": {"friendly_name": "room_presence",
                                          "filtered_cache": ["^occupancy$"]}}}}).encode())
    mqtt._on_message(client, None, info)
    mqtt.drain()
    mqtt._on_message(client, None, message)  # transition publication may contain old cache
    assert mqtt.drain() == []
    message.payload = b'{"last_seen":1002,"illumination":"bright"}'
    mqtt._on_message(client, None, message)
    assert mqtt.drain() == []
    message.payload = b'{"last_seen":1002,"occupancy":true}'
    mqtt._on_message(client, None, message)
    assert mqtt.drain()[0][1].value is True


def test_first_real_occupancy_is_refreshed_after_cache_barrier_without_another_entry():
    now, client = [1000.], MQTTClient()
    mqtt = HomeMQTT(HomeConfig(), client=client, clock=lambda: now[0], wall_clock=lambda: now[0])
    mqtt._on_connect(client, None, None, 0, None)
    mqtt.drain()
    mqtt._on_message(client, None, SimpleNamespace(topic="zigbee2mqtt/bridge/info", retain=True,
        payload=b'{"config":{"devices":{"id":{"friendly_name":"room_presence","filtered_cache":["^occupancy$"]}}}}'))
    mqtt.drain()
    assert len(client.published) == 1
    now[0] = 1001
    mqtt._on_message(client, None, SimpleNamespace(topic="zigbee2mqtt/room_presence", retain=False,
        payload=b'{"last_seen":1001,"occupancy":true}'))
    assert mqtt.drain() == []
    now[0] = 1002
    mqtt.drain()
    assert len(client.published) == 2
    assert client.published[-1] == ("zigbee2mqtt/room_presence/set",
        {"read": {"cluster": "msOccupancySensing", "attributes": ["occupancy"]}}, {"qos": 0, "retain": False})
    mqtt._on_message(client, None, SimpleNamespace(topic="zigbee2mqtt/room_presence", retain=False,
        payload=b'{"last_seen":1002,"occupancy":true}'))
    assert mqtt.drain()[0][1].value is True
    now[0] = 1062
    mqtt.drain()
    assert len(client.published) == 3


def test_first_uncached_door_notification_survives_reconnect_without_a_closed_baseline():
    now, client = [1000.], MQTTClient()
    mqtt = HomeMQTT(HomeConfig(), client=client, clock=lambda: now[0], wall_clock=lambda: now[0])
    mqtt._on_connect(client, None, None, 0, None)
    def deliver(topic, payload, retain=False):
        mqtt._on_message(client, None, SimpleNamespace(topic='zigbee2mqtt/'+topic,
            payload=json.dumps(payload).encode(), retain=retain))
    door = '0x403802fffe5a36ce'
    deliver('bridge/devices', [dict(ieee_address=door, friendly_name='door_entry',
        interview_completed=True, definition={'model':'SNZB-04P'})], True)
    mqtt.drain()
    # Aggregated cache and retained event replays cannot invent an opening.
    event = dict(ieee_address=door, contact=False, observed_at=1001)
    now[0] = 1001
    deliver('another_door/home/contact', event)
    deliver('door_entry/home/contact', dict(event, ieee_address='0x0000000000000022'))
    deliver('door_entry', dict(contact=False, last_seen=1001))
    deliver('door_entry/home/contact', event, True)
    assert not mqtt.drain()
    deliver('door_entry/home/contact', event)
    events = mqtt.drain()
    assert len(events) == 1 and events[0][0] == 'door_event'
    assert events[0][1].value is False
    deliver('door_entry/home/contact', event)
    deliver('door_entry/home/contact', dict(event, observed_at=1002))  # Future.
    assert not mqtt.drain()
    mqtt._on_disconnect()
    mqtt._on_connect(client, None, None, 0, None)
    mqtt.drain()
    now[0] = 1002
    deliver('door_entry/home/contact', dict(event, observed_at=1002))
    assert not mqtt.drain()  # Binding must be rebuilt after reconnect.
    deliver('bridge/devices', [dict(ieee_address=door, friendly_name='door_entry',
        interview_completed=True, definition={'model':'SNZB-04P'})], True)
    deliver('door_entry/home/contact', dict(event, observed_at=1002))
    assert mqtt.drain()[0][0] == 'door_event'
    mqtt.close()


class Camera:
    def __init__(self):
        self.reasons, self.generation, self.error = set(), 0, ""
        self.packet = None
        self.closed = False
    @property
    def running(self): return bool(self.reasons)
    def require(self, name, needed):
        self.reasons.add(name) if needed else self.reasons.discard(name)
    def poll(self):
        packet, self.packet = self.packet, None
        return packet
    def close(self): self.closed = True; self.reasons.clear()


class MQTTEvents:
    def __init__(self): self.events = []
    def press_light(self, claim):
        claim()
        return 'requested'
    def drain(self):
        events, self.events = self.events, []
        return events
    def close(self): pass


def test_camera_preview_is_not_home_evidence_and_reloads_saved_regions(tmp_path, monkeypatch):
    config = HomeConfig(enabled=True, camera_device='camera', data_directory=str(tmp_path))
    camera, mqtt, now, packets, actions = HomeCamera(config, profile()), MQTTEvents(), [1.], [], []
    monkeypatch.setattr(camera, 'poll', lambda: packets.pop(0) if packets else None)
    runtime = HomeRuntime(config, mqtt, camera,
        SimpleNamespace(wake=lambda: actions.append(True) or DisplayResult('wake_requested')),
        profile(), clock=lambda: now[0])
    try:
        runtime.begin_camera_preview('desktop', 'one', camera_settings={})
        assert camera.preview and camera.reasons == {'settings_preview'}
        mqtt.events = [('presence', SensorObservation('room', True, 1001, 1, 181))]
        for at in (1, 1.5, 2):
            now[0] = at
            packets.append((frame(at), b'preview', None, (1920, 1080)))
            runtime.step()
        snapshot = runtime.snapshot()
        assert not actions and snapshot['room_occupied'] is True
        assert snapshot['vision'] is None and snapshot['room']['desk_occupied'] is None
        assert not runtime.scene_allowed()
        assert runtime.poll_camera_preview('other', 'one')['active'] is False
        assert runtime.poll_camera_preview('desktop', 'one')['packet'][1] == b'preview'
        assert runtime.poll_camera_preview('desktop', 'one')['packet'] is None
        updated = profile().model_copy(update={'desk': (0, 0, .4, 1), 'bed': (.5, 0, 1, 1)})
        assert runtime.save_camera_preview('desktop', 'one', updated.model_dump())['saved']
        runtime.end_camera_preview('desktop', 'one')
        assert camera.profile == runtime.locator.profile == updated
        assert not camera.preview and config.enabled is True  # Configuration is not rewritten.
        assert runtime.scene_allowed() and runtime.snapshot()['vision'] is None
        now[0] = 2.5
        packets.append((frame(2.5), None, None, (1920, 1080)))
        runtime.step()
        assert runtime.snapshot()['room']['bed_occupied'] is True and not actions
        runtime.begin_camera_preview('desktop', 'two', camera_settings={})
        with pytest.raises(RuntimeError, match='会话已结束'):
            runtime.save_camera_preview('desktop', 'one', profile().model_dump())
        assert load_profile(tmp_path, 'camera') == updated
        with pytest.raises(ValueError, match='有效新画面'):
            runtime.save_camera_preview('desktop', 'two', profile().model_dump())
        runtime.end_camera_preview('desktop', 'one')
        assert runtime.poll_camera_preview('desktop', 'two')['active'] is True
    finally:
        runtime.close()


def test_camera_preview_yields_to_alarm_preparation(tmp_path, monkeypatch):
    from test_home_alarms import setup
    env = setup(tmp_path)
    env.clock.advance(-600)
    env.config.camera_device = 'camera'
    env.config.data_directory = str(tmp_path)
    camera = HomeCamera(env.config, profile())
    monkeypatch.setattr(camera, 'poll', lambda: None)
    runtime = HomeRuntime(env.config, MQTTEvents(), camera, None, profile(), alarms=env.alarms,
                          clock=lambda: env.clock.mono, wall_clock=lambda: env.clock.at)
    try:
        runtime.begin_camera_preview('desktop', 'one', camera_settings={}, raw_preview=True)
        assert camera.raw_preview
        env.clock.advance(600)
        assert env.alarms.preparation_required()
        runtime.step()
        assert not camera.raw_preview and not camera.preview
        assert 'settings_preview' not in camera.reasons
        assert runtime.poll_camera_preview('desktop', 'one')['active'] is False
        with pytest.raises(RuntimeError, match='叫醒'):
            runtime.begin_camera_preview('desktop', 'two', camera_settings={})
    finally:
        runtime.close()


def test_preview_does_not_switch_modes_until_native_capture_is_reaped(tmp_path):
    from unittest.mock import Mock
    config = HomeConfig(data_directory=str(tmp_path))
    camera = HomeCamera(config, profile())
    stuck = Mock(pid=123)
    stuck.is_alive.return_value = True
    camera._process, camera._stop = stuck, Mock()
    with pytest.raises(RuntimeError, match='reaped'):
        camera.configure_preview(config, None, raw_preview=True)
    assert camera._process is stuck and not camera.raw_preview
    stuck.is_alive.return_value = False
    camera.configure_preview(config, None, raw_preview=True)
    assert camera._process is None and camera.raw_preview
    camera.close()


def test_first_step_after_actual_suspend_invalidates_old_presence_before_camera_demand(tmp_path):
    from test_home_alarms import setup, current
    from test_home_bedtime import power, settle
    from spica.home.bedtime import HomeBedtime
    env = setup(tmp_path)
    env.clock.advance(-600)
    env.config.wake.power_control_enabled = True
    env.config.wake.suspend_margin_seconds = 10
    adapter, _, _ = power(tmp_path)
    adapter.clock = lambda: env.clock.at
    bedtime = env.alarms.bedtime = HomeBedtime(env.config, env.store, adapter,
        clock=lambda: env.clock.mono, wall_clock=lambda: env.clock.at)
    with env.alarms.conversation('sleep', 'owner', want_audio=False):
        env.alarms.prepare_bedtime(reply_required=False)
    settle(bedtime, {'suspend_requested'})
    calls, camera, mqtt = [], Camera(), MQTTEvents()
    camera.reset_after_resume = lambda: calls.append('camera-reset')
    mqtt.invalidate_observations = lambda: calls.append('mqtt-reset')
    original_poll = camera.poll
    camera.poll = lambda: calls.append('poll') or original_poll()
    runtime = HomeRuntime(env.config, mqtt, camera, None, profile(), alarms=env.alarms,
        clock=lambda: env.clock.mono, wall_clock=lambda: env.clock.at)
    runtime._inputs.consume([('presence', SensorObservation('presence', True,
        env.clock.at, env.clock.mono, env.clock.mono+180))], env.clock.mono)
    env.clock.at = current(env)['wake_at']  # Suspend advances wall time, not monotonic time.
    adapter.suspend_count.write_text('1')
    try:
        runtime.step()
        assert calls == ['mqtt-reset', 'camera-reset', 'poll']
        assert not camera.reasons and runtime.snapshot()['room_occupied'] is None
        assert bedtime.resume_generation == 1 and bedtime.suppress_daily
    finally:
        runtime.close()


def test_power_observation_failure_does_not_shut_down_daily_home_sensing(tmp_path):
    from test_home_alarms import setup
    env = setup(tmp_path)
    def failed_read():
        raise OSError('resume counter temporarily unavailable')
    env.alarms.bedtime = SimpleNamespace(resume_generation=0, step=failed_read, intent=None,
        suppress_daily=False, suppress_alarm=False, cancel=lambda **kwargs:None, close=lambda:None)
    camera, mqtt = Camera(), MQTTEvents()
    mqtt.events = [('presence', SensorObservation('room', True, env.clock.at, env.clock.mono, env.clock.mono+180))]
    runtime = HomeRuntime(env.config, mqtt, camera, None, profile(), alarms=env.alarms,
        clock=lambda:env.clock.mono, wall_clock=lambda:env.clock.at)
    try:
        runtime.step()
        assert 'resume counter' in env.alarms.error
        assert camera.reasons == {'occupancy'} and not camera.closed
    finally:
        runtime.close()


def test_kernel_resume_waits_for_driver_restore_before_camera_and_alarm_work(tmp_path):
    from test_home_alarms import setup
    from test_home_bedtime import power
    env = setup(tmp_path)
    adapter, logind, _ = power(tmp_path)
    camera, mqtt, calls = Camera(), MQTTEvents(), []
    camera.reset_after_resume = lambda: calls.append('camera-reset')
    camera.poll = lambda: calls.append('camera-poll')
    mqtt.invalidate_observations = lambda: calls.append('mqtt-reset')
    env.alarms.prepare_environment = lambda: calls.append('alarm-prepare')
    runtime = HomeRuntime(env.config, mqtt, camera, None, profile(), power=adapter,
        alarms=env.alarms, clock=lambda:env.clock.mono, wall_clock=lambda:env.clock.at)
    try:
        runtime.step()
        calls.clear()
        runtime._inputs.consume([('presence', SensorObservation('room', True,
            env.clock.at, env.clock.mono, env.clock.mono+180))], env.clock.mono)
        runtime._vision = frame(env.clock.mono)
        adapter.suspend_count.write_text('1')
        logind.preparing_sleep = True
        # Live 09-18: the kernel counter advanced about 17 s before the
        # NVIDIA post-sleep work finished. Never kill/reopen the camera then.
        for _ in range(3):
            runtime.step()
            assert calls == []
            assert runtime.snapshot()['vision'] is None
            assert runtime.snapshot()['room_occupied'] is None
        logind.preparing_sleep = False
        runtime.step()
        assert calls == ['mqtt-reset', 'camera-reset', 'alarm-prepare', 'camera-poll']
        assert runtime.snapshot()['resume_observation_error'] == ''
        calls.clear()
        runtime.step()
        assert calls == ['alarm-prepare', 'camera-poll']
    finally:
        runtime.close()


def test_h1_without_wake_alarms_discards_old_mailbox_and_camera_demand_on_resume():
    now, wall, count, actions = [1000.], [2000.], [0], []
    camera, mqtt = Camera(), MQTTEvents()
    mqtt.invalidate_observations = lambda:mqtt.events.clear()
    camera.reset_after_resume = lambda:setattr(camera, 'packet', None)
    runtime = HomeRuntime(HomeConfig(), mqtt, camera,
        SimpleNamespace(wake=lambda:actions.append(True) or DisplayResult('wake_requested')), profile(),
        clock=lambda:now[0], wall_clock=lambda:wall[0],
        power=SimpleNamespace(resume_stamp=lambda:dict(suspend_count=count[0]), resume_ready=lambda:True))
    try:
        runtime.step()
        mqtt.events = [('presence', SensorObservation('room', True, wall[0], now[0], now[0]+180))]
        for at in (1000., 1000.5):
            now[0] = at
            camera.packet = (frame(at), None, None, None)
            runtime.step()
        assert camera.reasons == {'occupancy'} and not actions
        wall[0] += 3600
        now[0] += .5
        count[0] += 1
        camera.packet = (frame(now[0]), None, None, None)
        runtime.step()
        assert not actions and not camera.reasons and runtime.snapshot()['room_occupied'] is None
    finally:
        runtime.close()


def test_bedtime_and_home_close_do_not_hold_each_others_locks(tmp_path):
    import threading
    from test_home_alarms import setup
    from test_home_bedtime import power
    from spica.home.bedtime import HomeBedtime
    env = setup(tmp_path)
    camera, mqtt = Camera(), MQTTEvents()
    runtime = HomeRuntime(env.config, mqtt, camera, None, profile(), alarms=env.alarms,
        store=env.store, clock=lambda:env.clock.mono, wall_clock=lambda:env.clock.at)
    entered, release = threading.Event(), threading.Event()
    failures = []
    def light_off():
        entered.set()
        assert release.wait(2)
        return runtime.set_room_light(action='off', _source='bedtime')
    adapter, _, _ = power(tmp_path)
    env.alarms.bedtime = HomeBedtime(env.config, env.store, adapter, light_off=light_off,
        clock=lambda:env.clock.mono, wall_clock=lambda:env.clock.at)
    mqtt.close = release.set
    def sleep_request():
        try:
            with runtime.conversation('sleep', 'owner', user_text='我准备睡觉了', want_audio=False):
                env.alarms.prepare_bedtime(reply_required=False)
        except Exception as exc:
            failures.append(exc)
    def close():
        try:
            runtime.close()
        except Exception as exc:
            failures.append(exc)
    request = threading.Thread(target=sleep_request, daemon=True)
    closing = threading.Thread(target=close, daemon=True)
    request.start()
    assert entered.wait(2)
    closing.start()
    request.join(2)
    closing.join(2)
    release.set()
    assert not request.is_alive() and not closing.is_alive(), 'bedtime and close inverted their locks'
    assert not failures and runtime._resources_done.is_set()


def test_runtime_sensor_camera_desk_then_display_and_expiry():
    config, mqtt, camera, now = HomeConfig(camera_idle_seconds=1), MQTTEvents(), Camera(), [1.]
    actions = []
    display = SimpleNamespace(wake=lambda: actions.append(1) or DisplayResult("wake_requested"))
    runtime = HomeRuntime(config, mqtt, camera, display, profile(), clock=lambda: now[0])
    runtime.step()
    assert not camera.running
    mqtt.events = [("presence", SensorObservation("room", True, 1001, 1, 20))]
    for at in (1, 1.5, 2, 2.5):
        now[0] = at
        camera.packet = (frame(at), None, None, None)
        runtime.step()
    assert actions == [1] and camera.reasons == {"occupancy"}
    published = json.dumps(runtime.snapshot())
    assert "appearance" not in published and "room" in runtime.snapshot()
    mqtt.events = [("connection", "disconnected")]
    now[0] = 3
    runtime.step()
    assert runtime.snapshot()["room_occupied"] is None
    camera.require("alarm", True)  # No H2 schedule; second demand shares the same instance.
    now[0] = 5
    runtime.step()
    assert camera.reasons == {"alarm"}
    assert actions == [1]
    runtime.close()
    assert camera.closed


@pytest.mark.parametrize('trigger', ['presence', 'door_event'])
def test_visual_person_keeps_camera_and_admits_desk_when_sensor_reports_empty(trigger):
    now, mqtt, camera, actions = [1.], MQTTEvents(), Camera(), []
    runtime = HomeRuntime(HomeConfig(camera_idle_seconds=1, door_camera_seconds=1), mqtt, camera,
        SimpleNamespace(wake=lambda: actions.append(now[0]) or DisplayResult('wake_requested')),
        profile(), clock=lambda: now[0])
    mqtt.events = [(trigger, SensorObservation(trigger, trigger == 'presence', 1001, 1, 181))]
    try:
        for at in (1., 1.5, 2., 2.5, 3., 3.5, 4., 4.5, 5., 7.1, 7.6, 8.1):
            now[0] = at
            if at > 1:
                mqtt.events = [('presence', SensorObservation('room', False, 1000+at, at, at+180))]
            box = (.1,.2,.2,.6) if at < 3 else (.6,.2,.2,.6)
            camera.packet = (frame(at, box=box), None, None, None)
            runtime.step()
            assert camera.running, 'Fresh camera people must survive false vacancy and the initial trigger timeout'
        assert actions == [4.], 'Repeated false reports must neither block desk confirmation nor rearm it'
        assert runtime.snapshot()['room_occupied'] is False  # Preserve the raw sensor fact.
        for at in (10.2, 11.3):
            now[0] = at
            runtime.step()
        assert not camera.running, 'Missing fresh people must eventually release the visual camera demand'
    finally:
        runtime.close()


@pytest.mark.parametrize('delay_at', ['preparation', 'camera_poll'])
@pytest.mark.parametrize('fresh_frame', [True, False])
def test_camera_fallback_rechecks_frame_when_sensor_expires_during_step(delay_at, fresh_frame, monkeypatch):
    mqtt, camera, now, actions = MQTTEvents(), Camera(), [179.], []
    runtime = HomeRuntime(HomeConfig(), mqtt, camera,
        SimpleNamespace(wake=lambda: actions.append(now[0]) or DisplayResult('wake_requested')),
        profile(), clock=lambda: now[0])
    mqtt.events = [('presence', SensorObservation('room', True, 1000, 0, 180.1))]
    for at in (179., 179.5):
        now[0] = at
        camera.packet = (frame(at), None, None, None)
        runtime.step()
    runtime.alarms = SimpleNamespace(bedtime=None, refresh_bedtime=lambda: None,
        prepare_environment=lambda: None, camera_required=lambda: True,
        step=lambda *args, **kwargs: None, snapshot=lambda: None, close=lambda: None)
    owner, method = ((runtime.alarms, 'prepare_environment') if delay_at == 'preparation'
                     else (camera, 'poll'))
    original = getattr(owner, method)

    def delayed():
        now[0] = 180.2
        return original()

    try:
        now[0] = 180.
        camera.packet = (frame(180.2 if fresh_frame else 177.), None, None, None)
        with monkeypatch.context() as patch:
            patch.setattr(owner, method, delayed)
            runtime.step()
        assert runtime.snapshot()['room_occupied'] is None
        assert actions == ([180.2] if fresh_frame else []), 'Only a valid new camera frame may finish desk confirmation'
        mqtt.events = [('presence', SensorObservation('room', True, 1180.5, 180.5, 190))]
        for at in (180.5, 181., 181.5):
            now[0] = at
            camera.packet = (frame(at), None, None, None)
            runtime.step()
        assert actions == ([180.2] if fresh_frame else [181.5]), 'Sensor recovery must not repeat a camera-confirmed visit'
    finally:
        runtime.close()


def test_sensor_unknown_cannot_consume_next_valid_visit():
    mqtt, camera, now = MQTTEvents(), Camera(), [1.]
    actions = []
    runtime = HomeRuntime(HomeConfig(), mqtt, camera,
        SimpleNamespace(wake=lambda: actions.append(1) or DisplayResult("already_on")), profile(), clock=lambda: now[0])
    for at in (1, 1.5, 2):
        now[0] = at; camera.packet = (frame(at), None, None, None); runtime.step()
    assert actions == []
    mqtt.events = [("presence", SensorObservation("room", True, 1003, 3, 20))]
    for at in (3, 3.5, 4):
        now[0] = at; camera.packet = (frame(at), None, None, None); runtime.step()
    assert actions == [1]


@pytest.mark.parametrize('suppression', ['disabled', 'bedtime'])
def test_visual_camera_fallback_releases_for_daily_suppression_but_preserves_alarm(suppression):
    now, mqtt, camera = [1.], MQTTEvents(), Camera()
    runtime = HomeRuntime(HomeConfig(), mqtt, camera, None, profile(), clock=lambda: now[0])
    mqtt.events = [('door_event', SensorObservation('door', False, 1001, 1, 181))]
    camera.packet = (frame(1), None, None, None)
    runtime.step()
    assert camera.reasons == {'door', 'occupancy'}
    runtime.alarms = SimpleNamespace(
        bedtime=SimpleNamespace(resume_generation=0, suppress_daily=suppression == 'bedtime'),
        refresh_bedtime=lambda: None, prepare_environment=lambda: None, camera_required=lambda: True,
        step=lambda *args, **kwargs: None, snapshot=lambda: None, close=lambda: None)
    if suppression == 'disabled':
        runtime.config.daily_detection_enabled = False
    try:
        now[0] = 1.5
        camera.packet = (frame(1.5), None, None, None)
        runtime.step()
        assert camera.reasons == {'wake_alarm'}, 'Daily fallback must not acquire or cancel the alarm camera lease'
    finally:
        runtime.close()


def test_new_door_open_starts_bounded_camera_and_desk_can_wake_before_presence():
    mqtt, camera, now, actions = MQTTEvents(), Camera(), [1.], []
    runtime = HomeRuntime(HomeConfig(), mqtt, camera,
        SimpleNamespace(wake=lambda: actions.append(1) or DisplayResult('wake_requested')),
        profile(), clock=lambda: now[0])
    mqtt.events = [('door', SensorObservation('door', True, 1001, 1, 1.25))]
    runtime.step()
    now[0] = 2
    mqtt.events = [('door', SensorObservation('door', False, 1002, 2, 40))]
    runtime.step()
    assert camera.running and not actions
    for at in (2, 2.5, 3):
        now[0] = at
        camera.packet = (frame(at, identity='unknown'), None, None, None)
        runtime.step()
    assert actions == [1] and runtime.snapshot()['room_occupied'] is None
    camera.require('alarm', True)
    now[0] = 6  # No new people; let the normal idle countdown start.
    runtime.step()
    now[0] = 2 + runtime.config.door_camera_seconds
    runtime.step()
    assert camera.reasons == {'alarm'}
    runtime.close()


def test_door_open_level_disable_and_reconnect_do_not_replay_camera_trigger():
    mqtt, camera, now = MQTTEvents(), Camera(), [1.]
    runtime = HomeRuntime(HomeConfig(), mqtt, camera, None, profile(), clock=lambda: now[0])
    def report(contact, at):
        now[0] = at
        mqtt.events = [('door', SensorObservation('door', contact, 1000+at, at, at+40))]
        runtime.step()
    report(False, 1)  # Initial open state is not an observed opening.
    assert not camera.running
    report(True, 2)
    runtime.config.daily_detection_enabled = False
    report(False, 3)
    assert not camera.running
    runtime.config.daily_detection_enabled = True
    report(False, 4)
    assert not camera.running
    report(True, 5)
    report(False, 6)
    assert camera.running
    mqtt.events = [('connection', 'disconnected')]
    runtime.step()
    assert not camera.running
    report(False, 7)
    assert not camera.running
    runtime.close()


def test_real_first_opening_starts_camera_without_triggering_farewell(tmp_path):
    from spica.home.alarm_store import AlarmStore
    mqtt, camera, now, openings = MQTTEvents(), Camera(), [1.], []
    runtime = HomeRuntime(HomeConfig(), mqtt, camera, None, profile(), clock=lambda: now[0],
                          store=AlarmStore(tmp_path/'home.sqlite3'))
    runtime.scenes.departure = openings.append
    for at in (1, 2):
        now[0] = at
        mqtt.events = [('door_event', SensorObservation('door', False, 1000+at, at, at+40))]
        runtime.step()
    assert camera.reasons == {'door'} and openings == []
    runtime.close()


def test_zigbee_extension_only_forwards_actual_notifications_for_supported_doors():
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is needed to exercise the Zigbee2MQTT extension')
    root = Path(__file__).resolve().parents[1]
    subprocess.run([node, '--input-type=module'], cwd=root, check=True, capture_output=True, text=True, input='''
        import assert from 'node:assert/strict';
        import Extension from './scripts/zigbee2mqtt_home_contact.mjs';
        let receive, online = true, removed = false;
        const published = [];
        const mqtt = {isConnected: () => online, publish: (...args) => published.push(args)};
        const bus = {onDeviceMessage: (key, cb) => receive = cb,
                     removeListeners: () => removed = true};
        const extension = new Extension(null, mqtt, null, null, bus);
        extension.start();
        const packet = {device: {ieeeAddr: '0x403802fffe5a36ce', name: 'door_entry',
            definition: {model: 'SNZB-04P'}, interviewed: true},
            cluster: 'ssIasZone', type: 'commandStatusChangeNotification', data: {zonestatus: 1}};
        await receive({...packet, type: 'readResponse'});
        await receive({...packet, cluster: 'genPowerCfg'});
        await receive({...packet, device: {...packet.device, ieeeAddr: 'other'}});
        await receive({...packet, device: {...packet.device, definition: {model: 'another-model'}}});
        assert.equal(published.length, 0);
        await receive(packet);
        assert.equal(published[0][0], 'door_entry/home/contact');
        assert.equal(JSON.parse(published[0][1]).contact, false);
        assert.deepEqual(published[0][2], {clientOptions: {qos: 0, retain: false}});
        await receive({...packet, data: {zonestatus: 0}});
        assert.equal(JSON.parse(published[1][1]).contact, true);
        // Another installation's device must work without editing this extension.
        await receive({...packet, device: {...packet.device, ieeeAddr: '0x0000000000000011', name: 'my_door'}});
        assert.equal(published[2][0], 'my_door/home/contact');
        assert.equal(JSON.parse(published[2][1]).ieee_address, '0x0000000000000011');
        online = false;
        await receive(packet);
        extension.stop();
        online = true;
        await receive(packet);
        assert.equal(published.length, 3);
        assert.ok(removed);
    ''')


def xrandr_result(*, other=False, name="27M1 Max"):
    edid = bytearray(128)
    edid[54:72] = b"\0\0\0\xfc\0" + name.encode().ljust(13, b" ")
    blocks = "\n".join("\t\t"+edid[i:i+16].hex() for i in range(0, 128, 16))
    return "DP-2 connected primary 5120x2880+0+0\n\tEDID:\n" + blocks + "\n" + (
        "HDMI-0 connected 1920x1080+5120+0\n" if other else "")


@pytest.mark.parametrize("state,other,name,expected,commands", [
    ("On", False, "27M1 Max", "already_on", 2),
    ("Off", False, "27M1 Max", "wake_requested", 3),
    ("Off", True, "27M1 Max", "unavailable", 1),
    ("Off", False, "Other", "unavailable", 1),
    ("Unknown", False, "27M1 Max", "unavailable", 2),
])
@pytest.mark.parametrize("action", ["wake", "blank"])
def test_display_checks_physical_target_and_does_not_fake_input(state, other, name, expected, commands, action):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=xrandr_result(other=other, name=name)
            if args[0].endswith("xrandr") else "Monitor is " + state)
    display = HomeDisplay(HomeConfig(display=":0", xauthority="auth", monitor_output="DP-2", monitor_name="27M1 Max"), run=run)
    if action == 'blank' and expected != 'unavailable':
        expected, commands = ('already_off', 2) if state == 'Off' else ('blank_requested', 3)
    assert getattr(display, action)().status == expected
    assert len(calls) == commands
    if commands == 3:
        assert calls[-1] == ("/usr/bin/xset", "dpms", "force", "on" if action == 'wake' else "off")


def blocked_capture(*args):
    time.sleep(30)


def test_camera_two_demands_and_blocked_native_shutdown(monkeypatch, tmp_path):
    from spica.adapters import home_camera
    monkeypatch.setattr(home_camera, "_capture", blocked_capture)
    camera = HomeCamera(HomeConfig(data_directory=str(tmp_path)), None)
    camera.require("occupancy", True)
    camera.require("alarm", True)
    try:
        camera.poll()
        process = camera._process
        camera.require("occupancy", False)
        camera.poll()
        assert camera._process is process and camera.running
        started = time.monotonic()
        camera.close()
        assert time.monotonic()-started < 3
        assert not camera.running
    finally:
        camera.close()


def crashed_writer(config, profile, output, stop, preview):
    output.lock.acquire()
    output.length.value = 10000
    output.buffer[0] = 1
    os._exit(1)


def test_camera_writer_crash_mid_packet_never_blocks_poll(monkeypatch, tmp_path):
    from spica.adapters import home_camera
    monkeypatch.setattr(home_camera, "_capture", crashed_writer)
    camera = HomeCamera(HomeConfig(data_directory=str(tmp_path)), None)
    camera.require("occupancy", True)
    try:
        camera.poll()
        camera._process.join(3)
        assert not camera.running
        started = time.monotonic()
        assert camera.poll()[0].error == "camera_no_fresh_frames"
        assert time.monotonic()-started < .5
    finally:
        camera.close()


def exited_after_owner_frame(config, profile, output, stop, preview):
    now = time.monotonic()
    observation = VisionObservation(config.camera_device, 1, time.time(), now,
                                    (Person((.6, .2, .2, .6), "owner"),))
    output.put((observation, None, None, (1920, 1080)))
    os._exit(1)


def test_camera_exit_invalidates_a_pending_successful_owner_frame(monkeypatch, tmp_path):
    from spica.adapters import home_camera
    monkeypatch.setattr(home_camera, "_capture", exited_after_owner_frame)
    camera = HomeCamera(HomeConfig(camera_device="camera", data_directory=str(tmp_path)), None)
    camera.require("occupancy", True)
    try:
        camera.poll()
        camera._process.join(3)
        assert not camera.running
        packet = camera.poll()
        assert packet[0].error == "camera_no_fresh_frames"
        assert packet[0].people == ()
    finally:
        camera.close()


def test_runtime_failure_releases_mqtt_even_when_camera_cleanup_raises():
    closed = []
    camera = Camera()
    mqtt = MQTTEvents()
    def broken_close():
        closed.append("camera")
        raise RuntimeError("camera cleanup failed")
    camera.close = broken_close
    mqtt.close = lambda: closed.append("mqtt")
    runtime = HomeRuntime(HomeConfig(), mqtt, camera, None, profile())
    runtime.step = lambda: (_ for _ in ()).throw(RuntimeError("controlled observation failure"))
    with pytest.raises(RuntimeError, match="camera cleanup"):
        runtime._run()
    assert closed == ["camera", "mqtt"]
    assert runtime.snapshot()["connection"] == "stopped"
    assert runtime.snapshot()["room_occupied"] is None
    with pytest.raises(RuntimeError, match='resource cleanup failed'):
        runtime.close()


def test_home_close_can_reap_a_camera_after_transient_driver_restore_delay():
    import threading
    calls, restoring = [], [True]
    camera, mqtt = Camera(), MQTTEvents()
    def release_camera():
        calls.append('camera')
        if restoring[0]:
            raise RuntimeError('Home camera process could not be reaped')
        camera.closed = True
    camera.close = release_camera
    mqtt.close = lambda: calls.append('mqtt')
    runtime = HomeRuntime(HomeConfig(), mqtt, camera, None, profile())
    errors = []
    def stop_worker():
        try:
            runtime._close_resources()
        except RuntimeError as exc:
            errors.append(str(exc))
    runtime._thread = threading.Thread(target=stop_worker)
    runtime._thread.start()
    runtime._thread.join(1)
    assert errors == ['Home camera process could not be reaped']
    assert calls == ['camera', 'mqtt']
    restoring[0] = False
    runtime.close()
    runtime.close()
    assert calls == ['camera', 'mqtt', 'camera'] and camera.closed


def test_optional_home_disabled_starts_nothing(monkeypatch):
    from spica.host.assemblies import home
    monkeypatch.setattr(home, "build", lambda *a: pytest.fail("disabled Home built hardware"))
    assert home.install(SimpleNamespace(config=SimpleNamespace(home=HomeConfig()))) is None




def test_a_faceless_frame_does_not_erase_conflicting_identity_evidence():
    locator = RoomLocator(HomeConfig(), profile())
    target = locator.observe(frame(1), 1).people[0].track_id
    assert locator.observe(frame(1.5, identity='unknown'), 1.5).people[0].track_id == target
    changed = np.zeros(512, np.float32)
    changed[0], changed[1] = .8, .6
    other = Person((.6,.2,.2,.6), 'other', appearance=changed.tobytes())
    result = locator.observe(frame(2, people=(other,)), 2)
    assert result.people[0].track_id != target
    assert result.desk_occupied is True


def test_illuminance_requires_its_own_cache_filter_and_fresh_numeric_report():
    clock = [1000.]
    client = MQTTClient()
    mqtt = HomeMQTT(HomeConfig(), client=client, clock=lambda:clock[0], wall_clock=lambda:clock[0])
    mqtt._on_connect(client, None, None, 0, None)
    def message(topic, value, retained=False):
        mqtt._on_message(client, None, SimpleNamespace(topic='zigbee2mqtt/'+topic,
            payload=json.dumps(value).encode(), retain=retained))
    def info(filters):
        message('bridge/info', {'config':{'devices':{'sensor':{'friendly_name':'room_presence',
            'filtered_cache':filters}}}}, True)
    info(['^occupancy$'])
    mqtt.drain()
    message('room_presence', {'occupancy':True, 'illuminance':1, 'last_seen':1000})
    clock[0] += 1
    message('room_presence', {'occupancy':True, 'illuminance':1, 'last_seen':clock[0]})
    assert [name for name,_ in mqtt.drain()] == ['presence']
    info(['^occupancy$', '^illuminance$'])
    mqtt.drain()
    message('room_presence', {'last_seen':clock[0]})  # Cache barrier for both fields.
    clock[0] += 1
    message('room_presence', {'occupancy':False, 'illuminance':0, 'last_seen':clock[0]})
    assert [(name,value.value) for name,value in mqtt.drain()] == [('presence',False),('illuminance',0)]
    for invalid in (True, -1, float('nan'), '0'):
        clock[0] += 1
        message('room_presence', {'illuminance':invalid, 'last_seen':clock[0]})
    assert mqtt.drain() == []
    mqtt.invalidate_observations()
    mqtt.drain()
    message('room_presence', {'illuminance':0, 'last_seen':clock[0]-1})
    assert mqtt.drain() == []
