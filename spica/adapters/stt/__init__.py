"""Speech recognition adapters, with explicit selection and no automatic provider fallback."""


def build_stt_adapter(config, *, dashscope_api_key=None):
    """Build only the chosen backend; cloud never constructs the local worker."""
    if config.backend == "qwen_cloud":
        from .qwen_cloud import QwenCloudAsrAdapter
        return QwenCloudAsrAdapter(api_key=dashscope_api_key, base_url=config.cloud_base_url,
            model=config.cloud_model, language=config.language, timeout_seconds=config.cloud_timeout_seconds)
    if config.backend != "qwen_asr":
        raise ValueError(f"Unsupported speech recognition backend: {config.backend}")
    from pathlib import Path
    from .qwen_asr import QwenAsrAdapter
    root = Path(__file__).resolve().parents[3]
    return QwenAsrAdapter(
        model=str(root / config.model), worker_python=config.worker_python,
        device=config.device, compute_type=config.compute_type, language=config.language)


class UnavailableSpeechRecognition:
    name = 'stt_unavailable'

    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str:
        raise RuntimeError('语音识别初始化失败；请检查所选后端的配置与密钥。录音未转交其他识别服务。')

    def warmup(self):
        return {'ok': False, 'error': '语音识别初始化失败，请检查功能状态'}

    def close(self):
        pass  # No model, connection, or microphone was acquired.
