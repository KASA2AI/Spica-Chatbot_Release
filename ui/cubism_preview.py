"""Render portable PNG previews with the same native surface as the desktop.

Run: python -m ui.cubism_preview PACK_FOLDER --output OUTPUT_FOLDER
"""

import argparse
import json
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from spica.core.cubism import cubism_files, load_bindings
from ui.widgets.cubism_character import CubismCanvas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--costume")
    parser.add_argument("--check-actions", action="store_true")
    args = parser.parse_args()
    root = args.pack.resolve()
    meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    binding_path = meta["visuals"]["cubism"]
    bindings = load_bindings(root, binding_path)
    cubism_files(root, binding_path, set(bindings.models))
    models = [(key, spec) for key, spec in bindings.models.items() if not args.costume or key == args.costume]
    if not models:
        parser.error("unknown costume")
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    canvas = CubismCanvas()
    canvas.resize(720, 1024)
    remaining = iter(models)
    current = None
    actions = iter(())
    failures = []

    def fail(message):
        failures.append(message)
        print(message, flush=True)
        app.exit(1)

    def next_model():
        nonlocal current
        current = next(remaining, None)
        if current is None:
            app.quit()
            return
        canvas.setWindowTitle(f"Cubism preview — {current[0]}")
        canvas.set_model(root, current[1])

    def capture():
        nonlocal actions
        if canvas.model is None or current is None:
            fail("Cubism model did not load")
            return
        output = args.output / f"{current[0]}.png"
        if not canvas.grabFramebuffer().save(str(output)):
            fail(f"could not save {output}")
            return
        print(f"preview {current[0]} parameters={len(canvas._params)} motions={len(current[1].motions)}", flush=True)
        actions = iter(current[1].motions) if args.check_actions else iter(())
        next_action()

    def next_action():
        action = next(actions, None)
        if action is None:
            QTimer.singleShot(0, next_model)
        else:
            canvas.start_motion(action)
            QTimer.singleShot(70, next_action)

    canvas.ready.connect(lambda: QTimer.singleShot(800, capture))
    canvas.failed.connect(fail)
    next_model()
    canvas.show()
    QTimer.singleShot(180000, lambda: fail("Cubism preview timed out"))
    result = app.exec()
    canvas.release()
    return 1 if failures else result


if __name__ == "__main__":
    raise SystemExit(main())
