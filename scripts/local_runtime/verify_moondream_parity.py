"""Moondream cut 4: legacy moondream_local vs isolated moondream_hf screen parity.

Compares the relocated ``moondream_hf`` provider (``spica/local_runtime/vision``)
against the LEGACY ``moondream_local`` backend (``agent_tools...backends.moondream``)
on the SAME fixed image / question / config, through the SAME production path
(``analyze_screen_image_local`` -> ``get_moondream_manager`` -> the
``load_moondream_backend`` seam) -- so this exercises the real seam, not a private
load call. OCR is disabled in the parity config to isolate the ONE thing this cut
moves (the Moondream VLM), so the run stays bounded to a single model.

PARITY POSTURE (cut-4 decision 1): this is a MOVE, not an export. The load-bearing
guarantees are the import preflight + the seam zero-diff test + code-equivalence
(``moondream_hf`` body == legacy body) -- all CI-pure. This GPU harness is the
belt-and-suspenders check. ``moondream_hf`` IS the legacy ``from_pretrained`` path
verbatim, so output SHOULD be identical; but VLM generation can carry
non-deterministic micro-diffs even at a fixed seed. So the gate is: prefer
bit-identical text, else normalized-identical, else high text similarity
(>= SIM_GATE) + structural equivalence (same observation schema / engine tags /
error shape). We do NOT add determinism settings or touch ``from_pretrained`` to
chase bit-identical -- parity yields to the zero-diff default.

Why subprocesses: ``get_moondream_manager`` caches a process-global singleton keyed
by config signature, and the HF model + transformers remote code persist in
``sys.modules`` / VRAM. Each side therefore loads in its OWN ``worker`` subprocess;
the parent (``compare``) diffs the two result JSONs.

Boundaries: does NOT touch production model_manager / analyzer / app_host / config /
env. Reads no environment (§3.3). The fixed image + result JSONs + report go to a
gitignored scratch dir (``artifacts/parity/``).

Modes (run as separate calls so each GPU step stays bounded):
  prepare       -- build the fixed image + both specs; path + import preflight. NO GPU.
  worker --spec -- analyze_screen_image_local for ONE provider; save a result JSON.
  compare       -- diff legacy vs hf text + structure; print table + write report.
  import-check  -- (-B subprocess) import moondream_hf, assert structure; print json.
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

SIM_GATE = 0.98  # normalized text similarity floor when not exactly identical
DEFAULT_SCRATCH = "artifacts/parity/moondream_cut4"  # gitignored (artifacts/parity/)
QUESTION = "What is shown on this screen? Describe the windows, text, and UI."
MODE = "full_screen"

# Fixed screen-pipeline config shared by BOTH sides -- identical except `provider`.
# OCR off (isolate Moondream); cuda/bfloat16/revision pinned to the production
# defaults so the legacy and hf loads use the SAME weights + dtype.
_BASE_CONFIG = {
    "enabled": True,
    "model_id": "vikhyatk/moondream2",
    "revision": "2025-06-21",
    "device": "cuda",
    "dtype": "bfloat16",
    "max_side": 768,
    "reasoning": False,
    "preload": False,
    "ocr_enabled": False,
    "ocr_engine": "rapidocr",
    "capture_format": "png",
    "infer_timeout_sec": 60.0,
    "log_timing": False,
    "debug_save_images": False,
}


# ---- spec construction + fixed image (no GPU) --------------------------------

def make_test_image(path: Path) -> None:
    """Deterministic synthetic 'desktop' PNG -- no randomness, so both sides (and
    re-runs) feed the model byte-identical pixels. Drawn with PIL so the harness
    needs no repo asset."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1024, 640), (32, 38, 54))
    d = ImageDraw.Draw(img)
    # a 'window' with a title bar + body
    d.rectangle([80, 70, 944, 560], fill=(245, 246, 250), outline=(120, 130, 150), width=3)
    d.rectangle([80, 70, 944, 120], fill=(70, 110, 200))
    d.text((100, 88), "Spica Screen Parity - Untitled Document", fill=(255, 255, 255))
    # body text lines
    for i, line in enumerate(
        [
            "The quick brown fox jumps over the lazy dog.",
            "Provider parity check: moondream_local vs moondream_hf.",
            "Line three contains a number: 42 items remaining.",
            "Status: OK    [ Save ]   [ Cancel ]",
        ]
    ):
        d.text((110, 160 + i * 48), line, fill=(20, 24, 32))
    # two 'buttons'
    d.rectangle([700, 470, 800, 520], fill=(60, 170, 90))
    d.text((718, 488), "Save", fill=(255, 255, 255))
    d.rectangle([820, 470, 920, 520], fill=(190, 70, 70))
    d.text((838, 488), "Cancel", fill=(255, 255, 255))
    img.save(str(path), format="PNG")


