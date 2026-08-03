"""ESP32 双工桥 v6 —— v5.2 + 摄像头旋转修复 + 模型视角回放视频
======================================================================

v6 相对 v5.2 的新增能力（不改动 v5.2；此文件是新副本）
----------------------------------------------------------------------
1. **摄像头顺时针 90° 修复（默认 ``--rotate 90``）**
   参考 ``demo_asr_vlm_stream_tts_glasses_esp32mic_v4_vad.py`` 的做法，
   在把 JPEG 送给模型之前用 PIL 旋转，顺便落盘同样已旋转的图像，
   这样录像出来看到的就是"模型实际看到的那帧"。

2. **退出时自动合成"模型视角"回放视频 ``model_view.mp4``**
   为避免实时合视频拖慢 MiniCPM-o 推理，**运行期间只记录时间戳**：

   * ``events.jsonl``           —— 每个 chunk 的 ``ts_offset_ms`` + 图像路径
   * ``user_raw.pcm``           —— 麦克风 16kHz int16 连续轨
   * ``ai_audio.pcm``           —— AI TTS 24kHz float32 按到达顺序拼接
   * ``ai_audio_timeline.jsonl`` —— 每段 AI 音频的到达 session_ts_ms + 样本数（v6 新增）
   * ``subtitles.jsonl``        —— 每个 turn 的 [start_ms, end_ms, text, is_listen]（v6 新增）

   当脚本被 Ctrl-C 或其他方式终止时，在 ``finally`` 里调用
   ``render_model_view_video(session_dir)``，用 ffmpeg 按下列方式合成：

   * 视频轨：按 ``events.jsonl`` 的 ``ts_offset_ms`` 用 concat demuxer 拼接旋转后的 JPEG
   * 用户音轨：``user_raw.pcm`` 直接当 s16le/16000Hz/mono
   * AI 音轨：把 ``ai_audio.pcm`` 按 ``ai_audio_timeline.jsonl`` 的 session_ts_ms
     在一条 f32le/24000Hz/mono 的空轨上"归位"后落盘
   * 两条音轨 ``amix`` 合并，字幕从 ``subtitles.jsonl`` 生成 SRT 后用
     ``subtitles=`` 视频滤镜烧入
   * 输出 ``<session_dir>/model_view.mp4``

   若找不到 ffmpeg（PATH / imageio_ffmpeg / FFMPEG_BINARY 环境变量都没有），
   会跳过合成并打印提示，其他产物依然保留，后续可手工合成。

CLI 新增
--------
  ``--rotate {0,90,180,270}``   摄像头画面顺时针旋转角度（默认 ``90``，眼镜侧装修正）
  ``--no-render-video``         退出时不合成回放视频（默认合成）
  ``--video-fps``               合成视频的视频帧率（默认 10）
  ``--video-font``              字幕字体名（默认 ``Microsoft YaHei``）
  ``--ffmpeg-bin``              显式指定 ffmpeg 可执行文件（覆盖自动探测）

其余 CLI 与 v5.2 完全一致。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as _dt
import difflib
import io
import json
import logging
import os
import queue
import re
import shutil
import ssl
import signal
import struct
import subprocess
import sys
import threading
import time
import collections
from pathlib import Path
from typing import Any, Optional

import aiohttp
import numpy as np

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover
    sd = None

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]


# ============================================================
# Config
# ============================================================

SAMPLE_RATE_IN   = 16000
SAMPLE_RATE_OUT  = 24000
CHUNK_MS         = 1000
GRACE_MS         = 500
RING_CAPACITY_S  = 10

ESP32_PKT_HDR    = 12
ESP32_PKT_BODY   = 640

LOGGER = logging.getLogger("duplex_v6_6")

# —— v6.6 移植：录制 / Web 观测 / rerun 输入源（取代旧的 render_model_view_video）——
# 本文件由 panel 以独立子进程启动，工作目录可能不是本文件所在目录（cwd 常指向
# MiniCPM-o-Demo，供 worker/gateway 定位）。把本文件目录加入 sys.path，使下面对同
# 目录模块(recorder_live/bridge_ui/rerun_source)的导入不依赖 cwd。
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
from recorder_live import LiveRecorder
from bridge_ui import WebUIServer
try:
    from rerun_source import local_pcm_reader, LocalImageSource
except Exception:  # rerun 依赖缺失时不影响 live
    local_pcm_reader = None
    LocalImageSource = None

ASR_RUNTIME_DIR = Path(__file__).resolve().parent / "ASR"
if str(ASR_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_RUNTIME_DIR))
try:
    from skill_router import SkillRouter
except Exception:  # pragma: no cover
    SkillRouter = None  # type: ignore[assignment]

SKILL_ROUTER = None
ENABLED_ROUTER_SKILLS: set[str] = set()

SENSEVOICE_TAG_RE = re.compile(r"<\|[^|]+?\|>")

FIND_OBJECT_SUFFIX = """

[当前任务：寻找物体]
用户正在让你寻找一个具体物体。你可以看到用户眼镜视角的画面。

规则：
1. 如果画面里清楚看到目标物体，必须回答："定位模式：<目标>在<方位>。"
2. 方位尽量使用：左、右、中间、上、下、左上、右上、左下、右下。
3. 如果只是疑似看到，必须回答："定位模式：疑似在<方位>。"
4. 如果没有看到目标物体，保持安静，不要描述无关物体。
5. 不要说"过去拿"、"走过去"、"就在那边"。
6. 每句话尽量不超过 15 个字。
7. 不要使用"我看到："开头。
"""

SCENE_DESCRIPTION_SUFFIX = """

[当前任务：描述场景]
用户正在让你描述当前看到的画面或周围环境。你可以看到用户眼镜视角的画面。

规则：
1. 回答必须以："场景模式：" 开头。
2. 用 1-3 句概括主要物体、位置关系和明显状态。
3. 优先描述用户可能关心的桌面、道路、门口、文字、人物、障碍物。
4. 不要编造看不清的内容；看不清就说"场景模式：画面不太清楚。"
5. 不要使用寻找物体模式的"定位模式：..."格式。
"""

MULTI_SKILL_ROUTER_SUFFIX = """

