import json
import os
import re
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from timing_utils import elapsed_ms, log_timing, now_ms
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "tts_config.json"
DEFAULT_OUTPUT_DIR = BASE_DIR / "static" / "generated_voice"
PROXY_ENV_KEYS = ("all_proxy", "ALL_PROXY", "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
UNSAFE_TTS_CHUNK_ENDINGS = ("、", "，", ",")
SHORT_TTS_OPENERS = {"もちろん。", "はい。", "ええ。", "そうですね。"}


@contextmanager
def pushd(path: Path):
    old_cwd = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old_cwd)


class GPTSoVITSTool:
    """Required local tool that turns a Japanese reply into Spica voice audio."""

    def __init__(self, config_path: str | Path = DEFAULT_CONFIG_PATH):
        self.config_path = Path(config_path).resolve()
        self.config_dir = self.config_path.parent
        self._lock = threading.RLock()
        self._config_mtime = 0.0
        self.config: dict[str, Any] = {}

        self.gptsovits_root = Path()
        self.output_dir = Path()
        self.static_url_prefix = ""

        self._i18n = None
        self._change_gpt_weights = None
        self._change_sovits_weights = None
        self._get_tts_wav = None
        self._module_ready = False
        self._loaded_gpt_path: str | None = None
        self._loaded_sovits_path: str | None = None
        self._loaded_languages: tuple[str, str] | None = None

        self.reload_config(force=True)

    def reload_config(self, force: bool = False) -> None:
        try:
            mtime = self.config_path.stat().st_mtime
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"TTS 配置不存在：{self.config_path}") from exc

        if not force and mtime == self._config_mtime:
            return

        with self.config_path.open("r", encoding="utf-8") as file:
            config = json.load(file)

        self.config = config
        self._config_mtime = mtime
        self.gptsovits_root = self._resolve_path(config["gptsovits_root"])
        output_dir = config.get("output_dir")
        self.output_dir = self._resolve_path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
        self.static_url_prefix = str(config.get("static_url_prefix", "/static/generated_voice")).rstrip("/")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def public_config(self) -> dict[str, Any]:
        self.reload_config()
        return self.config

    def warmup(self, emotion: str | None = None, synthesize: bool | None = None) -> dict[str, Any]:
        """Load GPT-SoVITS weights once at Flask startup.

        The warmup emotion only chooses the reference sample used to initialize
        the TTS service. Visual diff selection is handled separately by
        visual_service.py and does not read this value.
        """
        with self._lock:
            start_ms = now_ms()
            self.reload_config()
            emotion_key = self.normalize_emotion(emotion or self.config.get("warmup_emotion") or "happy")
            sample = self._emotion_sample(emotion_key)
            gpt_model_path = self._resolve_path(sample.get("gpt_model_path") or self.config["gpt_model_path"])
            sovits_model_path = self._resolve_path(sample.get("sovits_model_path") or self.config["sovits_model_path"])
            ref_language = sample.get("ref_language") or self.config.get("ref_language", "日文")
            target_language = self.config.get("target_language", "日文")
            should_synthesize = bool(self.config.get("warmup_synthesize", False) if synthesize is None else synthesize)

            self._lazy_import()
            with pushd(self.gptsovits_root):
                self._ensure_models(gpt_model_path, sovits_model_path, ref_language, target_language)
                if should_synthesize:
                    warmup_text = str(self.config.get("warmup_text") or "はい。")
                    list(
                        self._get_tts_wav(
                            ref_wav_path=str(sample["ref_audio_path"]),
                            prompt_text=sample["prompt_text"],
                            prompt_language=self._i18n(ref_language),
                            text=warmup_text,
                            text_language=self._i18n(target_language),
                            top_p=1,
                            temperature=1,
                            inp_refs=None,
                            how_to_cut="不切",
                            pause_second=0.3,
                            speed=1,
                            top_k=15,
                            ref_free=False,
                        )
                    )

            duration_ms = elapsed_ms(start_ms)
            log_timing("tts_warmup", duration_ms, emotion=emotion_key, synthesize=should_synthesize)
            return {
                "ok": True,
                "emotion": emotion_key,
                "synthesize": should_synthesize,
                "duration_ms": duration_ms,
            }

    def synthesize(
        self,
        text: str,
        emotion: str,
        tts_param_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise ValueError("TTS 文本为空，无法合成语音。")
        text = self._normalize_tts_text(text)

        with self._lock:
            total_start_ms = now_ms()
            self.reload_config()
            emotion_key = self.normalize_emotion(emotion)
            sample = self._emotion_sample(emotion_key)
            params = self._tts_params(sample, tts_param_overrides or {})
            gpt_model_path = self._resolve_path(sample.get("gpt_model_path") or self.config["gpt_model_path"])
            sovits_model_path = self._resolve_path(sample.get("sovits_model_path") or self.config["sovits_model_path"])
            ref_language = sample.get("ref_language") or self.config.get("ref_language", "日文")
            target_language = self.config.get("target_language", "日文")
            output_wav_path = self._new_output_path(emotion_key)
            text_chunks = self._split_tts_text(text, params)
            chunk_audio_items = []

            self._lazy_import()
            import soundfile as sf

            with pushd(self.gptsovits_root):
                model_start_ms = now_ms()
                self._ensure_models(gpt_model_path, sovits_model_path, ref_language, target_language)
                log_timing("tts_models", elapsed_ms(model_start_ms), emotion=emotion_key)
                result_list = []
                chunk_timings = []
                for index, chunk in enumerate(text_chunks):
                    chunk_start_ms = now_ms()
                    synthesis_result = self._get_tts_wav(
                        ref_wav_path=str(sample["ref_audio_path"]),
                        prompt_text=sample["prompt_text"],
                        prompt_language=self._i18n(ref_language),
                        text=chunk,
                        text_language=self._i18n(target_language),
                        top_p=params["top_p"],
                        temperature=params["temperature"],
                        inp_refs=params["inp_refs"],
                        how_to_cut="不切",
                        pause_second=params["pause_second"],
                        speed=params["speed"],
                        top_k=params["top_k"],
                        ref_free=False,
                    )
                    chunk_results = list(synthesis_result)
                    if chunk_results:
                        chunk_sampling_rate, chunk_audio_data = self._combine_audio_results(chunk_results)
                        chunk_wav_path = self._chunk_output_path(output_wav_path, index)
                        sf.write(chunk_wav_path, chunk_audio_data, chunk_sampling_rate)
                        chunk_audio_items.append(
                            {
                                "index": index,
                                "text": chunk,
                                "audio_url": f"{self.static_url_prefix}/{chunk_wav_path.name}",
                                "audio_path": str(chunk_wav_path),
                                "sampling_rate": chunk_sampling_rate,
                            }
                        )
                    result_list.extend(chunk_results)
                    chunk_duration = elapsed_ms(chunk_start_ms)
                    chunk_timings.append(
                        {
                            "index": index,
                            "duration_ms": chunk_duration,
                            "chars": len(chunk),
                            "result_count": len(chunk_results),
                            "text": chunk,
                        }
                    )
                    log_timing(
                        "tts_chunk",
                        chunk_duration,
                        index=index,
                        chars=len(chunk),
                        result_count=len(chunk_results),
                        text=chunk,
                    )

            if not result_list:
                raise RuntimeError("GPT-SoVITS 未返回音频数据。")

            combine_start_ms = now_ms()
            sampling_rate, audio_data = self._combine_audio_results(result_list)
            combine_ms = elapsed_ms(combine_start_ms)

            write_start_ms = now_ms()
            sf.write(output_wav_path, audio_data, sampling_rate)
            write_ms = elapsed_ms(write_start_ms)
            total_ms = elapsed_ms(total_start_ms)
            log_timing(
                "tts_total",
                total_ms,
                emotion=emotion_key,
                chunks=len(text_chunks),
                combine_ms=combine_ms,
                write_ms=write_ms,
            )
            return {
                "ok": True,
                "tool": "gptsovits_tts",
                "audio_url": f"{self.static_url_prefix}/{output_wav_path.name}",
                "audio_path": str(output_wav_path),
                "sampling_rate": sampling_rate,
                "emotion": emotion_key,
                "reference": {
                    "prompt_text": sample["prompt_text"],
                    "ref_audio_path": str(sample["ref_audio_path"]),
                    "inp_refs_path": str(sample["inp_refs_path"]) if sample.get("inp_refs_path") else None,
                },
                "tts_params": self._json_safe_params(params),
                "tts_chunks": text_chunks,
                "tts_chunk_audio": chunk_audio_items,
                "timing": {
                    "tts_total_ms": total_ms,
                    "tts_chunks": chunk_timings,
                    "tts_combine_ms": combine_ms,
                    "tts_write_ms": write_ms,
                },
            }

    def normalize_emotion(self, emotion: str | None) -> str:
        aliases = {
            "joy": "happy",
            "fun": "happy",
            "happy": "happy",
            "喜": "happy",
            "乐": "happy",
            "angry": "angry",
            "anger": "angry",
            "怒": "angry",
            "sad": "sad",
            "sorrow": "sad",
            "哀": "sad",
            "悲": "sad",
            "surprised": "surprised",
            "surprise": "surprised",
            "惊": "surprised",
            "驚": "surprised",
        }
        value = (emotion or "").strip().lower()
        emotions = self.config.get("emotions", {})
        default_emotion = self.config.get("default_emotion", "happy")
        return aliases.get(value, value if value in emotions else default_emotion)

    def _lazy_import(self) -> None:
        if self._module_ready:
            return

        for key in PROXY_ENV_KEYS:
            os.environ.pop(key, None)

        package_dir = self.gptsovits_root / "GPT_SoVITS"
        import_paths = [
            str(self.gptsovits_root),
            str(package_dir),
            str(package_dir / "eres2net"),
        ]
        for import_path in reversed(import_paths):
            if import_path in sys.path:
                sys.path.remove(import_path)
            sys.path.insert(0, import_path)

        loaded_tools = sys.modules.get("tools")
        if loaded_tools is not None and not hasattr(loaded_tools, "__path__"):
            del sys.modules["tools"]

        with pushd(self.gptsovits_root):
            from tools.i18n.i18n import I18nAuto
            from GPT_SoVITS.inference_webui import change_gpt_weights, change_sovits_weights, get_tts_wav
            self._i18n = I18nAuto()

        self._change_gpt_weights = change_gpt_weights
        self._change_sovits_weights = change_sovits_weights
        self._get_tts_wav = get_tts_wav
        self._module_ready = True

    def _ensure_models(
        self,
        gpt_model_path: Path,
        sovits_model_path: Path,
        ref_language: str,
        target_language: str,
    ) -> None:
        force_reload = bool(self.config.get("reload_model_each_request", False))
        gpt_path = str(gpt_model_path)
        sovits_path = str(sovits_model_path)
        language_pair = (ref_language, target_language)

        if force_reload or self._loaded_gpt_path != gpt_path:
            self._change_gpt_weights(gpt_path=gpt_path)
            self._loaded_gpt_path = gpt_path

        if force_reload or self._loaded_sovits_path != sovits_path or self._loaded_languages != language_pair:
            for _ in self._change_sovits_weights(
                sovits_path=sovits_path,
                prompt_language=self._i18n(ref_language),
                text_language=self._i18n(target_language),
            ):
                pass
            self._loaded_sovits_path = sovits_path
            self._loaded_languages = language_pair

    def _emotion_sample(self, emotion: str) -> dict[str, Any]:
        emotions = self.config.get("emotions", {})
        if emotion not in emotions:
            emotion = self.config.get("default_emotion", "happy")
        sample = dict(emotions[emotion])
        sample["ref_audio_path"] = self._resolve_path(sample["ref_audio_path"])
        sample["prompt_text"] = self._prompt_text(sample)
        sample["inp_refs_path"] = self._resolve_optional_path(sample.get("inp_refs_path"))

        if not sample["ref_audio_path"].exists():
            raise FileNotFoundError(f"参考音频不存在：{sample['ref_audio_path']}")
        if sample["inp_refs_path"] and not sample["inp_refs_path"].exists():
            raise FileNotFoundError(f"参考音频目录不存在：{sample['inp_refs_path']}")
        return sample

    def _prompt_text(self, sample: dict[str, Any]) -> str:
        if sample.get("prompt_text"):
            return str(sample["prompt_text"]).strip()

        prompt_text_path = self._resolve_optional_path(sample.get("prompt_text_path"))
        if prompt_text_path and prompt_text_path.exists():
            return prompt_text_path.read_text(encoding="utf-8").strip()

        return sample["ref_audio_path"].stem

    def _tts_params(self, sample: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
        params = dict(self.config.get("tts_params", {}))
        for key in (
            "top_p",
            "temperature",
            "inp_refs",
            "how_to_cut",
            "pause_second",
            "speed",
            "top_k",
            "sentence_chunking",
            "max_chunk_chars",
            "max_chunk_sentences",
        ):
            if key in overrides:
                params[key] = overrides[key]

        params.setdefault("top_p", 1)
        params.setdefault("temperature", 1)
        params.setdefault("how_to_cut", "凑50字一切")
        params.setdefault("pause_second", 0.3)
        params.setdefault("speed", 1)
        params.setdefault("top_k", 15)
        params.setdefault("sentence_chunking", True)
        params.setdefault("max_chunk_chars", 36)
        params.setdefault("max_chunk_sentences", 2)

        inp_refs = params.get("inp_refs")
        if inp_refs in (None, "", "{emotion.inp_refs_path}"):
            inp_refs = sample.get("inp_refs_path")
        params["inp_refs"] = self._normalize_inp_refs(inp_refs)
        params["top_p"] = float(params["top_p"])
        params["temperature"] = float(params["temperature"])
        params["pause_second"] = float(params["pause_second"])
        params["speed"] = float(params["speed"])
        params["top_k"] = int(params["top_k"])
        params["sentence_chunking"] = bool(params["sentence_chunking"])
        params["max_chunk_chars"] = int(params["max_chunk_chars"])
        params["max_chunk_sentences"] = int(params["max_chunk_sentences"])
        return params

    def _normalize_tts_text(self, text: str) -> str:
        text = self._clean_tts_punctuation(text.strip())
        text = re.sub(r"[、，,]+$", "。", text)
        if not text:
            return "。"
        terminal_marks = "。！？!?…」』）)]"
        return text if text[-1] in terminal_marks else f"{text}。"

    def _split_tts_text(self, text: str, params: dict[str, Any]) -> list[str]:
        if not params.get("sentence_chunking", True):
            return [self._finalize_tts_chunk(text)]

        max_chars = max(8, int(params.get("max_chunk_chars") or 36))
        max_sentences = max(1, int(params.get("max_chunk_sentences") or 2))
        sentences = [
            match.group(0).strip()
            for match in re.finditer(r"[^。！？!?]+[。！？!?]*", text)
            if match.group(0).strip()
        ] or [text]

        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for sentence in sentences:
            sentence_parts = self._split_long_tts_sentence(sentence, max_chars)
            for part in sentence_parts:
                part = self._clean_tts_punctuation(part)
                next_len = current_len + len(part)
                should_flush = current and (next_len > max_chars or len(current) >= max_sentences)
                if should_flush and self._chunk_must_continue("".join(current)):
                    should_flush = False
                if should_flush:
                    chunks.append("".join(current))
                    current = []
                    current_len = 0

                current.append(part)
                current_len += len(part)

        if current:
            chunks.append("".join(current))
        return self._merge_unsafe_tts_chunks(chunks, max_chars) or [self._finalize_tts_chunk(text)]

    def _split_long_tts_sentence(self, sentence: str, max_chars: int) -> list[str]:
        if len(sentence) <= max_chars:
            return [sentence]

        parts = [
            match.group(0).strip()
            for match in re.finditer(r"[^、，,；;：:]+[、，,；;：:]*", sentence)
            if match.group(0).strip()
        ]
        if len(parts) <= 1:
            return [sentence]

        chunks: list[str] = []
        current = ""
        for part in parts:
            if current and len(current) + len(part) > max_chars and not self._chunk_must_continue(current):
                chunks.append(current)
                current = part
            else:
                current += part
        if current:
            chunks.append(current)
        return chunks

    def _clean_tts_punctuation(self, text: str) -> str:
        text = re.sub(r"[、，,；;：:]+([。！？!?])", r"\1", text.strip())
        text = re.sub(r"[、，,]{2,}", "、", text)
        text = re.sub(r"。{2,}", "。", text)
        return text

    def _chunk_must_continue(self, text: str) -> bool:
        compact = re.sub(r"\s+", "", text or "")
        if not compact:
            return False
        if compact.endswith(UNSAFE_TTS_CHUNK_ENDINGS):
            return True
        return compact in SHORT_TTS_OPENERS

    def _merge_unsafe_tts_chunks(self, chunks: list[str], max_chars: int) -> list[str]:
        merged: list[str] = []
        for chunk in chunks:
            chunk = self._clean_tts_punctuation(chunk)
            if not chunk:
                continue
            if not merged:
                merged.append(chunk)
                continue

            previous = merged[-1]
            if self._chunk_must_continue(previous):
                merged[-1] = previous + chunk
            else:
                merged.append(chunk)

        finalized = []
        for chunk in merged:
            chunk = self._finalize_tts_chunk(chunk)
            if chunk:
                finalized.append(chunk)
        return finalized

    def _finalize_tts_chunk(self, chunk: str) -> str:
        chunk = self._clean_tts_punctuation(chunk)
        chunk = re.sub(r"[、，,]+$", "。", chunk)
        terminal_marks = "。！？!?…」』）)]"
        return chunk if chunk and chunk[-1] in terminal_marks else f"{chunk}。"

    def _combine_audio_results(self, result_list: list[Any]) -> tuple[int, Any]:
        if len(result_list) == 1:
            return result_list[0]

        import numpy as np

        sampling_rates = {int(rate) for rate, _ in result_list}
        if len(sampling_rates) != 1:
            raise RuntimeError(f"GPT-SoVITS 返回了不同采样率的音频片段：{sorted(sampling_rates)}")

        audio_chunks = [audio for _, audio in result_list if audio is not None and len(audio) > 0]
        if not audio_chunks:
            raise RuntimeError("GPT-SoVITS 返回的音频片段为空。")

        return sampling_rates.pop(), np.concatenate(audio_chunks)

    def _normalize_inp_refs(self, inp_refs: Any) -> Any:
        if inp_refs is None:
            return None
        if isinstance(inp_refs, Path):
            return str(inp_refs)
        if isinstance(inp_refs, str):
            return str(self._resolve_path(inp_refs))
        if isinstance(inp_refs, list):
            normalized = []
            for item in inp_refs:
                if isinstance(item, str):
                    normalized.append(str(self._resolve_path(item)))
                else:
                    normalized.append(item)
            return normalized
        return inp_refs

    def _new_output_path(self, emotion: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"spica_{stamp}_{emotion}_{uuid.uuid4().hex[:8]}.wav"
        return self.output_dir / filename

    def _chunk_output_path(self, output_wav_path: Path, index: int) -> Path:
        return output_wav_path.with_name(f"{output_wav_path.stem}_chunk{index:02d}{output_wav_path.suffix}")

    def _resolve_optional_path(self, path_value: str | Path | None) -> Path | None:
        if not path_value:
            return None
        return self._resolve_path(path_value)

    def _resolve_path(self, path_value: str | Path) -> Path:
        path = Path(path_value)
        if path.is_absolute():
            return path
        return (self.config_dir / path).resolve()

    def _json_safe_params(self, params: dict[str, Any]) -> dict[str, Any]:
        safe = dict(params)
        if isinstance(safe.get("inp_refs"), Path):
            safe["inp_refs"] = str(safe["inp_refs"])
        return safe
