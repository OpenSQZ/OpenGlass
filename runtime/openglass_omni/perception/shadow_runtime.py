from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from .base import CVObservation, FrameEnvelope
from .pipeline import CVPipeline
from .registry import CVProviderRegistry, load_provider


LOG = logging.getLogger("openglass.perception")


class ShadowPerceptionRuntime:
    """Bridge hook that logs provider output but never controls MiniCPM."""

    def __init__(
        self,
        provider_reference: str,
        *,
        options: dict[str, Any] | None = None,
        skill_id: str = "idle_chat",
        slots: dict[str, Any] | None = None,
        log_path: str | Path = "logs/cv_events.jsonl",
        inference_timeout_ms: float = 2000.0,
    ) -> None:
        self.provider_id = provider_reference
        self.skill_id = skill_id
        self.slots = dict(slots or {})
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        provider = load_provider(provider_reference, options)
        self.pipeline = CVPipeline(
            CVProviderRegistry({provider_reference: provider}),
            on_observation=self._record,
            inference_timeout_ms=inference_timeout_ms,
        )

    def submit(self, jpeg: bytes, frame_id: str, timestamp_ms: float) -> bool:
        return self.pipeline.submit(
            FrameEnvelope(
                frame=jpeg,
                frame_id=frame_id,
                timestamp_ms=timestamp_ms,
                skill_id=self.skill_id,
                slots=dict(self.slots),
                mode="shadow",
                provider_id=self.provider_id,
            )
        )

    def snapshot(self) -> dict[str, int | bool]:
        return self.pipeline.snapshot()

    async def close(self) -> None:
        await self.pipeline.close()

    def _record(self, observation: CVObservation) -> None:
        record = {"logged_at_ms": time.time() * 1000.0, **observation.to_dict()}
        with self._write_lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        LOG.info(
            "[CV] provider=%s frame=%s error=%s values=%s",
            observation.provider,
            observation.frame_id,
            observation.error or "-",
            observation.values,
        )
