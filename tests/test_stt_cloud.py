"""Cloud ASR wire format, offline isolation, credentials and request lifetime."""
import asyncio
import base64
import builtins
import io
import json
import threading
import wave
from types import SimpleNamespace

import httpx
import pytest

from spica.adapters.stt import build_stt_adapter
from spica.adapters.stt.qwen_cloud import QwenCloudAsrAdapter
from spica.config.schema import SttConfig

KEY = 'test-only-bailian-key'
PCM = b'\x01\x00' * 480


@pytest.fixture
def cloud(monkeypatch):
    adapter = build_stt_adapter(SttConfig(backend='qwen_cloud'), dashscope_api_key=KEY)
    monkeypatch.setattr(adapter, '_has_speech', lambda pcm: True)
    yield adapter
    adapter.close()


def transport(monkeypatch, handler):
    monkeypatch.setattr('spica.adapters.stt.qwen_cloud.httpx', SimpleNamespace(
        AsyncClient=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw),
        TimeoutException=httpx.TimeoutException, HTTPError=httpx.HTTPError))


def test_cloud_factory_and_startup_never_import_or_load_local_models(monkeypatch):
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert name not in {'torch', 'qwen_asr', 'silero_vad', 'transformers', 'qwen_worker'}
        assert name != 'spica.adapters.stt.qwen_asr'
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    transport(monkeypatch, lambda request: pytest.fail('startup must be offline'))
    from spica.host.app_host import AppHost
    from spica.host.warmup import _warmup_stt
    from spica.config.secrets import Secrets
    host = SimpleNamespace(config=SimpleNamespace(stt=SttConfig(backend='qwen_cloud', model='/missing')),
                           secrets=Secrets(dashscope_api_key=KEY))
    adapter = AppHost._new_stt_adapter(host)
    progress = []
    _warmup_stt(adapter, lambda *args: progress.append(args), warmup_on_startup=False)
    assert adapter.name == 'qwen_cloud' and '待说话验证' in progress[-1][1]
    adapter.close()


def test_wav_request_only_contains_audio_and_returns_final_text(cloud, monkeypatch):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'choices':[{'finish_reason':'stop', 'message':{'content':' 关灯。 '}}]})
    transport(monkeypatch, handle)
    assert cloud.transcribe(PCM) == '关灯。'
    assert len(requests) == 1
    request = requests[0]
    assert request.headers['Authorization'] == 'Bearer ' + KEY
    assert str(request.url) == 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
    body = json.loads(request.content)
    assert body['model'] == 'qwen3-asr-flash' and body['stream'] is False
    assert body['asr_options'] == {'language':'zh', 'enable_itn':False}
    assert len(body['messages']) == 1 and 'tools' not in body
    data = body['messages'][0]['content'][0]['input_audio']['data']
    with wave.open(io.BytesIO(base64.b64decode(data.split(',')[1])), 'rb') as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.readframes(wav.getnframes()) == PCM


def test_empty_silent_and_invalid_audio_never_leave_host(monkeypatch):
    adapter = build_stt_adapter(SttConfig(backend='qwen_cloud'), dashscope_api_key=KEY)
    transport(monkeypatch, lambda request: pytest.fail('must not upload'))
    assert adapter.transcribe(b'') == ''
    assert adapter.transcribe(b'\0' * 32000) == ''
    for pcm, rate in [(b'x',16000), (PCM,48000), (b'\0' * (32000 * 60 + 2),16000)]:
        with pytest.raises(ValueError): adapter.transcribe(pcm, sample_rate=rate)
    adapter.close()
    with pytest.raises(RuntimeError, match='已关闭'): adapter.transcribe(PCM)


