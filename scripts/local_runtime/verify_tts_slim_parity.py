"""B1 step4: slim runtime vs ORIGINAL vendored audio_diff parity (LOCAL_RUNTIME_PLAN).

Compares the GPT-SoVITS slim runtime (``artifacts/tts_slim``: ``base/`` as the
gptsovits_root + ``characters/spcia/`` pack) against the ORIGINAL vendored tree, on the
SAME seed / 4 JA texts / 4 emotions / params / weights (the slim files are byte-identical
copies), through the SAME ``GptSovitsV2ProDriver`` + the SAME ``audio_metrics`` comparator.

Why subprocesses: the vendored ``inference_webui`` keeps module-GLOBAL model state and
loads BERT / cnhubert / sv at import time relative to cwd (``now_dir``). Two roots cannot
coexist in one process (the 2nd import reuses the 1st's module + pretrained models). So
each root synthesizes in its OWN subprocess (``worker`` mode), writes wavs, and the parent
(``compare``) diffs the two wav sets.

Boundaries: does NOT touch production driver.py / service.py / TTSPort / run_turn /
orchestrator / ChatEngine, does NOT change any default, does NOT switch the slim runtime
in. The ORIGINAL ``weight.json`` is snapshot+restored (vendored stays byte-identical); the
slim ``weight.json`` IS created (writability evidence) and kept. wavs + specs + report go
to a gitignored scratch dir. No ONNX / TensorRT / Genie.

Modes (run as separate calls so each GPU step stays bounded):
  prepare  -- build both specs, preflight (paths exist, slim base has the pretrained
              models, inp_refs glob isolation), write spec JSONs. NO GPU.
  worker <spec.json>  -- load driver(root)+weights, synth 4 texts x 4 emotions, save wavs.
  compare  -- audio_metrics every (emotion,text) pair; print table + write report.

ENV NAMES (S3.3): reads NONE.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
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
EMOTIONS = ["happy", "angry", "sad", "surprised"]
WAVEFORM_TOL = 1e-3      # parity gate (A1 noise floor threshold)
NOISE_FLOOR = 6.6e-4     # the A1/A2/A3 self-vs-self max RMSE to approach
DEFAULT_SCRATCH = "artifacts/parity/tts_slim_stepd"   # gitignored (artifacts/parity/)


# ---- spec construction (no GPU) ----------------------------------------------

def _resolve_against(base: Path, p: str) -> str:
    q = Path(p)
    return str(q.resolve() if q.is_absolute() else (base / q).resolve())


def _read_prompt(emotion_cfg: dict, base: Path, ref_wav: str) -> str:
    if emotion_cfg.get("prompt_text"):
        return str(emotion_cfg["prompt_text"]).strip()
    ptp = emotion_cfg.get("prompt_text_path")
    if ptp:
        path = Path(_resolve_against(base, ptp))
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return Path(ref_wav).stem


def _tts_params(cfg: dict) -> dict:
    p = dict(cfg.get("tts_params", {}))
    return {
        "top_p": p.get("top_p", 1),
        "temperature": p.get("temperature", 1),
        "top_k": p.get("top_k", 15),
        "pause_second": p.get("pause_second", 0.3),
        "speed": p.get("speed", 1),
    }


def build_original_spec(scratch: Path, seed: int) -> dict:
    import yaml
    cfg_path = _REPO_ROOT / "data" / "config" / "tts.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    base = cfg_path.parent  # _resolve_path resolves relative to the config dir
    emotions = {}
    for emo in EMOTIONS:
        e = cfg["emotions"][emo]
        ref = _resolve_against(base, e["ref_audio_path"])
        emotions[emo] = {
            "ref_wav": ref,
            "prompt": _read_prompt(e, base, ref),
            "inp_refs_dir": _resolve_against(base, e["inp_refs_path"]) if e.get("inp_refs_path") else None,
        }
    return {
        "side": "original",
        "root": _resolve_against(base, cfg["gptsovits_root"]),
        "gpt_weight": _resolve_against(base, cfg["gpt_model_path"]),
        "sovits_weight": _resolve_against(base, cfg["sovits_model_path"]),
        "ref_language": cfg.get("ref_language", "日文"),
        "target_language": cfg.get("target_language", "日文"),
        "tts_params": _tts_params(cfg),
        "emotions": emotions,
        "texts": REFERENCE_TEXTS,
        "seed": seed,
        "out_dir": str(scratch / "wav_original"),
        "restore_weight_json": True,   # keep the vendored tree byte-identical
    }


def build_slim_spec(scratch: Path, seed: int, tts_params: dict) -> dict:
    import yaml
    slim = _REPO_ROOT / "artifacts" / "tts_slim"
    base_root = slim / "base"
    pack = slim / "characters" / "spcia"
    cy = yaml.safe_load((pack / "character.yaml").read_text(encoding="utf-8"))
    emotions = {}
    for emo in EMOTIONS:
        e = cy["emotions"][emo]
        ref = _resolve_against(pack, e["ref_audio_path"])
        emotions[emo] = {
            "ref_wav": ref,
            "prompt": _read_prompt(e, pack, ref),
            "inp_refs_dir": _resolve_against(pack, e["inp_refs_path"]) if e.get("inp_refs_path") else None,
        }
    return {
        "side": "slim",
        "root": str(base_root.resolve()),
        "gpt_weight": _resolve_against(pack, cy["gpt_model_path"]),
        "sovits_weight": _resolve_against(pack, cy["sovits_model_path"]),
        "ref_language": cy.get("ref_language", "日文"),
        "target_language": cy.get("target_language", "日文"),
        "tts_params": tts_params,   # SAME params as original (the pack carries none)
        "emotions": emotions,
        "texts": REFERENCE_TEXTS,
        "seed": seed,
        "out_dir": str(scratch / "wav_slim"),
        "restore_weight_json": False,  # slim weight.json is created + kept (writability)
    }


def _preflight(spec: dict) -> list[str]:
    """Return a list of problems (empty = ok)."""
    problems = []
    for key in ("root", "gpt_weight", "sovits_weight"):
        if not os.path.exists(spec[key]):
            problems.append(f"{spec['side']}: missing {key}: {spec[key]}")
    for emo, e in spec["emotions"].items():
        if not os.path.isfile(e["ref_wav"]):
            problems.append(f"{spec['side']}/{emo}: missing ref_wav {e['ref_wav']}")
        if e["inp_refs_dir"]:
            if not os.path.isdir(e["inp_refs_dir"]):
                problems.append(f"{spec['side']}/{emo}: missing inp_refs dir {e['inp_refs_dir']}")
            else:
                refs = sorted(glob.glob(os.path.join(e["inp_refs_dir"], "*.wav")))
                if len(refs) != 4:
                    problems.append(f"{spec['side']}/{emo}: inp_refs glob found {len(refs)} wavs (want 4)")
                if os.path.abspath(e["ref_wav"]) in {os.path.abspath(r) for r in refs}:
                    problems.append(f"{spec['side']}/{emo}: primary ref leaked into refs/ glob")
    if spec["side"] == "slim":  # the pruned base must still carry every loaded asset
        base = Path(spec["root"])
        for rel in (
            "GPT_SoVITS/inference_webui.py",
            "tools/__init__.py",
            "tools/assets.py",     # inference_webui:128 module-level import (the B1-step4 gap)
            "tools/my_utils.py",   # module/data_utils.py:11 module-level import
            "tools/i18n/i18n.py",
            "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
            "GPT_SoVITS/pretrained_models/chinese-hubert-base",
            "GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt",
            "GPT_SoVITS/pretrained_models/fast_langdetect/lid.176.bin",
        ):
            if not (base / rel).exists():
                problems.append(f"slim base missing load-bearing asset: {rel}")
    return problems


def import_check(root: str, importer=None) -> tuple[bool, str | None]:
    """Actually IMPORT the vendored inference from ``root`` (this process is the fresh
    subprocess). Returns (ok, detail); on a missing module reports the module name, not
    a stack. ``importer`` is injectable for tests (default = the real driver import)."""
    if importer is None:
        from spica.local_runtime.tts.model_imports import import_gptsovits_inference
        importer = import_gptsovits_inference
    try:
        importer(root)
        return True, None
    except ModuleNotFoundError as exc:
        return False, f"ModuleNotFoundError: No module named {exc.name!r}" if exc.name else f"ModuleNotFoundError: {exc}"
    except Exception as exc:  # any import-time failure blocks parity
        return False, f"{type(exc).__name__}: {exc}"


def run_import_check(root: str) -> int:
    ok, detail = import_check(root)
    if ok:
        print(f"import-check OK: {root}")
        return 0
    print(f"import-check FAILED (blocking parity): {root}\n  {detail}", file=sys.stderr)
    return 1


def _print_spec(spec: dict) -> None:
    print(f"[{spec['side']}] root={spec['root']}")
    print(f"   gpt   ={spec['gpt_weight']}")
    print(f"   sovits={spec['sovits_weight']}")
    for emo, e in spec["emotions"].items():
        nrefs = len(glob.glob(os.path.join(e["inp_refs_dir"], "*.wav"))) if e["inp_refs_dir"] else 0
        print(f"   {emo:10s} ref={os.path.basename(e['ref_wav'])[:34]:34s} inp_refs={nrefs} prompt={e['prompt'][:18]!r}")


# ---- worker (one root, GPU) --------------------------------------------------

def run_worker(spec_path: str) -> int:
    import numpy as np
    import soundfile as sf
    import torch

    from spica.local_runtime.tts.driver import GptSovitsV2ProDriver

    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    out_dir = Path(spec["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    def seed_all():
        torch.manual_seed(spec["seed"])
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(spec["seed"])

    def combine(pieces):
        srs = {int(sr) for sr, _ in pieces}
        audios = [a for _, a in pieces if a is not None and len(a) > 0]
        if len(srs) != 1 or not audios:
            raise RuntimeError(f"unexpected synthesis pieces: sr={srs} n={len(audios)}")
        return srs.pop(), np.concatenate(audios)

    wj = Path(spec["root"]) / "weight.json"
    wj_backup = wj.read_bytes() if wj.exists() else None
    driver = GptSovitsV2ProDriver(spec["root"])
    try:
        i18n = driver.i18n  # triggers the one-time vendored import (loads BERT/cnhubert/sv)
        driver.load(
            gpt_path=spec["gpt_weight"], sovits_path=spec["sovits_weight"],
            prompt_language=i18n(spec["ref_language"]), text_language=i18n(spec["target_language"]),
            force=False,
        )
        p = spec["tts_params"]
        for emo, e in spec["emotions"].items():
            for idx, text in enumerate(spec["texts"]):
                seed_all()
                pieces = list(driver.synthesize_chunks(
                    ref_wav_path=e["ref_wav"], prompt_text=e["prompt"],
                    prompt_language=i18n(spec["ref_language"]), text=text,
                    text_language=i18n(spec["target_language"]),
                    top_p=p["top_p"], temperature=p["temperature"], inp_refs=e["inp_refs_dir"],
                    how_to_cut="不切", pause_second=p["pause_second"], speed=p["speed"],
                    top_k=p["top_k"], ref_free=False,
                ))
                sr, audio = combine(pieces)
                sf.write(str(out_dir / f"{emo}__{idx}.wav"), audio, sr)
                print(f"[{spec['side']}] {emo} #{idx}  sr={sr}  len={len(audio)}", flush=True)
    finally:
        # weight.json hygiene: restore the ORIGINAL vendored's file (byte-identical),
        # keep the slim's (writability evidence).
        if spec.get("restore_weight_json"):
            if wj_backup is not None:
                wj.write_bytes(wj_backup)
            elif wj.exists():
                wj.unlink()
    return 0


# ---- compare (no GPU) --------------------------------------------------------

def run_compare(scratch: Path) -> int:
    import soundfile as sf
    from spica.local_runtime.parity.comparators import audio_metrics

    orig_dir = scratch / "wav_original"
    slim_dir = scratch / "wav_slim"
    rows = []
    for emo in EMOTIONS:
        for idx in range(len(REFERENCE_TEXTS)):
            name = f"{emo}__{idx}.wav"
            a_old, sr0 = sf.read(str(orig_dir / name))
            a_new, sr1 = sf.read(str(slim_dir / name))
            m = audio_metrics((sr0, a_old), (sr1, a_new))
            rows.append({
                "emotion": emo, "text_idx": idx,
                "rmse": m["waveform_rmse"], "mel_mean_db": m["mel_mean_db"],
                "len_original": len(a_old), "len_slim": len(a_new),
                "len_equal": len(a_old) == len(a_new),
                "len_ratio": m["len_ratio"],
            })

    max_rmse = max(r["rmse"] for r in rows)
    max_mel = max(abs(r["mel_mean_db"]) for r in rows)
    all_len_equal = all(r["len_equal"] for r in rows)
    verdict = "PASS" if (max_rmse <= WAVEFORM_TOL and all_len_equal) else "FAIL"

    print(f"\n{'emotion':10s} {'idx':3s} {'rmse':>10s} {'mel_db':>9s} "
          f"{'len_orig':>9s} {'len_slim':>9s} {'eq':>3s}")
    for r in rows:
        print(f"{r['emotion']:10s} {r['text_idx']:<3d} {r['rmse']:10.3e} {r['mel_mean_db']:9.3f} "
              f"{r['len_original']:9d} {r['len_slim']:9d} {'Y' if r['len_equal'] else 'N':>3s}")
    print(f"\nmax RMSE       : {max_rmse:.3e}  (gate <= {WAVEFORM_TOL:.0e}, noise floor {NOISE_FLOOR:.1e})")
    print(f"max |mel_db|   : {max_mel:.3f}")
    print(f"all len equal  : {all_len_equal}")
    print(f"near noise floor: {max_rmse <= NOISE_FLOOR * 1.5}")
    print(f"VERDICT        : {verdict}")

    report = {
        "verdict": verdict,
        "gate_rmse": WAVEFORM_TOL, "noise_floor": NOISE_FLOOR,
        "max_rmse": max_rmse, "max_mel_db": max_mel, "all_len_equal": all_len_equal,
        "rows": rows,
    }
    (scratch / "parity_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"report: {scratch / 'parity_report.json'}")
    return 0 if verdict == "PASS" else 1


# ---- CLI ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="B1 step4 slim-vs-vendored TTS parity.")
    ap.add_argument("mode", choices=["prepare", "worker", "compare", "import-check"])
    ap.add_argument("spec", nargs="?", help="spec JSON (worker) or root (import-check)")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--scratch", default=DEFAULT_SCRATCH)
    args = ap.parse_args(argv)

    scratch = Path(args.scratch)
    if not scratch.is_absolute():
        scratch = _REPO_ROOT / scratch

    if args.mode == "import-check":
        root = args.spec or str(_REPO_ROOT / "artifacts" / "tts_slim" / "base")
        return run_import_check(root)

    if args.mode == "worker":
        if not args.spec:
            print("worker mode needs a spec.json path", file=sys.stderr)
            return 2
        return run_worker(args.spec)

    if args.mode == "compare":
        return run_compare(scratch)

    # prepare
    scratch.mkdir(parents=True, exist_ok=True)
    original = build_original_spec(scratch, args.seed)
    slim = build_slim_spec(scratch, args.seed, original["tts_params"])
    problems = _preflight(original) + _preflight(slim)
    _print_spec(original)
    _print_spec(slim)
    (scratch / "spec_original.json").write_text(json.dumps(original, indent=2, ensure_ascii=False), encoding="utf-8")
    (scratch / "spec_slim.json").write_text(json.dumps(slim, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nspecs written under: {scratch}")
    if problems:
        print("PREFLIGHT PROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("preflight: OK (all paths exist, slim base complete, inp_refs glob isolated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