[技能路由规则]
你会根据用户问题选择回答模式：
1. 用户问"在哪/哪里/哪儿/找/有没有某物/看到某物吗"时，使用[当前任务：寻找物体]规则。
2. 用户问"描述一下/看看周围/画面里有什么/现在是什么场景/桌面上有什么"时，使用[当前任务：描述场景]规则。
3. 其他普通聊天，正常简短回答。
"""


class SkillReconnect(Exception):
    def __init__(self, skill: str) -> None:
        super().__init__(skill)
        self.skill = skill


# ============================================================
# Rotation helper（v6 新增）
# ============================================================

_ROTATE_MAP = None


def _build_rotate_map():
    """延迟构造，避免 PIL 不可用时模块导入失败。"""
    global _ROTATE_MAP
    if _ROTATE_MAP is not None or Image is None:
        return _ROTATE_MAP
    _ROTATE_MAP = {
        0: None,
        90: Image.Transpose.ROTATE_270,   # PIL 按逆时针算：顺时针 90° = ROTATE_270
        180: Image.Transpose.ROTATE_180,
        270: Image.Transpose.ROTATE_90,
    }
    return _ROTATE_MAP


def rotate_jpeg(jpg_bytes: bytes, rotate_cw: int, quality: int = 90) -> bytes:
    """顺时针旋转一张 JPEG；0 时原样返回。PIL 不可用时原样返回。"""
    deg = rotate_cw % 360
    if deg == 0:
        return jpg_bytes
    if Image is None:
        LOGGER.warning("[ROTATE] Pillow 未安装，--rotate 将被忽略")
        return jpg_bytes
    m = _build_rotate_map()
    op = m.get(deg) if m else None
    if op is None:
        return jpg_bytes
    try:
        img = Image.open(io.BytesIO(jpg_bytes))
        img = img.transpose(op)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception as e:
        LOGGER.warning("[ROTATE] 旋转失败: %s (原样返回)", e)
        return jpg_bytes


# ============================================================
# TimestampedRingBuffer（与 v5.2 完全一致）
# ============================================================

def _clean_asr_text(text: str) -> str:
    return SENSEVOICE_TAG_RE.sub("", text or "").strip()


def _norm_match_text(text: str) -> str:
    return "".join(ch for ch in (text or "") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _extract_asr_text(result: Any) -> str:
    if isinstance(result, list):
        return "".join(_extract_asr_text(x) for x in result)
    if isinstance(result, dict):
        return str(result.get("text") or result.get("sentence") or "")
    return str(result or "")


def _load_text_file(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception as e:
        LOGGER.warning("[ASR] failed to read prompt suffix %s: %s", path, e)
        return ""


def _detect_find_object(text: str) -> tuple[str, str]:
    text = (text or "").strip()
    target_words = (
        "绿色签字笔", "深绿色笔记本", "白色耳机盒子", "耳机盒子",
        "百事可乐", "矿泉水瓶", "电风扇", "签字笔", "笔记本",
        "手机", "钥匙", "眼镜", "水杯", "杯子", "钱包", "电脑",
        "鼠标", "盒子", "瓶子", "风扇", "可乐", "耳机", "书", "笔",
    )
    has_find_intent = any(w in text for w in (
        "在哪", "在哪里", "哪儿", "哪里", "找", "寻找", "有没有",
    ))
    has_seen_question = ("看到" in text or "看见" in text) and ("吗" in text or "么" in text)
    target = next((w for w in target_words if w in text), "")
    if not target and (has_find_intent or has_seen_question):
        target = text
        for mark in ("在哪里", "在哪儿", "在哪", "哪里", "哪儿", "有没有", "有吗", "吗", "？", "?"):
            target = target.replace(mark, "")
        for prefix in ("桌面上的", "桌子上的", "桌面上", "桌子上", "我的", "一个", "一只", "一支", "的"):
            target = target.replace(prefix, "")
        target = target.strip(" ，,。.")
    if (has_find_intent or has_seen_question) and target:
        return "find_object", target
    return "", target


def _detect_describe_scene(text: str) -> bool:
    text = (text or "").strip()
    return any(w in text for w in (
        "描述一下", "描述下", "介绍一下", "看看周围", "看一下周围",
        "周围有什么", "画面里有什么", "画面有什么", "场景", "环境",
        "桌面上有什么", "桌子上有什么", "前面有什么", "我面前有什么",
        "现在看到什么", "你看到了什么", "你看到什么",
    ))


def _classify_skill(text: str) -> tuple[str, str, str]:
    if SKILL_ROUTER is not None:
        try:
            match = SKILL_ROUTER.classify(text)
            if (
                ENABLED_ROUTER_SKILLS
                and match.skill
                and match.skill not in ENABLED_ROUTER_SKILLS
            ):
                if match.skill != "idle_chat":
                    LOGGER.info(
                        "[SKILL] matched %s but it is disabled in this run; using base prompt",
                        match.skill,
                    )
                return "", match.target, ""
            return match.skill, match.target, match.inject
        except Exception as e:
            LOGGER.warning("[SKILL] external router failed; fallback: %s", e)
    intent, target = _detect_find_object(text)
    if intent == "find_object":
        return intent, target, FIND_OBJECT_SUFFIX
    if _detect_describe_scene(text):
        return "describe_scene", "", SCENE_DESCRIPTION_SUFFIX
    return "", target, ""


def _active_skill_from_args(args) -> str:
    if getattr(args, "multi_skill_mode", False):
        return "multi"
    if getattr(args, "find_object_mode", False):
        return "find_object"
    if getattr(args, "describe_scene_mode", False):
        return "describe_scene"
    if getattr(args, "read_text_mode", False):
        return "read_text"
    if getattr(args, "visual_qa_mode", False):
        return "visual_qa"
    return ""


def _set_active_skill_args(args, skill: str) -> None:
    args.multi_skill_mode = False
    args.find_object_mode = skill == "find_object"
    args.describe_scene_mode = skill == "describe_scene"
    args.read_text_mode = skill == "read_text"
    args.visual_qa_mode = skill == "visual_qa"


def _selected_multi_skills(args) -> list[str]:
    if getattr(args, "all_skills_mode", False):
        if SKILL_ROUTER is not None:
            try:
                return list(SKILL_ROUTER.rules.get("skills", {}).keys())
            except Exception:
                pass
        return ["find_object", "describe_scene", "read_text", "visual_qa", "idle_chat"]
    return ["find_object", "describe_scene"]


def _set_enabled_router_skills(args) -> None:
    global ENABLED_ROUTER_SKILLS
    enabled: set[str] = set()
    if getattr(args, "multi_skill_mode", False):
        enabled.update(_selected_multi_skills(args))
    else:
        for flag_name, skill in (
            ("find_object_mode", "find_object"),
            ("describe_scene_mode", "describe_scene"),
            ("read_text_mode", "read_text"),
            ("visual_qa_mode", "visual_qa"),
        ):
            if getattr(args, flag_name, False):
                enabled.add(skill)
    ENABLED_ROUTER_SKILLS = enabled
    if enabled:
        LOGGER.info("[SKILL] enabled router skills: %s", ",".join(sorted(enabled)))


def _init_skill_router(args) -> None:
    global SKILL_ROUTER
    if SkillRouter is None:
        LOGGER.warning("[SKILL] skill_router.py is unavailable; using built-in rules")
        SKILL_ROUTER = None
        return
    try:
        skills_dir = Path(args.skills_dir)
        if not skills_dir.is_absolute():
            skills_dir = Path(__file__).resolve().parent / skills_dir
        SKILL_ROUTER = SkillRouter(skills_dir)
        LOGGER.info("[SKILL] loaded router from %s", skills_dir)
    except Exception as e:
        LOGGER.warning("[SKILL] load external skills failed; using built-in rules: %s", e)
        SKILL_ROUTER = None


def _skill_inject(skill: str) -> str:
    if SKILL_ROUTER is not None:
        try:
            return SKILL_ROUTER.injects.get(skill, "")
        except Exception:
            pass
    if skill == "find_object":
        return FIND_OBJECT_SUFFIX
    if skill == "describe_scene":
        return SCENE_DESCRIPTION_SUFFIX
    return ""


def _runtime_skill_prompt(intent: str, target: str, user_text: str) -> str:
    user_text = (user_text or "").strip()
    target = (target or "").strip()
    if intent == "find_object":
        target_line = f"目标物体：{target}" if target else "目标物体：用户刚才询问的物体"
        return (
            "[实时技能注入]\n"
            f"FunASR识别到用户说：{user_text}\n"
            "现在进入定位模式。你下一次回答必须以“现在进入定位模式：”开头。\n"
            f"{target_line}\n"
            "请只根据当前画面寻找目标物体，回答它的位置；如果没看到，就说没看到。"
        )
    if intent == "describe_scene":
        return (
            "[实时技能注入]\n"
            f"FunASR识别到用户说：{user_text}\n"
            "现在进入场景模式。你下一次回答必须以“现在进入场景模式：”开头。\n"
            "请用一到三句话描述当前画面里的主要物体、位置关系和明显状态。"
        )
    return ""


class FunASRMirror:
    """Mirror ESP32 mic chunks to local FunASR without blocking the gateway path."""

    def __init__(
        self,
        jsonl_path: Path,
        model_name: str,
        language: str,
        device: str,
        window_s: float,
        interval_s: float,
        min_rms: float,
        utterance_end_s: float,
        min_utterance_s: float,
        max_utterance_s: float,
        prompt_suffix: str = "",
    ) -> None:
        self.jsonl_path = jsonl_path
        self.model_name = model_name
        self.language = language
        self.device = device
        self.window_samples = max(SAMPLE_RATE_IN, int(window_s * SAMPLE_RATE_IN))
        self.interval_s = max(0.5, interval_s)
        self.min_rms = max(0.0, min_rms)
        self.end_silence_samples = max(1, int(utterance_end_s * SAMPLE_RATE_IN))
        self.min_utterance_samples = max(1, int(min_utterance_s * SAMPLE_RATE_IN))
        self.max_utterance_samples = max(SAMPLE_RATE_IN, int(max_utterance_s * SAMPLE_RATE_IN))
        self.prompt_suffix = prompt_suffix
        self.latest_text = ""
        self.latest_clean_text = ""
        self.latest_intent = ""
        self.latest_target = ""
        self.latest_prompt_suffix = prompt_suffix
        self.latest_ts = 0.0
        self.latest_event_id = 0
        self.suppress_until = 0.0
        self._suppress_lock = threading.Lock()
        self._recent_model_text: list[tuple[float, str]] = []
        self._recent_model_lock = threading.Lock()
        self._q: queue.Queue[Optional[np.ndarray]] = queue.Queue(maxsize=64)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="FunASRMirror", daemon=True)

    def start(self) -> None:
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        LOGGER.info(
            "[ASR] starting mirror model=%s device=%s jsonl=%s",
            self.model_name, self.device, self.jsonl_path,
        )
        self._thread.start()

    def wait_ready(self, timeout_s: float) -> bool:
        return self._ready.wait(timeout=max(0.0, timeout_s))

    def suppress_for(self, seconds: float, reason: str = "") -> None:
        until = time.monotonic() + max(0.0, seconds)
        with self._suppress_lock:
            self.suppress_until = max(self.suppress_until, until)
        if reason:
            LOGGER.debug("[ASR] suppressed %.1fs: %s", seconds, reason)

    def is_suppressed(self) -> bool:
        with self._suppress_lock:
            return time.monotonic() < self.suppress_until

    def note_model_text(self, text: str) -> None:
        text = _clean_asr_text(text)
        if not text:
            return
        now = time.monotonic()
        norm = _norm_match_text(text)
        if not norm:
            return
        with self._recent_model_lock:
            self._recent_model_text.append((now, norm))
            self._recent_model_text = [
                (ts, val) for ts, val in self._recent_model_text
                if now - ts <= 20.0
            ][-20:]

    def looks_like_model_echo(self, text: str) -> bool:
        norm = _norm_match_text(text)
        if len(norm) < 4:
            return True
        now = time.monotonic()
        with self._recent_model_lock:
            recent = [
                val for ts, val in self._recent_model_text
                if now - ts <= 20.0 and val
            ]
        for model_text in recent:
            if len(model_text) >= 4 and (norm in model_text or model_text in norm):
                return True
            if difflib.SequenceMatcher(None, norm, model_text).ratio() >= 0.62:
                return True
        return False

    def submit(self, audio_f32: np.ndarray) -> None:
        if (
            audio_f32 is None
            or audio_f32.size == 0
            or self._stop.is_set()
            or self.is_suppressed()
        ):
            return
        item = audio_f32.astype(np.float32, copy=True)
        try:
            self._q.put_nowait(item)
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.put_nowait(item)
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        self._thread.join(timeout=2.0)

    def _append_jsonl(self, record: dict[str, Any]) -> None:
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _run(self) -> None:
        try:
            from funasr import AutoModel
            import torch

            asr_device = self.device
            if asr_device == "auto":
                asr_device = "cuda:0" if torch.cuda.is_available() else "cpu"
            model = AutoModel(
                model=self.model_name,
                trust_remote_code=True,
                device=asr_device,
                disable_update=True,
            )
            self._ready.set()
            LOGGER.info("[ASR] model loaded")
        except Exception as e:
            LOGGER.exception("[ASR] model load failed: %s", e)
            self._ready.set()
            return

        speech_parts: list[np.ndarray] = []
        speech_samples = 0
        silence_samples = 0
        last_text = ""
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            if self.is_suppressed():
                if speech_parts:
                    silence_samples += item.size
                else:
                    last_text = ""
                continue

            chunk_rms = float(np.sqrt(np.mean(item * item))) if item.size else 0.0
            if chunk_rms >= self.min_rms:
                speech_parts.append(item)
                speech_samples += item.size
                silence_samples = 0
            elif speech_parts:
                speech_parts.append(item)
                speech_samples += item.size
                silence_samples += item.size
            else:
                continue

            if speech_samples < self.max_utterance_samples:
                if silence_samples < self.end_silence_samples:
                    continue
                if self.is_suppressed() and speech_samples < self.min_utterance_samples * 2:
                    continue
            if not speech_parts:
                continue

            buf = np.concatenate(speech_parts) if speech_parts else np.zeros(0, dtype=np.float32)
            speech_parts.clear()
            speech_samples = 0
            silence_samples = 0

            if buf.size < self.min_utterance_samples:
                continue
            rms = float(np.sqrt(np.mean(buf * buf))) if buf.size else 0.0
            if rms < self.min_rms:
                continue

            try:
                result = model.generate(
                    input=buf,
                    fs=SAMPLE_RATE_IN,
                    language=self.language,
                    use_itn=True,
                    batch_size_s=60,
                )
                raw_text = _extract_asr_text(result).strip()
                text = _clean_asr_text(raw_text)
            except Exception as e:
                LOGGER.warning("[ASR] recognize failed: %s", e)
                continue

            if not text or text == last_text or self.looks_like_model_echo(text):
                if text and self.looks_like_model_echo(text):
                    LOGGER.info("[ASR] drop likely model echo: %s", text)
                continue
            last_text = text
            self.latest_text = raw_text
            self.latest_clean_text = text
            self.latest_ts = time.time()
            intent, target, skill_suffix = _classify_skill(text)
            self.latest_intent = intent
            self.latest_target = target
            self.latest_prompt_suffix = skill_suffix or self.prompt_suffix
            self.latest_event_id += 1
            LOGGER.info("[ASR] user_text=%s intent=%s target=%s", text, intent or "-", target or "-")
            self._append_jsonl({
                "ts": self.latest_ts,
                "event_id": self.latest_event_id,
                "source": "esp32_mirror",
                "sample_rate": SAMPLE_RATE_IN,
                "window_s": round(buf.size / SAMPLE_RATE_IN, 3),
                "rms": rms,
                "text": text,
                "intent": intent,
                "target": target,
                "skill": intent,
                "raw_text": raw_text,
                "model": self.model_name,
                "language": self.language,
                "prompt_suffix": self.latest_prompt_suffix,
            })


class TimestampedRingBuffer:
    def __init__(self, capacity_s: float = RING_CAPACITY_S):
        self.capacity = int(capacity_s * SAMPLE_RATE_IN)
        self.buf = np.zeros(self.capacity, dtype=np.int16)
        self.ts_start_ms: Optional[float] = None
        self.n_samples: int = 0
        self.head_ts_ms: float = 0.0
        self._lock = asyncio.Lock()
        self.gap_fill_threshold_ms = 1.0
        self.seq_base: Optional[int] = None
        self.audio_ts_base_ms: Optional[float] = None
        self.last_seq: Optional[int] = None
        self.last_esp_ts_ms: Optional[int] = None
        self.last_drops: int = 0
        self.seq_gap_events: int = 0
        self.seq_gap_packets: int = 0
        self.esp_drop_delta_total: int = 0
        self.esp_ts_jitter_events: int = 0
        self.esp_ts_jitter_ms_total: float = 0.0

    async def write(self, ts_ms: int, pcm: np.ndarray) -> None:
        async with self._lock:
            if self.ts_start_ms is None:
                self.ts_start_ms = float(ts_ms)
                self.head_ts_ms = float(ts_ms)
            delta_ms = ts_ms - self.head_ts_ms
            if delta_ms > self.gap_fill_threshold_ms:
                gap_samples = int(round(delta_ms * SAMPLE_RATE_IN / 1000.0))
                if gap_samples > 0:
                    self._append(np.zeros(gap_samples, dtype=np.int16))
                    if gap_samples > SAMPLE_RATE_IN // 10:
                        LOGGER.warning("[RING] filled %dms silence gap", int(delta_ms))
            elif delta_ms < -40:
                LOGGER.debug("[RING] out-of-order pkt ts=%d head=%d drop",
                             ts_ms, int(self.head_ts_ms))
                return
            self._append(pcm)
            self.head_ts_ms = ts_ms + len(pcm) * 1000.0 / SAMPLE_RATE_IN

    async def write_packet(self, seq: int, esp_ts_ms: int, pcm: np.ndarray, drops: int) -> None:
        """Rebuild PC audio time from ESP32 seq; keep esp_ts_ms for diagnostics only."""
        if pcm.size == 0:
            return

        pkt_ms = len(pcm) * 1000.0 / SAMPLE_RATE_IN
        if self.seq_base is None:
            self.seq_base = seq
            self.audio_ts_base_ms = float(esp_ts_ms)
            self.last_seq = seq - 1
            self.last_esp_ts_ms = esp_ts_ms - int(round(pkt_ms))
            self.last_drops = drops

        assert self.seq_base is not None
        assert self.audio_ts_base_ms is not None

        if self.last_seq is not None:
            seq_delta = seq - self.last_seq
            if seq_delta <= 0:
                LOGGER.debug("[RING] duplicate/out-of-order seq=%d last=%d drop", seq, self.last_seq)
                return
            if seq_delta > 1:
                missing = seq_delta - 1
                self.seq_gap_events += 1
                self.seq_gap_packets += missing
                LOGGER.warning(
                    "[RING] seq gap: missing=%d seq=%d last=%d drops=%d",
                    missing, seq, self.last_seq, drops,
                )

            if self.last_esp_ts_ms is not None:
                esp_delta = esp_ts_ms - self.last_esp_ts_ms
                expected_delta = seq_delta * pkt_ms
                jitter_ms = esp_delta - expected_delta
                if abs(jitter_ms) >= 80.0:
                    self.esp_ts_jitter_events += 1
                    self.esp_ts_jitter_ms_total += abs(jitter_ms)
                    LOGGER.info(
                        "[RING] esp_ts diagnostic jitter=%dms seq_delta=%d esp_delta=%dms",
                        int(round(jitter_ms)), seq_delta, int(esp_delta),
                    )

        drop_delta = drops - self.last_drops
        if drop_delta < 0:
            drop_delta = drops
        if drop_delta > 0:
            self.esp_drop_delta_total += drop_delta
            LOGGER.warning("[RING] esp reported drops +%d total=%d", drop_delta, drops)

        self.last_seq = seq
        self.last_esp_ts_ms = esp_ts_ms
        self.last_drops = drops

        audio_ts_ms = self.audio_ts_base_ms + (seq - self.seq_base) * pkt_ms
        await self.write(int(round(audio_ts_ms)), pcm)

    def _append(self, samples: np.ndarray) -> None:
        n = len(samples)
        if n == 0:
            return
        if self.n_samples + n > self.capacity:
            drop = self.n_samples + n - self.capacity
            self.buf[: self.n_samples - drop] = self.buf[drop : self.n_samples]
            self.n_samples -= drop
            self.ts_start_ms += drop * 1000.0 / SAMPLE_RATE_IN  # type: ignore[operator]
        self.buf[self.n_samples : self.n_samples + n] = samples
        self.n_samples += n

    async def slice(self, t_start_ms: int, t_end_ms: int) -> np.ndarray:
        n_want = int(round((t_end_ms - t_start_ms) * SAMPLE_RATE_IN / 1000.0))
        out = np.zeros(n_want, dtype=np.int16)
        async with self._lock:
            if self.ts_start_ms is None or self.n_samples == 0:
                return out.astype(np.float32) / 32768.0
            have_start = self.ts_start_ms
            have_end = self.ts_start_ms + self.n_samples * 1000.0 / SAMPLE_RATE_IN
            ol_start = max(float(t_start_ms), have_start)
            ol_end = min(float(t_end_ms), have_end)
            if ol_end <= ol_start:
                return out.astype(np.float32) / 32768.0
            src_off = int(round((ol_start - have_start) * SAMPLE_RATE_IN / 1000.0))
            dst_off = int(round((ol_start - t_start_ms) * SAMPLE_RATE_IN / 1000.0))
            n_copy = int(round((ol_end - ol_start) * SAMPLE_RATE_IN / 1000.0))
            n_copy = min(n_copy, self.n_samples - src_off, n_want - dst_off)
            if n_copy > 0:
                out[dst_off : dst_off + n_copy] = self.buf[src_off : src_off + n_copy]
        return out.astype(np.float32) / 32768.0

    @property
    def latest_ts_ms(self) -> float:
        if self.ts_start_ms is None:
            return 0.0
        return self.ts_start_ms + self.n_samples * 1000.0 / SAMPLE_RATE_IN

    def diagnostics(self) -> dict:
        return {
            "clock": "seq*packet_duration; esp_ts_ms diagnostic only",
            "seq_base": self.seq_base,
            "last_seq": self.last_seq,
            "last_esp_ts_ms": self.last_esp_ts_ms,
            "seq_gap_events": self.seq_gap_events,
            "seq_gap_packets": self.seq_gap_packets,
            "esp_drop_delta_total": self.esp_drop_delta_total,
            "esp_ts_jitter_events": self.esp_ts_jitter_events,
            "esp_ts_jitter_ms_total": int(round(self.esp_ts_jitter_ms_total)),
        }


# ============================================================
# ESP32 reader / image fetcher（与 v5.2 完全一致）
# ============================================================

async def esp32_audio_reader(
    host: str, port: int, ring: TimestampedRingBuffer,
    stop_evt: asyncio.Event, stats: dict,
    live_rec=None,
) -> None:
    url = f"ws://{host}:{port}/ws_audio_v2"
    LOGGER.info("[ESP32] audio WS: %s", url)
    backoff = 1.0
    recv_count = 0
    last_log_ts = time.monotonic()

    while not stop_evt.is_set():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url, heartbeat=30, max_msg_size=0) as ws:
                    LOGGER.info("[ESP32] audio WS connected")
                    backoff = 1.0
                    async for msg in ws:
                        if stop_evt.is_set():
                            break
                        if msg.type != aiohttp.WSMsgType.BINARY:
                            continue
                        data = msg.data
                        if len(data) < ESP32_PKT_HDR:
                            continue
                        seq, ts_ms, n_samples, drops = struct.unpack(
                            "<IIHH", data[:ESP32_PKT_HDR]
                        )
                        body = data[ESP32_PKT_HDR : ESP32_PKT_HDR + n_samples * 2]
                        if len(body) != n_samples * 2:
                            continue
                        pcm = np.frombuffer(body, dtype=np.int16)
                        await ring.write_packet(seq=seq, esp_ts_ms=ts_ms, pcm=pcm, drops=drops)
                        # v6.6 移植：把原始 user PCM 喂给 LiveRecorder（丢包=缺失，不补零）
                        if live_rec is not None:
                            live_rec.feed_user_raw(pcm.astype(np.float32) / 32768.0)
                        recv_count += 1
                        stats["rx_pkts"] = recv_count
                        stats["seq"] = seq
                        stats["ts_ms"] = ts_ms
                        stats["esp_drops"] = drops
                        stats["seq_gap_events"] = ring.seq_gap_events
                        stats["seq_gap_packets"] = ring.seq_gap_packets
                        stats["esp_drop_delta_total"] = ring.esp_drop_delta_total
                        stats["esp_ts_jitter_events"] = ring.esp_ts_jitter_events
                        now = time.monotonic()
                        if now - last_log_ts >= 5.0:
                            LOGGER.info(
                                "[ESP32] rx=%d pkts seq=%d ts=%d drops=%d",
                                recv_count, seq, ts_ms, drops,
                            )
                            last_log_ts = now
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            LOGGER.warning("[ESP32] audio WS error: %s, retry in %.1fs", e, backoff)
            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 10.0)

    LOGGER.info("[ESP32] audio reader stopped")


async def esp32_capture_image(
    host: str, port: int, timeout_s: float = 1.0
) -> Optional[bytes]:
    url = f"http://{host}:{port}/capture"
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()
    except Exception as e:
        LOGGER.debug("[ESP32] /capture failed: %s", e)
        return None


# ============================================================
# TCP 图像通道客户端（与 HTTP /capture 对等的受控对比通道）
#   持久 TCP 连接到固件的 5000 端口，请求-响应：发 1 字节请求 → 收裸帧。
#   帧头(20B 小端)：magic(4,0x55AA55AA) frame_id(4) w(2) h(2) fmt(1) reserved(3) len(4)
#   只换"取图协议"，返回的 JPEG bytes 与 esp32_capture_image 完全一致，
#   下游(旋转/缓存/喂模型/录制)零改动，保证对比纯净。
# ============================================================

_TCP_IMG_MAGIC = 0x55AA55AA


class TcpImageClient:
    def __init__(self, host: str, port: int = 5000):
        self.host = host
        self.port = port
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()

    async def _ensure_conn(self, timeout_s: float) -> bool:
        if self._reader is not None and self._writer is not None and not self._writer.is_closing():
            return True
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=timeout_s)
            sock = self._writer.get_extra_info("socket")
            if sock is not None:
                import socket as _s
                sock.setsockopt(_s.IPPROTO_TCP, _s.TCP_NODELAY, 1)
            LOGGER.info("[TCP-IMG] connected to %s:%d", self.host, self.port)
            return True
        except Exception as e:
            LOGGER.debug("[TCP-IMG] connect failed: %s", e)
            await self._close()
            return False

    async def _close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    async def capture(self, timeout_s: float = 1.0) -> Optional[bytes]:
        async with self._lock:
            if not await self._ensure_conn(timeout_s):
                return None
            try:
                # 请求一帧
                self._writer.write(b"C")
                await self._writer.drain()
                # 读 20B 帧头
                hdr = await asyncio.wait_for(self._reader.readexactly(20), timeout=timeout_s)
                magic, frame_id, w, h = struct.unpack_from("<IIHH", hdr, 0)
                # fmt(1) reserved(3) 跳过，len 在偏移 16
                (length,) = struct.unpack_from("<I", hdr, 16)
                if magic != _TCP_IMG_MAGIC:
                    LOGGER.warning("[TCP-IMG] bad magic 0x%08x, resync by reconnect", magic)
                    await self._close()
                    return None
                if length == 0:
                    return None  # 固件没抓到
                # sanity：HD JPEG 合理范围内；异常 length 视为流错位，重连而非盲读
                if length < 0 or length > 4 * 1024 * 1024:
                    LOGGER.warning("[TCP-IMG] insane len=%d (w=%d h=%d), reconnect", length, w, h)
                    await self._close()
                    return None
                data = await asyncio.wait_for(self._reader.readexactly(length), timeout=timeout_s)
                return bytes(data)
            except Exception as e:
                LOGGER.debug("[TCP-IMG] capture failed: %s", e)
                await self._close()
                return None


# 全局 TCP 图像客户端（按需创建）
_tcp_image_client: Optional[TcpImageClient] = None


async def capture_image_dispatch(args) -> Optional[bytes]:
    """按 --image-transport 选择 HTTP /capture 或 TCP 5000 取图。返回 JPEG bytes。"""
    global _tcp_image_client
    if getattr(args, "image_transport", "http") == "tcp":
        if _tcp_image_client is None:
            _tcp_image_client = TcpImageClient(args.esp32_host, args.image_tcp_port)
        return await _tcp_image_client.capture(timeout_s=args.image_timeout_s)
    else:
        return await esp32_capture_image(
            args.esp32_host, args.esp32_port, timeout_s=args.image_timeout_s)


# ============================================================
# EchoGate（与 v5.2 完全一致）
# ============================================================

def _pcm_rms(pcm_f32: np.ndarray) -> float:
    if pcm_f32.size == 0:
        return 0.0
    pcm = pcm_f32.astype(np.float32, copy=False)
    return float(np.sqrt(np.mean(pcm * pcm)))


class AudioFirstImageState:
    """Shared state for the background image cache worker."""

    def __init__(self) -> None:
        now = time.monotonic()
        self.lock = asyncio.Lock()
        self.last_image_jpeg: Optional[bytes] = None
        self.image_seq = 0
        self.last_image_mono = 0.0
        self.last_capture_mono = 0.0
        self.capture_inflight = False
        self.audio_active_until_mono = now + 1.0
        self.last_audio_rms = 0.0
        self.last_ring_behind_ms = 0
        self.last_chunk_idx = 0
        self.timeout_drop_pending = False  # 取图超时且开了drop开关:下一个chunk不带图

    def _image_age_ms_unlocked(self) -> int:
        if self.last_image_mono <= 0:
            return -1
        return int((time.monotonic() - self.last_image_mono) * 1000)

    async def note_audio(
        self,
        audio_rms: float,
        is_user_speech: bool,
        speech_hold_s: float,
        ring_behind_ms: int,
        chunk_idx: int,
    ) -> None:
        now = time.monotonic()
        async with self.lock:
            self.last_audio_rms = audio_rms
            self.last_ring_behind_ms = ring_behind_ms
            self.last_chunk_idx = chunk_idx
            if is_user_speech:
                self.audio_active_until_mono = max(
                    self.audio_active_until_mono,
                    now + speech_hold_s,
                )

    async def should_pause_image(self, args: argparse.Namespace) -> tuple[bool, str]:
        now = time.monotonic()
        async with self.lock:
            if self.last_ring_behind_ms > args.image_pause_backlog_ms:
                return True, "audio_backlog"
            if self.capture_inflight:
                return True, "inflight"
            # 说话期间：不再完全停抓图（否则"对准目标+开口提问"时图停 2-3s，
            #   模型拿到的是开口前还在移动的旧图）。改为降频——说话时按较长间隔
            #   image_speaking_interval_s 抓图，保证提问时画面仍在更新；
            #   不说话时用正常的 image_min_interval_s。
            speaking = now < self.audio_active_until_mono
            interval = args.image_speaking_interval_s if speaking else args.image_min_interval_s
            if now - self.last_capture_mono < interval:
                return True, ("speaking_throttle" if speaking else "min_interval")
            return False, ""

    async def begin_capture(self) -> None:
        async with self.lock:
            self.capture_inflight = True
            self.last_capture_mono = time.monotonic()

    async def finish_capture(self, img_jpeg: Optional[bytes]) -> tuple[int, int]:
        async with self.lock:
            self.capture_inflight = False
            if img_jpeg:
                self.last_image_jpeg = img_jpeg
                self.image_seq += 1
                self.last_image_mono = time.monotonic()
            return self.image_seq, self._image_age_ms_unlocked()

    async def cancel_capture(self) -> None:
        async with self.lock:
            self.capture_inflight = False

    async def mark_timeout_drop(self) -> None:
        """取图超时且开了 --drop-image-on-timeout:标记下一个 chunk 不带图(只发音频)。"""
        async with self.lock:
            self.timeout_drop_pending = True

    async def consume_timeout_drop(self) -> bool:
        """发 chunk 时取用并清除该标记(一次性,只影响紧随的这一个 chunk)。"""
        async with self.lock:
            v = self.timeout_drop_pending
            self.timeout_drop_pending = False
            return v

    async def get_image(self) -> tuple[Optional[bytes], int, int]:
        async with self.lock:
            return (
                self.last_image_jpeg,
                self.image_seq,
                self._image_age_ms_unlocked(),
            )


async def audio_first_image_cache_loop(
    args: argparse.Namespace,
    image_state: AudioFirstImageState,
    stop_evt: asyncio.Event,
    stats: dict[str, Any],
) -> None:
    if not args.use_image:
        return

    while not stop_evt.is_set():
        await asyncio.sleep(max(0.02, args.image_poll_s))
        paused, reason = await image_state.should_pause_image(args)
        if paused:
            key = f"image_skip_{reason}"
            if key in stats:
                stats[key] += 1
            continue

        await image_state.begin_capture()
        stats["image_attempts"] += 1
        t0 = time.monotonic()
        capture_task = asyncio.create_task(
            capture_image_dispatch(args)
        )
        try:
            while not capture_task.done():
                await asyncio.sleep(max(0.01, args.image_abort_poll_s))
                # (1) 取图耗时超过 image_abort_ms:判定这张抓不到/太慢,立即放弃,放行音频。
                #     不等待、不重试。默认沿用上一帧缓存图(方案a);若开 --drop-image-on-timeout
                #     则本轮连图都不带(方案b),只发音频。
                elapsed_now_ms = int((time.monotonic() - t0) * 1000)
                if args.image_abort_ms > 0 and elapsed_now_ms >= args.image_abort_ms:
                    capture_task.cancel()
                    try:
                        await capture_task
                    except asyncio.CancelledError:
                        pass
                    await image_state.cancel_capture()
                    if args.drop_image_on_timeout:
                        await image_state.mark_timeout_drop()
                    stats["image_abort_timeout"] = stats.get("image_abort_timeout", 0) + 1
                    LOGGER.info("[IMG] capture aborted: timeout %dms (drop_image=%s)",
                                elapsed_now_ms, args.drop_image_on_timeout)
                    break
                # (2) 用户说话 / 音频积压:原有的让位逻辑。
                paused, reason = await image_state.should_pause_image(args)
                if reason in ("user_speech", "audio_backlog"):
                    capture_task.cancel()
                    try:
                        await capture_task
                    except asyncio.CancelledError:
                        pass
                    await image_state.cancel_capture()
                    key = f"image_abort_{reason}"
                    if key in stats:
                        stats[key] += 1
                    LOGGER.info("[IMG] capture aborted: %s", reason)
                    break
            else:
                # Unreachable; the loop exits via done() and continues below.
                pass

            if capture_task.cancelled():
                continue
            if not capture_task.done():
                continue

            img_jpeg = capture_task.result()
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            stats["image_last_capture_ms"] = elapsed_ms
            if elapsed_ms >= args.image_slow_ms:
                stats["image_slow"] += 1
                LOGGER.info("[IMG] slow capture: %dms", elapsed_ms)
            if img_jpeg and args.rotate % 360 != 0:
                _rot_t0 = time.monotonic()
                img_jpeg = rotate_jpeg(
                    img_jpeg,
                    args.rotate,
                    quality=args.jpeg_quality,
                )
                _rot_ms = int((time.monotonic() - _rot_t0) * 1000)
                # 旋转在主事件循环同步执行,这段时间事件循环无法读音频 socket。
                # 若 _rot_ms 偏大(几十ms+)且断连时刻与之吻合,则旋转是音频断连嫌疑。
                LOGGER.info("[IMG] rotate took %dms (blocks event loop)", _rot_ms)
            seq, age_ms = await image_state.finish_capture(img_jpeg)
            if img_jpeg:
                stats["image_ok"] += 1
                LOGGER.info("[IMG] cache updated seq=%d age=%dms", seq, age_ms)
            else:
                stats["image_fail"] += 1
        except asyncio.CancelledError:
            capture_task.cancel()
            await image_state.cancel_capture()
            raise
        except Exception as e:
            capture_task.cancel()
            await image_state.cancel_capture()
            stats["image_fail"] += 1
            LOGGER.warning("[IMG] cache worker error: %s", e)


class KvPruneTracker:
    """逐轮跟踪 gateway result 里的 kv_cache_length,检测 sliding-window prune 并计数。

    移植自 web 前端 duplex-session.js 的 KV 检测逻辑:gateway 每轮 decode 返回的
    result 带 kv_cache_length 字段(gateway 纯透传 worker 的返回体,字段与 web 前端一致)。
    发现本轮 kv 比上一轮小 => 判定发生 sliding-window prune,写进 logs/run_<ts>.txt。
    目的:用「prune 次数 / kv 长度」这个硬指标替代「挂钟时间」来刻画复读退化。
    """

    def __init__(self, logger, max_kv: int = 8192):
        self.logger = logger
        self.max_kv = max_kv
        self._last_kv = 0          # 对应 JS 的 this._lastKvCacheLength
        self.prune_count = 0       # 累计 prune 次数(硬指标)
        self.last_kv_len = 0       # 最近一次 kv 长度,供 [RX] 行/快照读取

    def update(self, result: dict) -> Optional[dict]:
        """每轮收到 gateway 的 result 后调用一次。

        返回:检测到 sliding-window prune 时返回一个 dict(供前端 emit),否则 None。
        """
        cur_kv = result.get("kv_cache_length")
        if cur_kv is None or cur_kv <= 0:
            return None  # 该轮无有效 kv(如纯 listen 轮),跳过
        event = None
        if cur_kv >= self.max_kv:
            self.logger.info("[KV] \u26a0 KV cache (%s) reached limit %s.",
                             f"{cur_kv:,}", f"{self.max_kv:,}")
        elif self._last_kv > 0 and cur_kv < self._last_kv:
            self.prune_count += 1
            prev = self._last_kv
            self.logger.info("[KV] \u2702 prune #%d (sliding window): %s \u2192 %s  (shrunk %s)",
                             self.prune_count, f"{prev:,}", f"{cur_kv:,}", f"{prev - cur_kv:,}")
            event = {
                "type": "kv_prune",
                "count": self.prune_count,
                "prev": prev,
                "cur": cur_kv,
                "shrunk": prev - cur_kv,
            }
        self._last_kv = cur_kv
        self.last_kv_len = cur_kv
        return event

    def snapshot(self) -> str:
        return f"prune_count={self.prune_count}, cur_kv={self.last_kv_len:,}"


class EchoGate:
    def __init__(self, mode: str = "noise", tail_ms: int = 600,
                 noise_level_db: float = -60.0):
        assert mode in ("mute", "noise", "off"), mode
        self.mode = mode
        self.tail_ms = tail_ms
        self.noise_amp = 10 ** (noise_level_db / 20.0)
        self.is_speaking = False
        self._speak_until_ts = 0.0
        self._rng = np.random.default_rng(20260418)

    def update(self, is_listen: bool, end_of_turn: bool) -> None:
        now_ms = time.monotonic() * 1000.0
        if not is_listen:
            self.is_speaking = True
            self._speak_until_ts = now_ms + self.tail_ms
        else:
            self.is_speaking = False

    def should_mute(self) -> bool:
        if self.mode == "off":
            return False
        if self.is_speaking:
            return True
        return time.monotonic() * 1000.0 < self._speak_until_ts

    def apply(self, pcm_f32: np.ndarray) -> np.ndarray:
        if not self.should_mute():
            return pcm_f32
        if self.mode == "mute":
            return np.zeros_like(pcm_f32)
        if self.mode == "noise":
            return self._rng.standard_normal(pcm_f32.shape).astype(np.float32) * self.noise_amp
        return pcm_f32


# ============================================================
# CallbackSpeakerPlayer（与 v5.2 完全一致）
# ============================================================

def _resample_linear(samples: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """线性插值重采样，忠实复制 gateway 前端 duplex-utils.js 的 resampleAudio。

    JS 原实现：
        ratio = fromRate / toRate
        newLen = round(len / ratio)
        result[i] = samples[floor] * (1-frac) + samples[next] * frac
    这里用向量化 numpy 复现同样的算法（逐样本等价），避免 Python 循环。
    """
    if from_rate == to_rate or samples.size == 0:
        return np.ascontiguousarray(samples, dtype=np.float32).reshape(-1)
    ratio = from_rate / to_rate
    new_len = int(round(samples.size / ratio))
    if new_len <= 0:
        return np.zeros(0, dtype=np.float32)
    src = samples.astype(np.float32, copy=False).reshape(-1)
    # srcIdx = i * ratio; floor / frac / next(clamp 到末尾) —— 与 JS 完全一致
    idx = np.arange(new_len, dtype=np.float64) * ratio
    floor = np.floor(idx).astype(np.int64)
    frac = (idx - floor).astype(np.float32)
    nxt = np.minimum(floor + 1, src.size - 1)
    floor = np.minimum(floor, src.size - 1)
    out = src[floor] * (1.0 - frac) + src[nxt] * frac
    return np.ascontiguousarray(out, dtype=np.float32)


class CallbackSpeakerPlayer:
    """忠实复制 gateway 前端 audio-player.js 语义的播放器（无上界 FIFO）。

    对照 audio-player.js 的不吞音机制：
      - 无上界 FIFO（deque of np.float32 块），enqueue 永不丢样本
        （对应 JS _sources 数组无上界、每 chunk 独立排程、从不丢弃）
      - _cb 欠载时补零且【读位置不动、不消费任何待播数据】
        （对应 JS _nextTime 只在 <now 时追平、新 chunk 从 _nextTime 无缝续播）
      - 起播一次性 prebuffer 门控（默认 200ms = JS getPlaybackDelayMs）

    方案 A：不接 begin/end_turn 的每-turn 门控，起播只 gate 一次（全局）。
    begin_turn/end_turn 方法保留（幂等、无害），便于日后切到方案 B 时无需改类。

    接口签名与旧实现完全一致：enqueue / flush / start / stop / stats 不变，
    上层调用零改动。构造新增可选参数 prebuffer_ms（默认 200），旧调用不传也可。

    线程模型：enqueue 从 asyncio 线程调用，_cb 从 PortAudio 回调线程调用，占锁极短。
    """

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE_OUT,   # AI 音频原始率（24000），入队时会被重采样到设备率
        ring_seconds: float = 5.0,            # 保留形参以兼容原构造调用；FIFO 无上界，仅日志用
        device: Optional[int] = None,
        latency: str = "low",
        prebuffer_ms: float = 200.0,          # = JS getPlaybackDelayMs 默认值
        device_sr: int = 0,                   # 声卡播放率；0=自动查询设备原生率（推荐）
        hostapi: str = "wasapi",              # 优先音频主机 API；默认 WASAPI（现代低延迟，避开 MME 丢帧）
    ):
        if sd is None:
            raise RuntimeError("sounddevice 未安装，请 `pip install sounddevice`.")
        # 源率 = AI 音频采样率（24000）；设备率 = 声卡实际播放率（如 44100）。
        # 先按 hostapi 偏好选输出设备（默认 WASAPI，绕开默认 MME 的实时丢帧），
        # 再查该设备原生率，enqueue 时把 24000 线性插值重采样到设备率（对齐 gateway）。
        self._source_sr = sample_rate
        self._device = self._resolve_output_device(device, hostapi)
        self._device_sr = self._resolve_device_sr(self._device, device_sr, sample_rate)
        # self.sample_rate 对外表示"队列/播放所用采样率"= 设备率（stats 的 ms 换算依赖它）
        self.sample_rate = self._device_sr
        self._latency = latency
        self._stream: Optional[sd.OutputStream] = None
        self._lock = threading.Lock()

        # 无上界 FIFO：每个元素是一块 np.float32 一维数组（设备率）；_head_off 是队首块已消费偏移
        self._q: "collections.deque[np.ndarray]" = collections.deque()
        self._head_off = 0
        self._size = 0                      # 队列中待播样本总数（设备率）

        # 起播一次性门控（对应 JS 的 _playing / _delayTimer），按设备率算样本数
        self._prebuffer_samples = int(max(0.0, prebuffer_ms) * self._device_sr / 1000.0)
        self._turn_active = False
        self._armed = (self._prebuffer_samples <= 0)  # True=可消费；False=攒够 prebuffer 前不消费

        # 兼容日志：估算等效 ring 秒数（仅用于 start 日志展示）
        self.ring_cap = max(int(ring_seconds * self._device_sr), self._device_sr // 10)

        # stats（字段名完全沿用原实现，stats() 输出与上层日志/监控不变）
        self._underrun_events = 0
        self._zero_filled_samples = 0
        self._total_written_samples = 0
        self._total_played_samples = 0
        self._max_pending_samples = 0
        self._dropped_oldest_samples = 0    # FIFO 永不丢，仅 flush(barge) 时累加
        self._first_dac_ts = 0.0
        self._start_ts = 0.0
        # DAC 输出探针缓冲（None=关闭）。开启后 _cb 会把送声卡的样本存这里，stop 时落盘。
        self._dac_probe: Optional[list] = None
        # RAW 输入探针缓冲（None=关闭）。开启后 enqueue 把上游原始块(源率)连续存这里。
        self._raw_probe: Optional[list] = None

    # ---- callback (PortAudio thread) ----
    @staticmethod
    def _resolve_output_device(device, hostapi_pref: str):
        """选输出设备。若用户显式给了 device 则用它；否则按 hostapi_pref('wasapi'/'mme'/'')
        在设备列表里找该 hostapi 的默认输出设备。找不到返回 None(=系统默认，通常 MME)。

        绕开默认 MME、改走 WASAPI 是关键：MME 是最老的 Windows 音频 API、延迟高、
        实时播放易丢帧；WASAPI 是现代低延迟路径，与 gateway 前端(浏览器→WASAPI)一致。
        """
        if device is not None:
            return device
        pref = (hostapi_pref or "").strip().lower()
        if not pref or pref == "default":
            return None
        try:
            hostapis = sd.query_hostapis()
            for hi, h in enumerate(hostapis):
                if pref in h["name"].lower():
                    # 优先该 hostapi 的默认输出设备
                    dflt = h.get("default_output_device", -1)
                    if isinstance(dflt, int) and dflt >= 0:
                        info = sd.query_devices(dflt)
                        if info.get("max_output_channels", 0) > 0:
                            LOGGER.info("[SPK] 选用 %s 输出设备 [%d] %s",
                                        h["name"], dflt, info.get("name"))
                            return dflt
                    # 否则遍历该 hostapi 下第一个可输出设备
                    for di in h.get("devices", []):
                        info = sd.query_devices(di)
                        if info.get("max_output_channels", 0) > 0:
                            LOGGER.info("[SPK] 选用 %s 输出设备 [%d] %s",
                                        h["name"], di, info.get("name"))
                            return di
            LOGGER.warning("[SPK] 未找到 %s 输出设备，回退系统默认", pref)
        except Exception as e:
            LOGGER.warning("[SPK] 解析 hostapi 失败(%r)，回退系统默认", e)
        return None

    @staticmethod
    def _resolve_device_sr(device, device_sr: int, fallback: int) -> int:
        """确定声卡播放率：显式 device_sr>0 优先；否则查询设备原生率；查询失败回退到 fallback。"""
        if device_sr and device_sr > 0:
            return int(device_sr)
        try:
            info = sd.query_devices(device, "output") if device is not None \
                else sd.query_devices(kind="output")
            native = int(round(float(info["default_samplerate"])))
            if native > 0:
                LOGGER.info("[SPK] 设备原生采样率 = %dHz（AI 音频 %dHz 将重采样到此率）",
                            native, fallback)
                return native
        except Exception as e:
            LOGGER.warning("[SPK] 查询设备采样率失败(%r)，回退用 %dHz", e, fallback)
        return int(fallback)

    def _cb(self, outdata, frames, time_info, status):  # noqa: ARG002
        if status and status.output_underflow:
            self._underrun_events += 1

        with self._lock:
            # 门控：起播前未攒够 prebuffer 则整帧补零、【不消费】
            if not self._armed:
                if self._size >= self._prebuffer_samples and self._size > 0:
                    self._armed = True
                else:
                    outdata[:, 0] = 0.0
                    self._zero_filled_samples += frames
                    return

            need = frames
            filled = 0
            while need > 0 and self._q:
                blk = self._q[0]
                avail = blk.shape[0] - self._head_off
                take = avail if avail < need else need
                outdata[filled:filled + take, 0] = blk[self._head_off:self._head_off + take]
                self._head_off += take
                filled += take
                need -= take
                self._size -= take
                self._total_played_samples += take
                if self._head_off >= blk.shape[0]:
                    self._q.popleft()
                    self._head_off = 0

            if filled > 0 and self._first_dac_ts == 0.0:
                self._first_dac_ts = time.perf_counter()

            # 欠载：补零，且【不消费任何待播数据、读位置不动】—— 不吞音的关键
            if need > 0:
                outdata[filled:, 0] = 0.0
                self._zero_filled_samples += need

            # ── 诊断探针：录下 callback 实际送给声卡的样本（设备率）──
            # 与 live_ai.wav（enqueue 入参，源率）是两个观测点。跑完对比两份 wav，
            # 若某段字在 enqueue 有、在此处无 → 丢在 callback 消费；若此处有、耳朵无
            # → 丢在声卡/驱动。仅在 _dac_probe 开启时记录，默认关闭、零开销。
            if self._dac_probe is not None:
                try:
                    self._dac_probe.append(outdata[:, 0].copy())
                except Exception:
                    pass

    def flush(self) -> int:
        """丢弃 FIFO 内所有未播样本（用户 barge 打断用）。返回被丢弃的样本数。"""
        with self._lock:
            dropped = self._size
            self._q.clear()
            self._head_off = 0
            self._size = 0
            if dropped > 0:
                self._dropped_oldest_samples += dropped
        return dropped

    def enqueue(self, pcm_f32: np.ndarray) -> None:
        """非阻塞入队；无上界，永不丢样本（对应 JS 从不丢 chunk）。

        入参是 AI 音频原始率(_source_sr, 24000)。这里先线性插值重采样到设备率
        (_device_sr, 如 44100)再入队——完全对齐 gateway 前端的 resampleAudio。
        注意：录音钩子(recorder_live.attach_to_player)包装的是本方法，录的是它的
        入参 pcm_f32(即原始 24000)，不受此处内部重采样影响，录音保持干净。
        """
        if pcm_f32 is None or pcm_f32.size == 0:
            return
        # ── 诊断探针：把上游发来的原始块(重采样前, 源率)连续拼接留存 ──
        # 不按时间戳归位、不补零，就是把每个 audio_only 块首尾相接。
        # 这排除了 live_ai(归位填静音) 和 live_dac(_cb补零) 两个污染源，
        # 是"上游到底发了什么内容"的干净证据。仅 _raw_probe 开启时记录。
        if self._raw_probe is not None:
            try:
                self._raw_probe.append(
                    np.ascontiguousarray(pcm_f32, dtype=np.float32).reshape(-1).copy()
                )
            except Exception:
                pass
        # 重采样到设备率（源率==设备率时 _resample_linear 直接返回原样）
        blk = _resample_linear(pcm_f32, self._source_sr, self._device_sr)
        if blk.size == 0:
            return
        n = blk.shape[0]
        with self._lock:
            self._q.append(blk)
            self._size += n
            self._total_written_samples += n
            if self._size > self._max_pending_samples:
                self._max_pending_samples = self._size

    # ---- turn 语义（方案 A 下未被上层调用；保留供方案 B 使用）----
    def begin_turn(self) -> None:
        """新 SPEAK turn 起点：清空残留、重新进入起播门控。幂等。
        对应 JS beginTurn→_stopAllSources（不积压前提下砍残留安全）。"""
        with self._lock:
            if self._turn_active:
                return
            self._turn_active = True
            self._q.clear()
            self._head_off = 0
            self._size = 0
            self._armed = (self._prebuffer_samples <= 0)

    def end_turn(self) -> None:
        """turn 结束：立即解除门控让积压起播；不清队列，让尾音自然播完。
        对应 JS endTurn。"""
        with self._lock:
            self._turn_active = False
            self._armed = True

    def start(self) -> None:
        self._start_ts = time.perf_counter()
        self._armed = (self._prebuffer_samples <= 0)

        def _open(dev, sr):
            s = sd.OutputStream(
                samplerate=sr, channels=1, dtype="float32", blocksize=0,
                latency=self._latency, device=dev, callback=self._cb,
            )
            s.start()
            return s

        try:
            self._stream = _open(self._device, self.sample_rate)
        except Exception as e:
            # WASAPI 共享模式对采样率/设备较敏感，开流失败则回退系统默认设备重开，
            # 保证一定有声音输出（宁可回到 MME，也不能完全静音）。
            LOGGER.warning("[SPK] 首选设备(dev=%s sr=%d)开流失败(%r)，回退系统默认设备",
                           self._device, self.sample_rate, e)
            self._device = None
            self._device_sr = self._resolve_device_sr(None, 0, self._source_sr)
            self.sample_rate = self._device_sr
            # prebuffer 样本数按新设备率重算（保持 200ms 语义）
            if self._prebuffer_samples > 0:
                self._prebuffer_samples = int(200.0 * self._device_sr / 1000.0)
            self._stream = _open(None, self.sample_rate)

        LOGGER.info(
            "[SPK] callback stream started (FIFO): dev=%s src_sr=%d dev_sr=%d prebuffer=%.0fms reported_lat=%.0fms",
            self._device, self._source_sr, self._device_sr,
            self._prebuffer_samples * 1000.0 / self.sample_rate,
            float(self._stream.latency) * 1000.0,
        )

    def stop(self, drain_timeout_s: float = 3.0) -> None:
        if self._stream is None:
            return
        t0 = time.monotonic()
        while True:
            with self._lock:
                remain = self._size
            if remain <= 0 or (time.monotonic() - t0) > drain_timeout_s:
                break
            time.sleep(0.05)
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as e:
            LOGGER.debug("[SPK] stop: %s", e)
        self._stream = None

    def stats(self) -> dict:
        with self._lock:
            pending = self._size
            max_pending = self._max_pending_samples
            written = self._total_written_samples
            played = self._total_played_samples
            underrun = self._underrun_events
            zeroed = self._zero_filled_samples
            dropped = self._dropped_oldest_samples
            first_dac_ms = (
                int((self._first_dac_ts - self._start_ts) * 1000.0)
                if self._first_dac_ts > 0 else -1
            )
        to_ms = 1000.0 / self.sample_rate
        return {
            "pending_ms": int(pending * to_ms),
            "max_pending_ms": int(max_pending * to_ms),
            "written_s": round(written / self.sample_rate, 2),
            "played_s": round(played / self.sample_rate, 2),
            "underrun_events": underrun,
            "zero_filled_ms": int(zeroed * to_ms),
            "dropped_oldest_ms": int(dropped * to_ms),
            "first_dac_ms": first_dac_ms,
        }

    def enable_dac_probe(self) -> None:
        """开启 DAC 输出探针：之后 _cb 送声卡的样本会被记录，供 dump_dac_probe 落盘。"""
        with self._lock:
            self._dac_probe = []

    def enable_raw_probe(self) -> None:
        """开启 RAW 输入探针：之后 enqueue 收到的上游原始块(源率)连续记录，供 dump_raw_probe 落盘。"""
        with self._lock:
            self._raw_probe = []

    def dump_raw_probe(self, path: str) -> None:
        """把探针记录的上游原始块连续拼接落盘为 wav（源率 24000，无归位无补零）。未开启则跳过。"""
        with self._lock:
            if not self._raw_probe:
                return
            buf = np.concatenate(self._raw_probe) if self._raw_probe else np.zeros(0, np.float32)
        try:
            import wave as _wave
            i16 = np.clip(buf * 32768.0, -32768, 32767).astype(np.int16)
            with _wave.open(path, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(self._source_sr)
                w.writeframes(i16.tobytes())
            LOGGER.info("[SPK] RAW 探针已落盘: %s (%.1fs @ %dHz, 上游原始块连续拼接)",
                        path, buf.size / self._source_sr, self._source_sr)
        except Exception as e:
            LOGGER.warning("[SPK] RAW 探针落盘失败: %s", e)

    def dump_dac_probe(self, path: str) -> None:
        """把探针记录的 callback 输出落盘为 wav（设备率）。未开启则跳过。"""
        with self._lock:
            if not self._dac_probe:
                return
            buf = np.concatenate(self._dac_probe) if self._dac_probe else np.zeros(0, np.float32)
        try:
            import wave as _wave
            i16 = np.clip(buf * 32768.0, -32768, 32767).astype(np.int16)
            with _wave.open(path, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(self._device_sr)
                w.writeframes(i16.tobytes())
            LOGGER.info("[SPK] DAC 探针已落盘: %s (%.1fs @ %dHz)",
                        path, buf.size / self._device_sr, self._device_sr)
        except Exception as e:
            LOGGER.warning("[SPK] DAC 探针落盘失败: %s", e)


async def player_monitor_task(
    player: CallbackSpeakerPlayer,
    interval_s: float,
    stop_evt: asyncio.Event,
) -> None:
    if interval_s <= 0:
        return
    last_dac_logged = False
    while not stop_evt.is_set():
        try:
            await asyncio.wait_for(stop_evt.wait(), timeout=interval_s)
            break
        except asyncio.TimeoutError:
            pass
        st = player.stats()
        if not last_dac_logged and st["first_dac_ms"] >= 0:
            LOGGER.info("[SPK] first DAC @ %dms since start", st["first_dac_ms"])
            last_dac_logged = True
        LOGGER.info(
            "[SPK] pending=%dms max=%dms written=%.1fs played=%.1fs "
            "underrun=%d zero=%dms dropped=%dms",
            st["pending_ms"], st["max_pending_ms"],
            st["written_s"], st["played_s"],
            st["underrun_events"], st["zero_filled_ms"], st["dropped_oldest_ms"],
        )


# ============================================================
# SessionRecorder（v6：新增 AI 音频时间线 + 字幕时间线）
# ============================================================

class SessionRecorder:
    def __init__(self, root_dir: Path, enabled: bool, meta: dict):
        self.enabled = enabled
        if not enabled:
            self.dir: Optional[Path] = None
            return
        ts_tag = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = root_dir / ts_tag
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "images").mkdir(exist_ok=True)

        self._meta = dict(meta)
        self._meta["session_tag"] = ts_tag
        self._meta["start_time"] = _dt.datetime.now().isoformat()
        self._write_meta()

        self._events = (self.dir / "events.jsonl").open("w", encoding="utf-8")
        self._user_raw = (self.dir / "user_raw.pcm").open("wb")
        self._user_sent = (self.dir / "user_sent.pcm").open("wb")
        self._ai_audio = (self.dir / "ai_audio.pcm").open("wb")
        self._transcript = (self.dir / "transcript.txt").open("w", encoding="utf-8")
        self._transcript.write(
            f"# Session {ts_tag}\n# prompt: {meta.get('prompt', '')}\n\n"
        )
        self._transcript.flush()
        # v6 新增
        self._ai_timing = (self.dir / "ai_audio_timeline.jsonl").open(
            "w", encoding="utf-8"
        )
        self._subtitles = (self.dir / "subtitles.jsonl").open(
            "w", encoding="utf-8"
        )
        self.session_start_wall: Optional[float] = None

        self.stats = {
            "chunks_sent": 0,
            "chunks_muted": 0,
            "images_sent": 0,
            "results_received": 0,
            "turns": 0,
            "user_raw_ms": 0,
            "ai_audio_ms": 0,
        }

    def _write_meta(self) -> None:
        if not self.enabled or self.dir is None:
            return
        with (self.dir / "meta.json").open("w", encoding="utf-8") as f:
            json.dump(self._meta, f, ensure_ascii=False, indent=2)

    def _emit(self, event: dict) -> None:
        if not self.enabled:
            return
        event["t"] = time.time()
        self._events.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._events.flush()

    def mark_session_start(self, wall: float) -> None:
        if not self.enabled:
            return
        self.session_start_wall = wall
        self._meta["session_start_wall"] = wall
        self._write_meta()

    def log_chunk(
        self,
        idx: int,
        ts_offset_ms: int,
        user_raw_f32: np.ndarray,
        user_sent_f32: np.ndarray,
        image_jpeg: Optional[bytes],
        muted: bool,
        ring_behind_ms: int,
    ) -> None:
        if not self.enabled:
            return
        user_raw_i16 = np.clip(user_raw_f32 * 32768.0, -32768, 32767).astype(np.int16)
        user_sent_i16 = np.clip(user_sent_f32 * 32768.0, -32768, 32767).astype(np.int16)
        self._user_raw.write(user_raw_i16.tobytes())
        self._user_sent.write(user_sent_i16.tobytes())
        img_path = None
        if image_jpeg is not None and self.dir is not None:
            img_path = f"images/img_{idx:05d}.jpg"
            (self.dir / img_path).write_bytes(image_jpeg)
            self.stats["images_sent"] += 1
        self.stats["chunks_sent"] += 1
        if muted:
            self.stats["chunks_muted"] += 1
        self.stats["user_raw_ms"] += int(len(user_raw_f32) * 1000 / SAMPLE_RATE_IN)
        self._emit({
            "kind": "chunk_sent",
            "idx": idx,
            "ts_offset_ms": ts_offset_ms,
            "muted": muted,
            "ring_behind_ms": ring_behind_ms,
            "img": img_path,
        })

    def log_result(
        self,
        is_listen: bool,
        end_of_turn: bool,
        text: str,
        ai_audio_f32: Optional[np.ndarray],
        turn_idx: int,
        player_pending_ms: int = -1,
    ) -> None:
        if not self.enabled:
            return
        self.stats["results_received"] += 1
        if ai_audio_f32 is not None and ai_audio_f32.size > 0:
            self._ai_audio.write(ai_audio_f32.astype(np.float32).tobytes())
            self.stats["ai_audio_ms"] += int(
                ai_audio_f32.size * 1000 / SAMPLE_RATE_OUT
            )
        if end_of_turn:
            self.stats["turns"] += 1
        self._emit({
            "kind": "result",
            "is_listen": is_listen,
            "end_of_turn": end_of_turn,
            "text": text,
            "ai_audio_samples": int(ai_audio_f32.size) if ai_audio_f32 is not None else 0,
            "turn_idx": turn_idx,
            "player_pending_ms": player_pending_ms,
        })

    def log_ai_audio_timing(self, session_ts_ms: int, n_samples: int) -> None:
        """v6 新增：为每段到达的 AI 音频，记录它在"会话本地时间轴"上的落点。"""
        if not self.enabled:
            return
        self._ai_timing.write(json.dumps({
            "session_ts_ms": int(session_ts_ms),
            "n_samples": int(n_samples),
            "sample_rate": SAMPLE_RATE_OUT,
        }) + "\n")
        self._ai_timing.flush()

    def log_turn_text(self, turn_idx: int, text: str, is_listen: bool) -> None:
        if not self.enabled:
            return
        tag = "LISTEN" if is_listen else "AI"
        self._transcript.write(f"[{tag} #{turn_idx}] {text}\n")
        self._transcript.flush()

    def log_subtitle(
        self,
        start_ms: int,
        end_ms: int,
        text: str,
        is_listen: bool,
        turn_idx: int,
    ) -> None:
        """v6 新增：为每个 turn 记录 [start_ms, end_ms]，用来生成 SRT。"""
        if not self.enabled:
            return
        self._subtitles.write(json.dumps({
            "start_ms": int(start_ms),
            "end_ms": int(end_ms),
            "text": text,
            "is_listen": bool(is_listen),
            "turn_idx": int(turn_idx),
        }, ensure_ascii=False) + "\n")
        self._subtitles.flush()

    def close(self, extra_stats: Optional[dict] = None) -> None:
        if not self.enabled or self.dir is None:
            return
        for fobj in (self._events, self._user_raw, self._user_sent,
                     self._ai_audio, self._transcript,
                     self._ai_timing, self._subtitles):
            try:
                fobj.close()
            except Exception:
                pass
        self._meta["end_time"] = _dt.datetime.now().isoformat()
        self._meta["stats"] = self.stats
        if extra_stats:
            self._meta["extra_stats"] = extra_stats
        self._write_meta()
        LOGGER.info("[SESSION] saved to: %s", self.dir)


# ============================================================
# TurnPrinter（与 v5.2 完全一致）
# ============================================================

class TurnPrinter:
    def __init__(self) -> None:
        self.turn_idx = 0
        self._in_turn = False
        self._turn_is_listen: Optional[bool] = None
        self._turn_text: list[str] = []

    def _flush(self) -> None:
        if not self._in_turn:
            return
        tag = "LISTEN" if self._turn_is_listen else "AI"
        line = f"[{tag} #{self.turn_idx}] {''.join(self._turn_text)}"
        print(line, flush=True)
        self._in_turn = False
        self._turn_text.clear()

    def feed(self, is_listen: bool, end_of_turn: bool, text: str) -> tuple[int, str, bool]:
        if text:
            if (not self._in_turn) or (self._turn_is_listen != is_listen):
                self._flush()
                self.turn_idx += 1
                self._in_turn = True
                self._turn_is_listen = is_listen
            self._turn_text.append(text)
        if end_of_turn:
            full_text = "".join(self._turn_text)
            cur_idx = self.turn_idx
            cur_listen = bool(self._turn_is_listen)
            self._flush()
            return cur_idx, full_text, cur_listen
        return self.turn_idx, "", is_listen


# ============================================================
# Video renderer（v6 核心新增，仅在会话结束时调用）
# ============================================================



# ============================================================
# 主循环
# ============================================================

async def run_bridge(args) -> None:
    _init_skill_router(args)
    _set_enabled_router_skills(args)
    ring = TimestampedRingBuffer(capacity_s=RING_CAPACITY_S)
    echo_gate = EchoGate(
        mode=args.echo_mode,
        tail_ms=args.echo_tail_ms,
        noise_level_db=args.echo_noise_db,
    )
    stop_evt = asyncio.Event()

    # v6.6 移植：把 SIGINT/SIGTERM 改成"设旗子让主循环干净退",
    # 避免 KeyboardInterrupt 在 cleanup 中段抛出导致 finalize_mp4 被跳过。
    def _request_stop(signame: str):
        if not stop_evt.is_set():
            LOGGER.warning("[MAIN] %s received, requesting graceful stop...", signame)
            stop_evt.set()
        else:
            LOGGER.warning("[MAIN] %s received again — finalize 还在跑,请稍等", signame)

    try:
        _loop = asyncio.get_running_loop()
        for _sig, _name in ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")):
            try:
                _loop.add_signal_handler(_sig, _request_stop, _name)
            except NotImplementedError:
                pass  # Windows 不支持 add_signal_handler，靠 main() 的 KeyboardInterrupt 兜底
    except RuntimeError:
        pass

        # Windows: 面板发的 CTRL_BREAK / 窗口关闭，用 console ctrl handler 接住。
        # 回调在 Windows 独立线程里跑，不受 asyncio 主线程 IOCP 阻塞影响；
        # 通过 call_soon_threadsafe 唤醒 proactor loop 并设 stop_evt，
        # 走与"网页停止按钮"完全相同的优雅落盘路径(run_bridge 的 finally)。
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        _loop_for_ctrl = asyncio.get_running_loop()

        def _win_ctrl_handler(ctrl_type):
            # 0=CTRL_C 1=CTRL_BREAK 2=CTRL_CLOSE 5=LOGOFF 6=SHUTDOWN
            if ctrl_type in (1, 2, 5, 6):  # CTRL_C(0) 仍交给 CPython 原生处理(保持现状)
                try:
                    _loop_for_ctrl.call_soon_threadsafe(_request_stop, f"CTRL_{ctrl_type}")
                except Exception:
                    pass
                return True  # 已处理:阻止默认立即终止,给 finally 落盘留时间
            return False

        _CTRL_CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(_win_ctrl_handler)
        if ctypes.windll.kernel32.SetConsoleCtrlHandler(_CTRL_CB, True):
            LOGGER.info("[MAIN] console ctrl handler installed (CTRL_BREAK→graceful stop)")
        else:
            LOGGER.warning("[MAIN] SetConsoleCtrlHandler 注册失败")
        globals()["_WIN_CTRL_HANDLER_REF"] = _CTRL_CB  # 必须保住引用,否则被 GC,回调悬空
    esp_stats: dict = {"rx_pkts": 0, "esp_drops": 0}
    prompt_suffix = ""
    active_skill = _active_skill_from_args(args)
    if args.multi_skill_mode:
        if SKILL_ROUTER is not None:
            prompt_suffix = SKILL_ROUTER.build_router_prompt(
                include_skills=_selected_multi_skills(args)
            )
        else:
            prompt_suffix = (
                MULTI_SKILL_ROUTER_SUFFIX
                + "\n\n"
                + FIND_OBJECT_SUFFIX
                + "\n\n"
                + SCENE_DESCRIPTION_SUFFIX
            )
    if args.find_object_mode:
        prompt_suffix = (prompt_suffix + "\n\n" + _skill_inject("find_object")).strip()
    if args.describe_scene_mode:
        prompt_suffix = (prompt_suffix + "\n\n" + _skill_inject("describe_scene")).strip()
    if args.read_text_mode:
        prompt_suffix = (prompt_suffix + "\n\n" + _skill_inject("read_text")).strip()
    if args.visual_qa_mode:
        prompt_suffix = (prompt_suffix + "\n\n" + _skill_inject("visual_qa")).strip()
    if args.prompt_suffix_file:
        extra_suffix = _load_text_file(args.prompt_suffix_file)
        if extra_suffix:
            prompt_suffix = (prompt_suffix + "\n\n" + extra_suffix).strip()
    effective_prompt = args.prompt
    if prompt_suffix:
        effective_prompt = args.prompt.rstrip() + "\n\n" + prompt_suffix

    # 把实际生效的 system_prompt 打到日志（前 80 字 + 总长），便于排查"prompt 没进入"：
    # - 看到期望的开头 = prompt 正确进入
    # - 看到字面量 "{prompt}" = panel 占位符没被替换
    # - 看到默认/通用开头 = 选中的 prompt 没传进来（常见于 conda run -n base 截断多行参数，
    #   见 README 的 conda 说明——请用命名虚拟环境，不要用 base）
    _pp = (effective_prompt or "").replace("\n", " ")
    LOGGER.info("[PROMPT] len=%d head=%s", len(effective_prompt or ""),
                _pp[:80] + ("…" if len(_pp) > 80 else ""))

    recorder = SessionRecorder(
        root_dir=Path(args.save_session),
        enabled=not args.no_save,
        meta={
            "version": "v6.6-audio-first-seq-clock",
            "args": {k: v for k, v in vars(args).items()},
            "sample_rate_in": SAMPLE_RATE_IN,
            "sample_rate_out": SAMPLE_RATE_OUT,
            "chunk_ms": CHUNK_MS,
            "grace_ms": GRACE_MS,
            "player_mode": "callback+ring",
            "rotate_cw": args.rotate,
            "funasr_enabled": args.enable_funasr,
            "effective_prompt": effective_prompt,
        },
    )

    asr_mirror: Optional[FunASRMirror] = None
    if args.enable_funasr:
        asr_jsonl = Path(args.asr_jsonl)
        if not asr_jsonl.is_absolute():
            asr_jsonl = (recorder.dir / asr_jsonl) if recorder.dir is not None else Path(args.asr_jsonl)
        asr_mirror = FunASRMirror(
            jsonl_path=asr_jsonl,
            model_name=args.funasr_model,
            language=args.funasr_language,
            device=args.funasr_device,
            window_s=args.funasr_window_s,
            interval_s=args.funasr_interval_s,
            min_rms=args.funasr_min_rms,
            utterance_end_s=args.funasr_utterance_end_s,
            min_utterance_s=args.funasr_min_utterance_s,
            max_utterance_s=args.funasr_max_utterance_s,
            prompt_suffix=prompt_suffix,
        )
        asr_mirror.start()
        if args.funasr_wait_ready_s > 0:
            LOGGER.info("[ASR] waiting up to %.1fs for model ready...", args.funasr_wait_ready_s)
            if asr_mirror.wait_ready(args.funasr_wait_ready_s):
                LOGGER.info("[ASR] ready; starting ESP32/gateway path")
            else:
                LOGGER.warning("[ASR] not ready after %.1fs; continue anyway", args.funasr_wait_ready_s)

    # v6.6 移植：bridge_ui Web 观测/回放服务
    ui_server: Optional[WebUIServer] = None
    if not args.no_ui:
        ui_server = WebUIServer(
            port=args.ui_port,
            sessions_root=Path(args.save_session),
            stop_callback=lambda: stop_evt.set(),   # ★ 网页点停止 → 设旗子干净退出并落盘
            mode_info={"mode": "rerun" if args.rerun_from else "live"},
        )
        await ui_server.start()
        if recorder.dir is not None:
            await ui_server.emit({
                "type": "session_start",
                "session_id": recorder.dir.name,
            })

    # v6.6 移植：先建 LiveRecorder，attach 到 speaker（必须在 start 前），
    # 再 start，最后启动 reader——顺序照 recover 契约，保证首包 user 音频被录。
    live_rec = LiveRecorder(session_dir=recorder.dir)

    speaker: Optional[CallbackSpeakerPlayer] = None
    player_monitor: Optional[asyncio.Task] = None
    recv_task: Optional[asyncio.Task] = None          # 预初始化：防 Ctrl+C 早于其创建时外层 finally NameError
    image_cache_task: Optional[asyncio.Task] = None   # 同上
    if not args.no_play:
        speaker = CallbackSpeakerPlayer(
            sample_rate=SAMPLE_RATE_OUT,
            ring_seconds=args.player_ring_s,
            device=args.player_device,
            latency=args.player_latency,
            prebuffer_ms=args.player_prebuffer_ms,
            device_sr=args.player_device_sr,
            hostapi=args.player_hostapi,
        )
        live_rec.attach_to_player(speaker)   # ★ 必须在 speaker.start() 之前
        speaker.start()
        if getattr(args, "player_dac_probe", False):
            speaker.enable_dac_probe()
            LOGGER.info("[SPK] DAC 探针已开启，将在结束时落盘 live_dac.wav")
        if getattr(args, "player_raw_probe", False):
            speaker.enable_raw_probe()
            LOGGER.info("[SPK] RAW 探针已开启，将在结束时落盘 live_raw.wav")
        if args.player_stats_s > 0:
            player_monitor = asyncio.create_task(
                player_monitor_task(speaker, args.player_stats_s, stop_evt)
            )

    live_rec.start()   # ★ 此后 user/ai/frame 才被记录

    # rerun vs live：决定音频输入源（图像源在 send loop 内分流）
    rerun_img_source = None
    if args.rerun_from and local_pcm_reader is not None:
        rerun_dir = Path(args.rerun_from)
        rerun_img_source = LocalImageSource(rerun_dir)
        audio_reader_task = asyncio.create_task(
            local_pcm_reader(
                rerun_dir / "user_raw.pcm",
                ring, stop_evt, esp_stats,
                live_rec=live_rec,
                speaker=speaker,
                speaker_sr=SAMPLE_RATE_OUT,
                speed=args.rerun_speed,
                drain_s=args.rerun_drain_s,
            )
        )
    elif args.rerun_from and local_pcm_reader is None:
        LOGGER.error("[RERUN] rerun_source 不可用（import 失败），无法 rerun")
        stop_evt.set()
        return
    else:
        audio_reader_task = asyncio.create_task(
            esp32_audio_reader(args.esp32_host, args.esp32_port,
                               ring, stop_evt, esp_stats, live_rec)
        )

    last_runtime_skill_event_id = 0

    session_id = f"omni_esp32v6_{int(time.time())}"
    scheme = "wss" if args.gateway_tls else "ws"
    # 新架构：统一入口 /v1/realtime；mode=video(音频+视频帧)。session_id 由 gateway 下发。
    gw_url = f"{scheme}://{args.gateway}/v1/realtime?mode=video"
    LOGGER.info("[GW] connecting: %s", gw_url)
    LOGGER.info(
        "[CFG] echo=%s tail=%dms noise=%.1fdB save=%s img=%s rotate=%d° "
        "player=callback ring=%.1fs stats=%.1fs mode=%s img_transport=%s",
        args.echo_mode, args.echo_tail_ms, args.echo_noise_db,
        "off" if args.no_save else str(recorder.dir),
        "Y" if args.use_image else "N",
        args.rotate,
        args.player_ring_s, args.player_stats_s,
        "rerun" if args.rerun_from else "live",
        args.image_transport,
    )

    ssl_ctx: Optional[ssl.SSLContext] = None
    if args.gateway_tls:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    connector_kwargs = {}
    if ssl_ctx is not None:
        connector_kwargs["ssl"] = ssl_ctx

    turn_printer = TurnPrinter()
    kv_tracker = KvPruneTracker(logger=LOGGER, max_kv=getattr(args, "max_kv", 8192))
    run_stats = {
        "chunks_sent": 0,
        "behind_max_ms": 0,
        "grace_hits": 0,
        "audio_waits": 0,
        "audio_wait_timeouts": 0,
        "audio_gap_fill_chunks": 0,
        "audio_gap_fill_ms_total": 0,
        "audio_future_prevented": 0,
        "image_attempts": 0,
        "image_ok": 0,
        "image_fail": 0,
        "image_slow": 0,
        "image_last_capture_ms": 0,
        "image_abort_user_speech": 0,
        "image_abort_audio_backlog": 0,
        "image_skip_user_speech": 0,
        "image_skip_audio_backlog": 0,
        "image_skip_inflight": 0,
        "image_skip_min_interval": 0,
    }

    # v6：turn 的起始 session_ts_ms（用于生成字幕）
    turn_start_session_ms: Optional[int] = None
    session_start_wall: Optional[float] = None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                gw_url, heartbeat=30, max_msg_size=0, **connector_kwargs
            ) as gw:
                LOGGER.info("[GW] connected (session=%s)", session_id)

                # ── 新架构握手：queue → session.init(payload) → session.created ──
                # 参考 realtime-session.js：连上后处理排队消息；收到 queue_done 或
                # 100ms 兜底后发 session.init（system_prompt 等放进 payload）；
                # 收到 session.created 视为就绪。gateway 排队期间会缓冲我们发的
                # session.init，worker 分配后再转发，故提前发送安全。
                init_payload = {
                    "system_prompt": effective_prompt,
                    "config": {
                        "force_listen_count": args.force_listen,
                        "chunk_ms": CHUNK_MS,
                        "generate_audio": True,
                        "max_new_speak_tokens_per_chunk": 5,
                    },
                    "max_slice_nums": 1,
                    "deferred_finalize": True,
                }
                _init_sent = False

                async def _send_session_init() -> None:
                    nonlocal _init_sent
                    if _init_sent:
                        return
                    _init_sent = True
                    await gw.send_json({"type": "session.init", "payload": init_payload})
                    LOGGER.info("[GW] session.init sent")

                _session_ready = False
                _hs_start = time.monotonic()
                while not _session_ready and not stop_evt.is_set():
                    if not _init_sent:
                        _remaining = 0.1 - (time.monotonic() - _hs_start)
                        _recv_timeout = max(0.02, _remaining) if _remaining > 0 else 0.02
                    else:
                        _recv_timeout = 1.0  # 已发 init：短超时轮询，便于响应 Ctrl+C/急停
                    try:
                        _wsmsg = await asyncio.wait_for(gw.receive(), timeout=_recv_timeout)
                    except asyncio.TimeoutError:
                        # 100ms 兜底：无排队时也主动发 init（与前端 setTimeout(100) 一致）
                        if not _init_sent and (time.monotonic() - _hs_start) >= 0.1:
                            await _send_session_init()
                        continue
                    if _wsmsg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            _m = json.loads(_wsmsg.data)
                        except Exception:
                            continue
                        _mt = _m.get("type", "")
                        if _mt in ("session.queued", "queued",
                                   "session.queue_update", "queue_update"):
                            LOGGER.info(
                                "[GW] queued position=%s eta=%ss qlen=%s",
                                _m.get("position"), _m.get("estimated_wait_s"),
                                _m.get("queue_length"),
                            )
                        elif _mt in ("session.queue_done", "queue_done"):
                            LOGGER.info("[GW] worker assigned")
                            await _send_session_init()
                        elif _mt == "session.created":
                            session_id = _m.get("session_id") or session_id
                            LOGGER.info(
                                "[GW] session.created id=%s prompt_len=%s",
                                session_id, _m.get("prompt_length"),
                            )
                            _session_ready = True
                        elif _mt == "error":
                            LOGGER.error("[GW] handshake error: %s", _m.get("error"))
                            stop_evt.set()
                            return
                    elif _wsmsg.type in (aiohttp.WSMsgType.CLOSED,
                                         aiohttp.WSMsgType.ERROR):
                        LOGGER.error("[GW] closed during handshake")
                        stop_evt.set()
                        return

                if not _session_ready:
                    LOGGER.info("[GW] 握手期间收到停止请求，退出")
                    return
                LOGGER.info("[GW] session ready; waiting for ESP32 audio...")
                # ── 等待 ESP32 音频首包（现场连接韧性）──
                #   后台 esp32_audio_reader 已带 backoff 自动重连；这里负责"耐心等"：
                #   等待时长 = --connect-wait-s（默认 120s，旧版写死 15s）；
                #   期间每 --connect-poll-log-s 打印一次"等待眼镜连接…"，
                #   便于面板据日志显示"连接中"。超时后：
                #     --connect-retry 开 → 继续下一轮等待，不退出（推荐现场用）
                #     未开             → 保持旧行为：停止并退出
                _wait_deadline_s = max(1.0, float(getattr(args, "connect_wait_s", 120.0)))
                _poll_log_s = max(1.0, float(getattr(args, "connect_poll_log_s", 5.0)))
                t_wait = time.monotonic()
                _last_wait_log = 0.0
                while ring.latest_ts_ms == 0 and not stop_evt.is_set():
                    _elapsed = time.monotonic() - t_wait
                    if _elapsed - _last_wait_log >= _poll_log_s:
                        LOGGER.warning(
                            "[ESP32] 等待眼镜连接… 已等待 %.0fs（目标 %s:%d，reader 自动重连中）",
                            _elapsed, args.esp32_host, args.esp32_port,
                        )
                        _last_wait_log = _elapsed
                    if _elapsed >= _wait_deadline_s:
                        if getattr(args, "connect_retry", False):
                            LOGGER.warning(
                                "[ESP32] %.0fs 内仍无音频；--connect-retry 已开，继续等待…",
                                _wait_deadline_s,
                            )
                            t_wait = time.monotonic()
                            _last_wait_log = 0.0
                            continue
                        else:
                            LOGGER.error(
                                "[ESP32] %.0fs 内无音频，abort（如需持续等待请加 --connect-retry）",
                                _wait_deadline_s,
                            )
                            stop_evt.set()
                            await gw.send_json({"type": "session.close", "reason": "no_audio"})
                            return
                    await asyncio.sleep(0.05)
                if ring.latest_ts_ms == 0:
                    # 走到这里通常是 stop_evt 被外部置位（面板急停/Ctrl-C）
                    LOGGER.info("[ESP32] 等待音频期间收到停止请求，退出")
                    await gw.send_json({"type": "session.close", "reason": "user_stop"})
                    return
                LOGGER.info("[ESP32] 音频首包到达，进入正常双工循环")

                assert ring.ts_start_ms is not None
                start_ts_ms = ring.ts_start_ms
                last_end_ts_ms = float(start_ts_ms)
                chunk_idx = 0
                image_state = AudioFirstImageState()
                # v6.6 移植：live 用图像优化路（abort/cache）；rerun 用直读路，不启动该 loop
                if rerun_img_source is None:
                    image_cache_task = asyncio.create_task(
                        audio_first_image_cache_loop(args, image_state, stop_evt, run_stats)
                    )
                else:
                    image_cache_task = None
                last_sent_image_seq = 0
                last_image_send_mono = 0.0
                last_send_mono = 0.0
                last_muted_state: Optional[bool] = None
                # barge-in 状态（移植自 recover，挂为局部变量）
                _barge_loud_since: Optional[float] = None
                _barge_last_trigger_ms = -1e9
                run_stats["barge_events"] = 0

                # 会话起点：与 chunk 的 ts_offset_ms = 0 对齐
                session_start_wall = time.time()
                recorder.mark_session_start(session_start_wall)

                def _now_session_ms() -> int:
                    if session_start_wall is None:
                        return 0
                    return max(0, int((time.time() - session_start_wall) * 1000))

                # ----- recv loop -----
                # ── turn 边界（关键）──
                # V1 后端会发 end_of_turn=True 标记"AI 这轮说完了"；V2 后端实测恒发
                # false，于是 TurnPrinter / live.html 的 `if (endOfTurn)` 永不触发，
                # 所有文本累积进同一个气泡 → 显示成"1 轮"、聚成一坨。
                # V2 里真正的"说完"信号是 response.listen（模型回到听的状态）——gateway
                # 前端 realtime-session.js 的 _handleListen 正是靠它 onSpeakEnd。
                # 这里补上 V1 语义：AI 说话中收到 listen → end_of_turn=True。
                # 连续 listen（模型持续在听）不重复结束 turn。live.html 无需改动。
                _spk = {"speaking": False}

                async def recv_loop() -> None:
                    nonlocal turn_start_session_ms

                    async def handle_result(
                        is_listen: bool,
                        end_of_turn: bool,
                        text: str,
                        audio_b64: Optional[str],
                        kv_cache_length: Optional[int],
                    ) -> None:
                        """把新协议各类事件归一成旧的 result 语义后统一处理。"""
                        nonlocal turn_start_session_ms
                        # KV prune 检测
                        if kv_cache_length is not None:
                            kv_evt = kv_tracker.update({"kv_cache_length": kv_cache_length})
                            if (kv_evt is not None and ui_server is not None
                                    and ui_server.live_clients):
                                asyncio.create_task(ui_server.emit(kv_evt))
                        echo_gate.update(is_listen, end_of_turn)
                        if asr_mirror is not None and not is_listen:
                            asr_mirror.note_model_text(text)
                            asr_mirror.suppress_for(
                                args.funasr_echo_suppress_s, "model result"
                            )

                        ai_pcm: Optional[np.ndarray] = None
                        if audio_b64:
                            try:
                                ai_bytes = base64.b64decode(audio_b64)
                                ai_pcm = np.frombuffer(ai_bytes, dtype=np.float32)
                                if speaker is not None and ai_pcm.size > 0:
                                    speaker.enqueue(ai_pcm)
                            except Exception as e:
                                LOGGER.warning("decode AI audio: %s", e)
                                ai_pcm = None

                        if ai_pcm is not None and ai_pcm.size > 0:
                            recorder.log_ai_audio_timing(
                                _now_session_ms(), int(ai_pcm.size)
                            )

                        audio_ms_in = (
                            int(ai_pcm.size * 1000 / SAMPLE_RATE_OUT)
                            if ai_pcm is not None else 0
                        )
                        LOGGER.info(
                            "[RX] listen=%s eot=%s audio=%dms kv=%s text=%r",
                            is_listen, end_of_turn, audio_ms_in,
                            kv_tracker.last_kv_len,
                            text[:60] + ("..." if len(text) > 60 else ""),
                        )

                        prev_turn_idx = turn_printer.turn_idx
                        turn_idx_cur, full_text, turn_listen = turn_printer.feed(
                            is_listen, end_of_turn, text
                        )
                        if text and turn_printer.turn_idx > prev_turn_idx:
                            turn_start_session_ms = _now_session_ms()

                        # 有文本 → 推增量字幕；end_of_turn → 推轮次结束信号。
                        # listen 事件没有 text，但它携带 end_of_turn=True（V2 的
                        # 说完信号），必须推给 live.html，否则前端 `if (endOfTurn)`
                        # 永不触发、所有文本会累积进同一个气泡（聚成一坨）。
                        if ui_server is not None and (text or end_of_turn):
                            asyncio.create_task(ui_server.emit({
                                "type": "result",
                                "is_listen": is_listen,
                                "end_of_turn": end_of_turn,
                                "text": text,
                            }))

                        if end_of_turn and full_text:
                            recorder.log_turn_text(turn_idx_cur, full_text, turn_listen)
                            end_ms = _now_session_ms() + 800  # 多挂 0.8s
                            start_ms = (turn_start_session_ms
                                        if turn_start_session_ms is not None
                                        else max(end_ms - 3000, 0))
                            recorder.log_subtitle(
                                start_ms=start_ms,
                                end_ms=end_ms,
                                text=full_text,
                                is_listen=turn_listen,
                                turn_idx=turn_idx_cur,
                            )
                            turn_start_session_ms = None

                        pending_ms = -1
                        if speaker is not None:
                            pending_ms = speaker.stats()["pending_ms"]
                        recorder.log_result(
                            is_listen=is_listen,
                            end_of_turn=end_of_turn,
                            text=text,
                            ai_audio_f32=ai_pcm,
                            turn_idx=turn_printer.turn_idx,
                            player_pending_ms=pending_ms,
                        )

                    async for msg in gw:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                result = json.loads(msg.data)
                            except Exception:
                                continue
                            mtype = result.get("type", "")

                            # ── 新协议 ──
                            if mtype == "response.output_audio.delta":
                                _spk["speaking"] = True
                                await handle_result(
                                    is_listen=False,
                                    end_of_turn=bool(result.get("end_of_turn", False)),
                                    text=result.get("text", "") or "",
                                    audio_b64=result.get("audio"),
                                    kv_cache_length=result.get("kv_cache_length"),
                                )
                            elif mtype == "response.output.delta":
                                kind = result.get("kind", "")
                                LOGGER.info("[RX-DBG] output.delta kind=%s keys=%s has_audio=%s",
                                            kind, list(result.keys()), bool(result.get("audio")))
                                eot = bool(result.get("end_of_turn", False))
                                kv = result.get("kv_cache_length")
                                if kind == "listen":
                                    # AI 说话中收到 listen = 这一轮说完
                                    _eot = _spk["speaking"] or eot
                                    _spk["speaking"] = False
                                    await handle_result(True, _eot, "", None, kv)
                                elif kind == "text":
                                    _spk["speaking"] = True
                                    await handle_result(
                                        False, eot, result.get("text", "") or "", None, kv
                                    )
                                elif kind == "audio":
                                    _spk["speaking"] = True
                                    await handle_result(
                                        False, eot, "", result.get("audio"), kv
                                    )
                                else:
                                    LOGGER.debug("[RX] unknown output.delta kind=%s", kind)
                            elif mtype == "response.listen":
                                # AI 说话中收到 listen = 这一轮说完；持续 listen 不重复结束
                                _eot = _spk["speaking"] or bool(
                                    result.get("end_of_turn", False)
                                )
                                _spk["speaking"] = False
                                await handle_result(
                                    True,
                                    _eot,
                                    "", None,
                                    result.get("kv_cache_length"),
                                )
                            elif mtype == "response.metrics":
                                kv = result.get("kv_cache_length")
                                if kv is not None:
                                    kv_evt = kv_tracker.update({"kv_cache_length": kv})
                                    if (kv_evt is not None and ui_server is not None
                                            and ui_server.live_clients):
                                        asyncio.create_task(ui_server.emit(kv_evt))
                            elif mtype == "runtime_prompt_ack":
                                LOGGER.info(
                                    "[SKILL] runtime prompt ack event=%s intent=%s",
                                    result.get("event_id"),
                                    result.get("intent") or "-",
                                )
                            elif mtype == "session.closed":
                                LOGGER.warning(
                                    "[GW] session closed: %s", result.get("reason")
                                )
                                stop_evt.set()
                                return
                            elif mtype == "error":
                                LOGGER.error("[GW] error: %s", result.get("error"))
                            elif mtype in (
                                "session.queued", "session.queue_update",
                                "session.queue_done", "session.created",
                            ):
                                pass  # 迟到/重复的握手消息，忽略

                            # ── 向后兼容：旧协议 ──
                            elif mtype == "result":
                                await handle_result(
                                    bool(result.get("is_listen", True)),
                                    bool(result.get("end_of_turn", False)),
                                    result.get("text", "") or "",
                                    result.get("audio_data"),
                                    result.get("kv_cache_length"),
                                )
                            elif mtype == "audio_only":
                                if asr_mirror is not None:
                                    asr_mirror.suppress_for(
                                        args.funasr_echo_suppress_s, "model audio_only"
                                    )
                                await handle_result(
                                    False, False, "", result.get("audio_data"), None
                                )
                            elif mtype == "timeout":
                                LOGGER.warning("[GW] timeout: %s", result.get("reason"))
                                stop_evt.set()
                                return
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            LOGGER.warning("[GW] WS closed/error")
                            stop_evt.set()
                            return

                recv_task = asyncio.create_task(recv_loop())

                # ----- main send loop -----
                try:
                    while not stop_evt.is_set():
                        target_end_ts = last_end_ts_ms + CHUNK_MS
                        wait_started = time.monotonic()
                        wait_warn_started = wait_started
                        grace_used = False
                        while ring.latest_ts_ms < target_end_ts and not stop_evt.is_set():
                            grace_used = True
                            missing_ms = int(target_end_ts - ring.latest_ts_ms)
                            if missing_ms <= args.audio_max_gap_fill_ms:
                                run_stats["audio_gap_fill_chunks"] += 1
                                run_stats["audio_gap_fill_ms_total"] += max(missing_ms, 0)
                                break
                            if time.monotonic() - wait_warn_started >= args.audio_wait_timeout_s:
                                run_stats["audio_wait_timeouts"] += 1
                                run_stats["audio_future_prevented"] += 1
                                LOGGER.warning(
                                    "[AUDIO] waiting for ESP32 PSRAM backfill: "
                                    "missing=%dms target=%d latest=%d",
                                    missing_ms,
                                    int(target_end_ts),
                                    int(ring.latest_ts_ms),
                                )
                                wait_warn_started = time.monotonic()
                            await asyncio.sleep(0.02)
                        if stop_evt.is_set():
                            break
                        audio_wait_ms = (
                            int((time.monotonic() - wait_started) * 1000)
                            if grace_used else 0
                        )
                        if grace_used:
                            run_stats["grace_hits"] += 1
                            run_stats["audio_waits"] += 1

                        audio_raw_f32 = await ring.slice(
                            int(last_end_ts_ms), int(target_end_ts)
                        )
                        muted = echo_gate.should_mute()
                        audio_rms = _pcm_rms(audio_raw_f32)
                        audio_sent_f32 = echo_gate.apply(audio_raw_f32)

                        # ── 主动打断 barge-in（移植自 recover）──
                        # AI 正在播(spk_pending 高) + 用户持续高音量(audio_rms) → flush + force_listen
                        barge_force_listen = False
                        if args.barge_enable:
                            try:
                                spk_pending_ms = speaker.stats()["pending_ms"] if speaker is not None else 0
                            except Exception:
                                spk_pending_ms = 0
                            ai_speaking = spk_pending_ms >= args.barge_min_pending_ms
                            user_loud = audio_rms >= args.barge_user_rms
                            now_ms_barge = time.monotonic() * 1000.0
                            if user_loud and ai_speaking:
                                if _barge_loud_since is None:
                                    _barge_loud_since = now_ms_barge
                                loud_dur_ms = now_ms_barge - _barge_loud_since
                                cooldown_ok = (now_ms_barge - _barge_last_trigger_ms
                                               >= args.barge_cooldown_ms)
                                if loud_dur_ms >= args.barge_hold_ms and cooldown_ok:
                                    dropped = speaker.flush() if speaker is not None else 0
                                    barge_force_listen = True
                                    _barge_last_trigger_ms = now_ms_barge
                                    _barge_loud_since = None
                                    run_stats["barge_events"] += 1
                                    LOGGER.warning(
                                        "[BARGE] triggered: rms=%.4f hold=%dms "
                                        "spk_pending=%dms dropped=%dms → force_listen",
                                        audio_rms, int(loud_dur_ms),
                                        spk_pending_ms, int(dropped * 1000 / SAMPLE_RATE_OUT),
                                    )
                            else:
                                _barge_loud_since = None
                        runtime_skill_payload: Optional[dict[str, Any]] = None
                        if asr_mirror is not None:
                            pending_ms = 0
                            if speaker is not None:
                                try:
                                    pending_ms = speaker.stats()["pending_ms"]
                                except Exception:
                                    pending_ms = 0
                            if muted or pending_ms > args.funasr_speaker_pending_ms:
                                asr_mirror.suppress_for(
                                    args.funasr_echo_suppress_s,
                                    f"echo gate muted={muted} pending={pending_ms}ms",
                                )
                            else:
                                asr_mirror.submit(audio_raw_f32)
                            if (
                                args.skill_reconnect_mode
                                and asr_mirror.latest_intent
                                and asr_mirror.latest_intent != active_skill
                            ):
                                LOGGER.warning(
                                    "[SKILL] reconnect requested: %s -> %s by user_text=%r",
                                    active_skill or "base",
                                    asr_mirror.latest_intent,
                                    asr_mirror.latest_clean_text,
                                )
                                raise SkillReconnect(asr_mirror.latest_intent)
                            if args.runtime_skill_inject:
                                event_id = int(getattr(asr_mirror, "latest_event_id", 0) or 0)
                                if event_id and event_id != last_runtime_skill_event_id:
                                    intent = asr_mirror.latest_intent
                                    runtime_text = _runtime_skill_prompt(
                                        intent,
                                        asr_mirror.latest_target,
                                        asr_mirror.latest_clean_text,
                                    )
                                    if runtime_text:
                                        runtime_skill_payload = {
                                            "type": "runtime_prompt",
                                            "source": "funasr",
                                            "event_id": event_id,
                                            "intent": intent,
                                            "target": asr_mirror.latest_target,
                                            "user_text": asr_mirror.latest_clean_text,
                                            "runtime_text": runtime_text,
                                        }
                                        await gw.send_json(runtime_skill_payload)
                                        LOGGER.info(
                                            "[SKILL] runtime prompt sent event=%d intent=%s target=%s",
                                            event_id,
                                            intent,
                                            asr_mirror.latest_target or "-",
                                        )
                                    last_runtime_skill_event_id = event_id

                        # v6：在落盘 & 送模型之前，先做旋转
                        frame_b64_list = None
                        img_for_chunk: Optional[bytes] = None
                        # v6.6 移植：rerun 从本地 session 直读图（按 chunk_idx），live 走优化缓存
                        if rerun_img_source is not None:
                            # events.jsonl 里的 idx 是 log_chunk 写入的 1-based 值（见下方 +1 后调用），
                            # 这里取图要用同一基准：chunk_idx 此刻还是发送前(0-based)，故 +1 对齐。
                            cached_img = await rerun_img_source.capture(chunk_idx + 1)
                            image_seq = chunk_idx + 1
                            image_age_ms = 0
                        else:
                            cached_img, image_seq, image_age_ms = await image_state.get_image()
                        attach_image = args.use_image and cached_img is not None
                        # 方案b:取图超时且开了 --drop-image-on-timeout → 本轮不带图,只发音频。
                        if args.drop_image_on_timeout:
                            if await image_state.consume_timeout_drop():
                                attach_image = False
                        if (
                            attach_image
                            and args.image_max_age_s > 0
                            and image_age_ms > int(args.image_max_age_s * 1000)
                        ):
                            attach_image = False
                        if attach_image and args.image_resend_s > 0:
                            now_mono = time.monotonic()
                            if (
                                image_seq == last_sent_image_seq
                                and now_mono - last_image_send_mono < args.image_resend_s
                            ):
                                attach_image = False
                        if attach_image and cached_img:
                            frame_b64_list = [
                                base64.b64encode(cached_img).decode("ascii")
                            ]
                            img_for_chunk = cached_img
                            last_sent_image_seq = image_seq
                            last_image_send_mono = time.monotonic()

                        audio_b64 = base64.b64encode(
                            audio_sent_f32.tobytes()
                        ).decode("ascii")
                        # 新架构：input.append，音频/视频帧放进 input.*
                        _input: dict[str, Any] = {"audio": audio_b64}
                        if frame_b64_list:
                            _input["video_frames"] = frame_b64_list  # 旧 frame_base64_list
                        # 主动打断：这个 chunk 强制 worker 进入 LISTEN
                        if barge_force_listen:
                            _input["force_listen"] = True
                        # runtime skill 字段：gateway 纯透传给 worker；需 worker 侧支持，
                        # 不支持时被忽略，不影响音视频主链路。
                        if runtime_skill_payload is not None:
                            _input["runtime_text"] = runtime_skill_payload["runtime_text"]
                            _input["runtime_event_id"] = runtime_skill_payload["event_id"]
                            _input["runtime_intent"] = runtime_skill_payload["intent"]
                            _input["runtime_target"] = runtime_skill_payload["target"]
                            _input["runtime_user_text"] = runtime_skill_payload["user_text"]
                        payload = {"type": "input.append", "input": _input}

                        behind_ms = int(ring.latest_ts_ms - target_end_ts)
                        await image_state.note_audio(
                            audio_rms=audio_rms,
                            is_user_speech=(not muted) and audio_rms >= args.media_speech_rms,
                            speech_hold_s=args.media_speech_hold_s,
                            ring_behind_ms=behind_ms,
                            chunk_idx=chunk_idx,
                        )
                        if args.min_send_interval_s > 0 and last_send_mono > 0:
                            sleep_s = args.min_send_interval_s - (
                                time.monotonic() - last_send_mono
                            )
                            if sleep_s > 0:
                                await asyncio.sleep(sleep_s)
                        await gw.send_json(payload)
                        last_send_mono = time.monotonic()
                        chunk_idx += 1
                        run_stats["chunks_sent"] = chunk_idx

                        run_stats["behind_max_ms"] = max(
                            run_stats["behind_max_ms"], abs(behind_ms)
                        )

                        recorder.log_chunk(
                            idx=chunk_idx,
                            ts_offset_ms=int(target_end_ts - start_ts_ms),
                            user_raw_f32=audio_raw_f32,
                            user_sent_f32=audio_sent_f32,
                            image_jpeg=img_for_chunk if frame_b64_list else None,
                            muted=muted,
                            ring_behind_ms=behind_ms,
                        )
                        # v6.6 移植：LiveRecorder 记录该 chunk 配的图（idx 与 log_chunk 一致，1-based）
                        live_rec.on_frame(
                            img_for_chunk if frame_b64_list else None, chunk_idx
                        )

                        # v6.6 移植：把当前帧推给 bridge_ui live 前端。
                        # 守卫:无 live 客户端时连 dict(含图 base64)都不构造,零负担。
                        if ui_server is not None and ui_server.live_clients:
                            asyncio.create_task(ui_server.emit({
                                "type": "chunk",
                                "idx": chunk_idx,
                                "ts": int(target_end_ts - start_ts_ms),
                                "muted": muted,
                                "img_b64": (frame_b64_list[0] if frame_b64_list else None),
                                "img_age_ms": int(image_age_ms),
                                "img_sent": bool(frame_b64_list),
                            }))

                        muted_changed = (last_muted_state is not None
                                         and muted != last_muted_state)
                        last_muted_state = muted
                        anomaly = (behind_ms < -200) or grace_used or muted_changed
                        lvl = (logging.INFO if (args.verbose_chunks or anomaly)
                               else logging.DEBUG)
                        LOGGER.log(
                            lvl,
                            "chunk#%d ts=%d muted=%s img=%s ring_behind=%+dms "
                            "grace=%s wait=%dms rms=%.4f img_age=%dms img_seq=%d",
                            chunk_idx,
                            int(target_end_ts - start_ts_ms),
                            muted,
                            "Y" if frame_b64_list else "-",
                            behind_ms,
                            "Y" if grace_used else "-",
                            audio_wait_ms,
                            audio_rms,
                            image_age_ms,
                            image_seq,
                        )

                        last_end_ts_ms = target_end_ts
                except (asyncio.CancelledError, KeyboardInterrupt):
                    pass
                finally:
                    # 只做同步 cancel（不 await）——任何 await 都可能被 Ctrl+C 二次打断，
                    # 从而阻止控制流走到外层落盘 finally。await 清理统一放外层落盘之后。
                    stop_evt.set()
                    recv_task.cancel()
                    if image_cache_task is not None:
                        image_cache_task.cancel()
    finally:
        stop_evt.set()
        LOGGER.info("[LIVE] 正在保存录制并合成 mp4，请稍候（勿重复 Ctrl+C）...")

        # ============================================================
        # 关键：所有同步落盘（stop / speaker.stop / recorder.close / finalize_mp4）
        # 必须放在任何 await 之前。Windows 上 Ctrl+C 会打断 finally 里的 await，
        # 若把 await 夹在落盘中间，会导致 finalize_mp4 被跳过。照 recover 的顺序：
        # 先把所有落盘同步做完，await 的清理放最后。
        # ============================================================

        # —— 1) live_rec.stop：写 live_user.wav / live_ai.wav ——
        try:
            live_rec.stop()
        except Exception as e:
            LOGGER.warning("[LIVE] stop err: %r", e)

        # —— 2) 取 player 状态 + 关 speaker（同步）——
        player_stats = {}
        try:
            if speaker is not None:
                player_stats = speaker.stats()
        except Exception:
            pass
        try:
            if speaker is not None and getattr(args, "player_dac_probe", False):
                _dac_path = str((recorder.dir / "live_dac.wav")) if recorder.dir is not None else "live_dac.wav"
                speaker.dump_dac_probe(_dac_path)
        except Exception as e:
            LOGGER.warning("[SPK] DAC 探针落盘异常: %r", e)
        try:
            if speaker is not None and getattr(args, "player_raw_probe", False):
                _raw_path = str((recorder.dir / "live_raw.wav")) if recorder.dir is not None else "live_raw.wav"
                speaker.dump_raw_probe(_raw_path)
        except Exception as e:
            LOGGER.warning("[SPK] RAW 探针落盘异常: %r", e)
        try:
            if speaker is not None:
                speaker.stop()
        except Exception as e:
            LOGGER.warning("[SPK] stop err: %r", e)

        # —— 3) recorder.close 写 meta / transcript（同步）——
        try:
            recorder.close(extra_stats={
                "run": run_stats,
                "esp32": esp_stats,
                "audio_ring": ring.diagnostics(),
                "player": player_stats,
            })
        except Exception as e:
            LOGGER.warning("[REC] close err: %r", e)

        # —— 4) finalize_mp4：合成 mp4（同步，兜二次 Ctrl+C；此时 wav/帧已落地）——
        try:
            out = live_rec.finalize_mp4()
            if out:
                LOGGER.info("[LIVE] mp4 finalized: %s", out)
        except KeyboardInterrupt:
            LOGGER.warning("[LIVE] finalize_mp4 被二次 Ctrl+C 打断；wav/帧已保留，可手动跑 ffmpeg")
        except Exception as e:
            LOGGER.warning("[LIVE] finalize_mp4 err: %r", e)

        # ============================================================
        # 落盘已全部完成。下面是可被 Ctrl+C 安全打断的 await 清理。
        # ============================================================
        if asr_mirror is not None:
            try:
                asr_mirror.stop()
            except Exception as e:
                LOGGER.warning("[ASR] stop err: %r", e)

        for _t in (player_monitor, audio_reader_task, recv_task, image_cache_task):
            if _t is None:
                continue
            _t.cancel()
            try:
                await asyncio.wait_for(_t, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

        if ui_server is not None:
            try:
                await ui_server.stop()
            except Exception as e:
                LOGGER.warning("[UI] stop err: %r", e)

        # 关闭 TCP 图像连接（如启用）
        global _tcp_image_client
        if _tcp_image_client is not None:
            try:
                await _tcp_image_client._close()
            except Exception:
                pass
            _tcp_image_client = None


# ============================================================
# 设备配置文件（多副眼镜 IP 列表）
# ------------------------------------------------------------
#   现场多副眼镜、多台笔记本各有不同 IP。烧录后串口看到 IP，
#   只改这个 JSON 即可，无需改启动命令。JSON 只存眼镜列表；
#   gateway 走本机默认，不入此文件。
#
#   格式（devices.json）：
#     {
#       "devices": [
#         {"name": "左镜",  "esp32_host": "192.168.43.147", "esp32_port": 80},
#         {"name": "右镜",  "esp32_host": "192.168.43.148", "esp32_port": 80},
#         {"name": "备用镜", "esp32_host": "192.168.43.149", "esp32_port": 80}
#       ]
#     }
#
#   用法：--device-config devices.json --device 左镜
#   优先级：命令行显式 --esp32-host/--esp32-port > 选中设备 > 默认值。
# ============================================================


def _load_device_config(path: str) -> list[dict]:
    """读取设备列表 JSON，返回 devices 数组（失败抛异常，让 main 打印清晰错误）。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"设备配置文件不存在: {path}")
    data = json.loads(p.read_text(encoding="utf-8"))
    devices = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(devices, list) or not devices:
        raise ValueError(f"设备配置文件缺少非空的 'devices' 列表: {path}")
    return devices


