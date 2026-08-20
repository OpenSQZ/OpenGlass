from __future__ import annotations

import asyncio
import threading
import unittest

from runtime.openglass_omni.perception.base import CVObservation, FrameEnvelope
from runtime.openglass_omni.perception.pipeline import CVPipeline
from runtime.openglass_omni.perception.registry import CVProviderRegistry, load_provider


class _FailingProvider:
    def analyze(self, frame, frame_id, timestamp_ms, skill_id, slots):
        del frame, frame_id, timestamp_ms, skill_id, slots
        raise RuntimeError("model failed")


class _BlockingProvider:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def analyze(self, frame, frame_id, timestamp_ms, skill_id, slots):
        del frame, slots
        if frame_id == "first":
            self.started.set()
            self.release.wait(timeout=2.0)
        return CVObservation(
            frame_id=frame_id,
            timestamp_ms=timestamp_ms,
            skill_id=skill_id,
            provider="blocking",
            values={"status": "ok"},
        )


class PerceptionRegistryTests(unittest.TestCase):
    def test_builtin_provider_can_be_loaded_dynamically(self) -> None:
        provider = load_provider("noop")
        observation = provider.analyze(b"jpeg", "f1", 1.0, "idle_chat", {})
        self.assertEqual(observation.values["status"], "noop")

    def test_shadow_provider_isolates_plugin_failure(self) -> None:
        registry = CVProviderRegistry({"broken": _FailingProvider()})
        observation = registry.analyze(
            FrameEnvelope(
                frame=b"jpeg",
                frame_id="f1",
                timestamp_ms=1.0,
                skill_id="read_text",
                provider_id="broken",
            )
        )
        self.assertEqual(observation.values["status"], "error")
        self.assertIn("RuntimeError", observation.error or "")


class PerceptionPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_one_keeps_latest_waiting_frame(self) -> None:
        provider = _BlockingProvider()
        observations: list[CVObservation] = []
        pipeline = CVPipeline(
            CVProviderRegistry({"blocking": provider}),
            on_observation=observations.append,
            inference_timeout_ms=2000,
        )
        try:
            pipeline.submit(self._envelope("first"))
            started = await asyncio.to_thread(provider.started.wait, 1.0)
            self.assertTrue(started)
            pipeline.submit(self._envelope("middle"))
            pipeline.submit(self._envelope("latest"))
            provider.release.set()
            await asyncio.wait_for(pipeline.queue.join(), timeout=2.0)
        finally:
            provider.release.set()
            await pipeline.close()

        self.assertEqual([item.frame_id for item in observations], ["first", "latest"])
        self.assertEqual(pipeline.snapshot()["dropped_frames"], 1)

    @staticmethod
    def _envelope(frame_id: str) -> FrameEnvelope:
        return FrameEnvelope(
            frame=b"jpeg",
            frame_id=frame_id,
            timestamp_ms=1.0,
            skill_id="read_text",
            mode="shadow",
            provider_id="blocking",
        )


if __name__ == "__main__":
    unittest.main()
