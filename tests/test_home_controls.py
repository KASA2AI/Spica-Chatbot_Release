"""H3 manual room controls, with no live broker, device or production config."""
from contextlib import nullcontext
import json
import threading
import time
from types import SimpleNamespace

import pytest

from spica.adapters.home_mqtt import HomeMQTT
from spica.config.home import HomeConfig, HomeLightsConfig, HomeWakeConfig
from spica.home.alarm_store import AlarmStore
from spica.home.models import SensorObservation
from spica.home.runtime import HomeRuntime
from test_home import Camera, MQTTClient, profile

ON = '0xa4c1380f3cc5969d'
OFF = '0xa4c138c8cbbea8e9'


def broker(config):
    client = MQTTClient()
    reported_at = [time.time()]
    mqtt = HomeMQTT(config, client=client, wall_clock=lambda: reported_at[0])
    reported = {}
    def deliver(topic, value):
        if topic in (ON, OFF):
            reported.setdefault(topic, {}).update(value)
            reported_at[0] = max(time.time(), reported_at[0] + .001)
            value = dict(value, last_seen=reported_at[0])
        mqtt._on_message(client, None, SimpleNamespace(topic='zigbee2mqtt/'+topic,
            payload=json.dumps(value).encode(), retain=False))
    deliver('bridge/devices', [dict(ieee_address=device, friendly_name=device,
        interview_completed=True, definition={'model':'TS0001_fingerbot'}) for device in (ON, OFF)])
    deliver('bridge/state', {'state':'online'})
    for device in (ON, OFF):
        deliver(device, {'mode':'click', 'upper':0, 'lower':100, 'reverse':'ON', 'state':'ON'})
    def respond(topic, payload):
        device = topic.split('/')[-2]
        if device not in reported:
            return
        if topic.endswith('/set'):
            reported[device].update(payload)
        deliver(device, reported[device])
    client.on_publish = respond
    client.published.clear()
    return mqtt, client, deliver


def test_readiness_preserves_battery_and_age_without_resetting_or_pressing():
    mqtt, client, deliver = broker(HomeConfig(lights=HomeLightsConfig(on_device=ON, off_device=OFF)))
    deliver(ON, dict(mode='switch', upper=30, lower=80, battery=1))
    observed = mqtt.light_readiness()
    assert observed['status'] == 'needs_reset' and observed['settings']['battery'] == 1
    assert observed['light_state'] == 'unknown'
    mqtt.refresh_light_state()
    assert client.published and all(topic.endswith('/get') for topic, _, _ in client.published)
    mqtt.wall_clock = lambda: observed['observed_at']+mqtt.config.observation_ttl_seconds+1
    assert mqtt.light_readiness()['status'] == 'stale'
    mqtt.close()


def test_light_report_in_flight_during_clock_step_cannot_authorize_next_press(monkeypatch):
    wall, mono = [10000.], [100.]
    client = MQTTClient()
    mqtt = HomeMQTT(HomeConfig(lights=HomeLightsConfig(on_device=ON)), client=client,
                    wall_clock=lambda: wall[0], clock=lambda: mono[0])
    def deliver(topic, value):
        mqtt._on_message(client, None, SimpleNamespace(topic='zigbee2mqtt/'+topic,
            payload=json.dumps(value).encode(), retain=False))
    mqtt._on_connect(client, None, None, 0, None)
    deliver('bridge/devices', [dict(ieee_address=ON, friendly_name=ON,
        interview_completed=True, definition={'model':'TS0001_fingerbot'})])
    deliver('bridge/state', {'state':'online'})
    old_payload = json.dumps(dict(last_seen=wall[0], mode='click', upper=0, lower=100,
                                  reverse='ON', state='ON')).encode()
    loads = json.loads
    def clock_step_during_parse(raw, *args, **kwargs):
        # Force the receive/drain interleaving without timing-dependent sleeps.
        wall[0] += 60
        mqtt.drain()
        return loads(raw, *args, **kwargs)
    try:
        with monkeypatch.context() as patch:
            patch.setattr('spica.adapters.home_mqtt.json.loads', clock_step_during_parse)
            mqtt._on_message(client, None, SimpleNamespace(topic='zigbee2mqtt/'+ON,
                payload=old_payload, retain=False))
        def reply(topic, payload):
            if topic.endswith('/get'):
                deliver(ON, dict(last_seen=wall[0], state='ON'))
        client.on_publish = reply
        client.published.clear()
        assert mqtt.press_room_light('on', lambda: True) == 'direction_unknown'
        assert not any(topic.endswith('/set') for topic, _, _ in client.published)
    finally:
        mqtt.close()


