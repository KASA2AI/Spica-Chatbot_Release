"""Daily room rules with fresh sensor facts; no broker or physical actions."""
from types import SimpleNamespace
from dataclasses import replace

import pytest

from spica.config.home import HomeConfig, HomeLightsConfig
from spica.home.alarm_store import AlarmStore
from spica.home.models import SensorObservation, RoomObservation
from spica.home.scenes import HomeScenes
from spica.home.room_input import RoomInputs


def setup(tmp_path):
    now, presses, welcomes, openings = [1000.], [], [], []
    config = HomeConfig(lights=HomeLightsConfig(dark_lux=10))
    store = AlarmStore(tmp_path/'home.sqlite3')
    scene = HomeScenes(config, store, lambda action, source, key: presses.append((action, source)),
        clock=lambda: now[0], wall_clock=lambda: now[0],
        welcome=lambda: welcomes.append(now[0]), departure=lambda at: openings.append(now[0]))
    inputs = RoomInputs()
    def tick(dt=0, *, presence=True, contact=True, lux=5., opened=False, visual=False, enabled=True):
        now[0] += dt
        def observation(source, value):
            return None if value is None else SensorObservation(source, value, now[0], now[0], now[0]+120)
        occupied, door, light = observation('presence', presence), observation('door', contact), observation('lux', lux)
        # An explicit opening represents a new close/open transition, even
        # when this policy test omits the preceding closed-room interval.
        events = ([('door', replace(observation('door', True), observed_at=now[0]-.001)),
                   ('door_event', observation('door', False))] if opened else [])
        events += [(name, value) for name, value in
                   (('presence', occupied), ('door', door), ('illuminance', light)) if value is not None]
        batch = replace(inputs.consume(events, now[0]), presence=occupied, door=door, illuminance=light)
        scene.step(batch, room=RoomObservation(desk_occupied=visual, bed_occupied=visual),
                   frame_mono=now[0] if visual else None, enabled=enabled)
    return SimpleNamespace(scene=scene, now=now, presses=presses, welcomes=welcomes,
        openings=openings, tick=tick, store=store, config=config, inputs=inputs)


def test_departure_uses_fresh_presence_fall_not_opening_or_startup_absence(tmp_path):
    env = setup(tmp_path)
    departures = []
    env.scene.departure = departures.append
    env.tick(presence=False)
    env.tick(1, presence=True)
    env.tick(20, presence=False)
    assert departures == [env.now[0]], '告别必须在新无人上报时触发，不等开门'
    env.tick(1, presence=False, opened=True, contact=False)
    env.tick(60, presence=False)
    assert len(departures) == 1
    env.tick(1, presence=True)
    env.tick(121, presence=False)
    assert len(departures) == 1, '过期或断档后的无人不是连续有人→无人'
    env.inputs.reset('reconnected')
    env.tick(1, presence=False)
    assert len(departures) == 1


def camera_tick(env, *, person=False, bed=False, valid=True, dt=.5, fresh=True,
                room=None, enabled=True):
    env.now[0] += dt
    at = env.now[0]
    room = room or (RoomObservation(desk_occupied=person, bed_occupied=bed,
             outside_bed_occupied=person, reason='observed' if person or bed else 'no_person', source='camera')
            if valid else RoomObservation(reason='no_fresh_frame'))
    if fresh:
        env.camera_at = at
    batch = env.inputs.consume([('presence', SensorObservation('presence', True, at, at, at+120))], at)
    env.scene.step(batch, room=room, frame_mono=getattr(env, 'camera_at', None), enabled=enabled)


def test_camera_departure_precedes_delayed_sensor_and_door_after_three_empty_seconds(tmp_path):
    env = setup(tmp_path)
    env.tick(lux=50)
    for _ in range(4):
        camera_tick(env, person=True)
    camera_tick(env)
    for _ in range(5):
        camera_tick(env)
    assert not env.openings, 'Two and a half seconds of empty frames are not enough'
    camera_tick(env)
    assert env.openings == [env.now[0]], 'Valid empty frames must not wait for a door or radar report'
    for _ in range(8):
        camera_tick(env)
    assert len(env.openings) == 1


