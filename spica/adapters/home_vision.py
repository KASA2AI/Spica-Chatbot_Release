"""TensorRT room vision: person-only YOLO26, OSNet and optional local face hints.

Person model: https://huggingface.co/simoswish/PersonDetector_YOLO26_PRW
Face models: https://github.com/opencv/opencv_zoo/tree/main/models
No image is sent to a model service or retained by this adapter.
"""
from pathlib import Path
import time

from spica.home.models import MIN_TRACKING_CONFIDENCE, Person, VisionObservation

MODEL_FILES = (
    "face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx",
    "yolo26l_person_crowdhuman.onnx",
    "osnet_x1_0_msmt17.onnx",
)


def model_specs(config, *, include_faces=False):
    result = ((MODEL_FILES[2], (1, 3, 640, 640), "fp32"),
              (MODEL_FILES[3], (1, 3, 256, 128), "fp32"))
    if include_faces:
        result += ((MODEL_FILES[0], (1, 3, (config.camera_height+31)//32*32,
                                    (config.camera_width+31)//32*32), "fp32"),
                   (MODEL_FILES[1], (1, 3, 112, 112), "fp32"))
    return result


class HomeVision:
    def __init__(self, config, profile=None, *, enable_faces=None):
        import cv2
        import numpy as np
        self.cv, self.np = cv2, np
        cv2.setNumThreads(2)
        self.config, self.profile = config, profile
        directory = Path(config.model_directory)
        from spica.local_runtime.tensorrt import TensorRTEngine
        self._engines = []
        self.faces = self.recognizer = None
        self.face_error = ''
        try:
            for name, shape, precision in model_specs(config):
                self._engines.append(TensorRTEngine(directory / name, shape, precision=precision))
        except BaseException:
            self.close()
            raise
        self.people, self.reid = self._engines
        self.enrolled = np.asarray(profile.embeddings, dtype=np.float32) if profile and profile.embeddings else None
        want_faces = self.enrolled is not None if enable_faces is None else enable_faces
        if want_faces:
            optional = []
            try:
                for name, shape, precision in model_specs(config, include_faces=True)[2:]:
                    optional.append(TensorRTEngine(directory / name, shape, precision=precision))
                self.faces, self.recognizer = optional
            except Exception as exc:
                # A missing/failed optional face model cannot disable occupancy.
                self.face_error = f'{type(exc).__name__}: {exc}'
                for engine in reversed(optional):
                    try:
                        engine.close()
                    except Exception as cleanup:
                        self.face_error += f'; cleanup: {cleanup}'
            finally:
                self._engines.extend(optional)
        self.pad_height = (config.camera_height+31)//32*32
        self.pad_width = (config.camera_width+31)//32*32
        self.face_grids = {}
        for stride in (8, 16, 32):
            yy, xx = np.mgrid[:self.pad_height//stride, :self.pad_width//stride]
            self.face_grids[stride] = np.stack((xx, yy), -1).reshape(-1, 2)

    def _appearance(self, frame, box):
        cv, np = self.cv, self.np
        height, width = frame.shape[:2]
        x, y, w, h = box
        crop = frame[max(0, int(y*height)):min(height, int((y+h)*height)),
                     max(0, int(x*width)):min(width, int((x+w)*width))]
        if crop.size == 0:
            return b''
        rgb = cv.cvtColor(cv.resize(crop, (128, 256)), cv.COLOR_BGR2RGB).astype(np.float32)/255
        rgb = (rgb-np.asarray([.485, .456, .406], np.float32))/np.asarray([.229, .224, .225], np.float32)
        feature = next(iter(self.reid.infer(np.ascontiguousarray(rgb.transpose(2, 0, 1)[None])).values())).reshape(-1)
        norm = float(np.linalg.norm(feature))
        if feature.size != 512 or norm <= 0 or not np.isfinite(feature).all():
            return b''
        return np.asarray(feature/norm, np.float32).tobytes()

    def _people(self, frame):
        cv, np = self.cv, self.np
        height, width = frame.shape[:2]
        scale = min(640/width, 640/height)
        resized = cv.resize(frame, (round(width*scale), round(height*scale)))
        dx, dy = (640-resized.shape[1])/2, (640-resized.shape[0])/2
        left, top = round(dx-.1), round(dy-.1)
        padded = cv.copyMakeBorder(resized, top, round(dy+.1), left, round(dx+.1),
                                   cv.BORDER_CONSTANT, value=(114, 114, 114))
        outputs = self.people.infer(cv.dnn.blobFromImage(padded, 1/255., swapRB=True))
        predictions = outputs["output0"][0]
        if predictions.shape != (100, 6):
            raise ValueError("Home person-only YOLO26 output differs")
        rows = predictions[predictions[:, 4] >= min(self.config.person_confidence, MIN_TRACKING_CONFIDENCE)]
        if np.any(rows[:, 5] != 0):
            raise ValueError("Home YOLO26 must contain only the person class")
        # This weight exports its one-to-one head: xyxy, confidence, class.
        # A second NMS pass could hide another person and weaken the owner gate.
        result = []
        for x0, y0, x1, y1, confidence, _ in rows:
            x0, x1 = max(0, (x0-left)/scale), min(width, (x1-left)/scale)
            y0, y1 = max(0, (y0-top)/scale), min(height, (y1-top)/scale)
            if x1 > x0 and y1 > y0:
                result.append(Person(tuple(float(v) for v in
                    (x0/width, y0/height, (x1-x0)/width, (y1-y0)/height)), confidence=float(confidence)))
        return result

    def _faces(self, frame):
        cv, np = self.cv, self.np
        h, w = frame.shape[:2]
        padded = cv.copyMakeBorder(frame, 0, self.pad_height-h, 0, self.pad_width-w,
                                   cv.BORDER_CONSTANT, value=0)
        outputs = self.faces.infer(cv.dnn.blobFromImage(padded))
        found = []
        # Decode the same YuNet outputs and integer-box NMS as FaceDetectorYN.
        # Reference: OpenCV 4.10 modules/objdetect/src/face_detect.cpp.
        for stride, grid in self.face_grids.items():
            cls = outputs[f"cls_{stride}"].reshape(-1).clip(0, 1)
            obj = outputs[f"obj_{stride}"].reshape(-1).clip(0, 1)
            scores = np.sqrt(cls*obj)
            selected = np.flatnonzero(scores >= .85)
            boxes = outputs[f"bbox_{stride}"].reshape(-1, 4)[selected]
            points = outputs[f"kps_{stride}"].reshape(-1, 5, 2)[selected]
            centers = (boxes[:, :2]+grid[selected])*stride
            sizes = np.exp(boxes[:, 2:])*stride
            landmarks = (points+grid[selected, None, :])*stride
            found.append(np.concatenate((centers-sizes/2, sizes,
                          landmarks.reshape(-1, 10), scores[selected, None]), axis=1))
        faces = np.concatenate(found).astype(np.float32)
        if not np.isfinite(faces).all():
            raise ValueError("Home YuNet produced invalid landmarks")
        if len(faces) > 1:
            keep = cv.dnn.NMSBoxes(faces[:, :4].astype(np.int32).tolist(), faces[:, 14].tolist(),
                                   .85, .3, top_k=5000)
            faces = faces[np.asarray(keep, dtype=np.intp).reshape(-1)]
        return faces

    def _align_crop(self, frame, face):
        # Five-point similarity alignment, as used by OpenCV FaceRecognizerSF.
        # Only image geometry runs here; SFace neural inference is TensorRT.
        np = self.np
        source = face[4:14].reshape(5, 2).astype(np.float64)
        target = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                           [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float64)
        src_mean, dst_mean = source.mean(axis=0), target.mean(axis=0)
        src, dst = source-src_mean, target-dst_mean
        covariance = dst.T @ src / 5
        u, singular, vt = np.linalg.svd(covariance)
        variance = (src*src).sum()/5
        if variance <= 0 or np.linalg.matrix_rank(covariance) < 2:
            raise ValueError("Home face landmarks cannot be aligned")
        signs = np.ones(2)
        if np.linalg.det(covariance) < 0:
            signs[-1] = -1
        rotation = (u*signs) @ vt
        scale = (singular @ signs)/variance
        transform = np.column_stack((scale*rotation, dst_mean-scale*rotation @ src_mean))
        return self.cv.warpAffine(frame, transform, (112, 112), flags=self.cv.INTER_LINEAR)

    def infer(self, frame, sequence, captured_at, captured_mono, *, registration=False):
        cv, np = self.cv, self.np
        started = time.monotonic()
        height, width = frame.shape[:2]
        error = ""
        if (width, height) != (self.config.camera_width, self.config.camera_height) or (
                self.profile and (width, height) != self.profile.image_size):
            error = "camera_geometry_changed"
        elif float(cv.cvtColor(frame, cv.COLOR_BGR2GRAY).mean()) < 12:
            error = "too_dark"
        if error:
            return VisionObservation(self.config.camera_device, sequence, captured_at, captured_mono,
                                     error=error), None
        detections = self._people(frame)
        # Body observations survive optional face failure/association ambiguity.
        people = [Person(person.box, appearance=self._appearance(frame, person.box), confidence=person.confidence)
                  for person in detections if not registration or person.confidence >= self.config.person_confidence]
        enrollment = None
        if self.faces is not None and not self.face_error:
            try:
                people, enrollment = self._face_hints(frame, people, registration)
            except Exception as exc:
                self.face_error = f'{type(exc).__name__}: {exc}'
        return VisionObservation(self.config.camera_device, sequence, captured_at, captured_mono,
                                 tuple(people), inference_seconds=time.monotonic()-started,
                                 face_error=self.face_error), enrollment

    def _face_hints(self, frame, people, registration):
        from dataclasses import replace
        cv, np = self.cv, self.np
        height, width = frame.shape[:2]
        people = list(people)
        faces = self._faces(frame)
        candidates = [[] for _ in people]
        if faces is not None:
            for face in faces:
                x, y, w, h = face[:4]
                px, py = (x+w/2)/width, (y+h/2)/height
                associated = [i for i, person in enumerate(people)
                              if person.box[0] <= px <= person.box[0]+person.box[2]
                              and person.box[1] <= py <= person.box[1]+person.box[3]]
                strong = [i for i in associated if people[i].confidence >= self.config.person_confidence]
                if strong:
                    for index in associated:
                        if index not in strong:
                            # This face belongs to a strong candidate. A weak
                            # duplicate cannot bypass its identity evidence.
                            candidates[index].extend((None, None))
                associated = strong or associated
                if len(associated) == 1:
                    candidates[associated[0]].append(face)
                else:
                    for index in associated:
                        candidates[index].extend((None, None))
        enrollment = None
        for index, matched in enumerate(candidates):
            if len(matched) != 1:
                if len(matched) > 1:
                    people[index] = replace(people[index], association_error="face_person_association_ambiguous")
                continue
            face = matched[0]
            x, y, w, h = face[:4]
            if min(w, h) < self.config.face_min_pixels or x < 0 or y < 0 or x+w > width or y+h > height:
                continue
            aligned = self._align_crop(frame, face)
            feature = next(iter(self.recognizer.infer(cv.dnn.blobFromImage(aligned, swapRB=True)).values())).reshape(-1)
            norm = float(np.linalg.norm(feature))
            if norm <= 0 or not np.isfinite(feature).all():
                continue
            feature = feature/norm
            if self.enrolled is not None:
                similarity = float(np.sort(self.enrolled @ feature)[-2])
                people[index] = replace(people[index], identity='owner' if similarity >= self.config.face_similarity else 'other',
                                        similarity=similarity)
            if registration and len(people) == 1 and len(faces) == 1:
                enrollment = feature.tolist()
        return people, enrollment

    def close(self):
        errors = []
        for engine in reversed(self._engines):
            try:
                engine.close()
            except Exception as exc:
                errors.append(exc)
        self._engines.clear()
        if errors:
            raise RuntimeError("Home vision cleanup failed") from errors[0]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