@pytest.mark.parametrize('reverse,initial_state,retracted,pressed', [
    ('ON', 'ON', 'OFF', 'ON'), ('OFF', 'OFF', 'ON', 'OFF'), ('ON', 'OFF', 'OFF', 'ON'),
])
def test_switch_finger_returns_to_calibrated_top_before_clicking(reverse, initial_state, retracted, pressed, monkeypatch):
    mqtt, client, deliver = broker(HomeConfig(lights=HomeLightsConfig(on_device=ON)))
    monkeypatch.setattr(mqtt._fingers, 'SETTLE_SECONDS', 0)
    deliver(ON, dict(mode='switch', upper=30, lower=80, reverse=reverse, state=initial_state))
    assert mqtt.light_status()['on'] == 'needs_reset'
    claims = []
    assert mqtt.press_room_light('on', lambda: claims.append(True)) == 'requested'
    commands = [(topic, payload) for topic, payload, _ in client.published]
    assert commands == [
        ('zigbee2mqtt/'+ON+'/get', {'state':''}),
        ('zigbee2mqtt/'+ON+'/set', {'upper':0}),
        ('zigbee2mqtt/'+ON+'/set', {'state':retracted}),
        ('zigbee2mqtt/'+ON+'/set', {'mode':'click'}),
        ('zigbee2mqtt/'+ON+'/set', {'lower':100}),
        ('zigbee2mqtt/'+ON+'/set', {'state':pressed}),
    ]
    assert claims
    assert all(not options['retain'] for _, _, options in client.published)


def test_old_off_limits_cannot_override_full_return(monkeypatch):
    config = HomeConfig(lights=HomeLightsConfig(on_device=ON, off_device=OFF, off_upper=50, off_lower=100))
    mqtt, client, deliver = broker(config)
    monkeypatch.setattr(mqtt._fingers, 'SETTLE_SECONDS', 0)
    deliver(OFF, dict(mode='switch', upper=50, lower=80, reverse='ON', state='OFF'))
    assert mqtt.press_room_light('off', lambda: None) == 'requested'
    assert [payload for topic, payload, _ in client.published if topic.endswith('/set')] == [
        {'upper':0}, {'state':'OFF'},
        {'mode':'click'}, {'lower':100}, {'state':'ON'},
    ]
    assert all('/'+OFF+'/' in topic for topic, _, _ in client.published if topic.endswith('/set'))


def test_switch_recovery_tolerates_the_observed_queued_read_latency():
    mqtt, client, deliver = broker(HomeConfig(lights=HomeLightsConfig(on_device=ON)))
    deliver(ON, dict(mode='switch', upper=30, lower=80, reverse='ON', state='ON'))
    respond = client.on_publish
    def reply(topic, payload):
        # Live 09-18: the queued read took 2.4 s, then return-position and
        # retraction reports arrived around 2.7/2.9 s. All settle waits must
        # still fit before the single final press; do not bypass those waits.
        time.sleep(2.4 if topic.endswith('/get') else .2)
        respond(topic, payload)
    client.on_publish = reply
    try:
        assert mqtt.press_room_light('on', lambda: None) == 'requested'
        presses = [payload for topic, payload, _ in client.published
                   if topic.endswith('/set') and payload == {'state': 'ON'}]
        assert len(presses) == 1
        assert mqtt.light_status()['on'] == 'ready'
    finally:
        mqtt.close()


def test_mode_change_default_report_cannot_replace_the_maximum_press_travel():
    mqtt, client, deliver = broker(HomeConfig(lights=HomeLightsConfig(on_device=ON)))
    deliver(ON, dict(mode='switch', upper=0, lower=80, reverse='ON', state='OFF'))
    respond = client.on_publish
    resets = []
    def reply(topic, payload):
        respond(topic, payload)
        if payload.get('mode') == 'click':
            # Live 09-18: after acknowledging click mode the device reported
            # its default lower=85, replacing the travel sent in the same set.
            reset = threading.Timer(.2, lambda: deliver(ON, {'lower':85}))
            resets.append(reset)
            reset.start()
    client.on_publish = reply
    try:
        assert mqtt.press_room_light('on', lambda: None) == 'requested'
        assert mqtt.light_readiness()['settings']['lower'] == 100
        assert [payload for topic, payload, _ in client.published if topic.endswith('/set')] == [
            {'mode':'click'}, {'lower':100}, {'state':'ON'},
        ]
    finally:
        for reset in resets:
            reset.join()
        mqtt.close()