def build_specs(scratch: Path, seed: int) -> tuple[dict, dict]:
    image_path = str((scratch / "moondream_parity_in.png").resolve())
    legacy = {
        "side": "legacy",
        "provider": "moondream_local",
        "install_hf": False,
        "image": image_path,
        "question": QUESTION,
        "mode": MODE,
        "config": dict(_BASE_CONFIG, provider="moondream_local"),
        "result_json": str((scratch / "result_legacy.json").resolve()),
        "seed": seed,
    }
    hf = {
        "side": "hf",
        "provider": "moondream_hf",
        "install_hf": True,
        "image": image_path,
        "question": QUESTION,
        "mode": MODE,
        "config": dict(_BASE_CONFIG, provider="moondream_hf"),
        "result_json": str((scratch / "result_hf.json").resolve()),
        "seed": seed,
    }
    return legacy, hf


def _preflight(legacy: dict, hf: dict) -> list[str]:
    problems = []
    for spec in (legacy, hf):
        if not os.path.exists(spec["image"]):
            problems.append(f"{spec['side']}: missing input image: {spec['image']}")
    # the relocated runtime + its source legacy backend must both exist on disk
    for rel in (
        "spica/local_runtime/vision/moondream_hf.py",
        "agent_tools/function_tools/screen/backends/moondream.py",
        "agent_tools/function_tools/screen/backends/moondream_runtime.py",
    ):
        if not (_REPO_ROOT / rel).exists():
            problems.append(f"missing load-bearing source: {rel}")
    return problems


def _import_check_subprocess() -> tuple[bool, str | None]:
    """Re-run the structural import preflight in a fresh ``-B`` subprocess (no
    ``.pyc`` writes), before the GPU parity. Mirrors the RVC/TTS slim preflight."""
    r = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve()), "import-check"],
        capture_output=True, text=True, timeout=300,
    )
    for line in reversed(r.stdout.strip().splitlines()):
        try:
            p = json.loads(line)
            return bool(p["ok"]), p.get("detail")
        except Exception:
            continue
    return False, f"import-check subprocess failed (rc={r.returncode}): {r.stderr.strip()[-300:]}"


def run_import_check() -> int:
    """Import the relocated runtime + assert its structure -- NO GPU, NO from_pretrained
    (torch/transformers are lazy inside ``.load``). Proves the move did not drop a
    module-level import (the TTS-B1 lesson) and the provider/backend API is intact."""
    detail = None
    try:
        from spica.local_runtime.vision import MoondreamHfBackend, MoondreamHfProvider
        from spica.local_runtime.vision.moondream_hf import _result_to_text, _torch_dtype

        assert hasattr(MoondreamHfBackend, "load"), "MoondreamHfBackend.load missing"
        assert hasattr(MoondreamHfBackend, "query"), "MoondreamHfBackend.query missing"
        assert MoondreamHfProvider.name == "moondream_hf", "provider name drift"
        assert hasattr(MoondreamHfProvider(), "load"), "MoondreamHfProvider.load missing"
        # the helper bodies must be present (they carry the dtype + text extraction)
        assert _torch_dtype.__module__.endswith("moondream_hf")
        assert _result_to_text("x") == "x"
        ok = True
        detail = "moondream_hf import + structure OK"
    except Exception as exc:  # noqa: BLE001 -- report any import/structure failure
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    print(json.dumps({"ok": ok, "detail": detail}), flush=True)
    return 0 if ok else 1


