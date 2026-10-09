"""Prepare pinned Home ONNX weights and local TensorRT engines, without a camera.

YOLO26-L weights are person-only, trained on CrowdHuman by simoswish.
https://huggingface.co/simoswish/PersonDetector_YOLO26_PRW
The author's model card is CC BY 4.0; also retain Ultralytics' AGPL-3.0 terms.
OSNet uses the author's MSMT17 combine-all checkpoint (MIT).
YuNet/SFace are optional; prepare them with --with-faces.
"""
import argparse
import ast
import hashlib
from pathlib import Path
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ZOO = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
PERSON = "https://huggingface.co/simoswish/PersonDetector_YOLO26_PRW/resolve/6f9d8b873308e08f345d563284b6190ea5c428a8/weights/large_ch_pretrain.pt"
PERSON_SHA = "77eb0e0ce7259a26b99f247c2b397789e8a4219d5e5ffd61bdc9321366e1e230"
REID = "https://drive.usercontent.google.com/download?id=1IosIFlLiulGIjwW3H8uMRmx3MzPwf86x&export=download&confirm=t"
REID_SHA = "48df972f72887b95cf3b43b3a07c3a7d2398381aea0f9cae64a7ef11d512b727"
REID_SOURCE = "https://raw.githubusercontent.com/KaiyangZhou/deep-person-reid/f8cd150fdf77e8d9e1ed143b7f308c2c609ded50/"
REID_CODE_SHA = "c7c1c29187d6330f859c91da229271531920464c7011aec13842a086b2263cae"
REID_RECIPE = "osnet-x1-msmt17-256x128-rgb-imagenet-opset17-fp32-v1"
EXPORT_VERSION = "8.4.152"
EXPORT_RECIPE = "ultralytics-8.4.152-opset17-640-end2end-max100-fp32-v1"
MODELS = {
    "face_detection_yunet_2023mar.onnx": (ZOO+"face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    "face_recognition_sface_2021dec.onnx": (ZOO+"face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
    "yolo26l_person_crowdhuman.pt": (PERSON, PERSON_SHA),
    "osnet_x1_0_msmt17.pth": (REID, REID_SHA),
    "osnet_export.py": (REID_SOURCE+"torchreid/models/osnet.py", REID_CODE_SHA),
    "osnet.LICENSE": (REID_SOURCE+"LICENSE", "3ac8ce2a83d170cb1c7c84152e0c1faca1f187794303514383960d2441716247"),
}


def export_person(checkpoint_path):
    """Use a separate export environment; never install/upgrade voice packages."""
    import onnx
    target = checkpoint_path.with_suffix(".onnx")
    if target.exists():
        model = onnx.load(target)
        metadata = {item.key: item.value for item in model.metadata_props}
        if (metadata.get("spica_source_sha256") != PERSON_SHA
                or metadata.get("spica_export_recipe") != EXPORT_RECIPE):
            raise ValueError("existing Home person ONNX has a different source or export recipe")
        return target
    try:
        import torch
        import ultralytics
        from ultralytics.engine.exporter import Exporter
        from ultralytics.nn.modules import block, conv, head
        from ultralytics.nn.tasks import DetectionModel
    except ImportError as exc:
        raise RuntimeError("YOLO26 preparation needs an isolated export environment with "
                           "ultralytics==8.4.152; runtime only loads TensorRT engines") from exc
    if ultralytics.__version__ != EXPORT_VERSION:
        raise RuntimeError(f"Home ONNX preparation requires ultralytics=={EXPORT_VERSION}")
    # Restricted deserialization of this checksum-pinned public checkpoint.
    allowed = [set, DetectionModel, head.Detect]
    allowed += [getattr(torch.nn, name) for name in ("SiLU", "BatchNorm2d", "ModuleList",
                "Sequential", "Conv2d", "Identity", "MaxPool2d", "Upsample")]
    allowed += [getattr(block, name) for name in ("Attention", "Bottleneck", "C2PSA", "C3k",
                                                "C3k2", "PSABlock", "SPPF")]
    allowed += [getattr(conv, name) for name in ("Concat", "Conv", "DWConv")]
    with torch.serialization.safe_globals(allowed):
        checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    network = checkpoint.get("ema") or checkpoint["model"]
    if network.names != {0: "person"} or network.model[-1].nc != 1 or network.yaml.get("scale") != "l":
        raise ValueError("Home requires the single-class YOLO26-L person checkpoint")
    network.task = "detect"
    # Export into a temporary directory so an interrupted export is never cached.
    with tempfile.TemporaryDirectory(dir=checkpoint_path.parent) as temporary:
        network.pt_path = str(Path(temporary) / checkpoint_path.name)
        exported = Path(Exporter(overrides={"format": "onnx", "imgsz": 640, "opset": 17,
            "simplify": False, "dynamic": False, "nms": False, "batch": 1, "max_det": 100,
            "device": "0"})(model=network.float()))
        model = onnx.load(exported)
        metadata = {item.key: item.value for item in model.metadata_props}
        metadata.update(spica_source_sha256=PERSON_SHA, spica_export_recipe=EXPORT_RECIPE)
        onnx.helper.set_model_props(model, metadata)
        onnx.checker.check_model(model)
        onnx.save(model, exported)
        exported.replace(target)
    return target


def export_reid(directory):
    """Author implementation is used only at explicit preparation time."""
    import importlib.util
    import onnx
    target = directory / "osnet_x1_0_msmt17.onnx"
    if target.exists():
        model = onnx.load(target)
        metadata = {item.key: item.value for item in model.metadata_props}
        if metadata.get("spica_source_sha256") != REID_SHA or metadata.get("spica_export_recipe") != REID_RECIPE:
            raise ValueError("existing OSNet ONNX has a different source or export recipe")
        return target
    import torch
    source = directory / "osnet_export.py"
    checkpoint = directory / "osnet_x1_0_msmt17.pth"
    if (hashlib.sha256(source.read_bytes()).hexdigest() != REID_CODE_SHA
            or hashlib.sha256(checkpoint.read_bytes()).hexdigest() != REID_SHA):
        raise ValueError("OSNet export source/checkpoint differs")
    spec = importlib.util.spec_from_file_location("home_osnet_export", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = torch.load(checkpoint, map_location="cpu", weights_only=True)
    network = module.osnet_x1_0(num_classes=4101, pretrained=False)
    network.load_state_dict(state, strict=True)
    network = network.eval().cuda()
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        exported = Path(temporary) / target.name
        with torch.inference_mode():
            torch.onnx.export(network, torch.zeros(1, 3, 256, 128, device="cuda"), exported,
                              input_names=["images"], output_names=["features"], opset_version=17)
        model = onnx.load(exported)
        onnx.helper.set_model_props(model, {"spica_source_sha256": REID_SHA, "spica_export_recipe": REID_RECIPE})
        onnx.checker.check_model(model)
        onnx.save(model, exported)
        exported.replace(target)
    return target


def main():
    from spica.config.secrets import load_secrets
    load_secrets()
    from spica.config.manager import ConfigManager
    from spica.host.assemblies.home import resolved_home_config
    from spica.adapters.home_vision import HomeVision, model_specs
    from spica.local_runtime.tensorrt import build_engine
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="typed application YAML (default: data/config/app.yaml)")
    parser.add_argument("--with-faces", action="store_true", help="also prepare optional YuNet/SFace enrollment")
    args = parser.parse_args()
    config = resolved_home_config(ConfigManager(args.config).load().home)
    directory = Path(config.model_directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, (url, digest) in MODELS.items():
        if name.startswith("face_") and not args.with_faces:
            continue
        target = directory / name
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError(f"existing model differs: {target}")
            continue
        temporary = None
        try:
            with urllib.request.urlopen(url, timeout=30) as response, tempfile.NamedTemporaryFile(
                    dir=directory, delete=False) as output:
                temporary = Path(output.name)
                while chunk := response.read(1024*1024):
                    output.write(chunk)
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
                raise ValueError(f"model checksum mismatch: {target.name}")
            temporary.replace(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print(target.name, "verified", flush=True)
    import onnx
    person = onnx.load(export_person(directory / "yolo26l_person_crowdhuman.pt"))
    metadata = {item.key: item.value for item in person.metadata_props}
    if ast.literal_eval(metadata.get("names", "{}")) != {0: "person"}:
        raise ValueError("Home requires a person-only YOLO26 checkpoint")
    export_reid(directory)
    for name, shape, precision in model_specs(config, include_faces=args.with_faces):
        print("Preparing", name, shape, precision, flush=True)
        print(build_engine(directory / name, shape, precision=precision), flush=True)
    with HomeVision(config, enable_faces=args.with_faces) as vision:
        if vision.face_error:
            raise RuntimeError(vision.face_error)
        print("Home TensorRT person/ReID engines loaded; optional faces:", args.with_faces, flush=True)


if __name__ == "__main__":
    main()