@pytest.mark.parametrize('previous_person', ['none', 'bed', 'brief_outside'])
def test_empty_camera_requires_a_stable_person_outside_bed_first(tmp_path, previous_person):
    env = setup(tmp_path)
    for _ in range(4):
        camera_tick(env, bed=previous_person == 'bed')
    if previous_person == 'brief_outside':
        camera_tick(env, person=True)
    for _ in range(10):
        camera_tick(env)
    assert not env.openings, 'An empty room, blanket or one transient detection is not a departure'


@pytest.mark.parametrize('boundary', ['error', 'uncertain', 'gap', 'reconnect', 'night'])
def test_camera_departure_requires_continuous_known_frames_in_the_current_context(tmp_path, boundary):
    env = setup(tmp_path)
    for _ in range(4):
        camera_tick(env, person=True)
    for _ in range(5):
        camera_tick(env)
    if boundary == 'error':
        camera_tick(env, valid=False)
    elif boundary == 'uncertain':
        camera_tick(env, room=RoomObservation(bed_occupied=None, outside_bed_occupied=False,
                    reason='observed', source='camera'))
    elif boundary == 'gap':
        camera_tick(env, dt=3)
    elif boundary == 'reconnect':
        env.inputs.reset('reconnected')
    else:
        camera_tick(env, enabled=False)
    for _ in range(10):
        camera_tick(env)
    assert not env.openings, 'Lost or invalid evidence cannot complete an earlier departure'
    for _ in range(4):
        camera_tick(env, person=True)
    for _ in range(7):
        camera_tick(env)
    assert len(env.openings) == 1, 'A new complete observation must still work after recovery'


def test_camera_departure_restarts_absence_when_person_returns_and_rejects_frozen_images(tmp_path):
    env = setup(tmp_path)
    for _ in range(4):
        camera_tick(env, person=True)
    for _ in range(5):
        camera_tick(env)
    camera_tick(env, person=True)
    for _ in range(6):
        camera_tick(env)
    assert not env.openings, 'Earlier empty frames cannot count after somebody returns'
    camera_tick(env)
    assert len(env.openings) == 1
    for _ in range(4):
        camera_tick(env, person=True)
    camera_tick(env)
    for _ in range(8):
        camera_tick(env, fresh=False)
    for _ in range(7):
        camera_tick(env)
    assert len(env.openings) == 1, 'Polling a frozen empty image is not three seconds of fresh evidence'


def test_batched_absence_followed_by_presence_does_not_say_farewell(tmp_path):
    env = setup(tmp_path)
    departures = []
    env.scene.departure = departures.append
    env.tick()
    env.now[0] += 20
    at = env.now[0]
    batch = env.inputs.consume([
        ('presence', SensorObservation('presence', False, at-1, at-1, at+100)),
        ('presence', SensorObservation('presence', True, at, at, at+120)),
    ], at)
    env.scene.step(batch, room=RoomObservation())
    assert not departures


def test_camera_person_vetoes_false_sensor_departure_and_vacancy(tmp_path):
    env = setup(tmp_path)
    env.tick(presence=True, contact=False, visual=True, lux=50)
    env.tick(1, presence=False, contact=False, visual=True, lux=50)
    assert not env.openings, 'A visible person must veto a contradictory sensor farewell'
    for _ in range(6):
        env.tick(60, presence=False, contact=False, visual=True, lux=50)
    assert not env.openings and not env.presses
    assert env.scene.snapshot()['vacant_seconds'] == 0


@pytest.mark.parametrize('frame_age', [-1, 0, 3])
def test_only_current_camera_evidence_can_veto_a_sensor_departure(tmp_path, frame_age):
    env = setup(tmp_path)
    env.tick(presence=True, lux=50)
    env.now[0] += 10
    at = env.now[0]
    batch = env.inputs.consume([('presence', SensorObservation('room', False, at, at, at+120))], at)
    env.scene.step(batch, room=RoomObservation(desk_occupied=True), frame_mono=at-frame_age)
    assert env.openings == ([] if frame_age == 0 else [at])


