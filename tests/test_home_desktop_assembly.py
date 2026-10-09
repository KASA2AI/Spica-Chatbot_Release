"""Standalone Home tools traverse the real dialogue pipeline with fake model/I/O."""
import json
from types import SimpleNamespace
import pytest
from test_proactive_turn import _build_engine, _ChatCompletionsAPI
from test_home_alarm_management import home, view
from test_home import Camera, MQTTEvents, profile
from spica.home.runtime import HomeRuntime
from spica.home.tools import register_home_tools
from spica.host.assemblies.home import wire_conversation


@pytest.mark.parametrize('streaming', [False, True])
def test_local_home_tool_keeps_authority_across_turn_execution(tmp_path, streaming):
    env = home(tmp_path)
    calls = []
    class Model(_ChatCompletionsAPI):
        remaining = True
        def create(self, **kwargs):
            if kwargs.get('tools') and self.remaining:
                self.remaining = False
                function = SimpleNamespace(name='set_wake_alarm', arguments=json.dumps({'after_minutes': 30}))
                if kwargs.get('stream'):
                    return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None,
                        tool_calls=[SimpleNamespace(index=0, id='set', type='function', function=function)]))])])
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='',
                    tool_calls=[SimpleNamespace(id='set', type='function', function=function)]))], usage=None)
            return super().create(**kwargs)
    engine = _build_engine(SimpleNamespace(base_url='https://api.deepseek.com/v1', chat=Model(calls)), tmp_path)
    engine.config.tts.daily_enabled = False
    register_home_tools(engine.services.tool_registry, env.alarms)
    runtime = HomeRuntime(env.config, MQTTEvents(), Camera(), None, profile(), alarms=env.alarms)
    wire_conversation(SimpleNamespace(chat_engine=engine), runtime)
    try:
        if streaming:
            list(engine.stream_voice_runtime('半小时后叫醒我'))
        else:
            engine.run_voice('半小时后叫醒我')
        assert view(env)['next']['due_at'] == env.clock.at + 1800
        with pytest.raises(PermissionError):
            env.alarms.query()  # Authorization ends with the request, never ambient.
        count = len(view(env)['temporary'])
        list(engine.stream_system_turn('半小时后叫醒我', source='fake-system'))
        assert len(view(env)['temporary']) == count
        assert calls and not calls[-1][1].get('tools')
    finally:
        env.alarms.close()
