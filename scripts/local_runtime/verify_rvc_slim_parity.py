"""RVC Slim Step2B: original Applio vs slim Applio wav-to-wav parity.

Compares the RVC slim runtime (``artifacts/rvc_slim``: ``base/`` as the Applio root +
``characters/spica/{model,index}``) against the ORIGINAL Applio tree, on the SAME short
cached vocal / params / model / index / contentvec / rmvpe (the slim files are
byte-identical copies), through the SAME ``infer_spica_vocal`` entry + the SAME
``audio_metrics`` comparator.

Why subprocesses: ``rvc.py::_load_core`` caches the loaded Applio ``core`` module
GLOBALLY and the Applio import tree persists in ``sys.modules``; two roots cannot
coexist in one process (the 2nd reuses the 1st's modules). So each root synthesizes in
its OWN ``worker`` subprocess, writes a wav, and the parent (``compare``) diffs them.

Boundaries: does NOT touch production rvc.py / SongPipeline / sing_song / TTS /
GPT-SoVITS / config / env. Reads the original Applio + the slim artifact + a cached
vocal (no download, no song pipeline, no netease). wavs + report go to a gitignored
scratch dir. The original Applio + slim artifact are READ-ONLY.

Modes (run as separate calls so each GPU step stays bounded):
  prepare  -- build both specs, preflight (paths + slim import-check subprocess). NO GPU here.
  worker --spec <spec.json>  -- infer_spica_vocal for ONE root; save a wav.
  compare  -- audio_metrics(original, slim); print table + write report.

ENV NAMES (§3.3): reads NONE.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

WAVEFORM_TOL = 1e-3
NOISE_FLOOR = 6.6e-4  # the TTS A1 self-vs-self floor, as a reference point
DEFAULT_SCRATCH = "artifacts/parity/rvc_slim_step2b"  # gitignored (artifacts/parity/)
CACHED_VOCAL = "static/generated_song/cache/separated/c42682e1e399528523411307/vocals.wav"
TRIM_SECONDS = 12

_PARAM_KEYS = ("f0_method", "transpose", "index_rate", "protect", "device",
               "volume_envelope", "embedder_model", "sid")


# ---- spec construction (no GPU) ----------------------------------------------

def _voice_params(voice: dict) -> dict:
    return {
        "f0_method": voice.get("f0_method", "rmvpe"),
        "transpose": voice.get("transpose", 0),
        "index_rate": voice.get("index_rate", 0.75),
        "protect": voice.get("protect", 0.33),
        "device": voice.get("device", "cuda"),
        "volume_envelope": voice.get("volume_envelope", 1.0),
        "embedder_model": voice.get("embedder_model", "contentvec"),
        "sid": voice.get("sid", 0),
    }


def build_specs(scratch: Path, seed: int) -> tuple[dict, dict]:
    # song voice config (default DEFAULT_CONFIG embeds the spica voice + its params).
    from agent_tools.function_tools.song.config import load_song_config
    song = load_song_config()
    voice = song["rvc"]["voices"]["spica"]
    params = _voice_params(voice)
    input_vocal = str((scratch / "rvc_parity_in.wav").resolve())

    applio = _REPO_ROOT / "agent_tools/function_tools/song/Applio"
    original = {
        "side": "original",
        "applio_root": str(applio.resolve()),
        "model_path": str((applio / "logs/spica/spica_200e_57000s.pth").resolve()),
        "index_path": str((applio / "logs/spica/spica.index").resolve()),
        "input_vocal": input_vocal,
        "output_wav": str((scratch / "out_original.wav").resolve()),
        "params": params, "seed": seed,
    }
    slim_root = _REPO_ROOT / "artifacts/rvc_slim"
    slim_spec = {
        "side": "slim",
        "applio_root": str((slim_root / "base").resolve()),
        "model_path": str((slim_root / "characters/spica/model/spica_200e_57000s.pth").resolve()),
        "index_path": str((slim_root / "characters/spica/index/spica.index").resolve()),
        "input_vocal": input_vocal,
        "output_wav": str((scratch / "out_slim.wav").resolve()),
        "params": params, "seed": seed,
    }
    return original, slim_spec


def _preflight(original: dict, slim: dict) -> list[str]:
    problems = []
    for spec in (original, slim):
        for key in ("applio_root", "model_path", "index_path"):
            if not os.path.exists(spec[key]):
                problems.append(f"{spec['side']}: missing {key}: {spec[key]}")
    base = Path(slim["applio_root"])
    for rel in ("core.py", "rvc/infer/infer.py",
                "rvc/models/embedders/contentvec/pytorch_model.bin",
                "rvc/models/predictors/rmvpe.pt"):
        if not (base / rel).exists():
            problems.append(f"slim base missing load-bearing asset: {rel}")
    return problems


def _slim_import_check(base_root: str) -> tuple[bool, str | None]:
    """Re-run the build's import preflight (fresh -B subprocess) before audio parity."""
    builder = _REPO_ROOT / "scripts/local_runtime/build_rvc_slim.py"
    r = subprocess.run([sys.executable, "-B", str(builder), "--import-root", base_root],
                       capture_output=True, text=True, timeout=900)
    for line in reversed(r.stdout.strip().splitlines()):
        try:
            p = json.loads(line)
            return bool(p["ok"]), p.get("detail")
        except Exception:
            continue
    return False, f"import-check subprocess failed (rc={r.returncode}): {r.stderr.strip()[-300:]}"


