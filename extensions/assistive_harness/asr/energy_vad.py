from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class UtteranceAudio:
    audio: np.ndarray
    started_at_ms: float
    ended_at_ms: float


class EnergyVAD:
    """Small streaming endpoint detector; it does not perform recognition."""

    def __init__(
        self,
        sample_rate: int = 16_000,
        rms_threshold: float = 0.012,
        min_speech_ms: int = 180,
        end_silence_ms: int = 450,
        max_utterance_ms: int = 8_000,
        preroll_ms: int = 200,
    ):
        self.sample_rate = sample_rate
        self.rms_threshold = rms_threshold
        self.min_speech_ms = min_speech_ms
        self.end_silence_ms = end_silence_ms
        self.max_utterance_ms = max_utterance_ms
        self.preroll_ms = preroll_ms
        self._preroll: deque[tuple[np.ndarray, float]] = deque()
        self._active: list[np.ndarray] = []
        self._started_at_ms: float | None = None
        self._last_voice_ms: float | None = None

    def _duration_ms(self, audio: np.ndarray) -> float:
        return float(audio.size) * 1000.0 / self.sample_rate

    def feed(self, audio: np.ndarray, frame_started_at_ms: float) -> UtteranceAudio | None:
        frame = np.asarray(audio, dtype=np.float32).reshape(-1).copy()
        if frame.size == 0:
            return None
        duration_ms = self._duration_ms(frame)
        frame_end_ms = frame_started_at_ms + duration_ms
        rms = float(np.sqrt(np.mean(np.square(frame, dtype=np.float64))))
        voiced = rms >= self.rms_threshold

        if self._started_at_ms is None:
            self._preroll.append((frame, frame_started_at_ms))
            while self._preroll and frame_end_ms - self._preroll[0][1] > self.preroll_ms:
                self._preroll.popleft()
            if not voiced:
                return None
            self._started_at_ms = self._preroll[0][1] if self._preroll else frame_started_at_ms
            self._active = [item[0] for item in self._preroll]
            self._preroll.clear()
            self._last_voice_ms = frame_end_ms
        else:
            self._active.append(frame)
            if voiced:
                self._last_voice_ms = frame_end_ms

        active_ms = frame_end_ms - float(self._started_at_ms)
        silence_ms = frame_end_ms - float(self._last_voice_ms or frame_end_ms)
        if active_ms >= self.max_utterance_ms or silence_ms >= self.end_silence_ms:
            return self._finish(frame_end_ms)
        return None

    def _finish(self, ended_at_ms: float) -> UtteranceAudio | None:
        if self._started_at_ms is None or not self._active:
            self.reset()
            return None
        audio = np.concatenate(self._active).astype(np.float32, copy=False)
        started = self._started_at_ms
        voiced_duration = max(0.0, float(self._last_voice_ms or ended_at_ms) - started)
        self.reset()
        if voiced_duration < self.min_speech_ms:
            return None
        return UtteranceAudio(audio=audio, started_at_ms=started, ended_at_ms=ended_at_ms)

    def flush(self, ended_at_ms: float) -> UtteranceAudio | None:
        return self._finish(ended_at_ms)

    def reset(self) -> None:
        self._preroll.clear()
        self._active = []
        self._started_at_ms = None
        self._last_voice_ms = None
