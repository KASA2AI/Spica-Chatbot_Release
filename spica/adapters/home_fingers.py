"""The installed two-finger light group; MQTT transport is supplied by its owner."""
from datetime import datetime
import json
import math
import threading


class RoomLightFingers:
    # Preserve the established total preparation and mechanical settle bounds.
    TIMEOUT_SECONDS = 8.0
    SETTLE_SECONDS = .5

    def __init__(self, config, *, publish, connected, clock, wall_clock):
        self.config, self.clock, self.wall_clock = config, clock, wall_clock
        self._publish, self._connected = publish, connected
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._devices = {'on': config.lights.on_device, 'off': config.lights.off_device}
        base = config.mqtt_base_topic.rstrip('/')
        self._topics = {action: f'{base}/{device}' for action, device in self._devices.items() if device}
        self._bound = set()
        self._bridge_online = self._closed = False
        self._settings, self._reports = {}, {}
        self._epoch = 0

    @property
    def topics(self):
        return tuple(self._topics.values())

    @property
    def epoch(self):
        with self._lock:
            return self._epoch

    def reset_connection(self, *, connected):
        with self._changed:
            self._bound.clear()
            self._bridge_online = False
            if connected:
                self._settings.clear()
                self._reports.clear()
            self._epoch += 1
            self._changed.notify_all()

    def invalidate_reports(self, *, clear=True):
        with self._changed:
            if clear:
                self._reports.clear()
                self._settings.clear()
            self._epoch += 1
            self._changed.notify_all()

    def bridge_state(self, online):
        with self._changed:
            self._bridge_online = online
            if not online:
                self._epoch += 1
            self._changed.notify_all()

    def bind_devices(self, devices):
        bound = {action for action, device_id in self._devices.items() if device_id
            and any(isinstance(device, dict) and device.get('ieee_address') == device_id
                and device.get('friendly_name') == device_id
                and (device.get('definition') or {}).get('model') == 'TS0001_fingerbot'
                and device.get('interview_completed') is True for device in devices)}
        with self._changed:
            if bound != self._bound:
                self._epoch += 1
            self._bound = bound
            self._settings = {key: value for key, value in self._settings.items() if key in bound}
            self._changed.notify_all()
        for action in bound:
            self._publish(self._topics[action]+'/get', '{"state":""}', qos=0, retain=False)

    def observe(self, topic, payload, *, retained, epoch):
        action = next((key for key, value in self._topics.items() if value == topic), None)
        if action is None:
            return False
        fields = {key: payload[key] for key in ('mode', 'upper', 'lower', 'reverse', 'state', 'battery') if key in payload}
        timestamp = payload.get('last_seen')
        if isinstance(timestamp, str):
            parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            stamp = parsed.timestamp() if parsed.tzinfo is not None else float('-inf')
        elif type(timestamp) in (int, float):
            stamp = timestamp / 1000 if timestamp > 100_000_000_000 else timestamp
        else:
            stamp = float('-inf')
        with self._changed:
            if self._closed or epoch != self._epoch:
                return True
            if (not retained and math.isfinite(stamp)
                    and 0 <= self.wall_clock()-stamp <= self.config.observation_ttl_seconds):
                reports = self._reports.setdefault(action, {})
                for key, value in fields.items():
                    if stamp > reports.get(key, (float('-inf'), None))[0]:
                        reports[key] = (stamp, value)
                        self._settings.setdefault(action, {})[key] = value
            self._changed.notify_all()
        return True

    def close(self):
        with self._changed:
            self._closed = True
            self._changed.notify_all()

    def configure(self):
        """Explicit H2 installation operation; never called by daily tools."""
        if self._closed or 'on' not in self._bound or not self._bridge_online or not self._connected():
            raise RuntimeError('已绑定的开灯手指尚未就绪')
        result = self._publish(self._topics['on']+'/set',
            json.dumps(self._target()), qos=0, retain=False)
        if result.rc != 0:
            raise RuntimeError('开灯手指配置未发出')
        return 'configuration_requested'

    @staticmethod
    def _target():
        # Both installed fingers must press fully and return fully upward.
        # Neither gateway reports nor legacy OFF limits may redefine this.
        return dict(mode='click', upper=0, lower=100)

    def status(self):
        with self._lock:
            online = not self._closed and self._bridge_online and self._connected()
            result = {}
            for action, device in self._devices.items():
                settings = self._settings.get(action, {})
                mode_ready = all(settings.get(key) == value for key,value in self._target().items())
                direction_known = settings.get('reverse') in ('ON', 'OFF')
                result[action] = ('not_configured' if not device else
                    'not_ready' if not online or action not in self._bound or not direction_known else
                    'ready' if mode_ready else
                    'needs_reset' if settings.get('mode') in ('click', 'switch') else 'not_ready')
            return result

    def readiness(self, action='on'):
        """Reported settings and age, not proof of mechanical travel or lamp state."""
        status = self.status()[action]
        with self._lock:
            reports = self._reports.get(action, {})
            settings = dict(self._settings.get(action, {}))
            required = ('state', 'mode', 'upper', 'lower', 'reverse')
            stamp = min((reports.get(key, (0, None))[0] for key in required), default=0)
            age = self.wall_clock()-stamp
            if status in {'ready', 'needs_reset'} and (stamp == 0 or not 0 <= age <= self.config.observation_ttl_seconds):
                status = 'stale'
            return dict(status=status, device=self._devices[action], settings=settings,
                        observed_at=stamp or None, battery_observed_at=reports.get('battery', (None, None))[0],
                        light_state='unknown')

    def refresh(self):
        """Request only genOnOff reads; no reset, configuration write or press."""
        with self._lock:
            topics = tuple(self._topics[action] for action in self._bound)
            online = not self._closed and self._bridge_online and self._connected()
        if online:
            for topic in topics:
                self._publish(topic+'/get', '{"state":""}', qos=0, retain=False)

    def press(self, action, claim, *, deadline=None):
        """Bounded retract/configure/click, owned by the caller's durable claim.

        The callback is idempotent and rechecks authority/deadline at each phase.
        A fresh reported logical state is not a physical arm-position sensor.
        """
        if action not in {'on', 'off'}:
            raise ValueError('灯组操作只支持明确的 on/off')
        if not self._devices[action]:
            return 'not_configured'
        other = 'off' if action == 'on' else 'on'
        fingers = (other, action) if self._devices[other] else (action,)
        with self._lock:
            epoch = self._epoch
            if not all(self._available(finger, epoch) for finger in fingers):
                return 'not_ready'
        limit = self.clock()+self.TIMEOUT_SECONDS
        deadline = limit if deadline is None else min(limit, deadline)

        def admitted():
            # Never invoke owner callbacks while holding the MQTT receive lock.
            if self.clock() >= deadline or claim() is False:
                return False
            with self._lock:
                return self.clock() < deadline and all(
                    self._available(finger, epoch) for finger in fingers)

        prepared = {}
        for finger in fingers:
            status, guard = self._prepare(finger, admitted, deadline)
            if status != 'ready':
                return 'other_finger_'+status if finger != action else status
            prepared[finger] = guard
        if not admitted():
            return 'press_aborted'
        # The other arm must remain prepared while the requested arm is read
        # and configured. A gateway edit during that time cancels this press.
        with self._lock:
            for finger, guard in prepared.items():
                if any(self._settings.get(finger, {}).get(key) != value for key,value in guard.items()):
                    return 'other_finger_configuration_changed' if finger != action else 'configuration_changed'
        pressed = 'ON' if prepared[action]['reverse'] == 'ON' else 'OFF'
        result = self._publish(self._topics[action]+'/set',
            json.dumps({'state':pressed}), qos=0, retain=False)
        return 'requested' if result.rc == 0 else 'unconfirmed'

    def _prepare(self, action, admitted, deadline):
        """Restore this bound arm without issuing its downward press command."""

        def send(payload, expected, *, read=False, guard=None):
            if not admitted():
                return False
            with self._lock:
                if guard and any(self._settings.get(action, {}).get(key) != value for key,value in guard.items()):
                    return False
            sent_at = self.wall_clock()
            result = self._publish(self._topics[action]+('/get' if read else '/set'),
                json.dumps(payload), qos=0, retain=False)
            if result.rc != 0:
                return False
            while admitted():
                with self._changed:
                    settings = self._settings.get(action, {})
                    reports = self._reports.get(action, {})
                    if all(key in reports and reports[key][0] >= sent_at
                            and reports[key][1] == settings.get(key)
                            and (settings[key] in ('ON', 'OFF') if value is None else settings[key] == value)
                            for key, value in expected.items()):
                        return True
                    self._changed.wait(min(.05, max(0, deadline-self.clock())))
            return False

        def settle():
            settled_at = self.clock()+self.SETTLE_SECONDS
            while self.clock() < settled_at:
                if not admitted():
                    return False
                with self._changed:
                    self._changed.wait(min(.05, max(0, settled_at-self.clock())))
            return admitted()

        if not send({'state':''}, {'state':None}, read=True):
            return 'state_unconfirmed', None
        with self._lock:
            settings = dict(self._settings.get(action, {}))
        reverse, mode = settings.get('reverse'), settings.get('mode')
        if reverse not in {'ON', 'OFF'}:
            return 'direction_unknown', None
        if mode not in {'click', 'switch'}:
            return 'mode_unsupported', None
        upper, lower = settings.get('upper'), settings.get('lower')
        if not (type(upper) is int and type(lower) is int and 0 <= upper <= 50 <= lower <= 100 and upper < lower):
            return 'travel_unknown', None
        target = self._target()
        # Tuya DP104: 0=up_on, 1=up_off. Preserve reversal; never guess OFF is up.
        retracted = 'OFF' if reverse == 'ON' else 'ON'
        guard = {'reverse':reverse, 'mode':mode}
        # A wrong click-mode return limit can leave the arm on the wall switch.
        # Use positional mode for its recovery; a click-mode state command is
        # not a dedicated retraction and could operate the opposite light side.
        upper_changed = upper != target['upper']
        recovered = mode == 'switch' or upper_changed
        if upper_changed and mode == 'click':
            if not send({'mode':'switch'}, {'mode':'switch'}, guard=guard) or not settle():
                return 'configuration_unconfirmed', None
            mode = guard['mode'] = 'switch'
            with self._lock:
                settings = dict(self._settings.get(action, {}))
        if mode == 'switch':
            # Select the calibrated return position without increasing press
            # depth. Even an arm at the old upper limit must return to the new one.
            if settings.get('upper') != target['upper']:
                if not send({'upper':target['upper']}, {'upper':target['upper']}, guard=guard):
                    return 'configuration_unconfirmed', None
                settings['upper'] = target['upper']
            if settings['state'] != retracted or upper_changed:
                if not send({'state':retracted}, {'state':retracted}, guard=guard) or not settle():
                    return 'reset_unconfirmed', None
        # Never configure a deeper lower limit while the arm is held against
        # the room switch. Logical return reports cannot prove mechanical travel.
        if mode == 'switch':
            guard['state'] = retracted
            # This device reports default travel after changing modes. Settle
            # that transition before applying calibrated limits; a combined
            # mode/travel write can acknowledge 100 then revert to 85.
            if not send({'mode':'click'}, {'mode':'click'}, guard=guard) or not settle():
                return 'configuration_unconfirmed', None
            guard['mode'] = 'click'
            with self._lock:
                settings = dict(self._settings.get(action, {}))
        changes = {key:value for key,value in target.items() if settings.get(key) != value}
        if changes and not send(changes, changes, guard=guard):
            return 'configuration_unconfirmed', None
        if changes and not settle():
            return 'configuration_unconfirmed', None
        if not admitted():
            return 'press_aborted', None
        with self._lock:
            latest = self._settings.get(action, {})
            if latest.get('reverse') != reverse or any(latest.get(key) != value for key,value in target.items()):
                return 'configuration_changed', None
            if recovered and latest.get('state') != retracted:
                return 'reset_unconfirmed', None
        # A click-mode ON receipt can remain ON after automatic return. It is
        # not evidence of a held arm and must not cause an extra reset stroke.
        return 'ready', dict(target, reverse=reverse, **({'state':retracted} if recovered else {}))

    def _available(self, action, epoch):
        return (not self._closed and self._epoch == epoch and self._bridge_online
                and action in self._bound and self._connected())
