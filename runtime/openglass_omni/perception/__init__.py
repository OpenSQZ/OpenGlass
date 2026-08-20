"""Transport-neutral, non-blocking CV shadow plugins for OpenGlass."""

from .base import CVObservation, FrameEnvelope, PerceptionProvider
from .pipeline import CVPipeline
from .registry import CVProviderRegistry, load_provider
from .shadow_runtime import ShadowPerceptionRuntime

__all__ = [
    "CVObservation",
    "CVPipeline",
    "CVProviderRegistry",
    "FrameEnvelope",
    "PerceptionProvider",
    "ShadowPerceptionRuntime",
    "load_provider",
]
