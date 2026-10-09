"""Qwen admission, model reuse, and ownership without downloading any model."""
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from spica.adapters.stt import build_stt_adapter
from spica.adapters.stt.qwen_asr import QwenAsrAdapter
from spica.config.schema import SttConfig


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    # A real child/pipe exercises timeout and cleanup; no GPU or third-party ASR.
    worker = tmp_path / "worker.py"
    worker.write_text('''import json, os, pathlib, sys, time
root = pathlib.Path(sys.argv[sys.argv.index('--model') + 1])
for line in sys.stdin:
    request = json.loads(line)
    if request['op'] == 'transcribe':
        (root/'entered').touch()
        if (root/'block').exists(): time.sleep(10)
        if (root/'malformed').exists():
            print('not-json', flush=True)
            continue
    result = {'actual_device':'cuda:0','pid':os.getpid()} if request['op']=='warmup' else {'text':'recognized'}
    print(json.dumps({'ok':True,'result':result}), flush=True)
''')
    monkeypatch.setattr("spica.adapters.stt.qwen_asr.WORKER", worker)
    value = QwenAsrAdapter(model=str(tmp_path), worker_python=sys.executable)
    value._startup_timeout = 5
    value._inference_timeout = .15
    yield value
    value.close()


def test_qwen_factory_is_lazy_and_does_not_import_model_dependencies(tmp_path):
    before = sys.modules.get('qwen_asr')
    value = build_stt_adapter(SttConfig(backend='qwen_asr', model=str(tmp_path), compute_type='bfloat16'))
    assert isinstance(value, QwenAsrAdapter) and value._process is None
    assert sys.modules.get('qwen_asr') is before
    value.close()


def test_one_resident_process_reused_then_closed(adapter):
    assert adapter._process is None
    warm = adapter.warmup()
    assert warm['ok']
    process = adapter._process
    for _ in range(3):
        assert adapter.transcribe(b'\x01\x00') == 'recognized'
    assert adapter.warmup()['pid'] == warm['pid']
    adapter.close()
    assert process.poll() is not None
    with pytest.raises(RuntimeError, match='已关闭'):
        adapter.transcribe(b'\x01\x00')


def test_timeout_reaps_child_before_retry(adapter):
    assert adapter.warmup()['ok']
    process = adapter._process
    marker = Path(adapter._model_path)/'block'
    marker.touch()
    with pytest.raises(RuntimeError, match='超时'):
        adapter.transcribe(b'\x01\x00')
    assert process.poll() is not None and adapter._process is None
    marker.unlink()
    assert adapter.transcribe(b'\x01\x00') == 'recognized'
    assert adapter._process.pid != process.pid


def test_broken_reply_is_not_a_transcript_and_child_is_reaped(adapter):
    assert adapter.warmup()['ok']
    process = adapter._process
    (Path(adapter._model_path)/'malformed').touch()
    with pytest.raises(json.JSONDecodeError):
        adapter.transcribe(b'\x01\x00')
    assert process.poll() is not None and adapter._process is None


def test_shutdown_interrupts_inflight_request_without_restarting(adapter):
    assert adapter.warmup()['ok']
    adapter._inference_timeout = 10
    root = Path(adapter._model_path)
    (root/'block').touch()
    errors = []
    def transcribe():
        try:
            adapter.transcribe(b'\x01\x00')
        except Exception as exc:
            errors.append(exc)
    caller = threading.Thread(target=transcribe)
    caller.start()
    deadline = time.monotonic()+3
    while not (root/'entered').exists() and time.monotonic()<deadline:
        time.sleep(.01)
    assert (root/'entered').exists()
    process = adapter._process
    adapter.close()
    caller.join(timeout=2)
    assert not caller.is_alive() and errors
    assert process.poll() is not None and adapter._process is None


def test_invalid_pcm_never_starts_worker(adapter):
    for pcm, rate in [(b'x', 16000), (b'\0\0', 48000), (b'\0'*(16000*2*60+2), 16000)]:
        with pytest.raises(ValueError):
            adapter.transcribe(pcm, sample_rate=rate)
    assert adapter.transcribe(b'') == ''
    assert adapter._process is None


def test_queued_request_has_a_deadline_without_killing_current_inference(adapter):
    assert adapter.warmup()['ok']
    adapter._inference_timeout = 10
    root = Path(adapter._model_path)
    (root / 'block').touch()
    errors = []

    def transcribe():
        try:
            adapter.transcribe(b'\x01\x00')
        except Exception as exc:
            errors.append(str(exc))

    current = threading.Thread(target=transcribe)
    queued = threading.Thread(target=transcribe)
    current.start()
    try:
        deadline = time.monotonic() + 3
        while not (root / 'entered').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (root / 'entered').exists()
        process = adapter._process
        adapter._request_timeout = .1
        queued.start()
        queued.join(1)
        assert not queued.is_alive(), 'serialization wait exceeded its request deadline'
        assert len(errors) == 1 and '超时' in errors[0]
        assert current.is_alive() and process.poll() is None
    finally:
        adapter.close()
        current.join(2)
        if queued.ident is not None:
            queued.join(2)


def test_vad_blocks_silence_and_model_is_reused_without_context(tmp_path, monkeypatch):
    from spica.local_runtime.stt.qwen_worker import QwenRecognizer
    load_vad = Mock(return_value=object())
    monkeypatch.setitem(sys.modules, 'silero_vad', SimpleNamespace(
        load_silero_vad=load_vad,
        get_speech_timestamps=lambda audio, model, **kwargs: [{'start':0,'end':len(audio)}] if bool(audio.any()) else []))
    model = SimpleNamespace(model=SimpleNamespace(device='cpu', dtype='float32'),
                            transcribe=Mock(return_value=[SimpleNamespace(text=' 关灯。 ')]))
    factory = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, 'qwen_asr', SimpleNamespace(Qwen3ASRModel=SimpleNamespace(from_pretrained=factory)))
    recognizer = QwenRecognizer(str(tmp_path), 'cpu', 'float32', 'zh')
    assert recognizer.transcribe(np.zeros(16000, dtype='<i2').tobytes()) == ''
    factory.assert_not_called()
    assert recognizer.warmup()['actual_device'] == 'cpu'
    pcm = np.array([32767, -32768], dtype='<i2').tobytes()
    for _ in range(2):
        assert recognizer.transcribe(pcm) == '关灯。'
    factory.assert_called_once()
    load_vad.assert_called_once_with(onnx=True)
    assert factory.call_args.kwargs['local_files_only'] is True
    args = model.transcribe.call_args.kwargs
    assert args['context'] == '' and args['language'] == 'Chinese'
    np.testing.assert_allclose(args['audio'][0], [32767/32768, -1])


def test_qwen_rejects_unsupported_quantization():
    with pytest.raises(ValueError, match='compute_type'):
        SttConfig(backend='qwen_asr', compute_type='int8')


def test_qwen_light_check_is_not_reported_as_google_disabled(tmp_path):
    from scripts.self_check import check_stt_light
    config = SttConfig(backend='qwen_asr', model=str(tmp_path), worker_python=sys.executable)
    result = check_stt_light(SimpleNamespace(stt=config))
    assert result['status'] == 'DEGRADED'
    (tmp_path/'config.json').write_text('{}')
    (tmp_path/'model.safetensors').touch()
    result = check_stt_light(SimpleNamespace(stt=config))
    assert result['status'] == 'UNVERIFIED'
