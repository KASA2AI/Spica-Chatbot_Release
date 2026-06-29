"""TTS parity executor (LOCAL_RUNTIME_PLAN cut 2 / §6.1, A1 self-check).

Runs two TTS synth callables on a FIXED reference text set, scores each pair with
``audio_diff`` (waveform + log-mel, decision D3), and archives the metrics (NOT raw
audio) to ``artifacts/parity/`` (gitignored, §7.3).

A1 mode = ``self``: the vendored ``GPTSoVITSTool`` vs ITSELF under a FIXED RNG seed
-- the old-vs-old self-check that proves the harness + comparator on REAL audio and
establishes the near-determinism noise floor for A2's threshold. It does NOT touch
service.py / the runtime (read-only use of the existing tool). A2 will add a
``driver`` mode (vendored vs local_runtime driver).

MANUAL-ACCEPTANCE tool (needs the real GPT-SoVITS model + GPU), never CI (§6.5). Run:

  python -m scripts.local_runtime.verify_tts_parity [--seed 1234] [--out artifacts/parity]

ENV NAMES (§3.3): reads NONE here. (The vendored tool's own runtime_env shims are
unchanged and out of scope for A1.)
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

# Fixed reference set: varied length / punctuation, single emotion. Versioned here
# so the self-check is reproducible (real ref audio comes from tts.yaml emotions).
REFERENCE_TEXTS = [
    "はい。",
    "今日はいい天気ですね。",
    "ふぅん……男の価値観って、結局そういうものなのね。",
    "もちろん。麦のことだから、ちゃんと覚えてるよ。さあ、続きを始めよう。",
]
EMOTION = "happy"


def _load_wav(path: str):
    import soundfile as sf

    audio, sr = sf.read(path)
    return int(sr), audio


def _seeded_synth(tool, text: str, emotion: str, seed: int):
    import torch

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    result = tool.synthesize(text=text, emotion=emotion)
    if not result.get("ok") or not result.get("audio_path"):
        raise RuntimeError(f"synthesize failed: {result.get('error')}")
    return _load_wav(result["audio_path"])


def main() -> int:
    ap = argparse.ArgumentParser(description="TTS parity self-check (vendored vs vendored).")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--out", default="artifacts/parity")
    ap.add_argument("--emotion", default=EMOTION)
    args = ap.parse_args()

    from agent_tools.tts.gptsovits import GPTSoVITSTool
    from spica.local_runtime.parity.comparators import audio_metrics

    tool = GPTSoVITSTool()
    tool.warmup(synthesize=False)  # load weights once (no env/service change)

    per_input = []
    for text in REFERENCE_TEXTS:
        sr1, a1 = _seeded_synth(tool, text, args.emotion, args.seed)
        sr2, a2 = _seeded_synth(tool, text, args.emotion, args.seed)
        m = audio_metrics((sr1, a1), (sr2, a2))
        per_input.append({"text": text, **m})
        print(
            f"[{text[:16]:<16}] rmse={m['waveform_rmse']:.2e} "
            f"mel_mean_db={m['mel_mean_db']} len_ratio={m['len_ratio']:.4f}"
        )

    rmses = [p["waveform_rmse"] for p in per_input]
    mel_means = [p["mel_mean_db"] for p in per_input if p["mel_mean_db"] is not None]
    aggregate = {
        "max_waveform_rmse": max(rmses),
        "mean_waveform_rmse": sum(rmses) / len(rmses),
        "max_mel_mean_db": max(mel_means) if mel_means else None,
    }
    # self-check verdict: identical input + seed should be near-zero (the noise floor).
    verdict = "pass" if aggregate["max_waveform_rmse"] <= 1e-3 else "investigate"
    report = {
        "mode": "self",
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
    path = out_dir / f"tts_selfcheck_{stamp}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nverdict: {verdict}")
    print(f"max_waveform_rmse: {aggregate['max_waveform_rmse']:.3e}  (self-check noise floor)")
    print(f"report: {path}")
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
