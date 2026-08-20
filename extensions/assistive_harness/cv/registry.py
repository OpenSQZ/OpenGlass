from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import CVObservation, FrameEnvelope, PerceptionProvider
from .noop import NoOpCVProvider, ShadowCVProvider


class CVProviderRegistry:
    """Small explicit registry shared by browser and future device Adapters."""

    def __init__(self, providers: dict[str, PerceptionProvider] | None = None):
        self.providers: dict[str, PerceptionProvider] = {
            "noop": NoOpCVProvider(),
            **(providers or {}),
        }
        self._shadow = {
            provider_id: ShadowCVProvider(provider, provider_id)
            for provider_id, provider in self.providers.items()
        }

    def register(self, provider_id: str, provider: PerceptionProvider) -> None:
        normalized = provider_id.strip()
        if not normalized:
            raise ValueError("provider_id cannot be empty")
        self.providers[normalized] = provider
        self._shadow[normalized] = ShadowCVProvider(provider, normalized)

    def analyze(self, envelope: FrameEnvelope) -> CVObservation:
        if envelope.mode != "shadow":
            return CVObservation(
                frame_id=envelope.frame_id,
                timestamp_ms=envelope.timestamp_ms,
                skill_id=envelope.skill_id,
                provider=f"{envelope.mode}:{envelope.provider_id}",
                values={"status": "skipped", "reason": "unsupported_cv_mode"},
                error=f"Unsupported cv_mode: {envelope.mode}",
            )
        provider = self._shadow.get(envelope.provider_id)
        if provider is None:
            return CVObservation(
                frame_id=envelope.frame_id,
                timestamp_ms=envelope.timestamp_ms,
                skill_id=envelope.skill_id,
                provider=f"shadow:{envelope.provider_id}",
                values={"status": "skipped", "reason": "unknown_provider"},
                error=f"Unknown CV provider: {envelope.provider_id}",
            )
        return provider.analyze(
            envelope.frame,
            envelope.frame_id,
            envelope.timestamp_ms,
            envelope.skill_id,
            envelope.slots,
        )


def build_provider_registry(
    config: dict[str, Any], *, config_dir: Path
) -> CVProviderRegistry:
    registry = CVProviderRegistry()
    providers = dict((config.get("cv") or {}).get("providers") or {})
    for provider_id, raw_spec in providers.items():
        spec = dict(raw_spec or {})
        provider_type = str(spec.get("type") or provider_id)
        if provider_type == "noop":
            registry.register(str(provider_id), NoOpCVProvider())
            continue
        if provider_type == "yolo_onnx":
            from .yolo_onnx import YoloOnnxProvider

            raw_path = Path(str(spec.get("model_path") or ""))
            model_path = raw_path if raw_path.is_absolute() else config_dir / raw_path
            registry.register(
                str(provider_id),
                YoloOnnxProvider(
                    model_path.resolve(),
                    device=str(spec.get("device") or "cpu"),
                    confidence=float(spec.get("confidence", 0.25)),
                    image_size=int(spec.get("image_size", 640)),
                    target_aliases=dict(spec.get("target_aliases") or {}),
                ),
            )
            continue
        raise ValueError(
            f"Unsupported CV provider type {provider_type!r} for {provider_id!r}"
        )
    return registry
