from __future__ import annotations

from typing import Any

from .base import CVObservation, PerceptionProvider


class NoOpCVProvider:
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
            values={},
        )


class ShadowCVProvider:
    """Failure-isolated observer. Its result is never a control decision."""

    def __init__(
        self,
        provider: PerceptionProvider | None = None,
        provider_id: str | None = None,
    ):
        self.provider = provider or NoOpCVProvider()
        self.provider_id = provider_id or type(self.provider).__name__

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
        except Exception as exc:  # shadow failures must not escape
            return CVObservation(
                frame_id=frame_id,
                timestamp_ms=timestamp_ms,
                skill_id=skill_id,
                provider=f"shadow:{self.provider_id}",
                values={},
                error=f"{type(exc).__name__}: {exc}",
            )