@pytest.mark.parametrize('failure', ['stale', 'retained', 'disconnect', 'closed', 'revoked'])
def test_failed_reset_cannot_send_a_later_press(failure, monkeypatch):
    mqtt, client, deliver = broker(HomeConfig(lights=HomeLightsConfig(on_device=ON)))
    monkeypatch.setattr(mqtt._fingers, 'TIMEOUT_SECONDS', .04)
    deliver(ON, dict(mode='switch', upper=30, lower=80, reverse='ON', state='ON'))
    respond, allowed = client.on_publish, [True]
    def reply(topic, payload):
        if topic.endswith('/get') or 'upper' in payload:
            respond(topic, payload)
        elif failure == 'disconnect':
            mqtt._on_disconnect()
        elif failure == 'closed':
            mqtt.close()
        elif failure == 'revoked':
            allowed[0] = False
        else:
            value = dict(mode='switch', upper=30, lower=80, reverse='ON', state='OFF',
                         last_seen=time.time()-30 if failure == 'stale' else time.time())
            mqtt._on_message(client, None, SimpleNamespace(topic='zigbee2mqtt/'+ON,
                payload=json.dumps(value).encode(), retain=failure == 'retained'))
    client.on_publish = reply
    assert mqtt.press_room_light('on', lambda: allowed[0]) == 'reset_unconfirmed'
    assert [payload for topic, payload, _ in client.published if topic.endswith('/set')] == [{'upper':0}, {'state':'OFF'}]


def test_unconfirmed_configuration_after_reset_cannot_click(monkeypatch):
    mqtt, client, deliver = broker(HomeConfig(lights=HomeLightsConfig(on_device=ON)))
    monkeypatch.setattr(mqtt._fingers, 'TIMEOUT_SECONDS', .04)
    monkeypatch.setattr(mqtt._fingers, 'SETTLE_SECONDS', 0)
    deliver(ON, dict(mode='switch', upper=0, lower=80, reverse='ON', state='ON'))
    respond = client.on_publish
    client.on_publish = lambda topic, payload: respond(topic, payload) if 'mode' not in payload else None
    assert mqtt.press_room_light('on', lambda: None) == 'configuration_unconfirmed'
    assert [payload for topic, payload, _ in client.published if topic.endswith('/set')] == [
        {'state':'OFF'}, {'mode':'click'},
    ]


def test_alarm_deadline_bounds_device_wait_even_while_authority_remains_valid():
    mqtt, client, _ = broker(HomeConfig(lights=HomeLightsConfig(on_device=ON)))
    mono = [100.]
    mqtt.clock = lambda: mono[0]
    client.on_publish = None  # The state read receives no fresh acknowledgement.
    deadline = mono[0]+2

    def authorized():
        mono[0] += 1  # Time spent checking authority also consumes preparation.
        return True

    try:
        assert mqtt.press_light(authorized, deadline=deadline) == 'state_unconfirmed'
        assert mono[0] <= deadline
        assert [(topic, payload) for topic, payload, _ in client.published] == [
            ('zigbee2mqtt/'+ON+'/get', {'state':''}),
        ]
    finally:
        mqtt.close()


def test_click_receipt_is_not_a_held_arm_and_unknown_direction_never_moves():
    mqtt, client, deliver = broker(HomeConfig(lights=HomeLightsConfig(on_device=ON)))
    assert mqtt.press_room_light('on', lambda: None) == 'requested'
    assert [payload for topic, payload, _ in client.published if topic.endswith('/set')] == [{'state':'ON'}]
    client.published.clear()
    deliver(ON, dict(mode='switch', reverse='unknown', state='ON'))
    assert mqtt.press_room_light('on', lambda: None) == 'direction_unknown'
    assert not [topic for topic, _, _ in client.published if topic.endswith('/set')]


def room(tmp_path, *, store=None, save=None, alarms=None):
    config = HomeConfig(enabled=True, lights=HomeLightsConfig(on_device=ON, off_device=OFF))
    mqtt, client, deliver = broker(config)
    runtime = HomeRuntime(config, mqtt, Camera(), None, profile(), clock=lambda:1000,
        store=store or AlarmStore(tmp_path/'home.sqlite3'), save_detection=save, alarms=alarms)
    return runtime, client, deliver


