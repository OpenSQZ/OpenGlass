from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class FrameEnvelope:
    """Transport-neutral frame handed from an Adapter to the CV worker."""

    frame: bytes | None
    frame_id: str
    timestamp_ms: float
    skill_id: str
    slots: dict[str, Any] = field(default_factory=dict)
    mode: str = "shadow"
    provider_id: str = "noop"


@dataclass(slots=True)
class CVObservation:
    frame_id: str
    timestamp_ms: float
    skill_id: str
    provider: str
    values: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PerceptionProvider(Protocol):
    """Synchronous plugin contract; CVPipeline always calls it off-loop."""

    def analyze(
        self,
        frame: bytes | None,
        frame_id: str,
        timestamp_ms: float,
        skill_id: str,
        slots: dict[str, Any],
    ) -> CVObservation:
        ...
