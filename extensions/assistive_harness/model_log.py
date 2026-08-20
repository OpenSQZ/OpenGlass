from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ModelTurnAccumulator:
    """Aggregate streamed Gateway fragments into auditable model turns."""

    turn_index: int = 0
    _key: tuple[Any, ...] | None = None
    _text: list[str] = field(default_factory=list)
    _audio_ms: float = 0.0
    _metadata: dict[str, Any] = field(default_factory=dict)

    def _flush(self) -> dict[str, Any] | None:
        if self._key is None:
            return None
        self.turn_index += 1
        record = {
            "type": "model.turn",
            "turn_index": self.turn_index,
            **self._metadata,
            "text": "".join(self._text),
            "audio_ms": round(self._audio_ms, 1),
        }
        self._key = None
        self._text.clear()
        self._audio_ms = 0.0
        self._metadata = {}
        return record

    def feed(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        state = str(message.get("state") or "unknown")
        key = (
            message.get("session_id"),
            int(message.get("generation") or 0),
            str(message.get("skill_id") or ""),
            state,
        )
        completed: list[dict[str, Any]] = []
        if self._key is not None and key != self._key:
            record = self._flush()
            if record is not None:
                completed.append(record)

        text = str(message.get("text") or "")
        audio_ms = float(message.get("audio_ms") or 0.0)
        end_of_turn = bool(message.get("end_of_turn"))
        if self._key is None and (text or audio_ms > 0):
            self._key = key
            self._metadata = {
                "role": "assistant" if state == "speak" else "listen",
                "state": state,
                "session_id": message.get("session_id"),
                "generation": int(message.get("generation") or 0),
                "skill_id": str(message.get("skill_id") or ""),
                "slots": dict(message.get("slots") or {}),
            }
        if self._key is not None:
            if text:
                self._text.append(text)
            self._audio_ms += max(0.0, audio_ms)
            if end_of_turn:
                record = self._flush()
                if record is not None:
                    completed.append(record)
        return completed

    def flush(self) -> dict[str, Any] | None:
        return self._flush()