@pytest.mark.parametrize('source', ['presence', 'camera'])
def test_return_opening_starts_camera_and_first_person_proposes_welcome_before_light_wait(tmp_path, source):
    from test_home import Camera, MQTTEvents, frame, profile
    from spica.home.runtime import HomeRuntime
    now, actions = [1000.], []
    mqtt, camera = MQTTEvents(), Camera()
    runtime = HomeRuntime(HomeConfig(lights=HomeLightsConfig(dark_lux=10)), mqtt, camera, None, profile(),
        store=AlarmStore(tmp_path/'return.sqlite3'), clock=lambda: now[0], wall_clock=lambda: now[0])
    def event(name, value):
        return name, SensorObservation(name, value, now[0], now[0], now[0]+180)
    runtime.scenes.welcome = lambda: actions.append('welcome')
    def press(*args):
        actions.append('light')
        now[0] += 8  # A slow finger must not postpone an already justified greeting.
    runtime.scenes.press = press
    try:
        for at in range(1000, 2861, 60):
            now[0] = float(at)
            mqtt.events = [event('presence', False), event('illuminance', 50)]
            runtime.step()
        assert not camera.running and not actions
        now[0] += 1
        mqtt.events = [event('door_event', False), event('presence', source == 'presence'),
                       event('illuminance', 5)]
        if source == 'camera':
            camera.packet = (frame(now[0], identity='unknown'), None, None, None)
        runtime.step()
        assert 'door' in camera.reasons
        assert actions == ['welcome', 'light']
        now[0] += 1
        mqtt.events = [event('door_event', True), event('presence', True)]
        camera.packet = (frame(now[0], identity='unknown'), None, None, None)
        runtime.step()
        assert actions == ['welcome', 'light'], '第二路检测随后到达不能重复欢迎'
    finally:
        runtime.close()


@pytest.mark.parametrize('evidence', ['presence', 'camera'])
def test_long_absence_without_new_opening_does_not_welcome(tmp_path, evidence):
    env = setup(tmp_path)
    env.tick(presence=False, contact=None, lux=50)
    for _ in range(31):
        env.tick(60, presence=False, contact=None, lux=50)
    env.tick(1, presence=evidence == 'presence', contact=None, visual=evidence == 'camera', lux=50)
    assert not env.welcomes


@pytest.mark.parametrize('presence_offset,welcomed', [(-1, False), (0, True), (1, True)])
@pytest.mark.parametrize('split_batch', [False, True])
def test_welcome_uses_event_order_across_sensor_topics(tmp_path, presence_offset, welcomed, split_batch):
    env = setup(tmp_path)
    env.tick(presence=False, lux=50)
    for _ in range(31):
        env.tick(60, presence=False, lux=50)
    env.now[0] += 3
    at = env.now[0]
    # Presence is delivered first, but the door has its own source timestamp.
    events = [
        ('presence', SensorObservation('presence', True, at-1+presence_offset, at, at+100)),
        ('door_event', SensorObservation('door', False, at-1, at, at+100)),
    ]
    if split_batch:
        env.scene.step(env.inputs.consume(events[:1], at), room=RoomObservation())
        env.now[0] += .2
        at = env.now[0]
        events = [(events[1][0], replace(events[1][1], received_at=at))]
    env.scene.step(env.inputs.consume(events, at),
        room=RoomObservation(bed_occupied=True), frame_mono=at)
    assert bool(env.welcomes) is welcomed
    # A second delayed opening cannot reuse the same ended absence.
    events = [
        ('door_event', SensorObservation('door', True, at-.9, at+.1, at+100)),
        ('door_event', SensorObservation('door', False, at-.8, at+.1, at+100)),
    ]
    env.now[0] += .1
    env.scene.step(env.inputs.consume(events, env.now[0]),
        room=RoomObservation(bed_occupied=True), frame_mono=env.now[0])
    assert len(env.welcomes) == int(welcomed)


