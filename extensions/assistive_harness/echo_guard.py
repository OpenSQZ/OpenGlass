from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher

from .router import normalize_text
from .schemas import ControlIntent


@dataclass(frozen=True, slots=True)
class EchoDecision:
    allow: bool
    reason: str
    similarity: float = 0.0


class EchoGuard:
    def __init__(self, window_ms: int = 20_000, similarity_threshold: float = 0.86):
        self.window_ms = int(window_ms)
        self.similarity_threshold = float(similarity_threshold)
        self._model_text: deque[tuple[float, str]] = deque()
        self.ai_speaking = False

    def note_model_text(self, text: str, at_ms: float | None = None) -> None:
        normalized = normalize_text(text)
        if not normalized:
            return
        now = at_ms if at_ms is not None else time.time() * 1000
        self._model_text.append((now, normalized))
        self._prune(now)

    def set_ai_speaking(self, active: bool) -> None:
        self.ai_speaking = bool(active)

    def _prune(self, now_ms: float) -> None:
        cutoff = now_ms - self.window_ms
        while self._model_text and self._model_text[0][0] < cutoff:
            self._model_text.popleft()

    def evaluate(
        self,
        utterance: str,
        intent: ControlIntent,
        at_ms: float | None = None,
    ) -> EchoDecision:
        now = at_ms if at_ms is not None else time.time() * 1000
        self._prune(now)
        normalized = normalize_text(utterance)
        if len(normalized) < 2:
            return EchoDecision(False, "too_short")

        best = 0.0
        for _, model_text in self._model_text:
            if normalized in model_text or model_text in normalized:
                best = 1.0
                break
            best = max(best, SequenceMatcher(None, normalized, model_text).ratio())
        if best >= self.similarity_threshold:
            return EchoDecision(False, "matches_recent_model_echo", best)

        if self.ai_speaking and intent not in {
            ControlIntent.STOP_SPEECH,
            ControlIntent.RESUME_SPEECH,
            ControlIntent.RESET_SESSION,
            ControlIntent.CANCEL_SKILL,
            ControlIntent.RETURN_TO_CHAT,
            # A positively routed Skill command is also a control-plane
            # interruption. Recent-model-text similarity is checked above,
            # so model echo is still rejected before this exception applies.
            ControlIntent.ACTIVATE_SKILL,
        }:
            return EchoDecision(False, "ordinary_skill_suppressed_while_ai_speaking", best)
        return EchoDecision(True, "allowed", best)
