"""File-path entry for one resident Qwen model; no Spica/Qt imports required."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import queue
import sys
import threading


class QwenRecognizer:
    """Serialized by the worker loop. VAD is mandatory, context stays empty."""
    def __init__(self, model, device, compute_type, language):
        self.model_path, self.device = model, device
        self.compute_type = compute_type
        self.language = {"zh": "Chinese", "en": "English", "ja": "Japanese", "yue": "Cantonese"}.get(language, language)
        self.model = None
        self.vad = None

    def _load(self):
        if self.model is None:
            import torch
            from qwen_asr import Qwen3ASRModel
            if self.compute_type not in {"float16", "bfloat16", "float32"}:
                raise ValueError("Qwen 计算精度须为 float16、bfloat16 或 float32")
            path = Path(self.model_path).resolve(strict=True)
            if not path.is_dir():
                raise ValueError("Qwen 模型须为本地目录")
            torch.set_num_threads(4)
            device = self.device
            if device == "auto":
                device = "cuda" if torch.cuda.is_available() else "cpu"
            self.model = Qwen3ASRModel.from_pretrained(
                str(path), dtype=getattr(torch, self.compute_type), device_map=device,
                local_files_only=True, attn_implementation="sdpa",
                max_inference_batch_size=1, max_new_tokens=512)
        return self.model

    def _decode(self, audio):
        import torch
        model = self._load()
        with torch.inference_mode():
            return model.transcribe(audio=(audio, 16000), language=self.language, context="")[0].text.strip()

    def transcribe(self, pcm):
        import numpy as np
        import torch
        from silero_vad import load_silero_vad, get_speech_timestamps
        if self.vad is None:
            self.vad = load_silero_vad(onnx=True)
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32)/32768.0
        chunks = get_speech_timestamps(
            torch.from_numpy(audio), self.vad, sampling_rate=16000,
            threshold=0.5, neg_threshold=0.35, min_speech_duration_ms=0,
            min_silence_duration_ms=2000, speech_pad_ms=400)
        if not chunks:
            return ""
        return self._decode(np.concatenate([audio[c["start"]:c["end"]] for c in chunks]))

    def warmup(self):
        import numpy as np
        # Prepare both VAD and CUDA. The direct silent decode is discarded, never
        # returned as a transcript; ordinary input must pass VAD first.
        self.transcribe(np.zeros(16000, dtype="<i2").tobytes())
        self._decode(np.zeros(16000, dtype=np.float32))
        return {"actual_device": str(self.model.model.device),
                "compute_type": str(self.model.model.dtype)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--compute-type", required=True)
    parser.add_argument("--language", required=True)
    args = parser.parse_args()
    wire_out = sys.stdout
    sys.stdout = sys.stderr  # Third-party prints cannot corrupt the reply stream.
    # Initialize native math libraries before a thread blocks on piped stdin.
    # NumPy/SciPy can deadlock during Windows initialization otherwise. This
    # imports libraries only; model/CUDA loading still has the EOF monitor.
    import numpy  # noqa: F401
    import scipy.special  # noqa: F401
    import qwen_asr  # noqa: F401
    import silero_vad  # noqa: F401
    recognizer = QwenRecognizer(args.model, args.device, args.compute_type, args.language)
    requests = queue.Queue(maxsize=1)

    def read_requests():
        while True:
            line = sys.stdin.buffer.readline(2_600_001)
            # EOF also interrupts native inference when the owning core dies.
            if not line or not line.endswith(b"\n") or len(line) > 2_600_000:
                os._exit(0)
            requests.put(line)

    threading.Thread(target=read_requests, name="qwen-parent-pipe", daemon=True).start()
    while True:
        try:
            request = json.loads(requests.get())
            if request["op"] == "warmup":
                result = recognizer.warmup()
            elif request["op"] == "transcribe":
                pcm = base64.b64decode(request["pcm"], validate=True)
                if len(pcm) % 2 or len(pcm) > 16000 * 2 * 60:
                    raise ValueError("invalid PCM length")
                result = {"text": recognizer.transcribe(pcm)}
            else:
                raise ValueError("unknown ASR operation")
            reply = {"ok": True, "result": result}
        except Exception as exc:
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:800]}
        wire_out.write(json.dumps(reply, ensure_ascii=True)+"\n")
        wire_out.flush()


if __name__ == "__main__":
    main()