def _apply_device_config(args) -> None:
    """把 --device-config 选中的设备 IP 应用到 args（就地修改）。

    仅当用户没有在命令行显式给出 --esp32-host 时，才用配置文件的值覆盖，
    这样命令行始终拥有最高优先级。
    """
    if not getattr(args, "device_config", None):
        return
    devices = _load_device_config(args.device_config)
    names = [str(d.get("name", f"#{i}")) for i, d in enumerate(devices)]

    # 选中设备：--device 指定名字；否则用列表第一个
    chosen: Optional[dict] = None
    if args.device:
        for d in devices:
            if str(d.get("name")) == args.device:
                chosen = d
                break
        if chosen is None:
            raise ValueError(
                f"--device '{args.device}' 不在配置文件中。可选: {', '.join(names)}"
            )
    else:
        chosen = devices[0]
        LOGGER.info("[DEV] 未指定 --device，默认用列表第一个: %s", chosen.get("name"))

    # 命令行显式给的 --esp32-host 优先；否则用配置文件的
    if not getattr(args, "_esp32_host_explicit", False):
        if chosen.get("esp32_host"):
            args.esp32_host = str(chosen["esp32_host"])
        if chosen.get("esp32_port") is not None:
            args.esp32_port = int(chosen["esp32_port"])
    # 旋转角度：命令行显式 --rotate 优先；否则用该设备在 JSON 里的 rotate
    if not getattr(args, "_rotate_explicit", False) and chosen.get("rotate") is not None:
        args.rotate = int(chosen["rotate"])
    LOGGER.info(
        "[DEV] 使用设备 '%s' -> esp32=%s:%s rotate=%s（可选设备: %s）",
        chosen.get("name"), args.esp32_host,
        args.esp32_port if args.esp32_port is not None else "默认",
        args.rotate if args.rotate is not None else "默认",
        ", ".join(names),
    )


