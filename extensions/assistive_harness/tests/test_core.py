from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from extensions.assistive_harness.asr.energy_vad import EnergyVAD
from extensions.assistive_harness.cv.noop import ShadowCVProvider
from extensions.assistive_harness.cv.base import CVObservation, FrameEnvelope
from extensions.assistive_harness.cv.pipeline import CVPipeline
from extensions.assistive_harness.cv.registry import CVProviderRegistry
from extensions.assistive_harness.echo_guard import EchoGuard
from extensions.assistive_harness.model_log import ModelTurnAccumulator
from extensions.assistive_harness.registry import RegistryError, SkillRegistry
from extensions.assistive_harness.router import RuleIntentRouter
from extensions.assistive_harness.schemas import ControlIntent
from extensions.assistive_harness.state_machine import HarnessController


CONFIG = Path(__file__).resolve().parents[1] / "config" / "skills.example.yaml"


class RouterCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = SkillRegistry(CONFIG)
        cls.router = RuleIntentRouter(cls.registry)

    def test_control_and_skill_corpus_over_fifty_utterances(self) -> None:
        corpus = [
            ("停一下", ControlIntent.STOP_SPEECH, None),
            ("停一下啊", ControlIntent.STOP_SPEECH, None),
            ("停一下下", ControlIntent.STOP_SPEECH, None),
            ("同一下", ControlIntent.STOP_SPEECH, None),
            ("同一下下", ControlIntent.STOP_SPEECH, None),
            ("等一下", ControlIntent.STOP_SPEECH, None),
            ("等一一下", ControlIntent.STOP_SPEECH, None),
            ("请停一下啊", ControlIntent.STOP_SPEECH, None),
            ("请停一下", ControlIntent.STOP_SPEECH, None),
            ("麻烦你先停一下", ControlIntent.STOP_SPEECH, None),
            ("别说了", ControlIntent.STOP_SPEECH, None),
            ("请你别说了好吗", ControlIntent.STOP_SPEECH, None),
            ("闭嘴", ControlIntent.STOP_SPEECH, None),
            ("停止播报", ControlIntent.STOP_SPEECH, None),
            ("安静", ControlIntent.STOP_SPEECH, None),
            ("你先停一下，我有个问题", ControlIntent.STOP_SPEECH, None),
            ("说得有点长了麻烦停一下吧", ControlIntent.STOP_SPEECH, None),
            ("现在可以先停一下然后听我说吗", ControlIntent.STOP_SPEECH, None),
            ("不停停一下继续介绍", ControlIntent.STOP_SPEECH, None),
            ("我刚才没有说停一下", ControlIntent.STOP_SPEECH, None),
            ("请不要停一下，继续介绍", ControlIntent.STOP_SPEECH, None),
            ("停一下是什么意思", ControlIntent.STOP_SPEECH, None),
            ("恢复对话", ControlIntent.RESUME_SPEECH, None),
            ("好了现在恢复对话吧", ControlIntent.RESUME_SPEECH, None),
            ("乐奇恢复对话", ControlIntent.RESUME_SPEECH, None),
            ("停一下然后恢复对话", ControlIntent.STOP_SPEECH, None),
            ("重新开始", ControlIntent.RESET_SESSION, "idle_chat"),
            ("请重新开始", ControlIntent.RESET_SESSION, "idle_chat"),
            ("重置会话", ControlIntent.NONE, None),
            ("乐奇请重新开始", ControlIntent.RESET_SESSION, "idle_chat"),
            ("你有点卡了请重新开始会话", ControlIntent.RESET_SESSION, "idle_chat"),
            ("请现在重置会话然后听我说", ControlIntent.NONE, None),
            ("停一下然后重新开始", ControlIntent.STOP_SPEECH, None),
            ("恢复对话然后重新开始", ControlIntent.RESET_SESSION, "idle_chat"),
            ("新建会话", ControlIntent.NONE, None),
            ("清空上下文", ControlIntent.NONE, None),
            ("回到聊天", ControlIntent.RETURN_TO_CHAT, "idle_chat"),
            ("请回到聊天", ControlIntent.RETURN_TO_CHAT, "idle_chat"),
            ("回到普通聊天", ControlIntent.RETURN_TO_CHAT, "idle_chat"),
            ("回到普通聊天天", ControlIntent.RETURN_TO_CHAT, "idle_chat"),
            ("退出技能", ControlIntent.RETURN_TO_CHAT, "idle_chat"),
            ("普通聊天", ControlIntent.RETURN_TO_CHAT, "idle_chat"),
            ("取消任务", ControlIntent.CANCEL_SKILL, None),
            ("不找了", ControlIntent.CANCEL_SKILL, None),
            ("不读了", ControlIntent.CANCEL_SKILL, None),
            ("帮我找手机", ControlIntent.ACTIVATE_SKILL, "find_object"),
            ("帮我找一下我的手机", ControlIntent.ACTIVATE_SKILL, "find_object"),
            ("帮我找一下手机", ControlIntent.ACTIVATE_SKILL, "find_object"),
            ("帮我找一下下我的手机嗯", ControlIntent.ACTIVATE_SKILL, "find_object"),
            ("帮找一下手机手机", ControlIntent.ACTIVATE_SKILL, "find_object"),
            ("请帮我找钥匙", ControlIntent.ACTIVATE_SKILL, "find_object"),
            ("水杯在哪", ControlIntent.ACTIVATE_SKILL, "find_object"),
            ("书在哪里", ControlIntent.ACTIVATE_SKILL, "find_object"),
            ("看到门卡了吗", ControlIntent.ACTIVATE_SKILL, "find_object"),
            ("有没有雨伞", ControlIntent.ACTIVATE_SKILL, "find_object"),
            ("麻烦帮我找眼镜", ControlIntent.ACTIVATE_SKILL, "find_object"),
            ("请问钱包在哪", ControlIntent.ACTIVATE_SKILL, "find_object"),
            ("读一下", ControlIntent.ACTIVATE_SKILL, "read_text"),
            ("读一下这行字", ControlIntent.ACTIVATE_SKILL, "read_text"),
            ("请读一下", ControlIntent.ACTIVATE_SKILL, "read_text"),
            ("帮我读一下这个杯子上的字母母母", ControlIntent.ACTIVATE_SKILL, "read_text"),
            ("帮读一下这个水杯上面的字字个", ControlIntent.ACTIVATE_SKILL, "read_text"),
            ("帮我识字", ControlIntent.ACTIVATE_SKILL, "read_text"),
            ("上面写了什么", ControlIntent.ACTIVATE_SKILL, "read_text"),
            ("这是什么字", ControlIntent.ACTIVATE_SKILL, "read_text"),
            ("读文字", ControlIntent.ACTIVATE_SKILL, "read_text"),
            ("请你读文字", ControlIntent.ACTIVATE_SKILL, "read_text"),
            ("描述一下", ControlIntent.ACTIVATE_SKILL, "describe_scene"),
            ("帮我描述一下", ControlIntent.ACTIVATE_SKILL, "describe_scene"),
            ("看看周围", ControlIntent.ACTIVATE_SKILL, "describe_scene"),
            ("帮我避障", ControlIntent.ACTIVATE_SKILL, "obstacle_avoidance"),
            ("前面有障碍吗", ControlIntent.ACTIVATE_SKILL, "obstacle_avoidance"),
            ("今天天气怎么样", ControlIntent.NONE, None),
            ("停止是一个动词", ControlIntent.NONE, None),
            ("他说让我闭嘴但我没同意", ControlIntent.NONE, None),
            ("重新开始这个词怎么翻译", ControlIntent.RESET_SESSION, "idle_chat"),
            ("我在读一本书", ControlIntent.NONE, None),
            ("你觉得这杯水怎么样", ControlIntent.NONE, None),
            ("普通聊天机器人是什么", ControlIntent.NONE, None),
            ("请介绍一下上海", ControlIntent.NONE, None),
            ("我不想清空上下文因为还有用", ControlIntent.NONE, None),
            ("帮我分析这段话", ControlIntent.NONE, None),
            ("帮我一下手机手机", ControlIntent.NONE, None),
            ("能听见我吗", ControlIntent.NONE, None),
            ("今天周几", ControlIntent.NONE, None),
            ("为什么会这样", ControlIntent.NONE, None),
            ("继续说", ControlIntent.NONE, None),
            ("接着说", ControlIntent.NONE, None),
            ("你继续说话吧", ControlIntent.NONE, None),
            ("谢谢", ControlIntent.NONE, None),
            ("你好", ControlIntent.NONE, None),
            ("左边有什么", ControlIntent.NONE, None),
            ("给我讲个笑话", ControlIntent.NONE, None),
            ("", ControlIntent.NONE, None),
            ("。", ControlIntent.NONE, None),
            ("安静是一种状态", ControlIntent.NONE, None),
            ("取消任务是不是一个按钮", ControlIntent.NONE, None),
            ("有人说不读了然后离开", ControlIntent.NONE, None),
        ]
        self.assertGreaterEqual(len(corpus), 50)
        for utterance, expected_intent, expected_skill in corpus:
            with self.subTest(utterance=utterance):
                result = self.router.route(utterance)
                self.assertEqual(result.intent, expected_intent)
                self.assertEqual(result.skill_id, expected_skill)

    def test_find_target_slot(self) -> None:
        result = self.router.route("请帮我找深绿色的书")
        self.assertEqual(result.slots, {"target": "深绿色的书"})

        natural = self.router.route("帮我找一下我的手机")
        self.assertEqual(natural.slots, {"target": "手机"})

        repeated = self.router.route("帮我找一下下我的手机嗯")
        self.assertEqual(repeated.slots, {"target": "手机"})

        missing_pronoun = self.router.route("帮找一下手机手机")
        self.assertEqual(missing_pronoun.slots, {"target": "手机"})


class RegistryAndStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = SkillRegistry(CONFIG)
        self.router = RuleIntentRouter(self.registry)
        self.controller = HarnessController(self.registry)

    def event(self, text: str, event_id: int, asr_id: int):
        return self.router.make_event(
            self.router.route(text), event_id=event_id, asr_event_id=asr_id,
            utterance=text, created_at_ms=float(event_id * 1000),
        )

    def test_prompt_render_and_hash(self) -> None:
        prompt = self.registry.render("find_object", {"target": "手机"})
        self.assertIn("当前寻找目标：手机", prompt.text)
        self.assertIn("智能眼镜找物助手", prompt.text)
        self.assertEqual(len(prompt.sha256), 64)
        self.assertIn("手机", self.registry.task_trigger("find_object", {"target": "手机"}))

    def test_aaai_skill_prompt_files_are_user_editable_and_registered(self) -> None:
        find_prompt = self.registry.render("find_object", {"target": "水杯"})
        read_prompt = self.registry.render("read_text", {})
        self.assertTrue(Path(find_prompt.path).is_file())
        self.assertTrue(Path(read_prompt.path).is_file())
        self.assertIn("当前寻找目标：水杯", find_prompt.text)
        self.assertIn("智能眼镜读字助手", read_prompt.text)
        self.assertTrue(self.registry.is_enabled("describe_scene"))
        self.assertTrue(self.registry.is_enabled("obstacle_avoidance"))
        self.assertTrue(
            Path(self.registry.get("obstacle_avoidance")["prompt_file"]).name
            == "obstacle_avoidance_zh.txt"
        )

    def test_required_slot_validation(self) -> None:
        with self.assertRaises(RegistryError):
            self.registry.render("find_object", {})

    def test_unknown_skill_is_rejected(self) -> None:
        with self.assertRaises(RegistryError):
            self.registry.render("not_registered", {})

    def test_stop_hold_requires_explicit_resume(self) -> None:
        decision = self.controller.process(self.event("停一下", 1, 1))
        self.assertEqual(decision.action, "stop_speech")
        self.assertTrue(self.controller.state.speech_hold_active)
        self.assertTrue(self.controller.state.drop_output_until_listen)
        self.controller.mark_listen_fence()
        self.assertTrue(self.controller.state.speech_hold_active)
        self.assertTrue(self.controller.state.drop_output_until_listen)
        resumed = self.controller.process(self.event("恢复对话", 2, 2))
        self.assertEqual(resumed.action, "resume_speech")
        self.assertFalse(self.controller.state.speech_hold_active)
        self.assertFalse(self.controller.state.drop_output_until_listen)

    def test_reset_always_supplies_idle_prompt(self) -> None:
        decision = self.controller.process(self.event("重新开始", 1, 1))
        self.assertEqual(decision.action, "restart_session")
        self.assertIn("AI 眼镜助手", decision.payload["system_prompt"])
        self.assertTrue(decision.payload["prompt_path"].endswith("idle_chat_zh.txt"))

    def test_skill_activation_supplies_rendered_prompt_path_and_hash(self) -> None:
        decision = self.controller.process(self.event("帮我找手机", 1, 1))
        self.assertEqual(decision.action, "activate_skill")
        self.assertIn("当前寻找目标：手机", decision.payload["system_prompt"])
        self.assertTrue(decision.payload["prompt_path"].endswith("find_object_zh.txt"))
        self.assertEqual(len(decision.payload["prompt_sha256"]), 64)

    def test_duplicate_asr_event_is_ignored(self) -> None:
        first = self.controller.process(self.event("停一下", 1, 9))
        second = self.controller.process(self.event("停一下", 2, 9))
        self.assertTrue(first.accepted)
        self.assertEqual(second.reason, "duplicate_asr_event")

    def test_same_skill_same_slots_does_not_restart(self) -> None:
        first = self.controller.process(self.event("帮我找手机", 1, 1))
        self.assertTrue(first.accepted)
        self.controller.mark_restart_complete("find_object", {"target": "手机"}, 1)
        second = self.controller.process(self.event("帮我找手机", 2, 2), now_ms=9000)
        self.assertFalse(second.accepted)
        self.assertEqual(second.reason, "same_skill_same_slots")

    def test_restart_pending_is_last_write_wins(self) -> None:
        self.controller.process(self.event("帮我找手机", 1, 1))
        queued_one = self.controller.process(self.event("帮我找钥匙", 2, 2))
        queued_two = self.controller.process(self.event("读一下", 3, 3))
        self.assertEqual(queued_one.action, "queue_skill")
        self.assertEqual(queued_two.action, "queue_skill")
        pending = self.controller.mark_restart_complete("find_object", {"target": "手机"}, 1)
        self.assertEqual(pending, {"skill_id": "read_text", "slots": {}})

    def test_disconnect_clears_restart_and_pending(self) -> None:
        self.controller.process(self.event("帮我找手机", 1, 1))
        self.controller.process(self.event("读一下", 2, 2))
        self.controller.mark_disconnected()
        self.assertFalse(self.controller.state.restart_in_progress)
        self.assertIsNone(self.controller.state.pending_skill)


