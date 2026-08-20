from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from .base import CVObservation


COCO_NAMES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep",
    "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop",
    "mouse", "remote", "keyboard", "cell phone", "microwave", "oven",
    "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
    "teddy bear", "hair drier", "toothbrush",
)

DEFAULT_TARGET_ALIASES = {
    "手机": "cell phone", "电话": "cell phone", "智能手机": "cell phone",
    "phone": "cell phone", "cellphone": "cell phone", "cell phone": "cell phone",
    "书": "book", "书本": "book", "图书": "book", "book": "book",
    "杯子": "cup", "水杯": "cup", "茶杯": "cup", "cup": "cup",
    "瓶子": "bottle", "水瓶": "bottle", "bottle": "bottle",
    "椅子": "chair", "chair": "chair", "人": "person", "行人": "person",
    "person": "person", "电脑": "laptop", "笔记本电脑": "laptop",
    "laptop": "laptop", "遥控器": "remote", "remote": "remote",
    "键盘": "keyboard", "keyboard": "keyboard", "鼠标": "mouse",
    "mouse": "mouse", "背包": "backpack", "书包": "backpack",
    "backpack": "backpack",
}


class YoloOnnxProvider:
    """CPU-first YOLO reference plugin; it only produces shadow observations."""

    def __init__(
        self,
        model_path: str | Path,
        *,
        device: str = "cpu",
        confidence: float = 0.25,
        image_size: int = 640,
        target_aliases: dict[str, str] | None = None,
    ) -> None:
        if device.lower() != "cpu":
            raise ValueError("V1 YOLO reference accepts only device=cpu")
        self.model_path = Path(model_path).resolve()
        self.confidence = float(confidence)
        self.image_size = int(image_size)
        self.target_aliases = {
            **DEFAULT_TARGET_ALIASES,
            **{str(k).lower(): str(v) for k, v in (target_aliases or {}).items()},
        }
        self._session: Any = None
        self._input_name: str | None = None
        self._cv2: Any = None
        self._lock = threading.Lock()

    def analyze(
        self,
        frame: bytes | None,
        frame_id: str,
        timestamp_ms: float,
        skill_id: str,
        slots: dict[str, Any],
    ) -> CVObservation:
        if not frame:
            raise ValueError("YOLO requires a non-empty JPEG frame")
        self._load()
        target = str(slots.get("target") or "").strip()
        target_label = self._resolve_target(target) if target else None
        if target and target_label is None:
            return CVObservation(
                frame_id, timestamp_ms, skill_id, "yolo_onnx",
                {"status": "skipped", "reason": "unsupported_target", "target": target},
            )

        image = self._cv2.imdecode(
            np.frombuffer(frame, dtype=np.uint8), self._cv2.IMREAD_COLOR
        )
        if image is None:
            raise ValueError("YOLO could not decode the JPEG frame")
        height, width = image.shape[:2]
        started = time.perf_counter()
        tensor, scale, pad_x, pad_y = self._prepare_input(image)
        with self._lock:
            output = self._session.run(None, {self._input_name: tensor})[0]
        if output.ndim != 3 or output.shape[0] != 1 or output.shape[2] != 6:
            raise ValueError(f"Unexpected YOLO output shape: {output.shape}")

        target_id = COCO_NAMES.index(target_label) if target_label else None
        detections: list[dict[str, Any]] = []
        for x1, y1, x2, y2, confidence, raw_class in output[0]:
            class_id = round(float(raw_class))
            if float(confidence) < self.confidence or not 0 <= class_id < len(COCO_NAMES):
                continue
            if target_id is not None and class_id != target_id:
                continue
            left = max(0.0, min(width, (float(x1) - pad_x) / scale))
            top = max(0.0, min(height, (float(y1) - pad_y) / scale))
            right = max(0.0, min(width, (float(x2) - pad_x) / scale))
            bottom = max(0.0, min(height, (float(y2) - pad_y) / scale))
            if right <= left or bottom <= top:
                continue
            center_x = ((left + right) / 2.0) / width
            center_y = ((top + bottom) / 2.0) / height
            detections.append(
                {
                    "label": COCO_NAMES[class_id],
                    "confidence": round(float(confidence), 4),
                    "bbox_xyxy": [round(left), round(top), round(right), round(bottom)],
                    "center_normalized": [round(center_x, 4), round(center_y, 4)],
                    "position": self._position(center_x, center_y),
                }
            )
        detections.sort(key=lambda item: float(item["confidence"]), reverse=True)
        return CVObservation(
            frame_id,
            timestamp_ms,
            skill_id,
            "yolo_onnx",
            {
                "status": "ok",
                "found": bool(detections),
                "target": target or None,
                "canonical_label": target_label,
                "best_detection": detections[0] if detections else None,
                "detections": detections,
                "image": {"width": width, "height": height},
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
                "model": self.model_path.name,
                "device": "cpu",
            },
        )

    def _load(self) -> None:
        if self._session is not None:
            return
        if not self.model_path.is_file():
            raise FileNotFoundError(f"YOLO weights not found: {self.model_path}")
        import cv2
        import onnxruntime as ort

        session = ort.InferenceSession(
            str(self.model_path), providers=["CPUExecutionProvider"]
        )
        model_input = session.get_inputs()[0]
        self._cv2 = cv2
        self._session = session
        self._input_name = model_input.name

    def _prepare_input(
        self, image: np.ndarray
    ) -> tuple[np.ndarray, float, float, float]:
        height, width = image.shape[:2]
        scale = min(self.image_size / width, self.image_size / height)
        resized_width, resized_height = round(width * scale), round(height * scale)
        resized = self._cv2.resize(image, (resized_width, resized_height))
        pad_x = (self.image_size - resized_width) / 2.0
        pad_y = (self.image_size - resized_height) / 2.0
        left, top = round(pad_x - 0.1), round(pad_y - 0.1)
        right = self.image_size - resized_width - left
        bottom = self.image_size - resized_height - top
        padded = self._cv2.copyMakeBorder(
            resized, top, bottom, left, right,
            self._cv2.BORDER_CONSTANT, value=(114, 114, 114),
        )
        rgb = self._cv2.cvtColor(padded, self._cv2.COLOR_BGR2RGB)
        tensor = np.ascontiguousarray(rgb.transpose(2, 0, 1), dtype=np.float32)
        return np.expand_dims(tensor / 255.0, axis=0), scale, float(left), float(top)

    def _resolve_target(self, target: str) -> str | None:
        normalized = target.lower().replace(" ", "")
        for alias in sorted(self.target_aliases, key=len, reverse=True):
            if alias.lower().replace(" ", "") in normalized:
                label = self.target_aliases[alias]
                return label if label in COCO_NAMES else None
        return None

    @staticmethod
    def _position(center_x: float, center_y: float) -> str:
        horizontal = "左" if center_x < 1 / 3 else "右" if center_x > 2 / 3 else "中"
        vertical = "上" if center_y < 1 / 3 else "下" if center_y > 2 / 3 else "中"
        return {
            ("左", "上"): "左上方", ("中", "上"): "正上方",
            ("右", "上"): "右上方", ("左", "中"): "左侧",
            ("中", "中"): "中央", ("右", "中"): "右侧",
            ("左", "下"): "左下方", ("中", "下"): "正下方",
            ("右", "下"): "右下方",
        }[(horizontal, vertical)]
