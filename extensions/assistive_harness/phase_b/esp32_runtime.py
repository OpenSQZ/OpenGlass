"""ESP32 Phase B Harness runtime.

复用 rokid_runtime 的全部 harness 骨架（HarnessClient / GatewaySessionManager /
GatewayDuplexSession / PCSpeaker / OutputGate / 控制逻辑），只把设备 I/O 从
"PC 起 web server 等 Rokid 推" 换成 "PC 主动连 ESP32 拉音视频"。

数据方向对比：
  Rokid : 眼镜(APK) --push--> PC 的 aiohttp server
  ESP32 : PC --pull--> ESP32(CameraWebServer, /ws_audio_v2 + TCP:5000)

汇聚点完全一致：
  音频 -> audio_queue(给 gateway session) + harness.send_audio(镜像给 8021 ASR)
  图像 -> latest_frame.set() + harness.send_frame(镜像给 8021)
  AI 音频输出 -> PCSpeaker
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import ssl
import struct
import base64
import os
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp
import numpy as np

from .timing_probe import TimingProbe
from .funnel_gate import FunnelGate
from .session_recorder import SessionRecorder
from .recorder_live import LiveRecorder
try:
    from .bridge_ui import WebUIServer
except Exception:
    WebUIServer = None

# ── 复用 rokid_runtime 的 harness 骨架（一个字不改）──────────────────────
from .rokid_runtime import (
    RokidRuntimeConfig,
    GatewaySessionManager,
    GatewayDuplexSession,
    HarnessClient,
    PCSpeaker,
    NullSpeaker,
    OutputGate,
    DropOldestAudioQueue,
    AudioMirrorChunker,
    LatestFrame,
    RuntimeStats,
    CtrlCExitWatchdog,
    SkillRegistry,
    default_skills_config,
    pcm16le_to_float32,
    apply_pcm16_gain,
    now_ms,
    SAMPLE_RATE_IN,
)

LOG = logging.getLogger("assistive_harness.phase_b.esp32")

# ESP32 音频包头：<IIHH> = seq(4) ts_ms(4) n_samples(2) reserved(2)
#   注意：第4字段是 reserved(pad)，固件真丢包计数 g_ring_drops 未发到 wire，
#   PC 端用 seq 跳变推断真丢包。
ESP32_PKT_HDR = 12
# TCP 图像帧头 magic
_TCP_IMG_MAGIC = 0x55AA55AA

# 句子边界标点（作为"保护单位"的边界）
def _install_asr_tap(harness, live_rec) -> None:
    """把 8021 的 asr.transcript 截下来落盘（评分取提问时刻 t0 用）。

    为什么不是另开一条连接：8021 每个 client_id 有独立的 runtime 和 outbound 队列，
    asr.transcript 只投递给"送音频进来的那个 client"（即 esp32-phase-b），
    另开的 esp32-asr-tap 虽然能连上，但永远收不到任何消息。
    所以只能挂在同一条 HarnessClient 上——rokid_runtime 里加了个可选的 on_message
    旁路（默认 None，不影响原行为），这里赋值即可。

    8021 推的字段（server.py 148-154）：
        {"type":"asr.transcript", "asr_event_id":..., "utterance":"这上面写的是什么",
         "confidence":..., "final_at_ms":...,
         "suppressed": true/false, "reason": "..."}   ← 被回声抑制时带这两个
    注意：**被 echo drop 的也照样推**（抑制发生在 route/echo 之后），所以这里全都记，
    由评分侧按问句筛，不在这里过滤。
    """
    if live_rec is None or harness is None:
        return

    def _on_message(payload):
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "asr.transcript":
            return
        utt = str(payload.get("utterance") or "").strip()
        if not utt:
            return
        try:
            live_rec.log_asr(utt, payload.get("final_at_ms"),
                             payload.get("confidence"))
            LOG.info("[ASR] %s%s", utt,
                     "  (suppressed)" if payload.get("suppressed") else "")
        except Exception as e:
            LOG.warning("[ASR] 落盘失败: %s", e)

    harness.on_message = _on_message
    LOG.info("[ASR] transcript 旁路已挂在 HarnessClient 上")


class TurnPrinter:
    """把逐 chunk 的 text 碎片聚合成整段 turn（移植自 demo_esp32_duplex_0703）。

    -o 每个 chunk 只吐几个字，评分要的是"这次回答说了什么"。规则：
      text 非空 且（不在 turn 内 或 listen/speak 发生切换）→ 开新 turn，turn_idx +1
      end_of_turn → 返回 (turn_idx, 整段文本, is_listen) 并结束本 turn
    is_listen=True 的 turn 是 8021 ASR 识别出的用户说话（用于取提问结束时刻 t0）。
    """

    def __init__(self) -> None:
        self.turn_idx = 0
        self._in_turn = False
        self._turn_is_listen = None
        self._turn_text: list[str] = []

    def _reset(self) -> None:
        self._in_turn = False
        self._turn_text.clear()

    def feed(self, is_listen: bool, end_of_turn: bool, text: str):
        """返回 (turn_idx, 完整文本或"", is_listen)。

        除 end_of_turn 外，**listen/speak 切换时也会把上一段吐出来** ——
        原实现在切换时直接 _reset() 丢掉，模型一路流式说、end_of_turn 迟迟不来时
        整段就没了（bare 组实测 transcript 全空）。
        """
        flushed = None
        if text:
            if (not self._in_turn) or (self._turn_is_listen != is_listen):
                if self._in_turn and self._turn_text:
                    flushed = (self.turn_idx, "".join(self._turn_text),
                               bool(self._turn_is_listen))
                self._reset()
                self.turn_idx += 1
                self._in_turn = True
                self._turn_is_listen = is_listen
            self._turn_text.append(text)
        if flushed is not None and not end_of_turn:
            return flushed
        if end_of_turn:
            full_text = "".join(self._turn_text)
            cur_idx = self.turn_idx
            cur_listen = bool(self._turn_is_listen)
            self._reset()
            return cur_idx, full_text, cur_listen
        return self.turn_idx, "", is_listen


_SENT_PUNCT = "，。！？,.!?"


def _emit_chunk_img(web_ui, jpeg: bytes, idx: int, img_sent: bool, age_ms: int = 0):
    """把一帧图推给 bridge_ui 第一视角（type=chunk, img_b64）。无客户端时零负担。"""
    if web_ui is None or not getattr(web_ui, "live_clients", None):
        return
    try:
        b64 = base64.b64encode(jpeg).decode("ascii") if jpeg else None
        import asyncio as _a
        _a.create_task(web_ui.emit({
            "type": "chunk",
            "idx": int(idx),
            "img_b64": b64,
            "img_sent": bool(img_sent),
            "img_age_ms": int(age_ms),
        }))
    except Exception:
        pass


class SentenceTracker:
    """跟踪模型输出的句子边界，供漏斗"标点保护"策略用。

    handle_result 每个 chunk 调 update()：检测句末标点，累计标点序号。
    image_loop：reject 挂起时 snapshot() 记当前标点序号；之后
    boundary_reached(snap, speaker) 判断"自挂起后是否出现了新句末标点，
    且该段语音已经播完（speaker 队列基本清空）"。

    关键：模型文字生成远快于语音播放，文字标点会早于语音到达。所以"语音播到
    标点"不能靠文字标点时刻或墙钟估算，而要看 speaker.pending_ms —— 队列里还
    没播的语音降到很低时，才说明当前这段（含标点前的字）真的播完了。这样打断点
    落在句末标点、语音播完那一刻，不会把句子从中间截断。
    """
    def __init__(self):
        self._speaking = False
        self._punct_seq = 0               # 句末标点计数（每出现一个 +1）

    def update(self, text: str, audio_ms: float, is_listen: bool):
        self._speaking = not is_listen
        if text and any(p in text for p in _SENT_PUNCT):
            self._punct_seq += 1

    @property
    def speaking(self) -> bool:
        return self._speaking

    def snapshot(self):
        return {"punct_seq": self._punct_seq}

    def boundary_reached(self, snap, speaker=None):
        """自 snap 之后：出现了新句末标点，且该段语音已播完（speaker 队列近空）。"""
        if self._punct_seq <= snap["punct_seq"]:
            return False  # 还没出现新的句末标点 → 仍在保护当前句
        # 出现了新标点：等语音真的播到这里（speaker 队列基本清空）
        if speaker is not None:
            try:
                if speaker.pending_ms() > 150.0:  # 还有 >150ms 没播完 → 语音还没到标点
                    return False
            except Exception:
                pass
        return True

# ── 强制措施：播放离线预生成的 reject 提示 wav（模型音色，运行时不碰 chat/KV）──
_REJECT_WAV_CACHE = {}  # reason -> float32 ndarray（进程内缓存，只读一次盘）


def _load_reject_wav(wav_dir, reason):
    """读预生成的 {reason}.wav（24kHz 16-bit mono）→ float32 ndarray，带缓存。
    找不到返回 None（跳过播报，不阻断主流程）。"""
    import wave as _wave
    if reason in _REJECT_WAV_CACHE:
        return _REJECT_WAV_CACHE[reason]
    path = os.path.join(wav_dir, str(reason) + ".wav")
    if not os.path.isfile(path):
        _REJECT_WAV_CACHE[reason] = None
        return None
    try:
        with _wave.open(path, "rb") as wf:
            n = wf.getnframes()
            raw = wf.readframes(n)
        pcm16 = np.frombuffer(raw, dtype=np.int16)
        pcm = (pcm16.astype(np.float32) / 32768.0)
        _REJECT_WAV_CACHE[reason] = pcm
        return pcm
    except Exception:
        _REJECT_WAV_CACHE[reason] = None
        return None

# ── 强制措施（防幻觉）：reject 时用模型音色念提示 ─────────────────────────
#   复用 test_tts_speak 验证过的链路：走 gateway 的 /ws/chat(wss)，zero-shot TTS，
#   模型音色念任意文本，返回 float32 24kHz 音频 → PCSpeaker 播。
#   停/恢复走 8021(funnel.stop/funnel.resume)，和真人「停一下」同一条 controller。
_TTS_SYS_PROMPT = (
    "模仿音频样本的音色并生成新的内容。请用这种声音风格来为用户提供帮助。"
    "直接作答，不要有冗余内容。"
)


async def _speak_hint_via_chat(
    gateway_host: str,
    gateway_port: int,
    hint_text: str,
    speaker,
    ssl_ctx,
) -> bool:
    """用 chat zero-shot TTS 让模型用自己音色念 hint_text，播到 PCSpeaker。

    返回 True=念出并已入队播放；False=失败（不阻断主流程）。
    """
    url = f"wss://{gateway_host}:{gateway_port}/ws/chat"
    req = {
        "messages": [
            {"role": "system", "content": _TTS_SYS_PROMPT},
            {"role": "user", "content": "请朗读以下内容：" + hint_text},
        ],
        "streaming": False,
        "generation": {"max_new_tokens": 128, "length_penalty": 1.1},
        "tts": {"enabled": True},
        "use_tts_template": True,
        "omni_mode": False,
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url, ssl=ssl_ctx, max_msg_size=128 * 1024 * 1024) as ws:
                await ws.send_json(req)
                audio_b64 = None
                sr = 24000
                # 加超时：念提示走 /ws/chat,若 duplex 占着 worker 会排队。
                # 超时返回,避免永久阻塞 image_loop(否则后续抓图全停)。
                deadline = time.monotonic() + 8.0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        LOG.warning("[强制措施] 念提示超时(可能 /ws/chat 排队,duplex 占用 worker)")
                        return False
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
                    except asyncio.TimeoutError:
                        LOG.warning("[强制措施] 念提示超时(等 chat 响应)")
                        return False
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            return False
                        continue
                    data = json.loads(msg.data)
                    if data.get("type") == "done":
                        audio_b64 = data.get("audio_data")
                        sr = data.get("audio_sample_rate") or 24000
                        break
                    if data.get("type") == "error":
                        LOG.warning("[强制措施] chat TTS error: %s", data.get("error"))
                        return False
        if not audio_b64:
            LOG.warning("[强制措施] chat TTS 无音频返回")
            return False
        # audio_data 是 float32 base64（24kHz mono）→ PCSpeaker(也是 24kHz float32)
        import base64 as _b64
        pcm = np.frombuffer(_b64.b64decode(audio_b64), dtype=np.float32)
        # block_and_flush 之后 PCSpeaker.blocked=True，enqueue 会被丢；先 resume 解除
        await speaker.resume()
        await speaker.enqueue(pcm, generation=0)
        LOG.info("[强制措施] 念提示: %r (%d samples, %.2fs)",
                 hint_text, pcm.size, pcm.size / float(sr))
        return True
    except Exception as e:
        LOG.warning("[强制措施] 念提示失败: %s", e)
        return False


# ============================================================
# ESP32 音频输入：PC 主动连 ESP32 的 /ws_audio_v2，收 int16 PCM
#   （摘自 demo_esp32_duplex_0703 的 esp32_audio_reader，去掉 ring/live_rec，
#     直接把每包音频喂给 harness 骨架的 audio_queue + harness.send_audio）
# ============================================================
async def esp32_audio_reader(
    host: str,
    port: int,
    manager: GatewaySessionManager,
    harness: HarnessClient,
    audio_queue: DropOldestAudioQueue,
    audio_mirror: AudioMirrorChunker,
    stats: RuntimeStats,
    input_gain: float,
    stop_evt: asyncio.Event,
    probe=None,
    live_rec=None,
) -> None:
    url = f"ws://{host}:{port}/ws_audio_v2"
    LOG.info("[ESP32] audio WS: %s", url)
    backoff = 1.0
    last_log = time.monotonic()

    while not stop_evt.is_set():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url, heartbeat=30, max_msg_size=0) as ws:
                    LOG.info("[ESP32] audio WS connected")
                    backoff = 1.0
                    stats.audio_clients = 1
                    async for msg in ws:
                        if stop_evt.is_set():
                            break
                        if msg.type != aiohttp.WSMsgType.BINARY:
                            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                            continue
                        data = msg.data
                        if len(data) < ESP32_PKT_HDR:
                            continue
                        # 固件包头: seq(4) ts_ms(4) n_samples(2) reserved(2)
                        # 注意: 第4字段是 reserved(pad)，不是丢包数；真丢包用 seq 跳变推断
                        seq, ts_ms, n_samples, reserved = struct.unpack("<IIHH", data[:ESP32_PKT_HDR])
                        body = data[ESP32_PKT_HDR : ESP32_PKT_HDR + n_samples * 2]
                        if len(body) != n_samples * 2:
                            continue

                        # ── 与 rokid_audio 完全相同的接线 ──
                        raw = bytes(body)                       # int16 LE PCM
                        stats.audio_packets += 1
                        stats.audio_bytes += len(raw)
                        stats.last_audio_ms = now_ms()
                        samples = pcm16le_to_float32(raw)
                        packet_rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
                        packet_peak = float(np.max(np.abs(samples))) if samples.size else 0.0
                        stats.audio_rms = 0.8 * stats.audio_rms + 0.2 * packet_rms
                        stats.audio_peak = packet_peak
                        if packet_rms > 0.0:
                            stats.non_silent_audio_packets += 1
                        amplified = apply_pcm16_gain(raw, input_gain)
                        audio_queue.put_nowait(amplified)               # -> gateway session
                        for frame in audio_mirror.feed(amplified):
                            await harness.send_audio(frame)             # -> 8021 ASR 镜像
                        # -o record：录 user 音（原始 samples，未放大）
                        if live_rec is not None:
                            try:
                                live_rec.feed_user_raw(samples)
                            except Exception:
                                pass

                        if probe is not None:
                            probe.mark_audio(seq, n_samples, packet_rms)

                        now = time.monotonic()
                        if now - last_log >= 5.0:
                            LOG.info(
                                "[ESP32] rx=%d pkts seq=%d ts=%d rsv=%d rms=%.4f",
                                stats.audio_packets, seq, ts_ms, reserved, stats.audio_rms,
                            )
                            last_log = now
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            LOG.warning("[ESP32] audio WS error: %s, retry in %.1fs", e, backoff)
        finally:
            stats.audio_clients = 0
        if not stop_evt.is_set():
            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 10.0)

    LOG.info("[ESP32] audio reader stopped")


# ============================================================
# rerun 音频 reader：读 live_user.wav，按 40ms 包喂 audio_queue + harness
#   （移植自 rerun_source.local_pcm_reader，下游从 ring 改为 esp32 的
#    audio_queue + harness，其余节奏/回放逻辑一致）
# ============================================================
async def rerun_audio_reader(
    session_dir: str,
    manager: GatewaySessionManager,
    harness: HarnessClient,
    audio_queue: DropOldestAudioQueue,
    audio_mirror: AudioMirrorChunker,
    stats: RuntimeStats,
    input_gain: float,
    stop_evt: asyncio.Event,
    speed: float = 1.0,
    live_rec=None,
    ready_evt: Optional[asyncio.Event] = None,
    replay_t0=None,
    hard_stop: Optional[asyncio.Event] = None,
    done_evt: Optional[asyncio.Event] = None,
) -> None:
    if hard_stop is None:
        hard_stop = stop_evt
    # 等 duplex session 就绪再推，否则排队期间的音频全丢（见 _wait_gateway_ready）
    if ready_evt is not None:
        await ready_evt.wait()
    import wave as _wave
    wav_path = os.path.join(session_dir, "live_user.wav")
    if not os.path.isfile(wav_path):
        LOG.error("[RERUN] live_user.wav 不存在: %s", wav_path)
        stop_evt.set()
        return
    with _wave.open(wav_path, "rb") as w:
        sr_in = w.getframerate()
        pcm_i16 = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    _SR = 16000
    _PKT = 640  # 40ms @16k
    total = pcm_i16.size
    LOG.info("[RERUN] audio: %s  samples=%d  dur=%.2fs  sr=%d",
             wav_path, total, total / _SR, sr_in)

    packet_interval = (_PKT / _SR) / max(speed, 0.01)
    ts_ms = 0
    cursor = 0
    pkt = 0
    # next_tick 必须是"本函数真正开始推包的那一刻"。
    #   曾经这里写成 replay_t0()（就绪门放行的时刻），但那之后还要经过任务调度、
    #   读 wav、打日志，等跑到循环里时 next_tick 已经是过去时刻 →
    #   sleep_for 恒为负 → 每轮都不 sleep → 564 个包在几十毫秒内全灌进 audio_queue
    #   → 队列容量 96、DropOldest 把开头的提问全丢了，模型只听到最后 3.8 秒的尾音。
    #   （stats 里的 audio=25.0pps 统计的是"推进队列"的数量，正好掩盖了这个问题。）
    # 图那边按 frames.jsonl 的 t 等待、以 replay_t0 为零点，两边起点差几十毫秒，
    # 对 1s 粒度的 chunk 没有影响。
    next_tick = time.monotonic()
    # 只受 hard_stop 控制，不再被 stop_evt 掐断。
    #   原来是 `while cursor < total and not stop_evt.is_set()`：
    #   图那条路径放完图会 set stop_evt，而素材的图轮数常少于音频秒数
    #   （drugbox 16 轮图 vs 22.5s 音频）→ 图先放完就把音频推送掐了，
    #   后半段提问根本没进模型。两条流应各自跑完自己的。
    while cursor < total and not hard_stop.is_set():
        end = min(cursor + _PKT, total)
        chunk_i16 = pcm_i16[cursor:end]
        cursor = end
        pkt += 1
        raw = chunk_i16.astype("<i2").tobytes()
        try:
            amplified = apply_pcm16_gain(raw, input_gain)
            audio_queue.put_nowait(amplified)
            for frame in audio_mirror.feed(amplified):
                await harness.send_audio(frame)
            stats.audio_packets = pkt
            # 录 rerun 的 user 音进 LiveRecorder（否则 mp4 的 user 轨为空）
            if live_rec is not None:
                try:
                    live_rec.feed_user_raw(chunk_i16.astype(np.float32) / 32768.0)
                except Exception:
                    pass
        except Exception as e:
            LOG.warning("[RERUN] audio feed err: %s", e)
        ts_ms += 40
        next_tick += packet_interval
        sleep_for = next_tick - time.monotonic()
        if sleep_for > 0:
            # 这里等的是 hard_stop，不是 stop_evt。
            #   之前 while 条件改成了 hard_stop，但循环体里仍 await stop_evt 并 break，
            #   等于没改——图放完 set stop_evt 照样把音频推送掐断。
            try:
                await asyncio.wait_for(hard_stop.wait(), timeout=sleep_for)
                break
            except asyncio.TimeoutError:
                pass

    if done_evt is not None:
        done_evt.set()
    LOG.info("[RERUN] audio 推完 %d 包 (%.2fs)，等模型说完…", pkt, ts_ms / 1000.0)
    # 音频推完不立即 stop。注意：这里**不能** await stop_evt 就 break ——
    # 图那条路径放完图也会 set stop_evt，两条互相踩，结果谁先到谁把另一条掐了
    # （图少的素材尤其明显：图很快放完 → 直接收工 → 模型话没说完）。
    # 这里独立按时间等，让模型有机会把最后一句说完；真正的结束由 image_loop
    # 的静默判据 + 尾巴决定，或走这里的兜底。
    waited = 0.0
    while waited < 120.0:
        if stop_evt.is_set():
            # 另一条已判定结束，再宽限一小段让 TTS 播完
            await asyncio.sleep(2.0)
            break
        await asyncio.sleep(0.5)
        waited += 0.5
    if not stop_evt.is_set():
        stop_evt.set()
    LOG.info("[RERUN] audio reader stopped")


# ============================================================
# rerun 图像 loop：用 LocalImageSource 按 chunk 顺序读 best 图，直接喂模型
#   （-o rerun 不重跑 funnel——best 已是筛过的，直接 send_frame）
# ============================================================
async def rerun_image_loop(
    session_dir: str,
    latest_frame,
    harness: HarnessClient,
    stats: RuntimeStats,
    interval_s: float,
    stop_evt: asyncio.Event,
    web_ui=None,
    live_rec=None,
    ready_evt: Optional[asyncio.Event] = None,
    replay_t0=None,
    done_evt: Optional[asyncio.Event] = None,
    peer_done: Optional[asyncio.Event] = None,
    speaker=None,
    sent_tracker=None,
    tail_wait_s: float = 30.0,
) -> None:
    if ready_evt is not None:
        await ready_evt.wait()
    try:
        from .rerun_source import LocalImageSource
    except Exception as e:
        LOG.error("[RERUN] 无法导入 LocalImageSource: %s", e)
        stop_evt.set()
        return
    from pathlib import Path as _Path
    try:
        src = LocalImageSource(_Path(session_dir))
    except Exception as e:
        LOG.error("[RERUN] LocalImageSource 初始化失败: %s", e)
        stop_evt.set()
        return
    # 按 chunk_idx 升序回放
    idxs = sorted(src._map.keys())
    LOG.info("[RERUN] image loop: %d 帧待回放", len(idxs))
    for cidx in idxs:
        if stop_evt.is_set():
            break
        jpeg = await src.capture(cidx)
        if jpeg:
            ts = now_ms()
            latest_frame.set(jpeg, ts)
            stats.image_count += 1
            await harness.send_frame(jpeg, latest_frame.sequence, ts)
            LOG.info("[RERUN] send frame idx=%d (%d bytes)", cidx, len(jpeg))
            _emit_chunk_img(web_ui, jpeg, latest_frame.sequence, True)
            if live_rec is not None:
                try:
                    live_rec.on_frame(jpeg, latest_frame.sequence)
                except Exception:
                    pass
        try:
            await asyncio.wait_for(stop_evt.wait(), timeout=interval_s)
            break
        except asyncio.TimeoutError:
            pass
    LOG.info("[RERUN] image loop 回放完毕")
    if done_evt is not None:
        done_evt.set()
    # -o rerun 的收尾。之前这条路径图放完就什么都不做，结束全靠
    # rerun_audio_reader 里 `while waited < 120.0` 的兜底干等两分钟
    #（日志表现：音频推完后一串 audio=0.0pps 的 STATS，两分钟才退），
    # 而且 --rerun-tail-wait-s 对它无效。
    # 判定与 esp32_image_loop 保持一致：先等音频也推完，再看模型说完没有
    #（队列空 且 不在 speak，连续保持 2s 才算），最后加 2s 尾巴。
    if peer_done is not None and not peer_done.is_set():
        LOG.info("[RERUN] 图放完，等音频推完…")
        try:
            await asyncio.wait_for(peer_done.wait(), timeout=180.0)
        except asyncio.TimeoutError:
            LOG.warning("[RERUN] 等音频超时")
    _QUIET_HOLD_S, _TAIL_S = 2.0, 2.0
    _quiet_since = None
    _deadline = time.monotonic() + tail_wait_s
    while time.monotonic() < _deadline and not stop_evt.is_set():
        try:
            pending = speaker.pending_ms() if speaker is not None else 0.0
        except Exception:
            pending = 0.0
        speaking = bool(getattr(sent_tracker, "speaking", False)) if sent_tracker else False
        if pending <= 50 and not speaking:
            if _quiet_since is None:
                _quiet_since = time.monotonic()
            elif time.monotonic() - _quiet_since >= _QUIET_HOLD_S:
                break
        else:
            _quiet_since = None
        await asyncio.sleep(0.25)
    try:
        await asyncio.wait_for(stop_evt.wait(), timeout=_TAIL_S)
    except asyncio.TimeoutError:
        pass
    LOG.info("[RERUN] 结束（静默保持%.1fs + 尾巴%.1fs）", _QUIET_HOLD_S, _TAIL_S)
    stop_evt.set()


# ============================================================
# ESP32 图像输入：TCP 5000 持久连接，请求-响应取裸 JPEG
#   （摘自 demo_esp32_duplex_0703 的 TcpImageClient，接口不变）
#   帧头(20B 小端): magic(4) frame_id(4) w(2) h(2) fmt(1) reserved(3) len(4)
# ============================================================
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
            LOG.info("[TCP-IMG] connected to %s:%d", self.host, self.port)
            return True
        except Exception as e:
            LOG.debug("[TCP-IMG] connect failed: %s", e)
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
                self._writer.write(b"C")
                await self._writer.drain()
                hdr = await asyncio.wait_for(self._reader.readexactly(20), timeout=timeout_s)
                magic, frame_id, w, h = struct.unpack_from("<IIHH", hdr, 0)
                (length,) = struct.unpack_from("<I", hdr, 16)
                if magic != _TCP_IMG_MAGIC:
                    LOG.warning("[TCP-IMG] bad magic 0x%08x, reconnect", magic)
                    await self._close()
                    return None
                if length == 0:
                    return None
                if length < 0 or length > 4 * 1024 * 1024:
                    LOG.warning("[TCP-IMG] insane len=%d, reconnect", length)
                    await self._close()
                    return None
                data = await asyncio.wait_for(self._reader.readexactly(length), timeout=timeout_s)
                return bytes(data)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOG.warning("[TCP-IMG] capture failed: %r, reconnect", e)
                await self._close()
                return None


# ============================================================
# funnel rerun：把录制的整簇多帧当图源，复用 esp32_image_loop 重跑漏斗+播报
#   RecordedImageClient.capture() 按 frames.jsonl 每轮整簇顺序吐帧，
#   funnel.run_once 调 N 次凑一簇 → 重判 send/reject → 播报/标点保护，逻辑全复用。
# ============================================================
class RecordedImageClient:
    def __init__(self, session_dir: str, n_frames: int, no_funnel: bool = False):
        import json as _json
        self.dir = session_dir
        self.images_dir = os.path.join(session_dir, "images")
        self.n_frames = max(1, int(n_frames))
        self._rounds = []   # 每轮: [jpg_bytes, ...]
        fr_path = os.path.join(session_dir, "frames.jsonl")
        self._round_names = []   # 每轮的帧文件名（供 record 复用）
        self._round_t = []       # 每轮录制时刻 t（秒），用于按原始时间轴回放
        with open(fr_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except Exception:
                    continue
                names = rec.get("frames") or []
                jpgs = []
                keep_names = []
                for nm in names:
                    p = os.path.join(self.images_dir, nm)
                    if os.path.isfile(p):
                        with open(p, "rb") as fp:
                            jpgs.append(fp.read())
                        keep_names.append(nm)
                if jpgs:
                    self._rounds.append(jpgs)
                    self._round_names.append(keep_names)
                    self._round_t.append(float(rec.get("t", len(self._rounds) - 1)))
        LOG.info("[RERUN-IMG] 载入 %d 轮，每轮≤%d帧%s",
                 len(self._rounds), self.n_frames,
                 "（无漏斗：每轮取中间张代表帧）" if no_funnel else "（funnel：整簇逐帧）")
        self._round_i = 0
        self._frame_i = 0
        self._no_funnel = no_funnel
        self.exhausted = False
        self._t0 = None          # 回放起点（优先用外部注入的统一零点）
        self.replay_t0 = None    # 由 runtime 注入，与音频共用
        # 整份 record 一张图都没有（图像链路当时丢了 / 手动删空 images）：
        #   这是有效素材（对照：无漏斗→模型 0 图编场景；有漏斗→全程 reject no_frames）。
        #   此时不能置 exhausted，否则会被当成"图放完了"秒退；让音频推完来结束整场。
        self._no_images = (len(self._rounds) == 0)
        if self._no_images:
            LOG.info("[RERUN-IMG] 这份 record 没有可用图像 → 全程无图回放"
                     "（由音频长度决定结束）")

    def current_round_cluster(self):
        """返回当前轮的整簇 (jpg_list, name_list)，供无漏斗 rerun 存 record 用。"""
        i = self._round_i
        if 0 <= i < len(self._rounds):
            return self._rounds[i], self._round_names[i]
        return [], []

    async def _wait_round_time(self):
        """按录制时刻回放：等到墙钟走到本轮的 t 再吐这一轮的帧。

        录制时每轮耗时并不均匀（对焦轮 ~1.9s、reject 播报轮 2.3~4s），若 rerun 按
        image_loop 的固定 interval_s 匀速吃图，就会以数倍速把图吃光（实测 33s 的录制
        11s 就放完）。音频是按真实时长回放的，用 frames.jsonl 的 t 对齐，两边同一条
        时间轴。
        """
        if self._t0 is None:
            # 优先用统一回放零点（与音频同源）；没有才退回"第一次 capture 那一刻"
            self._t0 = self.replay_t0 if self.replay_t0 is not None else time.monotonic()
        i = self._round_i
        if 0 <= i < len(self._round_t):
            target = self._round_t[i]
            delay = target - (time.monotonic() - self._t0)
            if delay > 0:
                await asyncio.sleep(min(delay, 10.0))

    async def capture(self, timeout_s: float = 1.0):
        if self._no_images:
            # 全程无图：一直返回 None，但不置 exhausted（不提前结束）。
            # 有漏斗 → run_once 拿到 0 帧 → reject(no_frames) → 播"没有拿到画面"；
            # 无漏斗 → 不 send → 模型 0 图 → 暴露编场景。
            await asyncio.sleep(0.05)
            return None
        if self._round_i >= len(self._rounds):
            self.exhausted = True
            return None
        # 只在一轮的第一帧上等待，同一轮内的多帧连续吐（模拟原始高频抓帧）
        if self._frame_i == 0:
            await self._wait_round_time()
        rnd = self._rounds[self._round_i]
        if self._no_funnel:
            # 无漏斗（对照组）：每轮取一张代表帧（中间张，模拟"随手抓一张就发"），
            # 每次 capture 直接进入下一轮。含模糊图会照发 → 暴露幻觉。
            jpg = rnd[len(rnd) // 2]
            self._round_i += 1
            return jpg
        # funnel：一帧帧吐整簇，供 run_once 调 N 次凑一簇
        jpg = rnd[self._frame_i % len(rnd)]
        self._frame_i += 1
        if self._frame_i >= self.n_frames:
            self._frame_i = 0
            self._round_i += 1
        return jpg

    async def _close(self):
        pass


# ============================================================
# ESP32 图像轮询循环：定时用 TCP 取图 -> latest_frame + harness.send_frame
# ============================================================
async def esp32_image_loop(
    client: TcpImageClient,
    latest_frame: LatestFrame,
    harness: HarnessClient,
    stats: RuntimeStats,
    interval_s: float,
    timeout_s: float,
    stop_evt: asyncio.Event,
    probe=None,
    funnel=None,
    recorder=None,
    speaker=None,
    gateway_host: str = "127.0.0.1",
    gateway_port: int = 8040,
    ssl_ctx=None,
    force_measure: bool = False,
    manager=None,
    reject_wav_dir: str = "assets/reject_wav",
    sent_tracker=None,
    live_rec=None,
    web_ui=None,
    ready_evt: Optional[asyncio.Event] = None,
    tail_wait_s: float = 30.0,
    no_reject: bool = False,
    peer_done: Optional[asyncio.Event] = None,
    done_evt: Optional[asyncio.Event] = None,
) -> None:
    if ready_evt is not None:
        await ready_evt.wait()
    LOG.info("[ESP32] image loop start (interval=%.2fs, funnel=%s)",
             interval_s, "on" if funnel else "off")
    _last_grab_mono = None

    # ── 强制措施状态 ──
    _funnel_stopped = False        # 是否已发过 funnel.stop（在停住态，等好图 resume）
    _last_hint_mono = 0.0          # 上次念提示的时刻（节流用）
    _HINT_THROTTLE_S = 5.0         # 同类连续 reject 的念提示节流间隔
    _pending_reject = None         # 标点保护：挂起中的 reject（等标点/超时再复查）
    # ── 「持续坏」检测：3s 窗口内 reject≥2 → 判定输出基于坏图=幻觉，直接打断+压制 ──
    #   覆盖"错错错对"场景（前半也错，标点保护不适用）。窗口单位=判定/chunk(每≈1s)。
    #   依据：模型拿图→输出约 1-1.5s；2s 都坏则幻觉必已在输出，必须闭嘴。
    _judge_history = []            # 最近判定时刻+是否reject: [(mono, is_reject), ...]
    _WINDOW_S = 3.0                # 窗口长度
    _BAD_THRESH = 2               # 窗口内 reject≥此 → 持续坏，直接打断(跳过标点保护)
    # _funnel_active：念提示临界区标志（预留给兜底：念提示那几秒真人喊话的处理）
    #   TODO(兜底,下次实现): 念提示期间若收到真人 STOP/RESUME/RESET，
    #   先掐掉提示音(speaker.block_and_flush)+清此标志，再执行真人指令，避免撞车。
    #   当前只占位，不实现逻辑。
    _funnel_active = False  # noqa: F841  (预留)

    async def _capture_one():
        return await client.capture(timeout_s=timeout_s)

    while not stop_evt.is_set():
        t0 = time.monotonic()
        # funnel rerun：录制的整簇多帧放完了 → 优雅结束（不报 no_frames）
        if getattr(client, "exhausted", False):
            LOG.info("[FUNNEL-RERUN] 录制图已放完")
            # 图轮数常少于音频秒数（drugbox 16 轮 vs 22.5s），不能图一放完就收工，
            # 否则后半段音频（可能正含提问）根本没推给模型。先等音频也推完。
            if peer_done is not None and not peer_done.is_set():
                LOG.info("[FUNNEL-RERUN] 等音频推完…")
                try:
                    await asyncio.wait_for(peer_done.wait(), timeout=180.0)
                except asyncio.TimeoutError:
                    LOG.warning("[FUNNEL-RERUN] 等音频超时")
            LOG.info("[FUNNEL-RERUN] 两条流都完事，等模型说完后结束")
            # 等模型把话说完再 stop。判据不能只看"队列空"——模型是流式产出的，
            # 某一瞬间队列排空只是"下一段还没到"，不代表这轮结束（图少的素材尤其明显：
            # 图很快放完，恰好撞上队列空，就把还没说完的话掐了）。
            # 改为：队列必须连续 _QUIET_HOLD_S 保持空，且 sent_tracker 不在 speak 中，
            # 再加一段固定尾巴，给最后一句 TTS 留出播放时间。
            _QUIET_HOLD_S = 2.0
            _TAIL_S = 2.0
            _quiet_since = None
            _deadline = time.monotonic() + tail_wait_s
            while time.monotonic() < _deadline and not stop_evt.is_set():
                try:
                    pending = speaker.pending_ms() if speaker is not None else 0.0
                except Exception:
                    pending = 0.0
                speaking = False
                if sent_tracker is not None:
                    speaking = bool(getattr(sent_tracker, "speaking", False))
                if pending <= 50 and not speaking:
                    if _quiet_since is None:
                        _quiet_since = time.monotonic()
                    elif time.monotonic() - _quiet_since >= _QUIET_HOLD_S:
                        break
                else:
                    _quiet_since = None
                await asyncio.sleep(0.25)
            # 尾巴：让最后一句 TTS 播完，也给模型一点补充的机会
            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=_TAIL_S)
            except asyncio.TimeoutError:
                pass
            LOG.info("[FUNNEL-RERUN] 结束（静默保持%.1fs + 尾巴%.1fs）",
                     _QUIET_HOLD_S, _TAIL_S)
            stop_evt.set()
            break
        try:
            if funnel is None:
                # ── 无漏斗：取 1 帧直发（含模糊图，暴露幻觉）──
                # record 仍存整簇（mp4 用全部采集帧，和有漏斗一致，只是无 reject 标注）
                cluster_jpgs, cluster_names = ([], [])
                if hasattr(client, "current_round_cluster"):
                    cluster_jpgs, cluster_names = client.current_round_cluster()
                jpeg = await client.capture(timeout_s=timeout_s)
                grab_ms = (time.monotonic() - t0) * 1000.0
                if jpeg:
                    ts = now_ms()
                    latest_frame.set(jpeg, ts)
                    stats.image_count += 1
                    await harness.send_frame(jpeg, latest_frame.sequence, ts)
                    _emit_chunk_img(web_ui, jpeg, latest_frame.sequence, True)
                    if probe is not None:
                        since_last = (t0 - _last_grab_mono) * 1000.0 if _last_grab_mono else 0.0
                        probe.mark_grab(latest_frame.sequence, grab_ms, len(jpeg), since_last)
                        _last_grab_mono = t0
                    # 存整簇给 record（无漏斗：send=True 无 reason）
                    if live_rec is not None and cluster_jpgs:
                        _dec = type("D", (), {"frames": cluster_jpgs, "best_index": len(cluster_jpgs)//2,
                                              "send": True, "reason": ""})()
                        try:
                            await asyncio.to_thread(
                                live_rec.on_funnel_round, _dec, latest_frame.sequence)
                        except Exception as e:
                            LOG.warning("[LIVE] on_funnel_round(no-funnel) err: %s", e)
            else:
                # ── 有漏斗：一轮判定，合格才 send_frame ──
                decision = await funnel.run_once(_capture_one)
                # 统一 record：每轮整簇多帧 + 判定 → live_rec（供三种 rerun）。
                #   必须放在所有分支之前：持续坏分支末尾有 continue，放在最后会把
                #   触发压制的那些轮整簇漏录（实测 42s 只落下 8 轮）。录制是原始素材，
                #   不该因为走了哪条处理路径而缺失。
                if live_rec is not None:
                    try:
                        await asyncio.to_thread(
                            live_rec.on_funnel_round, decision, latest_frame.sequence)
                    except Exception as e:
                        LOG.warning("[LIVE] on_funnel_round err: %s", e)
                grab_ms = decision.timings.get("grab_ms", 0.0)
                async def _do_reject_interrupt(reason):
                    """真正执行打断：停 duplex → 播 wav → 恢复。"""
                    wav_pcm = _load_reject_wav(reject_wav_dir, reason)
                    if wav_pcm is None:
                        LOG.warning("[强制措施] 无 %s.wav，跳过播报", reason)
                        return
                    try:
                        await harness.send({"type": "funnel.stop", "reason": reason})
                        LOG.info("[强制措施] funnel.stop 已发 (%s)", reason)
                    except Exception as e:
                        LOG.warning("[强制措施] funnel.stop 失败: %s", e)
                    await asyncio.sleep(0.35)  # 等 STOP 经 8021→rokid→block_and_flush
                    try:
                        await speaker.resume()
                        await speaker.enqueue(wav_pcm, generation=0)
                        dur = len(wav_pcm) / 24000.0
                        LOG.info("[强制措施] 播 reject wav: %s (%.2fs)", reason, dur)
                        if live_rec is not None:
                            live_rec.log_event("HINT", f"{reason} ({dur:.2f}s)")
                        await asyncio.sleep(dur + 0.2)
                    except Exception as e:
                        LOG.warning("[强制措施] 播 wav 失败: %s", e)
                    try:
                        await harness.send({"type": "funnel.resume", "reason": "hint_done"})
                        LOG.info("[强制措施] funnel.resume 已发（提示播完）")
                    except Exception as e:
                        LOG.warning("[强制措施] funnel.resume 失败: %s", e)

                # ── 消融用：选 best 但永远放行（关闭工作③的拒绝/播报）──
                #   用途：把"多帧选 best"(工作②)的收益从"拒绝坏图"(工作③)里剥出来。
                #   对照 bare（每轮取中间帧直发），本 arm 每轮取 best 直发，
                #   下游模型侧完全一致，差异只来自选图。
                if no_reject:
                    _b = decision.best
                    if _b:
                        ts = now_ms()
                        latest_frame.set(_b, ts)
                        stats.image_count += 1
                        await harness.send_frame(_b, latest_frame.sequence, ts)
                        _emit_chunk_img(web_ui, _b, latest_frame.sequence, True)
                        if live_rec is not None:
                            try:
                                await asyncio.to_thread(
                                    live_rec.on_frame, _b, latest_frame.sequence)
                            except Exception as e:
                                LOG.warning("[LIVE] on_frame err: %s", e)
                        LOG.info("[漏斗-放行] best (原判定=%s)", decision.reason)
                        # 消融 arm 不执行拒绝，但**判别结果要留痕**：
                        # 这一轮漏斗本来会不会拦、拦的理由是什么，是分析判别倾向的依据。
                        if live_rec is not None and not decision.send:
                            live_rec.log_event(
                                "WOULD-REJECT",
                                f"{decision.reason} {decision.hint or ''}")
                    else:
                        LOG.info("[漏斗-放行] 本轮无 best（%s），跳过", decision.reason)
                    elapsed = time.monotonic() - t0
                    try:
                        await asyncio.wait_for(stop_evt.wait(),
                                               timeout=max(0.0, interval_s - elapsed))
                    except asyncio.TimeoutError:
                        pass
                    continue

                # ── 「持续坏」检测（优先于标点保护）──
                #   3s 窗口内"真坏"reject≥2 → 输出基于坏图=幻觉。此时不走标点保护
                #   （前半也错，不值得保护），直接触发"停→播提示→恢复"。
                #   need_focus 不算坏：它是系统正在对焦(run_once 已发 /reg 重抓)，
                #   属于内部自愈，不是用户造成的持续坏，算进去会误判、干扰对焦流程。
                #   "真坏" = reject 且 reason 不是 need_focus（severe_shake/unstable/
                #   too_dark/orient 等，要用户动手的）。
                _now = time.monotonic()
                _is_real_bad = (not decision.send) and (decision.reason != "need_focus")
                _judge_history.append((_now, _is_real_bad))
                _judge_history[:] = [(t, r) for (t, r) in _judge_history
                                     if _now - t <= _WINDOW_S]
                _bad_in_window = sum(1 for (_t, r) in _judge_history if r)

                if _bad_in_window >= _BAD_THRESH and _is_real_bad:
                    _pending_reject = None  # 持续坏优先，取消标点保护挂起
                    # 到这里必是"真坏"(severe_shake/unstable/too_dark/orient)。
                    # 直接触发完整"停→播提示→恢复"（复用 _do_reject_interrupt，
                    # 停模型输出不杀死、播完恢复）。耗时≈播报时长，天然间隔。
                    LOG.info("[持续坏] 3s内%d次真坏reject，打断+播提示（停→播→恢复）",
                             _bad_in_window)
                    if live_rec is not None:
                        live_rec.log_event(
                            "PERSIST-BAD",
                            f"3s内{_bad_in_window}次真坏 → 打断 ({decision.reason})")
                    await _do_reject_interrupt(decision.reason)
                    elapsed = time.monotonic() - t0
                    try:
                        await asyncio.wait_for(stop_evt.wait(),
                                               timeout=max(0.0, interval_s - elapsed))
                    except asyncio.TimeoutError:
                        pass
                    continue

                # ── 「标点保护」核心：突发 reject 不立即打断，保护当前段语音播放到
                #    下一个句末标点，到标点那一刻复查当时判定；仍 reject 才打断，否则无事。
                #    挂起期间的 send/reject 都不算数，只看"到标点那一刻"的判定。
                #    （snapshot 记标点序号，boundary_reached 判断新标点+其语音已播完）
                if force_measure and speaker is not None and _pending_reject is not None:
                    # 正在保护中：只判断"是否到标点(语音播完)或超时"，到了才用当时判定复查
                    now_mono = time.monotonic()
                    reached = (sent_tracker is not None
                               and sent_tracker.boundary_reached(
                                   _pending_reject["snap"], speaker))
                    timed_out = (now_mono - _pending_reject["since"]) >= 1.5
                    if reached or timed_out:
                        # 到标点/超时 → 复查此刻判定
                        if decision.send:
                            LOG.info("[标点保护] %s，此刻已恢复(send)，无事发生",
                                     "到标点" if reached else "超时1.5s")
                            _pending_reject = None
                        elif decision.reason == "need_focus":
                            _pending_reject = None  # 复查是对焦，静默
                        else:
                            # 仍 reject → 打断（受节流约束）
                            LOG.info("[标点保护] %s，仍 reject(%s)，打断",
                                     "到标点" if reached else "超时1.5s", decision.reason)
                            if now_mono - _last_hint_mono >= _HINT_THROTTLE_S:
                                _last_hint_mono = now_mono
                                _pending_reject = None
                                await _do_reject_interrupt(decision.reason)
                            else:
                                _pending_reject = None  # 被节流

                if decision.send and decision.best:
                    ts = now_ms()
                    latest_frame.set(decision.best, ts)
                    stats.image_count += 1
                    await harness.send_frame(decision.best, latest_frame.sequence, ts)
                    LOG.info("[漏斗] send (%s)", decision.reason)
                    _emit_chunk_img(web_ui, decision.best, latest_frame.sequence, True)
                    # -o record：只录真发送给模型的 best 图（chunk_idx = sequence）
                    if live_rec is not None:
                        try:
                            await asyncio.to_thread(
                                live_rec.on_frame, decision.best, latest_frame.sequence)
                        except Exception as e:
                            LOG.warning("[LIVE] on_frame err: %s", e)
                    # 注意：send 不清 _pending_reject，保护期只看到标点那一刻
                else:
                    LOG.info("[漏斗] reject(%s) -> hint: %s",
                             decision.reason, decision.hint)
                    if live_rec is not None:
                        live_rec.log_event("REJECT", f"{decision.reason} {decision.hint or ''}")
                    if force_measure and speaker is not None and decision.reason != "need_focus":
                        now_mono = time.monotonic()
                        if _pending_reject is not None:
                            pass  # 已在保护中，上面已处理，不重复挂起
                        elif sent_tracker is not None and sent_tracker.speaking:
                            # 模型正念字：挂起，保护到下一个标点再复查
                            _pending_reject = {
                                "since": now_mono,
                                "reason": decision.reason,
                                "snap": sent_tracker.snapshot(),
                            }
                            LOG.info("[标点保护] speak中，reject(%s)挂起，等语音播到标点或超时1.5s",
                                     decision.reason)
                        else:
                            # 模型没念字（listen/静默）→ 无需保护，直接打断（受节流）
                            if now_mono - _last_hint_mono >= _HINT_THROTTLE_S:
                                _last_hint_mono = now_mono
                                await _do_reject_interrupt(decision.reason)
                if probe is not None:
                    since_last = (t0 - _last_grab_mono) * 1000.0 if _last_grab_mono else 0.0
                    jb = len(decision.best) if decision.best else 0
                    tm = decision.timings
                    probe.mark_grab(
                        latest_frame.sequence, grab_ms, jb, since_last,
                        reason=decision.reason,
                        judge_ms=tm.get("judge_ms"),
                        af_ms=tm.get("af_ms"),
                        n_frames=tm.get("n_frames"),
                    )
                    _last_grab_mono = t0
        except Exception as e:
            LOG.warning("[ESP32] image loop error: %s", e, exc_info=True)
        # 控制取图节奏
        elapsed = time.monotonic() - t0
        try:
            await asyncio.wait_for(stop_evt.wait(), timeout=max(0.0, interval_s - elapsed))
        except asyncio.TimeoutError:
            pass
    LOG.info("[ESP32] image loop stopped")


# ============================================================
# ESP32 Runtime：继承 rokid 的 PhaseBRokidRuntime，复用全部 harness 骨架，
#   只覆盖 start()：不起 web server，改起两个 ESP32 主动拉取 task。
# ============================================================
from .rokid_runtime import PhaseBRokidRuntime


def _insecure_ssl_for_wss(url: str) -> Optional[ssl.SSLContext]:
    """wss:// 且自签名证书时，返回一个不校验证书的 ssl context；ws:// 返回 None。

    与 rokid GatewayDuplexSession._ssl_context 同款做法（check_hostname=False,
    verify_mode=CERT_NONE），用于连本地自签名的 8021。"""
    if not url.lower().startswith("wss://"):
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class TlsHarnessClient(HarnessClient):
    """与 HarnessClient 完全一致，仅在 ws_connect 时对 wss:// 传入自签名 ssl。

    覆盖 run()：逐行照抄父类，唯一区别是 ws_connect 多了 ssl= 参数。"""

    async def run(self) -> None:
        ssl_ctx = _insecure_ssl_for_wss(self.url)
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession() as client:
                    async with client.ws_connect(
                        self.url, heartbeat=30, max_msg_size=0, ssl=ssl_ctx
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


class PhaseBEsp32Runtime(PhaseBRokidRuntime):
    """ESP32 版 runtime：I/O 从 'PC 起 server 被动收' 换成 'PC 主动拉 ESP32'。

    __init__ / close / health / _initial_session_loop / _stats_loop 全部继承。
    仅覆盖 start()：把 Rokid 的 web server 换成 esp32_audio_reader + esp32_image_loop。
    """

    def __init__(self, config, esp32_host, esp32_port, image_tcp_port,
                 image_interval_s, image_timeout_s, probe=None,
                 funnel=None, recorder=None, force_measure=False,
                 gateway_host="127.0.0.1", gateway_port=8040,
                 reject_wav_dir="assets/reject_wav",
                 live_record_dir=None, rerun_from=None,
                 funnel_rerun_from=None, web_ui_port=None,
                 record_no_media: bool = False,
                 rerun_tail_wait_s: float = 30.0,
                 no_reject: bool = False, **kwargs):
        super().__init__(config, **kwargs)
        # 用支持自签名 wss 的 harness client 替换父类建好的普通 HarnessClient，
        # 并同步更新 manager.harness 引用（发遥测/镜像走同一个）。
        self.harness = TlsHarnessClient(
            config.harness_url,
            config.harness_client_id,
            self.manager,
            config.reconnect_s,
        )
        self.manager.harness = self.harness
        self._esp32_host = esp32_host
        self._esp32_port = esp32_port
        self._image_tcp_port = image_tcp_port
        self._image_interval_s = image_interval_s
        self._image_timeout_s = image_timeout_s
        self._tcp_img = TcpImageClient(esp32_host, image_tcp_port)
        self._stop_evt = asyncio.Event()
        self._probe = probe
        self._funnel = funnel
        self._recorder = recorder
        self._force_measure = force_measure
        self._reject_wav_dir = reject_wav_dir
        self._gateway_host = gateway_host
        self._gateway_port = gateway_port
        # 念提示走 gateway wss /ws/chat，自签名证书 → 复用同款 insecure ssl
        self._chat_ssl = _insecure_ssl_for_wss(f"wss://{gateway_host}:{gateway_port}/ws/chat")

        self._rerun_from = rerun_from
        self._funnel_rerun_from = funnel_rerun_from
        self._rerun_tail_wait_s = rerun_tail_wait_s
        self._no_reject = no_reject
        self._replay_t0 = None      # rerun 统一回放零点（就绪时设）
        self._rec_client = None
        # 回放两条流各自的完成标志：图轮数常少于音频秒数，谁先完都不能掐死对方
        self._audio_done = asyncio.Event()
        self._image_done = asyncio.Event()
        # bridge_ui 第一视角（8080）。live/rerun/funnel-rerun 都可推 img_b64。
        self._web_ui = None
        if web_ui_port and WebUIServer is not None:
            try:
                self._web_ui = WebUIServer(
                    port=web_ui_port,
                    sessions_root=Path(live_record_dir).parent if live_record_dir
                    else Path("live_sessions"),
                    mode_info={"mode": "rerun" if (rerun_from or funnel_rerun_from)
                               else "live"},
                )
            except Exception as e:
                LOG.warning("[UI] WebUIServer 创建失败: %s", e)
                self._web_ui = None
        # -o record（音+图，只存真发送的 best）：和漏斗 record 并存。
        self.live_rec = (LiveRecorder(live_record_dir, no_media=record_no_media)
                         if live_record_dir else None)
        if self.live_rec is not None:
            try:
                self.live_rec.attach_to_player(self.speaker)  # 录 AI 音
            except Exception as e:
                LOG.warning("[LIVE] attach_to_player 失败: %s", e)

        # 句子边界追踪器：包装 manager.handle_result，每个模型 chunk 更新句子/语音进度，
        # 供 image_loop 的"标点保护"策略读取（reject 时保护当前段到下一个标点再复查）。
        self.sent_tracker = SentenceTracker()
        self._turn_printer = TurnPrinter()
        self._turn_start_ms = None      # 当前 turn 的起点（session 内相对 ms）
        _orig_handle_result = self.manager.handle_result

        async def _wrapped_handle_result(session, result):
            try:
                is_listen = bool(result.get("is_listen"))
                text = str(result.get("text") or "")
                end_of_turn = bool(result.get("end_of_turn"))
                ab64 = str(result.get("audio_data") or "")
                audio_ms = 0.0
                if ab64 and not is_listen:
                    n = len(base64.b64decode(ab64)) // 4  # float32
                    audio_ms = n * 1000.0 / 24000.0
                self.sent_tracker.update(text, audio_ms, is_listen)

                # ── 评分用：把 chunk 碎片聚合成整段 turn，落 transcript/subtitles ──
                if self.live_rec is not None:
                    # 先记原始逐 chunk（不依赖 end_of_turn，保证一定有东西可评分）
                    self.live_rec.log_model_chunk(text, is_listen, end_of_turn, audio_ms)
                    prev_idx = self._turn_printer.turn_idx
                    turn_idx_cur, full_text, turn_listen = self._turn_printer.feed(
                        is_listen, end_of_turn, text)
                    if text and self._turn_printer.turn_idx > prev_idx:
                        self._turn_start_ms = self.live_rec.session_ms()
                    # 只要拿到完整段就落盘（end_of_turn 或 listen/speak 切换冲出的）
                    if full_text:
                        end_ms = self.live_rec.session_ms() + 800   # 多挂 0.8s
                        start_ms = (self._turn_start_ms
                                    if self._turn_start_ms is not None
                                    else max(end_ms - 3000, 0))
                        self.live_rec.log_turn_text(turn_idx_cur, full_text, turn_listen)
                        self.live_rec.log_subtitle(
                            start_ms=start_ms, end_ms=end_ms, text=full_text,
                            is_listen=turn_listen, turn_idx=turn_idx_cur)
                        self._turn_start_ms = None

                # 推模型文字到 bridge_ui 第一视角（type=result）
                if self._web_ui is not None and getattr(self._web_ui, "live_clients", None):
                    asyncio.create_task(self._web_ui.emit({
                        "type": "result",
                        "is_listen": is_listen,
                        "end_of_turn": end_of_turn,
                        "text": text,
                    }))
            except Exception:
                pass
            return await _orig_handle_result(session, result)

        self.manager.handle_result = _wrapped_handle_result

    async def _gate_open_when_ready(self, ready_evt: asyncio.Event) -> None:
        """等 gateway 就绪后放行回放任务。"""
        try:
            await self._wait_gateway_ready()
        finally:
            # 录制的 t0 必须和回放起点一致，否则 frames.jsonl/subtitles 的时间轴
            # 会把排队那几十秒也算进去，两个 arm 无法对齐。
            if self.live_rec is not None:
                self.live_rec.start()
                LOG.info("[LIVE] rerun 录制已开（就绪后启动，结束自动出 mp4）")
            # 统一回放零点：音频按 40ms 绝对时钟推、图按 frames.jsonl 的 t 等待，
            # 两者必须用同一个 t0，否则各自以"自己被调度到的那一刻"为零点，
            # 起跑差多少全看事件循环，音图就对不齐。
            self._replay_t0 = time.monotonic()
            _rc = getattr(self, "_rec_client", None)
            if _rc is not None:
                _rc.replay_t0 = self._replay_t0
            ready_evt.set()

    async def _wait_gateway_ready(self, timeout_s: float = 180.0) -> bool:
        """等 duplex session 真正 prepared 之后再开始回放。

        实测：连着跑两次 rerun 时，后一次会在 gateway 排队（[GW] queue position=1）
        长达 17 秒才 prepared。而回放任务从第 0 秒就推音频和图 —— 这 17 秒的输入
        全部推给一个还不存在的 session，直接丢掉，提问就在里面，模型自然没反应。
        更要命的是两个 arm 被吞掉的长度不一样，配对设计直接失效，而且事后从结果上
        看不出来（长得就像"模型没回答"）。所以回放前必须等就绪。
        """
        t0 = time.monotonic()
        warned = False
        while time.monotonic() - t0 < timeout_s:
            if self._stop_evt.is_set():
                return False
            try:
                status = str(self.manager.health().get("gateway_status") or "")
            except Exception:
                status = ""
            if status == "running":
                waited = time.monotonic() - t0
                if waited > 1.0:
                    LOG.info("[RERUN] gateway 就绪（等了 %.1fs），开始回放", waited)
                return True
            if not warned and time.monotonic() - t0 > 3.0:
                warned = True
                LOG.info("[RERUN] 等 gateway session 就绪…（status=%s）", status or "?")
            await asyncio.sleep(0.25)
        LOG.warning("[RERUN] 等 gateway 就绪超时 %.0fs，仍开始回放（本次结果可能不可用）",
                    timeout_s)
        return False

    async def start(self) -> None:
        await self.speaker.start()
        _install_asr_tap(self.harness, self.live_rec)
        if self._web_ui is not None:
            try:
                await self._web_ui.start()
                LOG.info("[UI] bridge_ui 第一视角已启动: http://localhost:%d",
                         self._web_ui.port)
            except Exception as e:
                LOG.warning("[UI] bridge_ui 启动失败: %s", e)
                self._web_ui = None
        # 方向分类器预热（原设计，-o 迁移时漏了）：首次 process_orientation 含模型加载+
        #   oneDNN 编译（~2s，甚至 5s+）。不预热的话它会推迟到第一次 accept 才现场加载，
        #   卡住那一轮，且在此之前链路出不了 send（模型长时间拿不到图）。
        if self._funnel is not None:
            try:
                ms = await asyncio.to_thread(self._funnel.warmup_orient)
                LOG.info("[预热] 方向分类器就绪 (首次 %.0fms，运行时应降到 ~10ms)", ms)
            except Exception as e:
                LOG.warning("[预热] 方向分类器预热跳过: %s", e)
        # ── rerun 模式：用录制的 session 回放（音频+best图）重跑 -o，不接实时设备 ──
        if self._rerun_from:
            LOG.info("[RERUN] 模式启动，回放 session: %s", self._rerun_from)
            _ready = asyncio.Event()
            asyncio.create_task(self._gate_open_when_ready(_ready))
            self._tasks = [
                asyncio.create_task(self.harness.run()),
                asyncio.create_task(self._initial_session_loop()),
                asyncio.create_task(self._stats_loop()),
                asyncio.create_task(rerun_audio_reader(
                    self._rerun_from,
                    self.manager, self.harness,
                    self.audio_queue, self.audio_mirror, self.stats,
                    self.config.input_gain, self._stop_evt,
                    live_rec=self.live_rec, ready_evt=_ready,
                    replay_t0=lambda: self._replay_t0,
                    done_evt=self._audio_done,
                )),
                asyncio.create_task(rerun_image_loop(
                    self._rerun_from, self.latest_frame, self.harness,
                    self.stats, self._image_interval_s, self._stop_evt,
                    web_ui=self._web_ui,
                    live_rec=self.live_rec, ready_evt=_ready,
                    replay_t0=lambda: self._replay_t0,
                    done_evt=self._image_done,
                    peer_done=self._audio_done,
                    speaker=self.speaker, sent_tracker=self.sent_tracker,
                    tail_wait_s=self._rerun_tail_wait_s,
                )),
            ]
            return
        # ── funnel rerun：录制的整簇多帧重跑漏斗；不加 --funnel 则为无漏斗对照组 ──
        if self._funnel_rerun_from:
            no_funnel = (self._funnel is None)
            _ready = asyncio.Event()
            asyncio.create_task(self._gate_open_when_ready(_ready))
            LOG.info("[RERUN] %s 模式启动: %s",
                     "无漏斗对照组" if no_funnel else "funnel 重跑漏斗+播报",
                     self._funnel_rerun_from)
            n_frames = getattr(self._funnel, "n_frames", 3) if self._funnel else 3
            rec_client = RecordedImageClient(self._funnel_rerun_from, n_frames,
                                             no_funnel=no_funnel)
            self._rec_client = rec_client   # 就绪时注入统一零点
            self._tasks = [
                asyncio.create_task(self.harness.run()),
                asyncio.create_task(self._initial_session_loop()),
                asyncio.create_task(self._stats_loop()),
                asyncio.create_task(rerun_audio_reader(
                    self._funnel_rerun_from,
                    self.manager, self.harness,
                    self.audio_queue, self.audio_mirror, self.stats,
                    self.config.input_gain, self._stop_evt,
                    live_rec=self.live_rec, ready_evt=_ready,
                    replay_t0=lambda: self._replay_t0,
                    done_evt=self._audio_done,
                )),
                # 复用完整 esp32_image_loop（run_once 重判 + reject 停播恢复 + 标点保护），
                # 只把图源从 TCP 换成 RecordedImageClient（读录制整簇多帧）。
                asyncio.create_task(esp32_image_loop(
                    rec_client, self.latest_frame, self.harness, self.stats,
                    self._image_interval_s, self._image_timeout_s, self._stop_evt,
                    probe=self._probe, funnel=self._funnel, recorder=None,
                    speaker=self.speaker,
                    gateway_host=self._gateway_host, gateway_port=self._gateway_port,
                    ssl_ctx=self._chat_ssl, force_measure=self._force_measure,
                    manager=self.manager,
                    reject_wav_dir=self._reject_wav_dir,
                    sent_tracker=self.sent_tracker,
                    live_rec=self.live_rec,  # 开 --record-live 则录 rerun 输出→自动出 mp4
                    web_ui=self._web_ui, ready_evt=_ready,
                    tail_wait_s=self._rerun_tail_wait_s,
                    no_reject=self._no_reject,
                    peer_done=self._audio_done, done_evt=self._image_done,
                )),
            ]
            return
        # 设 ESP32 分辨率为 HD(1280×720)。固件默认 SVGA(800×600)，但漏斗阈值是按 HD
        # 标定的（cam_pipeline_v2 注释：分辨率固定 HD 后针对 HD 标定）。-o 迁移时漏了这步，
        # 导致一直跑 SVGA、画质与阈值不匹配。这里启动时补上。
        if self._funnel is not None:
            ok = await asyncio.to_thread(self._funnel.set_resolution_hd)
            LOG.info("[ESP32] 设分辨率 1280×720(UXGA档): %s",
                     "成功" if ok else "失败(检查ESP32 /control)")
            await asyncio.sleep(0.3)  # 切分辨率后固件重配，稍等
        if self.live_rec is not None:
            self.live_rec.start()
            LOG.info("[LIVE] -o record started")
        self._tasks = [
            # ── 继承自 rokid 的三个骨架 task ──
            asyncio.create_task(self.harness.run()),
            asyncio.create_task(self._initial_session_loop()),
            asyncio.create_task(self._stats_loop()),
            # ── ESP32 特有：主动拉取音视频 ──
            asyncio.create_task(esp32_audio_reader(
                self._esp32_host, self._esp32_port,
                self.manager, self.harness,
                self.audio_queue, self.audio_mirror, self.stats,
                self.config.input_gain, self._stop_evt,
                probe=self._probe,
                live_rec=self.live_rec,
            )),
            asyncio.create_task(esp32_image_loop(
                self._tcp_img, self.latest_frame, self.harness, self.stats,
                self._image_interval_s, self._image_timeout_s, self._stop_evt,
                probe=self._probe, funnel=self._funnel, recorder=self._recorder,
                speaker=self.speaker,
                gateway_host=self._gateway_host, gateway_port=self._gateway_port,
                ssl_ctx=self._chat_ssl, force_measure=self._force_measure,
                manager=self.manager,
                reject_wav_dir=self._reject_wav_dir,
                sent_tracker=self.sent_tracker,
                live_rec=self.live_rec,
                web_ui=self._web_ui,
                no_reject=self._no_reject,
            )),
        ]

    async def close(self) -> None:
        self._stop_evt.set()
        if self._web_ui is not None:
            try:
                await self._web_ui.stop()
            except Exception:
                pass
        if self.live_rec is not None:
            try:
                # 冲掉还没等到 end_of_turn 的那段（bare 组常见：模型一路流式说，
                # 没有 stop/resume 打断，end_of_turn 迟迟不来 → 整段丢失）
                tp = getattr(self, "_turn_printer", None)
                if tp is not None and getattr(tp, "_in_turn", False):
                    pending = "".join(getattr(tp, "_turn_text", []))
                    if pending:
                        end_ms = self.live_rec.session_ms()
                        start_ms = (self._turn_start_ms
                                    if self._turn_start_ms is not None
                                    else max(end_ms - 3000, 0))
                        self.live_rec.log_turn_text(tp.turn_idx, pending,
                                                    bool(tp._turn_is_listen))
                        self.live_rec.log_subtitle(
                            start_ms=start_ms, end_ms=end_ms, text=pending,
                            is_listen=bool(tp._turn_is_listen), turn_idx=tp.turn_idx)
                        LOG.info("[LIVE] 冲出未收尾的 turn #%d (%d字)",
                                 tp.turn_idx, len(pending))
            except Exception as e:
                LOG.warning("[LIVE] flush pending turn err: %s", e)
            try:
                self.live_rec.stop()
                LOG.info("[LIVE] -o record stopped")
            except Exception as e:
                LOG.warning("[LIVE] stop err: %s", e)
        await self._tcp_img._close()
        if self._probe is not None:
            self._probe.close()
        if self._recorder is not None:
            self._recorder.close()
        await super().close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ESP32 Phase B Harness runtime")
    # ── ESP32 设备 ──
    p.add_argument("--esp32-host", required=True, help="ESP32 IP address")
    p.add_argument("--esp32-port", type=int, default=80, help="ESP32 HTTP/WS port")
    p.add_argument("--image-tcp-port", type=int, default=5000, help="ESP32 TCP image port")
    p.add_argument("--image-interval-s", type=float, default=1.0, help="取图间隔(秒)")
    p.add_argument("--image-timeout-s", type=float, default=1.0, help="单次取图超时(秒)")
    # ── gateway / harness（与 rokid 一致）──
    p.add_argument("--gateway", default="localhost:8040")
    p.add_argument("--gateway-tls", action="store_true", default=True)
    p.add_argument("--no-tls", dest="gateway_tls", action="store_false")
    p.add_argument("--harness-url", default="ws://127.0.0.1:8021/ws/control")
    p.add_argument("--client-id", default="esp32-phase-b")
    p.add_argument("--skills-config", default=default_skills_config())
    p.add_argument("--cleanup-mode", choices=("light", "full"), default="light")
    p.add_argument("--chunk-ms", type=int, default=1_000)
    p.add_argument("--force-listen-count", type=int, default=3)
    p.add_argument("--audio-queue-packets", type=int, default=96)
    p.add_argument("--input-gain", type=float, default=12.0)
    p.add_argument("--prompt", default=None, help="system prompt（可选）")
    p.add_argument("--no-play", action="store_true")
    p.add_argument("--log-level", default="INFO")
    # ── 时序探针（纯旁路）──
    p.add_argument("--timing-probe", action="store_true", help="开启时序探针，写 CSV")
    p.add_argument("--timing-csv", default=None, help="探针 CSV 路径（默认自动带时间戳）")
    # ── CV 漏斗 ──
    p.add_argument("--funnel", action="store_true", help="开启 CV 漏斗（抓N帧判定，合格才送模型）")
    p.add_argument("--scene", default="medicine", choices=("medicine", "stationery"),
                   help="漏斗场景参数")
    p.add_argument("--funnel-frames", type=int, default=3, help="每轮抓帧数（3选2）")
    p.add_argument("--no-focus", dest="funnel_focus", action="store_false", default=True,
                   help="关闭漏斗内的自动对焦触发")
    # ── session 录制（对齐 -v 格式）──
    p.add_argument("--record", action="store_true", help="录 session（整簇帧+判定到 sessions/）")
    p.add_argument("--sessions-root", default="sessions", help="session 根目录")
    p.add_argument("--no-reject", action="store_true",
                   help="消融：漏斗只选 best、永远放行，不拒绝不播报"
                        "（用于把工作②选图的收益与工作③拒绝分开）")
    p.add_argument("--rerun-tail-wait-s", type=float, default=30.0,
                   help="rerun 图放完后，最多再等模型说完的秒数（默认30）")
    p.add_argument("--record-no-media", action="store_true",
                   help="批量评分用：只写 wav+jsonl+transcript，跳过 jpg 落盘和 mp4 拼接")
    p.add_argument("--record-live", action="store_true",
                   help="-o record：录音+图(只存真发送的best)到 live_sessions/，供 -o rerun")
    p.add_argument("--rerun-from", default=None,
                   help="-o rerun：从 live_sessions/<sid> 回放(音+best图)重跑 -o 模型")
    p.add_argument("--funnel-rerun-from", default=None,
                   help="funnel rerun：从 live_sessions/<sid> 回放(音+整簇多帧)重跑漏斗+播报+-o")
    p.add_argument("--web-ui-port", type=int, default=None,
                   help="开 bridge_ui 第一视角(如 8080)，live/rerun/funnel-rerun 都可看")
    # ── 强制措施（防幻觉）：reject 时用模型音色念提示 + 停/恢复走 8021 ──
    p.add_argument("--force-measure", action="store_true",
                   help="开启强制措施：reject→停duplex→播预生成wav→恢复duplex")
    p.add_argument("--reject-wav-dir", default="assets/reject_wav",
                   help="预生成的 reject 提示 wav 目录（gen_reject_wavs.py 生成）")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    config = RokidRuntimeConfig(
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
        play_audio=not args.no_play,
    )
    probe = TimingProbe(
        enabled=args.timing_probe,
        path=args.timing_csv,
        chunk_ms=args.chunk_ms,
    )
    funnel = None
    if args.funnel:
        # rerun 时强制关对焦：rerun 没有真实相机，trigger_af 对录制的图无意义，
        # 且对焦重抓会让 run_once 多抓一簇 → RecordedImageClient 多消耗一个 round
        # → 图提前用光（有漏斗比无漏斗先用光的根因）。
        _is_rerun = bool(args.rerun_from or args.funnel_rerun_from)
        _focus = args.funnel_focus and not _is_rerun
        funnel = FunnelGate(
            args.esp32_host,
            scene=args.scene,
            n_frames=args.funnel_frames,
            enable_focus=_focus,
        )
        LOG.info("CV 漏斗已开启: scene=%s frames=%d focus=%s%s",
                 args.scene, args.funnel_frames, _focus,
                 "（rerun 强制关对焦）" if (_is_rerun and args.funnel_focus) else "")
    recorder = None   # 旧 SessionRecorder 已废弃，统一走 LiveRecorder
    # 统一 record（音+整簇多帧+best标记）：--record 或 --record-live 都触发。
    live_record_dir = None
    if args.record or args.record_live or args.record_no_media:
        import time as _t
        live_record_dir = os.path.join("live_sessions", _t.strftime("%Y%m%d_%H%M%S"))
        # 日志同时落进 session 目录：评分时要按时间轴对齐 reject/播报/模型输出，
        # 控制台日志是滚动的、和 session 分离，跑多组 rerun 极易对错。
        try:
            os.makedirs(live_record_dir, exist_ok=True)
            _fh = logging.FileHandler(
                os.path.join(live_record_dir, "run.log"), encoding="utf-8")
            _fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s"))
            logging.getLogger().addHandler(_fh)
            LOG.info("[LIVE] 日志同时写入 %s", os.path.join(live_record_dir, "run.log"))
        except Exception as e:
            LOG.warning("[LIVE] run.log 创建失败: %s", e)
        os.makedirs(live_record_dir, exist_ok=True)
        LOG.info("统一 record 已开启: %s（音+整簇多帧+best标记，供三种 rerun）", live_record_dir)
    # 解析 gateway "host:port"（念提示的 chat TTS 走同一个 gateway 的 wss）
    _gw = args.gateway.split("://")[-1]
    _gw_host, _, _gw_port = _gw.partition(":")
    _gw_host = _gw_host or "127.0.0.1"
    _gw_port = int(_gw_port or "8040")
    runtime = PhaseBEsp32Runtime(
        config,
        esp32_host=args.esp32_host,
        esp32_port=args.esp32_port,
        image_tcp_port=args.image_tcp_port,
        image_interval_s=args.image_interval_s,
        image_timeout_s=args.image_timeout_s,
        probe=probe,
        funnel=funnel,
        recorder=recorder,
        force_measure=args.force_measure,
        reject_wav_dir=args.reject_wav_dir,
        gateway_host=_gw_host,
        gateway_port=_gw_port,
        live_record_dir=live_record_dir,
        rerun_from=args.rerun_from,
        funnel_rerun_from=args.funnel_rerun_from,
        web_ui_port=args.web_ui_port,
        record_no_media=args.record_no_media,
        rerun_tail_wait_s=args.rerun_tail_wait_s,
        no_reject=args.no_reject,
    )
    if args.force_measure:
        LOG.info("强制措施已开启: reject→funnel.stop+念提示, good→funnel.resume "
                 "(节流5s, chat TTS via wss://%s:%d)", _gw_host, _gw_port)

    LOG.info("ESP32 Phase B input: ws://%s:%d/ws_audio_v2 + TCP:%d",
             args.esp32_host, args.esp32_port, args.image_tcp_port)
    LOG.info("Harness: %s", config.harness_url)
    LOG.info("Gateway: %s://%s", "wss" if config.gateway_tls else "ws", config.gateway)

    async def _run() -> None:
        await runtime.start()
        stop = asyncio.Event()

        def _sig(*_a):
            stop.set()
        try:
            loop = asyncio.get_running_loop()
            for s in (signal.SIGINT, getattr(signal, "SIGBREAK", signal.SIGINT)):
                try:
                    loop.add_signal_handler(s, stop.set)
                except (NotImplementedError, ValueError):
                    signal.signal(s, _sig)
        except Exception:
            signal.signal(signal.SIGINT, _sig)
        # 等"信号(Ctrl+C)"或"runtime 内部 stop_evt(rerun 图放完自动结束)"任一触发
        _internal = getattr(runtime, "_stop_evt", None)
        waiters = [asyncio.create_task(stop.wait())]
        if _internal is not None:
            waiters.append(asyncio.create_task(_internal.wait()))
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for w in waiters:
            w.cancel()
        await runtime.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        LOG.info("Interrupted; shutting down")


if __name__ == "__main__":
    main()
