from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import numpy as np

from .base import ASRResult


class FunASREngine:
    """Lazy local FunASR adapter. It never downloads a model implicitly."""

    def __init__(self, model_path: str, device: str = "cpu", model_kwargs: dict[str, Any] | None = None):
        path = Path(model_path).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"FunASR model path does not exist: {path}")
        self.model_path = str(path)
        self.device = device
        self.model_kwargs = dict(model_kwargs or {})
        self._model: Any = None
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def ensure_loaded(self) -> None:
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            from funasr import AutoModel

            self._model = AutoModel(
                model=self.model_path,
                device=self.device,
                disable_update=True,
                **self.model_kwargs,
            )

    def warm_up(self) -> None:
        """Load the model and run one short silent inference before serving clients."""
        self.ensure_loaded()
        silence = np.zeros(8_000, dtype=np.float32)
        with self._infer_lock:
            self._model.generate(
                input=silence,
                cache={},
                is_final=True,
                batch_size_s=0,
            )

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> ASRResult:
        if sample_rate != 16_000:
            raise ValueError(f"FunASR Phase A requires 16 kHz audio, got {sample_rate}")
        self.ensure_loaded()
        waveform = np.asarray(audio, dtype=np.float32).reshape(-1)
        with self._infer_lock:
            results = self._model.generate(
                input=waveform,
                cache={},
                is_final=True,
                batch_size_s=0,
            )
        text = ""
        confidence = 1.0
        if isinstance(results, list) and results:
            first = results[0]
            if isinstance(first, dict):
                text = str(first.get("text") or "").strip()
                if isinstance(first.get("confidence"), (int, float)):
                    confidence = float(first["confidence"])
            else:
                text = str(first).strip()
        return ASRResult(
            text=text,
            confidence=confidence,
            model=self.model_path,
            device=self.device,
        )