@pytest.mark.parametrize('boundary', ['reconnect', 'night', 'expired'])
def test_late_opening_cannot_restore_welcome_after_context_reset(tmp_path, boundary):
    env = setup(tmp_path)
    env.tick(presence=False, lux=50)
    for _ in range(31):
        env.tick(60, presence=False, lux=50)
    env.tick(1, presence=True, lux=50)
    opening_at = env.now[0]-.1
    if boundary == 'reconnect':
        env.inputs.reset('reconnected')
    elif boundary == 'night':
        env.tick(enabled=False)
    env.now[0] += env.config.door_camera_seconds if boundary == 'expired' else .2
    at = env.now[0]
    report = SensorObservation('door', False, opening_at, at, at+100)
    env.scene.step(env.inputs.consume([('door_event', report)], at),
        room=RoomObservation(bed_occupied=True), frame_mono=at)
    assert not env.welcomes


@pytest.mark.parametrize('stale', ['pre_opening_frame', 'pre_opening_presence', 'expired_window'])
def test_return_welcome_requires_people_from_the_current_opening_window(tmp_path, stale):
    env = setup(tmp_path)
    env.tick(presence=False, lux=50)
    for _ in range(31):
        env.tick(60, presence=False, lux=50)
    env.tick(1, presence=False, contact=False, opened=True, lux=50)
    opened_at = env.now[0]
    env.now[0] += env.config.door_camera_seconds if stale == 'expired_window' else .2
    at = env.now[0]
    events = []
    room, image_at = RoomObservation(), None
    if stale == 'pre_opening_presence':
        events = [('presence', SensorObservation('presence', True, opened_at-.1, at, at+120))]
    else:
        room = RoomObservation(bed_occupied=True)
        image_at = opened_at-.1 if stale == 'pre_opening_frame' else at
    env.scene.step(env.inputs.consume(events, at), room=room, frame_mono=image_at)
    assert not env.welcomes


@pytest.mark.parametrize('boundary', ['short_absence', 'reconnect', 'night'])
def test_return_welcome_qualification_does_not_survive_invalid_room_context(tmp_path, boundary):
    env = setup(tmp_path)
    env.tick(presence=False, lux=50)
    for _ in range(5 if boundary == 'short_absence' else 31):
        env.tick(60, presence=False, lux=50)
    env.tick(1, presence=False, contact=False, opened=True, lux=50)
    if boundary == 'reconnect':
        env.inputs.reset('reconnected')
    elif boundary == 'night':
        env.tick(1, presence=False, enabled=False, lux=50)
    env.tick(1, presence=True, visual=True, lux=50)
    assert not env.welcomes


def test_camera_can_confirm_return_after_presence_expires_within_opening_window(tmp_path):
    env = setup(tmp_path)
    env.tick(presence=False, lux=50)
    for _ in range(31):
        env.tick(60, presence=False, lux=50)
    at = env.now[0]
    # Vacancy is still known at the opening, then the sensor goes unknown.
    batch = env.inputs.consume([
        ('presence', SensorObservation('presence', False, at+.1, at+.1, at+1)),
        ('door_event', SensorObservation('door', False, at+.2, at+.2, at+120)),
    ], at+.2)
    env.now[0] = at+.2
    env.scene.step(batch, room=RoomObservation())
    env.now[0] = at+2
    batch = env.inputs.consume([], env.now[0])
    assert batch.occupied is None
    env.scene.step(batch, room=RoomObservation(bed_occupied=True), frame_mono=env.now[0])
    assert env.welcomes == [env.now[0]], '相机确认不应再等待传感器恢复'


def test_return_opening_cannot_qualify_an_absence_that_already_expired(tmp_path):
    env = setup(tmp_path)
    env.tick(presence=False, lux=50)
    for _ in range(31):
        env.tick(60, presence=False, lux=50)
    # A stalled consumer receives opening and a new positive report together,
    # after its previous empty evidence has expired.
    env.tick(121, presence=True, contact=False, opened=True, lux=50)
    assert not env.welcomes


def test_closed_door_protects_room_and_delayed_positive_does_not_erase_departure(tmp_path):
    env = setup(tmp_path)
    env.tick(lux=50)
    for _ in range(31):
        env.tick(60, presence=False, lux=50)
    assert env.presses == []
    env.tick(1, lux=50)
    assert env.welcomes == []  # Closed-room sensor loss is not a return.
    env.tick(1, opened=True, contact=False, lux=50)
    env.tick(1, contact=True, lux=50)  # Still-positive sensor; no new false -> true return.
    env.tick(1, presence=False, lux=50)
    for _ in range(5):
        env.tick(60, presence=False, lux=50)
    assert env.presses == [('off', 'vacant')]


