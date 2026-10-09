"""Home optional assembly; no dialogue tool or UI lifetime owns room sensing."""
from pathlib import Path

from spica.adapters.home_camera import HomeCamera
from spica.adapters.home_display import HomeDisplay
from spica.adapters.home_mqtt import HomeMQTT
from spica.adapters.home_power import HomePower
from spica.home.profile import load_profile
from spica.home.runtime import HomeRuntime

_REPO_ROOT = Path(__file__).resolve().parents[3]


def camera_preview(host, owner, action, session_id, *, camera_settings=None, raw_preview=False, profile_data=None):
    runtime = getattr(host, 'home_runtime', None)
    if runtime is None:
        raise RuntimeError('Home 后台不可用，请检查配置与启动状态')
    if action == 'begin':
        return runtime.begin_camera_preview(owner, session_id,
            camera_settings=camera_settings, raw_preview=raw_preview)
    if action == 'poll':
        return runtime.poll_camera_preview(owner, session_id)
    if action == 'save':
        return runtime.save_camera_preview(owner, session_id, profile_data)
    if action == 'end':
        return runtime.end_camera_preview(owner, session_id)
    raise PermissionError('unsupported Home camera operation')


def control_text(host, text):
    import uuid
    alarms = getattr(getattr(host, 'home_runtime', None), 'alarms', None)
    if alarms is None:
        return None
    with alarms.conversation(uuid.uuid4().hex, 'home-local-control', user_text=text, want_audio=False):
        return alarms.control_text(text)


def alarm_command(host, action=None, arguments=None, *, expected_revision=None, request_id=None):
    import uuid
    runtime = getattr(host, 'home_runtime', None)
    if runtime is None or runtime.alarms is None:
        raise RuntimeError('Home 叫醒尚未启用，请先完成家庭功能配置')
    with runtime.conversation(request_id or uuid.uuid4().hex, 'home-local-control', want_audio=False):
        if action is None:
            return runtime.alarms.plan()
        return runtime.alarms.manage(action=action, arguments=arguments or {}, expected_revision=expected_revision)


def resolved_home_config(config):
    """Resolve installation-relative data/model locations once at the boundary."""
    return config.model_copy(update={
        "data_directory": str(_REPO_ROOT / config.data_directory),
        "model_directory": str(_REPO_ROOT / config.model_directory),
    })


def build(config, effective_platform, *, observe_only=False):
    if effective_platform not in {"linux", "windows"}:
        raise RuntimeError("Home requires a supported local camera/display platform")
    config = resolved_home_config(config)
    if not config.camera_device:
        raise ValueError("Home camera stable device path is required")
    try:
        profile = load_profile(config.data_directory, config.camera_device)
    except FileNotFoundError:
        profile = None
    mqtt = HomeMQTT(config)
    camera = HomeCamera(config, profile, effective_platform=effective_platform)
    if effective_platform == 'windows':
        from spica.adapters.windows_home_display import WindowsHomeDisplay
        display = WindowsHomeDisplay(config)
    else:
        display = HomeDisplay(config)
    return HomeRuntime(config, mqtt, camera, None if observe_only else display, profile,
                       power=build_power(effective_platform))


def build_power(effective_platform):
    if effective_platform == 'windows':
        from spica.adapters.windows_home_power import WindowsHomePower
        return WindowsHomePower()
    if effective_platform == 'linux':
        return HomePower()
    raise RuntimeError('unsupported Home power platform')


def install(host, speech=None):
    """Attach optional Home to the desktop's existing conversation presentation."""
    if not host.config.home.enabled:
        return None
    if speech is None:
        raise RuntimeError('Home 需要本机对话呈现就绪')
    runtime = build(host.config.home, host.services.effective_platform)
    try:
        from spica.config.manager import ConfigManager
        from spica.home.alarm_store import AlarmStore
        from spica.home.tools import register_home_tools, register_room_tools
        from spica.runtime.jobs import ThreadJobRunner
        if getattr(host, '_business_evidence_jobs', None) is None:
            host._business_evidence_jobs = ThreadJobRunner()
        runtime.store = AlarmStore(Path(runtime.config.data_directory) / 'alarms.sqlite3')
        runtime.install_scenes()
        runtime.save_detection = lambda enabled: ConfigManager().update(
            {'home': {'daily_detection_enabled': enabled}}, reject_overrides=True)
        if runtime.config.wake.enabled:
            _install_wake(host, runtime, speech)
        _install_greetings(host, runtime, speech)
        with host.registry.registration():
            register_room_tools(host.registry, runtime)
            if runtime.alarms is not None:
                register_home_tools(host.registry, runtime.alarms)
            runtime.start()
        return runtime
    except Exception:
        runtime.close()
        raise


