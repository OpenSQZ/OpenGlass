from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .base import CVObservation, FrameEnvelope
from .registry import CVProviderRegistry


class CVPipeline:
    """Capacity-one latest-frame lane; inference never runs on the audio loop."""

    def __init__(
        self,
        providers: CVProviderRegistry,
        *,
        on_observation: Callable[[CVObservation], None],
        inference_timeout_ms: float = 2000.0,
        worker_name: str = "openglass-cv",
    ) -> None:
        self.providers = providers
        self.on_observation = on_observation
        self.inference_timeout_ms = max(1.0, float(inference_timeout_ms))
        self.queue: asyncio.Queue[FrameEnvelope] = asyncio.Queue(maxsize=1)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=worker_name)
        self.worker_task: asyncio.Task[None] | None = None
        self.closed = False
        self.submitted_frames = 0
        self.processed_frames = 0
        self.dropped_frames = 0
        self.timeout_count = 0

    def submit(self, envelope: FrameEnvelope) -> bool:
        if self.closed or envelope.mode == "disabled":
            return False
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(self._run())
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
                self.dropped_frames += 1
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(envelope)
        self.submitted_frames += 1
        return True

    def snapshot(self) -> dict[str, int | bool]:
        return {
            "closed": self.closed,
            "queued_frames": self.queue.qsize(),
            "submitted_frames": self.submitted_frames,
            "processed_frames": self.processed_frames,
            "dropped_frames": self.dropped_frames,
            "timeout_count": self.timeout_count,
        }

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.worker_task is not None:
            self.worker_task.cancel()
            await asyncio.gather(self.worker_task, return_exceptions=True)
        self.executor.shutdown(wait=False, cancel_futures=True)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            envelope = await self.queue.get()
            started = time.perf_counter()
            future = loop.run_in_executor(self.executor, self.providers.analyze, envelope)
            try:
                observation = await asyncio.wait_for(
                    asyncio.shield(future), self.inference_timeout_ms / 1000.0
                )
            except asyncio.TimeoutError:
                self.timeout_count += 1
                observation = CVObservation(
                    envelope.frame_id,
                    envelope.timestamp_ms,
                    envelope.skill_id,
                    f"{envelope.mode}:{envelope.provider_id}",
                    {"status": "timeout", "timeout_ms": self.inference_timeout_ms},
                    f"TimeoutError: exceeded {self.inference_timeout_ms:.0f} ms",
                )
                self._emit(observation)
                try:
                    await future
                except Exception:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._emit(
                    CVObservation(
                        envelope.frame_id,
                        envelope.timestamp_ms,
                        envelope.skill_id,
                        f"{envelope.mode}:{envelope.provider_id}",
                        {"status": "error"},
                        f"{type(exc).__name__}: {exc}",
                    )
                )
            else:
                observation.values.setdefault(
                    "pipeline_latency_ms",
                    round((time.perf_counter() - started) * 1000.0, 1),
                )
                self.processed_frames += 1
                self._emit(observation)
            finally:
                self.queue.task_done()

    def _emit(self, observation: CVObservation) -> None:
        try:
            self.on_observation(observation)
        except Exception:
            pass
