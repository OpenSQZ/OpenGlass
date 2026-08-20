from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import os
import signal
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

import aiohttp
from aiohttp import web
import numpy as np

from ..registry import SkillRegistry

try:
    from PIL import Image
except Exception:  # pragma: no cover - optional at import time
    Image = None


LOG = logging.getLogger("assistive_harness.phase_b.rokid")
SAMPLE_RATE_IN = 16_000
SAMPLE_RATE_OUT = 24_000
PCM16_WIDTH = 2
CTRL_C_HARD_EXIT_S = 10.0


def now_ms() -> float:
    return time.time() * 1000.0


class CtrlCExitWatchdog:
    """Guarantee that a Windows Ctrl+C cannot leave the Rokid port behind."""

    def __init__(self, hard_exit_s: float = CTRL_C_HARD_EXIT_S):
        self.hard_exit_s = max(1.0, float(hard_exit_s))
        self._armed = False
        self._lock = threading.Lock()

    @property
    def armed(self) -> bool:
        return self._armed

    def arm(self) -> None:
        with self._lock:
            if self._armed:
                return
            self._armed = True
        LOG.info(
            "Console interrupt received; closing Rokid listener and runtime "
            "(hard cutoff %.1fs)",
            self.hard_exit_s,
        )
        threading.Thread(
            target=self._force_exit_after_deadline,
            name="rokid-ctrl-c-watchdog",
            daemon=True,
        ).start()

    def _force_exit_after_deadline(self) -> None:
        time.sleep(self.hard_exit_s)
        LOG.error(
            "Rokid shutdown exceeded %.1fs; forcing process exit so port 18080 "
            "cannot remain occupied",
            self.hard_exit_s,
        )
        logging.shutdown()
        os._exit(130)


def pcm16le_to_float32(raw: bytes) -> np.ndarray:
    if len(raw) % PCM16_WIDTH:
        raw = raw[:-1]
    if not raw:
        return np.zeros(0, dtype=np.float32)
    return (
        np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    ).clip(-1.0, 1.0)


def apply_pcm16_gain(raw: bytes, gain: float) -> bytes:
    """Apply device-specific gain with saturation while preserving PCM16 LE."""
    if gain <= 0:
        raise ValueError("input gain must be greater than zero")
    if gain == 1.0 or not raw:
        return raw
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    return np.clip(samples * gain, -32768, 32767).astype("<i2").tobytes()


def make_session_ready_chime(
    sample_rate: int = SAMPLE_RATE_OUT, amplitude: float = 0.32
) -> np.ndarray:
    """Return a short, non-speech two-note cue for restart_complete."""

    amplitude = min(1.0, max(0.0, float(amplitude)))

    def tone(frequency_hz: float, duration_s: float) -> np.ndarray:
        count = max(2, int(sample_rate * duration_s))
        phase = np.arange(count, dtype=np.float32) / float(sample_rate)
        envelope = np.sin(np.linspace(0.0, np.pi, count, dtype=np.float32)) ** 2
        return (
            amplitude
            * envelope
            * np.sin(2.0 * np.pi * frequency_hz * phase)
        ).astype(np.float32)

    gap = np.zeros(max(1, int(sample_rate * 0.030)), dtype=np.float32)
    return np.concatenate((tone(880.0, 0.080), gap, tone(1174.66, 0.105)))


def float32_to_base64(samples: np.ndarray) -> str:
    return base64.b64encode(
        samples.astype(np.float32, copy=False).tobytes()
    ).decode("ascii")


def same_slots(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return {str(key): str(value) for key, value in left.items()} == {
        str(key): str(value) for key, value in right.items()
    }


def rotate_jpeg_clockwise(jpeg: bytes, degrees: int, quality: int = 95) -> bytes:
    degrees %= 360
    if degrees == 0 or Image is None:
        return jpeg
    methods = {
        90: Image.Transpose.ROTATE_270,
        180: Image.Transpose.ROTATE_180,
        270: Image.Transpose.ROTATE_90,
    }
    method = methods.get(degrees)
    if method is None:
        raise ValueError("image rotation must be 0, 90, 180, or 270")
    with Image.open(io.BytesIO(jpeg)) as image:
        output = io.BytesIO()
        image.convert("RGB").transpose(method).save(
            output, format="JPEG", quality=quality
        )
        return output.getvalue()


class DropOldestAudioQueue:
    """Bounded device-audio queue that always preserves the newest packets."""

    def __init__(self, max_packets: int):
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=max(1, max_packets))
        self.dropped_packets = 0

    @property
    def size(self) -> int:
        return self._queue.qsize()

    def put_nowait(self, raw: bytes) -> None:
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self.dropped_packets += 1
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait(raw)

    async def get(self, timeout_s: float) -> bytes | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return None

    def clear(self) -> int:
        cleared = 0
        while True:
            try:
                self._queue.get_nowait()
                cleared += 1
            except asyncio.QueueEmpty:
                return cleared


class AudioMirrorChunker:
    """Repacketize Rokid PCM into the same 100 ms frames used in Phase A."""

    def __init__(self, samples_per_frame: int = 1_600):
        self.target_bytes = max(1, samples_per_frame) * PCM16_WIDTH
        self._carry = bytearray()

    def feed(self, raw: bytes) -> list[np.ndarray]:
        if len(raw) % PCM16_WIDTH:
            raw = raw[:-1]
        self._carry.extend(raw)
        frames: list[np.ndarray] = []
        while len(self._carry) >= self.target_bytes:
            frame = bytes(self._carry[: self.target_bytes])
            del self._carry[: self.target_bytes]
            frames.append(pcm16le_to_float32(frame))
        return frames

    def clear(self) -> None:
        self._carry.clear()


@dataclass(slots=True)
class LatestFrame:
    jpeg: bytes | None = None
    timestamp_ms: float = 0.0
    sequence: int = 0

    def set(self, jpeg: bytes, timestamp_ms: float) -> None:
        self.jpeg = jpeg
        self.timestamp_ms = timestamp_ms
        self.sequence += 1