class EchoAndVADTests(unittest.TestCase):
    def test_recent_model_echo_is_blocked_but_stop_is_allowed(self) -> None:
        guard = EchoGuard(window_ms=20_000, similarity_threshold=0.8)
        guard.note_model_text("请告诉我还需要什么帮助", at_ms=1000)
        blocked = guard.evaluate("请告诉我还需要什么帮助", ControlIntent.NONE, at_ms=1100)
        self.assertFalse(blocked.allow)
        guard.set_ai_speaking(True)
        allowed = guard.evaluate("停一下", ControlIntent.STOP_SPEECH, at_ms=1200)
        self.assertTrue(allowed.allow)
        resumed = guard.evaluate("恢复对话", ControlIntent.RESUME_SPEECH, at_ms=1300)
        self.assertTrue(resumed.allow)

        skill = guard.evaluate("帮我找一下我的手机", ControlIntent.ACTIVATE_SKILL, at_ms=1400)
        self.assertTrue(skill.allow)
        returned = guard.evaluate("回到普通聊天", ControlIntent.RETURN_TO_CHAT, at_ms=1450)
        self.assertTrue(returned.allow)
        cancelled = guard.evaluate("取消任务", ControlIntent.CANCEL_SKILL, at_ms=1475)
        self.assertTrue(cancelled.allow)
        ordinary = guard.evaluate("今天天气怎么样", ControlIntent.NONE, at_ms=1500)
        self.assertFalse(ordinary.allow)
        self.assertEqual(ordinary.reason, "ordinary_skill_suppressed_while_ai_speaking")

    def test_vad_emits_one_utterance(self) -> None:
        vad = EnergyVAD(rms_threshold=0.01, min_speech_ms=100, end_silence_ms=200)
        frames = [np.zeros(1600, np.float32)]
        frames += [np.full(1600, 0.1, np.float32) for _ in range(3)]
        frames += [np.zeros(1600, np.float32) for _ in range(3)]
        utterances = []
        for index, frame in enumerate(frames):
            result = vad.feed(frame, index * 100.0)
            if result is not None:
                utterances.append(result)
        self.assertEqual(len(utterances), 1)
        self.assertGreater(utterances[0].audio.size, 0)