@pytest.mark.parametrize('contact', [None, True])
def test_starting_while_room_empty_collects_new_absence_for_return(tmp_path, contact):
    env = setup(tmp_path)
    env.tick(presence=False, contact=contact)
    for _ in range(31):
        env.tick(60, presence=False, contact=contact)
    assert not env.presses and not env.welcomes
    assert env.scene.snapshot()['vacant_seconds'] >= 1800
    env.tick(1, presence=False, contact=False, opened=True)
    assert env.presses == [('on', 'return')]
    assert not env.welcomes  # An opening alone does not authorize a greeting.
    env.tick(1, presence=True, contact=True)
    assert len(env.welcomes) == 1
    env.tick(1, presence=True, contact=True)
    assert len(env.welcomes) == 1 and len(env.presses) == 1


def test_new_return_is_not_blocked_by_another_boots_persisted_on_intent(tmp_path):
    # The other OS (or a wall switch) changed the real lamp while this owner
    # was offline. The local journal is an intent, not a light-state sensor.
    AlarmStore(tmp_path/'home.sqlite3').save_room_state({
        'manual_off': False,
        'last_light': {'action': 'on', 'source': 'wake_alarm', 'at': 0.},
    })
    env = setup(tmp_path)
    for _ in range(6):
        env.tick(60, presence=False, contact=None, lux=1)
    assert not env.presses

    env.tick(1, presence=False, contact=False, opened=True, lux=1)
    assert env.presses == [('on', 'return')]
    # The door may bounce and radar may still say empty after the request.
    for _ in range(8):
        env.tick(1, presence=False, contact=False, opened=True, lux=1)
    env.tick(1, presence=True, contact=True, lux=1)
    for _ in range(6):
        env.tick(1, presence=True, contact=True, lux=1)
    assert env.presses == [('on', 'return')]


@pytest.mark.parametrize('boundary', ['reconnect', 'night', 'expired'])
def test_unused_return_light_permission_does_not_survive_lost_observations(tmp_path, boundary):
    env = setup(tmp_path)
    env.scene.light_intent('on', 'wake_alarm')
    env.tick(presence=False, contact=None, lux=50)
    for _ in range(6):
        env.tick(60, presence=False, contact=None, lux=50)
    assert not env.presses
    if boundary == 'reconnect':
        env.inputs.reset('reconnected')
    elif boundary == 'night':
        env.tick(enabled=False)
    else:
        env.tick(181, presence=None, contact=None, lux=50)
    for _ in range(8):
        env.tick(1, presence=True, contact=True, lux=1)
    assert not env.presses, 'A gap must not replay the old on request on reconnection'


def test_manual_on_during_absence_consumes_that_returns_light_permission(tmp_path):
    env = setup(tmp_path)
    env.tick(presence=False, contact=None, lux=1)
    for _ in range(4):
        env.tick(60, presence=False, contact=None, lux=1)
    env.scene.light_intent('on', 'manual')
    env.tick(60, presence=False, contact=None, lux=1)
    env.tick(1, presence=False, contact=False, opened=True, lux=1)
    env.tick(1, presence=True, contact=True, lux=1)
    assert not env.presses, 'An explicit on request already served this absence'


@pytest.mark.parametrize('outcome', ['requested', 'not_ready', 'unknown'])
def test_return_request_is_once_even_if_finger_result_is_not_confirmed(tmp_path, outcome):
    env = setup(tmp_path)
    env.scene.light_intent('on', 'wake_alarm')
    def press(action, source, key):
        env.presses.append((action, source))
        return {'press_status': outcome, 'light_state': 'unknown'}
    env.scene.press = press
    env.tick(presence=False, contact=None, lux=1)
    for _ in range(6):
        env.tick(60, presence=False, contact=None, lux=1)
    env.tick(1, presence=False, contact=False, opened=True, lux=1)
    for _ in range(10):
        env.tick(1, presence=True, contact=True, lux=1)
    assert env.presses == [('on', 'return')]
    # A process restart while somebody is already in the room grants nothing.
    restored = HomeScenes(env.config, env.store, press, clock=lambda: env.now[0])
    inputs = RoomInputs()
    for _ in range(8):
        env.now[0] += 1
        at = env.now[0]
        restored.step(inputs.consume([
            ('presence', SensorObservation('presence', True, at, at, at+120)),
            ('illuminance', SensorObservation('lux', 1, at, at, at+120)),
        ], at), room=RoomObservation())
    assert env.presses == [('on', 'return')]


