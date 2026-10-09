"""Fatal microphone/STT errors, shared without importing a GUI toolkit."""

FATAL_SPEECH_ERROR_MARKERS = (
    "语音识别初始化失败",
    "语音识别未配置",
    "Qwen 需要已下载",
    "百炼语音识别密钥无效",
    "采音资源尚未释放",
    "缺少 PyAudio",
    "Could not find PyAudio",
    "No Default Input Device",
    "Invalid input device",
    "无法打开 ReSpeaker",
    "ReSpeaker 硬件 VAD 不可用",
    # Recorder open failures must stop the loop; transient mid-take failures
    # deliberately do not carry this envelope.
    "无法打开麦克风",
)


def is_fatal_speech_error(message: str) -> bool:
    return any(marker in message for marker in FATAL_SPEECH_ERROR_MARKERS)