# ---- worker (one provider, GPU) ----------------------------------------------

def run_worker(spec_path: str) -> int:
    import torch

    from PIL import Image

    from agent_tools.function_tools.screen.backends import moondream_runtime
    from agent_tools.function_tools.screen.config import ScreenPipelineConfig
    from agent_tools.function_tools.screen.model_manager import clear_moondream_manager
    from agent_tools.function_tools.screen.analyzer import analyze_screen_image_local

    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    config = ScreenPipelineConfig(**spec["config"])

    # Install (hf) or not (legacy) -- this is the SEAM under test. clear the manager
    # singleton first so the signature reflects THIS subprocess's provider.
    clear_moondream_manager()
    moondream_runtime.reset_active_moondream_provider()
    if spec["install_hf"]:
        from spica.local_runtime.vision import MoondreamHfProvider

        moondream_runtime.set_active_moondream_provider(MoondreamHfProvider())

    image = Image.open(spec["image"]).convert("RGB")

    # Fixed seed before inference (cut-4 decision 1: pin determinism we CAN without
    # touching from_pretrained). Both sides set the same seed before the same query.
    torch.manual_seed(spec["seed"])
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(spec["seed"])

    observation = analyze_screen_image_local(
        image, spec["mode"], spec["question"], config=config
    )
    text = str(((observation.get("visual_summary") or {}).get("text")) or "")
    installed = type(moondream_runtime.get_active_moondream_provider()).__name__
    result = {
        "side": spec["side"],
        "provider": spec["provider"],
        "installed_provider": None if installed == "NoneType" else installed,
        "text": text,
        "errors": observation.get("errors") or [],
        "schema": observation.get("schema"),
        "schema_version": observation.get("schema_version"),
        "type": observation.get("type"),
        "visual_summary_meta": {
            k: (observation.get("visual_summary") or {}).get(k)
            for k in ("engine", "model", "revision")
        },
        "observation_keys": sorted(observation.keys()),
    }
    Path(spec["result_json"]).write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[{spec['side']}] provider={spec['provider']} installed={result['installed_provider']} "
          f"text_len={len(text)} errors={len(result['errors'])}", flush=True)
    return 0


# ---- compare (no GPU) --------------------------------------------------------