@dataclass(slots=True)
class OutputGate:
    generation: int = 0
    current_skill: str = "idle_chat"
    current_slots: dict[str, Any] | None = None
    speech_hold_active: bool = False
    drop_output_until_listen: bool = False
    restart_in_progress: bool = False
    dropped_old_text: int = 0
    dropped_old_audio: int = 0
    stop_count: int = 0
    local_cue_mute_until_mono: float = 0.0

    def __post_init__(self) -> None:
        if self.current_slots is None:
            self.current_slots = {}

    def stop(self) -> None:
        self.stop_count += 1
        self.speech_hold_active = True
        self.drop_output_until_listen = True

    def resume(self) -> None:
        self.speech_hold_active = False
        self.drop_output_until_listen = False

    def replacement_ready(self) -> None:
        self.speech_hold_active = False
        self.drop_output_until_listen = False
        self.restart_in_progress = False


class SpeakerSink(Protocol):
    async def start(self) -> None: ...

    async def enqueue(self, pcm: np.ndarray, generation: int) -> None: ...

    async def block_and_flush(self) -> None: ...

    async def resume(self) -> None: ...

    async def close(self) -> None: ...

    def pending_ms(self) -> float: ...


class NullSpeaker:
    async def start(self) -> None:
        return None

    async def enqueue(self, pcm: np.ndarray, generation: int) -> None:
        return None

    async def block_and_flush(self) -> None:
        return None

    async def resume(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def pending_ms(self) -> float:
        return 0.0


class PCSpeaker:
    """Interruptible 24 kHz PC playback with a short write quantum."""

    def __init__(self, block_ms: int = 50):
        self.block_samples = max(240, int(SAMPLE_RATE_OUT * block_ms / 1000))
        self.queue: asyncio.Queue[tuple[int, int, np.ndarray]] = asyncio.Queue(maxsize=32)
        self.blocked = False
        self.epoch = 0
        self._stream: Any = None
        self._worker: asyncio.Task[None] | None = None
        self._io_lock = asyncio.Lock()
        self._pending_samples = 0

    def _open_stream(self) -> None:
        import sounddevice as sd  # type: ignore

        stream = sd.OutputStream(
            samplerate=SAMPLE_RATE_OUT,
            channels=1,
            dtype="float32",
            blocksize=self.block_samples,
            latency="low",
        )
        stream.start()
        self._stream = stream

    def _close_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.abort()
        finally:
            stream.close()

    async def start(self) -> None:
        try:
            await asyncio.to_thread(self._open_stream)
            self._worker = asyncio.create_task(self._play_loop())
            LOG.info("PC speaker output ready")
        except Exception as exc:  # pragma: no cover - hardware dependent
            self._stream = None
            LOG.warning("PC speaker disabled: %s", exc)

    async def enqueue(self, pcm: np.ndarray, generation: int) -> None:
        if self._stream is None or self.blocked or pcm.size == 0:
            return
        if self.queue.full():
            try:
                _epoch, _generation, dropped = self.queue.get_nowait()
                self._pending_samples = max(
                    0, self._pending_samples - int(dropped.size)
                )
            except asyncio.QueueEmpty:
                pass
        copied = pcm.astype(np.float32, copy=True)
        self._pending_samples += int(copied.size)
        self.queue.put_nowait((self.epoch, generation, copied))

    async def _play_loop(self) -> None:
        while True:
            epoch, _generation, pcm = await self.queue.get()
            try:
                for offset in range(0, pcm.size, self.block_samples):
                    if self.blocked or epoch != self.epoch or self._stream is None:
                        break
                    chunk = pcm[offset : offset + self.block_samples].reshape(-1, 1)
                    stream = self._stream
                    try:
                        async with self._io_lock:
                            if self.blocked or epoch != self.epoch or self._stream is not stream:
                                break
                            await asyncio.to_thread(stream.write, chunk)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # pragma: no cover - hardware dependent
                        LOG.warning("speaker write failed: %s", exc)
                        break
            finally:
                self._pending_samples = max(
                    0, self._pending_samples - int(pcm.size)
                )

    async def block_and_flush(self) -> None:
        self.blocked = True
        self.epoch += 1
        self._pending_samples = 0
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if self._stream is not None:
            try:
                # Windows MME may reject start() immediately after abort() while
                # the previous buffer is still retiring.  Close the device here
                # and create a fresh stream only when output is resumed.
                async with self._io_lock:
                    await asyncio.to_thread(self._close_stream)
            except Exception as exc:  # pragma: no cover - hardware dependent
                LOG.warning("speaker flush failed: %s", exc)

    async def resume(self) -> None:
        if self._stream is None:
            try:
                async with self._io_lock:
                    if self._stream is None:
                        await asyncio.to_thread(self._open_stream)
            except Exception as exc:  # pragma: no cover - hardware dependent
                LOG.warning("speaker resume failed: %s", exc)
        self.blocked = False

    async def close(self) -> None:
        self.blocked = True
        self._pending_samples = 0
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        if self._stream is not None:
            try:
                async with self._io_lock:
                    await asyncio.to_thread(self._close_stream)
            except Exception:
                pass

    def pending_ms(self) -> float:
        return self._pending_samples * 1000.0 / SAMPLE_RATE_OUT


@dataclass(slots=True)
class RokidRuntimeConfig:
    host: str = "0.0.0.0"
    port: int = 18_080
    gateway: str = "localhost:8040"
    gateway_tls: bool = False
    harness_url: str = "ws://127.0.0.1:8021/ws/control"
    harness_client_id: str = "rokid-phase-b"
    skills_config: str = ""
    cleanup_mode: str = "light"
    chunk_ms: int = 1_000
    force_listen_count: int = 3
    max_new_speak_tokens_per_chunk: int = 20
    length_penalty: float = 1.1
    max_slice_nums: int = 1
    audio_queue_packets: int = 96
    input_gain: float = 12.0
    image_rotate_cw: int = 270
    image_jpeg_quality: int = 95
    image_resend_s: float = 0.5
    image_max_age_s: float = 30.0
    prepare_timeout_s: float = 120.0
    close_timeout_s: float = 2.0
    reconnect_s: float = 1.5
    play_audio: bool = True
    session_ready_chime: bool = True
    session_ready_chime_volume: float = 0.32
    session_ready_chime_feedback_guard_s: float = 0.65
    playback_echo_tail_s: float = 0.80


@dataclass(slots=True)
class SessionSpec:
    generation: int
    skill_id: str
    slots: dict[str, Any]
    system_prompt: str


class SessionTelemetry(Protocol):
    connected: bool

    async def send(self, payload: dict[str, Any]) -> None: ...


class GatewayDuplexSession:
    """One generation-fenced connection to the existing MiniCPM Gateway."""

    def __init__(
        self,
        config: RokidRuntimeConfig,
        spec: SessionSpec,
        audio_queue: DropOldestAudioQueue,
        latest_frame: LatestFrame,
        gate: OutputGate,
        on_result: Callable[["GatewayDuplexSession", dict[str, Any]], Awaitable[None]],
    ):
        self.config = config
        self.spec = spec
        self.audio_queue = audio_queue
        self.latest_frame = latest_frame
        self.gate = gate
        self.on_result = on_result
        self.session_id = (
            f"omni_rokid_pb_g{spec.generation}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        )
        self.status = "created"
        self.last_error = ""
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._stopped = asyncio.Event()
        self._closing = False
        self._send_lock = asyncio.Lock()

    async def _send_json(self, payload: dict[str, Any]) -> None:
        ws = self.ws
        if ws is None or ws.closed:
            raise RuntimeError("gateway session is not connected")
        async with self._send_lock:
            await ws.send_json(payload)

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self.config.gateway_tls:
            return None
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._ready.add_done_callback(
            lambda future: None if future.cancelled() else future.exception()
        )
        self._task = asyncio.create_task(self._run())
        await asyncio.wait_for(
            asyncio.shield(self._ready), timeout=self.config.prepare_timeout_s
        )

    async def _run(self) -> None:
        scheme = "wss" if self.config.gateway_tls else "ws"
        url = f"{scheme}://{self.config.gateway}/ws/duplex/{self.session_id}"
        self.status = "connecting"
        LOG.info("[GW] connecting generation=%d %s", self.spec.generation, url)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(
                    url,
                    heartbeat=30,
                    max_msg_size=0,
                    ssl=self._ssl_context(),
                ) as ws:
                    self.ws = ws
                    await self._prepare(ws)
                    self.status = "running"
                    if self._ready and not self._ready.done():
                        self._ready.set_result(None)
                    send_task = asyncio.create_task(self._send_loop(ws))
                    receive_task = asyncio.create_task(self._receive_loop(ws))
                    done, pending = await asyncio.wait(
                        (send_task, receive_task),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*done, *pending, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.status = "error"
            self.last_error = str(exc)
            if self._ready and not self._ready.done():
                self._ready.set_exception(exc)
            if not self._closing:
                LOG.warning("[GW] session %s failed: %s", self.session_id, exc)
        finally:
            self.ws = None
            self._stopped.set()
            if self.status != "error":
                self.status = "stopped"

    async def _prepare(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.status = "queued"
        while True:
            message = await ws.receive()
            if message.type != aiohttp.WSMsgType.TEXT:
                if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise RuntimeError("gateway closed while queued")
                continue
            payload = json.loads(message.data)
            message_type = payload.get("type")
            if message_type == "queue_done":
                break
            if message_type == "error":
                raise RuntimeError(payload.get("error") or "gateway queue error")
            if message_type in {"queued", "queue_update"}:
                LOG.info(
                    "[GW] queue position=%s eta=%s",
                    payload.get("position"),
                    payload.get("estimated_wait_s"),
                )

        self.status = "preparing"
        await self._send_json(
            {
                "type": "prepare",
                "system_prompt": self.spec.system_prompt,
                "config": {
                    "force_listen_count": self.config.force_listen_count,
                    "chunk_ms": self.config.chunk_ms,
                    "generate_audio": True,
                    "max_new_speak_tokens_per_chunk": (
                        self.config.max_new_speak_tokens_per_chunk
                    ),
                    "length_penalty": self.config.length_penalty,
                },
                "max_slice_nums": self.config.max_slice_nums,
                "deferred_finalize": True,
            }
        )
        while True:
            message = await ws.receive()
            if message.type != aiohttp.WSMsgType.TEXT:
                if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise RuntimeError("gateway closed while preparing")
                continue
            payload = json.loads(message.data)
            if payload.get("type") == "prepared":
                LOG.info(
                    "[GW] prepared session=%s generation=%d skill=%s",
                    self.session_id,
                    self.spec.generation,
                    self.spec.skill_id,
                )
                return
            if payload.get("type") == "error":
                raise RuntimeError(payload.get("error") or "gateway prepare error")

    async def _next_audio_chunk(self, carry: bytearray) -> np.ndarray | None:
        samples = int(SAMPLE_RATE_IN * self.config.chunk_ms / 1000)
        target_bytes = samples * PCM16_WIDTH
        output = bytearray()
        if carry:
            take = min(target_bytes, len(carry))
            output.extend(carry[:take])
            del carry[:take]
        deadline = time.monotonic() + max(0.1, self.config.chunk_ms / 1000 * 1.5)
        while len(output) < target_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            raw = await self.audio_queue.get(remaining)
            if raw is None:
                break
            needed = target_bytes - len(output)
            output.extend(raw[:needed])
            if len(raw) > needed:
                carry.extend(raw[needed:])
        if not output:
            return None
        if len(output) < target_bytes:
            output.extend(b"\x00" * (target_bytes - len(output)))
        return pcm16le_to_float32(bytes(output))

    async def _send_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        carry = bytearray()
        last_frame_sequence = -1
        last_frame_sent = 0.0
        while not self._closing:
            audio = await self._next_audio_chunk(carry)
            if audio is None:
                continue
            payload: dict[str, Any] = {
                "type": "audio_chunk",
                "audio_base64": float32_to_base64(audio),
            }
            if self.gate.speech_hold_active:
                payload["force_listen"] = True
            frame = self.latest_frame
            frame_age_ms = now_ms() - frame.timestamp_ms
            frame_due = time.monotonic() - last_frame_sent >= self.config.image_resend_s
            if (
                frame.jpeg
                and frame_age_ms <= self.config.image_max_age_s * 1000
                and (frame.sequence != last_frame_sequence or frame_due)
            ):
                payload["frame_base64_list"] = [
                    base64.b64encode(frame.jpeg).decode("ascii")
                ]
                last_frame_sequence = frame.sequence
                last_frame_sent = time.monotonic()
            await self._send_json(payload)

    async def inject_task(self, text: str) -> bool:
        """Send one text+latest-frame task to a newly prepared Skill Session."""
        text = text.strip()
        if not text:
            return False
        payload: dict[str, Any] = {
            "type": "audio_chunk",
            "inject_text": text,
            "max_slice_nums": self.config.max_slice_nums,
        }
        frame = self.latest_frame
        frame_age_ms = now_ms() - frame.timestamp_ms
        has_frame = bool(
            frame.jpeg
            and frame_age_ms <= self.config.image_max_age_s * 1000
        )
        if has_frame and frame.jpeg is not None:
            payload["frame_base64_list"] = [
                base64.b64encode(frame.jpeg).decode("ascii")
            ]
        await self._send_json(payload)
        LOG.info(
            "[TASK] injected generation=%d skill=%s frame=%s text=%r",
            self.spec.generation,
            self.spec.skill_id,
            has_frame,
            text,
        )
        return has_frame

    async def _receive_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for message in ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                payload = json.loads(message.data)
                message_type = payload.get("type")
                if message_type in {"result", "audio_only"}:
                    await self.on_result(self, payload)
                elif message_type == "stopped":
                    return
                elif message_type in {"timeout", "error"}:
                    raise RuntimeError(
                        payload.get("error") or payload.get("reason") or message_type
                    )
            elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return

    async def stop(self, cleanup_mode: str) -> None:
        self._closing = True
        if self.ws is not None and not self.ws.closed:
            try:
                await self._send_json({"type": "stop", "cleanup_mode": cleanup_mode})
            except Exception:
                pass
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=self.config.close_timeout_s)
        except asyncio.TimeoutError:
            pass
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


SessionFactory = Callable[
    [
        RokidRuntimeConfig,
        SessionSpec,
        DropOldestAudioQueue,
        LatestFrame,
        OutputGate,
        Callable[[GatewayDuplexSession, dict[str, Any]], Awaitable[None]],
    ],
    GatewayDuplexSession,
]


class GatewaySessionManager:
    """Python counterpart of the frozen BrowserSessionAdapter contract."""

    def __init__(
        self,
        config: RokidRuntimeConfig,
        registry: SkillRegistry,
        audio_queue: DropOldestAudioQueue,
        latest_frame: LatestFrame,
        speaker: SpeakerSink,
        session_factory: SessionFactory = GatewayDuplexSession,
    ):
        self.config = config
        self.registry = registry
        self.audio_queue = audio_queue
        self.latest_frame = latest_frame
        self.speaker = speaker
        self.session_factory = session_factory
        self.gate = OutputGate(current_skill=registry.default_skill)
        self.harness: SessionTelemetry | None = None
        self.active: GatewayDuplexSession | None = None
        self._restart_lock = asyncio.Lock()
        self._playback_epoch = 0
        self._playback_release_task: asyncio.Task[None] | None = None

    async def send_telemetry(self, payload: dict[str, Any]) -> None:
        if self.harness is not None:
            await self.harness.send(payload)

    async def _set_playback_active(self, active: bool, source: str) -> None:
        await self.send_telemetry(
            {
                "type": "playback.state",
                "active": active,
                "source": source,
                "generation": self.gate.generation,
                "pending_ms": round(self.speaker.pending_ms(), 1),
            }
        )

    async def _mark_playback_started(self, source: str) -> None:
        self._playback_epoch += 1
        epoch = self._playback_epoch
        if self._playback_release_task is not None:
            self._playback_release_task.cancel()
        await self._set_playback_active(True, source)

        async def release_after_drain() -> None:
            try:
                while self.speaker.pending_ms() > 0:
                    await asyncio.sleep(
                        max(0.02, min(0.25, self.speaker.pending_ms() / 1000.0))
                    )
                await asyncio.sleep(max(0.0, self.config.playback_echo_tail_s))
                if epoch == self._playback_epoch:
                    await self._set_playback_active(False, source)
            except asyncio.CancelledError:
                raise

        self._playback_release_task = asyncio.create_task(release_after_drain())

    async def _clear_playback_state(self, source: str) -> None:
        self._playback_epoch += 1
        if self._playback_release_task is not None:
            self._playback_release_task.cancel()
            await asyncio.gather(self._playback_release_task, return_exceptions=True)
            self._playback_release_task = None
        await self._set_playback_active(False, source)

    def _make_spec(
        self,
        skill_id: str,
        slots: dict[str, Any],
        system_prompt: str | None = None,
    ) -> SessionSpec:
        rendered = self.registry.render(skill_id, slots)
        return SessionSpec(
            generation=self.gate.generation,
            skill_id=skill_id,
            slots=dict(slots),
            system_prompt=system_prompt or rendered.text,
        )

    async def start_initial(self) -> None:
        async with self._restart_lock:
            if self.active is not None:
                return
            spec = self._make_spec(self.registry.default_skill, {})
            session = self.session_factory(
                self.config,
                spec,
                self.audio_queue,
                self.latest_frame,
                self.gate,
                self.handle_result,
            )
            self.active = session
            try:
                await session.start()
            except Exception:
                self.active = None
                raise
            await self._emit_bound()

    async def _emit_bound(self) -> None:
        await self.send_telemetry(
            {
                "type": "session.state",
                "phase": "bound",
                "generation": self.gate.generation,
                "skill_id": self.gate.current_skill,
                "slots": dict(self.gate.current_slots or {}),
                "session_id": self.active.session_id if self.active else None,
            }
        )

    async def emit_recovery_sync(self) -> None:
        if self.active is None:
            return
        await self.send_telemetry(
            {
                "type": "session.state",
                "phase": "restart_complete",
                "generation": self.gate.generation,
                "skill_id": self.gate.current_skill,
                "slots": dict(self.gate.current_slots or {}),
                "new_session_id": self.active.session_id,
                "cleanup_mode": "reconnect_sync",
            }
        )
        await self._emit_bound()

    async def handle_control(self, event: dict[str, Any]) -> dict[str, Any]:
        if not event.get("accepted"):
            return {"ok": False, "ignored": True}
        intent = str(event.get("intent") or "")
        if intent == "stop_speech":
            return await self.stop_speech(event)
        if intent == "resume_speech":
            return await self.resume_speech(event)
        if intent in {
            "reset_session",
            "activate_skill",
            "cancel_skill",
            "return_to_chat",
        }:
            return await self.restart(event)
        return {"ok": False, "ignored": True}

    def _ack_base(self, event: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "control.ack",
            "event_id": event.get("event_id"),
            "intent": event.get("intent"),
            "generation": self.gate.generation,
            "dropped_old_text": self.gate.dropped_old_text,
            "dropped_old_audio": self.gate.dropped_old_audio,
        }

    async def stop_speech(
        self, event: dict[str, Any], *, emit_ack: bool = True
    ) -> dict[str, Any]:
        self.gate.stop()
        await self.speaker.block_and_flush()
        await self._clear_playback_state("stop")
        ack = {**self._ack_base(event), "ok": True}
        if emit_ack:
            await self.send_telemetry(ack)
        LOG.info("[CONTROL] STOP event=%s", event.get("event_id"))
        return ack

    async def resume_speech(self, event: dict[str, Any]) -> dict[str, Any]:
        self.gate.resume()
        await self.speaker.resume()
        ack = {**self._ack_base(event), "ok": True}
        await self.send_telemetry(ack)
        LOG.info("[CONTROL] RESUME event=%s", event.get("event_id"))
        return ack

    async def restart(self, event: dict[str, Any]) -> dict[str, Any]:
        async with self._restart_lock:
            requested_skill = str(event.get("skill_id") or self.registry.default_skill)
            requested_slots = dict(event.get("slots") or {})
            if (
                str(event.get("intent")) != "reset_session"
                and requested_skill == self.gate.current_skill
                and same_slots(requested_slots, dict(self.gate.current_slots or {}))
            ):
                ack = {**self._ack_base(event), "ok": True, "no_restart": True}
                await self.send_telemetry(ack)
                return ack

            started = time.monotonic()
            await self.stop_speech(event, emit_ack=False)
            self.gate.restart_in_progress = True
            old_session = self.active
            old_session_id = old_session.session_id if old_session else None
            self.gate.generation += 1
            await self.send_telemetry(
                {
                    "type": "session.state",
                    "phase": "restart_started",
                    "generation": self.gate.generation,
                    "event_id": event.get("event_id"),
                    "old_session_id": old_session_id,
                }
            )
            self.active = None
            try:
                if old_session:
                    await old_session.stop(self.config.cleanup_mode)
                self.audio_queue.clear()
                spec = self._make_spec(
                    requested_skill,
                    requested_slots,
                    str(event.get("system_prompt") or "") or None,
                )
                session = self.session_factory(
                    self.config,
                    spec,
                    self.audio_queue,
                    self.latest_frame,
                    self.gate,
                    self.handle_result,
                )
                self.active = session
                await session.start()
                self.gate.current_skill = requested_skill
                self.gate.current_slots = requested_slots
                self.gate.replacement_ready()
                await self.speaker.resume()
                await self._emit_bound()
                latency_ms = (time.monotonic() - started) * 1000.0
                state = {
                    "type": "session.state",
                    "phase": "restart_complete",
                    "generation": self.gate.generation,
                    "skill_id": requested_skill,
                    "slots": requested_slots,
                    "old_session_id": old_session_id,
                    "new_session_id": session.session_id,
                    "cleanup_mode": self.config.cleanup_mode,
                }
                await self.send_telemetry(state)
                if self.config.play_audio and self.config.session_ready_chime:
                    cue = make_session_ready_chime(
                        amplitude=self.config.session_ready_chime_volume
                    )
                    self.gate.local_cue_mute_until_mono = max(
                        self.gate.local_cue_mute_until_mono,
                        time.monotonic()
                        + cue.size / SAMPLE_RATE_OUT
                        + self.config.session_ready_chime_feedback_guard_s,
                    )
                    await self.speaker.enqueue(cue, self.gate.generation)
                    await self._mark_playback_started("session_ready_chime")
                    LOG.info(
                        "[CUE] session ready generation=%d skill=%s",
                        self.gate.generation,
                        requested_skill,
                    )
                trigger = self.registry.task_trigger(requested_skill, requested_slots)
                trigger_frame = False
                trigger_error = ""
                if trigger:
                    try:
                        trigger_frame = await session.inject_task(trigger)
                        await self.send_telemetry(
                            {
                                "type": "session.state",
                                "phase": "task_trigger_sent",
                                "generation": self.gate.generation,
                                "skill_id": requested_skill,
                                "slots": requested_slots,
                                "session_id": session.session_id,
                                "has_frame": trigger_frame,
                            }
                        )
                    except Exception as exc:
                        trigger_error = str(exc)
                        LOG.warning(
                            "[TASK] one-shot trigger failed skill=%s: %s",
                            requested_skill,
                            exc,
                        )
                ack = {
                    **self._ack_base(event),
                    "ok": True,
                    "restart_latency_ms": latency_ms,
                    "skill_id": requested_skill,
                    "slots": requested_slots,
                    "old_session_id": old_session_id,
                    "new_session_id": session.session_id,
                    "cleanup_mode": self.config.cleanup_mode,
                    "task_trigger_sent": bool(trigger and not trigger_error),
                    "task_trigger_has_frame": trigger_frame,
                    "task_trigger_error": trigger_error,
                }
                await self.send_telemetry(ack)
                LOG.info(
                    "[CONTROL] %s ready generation=%d session=%s latency_ms=%.1f",
                    requested_skill,
                    self.gate.generation,
                    session.session_id,
                    latency_ms,
                )
                return ack
            except Exception as exc:
                if self.active:
                    await self.active.stop(self.config.cleanup_mode)
                self.active = None
                self.gate.restart_in_progress = False
                ack = {**self._ack_base(event), "ok": False, "error": str(exc)}
                await self.send_telemetry(ack)
                LOG.exception("[CONTROL] restart failed")
                return ack

    async def handle_result(
        self, session: GatewayDuplexSession, result: dict[str, Any]
    ) -> None:
        is_listen = bool(result.get("is_listen"))
        text = str(result.get("text") or "")
        audio_b64 = str(result.get("audio_data") or "")
        stale = (
            session is not self.active
            or session.spec.generation != self.gate.generation
        )
        if stale or (self.gate.drop_output_until_listen and not is_listen):
            if text:
                self.gate.dropped_old_text += 1
            if audio_b64:
                self.gate.dropped_old_audio += 1
            return
        if is_listen and self.gate.drop_output_until_listen:
            if not self.gate.speech_hold_active:
                self.gate.drop_output_until_listen = False
                await self.speaker.resume()
        audio_samples = 0
        if audio_b64 and not is_listen:
            try:
                pcm = np.frombuffer(base64.b64decode(audio_b64), dtype=np.float32)
                audio_samples = int(pcm.size)
                await self.speaker.enqueue(pcm, session.spec.generation)
                if self.config.play_audio:
                    await self._mark_playback_started("model")
            except Exception as exc:
                LOG.warning("model audio decode failed: %s", exc)
        await self.send_telemetry(
            {
                "type": "model.state",
                "state": "listen" if is_listen else "speak",
                "text": text,
                "generation": session.spec.generation,
                "session_id": session.session_id,
                "skill_id": session.spec.skill_id,
                "slots": dict(session.spec.slots),
                # llama.cpp-omni marks each decode slice as end_of_turn.  The
                # actual duplex turn boundary is the later __IS_LISTEN__
                # result, so keep the raw marker for diagnostics but aggregate
                # model text until listening resumes.
                "decode_end": bool(result.get("end_of_turn")),
                "end_of_turn": is_listen,
                "message_type": str(result.get("type") or "result"),
                "audio_samples": audio_samples,
                "audio_ms": round(
                    audio_samples * 1000.0 / SAMPLE_RATE_OUT, 1
                ),
            }
        )
        if text:
            LOG.info("[MODEL] listen=%s text=%s", is_listen, text)

    async def close(self) -> None:
        await self._clear_playback_state("close")
        if self.active:
            await self.active.stop(self.config.cleanup_mode)
            self.active = None

    def health(self) -> dict[str, Any]:
        return {
            "generation": self.gate.generation,
            "current_skill": self.gate.current_skill,
            "current_slots": dict(self.gate.current_slots or {}),
            "speech_hold_active": self.gate.speech_hold_active,
            "restart_in_progress": self.gate.restart_in_progress,
            "dropped_old_text": self.gate.dropped_old_text,
            "dropped_old_audio": self.gate.dropped_old_audio,
            "session_id": self.active.session_id if self.active else None,
            "gateway_status": self.active.status if self.active else "disconnected",
            "gateway_error": self.active.last_error if self.active else "",
        }


class HarnessClient:
    def __init__(
        self,
        url: str,
        client_id: str,
        manager: GatewaySessionManager,
        reconnect_s: float,
    ):
        separator = "&" if "?" in url else "?"
        self.url = f"{url}{separator}client_id={client_id}"
        self.manager = manager
        self.reconnect_s = reconnect_s
        self.connected = False
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self._stop = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._control_tasks: set[asyncio.Task[Any]] = set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession() as client:
                    async with client.ws_connect(
                        self.url, heartbeat=30, max_msg_size=0
                    ) as ws:
                        self.ws = ws
                        self.connected = True
                        LOG.info("Harness connected: %s", self.url)
                        async for message in ws:
                            if message.type != aiohttp.WSMsgType.TEXT:
                                if message.type in (
                                    aiohttp.WSMsgType.CLOSED,
                                    aiohttp.WSMsgType.ERROR,
                                ):
                                    break
                                continue
                            payload = json.loads(message.data)
                            message_type = payload.get("type")
                            if message_type == "harness.ready":
                                await self.manager.emit_recovery_sync()
                            elif message_type == "control.intent":
                                task = asyncio.create_task(
                                    self.manager.handle_control(payload)
                                )
                                self._control_tasks.add(task)
                                task.add_done_callback(self._control_tasks.discard)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stop.is_set():
                    LOG.warning("Harness connection failed: %s", exc)
            finally:
                self.connected = False
                self.ws = None
            if not self._stop.is_set():
                await asyncio.sleep(self.reconnect_s)

    async def send(self, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            if self.ws is not None and not self.ws.closed:
                await self.ws.send_json(payload)

    async def send_audio(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        await self.send(
            {
                "type": "audio.mirror",
                "started_at_ms": now_ms() - samples.size * 1000.0 / SAMPLE_RATE_IN,
                "sample_rate": SAMPLE_RATE_IN,
                "audio_b64": float32_to_base64(samples),
            }
        )

    async def send_frame(self, jpeg: bytes, sequence: int, timestamp_ms: float) -> None:
        await self.send(
            {
                "type": "frame.shadow",
                "frame_id": f"rokid_{sequence}_{int(timestamp_ms)}",
                "timestamp_ms": timestamp_ms,
                "jpeg_b64": base64.b64encode(jpeg).decode("ascii"),
            }
        )

    async def close(self) -> None:
        self._stop.set()
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        for task in tuple(self._control_tasks):
            task.cancel()
        await asyncio.gather(*self._control_tasks, return_exceptions=True)


@dataclass(slots=True)
class RuntimeStats:
    started_mono: float = field(default_factory=time.monotonic)
    audio_packets: int = 0
    audio_bytes: int = 0
    audio_clients: int = 0
    image_count: int = 0
    image_bytes: int = 0
    last_audio_ms: float = 0.0
    audio_rms: float = 0.0
    audio_peak: float = 0.0
    non_silent_audio_packets: int = 0


class PhaseBRokidRuntime:
    """Rokid sensor ingress + frozen Harness control + Gateway session adapter."""

    def __init__(
        self,
        config: RokidRuntimeConfig,
        *,
        speaker: SpeakerSink | None = None,
        session_factory: SessionFactory = GatewayDuplexSession,
    ):
        self.config = config
        self.registry = SkillRegistry(config.skills_config)
        self.audio_queue = DropOldestAudioQueue(config.audio_queue_packets)
        self.audio_mirror = AudioMirrorChunker()
        self.latest_frame = LatestFrame()
        self.stats = RuntimeStats(started_mono=time.monotonic())
        self.speaker = speaker or (PCSpeaker() if config.play_audio else NullSpeaker())
        self.manager = GatewaySessionManager(
            config,
            self.registry,
            self.audio_queue,
            self.latest_frame,
            self.speaker,
            session_factory=session_factory,
        )
        self.harness = HarnessClient(
            config.harness_url,
            config.harness_client_id,
            self.manager,
            config.reconnect_s,
        )
        self.manager.harness = self.harness
        self._tasks: list[asyncio.Task[Any]] = []
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._no_input_warning_emitted = False

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": "B",
            "device": "rokid",
            "uptime_s": round(time.monotonic() - self.stats.started_mono, 3),
            "harness_connected": self.harness.connected,
            "audio_packets_in": self.stats.audio_packets,
            "audio_bytes_in": self.stats.audio_bytes,
            "audio_clients": self.stats.audio_clients,
            "latest_audio_age_ms": (
                round(now_ms() - self.stats.last_audio_ms, 1)
                if self.stats.last_audio_ms
                else None
            ),
            "audio_queue_size": self.audio_queue.size,
            "dropped_audio_packets": self.audio_queue.dropped_packets,
            "audio_rms": round(self.stats.audio_rms, 6),
            "audio_peak": round(self.stats.audio_peak, 6),
            "input_gain": self.config.input_gain,
            "effective_audio_rms": round(
                min(1.0, self.stats.audio_rms * self.config.input_gain), 6
            ),
            "non_silent_audio_packets": self.stats.non_silent_audio_packets,
            "image_count": self.stats.image_count,
            "device_input_ready": bool(
                self.stats.audio_packets or self.stats.image_count
            ),
            "latest_image_age_ms": (
                round(now_ms() - self.latest_frame.timestamp_ms, 1)
                if self.latest_frame.timestamp_ms
                else None
            ),
            **self.manager.health(),
        }

    async def _initial_session_loop(self) -> None:
        while True:
            try:
                await self.manager.start_initial()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("initial Gateway session failed: %s", exc)
                await asyncio.sleep(self.config.reconnect_s)

    async def _stats_loop(self) -> None:
        previous_audio = 0
        previous_image = 0
        while True:
            await asyncio.sleep(5.0)
            if (
                not self._no_input_warning_emitted
                and time.monotonic() - self.stats.started_mono >= 10.0
                and self.stats.audio_packets == 0
                and self.stats.image_count == 0
            ):
                self._no_input_warning_emitted = True
                LOG.warning(
                    "[ROKID][NO_INPUT] no PCM/JPEG has reached this process. "
                    "After the PC runtime is listening, restart OpenGlass "
                    "Sensor Mode on the glasses (STOP -> RUN) and verify that "
                    "the APK targets the PC WLAN address on port %d",
                    self.config.port,
                )
            LOG.info(
                "[STATS] audio=%.1fpps images=%d(+%d) audio_clients=%d "
                "queue=%d drops=%d rms=%.4f peak=%.4f harness=%s gateway=%s "
                "skill=%s generation=%d",
                (self.stats.audio_packets - previous_audio) / 5.0,
                self.stats.image_count,
                self.stats.image_count - previous_image,
                self.stats.audio_clients,
                self.audio_queue.size,
                self.audio_queue.dropped_packets,
                self.stats.audio_rms,
                self.stats.audio_peak,
                self.harness.connected,
                self.manager.health()["gateway_status"],
                self.manager.gate.current_skill,
                self.manager.gate.generation,
            )
            previous_audio = self.stats.audio_packets
            previous_image = self.stats.image_count

    async def start(self) -> None:
        await self.speaker.start()
        self._tasks = [
            asyncio.create_task(self.harness.run()),
            asyncio.create_task(self._initial_session_loop()),
            asyncio.create_task(self._stats_loop()),
        ]

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            LOG.info("Rokid runtime shutdown started")

            async def bounded(label: str, awaitable: Awaitable[None]) -> None:
                try:
                    await asyncio.wait_for(
                        awaitable,
                        timeout=max(0.5, self.config.close_timeout_s),
                    )
                except asyncio.TimeoutError:
                    LOG.warning(
                        "Rokid shutdown step timed out: %s (continuing)", label
                    )
                except Exception as exc:
                    LOG.warning(
                        "Rokid shutdown step failed: %s: %s", label, exc
                    )

            await bounded("harness", self.harness.close())
            for task in self._tasks:
                task.cancel()
            if self._tasks:
                await bounded(
                    "background_tasks",
                    asyncio.gather(*self._tasks, return_exceptions=True),
                )
            self._tasks.clear()
            await bounded("gateway_session", self.manager.close())
            await bounded("pc_speaker", self.speaker.close())
            self.audio_queue.clear()
            self.audio_mirror.clear()
            self._closed = True
            LOG.info("Rokid runtime shutdown complete; port may be reused")

    def create_app(self) -> web.Application:
        app = web.Application(client_max_size=8 * 1024 * 1024)
        app["runtime"] = self

        async def health(_request: web.Request) -> web.Response:
            return web.json_response(self.health())

        async def capture(_request: web.Request) -> web.Response:
            if not self.latest_frame.jpeg:
                return web.Response(status=404, text="no image")
            return web.Response(body=self.latest_frame.jpeg, content_type="image/jpeg")

        async def rokid_image(request: web.Request) -> web.Response:
            raw = await request.read()
            if not (raw.startswith(b"\xff\xd8") and raw.endswith(b"\xff\xd9")):
                return web.Response(status=400, text="expected JPEG bytes")
            try:
                jpeg = await asyncio.to_thread(
                    rotate_jpeg_clockwise,
                    raw,
                    self.config.image_rotate_cw,
                    self.config.image_jpeg_quality,
                )
            except Exception as exc:
                return web.Response(status=400, text=str(exc))
            timestamp = now_ms()
            self.latest_frame.set(jpeg, timestamp)
            self.stats.image_count += 1
            self.stats.image_bytes += len(raw)
            if self.stats.image_count == 1:
                LOG.info("[ROKID] first JPEG received from %s", request.remote)
            asyncio.create_task(
                self.harness.send_frame(
                    jpeg, self.latest_frame.sequence, timestamp
                )
            )
            return web.json_response(
                {"ok": True, "image_count": self.stats.image_count, "bytes": len(raw)}
            )

        async def rokid_audio(request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
            await ws.prepare(request)
            self.stats.audio_clients += 1
            LOG.info("[ROKID] audio connected from %s", request.remote)
            try:
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        raw = bytes(message.data)
                        if len(raw) < PCM16_WIDTH:
                            continue
                        if len(raw) % PCM16_WIDTH:
                            raw = raw[:-1]
                        self.stats.audio_packets += 1
                        self.stats.audio_bytes += len(raw)
                        self.stats.last_audio_ms = now_ms()
                        if self.stats.audio_packets == 1:
                            LOG.info("[ROKID] first PCM packet received")
                        samples = pcm16le_to_float32(raw)
                        packet_rms = float(np.sqrt(np.mean(np.square(samples))))
                        packet_peak = float(np.max(np.abs(samples)))
                        self.stats.audio_rms = (
                            0.8 * self.stats.audio_rms + 0.2 * packet_rms
                        )
                        self.stats.audio_peak = packet_peak
                        if packet_rms >= 0.01:
                            self.stats.non_silent_audio_packets += 1
                        if time.monotonic() >= self.manager.gate.local_cue_mute_until_mono:
                            amplified = apply_pcm16_gain(raw, self.config.input_gain)
                            self.audio_queue.put_nowait(amplified)
                            for frame in self.audio_mirror.feed(amplified):
                                await self.harness.send_audio(frame)
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        break
            finally:
                self.stats.audio_clients = max(0, self.stats.audio_clients - 1)
                LOG.info("[ROKID] audio disconnected from %s", request.remote)
            return ws

        async def on_startup(_app: web.Application) -> None:
            await self.start()

        async def on_cleanup(_app: web.Application) -> None:
            await self.close()

        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        app.router.add_get("/capture", capture)
        app.router.add_post("/rokid/image", rokid_image)
        app.router.add_get("/rokid/audio", rokid_audio)
        app.on_startup.append(on_startup)
        app.on_cleanup.append(on_cleanup)
        return app


def default_skills_config() -> str:
    return str(
        Path(__file__).resolve().parents[1] / "config" / "skills.example.yaml"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rokid Phase B adapter: sensors + Harness + MiniCPM Gateway"
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18_080)
    parser.add_argument("--gateway", default="localhost:8040")
    parser.add_argument("--gateway-tls", action="store_true", default=False)
    parser.add_argument(
        "--no-gateway-tls", dest="gateway_tls", action="store_false"
    )
    parser.add_argument(
        "--harness-url", default="ws://127.0.0.1:8021/ws/control"
    )
    parser.add_argument("--client-id", default="rokid-phase-b")
    parser.add_argument("--skills-config", default=default_skills_config())
    parser.add_argument("--cleanup-mode", choices=("light", "full"), default="light")
    parser.add_argument("--image-rotate-cw", type=int, default=270)
    parser.add_argument("--image-jpeg-quality", type=int, default=95)
    parser.add_argument("--audio-queue-packets", type=int, default=96)
    parser.add_argument(
        "--input-gain",
        type=float,
        default=12.0,
        help="Rokid PCM gain before Harness/Gateway; clipped to PCM16",
    )
    parser.add_argument("--chunk-ms", type=int, default=1_000)
    parser.add_argument("--force-listen-count", type=int, default=3)
    parser.add_argument("--no-play", action="store_true")
    parser.add_argument(
        "--no-session-ready-chime",
        action="store_true",
        help="disable the local restart_complete notification cue",
    )
    parser.add_argument(
        "--session-ready-chime-volume",
        type=float,
        default=0.32,
        help="restart cue amplitude in [0, 1] (default: 0.32)",
    )
    parser.add_argument(
        "--playback-echo-tail-s",
        type=float,
        default=0.80,
        help="keep EchoGuard active after the PC speaker queue drains",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 0.0 <= args.session_ready_chime_volume <= 1.0:
        raise SystemExit("--session-ready-chime-volume must be between 0 and 1")
    if args.playback_echo_tail_s < 0.0:
        raise SystemExit("--playback-echo-tail-s must be non-negative")
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    config = RokidRuntimeConfig(
        host=args.host,
        port=args.port,
        gateway=args.gateway,
        gateway_tls=args.gateway_tls,
        harness_url=args.harness_url,
        harness_client_id=args.client_id,
        skills_config=args.skills_config,
        cleanup_mode=args.cleanup_mode,
        chunk_ms=args.chunk_ms,
        force_listen_count=args.force_listen_count,
        audio_queue_packets=args.audio_queue_packets,
        input_gain=args.input_gain,
        image_rotate_cw=args.image_rotate_cw,
        image_jpeg_quality=args.image_jpeg_quality,
        play_audio=not args.no_play,
        session_ready_chime=not args.no_session_ready_chime,
        session_ready_chime_volume=args.session_ready_chime_volume,
        playback_echo_tail_s=args.playback_echo_tail_s,
    )
    runtime = PhaseBRokidRuntime(config)
    watchdog = CtrlCExitWatchdog()
    shutdown_signals = [signal.SIGINT]
    if hasattr(signal, "SIGBREAK"):
        shutdown_signals.append(signal.SIGBREAK)
    previous_handlers = {
        signum: signal.getsignal(signum) for signum in shutdown_signals
    }

    def handle_console_shutdown(signum: int, frame: Any) -> None:
        watchdog.arm()
        previous_handler = previous_handlers[signum]
        if callable(previous_handler):
            previous_handler(signum, frame)
        else:
            raise KeyboardInterrupt

    for signum in shutdown_signals:
        signal.signal(signum, handle_console_shutdown)
    LOG.info("Rokid Phase B input: http://%s:%d", config.host, config.port)
    LOG.info("Harness: %s", config.harness_url)
    LOG.info("Gateway: %s://%s", "wss" if config.gateway_tls else "ws", config.gateway)
    try:
        web.run_app(
            runtime.create_app(),
            host=config.host,
            port=config.port,
            access_log=None,
            shutdown_timeout=max(0.5, config.close_timeout_s),
            handler_cancellation=True,
        )
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10048:
            LOG.error(
                "Port %d is already occupied by another process. A runtime "
                "started with this patched version exits within %.0fs after "
                "Ctrl+C.",
                config.port,
                CTRL_C_HARD_EXIT_S,
            )
            raise SystemExit(2) from None
        raise
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)
        if watchdog.armed:
            LOG.info("Graceful shutdown returned; exiting Rokid process")


if __name__ == "__main__":
    main()