def test_light_bindings_migrate_h2_and_reject_ambiguous_or_arbitrary_targets():
    config = HomeConfig(wake=HomeWakeConfig(light_on_device=ON))
    assert config.lights.on_device == ON and not config.lights.off_device
    with pytest.raises(ValueError, match='conflicting'):
        HomeConfig(wake=HomeWakeConfig(light_on_device=ON), lights=HomeLightsConfig(on_device=OFF))
    with pytest.raises(ValueError, match='distinct'):
        HomeConfig(lights=HomeLightsConfig(on_device=ON, off_device=ON))
    with pytest.raises(ValueError):
        HomeLightsConfig(off_device='arbitrary/device/set')
    with pytest.raises(ValueError, match='together'):
        HomeLightsConfig(off_upper=30)
    with pytest.raises(ValueError, match='less than'):
        HomeLightsConfig(off_upper=50, off_lower=50)


def test_room_off_click_has_its_own_binding_and_never_moves_the_on_finger():
    config = HomeConfig(lights=HomeLightsConfig(off_device=OFF))
    mqtt, client, deliver = broker(config)
    claims=[]
    assert mqtt.press_room_light('on', lambda:claims.append('on')) == 'not_configured'
    assert mqtt.press_room_light('off', lambda:claims.append('off')) == 'requested'
    assert set(claims) == {'off'}
    assert client.published == [
        ('zigbee2mqtt/'+OFF+'/get', {'state':''}, {'qos':0,'retain':False}),
        ('zigbee2mqtt/'+OFF+'/set', {'state':'ON'}, {'qos':0,'retain':False}),
    ]
    deliver(OFF, {'mode':'program'})
    assert mqtt.press_room_light('off', lambda:claims.append('off')) == 'mode_unsupported'
    count = len(client.published)
    mqtt._on_disconnect()
    mqtt._on_connect(client, None, None, 0, None)
    assert len(client.published) == count  # Reconnect does not replay a press.
    mqtt.close()


@pytest.mark.parametrize('action,mode,upper,reverse', [
    ('off', 'switch', 0, 'ON'), ('off', 'click', 50, 'ON'), ('on', 'switch', 50, 'OFF'),
])
def test_room_light_recovers_the_obstructing_other_finger_before_pressing(action, mode, upper, reverse, monkeypatch):
    config = HomeConfig(lights=HomeLightsConfig(on_device=ON, off_device=OFF, off_upper=50, off_lower=100))
    mqtt, client, deliver = broker(config)
    monkeypatch.setattr(mqtt._fingers, 'SETTLE_SECONDS', 0)
    other, requested = (ON, OFF) if action == 'off' else (OFF, ON)
    held, retracted = ('ON', 'OFF') if reverse == 'ON' else ('OFF', 'ON')
    deliver(other, dict(mode=mode, upper=upper, lower=100, reverse=reverse, state=held))
    assert mqtt.press_room_light(action, lambda: None) == 'requested'
    motions = [(topic.split('/')[-2], payload['state']) for topic, payload, _ in client.published
               if topic.endswith('/set') and 'state' in payload]
    assert motions == [(other, retracted), (requested, 'ON')], 'Retract the obstructing finger before pressing the other side'
    settings = mqtt.light_readiness('on' if action == 'off' else 'off')['settings']
    assert settings['upper'] == 0 and settings['mode'] == 'click'
    mqtt.close()


def test_failed_other_finger_retraction_cannot_press_the_requested_side(monkeypatch):
    mqtt, client, deliver = broker(HomeConfig(lights=HomeLightsConfig(on_device=ON, off_device=OFF)))
    monkeypatch.setattr(mqtt._fingers, 'TIMEOUT_SECONDS', .04)
    monkeypatch.setattr(mqtt._fingers, 'SETTLE_SECONDS', 0)
    deliver(ON, dict(mode='switch', state='ON'))
    respond = client.on_publish
    client.on_publish = lambda topic, payload: respond(topic, payload) if topic.endswith('/get') else None
    assert mqtt.press_room_light('off', lambda: None) == 'other_finger_reset_unconfirmed'
    assert [(topic, payload) for topic, payload, _ in client.published if topic.endswith('/set')] == [
        ('zigbee2mqtt/'+ON+'/set', {'state':'OFF'}),
    ]
    mqtt.close()


def test_gateway_change_to_prepared_other_finger_cancels_the_press():
    mqtt, client, deliver = broker(HomeConfig(lights=HomeLightsConfig(on_device=ON, off_device=OFF)))
    respond = client.on_publish
    def reply(topic, payload):
        respond(topic, payload)
        if topic == 'zigbee2mqtt/'+OFF+'/get':
            deliver(ON, dict(mode='switch', state='ON'))
    client.on_publish = reply
    assert mqtt.press_room_light('off', lambda: None) == 'other_finger_configuration_changed'
    assert not [topic for topic, _, _ in client.published if topic.endswith('/set')]
    mqtt.close()


