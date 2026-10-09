"""Room-stay policy over accepted inputs; physical commands remain in HomeRuntime."""
import time
import uuid


class HomeScenes:
    VACANT_SECONDS = 300
    WELCOME_SECONDS = 1800

    def __init__(self, config, store, press, *, clock=time.monotonic, wall_clock=time.time,
                 welcome=None, departure=None):
        self.config, self.store, self.press = config, store, press
        self.clock, self.wall_clock = clock, wall_clock
        self.welcome, self.departure = welcome, departure
        self.state = store.room_state()
        self.state.setdefault('manual_off', False)
        self.reset_observations()

    def reset_observations(self):
        # No freshness/timing evidence survives reconnect, restart or suspend.
        self.absent_since = self.off_since = self.dark_since = None
        self._absence_valid_until = None
        self._input_generation = None
        self._visual_stamp = None
        self._welcome_opening = None
        self._ended_absence = None
        self._door_opened = False
        self._closed_room_occupied = False
        self._off_after = self.clock()
        self._enabled = False
        self._light_absence = None
        self._return_light_available = False
        self._reset_departure_camera()

    def _reset_departure_camera(self):
        self._departure_frame = self._departure_source = None
        self._departure_person_since = self._departure_empty_since = None
        self._departure_person_frames = 0
        self._departure_armed = False

    def _camera_departure(self, room, at, now):
        usable = (at is not None and 0 <= now-at <= self.config.frame_ttl_seconds
                  and room.reason in {'observed', 'no_person'}
                  and room.bed_occupied is not None and room.outside_bed_occupied is not None)
        if not usable:
            self._reset_departure_camera()
            return False
        previous = self._departure_frame
        if (self._departure_source != room.source or previous is not None
                and (at < previous or at-previous > self.config.frame_ttl_seconds)):
            self._reset_departure_camera()
            previous = None
        if previous == at:
            return False  # Polling the same image cannot advance either timer.
        self._departure_frame, self._departure_source = at, room.source
        if room.bed_occupied or room.has_person and not room.outside_bed_occupied:
            self._reset_departure_camera()
            return False
        if room.outside_bed_occupied:
            self._departure_empty_since = None
            if self._departure_person_since is None:
                self._departure_person_since = at
            self._departure_person_frames += 1
            if self._departure_person_frames >= 3 and at-self._departure_person_since >= 1:
                self._departure_armed = True
            return False
        self._departure_person_since, self._departure_person_frames = None, 0
        if not self._departure_armed:
            return False
        if self._departure_empty_since is None:
            self._departure_empty_since = at
        if at-self._departure_empty_since < 3:
            return False
        self._departure_armed = False
        self._departure_empty_since = None
        return True

    def light_intent(self, action, source):
        if source == 'manual':
            self.state['manual_off'] = action == 'off'
        if action == 'on':
            self.off_since = None
            self._off_after = self.clock()
            self._return_light_available = False
            if self.absent_since is not None:
                self._light_absence = self.absent_since
        self.state['last_light'] = dict(action=action, source=source, at=self.wall_clock())
        self.store.save_room_state(self.state)

    def _request_light(self, action, source):
        # Claim the intent before sending; an unavailable/uncertain device does
        # not lead to a dark-room retry loop or replay after process restart.
        if ((self.state.get('last_light') or {}).get('action') == action
                and not (action == 'on' and self._return_light_available)):
            return
        self.light_intent(action, source)
        self.press(action, source, uuid.uuid4().hex)

    def step(self, inputs, *, room, frame_mono=None, enabled=True):
        now = self.clock()
        if not enabled:
            self.reset_observations()
            return
        if self._input_generation != inputs.generation:
            self.reset_observations()
            self._input_generation = inputs.generation
        if not self._enabled:
            self._enabled = True
            self._off_after = now
        presence, door = inputs.presence, inputs.door
        occupied, lux = inputs.occupied, inputs.lux
        opening = inputs.opening
        dark_lux = self.config.lights.dark_lux
        # A bright report breaks the dark period even if this same batch ends
        # dark again. The final value must not revive the earlier timer.
        if any(change.kind == 'illuminance' and (not change.continuous
                or dark_lux is not None and change.observation.value > dark_lux)
                for change in inputs.scene_changes):
            self.dark_since = None
        visual_person = (room.has_person and frame_mono is not None
            and 0 <= now-frame_mono <= self.config.frame_ttl_seconds)
        camera_departure = self._camera_departure(room, frame_mono, now)
        contact = door.value if door is not None else None
        latest_rise = None
        returned = False
        qualified_absence = None
        if (self._ended_absence is not None
                and not 0 <= now-self._ended_absence[2].received_at < self.config.door_camera_seconds):
            self._ended_absence = None
        # Different MQTT topics can arrive out of order. Merge this accepted
        # batch by source time after its last reset, preserving each topic's
        # continuity flags. Equal timestamps may confirm the same opening.
        changes = sorted(inputs.scene_changes,
            key=lambda change: (change.observation.observed_at, change.kind != 'opening'))
        for change in changes:
            if change.kind == 'opening':
                report = change.observation
                since, valid_until = self.absent_since, self._absence_valid_until
                ended = self._ended_absence
                if since is None and ended is not None and report.observed_at <= ended[2].observed_at:
                    since, valid_until, _ = ended
                # One ended absence can qualify only one opening, including
                # when its message arrives in the next tick after presence.
                self._ended_absence = None
                self._welcome_opening = (report if inputs.fresh(report)
                    and since is not None and valid_until is not None
                    and report.received_at <= valid_until
                    and report.received_at-since >= self.WELCOME_SECONDS else None)
                continue
            if change.kind != 'presence':
                continue
            report = change.observation
            fresh = inputs.fresh(report)
            if not fresh or not change.continuous:
                self.absent_since = self.off_since = self._absence_valid_until = None
                self._ended_absence = None
                self._return_light_available = False
                qualified_absence = None
            if fresh and report.value is False:
                self._absence_valid_until = report.valid_until
                self._ended_absence = None
            if fresh and report.value is True:
                if self.absent_since is not None:
                    absent_seconds = report.received_at-self.absent_since
                    returned |= absent_seconds >= self.VACANT_SECONDS
                    if absent_seconds >= self.VACANT_SECONDS:
                        qualified_absence = self.absent_since
                    if (self._welcome_opening is None and absent_seconds >= self.WELCOME_SECONDS
                            and self._absence_valid_until is not None
                            and report.received_at <= self._absence_valid_until):
                        self._ended_absence = (self.absent_since, self._absence_valid_until, report)
                self.absent_since = self.off_since = self._absence_valid_until = None
                if change.rising:
                    latest_rise = report.observed_at
                if contact is True and report.observed_at >= door.observed_at:
                    self._closed_room_occupied = True
        if occupied is None:
            self.absent_since = self.off_since = self._absence_valid_until = None
            self._return_light_available = False
            qualified_absence = None
        vacant = self.absent_since is not None and now-self.absent_since >= self.VACANT_SECONDS
        eligible_return = returned or vacant
        if vacant:
            qualified_absence = self.absent_since
        if qualified_absence is not None and qualified_absence != self._light_absence:
            # An old on request isn't evidence of the lamp's current state:
            # a wall switch or the other OS may have turned it off. A newly
            # observed vacancy grants one fresh on request, never a retry loop.
            self._light_absence = qualified_absence
            self._return_light_available = True
        if eligible_return and self.state['manual_off']:
            self.state['manual_off'] = False
            self.store.save_room_state(self.state)
        fresh_visual = (visual_person and frame_mono is not None
            and 0 <= now-frame_mono <= self.config.frame_ttl_seconds
            and (self._visual_stamp is None or frame_mono > self._visual_stamp))
        new_visual = fresh_visual and door is not None and frame_mono >= door.received_at
        if frame_mono is not None:
            self._visual_stamp = frame_mono
        if opening is not None:
            self._door_opened = True
        if contact is False:
            self._door_opened = True
        # A lone delayed positive presence report must not erase an opening.
        # Both visual and sensor evidence must follow closing: several reports
        # can be drained together, including a rise from before the opening.
        new_presence_rise = latest_rise is not None and door is not None and latest_rise >= door.observed_at
        if (new_visual or new_presence_rise) and contact is True:
            self._door_opened = False
        dark = dark_lux is not None and lux is not None and lux <= dark_lux
        if not dark:
            self.dark_since = None
        elif self.dark_since is None:
            self.dark_since = now
        present = occupied is True or visual_person
        pending = self._welcome_opening
        if pending is not None:
            if not 0 <= now-pending.received_at < self.config.door_camera_seconds:
                self._welcome_opening = None
            elif (occupied is True and presence.observed_at >= pending.observed_at
                  or fresh_visual and frame_mono >= pending.received_at):
                # Consume once even when the normal speech path skips a busy
                # desktop. Start generation before a synchronous finger wait.
                self._welcome_opening = None
                if self.welcome is not None:
                    self.welcome()
        # Prepare/propose speech before any synchronous finger operation. The
        # camera is an additional early cue, never an assertion of leaving home.
        if self.departure is not None:
            if camera_departure:
                self.departure(self.wall_clock()-(now-frame_mono))
            elif occupied is False and not visual_person:
                departure = next((change.observation for change in reversed(inputs.scene_changes)
                    if change.kind == 'presence' and change.falling and change.continuous
                    and inputs.fresh(change.observation)), None)
                if departure is not None:
                    self.departure(departure.observed_at)
        if (not self.state['manual_off'] and dark and
                (opening is not None and vacant
                 or present and (eligible_return or now-self.dark_since >= self.config.lights.dark_confirm_seconds))):
            self._request_light('on', 'return' if eligible_return else 'dark_room')
        if present:
            if contact is True:
                self._closed_room_occupied = True
            self.absent_since = self.off_since = self._absence_valid_until = None
        elif occupied is False and (self._door_opened or not self._closed_room_occupied):
            # Starting while already empty may never see the earlier exit.
            # Fresh empty observations still establish a return opportunity;
            # an unknown door cannot establish the closed-room protection.
            # turning lights off continues to require an observed open door.
            if self.absent_since is None:
                self.absent_since = now
            if self._door_opened and self.off_since is None and presence.received_at >= self._off_after:
                self.off_since = now
            if self.off_since is not None and now-self.off_since >= self.VACANT_SECONDS:
                self._request_light('off', 'vacant')
        else:
            self.absent_since = self.off_since = self._absence_valid_until = None

    def snapshot(self):
        return dict(manual_off=self.state['manual_off'], last_light=self.state.get('last_light'),
            vacant_seconds=max(0, self.clock()-self.absent_since) if self.absent_since is not None else 0,
            dark_threshold_configured=self.config.lights.dark_lux is not None)