def test_unknown_door_does_not_turn_one_positive_report_into_permanent_closed_room_protection(tmp_path):
    env = setup(tmp_path)
    env.tick(presence=False, contact=None, lux=50)
    env.tick(1, presence=True, contact=None, lux=50)
    env.tick(20, presence=False, contact=None, lux=50)
    for _ in range(31):
        env.tick(60, presence=False, contact=None, lux=50)
    assert not env.presses and not env.welcomes
    assert env.scene.snapshot()['vacant_seconds'] >= 1800
    env.tick(1, presence=False, contact=False, opened=True)
    assert env.presses == [('on', 'return')]
    env.tick(1, presence=True, contact=True)
    assert len(env.welcomes) == 1


def test_manual_off_protects_short_stay_then_five_minute_absence_releases_it(tmp_path):
    env = setup(tmp_path)
    env.tick(contact=False)
    env.scene.light_intent('off', 'manual')
    env.tick(10, contact=False)
    env.tick(1, presence=False, contact=False)
    for _ in range(4):
        env.tick(60, presence=False, contact=False)
    env.tick(1, contact=False)
    assert env.presses == [] and env.scene.state['manual_off']
    env.tick(1, presence=False, contact=False)
    for _ in range(5):
        env.tick(60, presence=False, contact=False)
    assert not env.scene.state['manual_off']
    env.tick(1, presence=False, opened=True, contact=False)
    assert env.presses == [('on', 'return')]
    for _ in range(4):
        env.tick(60, presence=False, contact=False)
    assert env.presses == [('on', 'return')]  # Old off timer was consumed by return opening.


def test_manual_on_restarts_off_timer_without_erasing_long_absence_welcome(tmp_path):
    env = setup(tmp_path)
    env.tick(presence=False, contact=False, lux=50)
    for _ in range(31):
        env.tick(60, presence=False, contact=False, lux=50)
    assert env.presses == [('off', 'vacant')]
    env.scene.light_intent('on', 'manual')
    env.tick(1, presence=False, contact=False, lux=50)
    for _ in range(4):
        env.tick(60, presence=False, contact=False, lux=50)
    assert len(env.presses) == 1
    env.tick(1, presence=False, contact=False, opened=True)
    assert env.welcomes == []
    env.tick(1, presence=True, contact=True)
    assert len(env.welcomes) == 1
    env.tick(1, presence=True)
    assert len(env.welcomes) == 1 and len(env.presses) == 1


@pytest.mark.parametrize('intermediate_lux', [5, 50])
def test_batched_light_reports_preserve_only_continuous_darkness(tmp_path, intermediate_lux):
    env = setup(tmp_path)
    env.tick(presence=True, lux=5)
    env.now[0] = 1005.
    events = [
        ('illuminance', SensorObservation('lux', intermediate_lux, 1004., 1004., 1124.)),
        ('illuminance', SensorObservation('lux', 5, 1005., 1005., 1125.)),
        ('presence', SensorObservation('presence', True, 1005., 1005., 1125.)),
    ]
    env.scene.step(env.inputs.consume(events, env.now[0]), room=RoomObservation())
    if intermediate_lux > env.config.lights.dark_lux:
        assert not env.presses, 'The bright report must break the earlier dark period'
        env.tick(4, presence=True, lux=5)
        assert not env.presses
        env.tick(1, presence=True, lux=5)
    assert env.presses == [('on', 'dark_room')]


