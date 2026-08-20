from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .registry import RegistryError, SkillRegistry
from .schemas import ControlEvent, ControlIntent, HarnessState


@dataclass(slots=True)
class ControlDecision:
    accepted: bool
    action: str
    reason: str
    payload: dict[str, Any]


class HarnessController:
    def __init__(self, registry: SkillRegistry):
        self.registry = registry
        self.state = HarnessState(current_skill=registry.default_skill)
        self._seen_asr_ids: set[int] = set()
        self._last_skill_activation_ms: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    @staticmethod
    def _skill_key(skill_id: str, slots: dict[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]]:
        return skill_id, tuple(sorted((str(key), str(value)) for key, value in slots.items()))

    def process(self, event: ControlEvent, now_ms: float | None = None) -> ControlDecision:
        now = now_ms if now_ms is not None else time.time() * 1000
        if event.asr_event_id in self._seen_asr_ids:
            return ControlDecision(False, "ignore", "duplicate_asr_event", {})
        self._seen_asr_ids.add(event.asr_event_id)
        self.state.asr_event_id = max(self.state.asr_event_id, event.asr_event_id)
        self.state.control_event_id = max(self.state.control_event_id, event.event_id)

        if event.intent is ControlIntent.STOP_SPEECH:
            self.state.speech_hold_active = True
            self.state.drop_output_until_listen = True
            return ControlDecision(True, "stop_speech", "highest_priority", event.to_dict())

        if event.intent is ControlIntent.RESUME_SPEECH:
            self.state.speech_hold_active = False
            self.state.drop_output_until_listen = False
            return ControlDecision(True, "resume_speech", "explicit_resume", event.to_dict())

        if event.intent is ControlIntent.RESET_SESSION:
            rendered = self.registry.render(self.registry.default_skill, {})
            event.skill_id = self.registry.default_skill
            event.slots = {}
            event.system_prompt = rendered.text
            event.prompt_path = rendered.path
            event.prompt_sha256 = rendered.sha256
            self.state.restart_in_progress = True
            self.state.pending_skill = None
            return ControlDecision(True, "restart_session", "explicit_reset", event.to_dict())

        if event.intent in {ControlIntent.CANCEL_SKILL, ControlIntent.RETURN_TO_CHAT}:
            event.skill_id = self.registry.default_skill
            event.slots = {}

        if event.intent in {
            ControlIntent.CANCEL_SKILL,
            ControlIntent.RETURN_TO_CHAT,
            ControlIntent.ACTIVATE_SKILL,
        }:
            skill_id = event.skill_id or self.registry.default_skill
            slots = dict(event.slots or {})
            try:
                rendered = self.registry.render(skill_id, slots)
            except RegistryError as exc:
                return ControlDecision(False, "clarify", str(exc), event.to_dict())

            if self.state.restart_in_progress:
                self.state.pending_skill = {"skill_id": skill_id, "slots": slots}
                return ControlDecision(True, "queue_skill", "restart_in_progress_last_write_wins", event.to_dict())

            key = self._skill_key(skill_id, slots)
            last = self._last_skill_activation_ms.get(key)
            cooldown = self.registry.cooldown_ms(skill_id)
            if skill_id == self.state.current_skill and slots == self.state.current_slots:
                return ControlDecision(False, "ignore", "same_skill_same_slots", event.to_dict())
            if last is not None and now - last < cooldown:
                return ControlDecision(False, "ignore", "skill_cooldown", event.to_dict())

            self._last_skill_activation_ms[key] = now
            event.system_prompt = rendered.text
            event.prompt_path = rendered.path
            event.prompt_sha256 = rendered.sha256
            self.state.restart_in_progress = True
            return ControlDecision(True, "activate_skill", "skill_switch", event.to_dict())

        return ControlDecision(False, "pass_chat", "ordinary_chat", event.to_dict())

    def mark_restart_complete(self, skill_id: str, slots: dict[str, Any], generation: int) -> dict[str, Any] | None:
        self.state.current_skill = skill_id
        self.state.current_slots = dict(slots)
        self.state.session_generation = int(generation)
        self.state.restart_in_progress = False
        self.state.speech_hold_active = False
        self.state.drop_output_until_listen = False
        pending = self.state.pending_skill
        self.state.pending_skill = None
        return pending

    def mark_listen_fence(self) -> None:
        if not self.state.speech_hold_active:
            self.state.drop_output_until_listen = False

    def mark_disconnected(self) -> None:
        self.state.restart_in_progress = False
        self.state.pending_skill = None
        self.state.speech_hold_active = False
        self.state.drop_output_until_listen = False
