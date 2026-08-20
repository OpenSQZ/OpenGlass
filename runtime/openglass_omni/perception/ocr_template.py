from __future__ import annotations

from typing import Any

from .base import CVObservation


class OcrTemplateProvider:
    """Copy this provider and replace only the model-specific inference body."""

    def __init__(self, model_path: str = "") -> None:
        self.model_path = model_path

    def analyze(
        self,
        frame: bytes | None,
        frame_id: str,
        timestamp_ms: float,
        skill_id: str,
        slots: dict[str, Any],
    ) -> CVObservation:
        del slots
        if not frame:
            raise ValueError("OCR requires a non-empty JPEG frame")
        raise NotImplementedError(
            "copy ocr_template.py to ocr_<backend>.py and implement analyze()"
        )