# ---- worker (one root, GPU) --------------------------------------------------

def run_worker(spec_path: str) -> int:
    import soundfile as sf  # noqa: F401  (ensures the dep is present; rvc writes the wav)
    import torch

    from agent_tools.function_tools.song.rvc import infer_spica_vocal

    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    torch.manual_seed(spec["seed"])
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(spec["seed"])

    out = infer_spica_vocal(
        input_vocal_path=spec["input_vocal"],
        output_vocal_path=spec["output_wav"],
        model_path=spec["model_path"],
        index_path=spec["index_path"],
        applio_root=spec["applio_root"],
        **spec["params"],
    )
    print(f"[{spec['side']}] output={out} exists={os.path.exists(str(out))}", flush=True)
    return 0


# ---- compare (no GPU) --------------------------------------------------------

def _sha256(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_compare(scratch: Path) -> int:
    import soundfile as sf
    from spica.local_runtime.parity.comparators import audio_metrics

    orig = scratch / "out_original.wav"
    slim = scratch / "out_slim.wav"
    for p in (orig, slim):
        if not p.exists():
            print(f"compare: missing worker output: {p}", file=sys.stderr)
            return 1

    a_old, sr0 = sf.read(str(orig))
    a_new, sr1 = sf.read(str(slim))
    m = audio_metrics((sr0, a_old), (sr1, a_new))
    sha_old, sha_new = _sha256(str(orig)), _sha256(str(slim))
    len_equal = len(a_old) == len(a_new)
    bit_identical = sha_old == sha_new
    rmse, mel = m["waveform_rmse"], m["mel_mean_db"]
    verdict = "PASS" if (rmse <= WAVEFORM_TOL and len_equal) else "FAIL"

    print(f"\n{'metric':14s} original   slim")
    print(f"{'length':14s} {len(a_old):9d} {len(a_new):9d}  equal={len_equal}")
    print(f"{'sample_rate':14s} {sr0:9d} {sr1:9d}")
    print(f"  RMSE          : {rmse:.3e}   (gate <= {WAVEFORM_TOL:.0e}, noise floor {NOISE_FLOOR:.1e})")
    print(f"  max_abs_diff  : {m['waveform_max']:.3e}")
    print(f"  mel_mean_db   : {mel:.4f}")
    print(f"  len_ratio     : {m['len_ratio']:.4f}")
    print(f"  bit_identical : {bit_identical}")
    print(f"  VERDICT       : {verdict}")

    report = {
        "verdict": verdict, "gate_rmse": WAVEFORM_TOL, "noise_floor": NOISE_FLOOR,
        "length_original": len(a_old), "length_slim": len(a_new), "length_equal": len_equal,
        "rmse": rmse, "max_abs_diff": m["waveform_max"], "mel_mean_db": mel,
        "len_ratio": m["len_ratio"], "sha256_original": sha_old, "sha256_slim": sha_new,
        "bit_identical": bit_identical,
    }
    (scratch / "parity_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"report: {scratch / 'parity_report.json'}")
    return 0 if verdict == "PASS" else 1


# ---- CLI ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RVC Slim Step2B original-vs-slim wav parity.")
    ap.add_argument("mode", choices=["prepare", "worker", "compare"])
    ap.add_argument("--spec", default=None, help="spec JSON (worker mode)")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--scratch", default=DEFAULT_SCRATCH)
    args = ap.parse_args(argv)

    scratch = Path(args.scratch)
    if not scratch.is_absolute():
        scratch = _REPO_ROOT / scratch

    if args.mode == "worker":
        if not args.spec:
            print("worker mode needs --spec", file=sys.stderr)
            return 2
        return run_worker(args.spec)

    if args.mode == "compare":
        return run_compare(scratch)

    # prepare
    scratch.mkdir(parents=True, exist_ok=True)
    import soundfile as sf
    src = _REPO_ROOT / CACHED_VOCAL
    if not src.exists():
        print(f"prepare: cached vocal not found: {src}", file=sys.stderr)
        return 1
    info = sf.info(str(src))
    nframes = min(info.frames, int(info.samplerate * TRIM_SECONDS))
    data, srate = sf.read(str(src), frames=nframes, always_2d=True)
    sf.write(str(scratch / "rvc_parity_in.wav"), data, srate)

    original, slim = build_specs(scratch, args.seed)
    problems = _preflight(original, slim)
    (scratch / "spec_original.json").write_text(json.dumps(original, indent=2, ensure_ascii=False), encoding="utf-8")
    (scratch / "spec_slim.json").write_text(json.dumps(slim, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[original] root={original['applio_root']}")
    print(f"[slim]     root={slim['applio_root']}")
    print(f"specs written under: {scratch}")
    if problems:
        print("PREFLIGHT PROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1

    ok, detail = _slim_import_check(slim["applio_root"])
    print(f"slim import-check: {'PASS' if ok else 'FAIL'}" + (f"  ({detail})" if detail else ""))
    if not ok:
        print("import preflight FAILED -- not running audio parity.", file=sys.stderr)
        return 1
    print("preflight: OK (paths exist, slim import PASS)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