def test_off_travel_baseline_is_not_taken_from_gateway_reports(monkeypatch):
    mqtt, client, deliver = broker(HomeConfig(lights=HomeLightsConfig(off_device=OFF)))
    monkeypatch.setattr(mqtt._fingers, 'SETTLE_SECONDS', 0)
    deliver(OFF, dict(mode='click', upper=30, lower=80))
    assert mqtt.press_room_light('off', lambda: None) == 'requested'
    settings = mqtt.light_readiness('off')['settings']
    assert (settings['upper'], settings['lower']) == (0, 100)
    mqtt.close()


def test_light_receipt_survives_restart_and_uncertain_publish_is_never_retried(tmp_path, monkeypatch):
    runtime, client, deliver = room(tmp_path)
    deliver(OFF, dict(mode='switch', state='ON'))
    attempts=[]
    publish = client.publish
    def uncertain(topic, payload, **kwargs):
        if topic.endswith('/get'):
            return publish(topic, payload, **kwargs)
        attempts.append((topic,payload))
        raise OSError('synthetic disconnect after send')
    monkeypatch.setattr(client, 'publish', uncertain)
    with runtime.conversation('same-request', 'qq'):
        first=runtime.set_room_light(action='off')
        assert first['press_status'] == 'unconfirmed' and first['light_state'] == 'unknown'
        assert runtime.set_room_light(action='off') == first
    runtime.close()
    restarted, new_client, _ = room(tmp_path, store=runtime.store)
    with restarted.conversation('same-request', 'qq'):
        assert restarted.set_room_light(action='off') == first
    assert len(attempts) == 1 and not new_client.published
    assert json.loads(attempts[0][1]) == {'state':'OFF'}  # Uncertain retraction is not replayed either.
    restarted.close()


def test_claimed_but_unsettled_press_is_queryable_without_recovery_execution(tmp_path, monkeypatch):
    runtime, client, _ = room(tmp_path)
    original = runtime.store.finish_command
    monkeypatch.setattr(runtime.store, 'finish_command', lambda *a: (_ for _ in ()).throw(OSError('disk failure')))
    with runtime.conversation('request', 'desktop'):
        with pytest.raises(OSError):
            runtime.set_room_light(action='on')
    monkeypatch.setattr(runtime.store, 'finish_command', original)
    with runtime.conversation('request', 'desktop'):
        result=runtime.set_room_light(action='on')
        assert result['press_status'] == 'unconfirmed'
        assert runtime.room_status()['commands'][0] == result
    assert len(client.published) == 3  # Two arm reads and one press; no replay.
    runtime.close()
    with runtime.conversation('after-close', 'desktop'):
        with pytest.raises(RuntimeError, match='Home 已停止'):
            runtime.set_room_light(action='on')


def test_daily_detection_persists_without_replaying_old_controls_or_stopping_wake_camera(tmp_path):
    saved=[]
    alarms = SimpleNamespace(bedtime=None, prepare_environment=lambda:None, refresh_bedtime=lambda:False,
        wake_active=lambda:False,
        camera_required=lambda:True, step=lambda *a, **k:None, snapshot=lambda:{}, close=lambda:None,
        conversation=lambda *a, **k:nullcontext())
    runtime, _, _ = room(tmp_path, save=saved.append, alarms=alarms)
    runtime.mqtt.drain()  # Discard the synthetic bridge-connect event.
    runtime._inputs.consume([('presence', SensorObservation('room', True, 1000, 1000, 1100))], 1000)
    runtime.step()
    assert runtime.camera.reasons == {'occupancy','wake_alarm'}
    with runtime.conversation('disable', 'mobile'):
        disabled=runtime.set_daily_detection(enabled=False)
    runtime.step()
    assert runtime.camera.reasons == {'wake_alarm'}
    with runtime.conversation('enable', 'qq'):
        assert runtime.set_daily_detection(enabled=True)['saved']
    with runtime.conversation('disable', 'mobile'):
        assert runtime.set_daily_detection(enabled=False) == disabled
    assert runtime.config.daily_detection_enabled is True and saved == [False,True]
    runtime.step()
    assert runtime.camera.reasons == {'occupancy','wake_alarm'}
    runtime.close()
