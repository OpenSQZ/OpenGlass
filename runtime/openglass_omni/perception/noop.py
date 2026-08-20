from __future__ import annotations

from typing import Any

from .base import CVObservation, PerceptionProvider


class NoOpProvider:
    def analyze(
        self,
        frame: bytes | None,
        frame_id: str,
        timestamp_ms: float,
        skill_id: str,
        slots: dict[str, Any],
    ) -> CVObservation:
        del frame, slots
        return CVObservation(
            frame_id=frame_id,
            timestamp_ms=timestamp_ms,
            skill_id=skill_id,
            provider="noop",
            values={"status": "noop"},
        )


class ShadowProvider:
    """Turn provider failures into observations instead of control failures."""

    def __init__(self, provider: PerceptionProvider, provider_id: str):
        self.provider = provider
        self.provider_id = provider_id

    def analyze(
        self,
        frame: bytes | None,
        frame_id: str,
        timestamp_ms: float,
        skill_id: str,
        slots: dict[str, Any],
    ) -> CVObservation:
        try:
            result = self.provider.analyze(
                frame, frame_id, timestamp_ms, skill_id, slots
            )
            result.provider = f"shadow:{result.provider}"
            return result
        except Exception as exc:
            return CVObservation(
                frame_id=frame_id,
                timestamp_ms=timestamp_ms,
                skill_id=skill_id,
                provider=f"shadow:{self.provider_id}",
                values={"status": "error"},
                error=f"{type(exc).__name__}: {exc}",
            )
