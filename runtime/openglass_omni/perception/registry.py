from __future__ import annotations

import importlib
import inspect
from typing import Any

from .base import CVObservation, FrameEnvelope, PerceptionProvider
from .noop import NoOpProvider, ShadowProvider


BUILTIN_PROVIDERS = {
    "noop": f"{__package__}.noop:NoOpProvider",
    "ocr_template": f"{__package__}.ocr_template:OcrTemplateProvider",
    "yolo_onnx": f"{__package__}.yolo_onnx:YoloOnnxProvider",
}


def load_provider(reference: str, options: dict[str, Any] | None = None) -> PerceptionProvider:
    """Load `package.module:Class` or one of the small built-in aliases."""
    target = BUILTIN_PROVIDERS.get(reference, reference)
    if ":" not in target:
        raise ValueError(
            "provider must be a built-in alias or package.module:Class"
        )
    module_name, attribute_name = target.split(":", 1)
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute_name)
    kwargs = dict(options or {})
    instance = factory(**kwargs) if inspect.isclass(factory) else factory(kwargs)
    if not callable(getattr(instance, "analyze", None)):
        raise TypeError(f"provider {reference!r} does not implement analyze()")
    return instance


class CVProviderRegistry:
    def __init__(self, providers: dict[str, PerceptionProvider] | None = None):
        raw = {"noop": NoOpProvider(), **(providers or {})}
        self.providers = raw
        self.shadow = {
            provider_id: ShadowProvider(provider, provider_id)
            for provider_id, provider in raw.items()
        }

    def analyze(self, envelope: FrameEnvelope) -> CVObservation:
        if envelope.mode != "shadow":
            return CVObservation(
                envelope.frame_id,
                envelope.timestamp_ms,
                envelope.skill_id,
                f"{envelope.mode}:{envelope.provider_id}",
                {"status": "skipped", "reason": "unsupported_mode"},
                f"Unsupported mode: {envelope.mode}",
            )
        provider = self.shadow.get(envelope.provider_id)
        if provider is None:
            return CVObservation(
                envelope.frame_id,
                envelope.timestamp_ms,
                envelope.skill_id,
                f"shadow:{envelope.provider_id}",
                {"status": "skipped", "reason": "unknown_provider"},
                f"Unknown provider: {envelope.provider_id}",
            )
        return provider.analyze(
            envelope.frame,
            envelope.frame_id,
            envelope.timestamp_ms,
            envelope.skill_id,
            envelope.slots,
        )