def test_unknown_or_camera_person_breaks_vacancy_and_light_is_not_retried(tmp_path):
    env = setup(tmp_path)
    env.tick(presence=False, contact=False, lux=None)
    for _ in range(4):
        env.tick(60, presence=False, contact=False, lux=None)
    env.tick(30, presence=False, contact=False, lux=None, visual=True)
    env.tick(30, presence=False, contact=False, lux=None)
    for _ in range(4):
        env.tick(60, presence=False, contact=False, lux=None)
    env.tick(30, presence=None, contact=False, lux=None)
    env.tick(30, presence=False, contact=False, lux=None)
    assert env.presses == []
    env.tick(1, presence=True, lux=None)
    env.tick(10, presence=True, lux=5)
    assert env.presses == []
    env.tick(5, presence=True, lux=5)
    env.tick(60, presence=True, lux=5)
    assert env.presses == [('on', 'dark_room')]
    env.tick(60, presence=True, lux=100)
    assert env.presses == [('on', 'dark_room')]  # Brightness never turns lights off.


def test_night_blocks_daily_actions_and_restart_preserves_only_intent(tmp_path):
    env = setup(tmp_path)
    env.scene.light_intent('off', 'manual')
    env.tick(enabled=False, opened=True, contact=False)
    for _ in range(31):
        env.tick(60, enabled=False, presence=False, contact=False)
    assert not env.presses and not env.welcomes and not env.openings
    restored = HomeScenes(env.config, env.store, lambda *a: env.presses.append(a), clock=lambda: env.now[0])
    assert restored.state['manual_off'] and restored.absent_since is None
    restored.light_intent('off', 'bedtime')
    assert restored.state['manual_off']  # Separate prior manual intent survives bedtime.
    restored.light_intent('on', 'manual')
    restored.light_intent('off', 'bedtime')
    assert not restored.state['manual_off']  # Bedtime does not leave an extra ordinary guard.


@pytest.mark.parametrize('new_person_after_close', [False, True])
def test_pre_opening_frame_cannot_cancel_departure_but_new_closed_room_person_can(tmp_path, new_person_after_close):
    from test_home import Camera, MQTTEvents, profile
    from spica.home.models import Person, VisionObservation
    from spica.home.runtime import HomeRuntime
    now, presses, welcomes = [1000.], [], []
    camera, mqtt = Camera(), MQTTEvents()
    runtime = HomeRuntime(HomeConfig(), mqtt, camera, None, profile(), store=AlarmStore(tmp_path/'room.sqlite3'),
                          clock=lambda: now[0], wall_clock=lambda: now[0])
    runtime.scenes.press = lambda action, source, key: presses.append(action)
    runtime.scenes.welcome = lambda: welcomes.append(now[0])
    def tick(at, occupied, closed, image_at=None):
        now[0] = at
        mqtt.events = [(name, SensorObservation(name, value, at, at, at+180))
                       for name, value in [('presence', occupied), ('door_event', closed)]]
        if image_at is not None:
            camera.packet = (VisionObservation('camera', int(image_at*10), image_at, image_at,
                (Person((.1,.2,.2,.6)),)), None, None, None)
        runtime.step()
    try:
        tick(1000, True, True, image_at=1000)
        tick(1000.5, True, False)
        tick(1001, True, True)
        if new_person_after_close:
            tick(1001.5, True, True, image_at=1001.5)
        for at in range(1003, 2864, 60):
            tick(at, False, True)
        assert presses == ([] if new_person_after_close else ['off'])
        tick(2863.5, False, False)  # Return now requires its own real opening.
        tick(2864, True, True)
        assert len(welcomes) == (0 if new_person_after_close else 1)
    finally:
        runtime.close()


@pytest.mark.parametrize('presence_after_close', [False, True])
def test_batched_presence_rise_must_be_observed_after_closing_to_cancel_departure(tmp_path, presence_after_close):
    env = setup(tmp_path)
    env.tick(presence=False, lux=50)
    env.now[0] = 1002.
    # The reports reach one scene step together; their receipt times alone
    # cannot order the true occupancy observation against opening/closing.
    presence_at = 1001.1 if presence_after_close else 1000.1
    events = [('door_event', SensorObservation('door', False, 1000.5, 1002., 1120.)),
              ('presence', SensorObservation('presence', True, presence_at, 1002., 1120.)),
              ('door_event', SensorObservation('door', True, 1001., 1002., 1120.))]
    env.scene.step(env.inputs.consume(events, env.now[0]), room=RoomObservation())
    for _ in range(31):
        env.tick(60, presence=False, lux=50)
    assert env.presses == ([] if presence_after_close else [('off', 'vacant')])
    env.tick(1, presence=False, contact=False, opened=True, lux=50)
    env.tick(1, presence=True, lux=50)
    assert len(env.welcomes) == (0 if presence_after_close else 1)