def _install_greetings(host, runtime, speech):
    from functools import partial
    from spica.home.greetings import HomeGreetings, record_farewell_outcome
    from spica.host.assemblies.memory import record_business_evidence
    runtime.greetings = HomeGreetings(runtime.store, prepare=speech.prepare_speech,
        propose=speech.submit, cancel=speech.cancel, local_state=speech.local_state,
        allowed=runtime.scene_allowed,
        character=lambda: host.chat_engine.config.character.character_id or 'spica',
        record_outcome=partial(record_farewell_outcome,
            partial(record_business_evidence, host), 'owner'))
    runtime.scenes.welcome, runtime.scenes.departure = runtime.greetings.welcome, runtime.greetings.departing


def _install_wake(host, runtime, speech):
    from dataclasses import asdict
    from spica.home.alarms import HomeAlarms
    from spica.home.bedtime import HomeBedtime
    from spica.adapters.home_night_lights import HomeNightLights
    from spica.ports.memory import MemoryScope
    from spica.home.alarm_speech import wake_event, wake_outcome
    from spica.host.assemblies.memory import record_business_evidence

    def warm_audio():
        from spica.host.warmup import prepare_output_audio
        return prepare_output_audio(host.tts_adapter)

    def prepare_output():
        revision = (alarms.output_generation, alarms.bedtime.resume_generation if alarms.bedtime else 0)
        return speech.prepare_audio(warm_audio, revision=revision)

    def reaction_guard():
        character = host.chat_engine.config.character
        role = character.model_dump()
        return lambda: (speech.local_state()[0] and host.chat_engine.config.character is character
                        and host.chat_engine.config.character.model_dump() == role)

    def check_readiness():
        runtime.mqtt.refresh_light_state()
        return dict(light=runtime.mqtt.light_readiness('on'), camera=runtime.camera.readiness(),
                    audio_endpoint='online' if speech.local_state()[0] else 'desktop_unavailable')

    def record_outcome(value, occurred_at):
        binding = wake_event(value)
        return record_business_evidence(host, MemoryScope(value['character_id'], 'owner', 'default'),
            event_id=f'{binding.kind}:{binding.event_id}:{binding.revision}:{binding.phase}',
            kind='environment', source=binding.kind, occurred_at=occurred_at, content=wake_outcome(value),
            metadata={'actor': 'business', 'event_binding': asdict(binding)})

    def retain_output(enabled):
        retain = getattr(host.tts_adapter, 'set_resident', None)
        if callable(retain):
            retain('home.wake', enabled)

    alarms = HomeAlarms(runtime.config, runtime.store, propose_speech=speech.submit,
        cancel_speech=speech.cancel, prepare_output=prepare_output,
        prepare_light=runtime.prepare_alarm_light, check_readiness=check_readiness,
        close_response_scope=speech.close_response_scope, cancel_reply=speech.cancel_reply,
        reaction_guard=reaction_guard, retain_output=retain_output,
        fallback_enabled=host.config.tts.enabled, record_outcome=record_outcome,
        active_character=lambda: host.chat_engine.config.character.character_id or 'spica')
    runtime.alarms = alarms
    indicators = HomeNightLights(runtime.config, effective_platform=host.services.effective_platform)
    alarms.bedtime = HomeBedtime(runtime.config, runtime.store, runtime.power,
        resources_ready=lambda: not runtime.camera.running,
        display_off=lambda: asdict(runtime.display.blank()),
        indicators_off=indicators.off, indicators_on=indicators.on,
        indicators_settled=indicators.cleanup_settled,
        light_off=lambda: runtime.set_room_light(action='off', _source='bedtime'))


def wire_conversation(host, runtime):
    """Bind local user tools and wake interaction to the one turn pipeline."""
    from contextlib import contextmanager
    from dataclasses import replace
    from spica.ports.conversation import MaterialHint
    engine = host.chat_engine
    @contextmanager
    def scope(request):
        if request.interaction_mode != 'chat':
            yield request
            return
        with runtime.conversation(request.evidence_turn_id, request.conversation_id,
                user_text=request.user_input, character_id=engine.config.character.character_id,
                want_audio=request.want_audio, embodied=True) as binding:
            if binding is not None and binding.kind == 'home.wake':
                request = replace(request, event_binding=binding,
                    material_hint=MaterialHint('home.wake', ''), audio_route='home',
                    want_audio=engine.config.tts.enabled and getattr(engine.deps.tts, 'name', '') != 'text_only')
            yield request
    engine.turn_scope = scope
    engine.reply_audio_route = lambda request: ('home' if runtime.uses_home_audio(request.evidence_turn_id)
        else request.audio_route)


def begin_shutdown(host):
    """Close optional I/O outside Qt; retain the owner until all receipts drain."""
    import threading
    runtime = getattr(host, 'home_runtime', None)
    if runtime is None:
        return None
    worker = getattr(host, '_home_shutdown', None)
    if worker is None:
        def close():
            try:
                runtime.close()
            except Exception as exc:
                host._home_shutdown_error = exc
        worker = host._home_shutdown = threading.Thread(target=close, name='home-close', daemon=True)
        worker.start()
    return worker
