"""Single Home owner: sensor freshness, camera demand and one wake per visit."""
from dataclasses import asdict
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import json
import logging
import threading
import time

from spica.home.models import RoomObservation
from spica.home.perception import DeskVisit, RoomLocator
from spica.home.profile import HomeProfile, load_profile, save_profile
from spica.home.room_input import RoomInputs
from spica.home.scenes import HomeScenes

logger = logging.getLogger(__name__)


class HomeRuntime:
    def __init__(self, config, mqtt, camera, display, profile, *, clock=time.monotonic, alarms=None,
                 store=None, save_detection=None, wall_clock=time.time, power=None):
        self.config, self.mqtt, self.camera, self.display = config, mqtt, camera, display
        self.clock = clock
        self.wall_clock = wall_clock
        self.alarms = alarms
        self.power = power
        self._power_stamp = None
        self._resume_error = ''
        self.store, self.save_detection = store, save_detection
        self._origin = ContextVar('home_room_origin', default=None)
        self._command_lock = threading.RLock()
        self._step_lock = threading.RLock()
        self._camera_preview = None
        self._preview_packet = None
        self._preview_frame = None
        self.locator, self.visit = RoomLocator(config, profile), DeskVisit(config)
        self._inputs = RoomInputs()
        self._door_camera_since = None
        self._door_camera_until = 0.
        self._room = RoomObservation(reason="calibration_required" if profile is None else "no_observation")
        self._idle_since = None
        self._generation = 0
        self._resume_generation = 0
        self._last_vision = float("-inf")
        self._vision = None
        self._display_result = None
        self._wake_attempts = 0
        self._stop = threading.Event()
        self._thread = None
        self._resources_closed = False
        self._resources_done = threading.Event()
        self._cleanup_lock = threading.Lock()
        self._released_resources = set()
        self._close_error = None
        self._lock = threading.Lock()
        self._snapshot = {}
        self.scenes = None
        self.greetings = None
        if store is not None:
            self.install_scenes()
        self._publish()

    def install_scenes(self):
        self.scenes = HomeScenes(self.config, self.store, self._automatic_light,
                                 clock=self.clock, wall_clock=self.wall_clock)

    def _automatic_light(self, action, source, identity):
        if self._stop.is_set() or self._resources_closed:
            return dict(press_status='not_ready', light_state='unknown')
        return self._issue_light(action, source, identity, 'home-auto', lambda: None)

    def scene_allowed(self):
        return (not self._stop.is_set() and not self._resources_closed and not self._resume_error
            and self._camera_preview is None and not (
                self.alarms and (self.alarms.bedtime and self.alarms.bedtime.suppress_daily or self.alarms.wake_active())))





    @contextmanager
    def conversation(self, request_id, conversation_id, *, user_text='', **kwargs):
        token = self._origin.set((request_id, conversation_id, user_text))
        try:
            with (self.alarms.conversation(request_id, conversation_id, user_text=user_text, **kwargs)
                  if self.alarms else nullcontext()) as event_binding:
                yield event_binding
        finally:
            self._origin.reset(token)

    def _require_origin(self):
        origin = self._origin.get()
        if origin is None:
            raise PermissionError('家庭操作需要已认证的本人请求')
        if self._stop.is_set() or self._resources_closed:
            raise RuntimeError('Home 已停止，未执行家庭操作')
        return origin

    def room_status(self):
        self._require_origin()
        snapshot = self.snapshot()
        room = snapshot.get('room') or {}
        return dict(connection=snapshot.get('connection'), room_occupied=snapshot.get('room_occupied'),
            door_contact=snapshot.get('door_contact'), door_scope='bedroom',
            daily_detection_enabled=self.config.daily_detection_enabled,
            camera_running=snapshot.get('camera_running'), camera_reasons=snapshot.get('camera_reasons'),
            desk_occupied=room.get('desk_occupied'), bed_occupied=room.get('bed_occupied'),
            outside_bed_occupied=room.get('outside_bed_occupied'), observation_reason=room.get('reason'),
            lights=self.mqtt.light_status(), light_state='unknown',
            scenes=snapshot.get('scenes'), illuminance=snapshot.get('illuminance'),
            commands=self.store.recent_commands() if self.store else [])

    def set_room_light(self, *, action, _source='manual'):
        if action not in {'on', 'off'}:
            raise ValueError('灯组操作只支持明确的 on/off')
        with self._command_lock:
            origin = self._require_origin()
            return self._issue_light(action, _source, origin[0], origin[1], self._require_origin)

    def _issue_light(self, action, source, request_id, conversation_id, authorize):
        if self.store is None:
            raise RuntimeError('家庭操作记录不可用，未按压')
        key = json.dumps(['light', request_id, conversation_id, action] + ([source] if source != 'manual' else []))
        previous = self.store.command_result(key)
        if previous is not None:
            return previous
        device = self.config.lights.on_device if action == 'on' else self.config.lights.off_device
        receipt = dict(kind='light', request_id=request_id, action=action, device=device,
            requested_at=self.wall_clock(), source=source, press_status='unconfirmed', light_state='unknown')
        if not self.store.claim_command(key, receipt):
            return self.store.command_result(key)
        if self.scenes is not None:
            self.scenes.light_intent(action, source)
        def still_authorized():
            if self._stop.is_set() or self._resources_closed:
                return False
            return authorize()
        try:
            receipt['press_status'] = self.mqtt.press_room_light(action, still_authorized)
        except Exception:
            logger.exception('Home light request result uncertain; do not repeat the press')
        self.store.finish_command(key, receipt)
        return receipt

    def prepare_alarm_light(self, claim, *, deadline):
        remaining = deadline-self.clock()
        if remaining <= 0 or not self._command_lock.acquire(timeout=remaining):
            return 'preparation_timeout'
        try:
            if self.clock() >= deadline:
                return 'preparation_timeout'
            recorded = False
            def claimed():
                nonlocal recorded
                if self._stop.is_set() or self._resources_closed or self.clock() >= deadline:
                    return False
                if claim() is False:
                    return False
                if not recorded and self.scenes is not None:
                    self.scenes.light_intent('on', 'wake_alarm')
                recorded = True
            return self.mqtt.press_light(claimed, deadline=deadline)
        finally:
            self._command_lock.release()

    def set_daily_detection(self, *, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled 必须为布尔值')
        with self._command_lock:
            origin = self._require_origin()
            if self.save_detection is None or self.store is None:
                raise RuntimeError('房间识别设置不可用')
            key = json.dumps(['detection', origin[0], origin[1], enabled])
            previous = self.store.command_result(key)
            if previous is not None:
                return previous
            receipt = dict(kind='detection', request_id=origin[0], requested_at=time.time(),
                           daily_detection_enabled=enabled, status='unconfirmed', saved=False)
            if not self.store.claim_command(key, receipt):
                return self.store.command_result(key)
            self.save_detection(enabled)
            self.config.daily_detection_enabled = enabled
            receipt.update(status='applied', saved=True,
                detail='已更新日常识别；叫醒期间的相机需求独立保留。相机状态请查询。')
            self.store.finish_command(key, receipt)
            return receipt

    def observe_reply(self, event):
        if self.alarms:
            self.alarms.observe_reply(event)

    def uses_home_audio(self, request_id):
        return bool(self.alarms and self.alarms.uses_home_audio(request_id))

    def observe_text_delivery(self, request_id, delivered):
        if self.alarms:
            self.alarms.observe_text_delivery(request_id, delivered)

    def start(self):
        if self._thread is not None:
            return
        try:
            self.mqtt.start()
            self._thread = threading.Thread(target=self._run, name="spica-home", daemon=True)
            self._thread.start()
        except Exception:
            self._close_resources()
            raise

    def _visual_person(self, now):
        return (self._vision is not None and self._room.has_person
                and 0 <= now-self._last_vision <= self.config.frame_ttl_seconds)

    def _preview_blocked(self):
        if self._resume_error:
            return True
        return bool(self.alarms and (self.alarms.camera_required() or self.alarms.preparation_required()
            or self.alarms.bedtime and self.alarms.bedtime.suppress_daily))

    def _reset_preview_evidence(self):
        self._preview_packet = self._vision = None
        self._preview_frame = None
        self._last_vision = float('-inf')
        self._room = RoomObservation(reason='camera_preview' if self._camera_preview else 'camera_starting')
        self.locator.reset()
        self.visit.observe(self._room, self.clock())
        self._door_camera_since, self._door_camera_until = None, 0.
        self._idle_since = self.clock()
        if self.scenes is not None:
            self.scenes.reset_observations()

    def begin_camera_preview(self, owner, session_id, *, camera_settings, raw_preview=False):
        """The core keeps physical ownership; only a desktop-scoped view is lent."""
        from spica.config.home import HomeConfig
        if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
            raise ValueError('无效的相机预览会话')
        if type(raw_preview) is not bool or not isinstance(camera_settings, dict) or set(camera_settings) - {
                'camera_device', 'camera_width', 'camera_height', 'camera_fps'}:
            raise ValueError('相机预览只接受设备、分辨率与帧率')
        config = HomeConfig.model_validate({**self.config.model_dump(), **camera_settings})
        with self._step_lock:
            if self._stop.is_set() or self._resources_closed:
                raise RuntimeError('Home 已停止，请重启后再打开相机')
            if self._camera_preview is not None:
                if self._camera_preview == (owner, session_id):
                    return {'active': True}
                raise RuntimeError('另一个相机预览尚未关闭')
            if self._preview_blocked():
                raise RuntimeError('Home 正在准备叫醒、叫醒或晚安，请结束后再校准')
            self.camera.configure_preview(config, None, preview=not raw_preview, raw_preview=raw_preview)
            self._camera_preview = (owner, session_id)
            self.camera.require('settings_preview', True)
            self._reset_preview_evidence()
            self._publish()
            return {'active': True}

    def poll_camera_preview(self, owner, session_id):
        with self._step_lock:
            if self._stop.is_set() or self._resources_closed or self._camera_preview != (owner, session_id):
                return {'active': False, 'detail': '相机预览已结束，Home 已收回相机。'}
            packet, self._preview_packet = self._preview_packet, None
            return {'active': True, 'packet': packet}

    def save_camera_preview(self, owner, session_id, profile_data):
        with self._step_lock:
            if self._stop.is_set() or self._resources_closed or self._camera_preview != (owner, session_id):
                raise RuntimeError('校准会话已结束，未保存区域，请重新打开校准')
            if self.camera.raw_preview:
                raise ValueError('相机预览不能保存区域，请打开区域校准')
            frame = self._preview_frame
            if frame is None or frame[0].error or not 0 <= self.clock()-frame[0].captured_mono <= self.config.frame_ttl_seconds:
                raise ValueError('请等待相机有效新画面后保存区域')
            profile = HomeProfile.model_validate(profile_data)
            config = self.camera.config
            if (profile.camera_device != config.camera_device or profile.image_size != frame[1]
                    or profile.image_size != (config.camera_width, config.camera_height)):
                raise ValueError('区域与当前相机或实际分辨率不匹配，未保存')
            save_profile(self.config.data_directory, profile)
            return {'saved': True}

    def end_camera_preview(self, owner, session_id=None):
        with self._step_lock:
            current = self._camera_preview
            if current is None or current[0] != owner or session_id is not None and current[1] != session_id:
                return {'active': False}
            if self._stop.is_set() or self._resources_closed:
                self._camera_preview = None
                self._preview_packet = None
                return {'active': False, 'detail': 'Home 已停止，相机由后台退出流程回收。'}
            profile = self.locator.profile
            detail = '相机预览已关闭，Home 已恢复。'
            try:
                saved = load_profile(self.config.data_directory, self.config.camera_device)
                if saved.image_size != (self.config.camera_width, self.config.camera_height):
                    raise ValueError('相机分辨率已变更')
                profile = saved
                detail = 'Home 已恢复，并已加载当前相机的桌区／床区。'
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                detail = 'Home 已按原配置恢复；保存的区域与运行配置不匹配或不可读，请检查设置并重启。'
            # On failed native cleanup keep the session owned: never start a
            # competing capture or claim successful restoration.
            self.camera.configure_preview(self.config, profile)
            self.locator.profile = profile
            self._camera_preview = None
            self._reset_preview_evidence()
            self._publish()
            return {'active': False, 'detail': detail}

    def step(self):
        with self._step_lock:
            self._step()

    def _step(self):
        now = self.clock()
        resumed = False
        if self.power is not None:
            try:
                stamp = self.power.resume_stamp()
                resumed = bool(self._resume_error or self._power_stamp is not None and stamp != self._power_stamp)
                if resumed and not self.power.resume_ready():
                    self._resume_error = 'host_resume_in_progress'
                else:
                    self._power_stamp, self._resume_error = stamp, ''
            except Exception as exc:
                resumed = not self._resume_error
                self._resume_error = type(exc).__name__+': '+str(exc)
                if resumed:
                    logger.warning('Home resume observation unavailable: %s', self._resume_error)
            if self._resume_error:
                # Driver restore can outlast camera shutdown's bounded waits.
                # Keep the old process owned until logind finishes, and expose
                # no old observations or hardware actions during that interval.
                self._inputs.reset()
                self._vision = None
                self._room = RoomObservation(reason=self._resume_error)
                self._publish()
                return
        bedtime = self.alarms.bedtime if self.alarms else None
        if self.alarms is not None:
            try:
                self.alarms.refresh_bedtime()
            except Exception as exc:
                logger.exception('Home power observation failed; daily sensing remains independent')
                self.alarms.fail(exc)
        if bedtime and bedtime.resume_generation != self._resume_generation:
            self._resume_generation = bedtime.resume_generation
            resumed = True
        if self._camera_preview is not None and (resumed or self._preview_blocked()):
            self.end_camera_preview(*self._camera_preview)
        if resumed:
            self._inputs.reset()
            self._vision = None
            self._door_camera_since, self._door_camera_until = None, 0.
            self._last_vision = float('-inf')
            self._room = RoomObservation(reason='host_resumed')
            self.locator.reset()
            self.visit.vacant()
            self.mqtt.invalidate_observations()
            self.camera.reset_after_resume()
            self.camera.require('occupancy', False)
            self.camera.require('door', False)
            self._idle_since = now
            if self.scenes is not None:
                self.scenes.reset_observations()
        if self.alarms is not None:
            try:
                self.alarms.prepare_environment()
            except Exception as exc:
                logger.exception('Home wake preparation failed; daily sensing remains independent')
                self.alarms.fail(exc)
        alarm_camera = self.alarms is not None and self.alarms.camera_required()
        daily = (self.config.daily_detection_enabled and not self._resume_error and self._camera_preview is None
            and not (self.alarms and self.alarms.bedtime and self.alarms.bedtime.suppress_daily))
        if self.alarms is not None:
            self.camera.require('wake_alarm', alarm_camera)
        if hasattr(self.camera, 'prepare_models'):
            self.camera.prepare_models(bool(self.alarms and self.alarms.preparation_required()))
        events = self.mqtt.drain()
        # Preparation and sensor I/O can outlast the evidence's validity.
        now = self.clock()
        inputs = self._inputs.consume(events, now)
        for change in inputs.changes:
            if change.kind == 'reset':
                self._door_camera_since, self._door_camera_until = None, 0.
                if not alarm_camera:
                    self.locator.reset()
                    self._room = RoomObservation(reason="sensor_connection_changed")
                self.visit.observe(self._room, now)
            elif change.kind == 'opening' and daily:
                self._door_camera_since = change.observation.received_at
                self._door_camera_until = self._door_camera_since + self.config.door_camera_seconds
        if not daily:
            self._door_camera_since, self._door_camera_until = None, 0.
        door_camera = daily and now < self._door_camera_until
        self.camera.require('door', door_camera)
        occupied = inputs.occupied
        # A current person in the camera is independent positive evidence.
        # A false/expired sensor report cannot close that person's camera.
        if daily and (occupied is True or self._visual_person(now)):
            self._idle_since = None
            self.camera.require("occupancy", True)
        else:
            if self._idle_since is None:
                self._idle_since = now
            if not daily or now-self._idle_since >= self.config.camera_idle_seconds:
                self.camera.require("occupancy", False)
        capture_requested = bool(self.camera.reasons)
        packet = self.camera.poll()
        if self._camera_preview is not None:
            if packet is not None:
                self._preview_packet = packet
                self._preview_frame = (packet[0], packet[3])
            packet = None  # Calibration observations are never product evidence.
        if self.camera.generation != self._generation:
            self._generation = self.camera.generation
            self.locator.reset()
            self._room = RoomObservation(reason="camera_starting")
        if packet is not None:
            frame = packet[0]
            self._vision = frame
            self._room = self.locator.observe(frame, self.clock())
            self._last_vision = frame.captured_mono
        elif self.clock()-self._last_vision > self.config.frame_ttl_seconds:
            self._vision = None
            self._room = RoomObservation(reason='camera_preview' if self._camera_preview else self.camera.error or "no_fresh_frame")
            self.locator.reset()
            self.visit.observe(self._room, self.clock())
        # Resolve a negative report against this tick's camera result before
        # rearming the visit; a delayed poll may already contain a fresh person.
        if (self._inputs.snapshot(self.clock()).occupied is False and not door_camera
                and not self._visual_person(self.clock()) and any(
                    change.kind == 'presence' and change.observation.value is False
                    for change in inputs.scene_changes)):
            self.visit.vacant()
        if packet is not None:
            # Door/presence/alarm demand starts the one camera. Valid camera
            # evidence then stands on its own; sensor timing is not a second
            # admission gate for a continuously observed desk visit.
            eligible = daily and capture_requested
            if eligible and self._visual_person(self.clock()):
                self._idle_since = None
                self.camera.require('occupancy', True)
            trigger = self.visit.observe(self._room if eligible else RoomObservation(), frame.captured_mono)
            if trigger and eligible and self.display is not None and not self._stop.is_set():
                self._wake_attempts += 1
                result = self.display.wake()
                self._display_result = asdict(result)
                logger.info("Home desk visit: %s", result.status)
        if self.alarms is not None:
            try:
                self.alarms.step(self._room, packet[0].captured_mono if packet is not None else None, self._generation,
                    environment_ready=self._vision is not None and not self._vision.error
                    and 0 <= self.clock()-self._last_vision <= self.config.frame_ttl_seconds)
            except Exception as exc:
                logger.exception('Home wake alarm failed; daily sensing remains independent')
                self.alarms.fail(exc)
        if self.scenes is not None:
            with self._command_lock:
                enabled = self.scene_allowed()
                if self.greetings is not None:
                    self.greetings.step()
                self.scenes.step(self._inputs.snapshot(self.clock()), room=self._room,
                    frame_mono=self._last_vision if self._vision is not None else None, enabled=enabled)
        self._publish()

    def _publish(self):
        now, wall_now = self.clock(), self.wall_clock()
        inputs = self._inputs.snapshot(now)
        snapshot = {
            "updated_at": wall_now,
            "running": self._thread is not None and self._thread.is_alive() and not self._stop.is_set() and not self._resources_closed,
            "daily_detection_enabled": self.config.daily_detection_enabled,
            "sensors": {name: ({"observed_at": observation.observed_at,
                "valid_until": wall_now + observation.valid_until - now} if observation is not None else None)
                for name, observation in (("presence", inputs.presence), ("door", inputs.door), ("illuminance", inputs.illuminance))},
            "connection": inputs.connection,
            "resume_observation_error": self._resume_error,
            "room_occupied": inputs.occupied,
            "door_contact": inputs.value(inputs.door),
            "illuminance": inputs.lux,
            "scenes": self.scenes.snapshot() if self.scenes is not None else None,
            "greetings": self.greetings.snapshot() if self.greetings is not None else None,
            "presence_observed_at": inputs.presence.observed_at if inputs.presence else None,
            "room": asdict(self._room),
            "camera_running": getattr(self.camera, 'capturing', self.camera.running),
            "camera_reasons": sorted(self.camera.reasons),
            "camera_generation": self.camera.generation,
            "camera_error": self.camera.error,
            "camera_preview": self._camera_preview is not None,
            "vision": ({
                **{key: value for key, value in vars(self._vision).items() if key != 'people'},
                "people": [{key: value for key, value in vars(person).items() if key != 'appearance'}
                           for person in self._vision.people],
            } if self._vision is not None else None),
            "wake_attempts": self._wake_attempts,
            "display_result": self._display_result,
            "wake": self.alarms.snapshot() if self.alarms is not None else None,
        }
        with self._lock:
            self._snapshot = snapshot

    def snapshot(self, *, details=False):
        import copy
        with self._lock:
            result = copy.deepcopy(self._snapshot)
        if details:
            # UI reads existing records only; it cannot materialize alarms or
            # acquire camera / lamp / playback resources by viewing this page.
            plan = self.store.plan_snapshot(self.wall_clock()) if self.store else {}
            result['next_alarm'] = plan.get('next')
            result['alarm_conflicts'] = bool(plan.get('fixed_conflicts'))
            result['commands'] = self.store.recent_commands() if self.store else []
        return result

    def _run(self):
        try:
            while not self._stop.is_set():
                self.step()
                self._stop.wait(.1)
        except Exception:
            logger.exception("Home stopped after an observation failure")
            self._room = RoomObservation(reason="home_failed")
        finally:
            try:
                self._close_resources()
            finally:
                self._inputs.reset('stopped')
                if self._room.reason != "home_failed":
                    self._room = RoomObservation(reason="home_stopped")
                self._publish()

    def _close_resources(self, timeout=None):
        with self._command_lock:
            self._resources_closed = True
        # Bedtime takes alarm -> command locks. Never wait for alarms or for
        # playback release while holding the command admission lock.
        acquired = (self._cleanup_lock.acquire() if timeout is None else
                    self._cleanup_lock.acquire(timeout=max(0., timeout)))
        if not acquired:
            raise RuntimeError('Home resource cleanup remains pending')
        try:
            if self._resources_done.is_set():
                return
            retry = self._close_error is not None
            self.locator.reset()
            self._vision = None
            errors = []
            for name, resource in (('greetings', self.greetings), ('camera', self.camera),
                                   ('mqtt', self.mqtt), ('alarms', self.alarms), ('power', self.power)):
                if name in self._released_resources:
                    continue
                try:
                    if name == 'greetings':
                        with self._command_lock:
                            if resource is not None:
                                resource.close()
                    elif name == 'power':
                        if errors:
                            continue  # Keep resume observation while another owner still drains.
                        if resource is not None and callable(getattr(resource, 'close', None)):
                            resource.close()
                    elif resource is not None:
                        resource.close()
                except Exception as exc:
                    errors.append(exc)
                else:
                    self._released_resources.add(name)
            self._close_error = errors[0] if errors else None
            if self._close_error is not None:
                if retry:
                    raise RuntimeError('Home resource cleanup failed') from self._close_error
                raise self._close_error
            self._resources_done.set()
        finally:
            self._cleanup_lock.release()

    def close(self, timeout=None):
        self._stop.set()
        if self._thread is not None:
            limit = 15 if self.store is not None or self.alarms is not None else 9
            self._thread.join(limit if timeout is None else min(limit, max(0., timeout)))
            if self._thread.is_alive():
                raise RuntimeError("Home shutdown did not finish within its I/O bounds")
            if self._close_error is not None:
                self._close_resources(timeout)
        else:
            self._close_resources(timeout)