class ModelTurnLogTests(unittest.TestCase):
    def test_streamed_fragments_are_aggregated_with_audio_duration(self) -> None:
        turns = ModelTurnAccumulator()
        first = turns.feed(
            {
                "state": "speak",
                "session_id": "s1",
                "generation": 2,
                "skill_id": "read_text",
                "text": "上海",
                "audio_ms": 120,
            }
        )
        self.assertEqual(first, [])
        second = turns.feed(
            {
                "state": "speak",
                "session_id": "s1",
                "generation": 2,
                "skill_id": "read_text",
                "text": "电力",
                "audio_ms": 180,
                "decode_end": True,
            }
        )
        self.assertEqual(second, [])
        completed = turns.feed(
            {
                "state": "listen",
                "session_id": "s1",
                "generation": 2,
                "skill_id": "read_text",
                "end_of_turn": True,
            }
        )
        self.assertEqual(completed[0]["text"], "上海电力")
        self.assertEqual(completed[0]["audio_ms"], 300.0)


class CVIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_shadow_provider_contains_failures(self) -> None:
        class Broken:
            def analyze(self, *args, **kwargs):
                raise RuntimeError("synthetic CV failure")

        result = ShadowCVProvider(Broken(), "broken").analyze(
            None, "f1", 1.0, "find_object", {}
        )
        self.assertEqual(result.provider, "shadow:broken")
        self.assertIn("synthetic CV failure", result.error or "")

    async def test_pipeline_is_non_blocking_and_latest_frame_wins(self) -> None:
        observations: list[CVObservation] = []

        class Slow:
            def analyze(self, frame, frame_id, timestamp_ms, skill_id, slots):
                time.sleep(0.08)
                return CVObservation(
                    frame_id=frame_id,
                    timestamp_ms=timestamp_ms,
                    skill_id=skill_id,
                    provider="slow",
                )

        pipeline = CVPipeline(
            CVProviderRegistry({"slow": Slow()}),
            on_observation=observations.append,
            inference_timeout_ms=500,
        )
        pipeline.submit(FrameEnvelope(b"1", "f1", 1.0, "find_object", {}, "shadow", "slow"))
        # submit() must return before the synchronous provider completes.  A
        # wall-clock threshold is flaky on a loaded Windows workstation.
        self.assertEqual(observations, [])
        self.assertIsNotNone(pipeline.worker_task)
        await asyncio.sleep(0.01)
        pipeline.submit(FrameEnvelope(b"2", "f2", 2.0, "find_object", {}, "shadow", "slow"))
        pipeline.submit(FrameEnvelope(b"3", "f3", 3.0, "find_object", {}, "shadow", "slow"))
        for _ in range(100):
            if len(observations) >= 2:
                break
            await asyncio.sleep(0.01)
        snapshot = pipeline.snapshot()
        await pipeline.close()
        self.assertEqual([item.frame_id for item in observations], ["f1", "f3"])
        self.assertEqual(snapshot["dropped_frames"], 1)

    async def test_pipeline_timeout_is_observed_without_escaping(self) -> None:
        observations: list[CVObservation] = []

        class TooSlow:
            def analyze(self, frame, frame_id, timestamp_ms, skill_id, slots):
                time.sleep(0.08)
                return CVObservation(frame_id, timestamp_ms, skill_id, "too_slow")

        pipeline = CVPipeline(
            CVProviderRegistry({"too_slow": TooSlow()}),
            on_observation=observations.append,
            inference_timeout_ms=10,
        )
        pipeline.submit(
            FrameEnvelope(b"x", "timeout", 1.0, "find_object", {}, "shadow", "too_slow")
        )
        for _ in range(50):
            if observations:
                break
            await asyncio.sleep(0.005)
        snapshot = pipeline.snapshot()
        await pipeline.close()
        self.assertIn("TimeoutError", observations[0].error or "")
        self.assertEqual(snapshot["timeout_count"], 1)


if __name__ == "__main__":
    unittest.main()
