"""Zigbee2MQTT observations and two explicitly bound room-light fingers.

The Z2M generic ZCL read uses a /set topic but calls endpoint.read, never write.
It is private to this adapter; no model or client can supply a cluster/payload.
"""
from collections import deque
from datetime import datetime
import json
import math
import threading
import time

from spica.home.models import SensorObservation
from spica.adapters.home_fingers import RoomLightFingers


class HomeMQTT:
    def __init__(self, config, *, clock=time.monotonic, wall_clock=time.time, client=None):
        if client is None:
            import paho.mqtt.client as mqtt
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, clean_session=True)
        self.config, self.client = config, client
        self.clock, self.wall_clock = clock, wall_clock
        self._lock = threading.Lock()
        self._fingers = RoomLightFingers(config,
            publish=lambda *args, **kwargs: self.client.publish(*args, **kwargs),
            connected=lambda: self.client.is_connected(),
            clock=lambda: self.clock(), wall_clock=lambda: self.wall_clock())
        self._events = deque(maxlen=64)
        self._highwater = {}
        self._observation_generation = 0
        self._clock_offset = self.wall_clock()-self.clock()
        self._filtered = set()
        self._primed = set()
        self._connected_at = float("inf")
        self._next_refresh_at = 0
        self._closed = False
        base = config.mqtt_base_topic.rstrip("/")
        self._topics = {f"{base}/{config.presence_device}": (("presence", "occupancy"), ("illuminance", "illuminance"))}
        self._door_topic = f'{base}/{config.door_device}/home/contact'
        self._door_ieee = None
        self._bridge = f"{base}/bridge/state"
        self._info = f"{base}/bridge/info"
        self._devices = f"{base}/bridge/devices"
        self._read_topic = f"{base}/{config.presence_device}/set"
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.connect_timeout = 2
        client.reconnect_delay_set(min_delay=1, max_delay=10)

    def _emit(self, event, *, generation=None):
        with self._lock:
            if not self._closed and (generation is None or generation == self._observation_generation):
                self._events.append(event)

    def _check_clock(self):
        """A wall-clock step starts new evidence, never a relaxed replay filter."""
        with self._lock:
            if self._closed:
                return self._observation_generation, self._highwater, self._primed, self._fingers.epoch
            wall = self.wall_clock()
            offset = wall-self.clock()
            # Compare adjacent samples so normal gradual clock discipline does
            # not discard observations. One second also exceeds sampling jitter.
            if abs(offset-self._clock_offset) > 1:
                self._observation_generation += 1
                # Replace these objects: an in-flight old callback must not
                # repopulate the new generation's watermarks/cache barrier.
                self._highwater, self._primed = {}, set()
                self._next_refresh_at = 0
                if math.isfinite(self._connected_at):
                    self._connected_at = wall
                self._events.clear()
                self._events.append(('connection', 'connected' if math.isfinite(self._connected_at) else 'disconnected'))
                self._fingers.invalidate_reports()
            self._clock_offset = offset
            # Capture the device receive epoch before JSON parsing too. An old
            # callback must not populate reports after a concurrent clock reset.
            return self._observation_generation, self._highwater, self._primed, self._fingers.epoch

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            self._emit(("connection", "unavailable"))
            return
        self._check_clock()
        self._connected_at = self.wall_clock()
        self._filtered.clear()
        self._primed.clear()
        self._door_ieee = None
        self._next_refresh_at = 0
        self._fingers.reset_connection(connected=True)
        topics = (*self._topics, self._door_topic, self._bridge, self._info,
                  self._devices, *self._fingers.topics)
        client.subscribe([(topic, 0) for topic in topics])
        self._emit(("connection", "connected"))

    def _on_disconnect(self, *args):
        self._connected_at = float("inf")
        self._filtered.clear()
        self._primed.clear()
        self._door_ieee = None
        self._fingers.reset_connection(connected=False)
        self._emit(("connection", "disconnected"))

    def _on_message(self, client, userdata, message):
        generation, highwater, primed, finger_epoch = self._check_clock()
        try:
            payload = json.loads(message.payload)
            if message.topic == self._devices and isinstance(payload, list):
                doors = [device['ieee_address'] for device in payload if isinstance(device, dict)
                         and device.get('friendly_name') == self.config.door_device
                         and device.get('interview_completed') is True
                         and (device.get('definition') or {}).get('model') == 'SNZB-04P'
                         and isinstance(device.get('ieee_address'), str)]
                self._door_ieee = doors[0] if len(doors) == 1 else None
                self._fingers.bind_devices(payload)
                return
            if not isinstance(payload, dict):
                return
            if message.topic == self._door_topic:
                # This topic contains one real IAS notification, never a merged
                # state/last_seen snapshot. Its first event needs no priming drop.
                stamp, contact = payload.get('observed_at'), payload.get('contact')
                if (message.retain or self._door_ieee is None
                        or payload.get('ieee_address') != self._door_ieee
                        or type(stamp) not in (int, float) or not math.isfinite(stamp)
                        or type(contact) is not bool):
                    return
                age, ttl = self.wall_clock()-stamp, self.config.observation_ttl_seconds
                if (not 0 <= age <= ttl or stamp < self._connected_at
                        or stamp <= highwater.get('door', 0)):
                    return
                highwater['door'] = stamp
                now = self.clock()
                self._emit(('door_event', SensorObservation(message.topic, contact, stamp, now, now+ttl-age)),
                           generation=generation)
                return
            if self._fingers.observe(message.topic, payload, retained=message.retain, epoch=finger_epoch):
                return
            if message.topic == self._info:
                configuration = payload.get("config")
                if not isinstance(configuration, dict):
                    return
                devices = configuration.get("devices", {})
                if not isinstance(devices, dict):
                    return
                filtered = set()
                for topic, fields in self._topics.items():
                    friendly = topic.rsplit("/", 1)[-1]
                    for name, field in fields:
                        if any(isinstance(device, dict) and device.get("friendly_name") == friendly
                               and "^"+field+"$" in device.get("filtered_cache", [])
                               for device in devices.values()):
                            filtered.add(name)
                if filtered != self._filtered:
                    self._filtered = filtered
                    self._primed.clear()
                    self._next_refresh_at = 0
                    self._emit(("connection", "connected" if "presence" in filtered else "cache_filter_required"))
                elif "presence" not in filtered:
                    self._emit(("connection", "cache_filter_required"))
                return
            if message.topic == self._bridge:
                if payload.get("state") == "offline":
                    self._fingers.bridge_state(False)
                    self._connected_at = float("inf")
                    self._filtered.clear()
                    self._primed.clear()
                    self._emit(("connection", "bridge_offline"))
                elif payload.get("state") == "online":
                    self._fingers.bridge_state(True)
                    self._connected_at = self.wall_clock()
                    self._primed.clear()
                    self._next_refresh_at = 0
                    self._emit(("connection", "connected"))
                # Even an online retained bridge does not refresh sensor values.
                return
            if message.retain or message.topic not in self._topics:
                return
            fields = []
            for name, field in self._topics[message.topic]:
                if name not in self._filtered:
                    continue
                if name not in primed:
                    # Z2M evicts filtered cache on any non-retained publication,
                    # including one with no usable timestamp or field value.
                    primed.add(name)
                    self._next_refresh_at = min(self._next_refresh_at, self.clock()+1)
                else:
                    fields.append((name, field))
            if not fields:
                return
            timestamp = payload.get("last_seen")
            if isinstance(timestamp, str):
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return
                stamp = parsed.timestamp()
            elif type(timestamp) in (int, float):
                stamp = timestamp / 1000 if timestamp > 100_000_000_000 else timestamp
            else:
                return
            age = self.wall_clock() - stamp
            if not math.isfinite(stamp) or age < 0 or stamp < self._connected_at:
                return
            for name, field in fields:
                value = payload.get(field)
                ttl = self.config.lights.illuminance_ttl_seconds if name == 'illuminance' else self.config.observation_ttl_seconds
                valid = (type(value) in (int, float) and math.isfinite(value) and value >= 0
                         if name == 'illuminance' else type(value) is bool)
                if not valid or age > ttl or stamp <= highwater.get(name, 0):
                    continue
                highwater[name] = stamp
                now = self.clock()
                self._emit((name, SensorObservation(message.topic, value, stamp, now, now+ttl-age)), generation=generation)
        except (ValueError, TypeError, OverflowError, UnicodeDecodeError):
            # A bad report must not terminate paho's sole network loop.
            return

    def start(self):
        self.client.connect_async(self.config.mqtt_host, self.config.mqtt_port, keepalive=15)
        self.client.loop_start()

    def invalidate_observations(self):
        """Same-process suspend is a new freshness boundary, without device writes."""
        self._check_clock()
        with self._lock:
            self._observation_generation += 1
            self._fingers.invalidate_reports(clear=False)
            self._connected_at = self.wall_clock()
            self._primed = set()
            self._next_refresh_at = 0
            self._events.clear()
            self._events.append(('connection', 'resumed'))

    def drain(self):
        self._check_clock()
        if (not self._closed and "presence" in self._filtered
                and math.isfinite(self._connected_at) and self.clock() >= self._next_refresh_at):
            self._next_refresh_at = self.clock() + 60
            self.client.publish(self._read_topic, json.dumps({"read": {
                "cluster": "msOccupancySensing", "attributes": ["occupancy"]}}),
                qos=0, retain=False)
            if 'illuminance' in self._filtered:
                self.client.publish(self._read_topic.removesuffix('/set')+'/get',
                                    '{"illuminance":""}', qos=0, retain=False)
        with self._lock:
            events = list(self._events)
            self._events.clear()
            return events

    def close(self):
        with self._lock:
            self._closed = True
            self._events.clear()
            self._fingers.close()
        self.client.disconnect()
        self.client.loop_stop()

    def configure_light_press(self):
        return self._fingers.configure()

    def light_status(self):
        return self._fingers.status()

    def light_readiness(self, action='on'):
        return self._fingers.readiness(action)

    def refresh_light_state(self):
        self._fingers.refresh()

    def press_room_light(self, action, claim, *, deadline=None):
        return self._fingers.press(action, claim, deadline=deadline)

    def press_light(self, claim, *, deadline=None):
        # Original H2 seam and not-ready semantics remain compatible.
        result = self.press_room_light('on', claim, deadline=deadline)
        return 'not_ready' if result == 'not_configured' else result
