from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import uvicorn
import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .asr.base import ASREngine
from .asr.energy_vad import EnergyVAD, UtteranceAudio
from .asr.funasr_engine import FunASREngine
from .cv.base import CVObservation, FrameEnvelope
from .cv.pipeline import CVPipeline
from .cv.registry import CVProviderRegistry, build_provider_registry
from .echo_guard import EchoGuard
from .model_log import ModelTurnAccumulator
from .registry import SkillRegistry
from .router import RuleIntentRouter
from .schemas import ASREvent, ControlEvent, ControlIntent
from .state_machine import HarnessController
from .telemetry import TelemetryWriter


def now_ms() -> float:
    return time.time() * 1000.0


def _decode_float32(value: str) -> np.ndarray:
    raw = base64.b64decode(value, validate=True)
    if len(raw) % 4:
        raise ValueError("float32 payload length is not divisible by four")
    return np.frombuffer(raw, dtype="<f4").copy()


@dataclass(slots=True)
class ClientRuntime:
    client_id: str
    registry: SkillRegistry
    router: RuleIntentRouter
    engine: ASREngine
    telemetry: TelemetryWriter
    vad: EnergyVAD
    echo: EchoGuard
    controller: HarnessController
    allow_test_injection: bool
    cv: CVPipeline
    event_counter: int = 0
    asr_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    outbound: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    last_frame_ms: float = 0.0
    model_turns: ModelTurnAccumulator = field(default_factory=ModelTurnAccumulator)
    model_speaking: bool = False
    playback_active: bool = False

    def next_event_id(self) -> int:
        self.event_counter += 1
        return self.event_counter

    def refresh_echo_speaking(self) -> None:
        self.echo.set_ai_speaking(self.model_speaking or self.playback_active)

    def record_model_turn(self, turn: dict[str, Any]) -> None:
        self.telemetry.write("model", turn)
        self.telemetry.append_model_transcript(turn)
        print(
            "[AssistiveHarness][MODEL] "
            f"turn={turn.get('turn_index')} role={turn.get('role')} "
            f"generation={turn.get('generation')} skill={turn.get('skill_id')} "
            f"audio_ms={turn.get('audio_ms')} text={turn.get('text')!r}",
            flush=True,
        )

    async def recognize(
        self, utterance: UtteranceAudio
    ) -> dict[str, Any] | list[dict[str, Any]]:
        async with self.asr_lock:
            started = now_ms()
            result = await asyncio.to_thread(
                self.engine.transcribe, utterance.audio, self.vad.sample_rate
            )
            return await self.route_transcript(
                result.text,
                confidence=result.confidence,
                model=result.model,
                device=result.device,
                started_at_ms=utterance.started_at_ms,
                ended_at_ms=utterance.ended_at_ms,
                final_at_ms=now_ms(),
                inference_started_at_ms=started,
            )

    def recognize_in_background(self, utterance: UtteranceAudio) -> None:
        """Keep receiving microphone frames while a completed utterance is decoded."""

        async def run() -> None:
            try:
                await self.outbound.put(await self.recognize(utterance))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.outbound.put(
                    {
                        "type": "error",
                        "code": "asr_failed",
                        "message": str(exc),
                    }
                )

        task = asyncio.create_task(run())
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def route_transcript(
        self,
        text: str,
        *,
        confidence: float,
        model: str,
        device: str,
        started_at_ms: float | None = None,
        ended_at_ms: float | None = None,
        final_at_ms: float | None = None,
        inference_started_at_ms: float | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        finished = final_at_ms if final_at_ms is not None else now_ms()
        asr_id = self.next_event_id()
        asr_event = ASREvent(
            event_id=asr_id,
            utterance=text.strip(),
            started_at_ms=started_at_ms if started_at_ms is not None else finished,
            ended_at_ms=ended_at_ms if ended_at_ms is not None else finished,
            final_at_ms=finished,
            model=model,
            device=device,
        )
        self.telemetry.write("asr", asr_event.to_dict())
        transcript_payload: dict[str, Any] = {
            "type": "asr.transcript",
            "asr_event_id": asr_id,
            "utterance": asr_event.utterance,
            "confidence": confidence,
            "final_at_ms": finished,
        }
        if inference_started_at_ms is not None:
            self.telemetry.metric("asr_inference_ms", finished - inference_started_at_ms, asr_id)

        route = self.router.route(asr_event.utterance)
        echo = self.echo.evaluate(asr_event.utterance, route.intent, at_ms=finished)
        print(
            "[AssistiveHarness][ASR] "
            f"text={asr_event.utterance!r} intent={route.intent.value} "
            f"echo={'allow' if echo.allow else 'drop'} reason={echo.reason}",
            flush=True,
        )
        self.telemetry.write(
            "echo",
            {
                "asr_event_id": asr_id,
                "allow": echo.allow,
                "reason": echo.reason,
                "similarity": echo.similarity,
            },
        )
        if not echo.allow or route.intent is ControlIntent.NONE:
            transcript_payload["suppressed"] = not echo.allow
            transcript_payload["reason"] = echo.reason if not echo.allow else route.reason
            return transcript_payload

        control_id = self.next_event_id()
        event = self.router.make_event(
            route,
            event_id=control_id,
            asr_event_id=asr_id,
            utterance=asr_event.utterance,
            created_at_ms=finished,
        )
        decision = self.controller.process(event, now_ms=finished)
        print(
            "[AssistiveHarness][CONTROL] "
            f"intent={route.intent.value} accepted={decision.accepted} "
            f"action={decision.action} event={control_id}",
            flush=True,
        )
        payload = {
            **decision.payload,
            "accepted": decision.accepted,
            "action": decision.action,
            "decision_reason": decision.reason,
            "client_id": self.client_id,
        }
        self.telemetry.write("control", payload)
        if route.intent in {
            ControlIntent.RESET_SESSION,
            ControlIntent.CANCEL_SKILL,
            ControlIntent.RETURN_TO_CHAT,
            ControlIntent.ACTIVATE_SKILL,
        }:
            self.telemetry.write(
                "skill",
                {
                    "control_event_id": control_id,
                    "intent": route.intent.value,
                    "skill_id": event.skill_id,
                    "slots": event.slots,
                    "prompt_path": event.prompt_path,
                    "prompt_sha256": event.prompt_sha256,
                    "accepted": decision.accepted,
                    "action": decision.action,
                },
            )
        if decision.accepted:
            return [transcript_payload, payload]
        transcript_payload["suppressed"] = True
        transcript_payload["reason"] = decision.reason
        return transcript_payload


class AssistiveHarnessService:
    def __init__(
        self,
        config_path: str | Path,
        *,
        enabled: bool,
        model_path: str | None = None,
        allow_test_injection: bool = False,
        engine: ASREngine | None = None,
        cv_providers: CVProviderRegistry | None = None,
    ):
        self.config_path = Path(config_path).resolve()
        self.config = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        self.registry = SkillRegistry(self.config_path)
        self.cv_config = dict(self.config.get("cv") or {})
        self.cv_providers = cv_providers or build_provider_registry(
            self.config, config_dir=self.config_path.parent
        )
        self.enabled = bool(enabled)
        self.allow_test_injection = bool(allow_test_injection)
        asr_cfg = dict(self.config.get("asr") or {})
        resolved_model_path = model_path or str(asr_cfg.get("model_path") or "")
        if engine is not None:
            self.engine = engine
        elif self.enabled:
            if not resolved_model_path:
                raise ValueError("--model-path is required when the Harness is enabled")
            self.engine = FunASREngine(
                resolved_model_path,
                device=str(asr_cfg.get("device") or "cpu"),
            )
        else:
            self.engine = None
        run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        telemetry_root = Path(__file__).resolve().parent / "runs"
        self.telemetry = TelemetryWriter(telemetry_root, run_id, self.config)
        self.clients: dict[str, ClientRuntime] = {}
        self.started_at_ms = now_ms()

    def make_runtime(self, client_id: str) -> ClientRuntime:
        if self.engine is None:
            raise RuntimeError("Harness is disabled")
        asr_cfg = dict(self.config.get("asr") or {})
        echo_cfg = dict(self.config.get("echo_guard") or {})
        return ClientRuntime(
            client_id=client_id,
            registry=self.registry,
            router=RuleIntentRouter(self.registry),
            engine=self.engine,
            telemetry=self.telemetry,
            vad=EnergyVAD(
                rms_threshold=float(asr_cfg.get("rms_threshold", 0.012)),
                min_speech_ms=int(asr_cfg.get("min_speech_ms", 180)),
                end_silence_ms=int(asr_cfg.get("end_silence_ms", 450)),
                max_utterance_ms=int(asr_cfg.get("max_utterance_ms", 8000)),
                preroll_ms=int(asr_cfg.get("preroll_ms", 200)),
            ),
            echo=EchoGuard(
                window_ms=int(echo_cfg.get("window_ms", 20000)),
                similarity_threshold=float(echo_cfg.get("similarity_threshold", 0.86)),
            ),
            controller=HarnessController(self.registry),
            allow_test_injection=self.allow_test_injection,
            cv=CVPipeline(
                self.cv_providers,
                on_observation=lambda observation: self.telemetry.write(
                    "cv", observation.to_dict()
                ),
                on_metric=lambda name, value: self.telemetry.metric(name, value),
                queue_size=int(self.cv_config.get("queue_size", 1)),
                inference_timeout_ms=float(
                    self.cv_config.get("inference_timeout_ms", 2000)
                ),
                worker_name=f"assistive-cv-{client_id[:8]}",
            ),
        )

    def create_app(self) -> FastAPI:
        app = FastAPI(title="Assistive Voice Skill Harness", version="0.1.0")

        @app.get("/health")
        async def health() -> JSONResponse:
            return JSONResponse(
                {
                    "ok": True,
                    "enabled": self.enabled,
                    "clients": len(self.clients),
                    "uptime_ms": now_ms() - self.started_at_ms,
                    "asr_loaded": bool(getattr(self.engine, "loaded", self.engine is not None)),
                    "config": self.config_path.name,
                    "cv_providers": sorted(self.cv_providers.providers),
                }
            )

        @app.get("/skills")
        async def skills() -> JSONResponse:
            return JSONResponse(
                {
                    "ok": True,
                    "default_skill": self.registry.default_skill,
                    "config_path": str(self.config_path),
                    "skills": {
                        skill_id: {
                            "enabled": self.registry.is_enabled(skill_id),
                            "description": str(spec.get("description") or ""),
                            "prompt_path": str(self.registry.prompt_path(skill_id)),
                            "requires_session_restart": bool(
                                spec.get("requires_session_restart", True)
                            ),
                            "cv_mode": str(spec.get("cv_mode") or "disabled"),
                            "cv_provider": str(spec.get("cv_provider") or "noop"),
                        }
                        for skill_id, spec in self.registry.skills.items()
                    },
                }
            )

        @app.on_event("shutdown")
        async def write_shutdown_summary() -> None:
            self.telemetry.write_summary(
                {
                    "enabled": self.enabled,
                    "uptime_ms": now_ms() - self.started_at_ms,
                    "connected_clients_at_shutdown": len(self.clients),
                    "asr_loaded": bool(getattr(self.engine, "loaded", self.engine is not None)),
                }
            )

        @app.websocket("/ws/control")
        async def control_socket(websocket: WebSocket) -> None:
            if not self.enabled:
                await websocket.close(code=1013, reason="Harness disabled")
                return
            await websocket.accept()
            client_id = websocket.query_params.get("client_id") or uuid.uuid4().hex
            runtime = self.make_runtime(client_id)
            self.clients[client_id] = runtime

            async def send_outbound() -> None:
                while True:
                    response = await runtime.outbound.get()
                    if isinstance(response, list):
                        for item in response:
                            await websocket.send_json(item)
                    elif response is not None:
                        await websocket.send_json(response)

            sender = asyncio.create_task(send_outbound())
            await runtime.outbound.put(
                {
                    "type": "harness.ready",
                    "client_id": client_id,
                    "state": runtime.controller.state.to_dict(),
                }
            )
            try:
                while True:
                    message = await websocket.receive_json()
                    response = await self._handle_message(runtime, message)
                    if response is not None:
                        await runtime.outbound.put(response)
            except WebSocketDisconnect:
                runtime.controller.mark_disconnected()
            finally:
                pending_turn = runtime.model_turns.flush()
                if pending_turn is not None:
                    runtime.record_model_turn(pending_turn)
                sender.cancel()
                for task in tuple(runtime.background_tasks):
                    task.cancel()
                await runtime.cv.close()
                await asyncio.gather(sender, *runtime.background_tasks, return_exceptions=True)
                self.clients.pop(client_id, None)

        return app

    async def _handle_message(
        self, runtime: ClientRuntime, message: dict[str, Any]
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        message_type = str(message.get("type") or "")
        if message_type == "ping":
            return {"type": "pong", "at_ms": now_ms()}
        if message_type == "audio.mirror":
            audio = _decode_float32(str(message.get("audio_b64") or ""))
            utterance = runtime.vad.feed(audio, float(message.get("started_at_ms") or now_ms()))
            if utterance is None:
                return None
            runtime.recognize_in_background(utterance)
            return None
        if message_type == "asr.inject":
            if not runtime.allow_test_injection:
                return {"type": "error", "code": "test_injection_disabled"}
            return await runtime.route_transcript(
                str(message.get("text") or ""),
                confidence=1.0,
                model="injected-test-only",
                device="none",
            )
        if message_type in ("funnel.stop", "funnel.resume"):
            # 漏斗 reject/恢复:程序直接触发,不经 ASR/router 文字匹配。
            # 复用 controller.process + STOP_SPEECH/RESUME_SPEECH,和真人「停一下/恢复对话」
            # 走完全相同的下游(设备端收到 control.intent 执行 stop_speech/resume_speech)。
            # 归口 8021:telemetry 统一记录,标 source=funnel 以区分真人还是漏斗触发。
            is_stop = message_type == "funnel.stop"
            intent = ControlIntent.STOP_SPEECH if is_stop else ControlIntent.RESUME_SPEECH
            event = ControlEvent(
                event_id=runtime.next_event_id(),
                intent=intent,
                utterance="[%s]" % message_type,
                confidence=1.0,
                asr_event_id=runtime.next_event_id(),
                created_at_ms=now_ms(),
                reason=str(message.get("reason") or "funnel"),
            )
            decision = runtime.controller.process(event, now_ms=event.created_at_ms)
            payload = {
                **decision.payload,
                "accepted": decision.accepted,
                "action": decision.action,
                "decision_reason": decision.reason,
                "source": "funnel",
                "client_id": runtime.client_id,
            }
            runtime.telemetry.write("control", payload)
            print(
                "[AssistiveHarness][FUNNEL] "
                "type=%s accepted=%s action=%s reason=%s"
                % (message_type, decision.accepted, decision.action, event.reason),
                flush=True,
            )
            return payload if decision.accepted else None
        if message_type == "model.state":
            event = dict(message)
            runtime.telemetry.write("model", event)
            speaking = str(message.get("state") or "") == "speak"
            runtime.model_speaking = speaking
            runtime.refresh_echo_speaking()
            text = str(message.get("text") or "")
            if speaking and text:
                runtime.echo.note_model_text(text)
            for turn in runtime.model_turns.feed(event):
                runtime.record_model_turn(turn)
            return None
        if message_type == "playback.state":
            event = dict(message)
            runtime.telemetry.write("model", event)
            runtime.playback_active = bool(message.get("active"))
            runtime.refresh_echo_speaking()
            return None
        if message_type == "session.state":
            event = dict(message)
            runtime.telemetry.write("session", event)
            phase = str(message.get("phase") or "")
            if phase == "listen":
                runtime.controller.mark_listen_fence()
            elif phase == "restart_complete":
                runtime.controller.mark_restart_complete(
                    str(message.get("skill_id") or runtime.registry.default_skill),
                    dict(message.get("slots") or {}),
                    int(message.get("generation") or 0),
                )
            return None
        if message_type == "control.ack":
            if bool(message.get("ok")) and str(message.get("intent") or "") == "stop_speech":
                # Browser STOP has already flushed/blocked playback. Do not
                # leave EchoGuard in a stale speaking state while the user
                # immediately issues the next explicit command.
                runtime.model_speaking = False
                runtime.playback_active = False
                runtime.refresh_echo_speaking()
            runtime.telemetry.write("session", dict(message))
            print(
                "[AssistiveHarness][ACK] "
                f"intent={message.get('intent')} ok={message.get('ok')} "
                f"generation={message.get('generation')} "
                f"session={message.get('old_session_id') or '-'}"
                f"->{message.get('new_session_id') or '-'} "
                f"cleanup={message.get('cleanup_mode') or '-'} "
                f"restart_ms={message.get('restart_latency_ms') or '-'} "
                f"stale_text={message.get('dropped_old_text') or 0} "
                f"stale_audio={message.get('dropped_old_audio') or 0}",
                flush=True,
            )
            metric = message.get("restart_latency_ms")
            if isinstance(metric, (int, float)):
                runtime.telemetry.metric(
                    "restart_latency_ms", float(metric), int(message.get("event_id") or 0)
                )
            return None
        if message_type == "frame.shadow":
            at_ms = float(message.get("timestamp_ms") or now_ms())
            max_fps = max(0.0, float(self.cv_config.get("max_fps", 1)))
            if max_fps <= 0:
                return None
            if at_ms - runtime.last_frame_ms < 1000.0 / max_fps:
                return None
            skill_id = runtime.controller.state.current_skill
            skill_spec = runtime.registry.get(skill_id)
            mode = str(skill_spec.get("cv_mode") or self.cv_config.get("mode") or "disabled")
            if mode == "disabled":
                return None
            provider_id = str(skill_spec.get("cv_provider") or "noop")
            frame_b64 = str(message.get("jpeg_b64") or "")
            frame_id = str(message.get("frame_id") or uuid.uuid4().hex)
            try:
                frame = base64.b64decode(frame_b64, validate=True) if frame_b64 else None
            except Exception as exc:
                runtime.telemetry.write(
                    "cv",
                    CVObservation(
                        frame_id=frame_id,
                        timestamp_ms=at_ms,
                        skill_id=skill_id,
                        provider=f"{mode}:{provider_id}",
                        values={"status": "error"},
                        error=f"InvalidFramePayload: {exc}",
                    ).to_dict(),
                )
                return None
            runtime.last_frame_ms = at_ms
            runtime.cv.submit(
                FrameEnvelope(
                    frame=frame,
                    frame_id=frame_id,
                    timestamp_ms=at_ms,
                    skill_id=skill_id,
                    slots=dict(runtime.controller.state.current_slots),
                    mode=mode,
                    provider_id=provider_id,
                )
            )
            return None
        return {"type": "error", "code": "unknown_message", "message_type": message_type}


def build_parser() -> argparse.ArgumentParser:
    default_config = Path(__file__).resolve().parent / "config" / "skills.example.yaml"
    parser = argparse.ArgumentParser(description="Optional local assistive voice-skill Harness")
    parser.add_argument("--config", default=str(default_config))
    parser.add_argument("--enabled", action="store_true", help="explicit opt-in; default is off")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8021)
    parser.add_argument("--model-path", default=None)
    parser.add_argument("--allow-test-injection", action="store_true")
    parser.add_argument("--certfile", default=None)
    parser.add_argument("--keyfile", default=None)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    service = AssistiveHarnessService(
        args.config,
        enabled=args.enabled,
        model_path=args.model_path,
        allow_test_injection=args.allow_test_injection,
    )
    if isinstance(service.engine, FunASREngine):
        warmup_started = time.perf_counter()
        print("[AssistiveHarness] loading and warming FunASR before accepting clients...")
        service.engine.warm_up()
        print(
            "[AssistiveHarness] FunASR ready "
            f"({time.perf_counter() - warmup_started:.2f}s)"
        )
    uvicorn.run(
        service.create_app(),
        host=args.host,
        port=args.port,
        ssl_certfile=args.certfile,
        ssl_keyfile=args.keyfile,
    )


if __name__ == "__main__":
    main()
