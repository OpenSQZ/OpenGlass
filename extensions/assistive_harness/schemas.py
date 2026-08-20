from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ControlIntent(str, Enum):
    STOP_SPEECH = "stop_speech"
    RESUME_SPEECH = "resume_speech"
    RESET_SESSION = "reset_session"
    CANCEL_SKILL = "cancel_skill"
    RETURN_TO_CHAT = "return_to_chat"
    ACTIVATE_SKILL = "activate_skill"
    NONE = "none"


@dataclass(slots=True)
class ASREvent:
    event_id: int
    utterance: str
    started_at_ms: float
    ended_at_ms: float
    final_at_ms: float
    model: str
    device: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ControlEvent:
    event_id: int
    intent: ControlIntent
    utterance: str
    confidence: float
    asr_event_id: int
    created_at_ms: float
    skill_id: str | None = None
    slots: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    system_prompt: str | None = None
    prompt_path: str | None = None
    prompt_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["type"] = "control.intent"
        payload["intent"] = self.intent.value
        return payload


@dataclass(slots=True)
class HarnessState:
    current_skill: str = "idle_chat"
    current_slots: dict[str, Any] = field(default_factory=dict)
    session_generation: int = 0
    control_event_id: int = 0
    asr_event_id: int = 0
    restart_in_progress: bool = False
    drop_output_until_listen: bool = False
    speech_hold_active: bool = False
    pending_skill: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