@pytest.mark.parametrize('status,fragment', [(401,'密钥'), (403,'授权'), (429,'受限'), (500,'请求失败'), (302,'请求失败')])
def test_errors_do_not_echo_secrets_audio_or_retry(cloud, monkeypatch, status, fragment):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, text=KEY + request.content.decode(),
                              headers={'location':'https://example.com/steal'})
    transport(monkeypatch, handle)
    with pytest.raises(RuntimeError, match=fragment) as error: cloud.transcribe(PCM)
    assert KEY not in str(error.value) and 'base64' not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize('reply', [b'not-json', b'{"choices":[]}',
    b'{"choices":[{"finish_reason":"length","message":{"content":"partial"}}]}',
    b'{"choices":[{"finish_reason":"stop","message":{"content":null}}]}', b'x' * 65537])
def test_malformed_truncated_and_oversized_reply_is_not_a_transcript(cloud, monkeypatch, reply):
    transport(monkeypatch, lambda request: httpx.Response(200, content=reply))
    with pytest.raises(RuntimeError, match='返回'): cloud.transcribe(PCM)


def test_total_deadline_cancels_request_and_allows_next_utterance(cloud, monkeypatch):
    cloud._timeout = .05
    closed = []
    async def delayed(request):
        try:
            await asyncio.sleep(10)
        finally:
            closed.append(True)
    transport(monkeypatch, delayed)
    with pytest.raises(RuntimeError, match='超时'): cloud.transcribe(PCM)
    assert closed and cloud._task is None
    transport(monkeypatch, lambda request: httpx.Response(200, json={
        'choices':[{'finish_reason':'stop','message':{'content':'next'}}]}))
    assert cloud.transcribe(PCM) == 'next'


def test_close_interrupts_inflight_upload_and_rejects_late_result(cloud, monkeypatch):
    entered = threading.Event()
    released = threading.Event()
    errors = []
    async def blocked(request):
        entered.set()
        try:
            await asyncio.sleep(10)
        finally:
            released.set()
    transport(monkeypatch, blocked)
    def transcribe():
        try: cloud.transcribe(PCM)
        except RuntimeError as exc: errors.append(str(exc))
    caller = threading.Thread(target=transcribe)
    caller.start()
    try:
        assert entered.wait(2)
        with pytest.raises(RuntimeError, match='仍在识别'): cloud.transcribe(PCM)
        cloud.close()
        caller.join(2)
        assert not caller.is_alive() and released.is_set() and errors
        assert not cloud.warmup()['ok'] and cloud._task is None
    finally:
        cloud.close()
        caller.join(2)


def test_cloud_configuration_cannot_send_keys_to_arbitrary_endpoints():
    for url in ['http://dashscope.aliyuncs.com/compatible-mode/v1',
                'https://example.com/compatible-mode/v1',
                'https://dashscope.aliyuncs.com/compatible-mode/v1?api_key=secret']:
        with pytest.raises(ValueError): SttConfig(cloud_base_url=url)
    assert SttConfig(cloud_base_url='https://ws-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/').cloud_base_url.endswith('/v1')
    for model in ['qwen3-asr-flash-realtime', 'qwen3-asr-flash-filetrans']:
        with pytest.raises(ValueError): SttConfig(cloud_model=model)
    with pytest.raises(ValueError, match='API Key'):
        build_stt_adapter(SttConfig(backend='qwen_cloud'))


def test_cloud_self_check_is_offline_and_does_not_claim_real_acceptance(monkeypatch):
    from spica.config.secrets import Secrets
    from scripts import self_check
    app = SimpleNamespace(stt=SttConfig(backend='qwen_cloud', model='/missing'))
    monkeypatch.setattr(self_check, 'load_secrets', lambda: Secrets(dashscope_api_key=KEY))
    monkeypatch.setattr('spica.config.manager.ConfigManager.load', lambda self: app)
    monkeypatch.setattr('spica.adapters.stt.build_stt_adapter', lambda *a, **kw: pytest.fail('must not construct a model/probe'))
    assert self_check.check_stt_light(app)['status'] == 'UNVERIFIED'
    assert self_check._worker_stt()['status'] == 'UNVERIFIED'