@pytest.mark.parametrize('batched', [False, True])
@pytest.mark.parametrize('scenario', ['interrupted_vacancy', 'closed_room_return', 'qualified_return'])
def test_presence_transitions_preserve_room_policy_when_drained_together(tmp_path, batched, scenario):
    from test_home import Camera, MQTTEvents, profile
    from spica.home.runtime import HomeRuntime
    now, presses, welcomes = [1000.], [], []
    mqtt = MQTTEvents()
    runtime = HomeRuntime(HomeConfig(), mqtt, Camera(), None, profile(), store=AlarmStore(tmp_path/'room.sqlite3'),
                          clock=lambda: now[0], wall_clock=lambda: now[0])
    runtime.scenes.press = lambda action, source, key: presses.append(action)
    runtime.scenes.welcome = lambda: welcomes.append(now[0])
    def event(name, value, at):
        return name, SensorObservation(name, value, at, at, at+180)
    def tick(at, events):
        now[0], mqtt.events = at, events
        runtime.step()
    def transitions(first, second):
        if batched:
            tick(second[1].received_at, [first, second])
        else:
            for item in (first, second):
                tick(item[1].received_at, [item])
    try:
        if scenario == 'closed_room_return':
            tick(1000, [event('presence', True, 1000), event('door_event', True, 1000)])
            tick(1001, [event('door_event', False, 1001)])
            tick(1002, [event('door_event', True, 1002)])
            transitions(event('presence', False, 1003), event('presence', True, 1004))
            for at in range(1064, 1425, 60):
                tick(at, [event('presence', False, at)])
            assert presses == [] and runtime.scenes.snapshot()['vacant_seconds'] == 0
        else:
            tick(1000, [event('door_event', False, 1000), event('presence', False, 1000)])
            if scenario == 'interrupted_vacancy':
                for at in (1060, 1120, 1180, 1240):
                    tick(at, [event('presence', False, at)])
                transitions(event('presence', True, 1241), event('presence', False, 1242))
                tick(1300, [event('presence', False, 1300)])
                assert presses == [] and runtime.scenes.snapshot()['vacant_seconds'] == 58
            else:
                for at in range(1060, 2861, 60):
                    tick(at, [event('presence', False, at)])
                tick(2860.5, [event('door_event', True, 2860.4), event('door_event', False, 2860.5)])
                transitions(event('presence', True, 2861), event('presence', True, 2862))
                assert len(welcomes) == 1 and runtime.scenes.snapshot()['vacant_seconds'] == 0
    finally:
        runtime.close()


def test_connection_boundary_discards_earlier_opening_in_same_sensor_batch(tmp_path):
    from test_home import Camera, MQTTEvents, profile
    from spica.home.runtime import HomeRuntime
    now, openings = [1000.], []
    mqtt = MQTTEvents()
    runtime = HomeRuntime(HomeConfig(), mqtt, Camera(), None, profile(), store=AlarmStore(tmp_path/'room.sqlite3'),
                          clock=lambda: now[0], wall_clock=lambda: now[0])
    runtime.scenes.departure = openings.append
    try:
        mqtt.events = [('door_event', SensorObservation('door', False, 1000., 1000., 1180.)),
                       ('connection', 'disconnected')]
        runtime.step()
        assert openings == [] and not runtime.scenes._door_opened
        # A new connection does not consume the next real opening.
        now[0] += 1
        mqtt.events = [('connection', 'connected'),
                       ('door_event', SensorObservation('door', False, 1001., 1001., 1181.))]
        runtime.step()
        assert openings == [] and runtime.scenes._door_opened
    finally:
        runtime.close()