# ============================================================
# CLI
# ============================================================

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="ESP32 双工 bridge v6（v5.2 + 摄像头旋转修复 + 模型视角回放视频）"
    )
    # 用哨兵 None 作默认，便于区分"用户显式给了 host"与"没给、走配置文件/默认"
    p.add_argument("--esp32-host", default=None,
                   help="ESP32 IP。显式给出时优先级最高，覆盖 --device-config 的选择。"
                        "未给且无 --device-config 时用内置默认。")
    p.add_argument("--esp32-port", type=int, default=None,
                   help="ESP32 端口（默认 80）")
    # ── 多设备配置文件（多副眼镜 IP 列表）──
    p.add_argument("--device-config", default=None,
                   help="设备列表 JSON 文件（含多副眼镜的 IP），见文件顶部 _load_device_config 说明")
    p.add_argument("--device", default=None,
                   help="从 --device-config 中按 name 选用哪副眼镜；不给则用列表第一个")
    p.add_argument("--gateway", default="localhost:8006")
    p.add_argument("--gateway-tls", action="store_true", default=True)
    p.add_argument("--no-tls", dest="gateway_tls", action="store_false")
    p.add_argument(
        "--prompt",
        default=(
            "你是一个友好的 AI 眼镜助手。请用简短的中文回答用户问题。"
            "用户通过佩戴的眼镜与你对话，你会看到用户视角的图像。"
        ),
    )
    p.add_argument("--force-listen", type=int, default=3)

    # ── 启动连接韧性（现场：眼镜可能忘开/没电/尚未连上 AP）──
    #   旧行为：等音频首包，写死 15s 超时 → abort/return（进程直接退出，需手动重敲命令）。
    #   新行为：等待时长可配（--connect-wait-s），并可循环重试（--connect-retry）不退出，
    #   期间周期性打印"等待眼镜连接…"，便于面板状态条显示"连接中"。
    p.add_argument("--connect-wait-s", type=float, default=120.0,
                   help="启动后等待 ESP32 音频首包的最长秒数（默认 120，旧版写死 15）")
    p.add_argument("--connect-retry", action="store_true", default=False,
                   help="等待超时后不退出、继续循环等待眼镜连接（现场推荐开启）")
    p.add_argument("--connect-poll-log-s", type=float, default=5.0,
                   help="等待连接期间每隔多少秒打印一次'等待眼镜连接…'（默认 5）")

    # ── 图像传输模式（受控对比：HD 固定，唯一变量是协议）──
    p.add_argument("--image-transport", choices=["http", "tcp"], default="http",
                   help="取图协议：http=GET /capture(现状)；tcp=独立 TCP 5000 端口(请求-响应)")
    p.add_argument("--image-tcp-port", type=int, default=5000,
                   help="TCP 图像通道端口（固件 image_tcp_task 监听端口，默认 5000）")

    # ── 主动打断 barge-in（移植自 recover；AI 说话时用户持续高音量→force_listen+flush）──
    # 注意：0629 用线性 RMS（与 --media-speech-rms 一致），不用 dBFS，避免引入两套度量。
    p.add_argument("--barge-enable", action="store_true", default=False,
                   help="启用主动打断：AI 输出时检测到用户持续说话则发 force_listen 并 flush 扬声器")
    p.add_argument("--barge-user-rms", type=float, default=0.02,
                   help="判定'用户在说话'的 RMS 阈值（线性，默认 0.02，约对应 -34dBFS）")
    p.add_argument("--barge-hold-ms", type=int, default=600,
                   help="用户音量需持续超阈值多少毫秒才触发打断（默认 600）")
    p.add_argument("--barge-cooldown-ms", type=int, default=2000,
                   help="两次打断之间的最小间隔，防抖（默认 2000）")
    p.add_argument("--barge-min-pending-ms", type=int, default=100,
                   help="扬声器未播音频≥此值才认为'AI 正在说'（默认 100）")
    p.add_argument("--image-timeout-s", type=float, default=1.0)
    p.add_argument("--image-abort-ms", type=int, default=1000,
                   help="取图进行中超过此毫秒即放弃本帧、放行音频(0=关闭)。默认1000")
    p.add_argument("--drop-image-on-timeout", action="store_true",
                   help="取图超时时本轮不带任何图、只发音频(默认关:超时则沿用上一帧旧图)")
    p.add_argument("--use-image", action="store_true", default=True)
    p.add_argument("--no-image", dest="use_image", action="store_false")
    p.add_argument("--audio-wait-timeout-s", type=float, default=3.0,
                   help="log every N seconds while waiting for timestamped audio backfill")
    p.add_argument("--audio-max-gap-fill-ms", type=int, default=0,
                   help="only gaps up to this size may be filled by ring.slice zeros; 0 waits for real audio")
    p.add_argument("--min-send-interval-s", type=float, default=0.25,
                   help="avoid flooding gateway while draining delayed ESP32 audio")
    p.add_argument("--media-speech-rms", type=float, default=0.006,
                   help="RMS threshold that pauses/aborts image capture")
    p.add_argument("--media-speech-hold-s", type=float, default=1.2,
                   help="keep image capture paused this long after speech-like audio")
    p.add_argument("--image-poll-s", type=float, default=0.2,
                   help="background image cache worker polling interval")
    p.add_argument("--image-min-interval-s", type=float, default=1.5,
                   help="minimum seconds between image capture attempts")
    p.add_argument("--image-speaking-interval-s", type=float, default=1.2,
                   help="说话期间的抓图间隔(秒)：说话时不再完全停抓图，而是按此间隔降频抓图，"
                        "保证'对准目标+开口提问'时画面仍在更新。设为很大值≈退回旧的说话时完全停。")
    p.add_argument("--image-abort-poll-s", type=float, default=0.05,
                   help="how often to abort /capture when audio becomes active")
    p.add_argument("--image-pause-backlog-ms", type=int, default=1200,
                   help="pause/abort image capture when audio ring is this far ahead")
    p.add_argument("--image-slow-ms", type=int, default=500,
                   help="log image capture as slow after this many milliseconds")
    p.add_argument("--image-resend-s", type=float, default=1.0,
                   help="minimum seconds before reattaching the same cached image")
    p.add_argument("--image-max-age-s", type=float, default=30.0,
                   help="do not attach cached images older than this many seconds")

    # v6 新增：摄像头顺时针旋转修正
    #   默认 None：未显式给出时，可由 --device-config 的设备 rotate 决定；
    #   两者都没给则回落到 90（见 main 里的回填）。命令行显式 --rotate 优先级最高。
    p.add_argument(
        "--rotate", type=int, default=None, choices=[0, 90, 180, 270],
        help="摄像头画面顺时针旋转角度（默认 90，修正眼镜上摄像头侧装）。"
             "会在送给模型 & 录像落盘之前统一旋转。"
             "可在 devices.json 里按设备设置 rotate；命令行显式给出则优先。",
    )
    p.add_argument(
        "--jpeg-quality", type=int, default=90,
        help="旋转后重新编码 JPEG 的质量（默认 90）",
    )

    # echo gate
    p.add_argument("--echo-mode", default="noise",
                   choices=["mute", "noise", "off"])
    p.add_argument("--echo-tail-ms", type=int, default=600)
    p.add_argument("--echo-noise-db", type=float, default=-60.0)

    # session
    p.add_argument("--save-session", default="./sessions")
    p.add_argument("--no-save", action="store_true")

    # local FunASR mirror
    p.add_argument("--enable-funasr", action="store_true",
                   help="mirror ESP32 mic audio to local FunASR and write user_input.jsonl")
    p.add_argument("--funasr-model", default="iic/SenseVoiceSmall")
    p.add_argument("--funasr-language", default="zh",
                   help="SenseVoice language: auto, zh, en, yue, ja, ko")
    p.add_argument("--funasr-device", default="auto",
                   help="auto, cpu, cuda:0")
    p.add_argument("--funasr-wait-ready-s", type=float, default=30.0,
                   help="wait for FunASR model before starting ESP32/gateway path")
    p.add_argument("--funasr-window-s", type=float, default=5.0,
                   help="ASR sliding window seconds")
    p.add_argument("--funasr-interval-s", type=float, default=2.0,
                   help="minimum seconds between ASR runs")
    p.add_argument("--funasr-min-rms", type=float, default=0.003,
                   help="skip ASR when audio RMS is below this threshold")
    p.add_argument("--funasr-utterance-end-s", type=float, default=0.8,
                   help="recognize once after this much low-energy audio")
    p.add_argument("--funasr-min-utterance-s", type=float, default=0.8,
                   help="drop shorter utterances before ASR")
    p.add_argument("--funasr-max-utterance-s", type=float, default=8.0,
                   help="force ASR when one utterance grows beyond this length")
    p.add_argument("--funasr-echo-suppress-s", type=float, default=2.5,
                   help="pause and clear ASR for this many seconds after model speech")
    p.add_argument("--funasr-speaker-pending-ms", type=int, default=120,
                   help="pause ASR while local speaker has more pending audio than this")
    p.add_argument("--asr-jsonl", default="user_input.jsonl",
                   help="relative to session dir unless absolute")
    p.add_argument("--find-object-mode", action="store_true",
                   help="append a find-object system prompt suffix at prepare time")
    p.add_argument("--describe-scene-mode", action="store_true",
                   help="append a describe-scene system prompt suffix at prepare time")
    p.add_argument("--read-text-mode", action="store_true",
                   help="append a read-text system prompt suffix at prepare time")
    p.add_argument("--visual-qa-mode", action="store_true",
                   help="append a visual-qa system prompt suffix at prepare time")
    p.add_argument("--multi-skill-mode", action="store_true",
                   help="append compact router + find-object + describe-scene prompts at prepare time")
    p.add_argument("--all-skills-mode", action="store_true",
                   help="with --multi-skill-mode, include every ASR/skills inject prompt")
    p.add_argument("--skill-reconnect-mode", action="store_true",
                   help="restart demo session with the skill detected by FunASR")
    p.add_argument("--runtime-skill-inject", action="store_true",
                   help="send FunASR-derived find/scene skill prompt as a runtime control event")
    p.add_argument("--skills-dir", default="ASR/skills",
                   help="directory containing router_rules.json and skill inject.md files")
    p.add_argument("--prompt-suffix-file", default=None,
                   help="optional UTF-8 prompt suffix file appended at prepare time")

    # 播放器（与 v5.2 相同）
    p.add_argument("--player-ring-s", type=float, default=5.0,
                   help="（兼容保留）FIFO 播放器无上界，此值仅用于起播日志展示（默认 5.0）")
    p.add_argument("--player-prebuffer-ms", type=float, default=200.0,
                   help="播放器起播前攒够多少毫秒再播（对应 gateway 前端 200ms，默认 200；0=不门控）")
    p.add_argument("--player-device-sr", type=int, default=0,
                   help="声卡播放采样率；0=自动查询设备原生率(推荐，对齐 gateway)。AI 音频(24000)会重采样到此率")
    p.add_argument("--player-hostapi", default="wasapi",
                   help="音频输出主机 API：wasapi(默认,现代低延迟,避开 MME 实时丢帧) / mme / default(系统默认)")
    p.add_argument("--player-dac-probe", action="store_true", default=False,
                   help="诊断：录下 callback 实际送声卡的音频到 logs/live_dac.wav，用于定位丢音在哪一跳")
    p.add_argument("--player-raw-probe", action="store_true", default=False,
                   help="诊断：把上游原始块连续拼接录到 live_raw.wav(无归位无补零)，看上游到底发了什么内容")
    p.add_argument("--player-stats-s", type=float, default=3.0,
                   help="播放器统计打印间隔秒（0=关，默认 3.0）")
    p.add_argument("--player-device", type=int, default=None,
                   help="sounddevice 输出设备 index（默认系统默认）")
    p.add_argument("--player-latency", default="low",
                   help="PortAudio latency 目标（'low'/'high'/数值秒；默认 low）")

    # v6 新增：模型视角视频
    # v6.6 移植：rerun 输入源 + bridge_ui Web 观测/回放
    p.add_argument("--rerun-from", default=None,
                   help="从已有 session 目录读取 user_raw.pcm + images/ + events.jsonl 重跑模型")
    p.add_argument("--rerun-speed", type=float, default=1.0,
                   help="rerun 推流速度倍率（默认 1.0 实时）")
    p.add_argument("--rerun-drain-s", type=float, default=5.0,
                   help="rerun 音频推完后等待模型说完的最大排空秒数")
    p.add_argument("--ui-port", type=int, default=8080,
                   help="bridge_ui Web 观测/回放端口（默认 8080）")
    p.add_argument("--no-ui", action="store_true", default=False,
                   help="不启动 bridge_ui Web 服务")

    # misc
    p.add_argument("--no-play", action="store_true")
    p.add_argument("--verbose-chunks", action="store_true")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p


