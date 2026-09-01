from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True, slots=True)
class ASRResult:
    text: str
    confidence: float
    model: str
    device: str


class ASREngine(Protocol):
    def transcribe(self, audio: np.ndarray, sample_rate: int) -> ASRResult:
        ...


class ScriptedASREngine:
    def __init__(self, transcripts: list[str]):
        self.transcripts = list(transcripts)

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> ASRResult:
        del audio, sample_rate
        text = self.transcripts.pop(0) if self.transcripts else ""
        return ASRResult(text=text, confidence=1.0, model="scripted", device="cpu")

