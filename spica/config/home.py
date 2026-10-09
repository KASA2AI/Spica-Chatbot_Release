"""Restart-effective Home settings; personal calibration is separate data."""
from pydantic import BaseModel, ConfigDict, Field, model_validator


class HomeWakeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    enabled: bool = False
    initial_volume: float = Field(default=.65, gt=0, le=1)
    volume_step: float = Field(default=.10, gt=0, le=1)
    maximum_volume: float = Field(default=.95, gt=0, le=1)
    intervals_seconds: tuple[float, ...] = (60, 45, 30)
    # Legacy step/interval fields remain readable; current wake policy uses time.
    volume_ramp_seconds: float = Field(default=180, ge=1, le=3600)
    maximum_duration_seconds: float = Field(default=600, ge=1, le=3600)
    response_window_seconds: float = Field(default=8, ge=1, le=60)
    response_window_min_seconds: float = Field(default=3, ge=1, le=60)
    light_on_device: str = Field(default="", pattern=r'^(?:0x[0-9a-f]{16})?$')
    # Owner-approved software initial values; site reliability is still unverified.
    start_confirm_seconds: float = Field(default=5, ge=1, le=120)
    start_confirm_min_frames: int = Field(default=10, ge=2, le=600)
    leave_bed_seconds: float | None = Field(default=3, ge=1, le=120)
    leave_bed_min_frames: int | None = Field(default=6, ge=2, le=600)
    power_control_enabled: bool = False
    suspend_margin_seconds: float | None = Field(default=None, ge=1, le=300)
    bedtime_prepare_timeout_seconds: float = Field(default=60, ge=15, le=120)

    @model_validator(mode="after")
    def valid(self):
        import math
        if self.initial_volume > self.maximum_volume:
            raise ValueError("initial wake volume exceeds its maximum")
        if self.volume_ramp_seconds > self.maximum_duration_seconds:
            raise ValueError("wake volume ramp exceeds the maximum duration")
        if self.response_window_min_seconds > self.response_window_seconds:
            raise ValueError("minimum response window exceeds its maximum")
        if (not self.intervals_seconds or len(self.intervals_seconds) > 10
                or any(not math.isfinite(value) or not 1 <= value <= 600 for value in self.intervals_seconds)):
            raise ValueError("wake intervals must be finite, positive seconds")
        if (self.leave_bed_seconds is None) != (self.leave_bed_min_frames is None):
            raise ValueError("continuous leave-bed time and frame count must be configured together")
        return self


class HomeLightsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    on_device: str = Field(default="", pattern=r'^(?:0x[0-9a-f]{16})?$')
    off_device: str = Field(default="", pattern=r'^(?:0x[0-9a-f]{16})?$')
    # Legacy keys remain readable; both fingers now use fixed 0..100 travel.
    off_upper: int | None = Field(default=None, ge=0, le=50, strict=True)
    off_lower: int | None = Field(default=None, ge=50, le=100, strict=True)
    dark_lux: float | None = Field(default=None, ge=0, le=10000)
    dark_confirm_seconds: float = Field(default=5, ge=1, le=120)
    illuminance_ttl_seconds: float = Field(default=180, ge=5, le=7200)
    bedtime_respeaker_leds_off: bool = False
    bedtime_case_leds_off: bool = False
    bedtime_gpu_leds_off: bool = False
    bedtime_ram_leds_off: bool = False
    bedtime_motherboard_leds_off: bool = False
    openrgb_executable: str = ""

    @model_validator(mode="after")
    def valid_off_travel(self):
        if (self.off_upper is None) != (self.off_lower is None):
            raise ValueError('off finger travel limits must be configured together')
        if self.off_upper is not None and self.off_upper >= self.off_lower:
            raise ValueError('off finger upper limit must be less than its lower limit')
        return self


class HomeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    enabled: bool = False
    daily_detection_enabled: bool = True
    # Desktop playback only: None inherits daily output; "" follows the system.
    output_device_id: str | None = None
    # An absent fixed primary may use this explicitly selected device at start.
    output_fallback_device_id: str = ''
    wake: HomeWakeConfig = Field(default_factory=HomeWakeConfig)
    lights: HomeLightsConfig = Field(default_factory=HomeLightsConfig)
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = Field(default=1883, ge=1, le=65535)
    mqtt_base_topic: str = "zigbee2mqtt"
    presence_device: str = "room_presence"
    door_device: str = "door_entry"
    observation_ttl_seconds: float = Field(default=180, ge=5, le=7200)
    camera_device: str = ""
    camera_width: int = Field(default=1920, ge=320, le=3840)
    camera_height: int = Field(default=1080, ge=240, le=2160)
    camera_fps: int = Field(default=15, ge=1, le=30)
    inference_interval_seconds: float = Field(default=.3, ge=.1, le=2)
    frame_ttl_seconds: float = Field(default=2, ge=.2, le=5)
    camera_idle_seconds: float = Field(default=10, ge=0, le=120)
    door_camera_seconds: float = Field(default=30, ge=1, le=180)
    camera_watchdog_seconds: float = Field(default=15, ge=5, le=60)
    # Accepted for old app YAML; no longer gates region/target observations.
    identity_ttl_seconds: float = Field(default=1, ge=0, le=3)
    desk_confirm_seconds: float = Field(default=1, ge=.2, le=5)
    departure_confirm_seconds: float = Field(default=2, ge=.5, le=30)
    face_similarity: float = Field(default=.5, ge=.3, le=.95)
    face_min_pixels: int = Field(default=55, ge=30, le=300)
    person_confidence: float = Field(default=.55, ge=.2, le=.95)
    model_directory: str = "models/home"
    data_directory: str = "data/runtime/home"
    display: str = ""
    xauthority: str = ""
    monitor_output: str = ""
    monitor_name: str = ""
    windows_monitor_id: str = ""

    @model_validator(mode="after")
    def light_bindings(self):
        # Keep existing H2 installations readable; lighting now belongs to the
        # room, so disabling wake alarms does not disable the light controls.
        legacy = self.wake.light_on_device
        if legacy and self.lights.on_device and legacy != self.lights.on_device:
            raise ValueError("conflicting legacy and room light-on devices")
        if not self.lights.on_device:
            self.lights.on_device = legacy
        if self.lights.on_device and self.lights.on_device == self.lights.off_device:
            raise ValueError("room light on/off require distinct finger devices")
        return self
