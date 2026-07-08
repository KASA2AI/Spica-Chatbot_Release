"""TTS parity executor (LOCAL_RUNTIME_PLAN cut 2 / §6.1, A1 + A2).

Scores two TTS synth paths on a FIXED reference text set with ``audio_metrics``
(waveform + log-mel, D3) and archives the metrics (NOT raw audio) to
``artifacts/parity/`` (gitignored). Fixed RNG seed (near-determinism). It does NOT
modify service.py / the runtime.

Modes:
- ``self`` (A1): ``GPTSoVITSTool.synthesize`` vs itself under a fixed seed -- the
  old-vs-old self-check that anchors the harness + the near-determinism noise floor.
- ``driver`` (A2): the vendored ``get_tts_wav`` called DIRECTLY vs through the
  ``GptSovitsV2ProDriver`` -- same args / same loaded models / same seed. Proves the
  A2 service->driver source swap is behaviour-preserving. PARITY GATE: must be <=
  the A1 noise floor (RMSE <= 1e-3, len_ratio ~1) before A3.

MANUAL-ACCEPTANCE tool (needs the real GPT-SoVITS model + GPU), never CI (§6.5). Run:

  python -m scripts.local_runtime.verify_tts_parity [--mode self|driver] [--seed 1234]

ENV NAMES (§3.3): reads NONE here.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

REFERENCE_TEXTS = [
    "はい。",
    "今日はいい天気ですね。",
    "ふぅん……男の価値観って、結局そういうものなのね。",
    "もちろん。麦のことだから、ちゃんと覚えてるよ。さあ、続きを始めよう。",
]
EMOTION = "happy"
WAVEFORM_TOL = 1e-3  # A1 noise floor


def _seed(seed: int) -> None:
    import torch

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def _combine(pieces):
    import numpy as np

    srs = {int(sr) for sr, _ in pieces}
    audios = [a for _, a in pieces if a is not None and len(a) > 0]
    if len(srs) != 1 or not audios:
        raise RuntimeError(f"unexpected synthesis pieces: sr={srs} n_audio={len(audios)}")
    return srs.pop(), np.concatenate(audios)


def _run_self(tool, seed: int, emotion: str):
    import soundfile as sf

    def synth(text):
        _seed(seed)
        r = tool.synthesize(text=text, emotion=emotion)
        if not r.get("ok"):
            raise RuntimeError(r.get("error"))
        audio, sr = sf.read(r["audio_path"])
        return int(sr), audio

    return [(t, synth(t), synth(t)) for t in REFERENCE_TEXTS]


def _run_driver(tool, seed: int, emotion: str):
    """vendored-direct get_tts_wav vs the driver, same args/models/seed."""
    from spica.local_runtime.tts.model_imports import import_gptsovits_inference, pushd

    tool.warmup(synthesize=False)  # builds + loads the driver (sets the vendored model globals)
    driver = tool._ensure_driver()
    _, _, get_tts_wav, i18n = import_gptsovits_inference(tool.gptsovits_root)  # same cached callables

    sample = tool._emotion_sample(emotion)
    params = tool._tts_params(sample, {})
    ref_lang = sample.get("ref_language") or tool.config.get("ref_language", "日文")
    tgt_lang = tool.config.get("target_language", "日文")

    def args_for(text):
        return dict(
            ref_wav_path=str(sample["ref_audio_path"]),
            prompt_text=sample["prompt_text"],
            prompt_language=i18n(ref_lang),
            text=text,
            text_language=i18n(tgt_lang),
            top_p=params["top_p"],
            temperature=params["temperature"],
            inp_refs=params["inp_refs"],
            how_to_cut="不切",
            pause_second=params["pause_second"],
            speed=params["speed"],
            top_k=params["top_k"],
            ref_free=False,
        )

    out = []
    for text in REFERENCE_TEXTS:
        _seed(seed)
        with pushd(tool.gptsovits_root):
            direct = _combine(list(get_tts_wav(**args_for(text))))
        _seed(seed)
        via_driver = _combine(list(driver.synthesize_chunks(**args_for(text))))
        out.append((text, direct, via_driver))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="TTS parity (self | driver).")
    ap.add_argument("--mode", choices=["self", "driver"], default="self")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--emotion", default=EMOTION)
    ap.add_argument("--out", default="artifacts/parity")
    args = ap.parse_args()

    from agent_tools.tts.gptsovits import GPTSoVITSTool
    from spica.local_runtime.parity.comparators import audio_metrics

    tool = GPTSoVITSTool()
    if args.mode == "self":
        tool.warmup(synthesize=False)
        pairs = _run_self(tool, args.seed, args.emotion)
    else:
        pairs = _run_driver(tool, args.seed, args.emotion)

    per_input = []
    for text, old, new in pairs:
        m = audio_metrics(old, new)
        per_input.append({"text": text, **m})
        print(
            f"[{text[:16]:<16}] rmse={m['waveform_rmse']:.2e} "
            f"mel_mean_db={m['mel_mean_db']} len_ratio={m['len_ratio']:.4f}"
        )

    rmses = [p["waveform_rmse"] for p in per_input]
    ratios = [abs(p["len_ratio"] - 1.0) for p in per_input]
    aggregate = {
        "max_waveform_rmse": max(rmses),
        "mean_waveform_rmse": sum(rmses) / len(rmses),
        "max_len_ratio_dev": max(ratios),
    }
    verdict = "pass" if aggregate["max_waveform_rmse"] <= WAVEFORM_TOL and aggregate["max_len_ratio_dev"] <= 0.02 else "investigate"
    report = {
        "mode": args.mode,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "seed": args.seed,
        "emotion": args.emotion,
        "per_input": per_input,
        "aggregate": aggregate,
        "verdict": verdict,
    }

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = _REPO_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"tts_{args.mode}_{stamp}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nmode: {args.mode}  verdict: {verdict}")
    print(f"max_waveform_rmse: {aggregate['max_waveform_rmse']:.3e}  (gate <= {WAVEFORM_TOL:.0e})")
    print(f"report: {path}")
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