def _normalize(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def run_compare(scratch: Path) -> int:
    from spica.local_runtime.parity.comparators import text_diff

    legacy_p = scratch / "result_legacy.json"
    hf_p = scratch / "result_hf.json"
    for p in (legacy_p, hf_p):
        if not p.exists():
            print(f"compare: missing worker output: {p}", file=sys.stderr)
            return 1

    legacy = json.loads(legacy_p.read_text(encoding="utf-8"))
    hf = json.loads(hf_p.read_text(encoding="utf-8"))

    t_old, t_new = legacy.get("text", ""), hf.get("text", "")
    both_nonempty = bool(t_old.strip()) and bool(t_new.strip())
    bit_identical = t_old == t_new
    norm_identical = _normalize(t_old) == _normalize(t_new)
    _, err = text_diff(_normalize(t_old), _normalize(t_new))
    similarity = 1.0 - err

    # structural equivalence: same observation schema / type / engine tags / keys,
    # and no errors on either side (a clean run). The analyzer is unchanged, so this
    # is expected to be trivially equal -- a regression here means the seam leaked.
    structural_keys = ("schema", "schema_version", "type", "visual_summary_meta", "observation_keys")
    structural_equal = all(legacy.get(k) == hf.get(k) for k in structural_keys)
    no_errors = not legacy.get("errors") and not hf.get("errors")
    # the install hook actually routed: legacy installed nothing, hf installed the provider
    seam_routed = (legacy.get("installed_provider") is None
                   and hf.get("installed_provider") == "MoondreamHfProvider")

    if bit_identical:
        text_verdict = "BIT_IDENTICAL"
    elif norm_identical:
        text_verdict = "NORMALIZED_IDENTICAL"
    elif both_nonempty and similarity >= SIM_GATE:
        text_verdict = "SIMILAR"
    else:
        text_verdict = "DIVERGENT"

    passed = (
        both_nonempty
        and text_verdict in ("BIT_IDENTICAL", "NORMALIZED_IDENTICAL", "SIMILAR")
        and structural_equal
        and no_errors
        and seam_routed
    )
    verdict = "PASS" if passed else "FAIL"

    print(f"\n{'metric':22s} value")
    print(f"{'legacy text_len':22s} {len(t_old)}")
    print(f"{'hf text_len':22s} {len(t_new)}")
    print(f"{'both_nonempty':22s} {both_nonempty}")
    print(f"{'bit_identical':22s} {bit_identical}")
    print(f"{'normalized_identical':22s} {norm_identical}")
    print(f"{'similarity':22s} {similarity:.4f}   (gate >= {SIM_GATE})")
    print(f"{'text_verdict':22s} {text_verdict}")
    print(f"{'structural_equal':22s} {structural_equal}")
    print(f"{'no_errors':22s} {no_errors}")
    print(f"{'seam_routed':22s} {seam_routed}")
    print(f"{'VERDICT':22s} {verdict}")
    if not bit_identical:
        print("\n--- legacy text ---\n" + t_old[:1200])
        print("\n--- hf text ---\n" + t_new[:1200])

    report = {
        "verdict": verdict,
        "text_verdict": text_verdict,
        "sim_gate": SIM_GATE,
        "similarity": similarity,
        "bit_identical": bit_identical,
        "normalized_identical": norm_identical,
        "both_nonempty": both_nonempty,
        "structural_equal": structural_equal,
        "no_errors": no_errors,
        "seam_routed": seam_routed,
        "legacy": legacy,
        "hf": hf,
    }
    (scratch / "parity_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"report: {scratch / 'parity_report.json'}")
    return 0 if passed else 1


# ---- CLI ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Moondream cut-4 legacy-vs-hf screen parity.")
    ap.add_argument("mode", choices=["prepare", "worker", "compare", "import-check"])
    ap.add_argument("--spec", default=None, help="spec JSON (worker mode)")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--scratch", default=DEFAULT_SCRATCH)
    args = ap.parse_args(argv)

    if args.mode == "import-check":
        return run_import_check()

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
    make_test_image(scratch / "moondream_parity_in.png")
    legacy, hf = build_specs(scratch, args.seed)
    (scratch / "spec_legacy.json").write_text(json.dumps(legacy, indent=2, ensure_ascii=False), encoding="utf-8")
    (scratch / "spec_hf.json").write_text(json.dumps(hf, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[legacy] provider={legacy['provider']} install_hf={legacy['install_hf']}")
    print(f"[hf]     provider={hf['provider']} install_hf={hf['install_hf']}")
    print(f"specs written under: {scratch}")

    problems = _preflight(legacy, hf)
    if problems:
        print("PREFLIGHT PROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1

    ok, detail = _import_check_subprocess()
    print(f"import-check: {'PASS' if ok else 'FAIL'}" + (f"  ({detail})" if detail else ""))
    if not ok:
        print("import preflight FAILED -- not running GPU parity.", file=sys.stderr)
        return 1
    print("preflight: OK (image + specs written, moondream_hf import PASS)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
