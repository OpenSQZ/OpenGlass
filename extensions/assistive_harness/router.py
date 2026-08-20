from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .registry import SkillRegistry
from .schemas import ControlEvent, ControlIntent


_PUNCTUATION = "，,。！？!?；;：:、\"'“”‘’"
_SPEECH_PARTICLES = ("嗯", "啊", "呀", "吧", "呢", "啦", "哦")


def normalize_text(text: str) -> str:
    compact = re.sub(r"\s+", "", text or "").lower()
    return compact.strip(_PUNCTUATION)


def _strip_speech_particles(text: str) -> str:
    cleaned = text
    while cleaned and any(cleaned.endswith(item) for item in _SPEECH_PARTICLES):
        cleaned = cleaned[:-1]
    return cleaned


def _collapse_repeated_tail(text: str, max_width: int = 4) -> str:
    """Collapse a short duplicated ASR tail such as ``手机手机`` or ``母母母``."""

    cleaned = text
    while cleaned:
        collapsed = False
        for width in range(min(max_width, len(cleaned) // 2), 0, -1):
            if cleaned[-width:] == cleaned[-2 * width : -width]:
                cleaned = cleaned[:-width]
                collapsed = True
                break
        if not collapsed:
            return cleaned
    return cleaned


def _clean_slot_value(value: str) -> str:
    cleaned = _strip_speech_particles(value.strip(_PUNCTUATION))
    cleaned = _collapse_repeated_tail(cleaned)
    return _strip_speech_particles(cleaned)


@dataclass(slots=True)
class RouteResult:
    intent: ControlIntent
    skill_id: str | None = None
    slots: dict[str, str] | None = None
    reason: str = ""
    confidence: float = 1.0


class RuleIntentRouter:
    """Small deterministic Chinese intent router with exact control commands."""

    def __init__(self, registry: SkillRegistry):
        self.registry = registry
        control = registry.control
        self.stop_phrases = {
            normalize_text(item)
            for item in (control.get("stop_speech") or {}).get("phrases", [])
        }
        self.stop_embedded_phrases = {
            normalize_text(item)
            for item in (control.get("stop_speech") or {}).get("embedded_phrases", [])
        }
        self.resume_embedded_phrases = {
            normalize_text(item)
            for item in (control.get("resume_speech") or {}).get("embedded_phrases", [])
        }
        self.reset_phrases = {
            normalize_text(item)
            for item in (control.get("reset_session") or {}).get("phrases", [])
        }
        self.reset_embedded_phrases = {
            normalize_text(item)
            for item in (control.get("reset_session") or {}).get("embedded_phrases", [])
        }
        self.return_phrases = {
            normalize_text(item)
            for item in (control.get("return_to_chat") or {}).get("phrases", [])
        }
        self.cancel_phrases = {
            normalize_text(item)
            for item in (control.get("cancel_skill") or {}).get("phrases", [])
        }

    @staticmethod
    def _is_explicit_command(compact: str, phrases: set[str]) -> bool:
        polite_prefixes = ("麻烦你先", "请", "麻烦", "你先", "请你", "可以", "能不能")
        polite_suffixes = ("一下", "好吗", "可以吗", "啊", "呀", "吧", "呢", "啦", "哦")

        # FunASR commonly appends a sentence particle ("停一下啊") or repeats
        # the final syllable ("停一下下"). Build a small, bounded closure of
        # command-only variants instead of doing substring matching, so a
        # sentence such as "我刚才没有说停一下" still cannot trigger STOP.
        candidates: set[str] = set()
        pending = [compact]
        while pending:
            candidate = pending.pop()
            if not candidate or candidate in candidates:
                continue
            candidates.add(candidate)

            for prefix in polite_prefixes:
                if candidate.startswith(prefix) and len(candidate) > len(prefix):
                    pending.append(candidate[len(prefix) :])
            for suffix in polite_suffixes:
                if candidate.endswith(suffix) and len(candidate) > len(suffix):
                    pending.append(candidate[: -len(suffix)])
            if len(candidate) >= 2 and candidate[-1] == candidate[-2]:
                pending.append(candidate[:-1])

        return any(candidate in phrases for candidate in candidates)

    @staticmethod
    def _contains_command(compact: str, phrases: set[str]) -> bool:
        """Use a literal command-anchor protocol; do not infer sentence meaning."""
        return any(phrase and phrase in compact for phrase in phrases)

    @staticmethod
    def _skill_variants(compact: str) -> list[str]:
        """Build bounded FunASR variants while retaining an explicit Skill verb."""

        candidates: set[str] = set()
        pending = [compact]
        while pending:
            candidate = pending.pop()
            if not candidate or candidate in candidates:
                continue
            candidates.add(candidate)

            without_particles = _strip_speech_particles(candidate)
            if without_particles and without_particles != candidate:
                pending.append(without_particles)

            # Observed FunASR variants include ``一下下`` and omission of the
            # pronoun in ``帮我找/读``. Restore only that pronoun; never invent
            # a missing Skill verb such as ``找`` or ``读``.
            collapsed_action = re.sub(r"一下下+", "一下", candidate)
            if collapsed_action != candidate:
                pending.append(collapsed_action)
            if candidate.startswith("帮") and not candidate.startswith("帮我"):
                pending.append("帮我" + candidate[1:])

            for prefix in ("请你", "麻烦你", "麻烦", "请", "能不能"):
                if candidate.startswith(prefix) and len(candidate) > len(prefix):
                    pending.append(candidate[len(prefix) :])
        polite_prefixes = ("请你", "麻烦你", "麻烦", "请", "能不能")

        def noise_rank(candidate: str) -> tuple[int, int, int, int, int, str]:
            return (
                int(bool(re.search(r"一下下+", candidate))),
                int(_strip_speech_particles(candidate) != candidate),
                int(candidate.startswith("帮") and not candidate.startswith("帮我")),
                int(candidate.startswith(polite_prefixes)),
                len(candidate),
                candidate,
            )

        # Regexes can match both the raw and corrected ASR text. Prefer the
        # bounded, lower-noise candidate so slot extraction is deterministic.
        return sorted(candidates, key=noise_rank)

    def route(self, utterance: str) -> RouteResult:
        compact = normalize_text(utterance)
        if not compact:
            return RouteResult(ControlIntent.NONE, reason="empty")

        variants = self._skill_variants(compact)

        if self._is_explicit_command(compact, self.stop_phrases):
            return RouteResult(ControlIntent.STOP_SPEECH, reason="explicit stop command")
        # STOP wins if a transcript contains both anchors.
        if self._contains_command(compact, self.stop_embedded_phrases):
            return RouteResult(ControlIntent.STOP_SPEECH, reason="stop anchor contained")
        # RESET wins over RESUME and all skills. The protocol is deliberately
        # literal: callers can add a product wake name to the same sentence.
        if (
            self._contains_command(compact, self.reset_embedded_phrases)
            or self._is_explicit_command(compact, self.reset_phrases)
        ):
            return RouteResult(
                ControlIntent.RESET_SESSION,
                skill_id=self.registry.default_skill,
                slots={},
                reason=(
                    "reset anchor contained"
                    if self._contains_command(compact, self.reset_embedded_phrases)
                    else "explicit reset command"
                ),
            )
        if self._contains_command(compact, self.resume_embedded_phrases):
            return RouteResult(ControlIntent.RESUME_SPEECH, reason="resume anchor contained")
        if self._is_explicit_command(compact, self.cancel_phrases):
            return RouteResult(ControlIntent.CANCEL_SKILL, reason="explicit cancel command")
        if self._is_explicit_command(compact, self.return_phrases):
            return RouteResult(
                ControlIntent.RETURN_TO_CHAT,
                skill_id=self.registry.default_skill,
                slots={},
                reason="explicit return-to-chat command",
            )

        for skill_id, spec in self.registry.skills.items():
            if not bool(spec.get("enabled", False)) or skill_id == self.registry.default_skill:
                continue
            for pattern in spec.get("activation_patterns") or []:
                for candidate in variants:
                    match = re.fullmatch(str(pattern), candidate)
                    if match:
                        slots = {
                            key: _clean_slot_value(value)
                            for key, value in match.groupdict().items()
                            if value
                        }
                        return RouteResult(
                            ControlIntent.ACTIVATE_SKILL,
                            skill_id=skill_id,
                            slots=slots,
                            reason=f"matched {skill_id} pattern",
                        )
            phrases = [normalize_text(item) for item in spec.get("activation_phrases") or []]
            if any(
                candidate == phrase or candidate.startswith(phrase)
                for candidate in variants
                for phrase in phrases
            ):
                return RouteResult(
                    ControlIntent.ACTIVATE_SKILL,
                    skill_id=skill_id,
                    slots={},
                    reason=f"matched {skill_id} phrase",
                )

        return RouteResult(ControlIntent.NONE, reason="ordinary chat")

    def make_event(
        self,
        route: RouteResult,
        *,
        event_id: int,
        asr_event_id: int,
        utterance: str,
        created_at_ms: float | None = None,
    ) -> ControlEvent:
        return ControlEvent(
            event_id=event_id,
            intent=route.intent,
            skill_id=route.skill_id,
            slots=dict(route.slots or {}),
            utterance=utterance,
            confidence=route.confidence,
            asr_event_id=asr_event_id,
            created_at_ms=created_at_ms if created_at_ms is not None else time.time() * 1000,
            reason=route.reason,
        )