_DEFAULT_ESP32_HOST = "10.100.7.160"
_DEFAULT_ESP32_PORT = 80


def main() -> None:
    args = build_arg_parser().parse_args()
    # 记录用户是否在命令行显式给了 --esp32-host（哨兵 None = 没给）
    args._esp32_host_explicit = args.esp32_host is not None
    # 记录用户是否显式给了 --rotate（哨兵 None = 没给，可由 device-config 决定）
    args._rotate_explicit = args.rotate is not None
    # 自动文件日志:logs/run_<时间戳>.txt,同时仍输出到控制台
    from datetime import datetime as _dt_now
    _log_dir = Path("logs")
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_file = _log_dir / f"run_{_dt_now.now().strftime('%Y%m%d_%H%M%S')}.txt"
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(_log_file, encoding="utf-8"),
        ],
    )
    LOGGER.info("[LOG] writing to %s", _log_file)

    # ── 应用设备配置文件（多副眼镜 IP 列表）──
    try:
        _apply_device_config(args)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        LOGGER.error("[DEV] 设备配置读取失败: %s", e)
        sys.exit(2)
    # 回填 host/port 默认值（命令行/配置文件都没给时用内置默认）
    if args.esp32_host is None:
        args.esp32_host = _DEFAULT_ESP32_HOST
    if args.esp32_port is None:
        args.esp32_port = _DEFAULT_ESP32_PORT
    # 回填 rotate 默认 90（命令行/配置文件都没给时）
    if args.rotate is None:
        args.rotate = 90
    LOGGER.info("[DEV] 最终 ESP32 目标: %s:%d rotate=%d°", args.esp32_host, args.esp32_port, args.rotate)

    while True:
        try:
            asyncio.run(run_bridge(args))
            break
        except SkillReconnect as e:
            _set_active_skill_args(args, e.skill)
            LOGGER.warning(
                "[SKILL] restarting with system prompt skill=%s; repeat the user question after reconnect",
                e.skill,
            )
            time.sleep(1.0)
            continue
        except KeyboardInterrupt:
            print("\nInterrupted.")
            sys.exit(0)


if __name__ == "__main__":
    main()
