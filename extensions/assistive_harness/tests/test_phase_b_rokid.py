from __future__ import annotations

import asyncio
import base64
import unittest
from pathlib import Path
from typing import Any

import numpy as np

from extensions.assistive_harness.phase_b.rokid_runtime import (
    AudioMirrorChunker,
    DropOldestAudioQueue,
    GatewaySessionManager,
    LatestFrame,
    PCSpeaker,
    PhaseBRokidRuntime,
    RokidRuntimeConfig,
    SessionSpec,
    apply_pcm16_gain,
    make_session_ready_chime,
    pcm16le_to_float32,
)
from extensions.assistive_harness.registry import SkillRegistry


CONFIG = Path(__file__).resolve().parents[1] / "config" / "skills.example.yaml"


class FakeSpeaker:
    def __init__(self) -> None:
        self.blocked = False
        self.flush_count = 0
        self.resume_count = 0
        self.enqueued: list[tuple[np.ndarray, int]] = []

    async def start(self) -> None:
        pass

    async def enqueue(self, samples: np.ndarray, generation: int) -> None:
        if not self.blocked:
            self.enqueued.append((samples.copy(), generation))

    async def block_and_flush(self) -> None:
        self.blocked = True
        self.flush_count += 1

    async def resume(self) -> None:
        self.blocked = False
        self.resume_count += 1

    async def close(self) -> None:
        pass

    def pending_ms(self) -> float:
        return 0.0


class FakeTelemetry:
    connected = True

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send(self, payload: dict[str, Any]) -> None:
        self.messages.append(dict(payload))


class FakeSession:
    next_id = 0

    def __init__(
        self,
        _config: RokidRuntimeConfig,
        spec: SessionSpec,
        _audio_queue: DropOldestAudioQueue,
        _latest_frame: LatestFrame,
        _gate: object,
        on_result: object,
    ) -> None:
        type(self).next_id += 1
        self.spec = spec
        self.on_result = on_result
        self.session_id = f"fake-{type(self).next_id}"
        self.status = "created"
        self.last_error = ""
        self.started = False
        self.stopped_with: str | None = None
        self.injected_tasks: list[str] = []

    async def start(self) -> None:
        self.started = True
        self.status = "running"

    async def stop(self, cleanup_mode: str) -> None:
        self.stopped_with = cleanup_mode
        self.status = "stopped"

    async def inject_task(self, text: str) -> bool:
        self.injected_tasks.append(text)
        return True


class RokidAudioBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_health_marks_device_input_not_ready_before_first_packet(self) -> None:
        runtime = PhaseBRokidRuntime(
            RokidRuntimeConfig(skills_config=str(CONFIG), play_audio=False),
            speaker=FakeSpeaker(),
            session_factory=FakeSession,
        )
        self.assertFalse(runtime.health()["device_input_ready"])
        runtime.stats.audio_packets = 1
        self.assertTrue(runtime.health()["device_input_ready"])

    def test_pcm16_conversion_and_mirror_chunking(self) -> None:
        raw = np.array([-32768, 0, 32767], dtype="<i2").tobytes()
        converted = pcm16le_to_float32(raw)
        np.testing.assert_allclose(
            converted,
            np.array([-1.0, 0.0, 32767 / 32768], dtype=np.float32),
        )

        first = np.arange(1_000, dtype="<i2").tobytes()
        second = np.arange(1_000, 2_000, dtype="<i2").tobytes()
        chunker = AudioMirrorChunker(samples_per_frame=1_600)
        self.assertEqual(chunker.feed(first), [])
        frames = chunker.feed(second)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].shape, (1_600,))

    def test_input_gain_saturates_without_pcm_wraparound(self) -> None:
        raw = np.array([-4_000, 1_000, 4_000], dtype="<i2").tobytes()
        gained = np.frombuffer(apply_pcm16_gain(raw, 12.0), dtype="<i2")
        np.testing.assert_array_equal(gained, np.array([-32768, 12000, 32767]))

    def test_session_ready_chime_is_short_bounded_float_audio(self) -> None:
        chime = make_session_ready_chime()
        self.assertEqual(chime.dtype, np.float32)
        self.assertGreater(chime.size, 24_000 * 0.15)
        self.assertLess(chime.size, 24_000 * 0.25)
        self.assertLessEqual(float(np.max(np.abs(chime))), 0.321)
        self.assertGreater(float(np.max(np.abs(chime))), 0.30)

    async def test_drop_oldest_queue_preserves_newest_packets(self) -> None:
        queue = DropOldestAudioQueue(max_packets=2)
        queue.put_nowait(b"old")
        queue.put_nowait(b"middle")
        queue.put_nowait(b"new")
        self.assertEqual(queue.dropped_packets, 1)
        self.assertEqual(await queue.get(0.01), b"middle")
        self.assertEqual(await queue.get(0.01), b"new")

    async def test_pc_speaker_stop_closes_and_resume_reopens_stream(self) -> None:
        class FakeOutputStream:
            def __init__(self) -> None:
                self.abort_count = 0
                self.close_count = 0

            def abort(self) -> None:
                self.abort_count += 1

            def close(self) -> None:
                self.close_count += 1

        speaker = PCSpeaker()
        old_stream = FakeOutputStream()
        new_stream = FakeOutputStream()
        speaker._stream = old_stream

        await speaker.block_and_flush()
        self.assertTrue(speaker.blocked)
        self.assertIsNone(speaker._stream)
        self.assertEqual(old_stream.abort_count, 1)
        self.assertEqual(old_stream.close_count, 1)

        speaker._open_stream = lambda: setattr(speaker, "_stream", new_stream)
        await speaker.resume()
        self.assertFalse(speaker.blocked)
        self.assertIs(speaker._stream, new_stream)


class PhaseBControlContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeSession.next_id = 0
        self.registry = SkillRegistry(CONFIG)
        self.speaker = FakeSpeaker()
        self.telemetry = FakeTelemetry()
        self.sessions: list[FakeSession] = []

        def factory(*args: Any) -> FakeSession:
            session = FakeSession(*args)
            self.sessions.append(session)
            return session

        self.manager = GatewaySessionManager(
            RokidRuntimeConfig(
                skills_config=str(CONFIG),
                play_audio=False,
                playback_echo_tail_s=0.0,
            ),
            self.registry,
            DropOldestAudioQueue(max_packets=4),
            LatestFrame(),
            self.speaker,
            session_factory=factory,
        )
        self.manager.harness = self.telemetry

    async def test_stop_and_resume_flush_pc_output_and_ack(self) -> None:
        await self.manager.start_initial()
        stop_ack = await self.manager.handle_control(
            {
                "type": "control.intent",
                "event_id": 1,
                "intent": "stop_speech",
                "accepted": True,
            }
        )
        self.assertTrue(stop_ack["ok"])
        self.assertTrue(self.manager.gate.speech_hold_active)
        self.assertTrue(self.speaker.blocked)
        self.assertEqual(self.speaker.flush_count, 1)

        resume_ack = await self.manager.handle_control(
            {
                "type": "control.intent",
                "event_id": 2,
                "intent": "resume_speech",
                "accepted": True,
            }
        )
        self.assertTrue(resume_ack["ok"])
        self.assertFalse(self.manager.gate.speech_hold_active)
        self.assertFalse(self.speaker.blocked)
        self.assertEqual(
            [message["intent"] for message in self.telemetry.messages if message.get("type") == "control.ack"],
            ["stop_speech", "resume_speech"],
        )

    async def test_skill_switch_replaces_session_and_fences_old_output(self) -> None:
        await self.manager.start_initial()
        old_session = self.sessions[0]
        ack = await self.manager.handle_control(
            {
                "type": "control.intent",
                "event_id": 3,
                "intent": "activate_skill",
                "accepted": True,
                "skill_id": "read_text",
                "slots": {},
            }
        )
        new_session = self.sessions[1]
        self.assertTrue(ack["ok"])
        self.assertEqual(old_session.stopped_with, "light")
        self.assertTrue(new_session.started)
        self.assertNotEqual(old_session.session_id, new_session.session_id)
        self.assertEqual(self.manager.gate.generation, 1)
        self.assertEqual(self.manager.gate.current_skill, "read_text")
        self.assertEqual(
            new_session.injected_tasks,
            ["请立即读取当前画面中最明显的文字，只读看到的内容。"],
        )
        self.assertTrue(ack["task_trigger_sent"])
        self.assertFalse(self.speaker.blocked)
        self.assertTrue(
            any(
                message.get("type") == "session.state"
                and message.get("phase") == "restart_complete"
                for message in self.telemetry.messages
            )
        )

        audio = base64.b64encode(np.ones(10, dtype=np.float32).tobytes()).decode()
        await self.manager.handle_result(
            old_session,
            {"type": "result", "text": "stale", "audio_data": audio},
        )
        self.assertEqual(self.manager.gate.dropped_old_text, 1)
        self.assertEqual(self.manager.gate.dropped_old_audio, 1)
        self.assertEqual(self.speaker.enqueued, [])

        await self.manager.handle_result(
            new_session,
            {"type": "result", "text": "current", "audio_data": audio},
        )
        self.assertEqual(len(self.speaker.enqueued), 1)
        self.assertEqual(self.speaker.enqueued[0][1], 1)

    async def test_restart_complete_plays_one_local_ready_chime(self) -> None:
        self.manager.config.play_audio = True
        await self.manager.start_initial()
        self.assertEqual(self.speaker.enqueued, [])

        ack = await self.manager.handle_control(
            {
                "type": "control.intent",
                "event_id": 4,
                "intent": "reset_session",
                "accepted": True,
                "skill_id": "idle_chat",
                "slots": {},
            }
        )

        self.assertTrue(ack["ok"])
        self.assertEqual(len(self.speaker.enqueued), 1)
        cue, generation = self.speaker.enqueued[0]
        self.assertEqual(generation, 1)
        self.assertGreater(cue.size, 0)
        self.assertGreater(self.manager.gate.local_cue_mute_until_mono, 0.0)

    async def test_obstacle_switch_injects_one_shot_visual_task(self) -> None:
        await self.manager.start_initial()
        ack = await self.manager.handle_control(
            {
                "type": "control.intent",
                "event_id": 5,
                "intent": "activate_skill",
                "accepted": True,
                "skill_id": "obstacle_avoidance",
                "slots": {},
            }
        )
        self.assertTrue(ack["task_trigger_sent"])
        self.assertIn("判断当前画面", self.sessions[1].injected_tasks[0])

    async def test_model_log_turn_ends_on_listen_not_decode_slice(self) -> None:
        await self.manager.start_initial()
        session = self.sessions[0]
        await self.manager.handle_result(
            session,
            {"type": "result", "text": "上海", "end_of_turn": True},
        )
        await self.manager.handle_result(
            session,
            {"type": "result", "text": "电力", "end_of_turn": True},
        )
        await self.manager.handle_result(
            session,
            {"type": "result", "is_listen": True},
        )
        states = [
            item for item in self.telemetry.messages
            if item.get("type") == "model.state"
        ]
        self.assertEqual([item["end_of_turn"] for item in states], [False, False, True])
        self.assertEqual([item["decode_end"] for item in states], [True, True, False])


if __name__ == "__main__":
    unittest.main()
