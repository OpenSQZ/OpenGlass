"""rerun_source.py — 用已录制 session 当输入重跑模型

替换主脚本里的两个网络输入函数：
  - esp32_audio_reader  →  local_pcm_reader
  - esp32_capture_image →  LocalImageSource.capture
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd
import threading


LOGGER = logging.getLogger("rerun_source")

_PACKET_MS = 40
_PACKET_SAMPLES = 640
_SAMPLE_RATE = 16000


class _UserMonitor:
    """独立 PortAudio OutputStream,用于 rerun 时播放 user 监听音频。

    为什么不复用主 SpeakerPlayer:主 speaker 是单 FIFO ring,
    AI 和 user 两路 enqueue 共享同一个 ring 时,DAC 按入队顺序消费会
    导致 AI 音频被 user 音频"插队"——听感是 AI 输出卡顿。

    独立 OutputStream 由 OS 端混音(同一声卡时钟驱动),物理上不互相挤压。
    """

    def __init__(self, sample_rate: int = 24000, ring_capacity_s: float = 2.0):
        self.sr = int(sample_rate)
        self.ring_cap = int(ring_capacity_s * self.sr)
        self._buf = np.zeros(self.ring_cap, dtype=np.float32)
        self._r = 0
        self._w = 0
        self._size = 0
        self._lock = threading.Lock()
        self._dropped = 0
        try:
            self._stream = sd.OutputStream(
                samplerate=self.sr, channels=1, dtype="float32",
                blocksize=0, callback=self._cb,
            )
            self._stream.start()
            LOGGER.info("[RERUN-MON] user monitor stream started: sr=%d  ring=%.1fs",
                        self.sr, ring_capacity_s)
        except Exception as e:
            LOGGER.warning("[RERUN-MON] failed to open OutputStream: %r", e)
            self._stream = None

    def _cb(self, outdata, frames, time_info, status):
        with self._lock:
            avail = min(frames, self._size)
            if avail > 0:
                first = min(avail, self.ring_cap - self._r)
                outdata[:first, 0] = self._buf[self._r:self._r + first]
                if avail > first:
                    outdata[first:avail, 0] = self._buf[:avail - first]
                self._r = (self._r + avail) % self.ring_cap
                self._size -= avail
            if avail < frames:
                outdata[avail:, 0] = 0.0

    def enqueue(self, pcm_f32: np.ndarray) -> None:
        if self._stream is None or pcm_f32 is None or pcm_f32.size == 0:
            return
        if pcm_f32.dtype != np.float32:
            pcm_f32 = pcm_f32.astype(np.float32)
        n = pcm_f32.size
        if n > self.ring_cap:
            pcm_f32 = pcm_f32[-self.ring_cap:]
            n = pcm_f32.size
        with self._lock:
            if self._size + n > self.ring_cap:
                drop = (self._size + n) - self.ring_cap
                self._r = (self._r + drop) % self.ring_cap
                self._size -= drop
                self._dropped += drop
            first = min(n, self.ring_cap - self._w)
            self._buf[self._w:self._w + first] = pcm_f32[:first]
            if n > first:
                self._buf[:n - first] = pcm_f32[first:n]
            self._w = (self._w + n) % self.ring_cap
            self._size += n

    def stop(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as e:
            LOGGER.warning("[RERUN-MON] stop err: %r", e)
        finally:
            self._stream = None
        LOGGER.info("[RERUN-MON] user monitor stopped (dropped=%d samples)",
                    self._dropped)



async def local_pcm_reader(
    pcm_path: Path,
    ring,
    stop_evt: asyncio.Event,
    stats,
    live_rec=None,
    speaker=None,            # ← 新增
    speaker_sr: int = 24000, # ← 新增:speaker 期望的采样率
    speed: float = 1.0,
    drain_s: float = 5.0,
    prefill_s: float = 0.0,
) -> None:
    pcm_path = Path(pcm_path)
    if not pcm_path.exists():
        LOGGER.error("[RERUN] pcm not found: %s", pcm_path)
        stop_evt.set()
        return

    # 优先用 live_user.wav (LiveRecorder 直接写的,干净);
    # 回退用 user_raw.pcm (主循环 ring.slice 落盘的,带 40% 零填充)
    import wave
    wav_path = pcm_path.parent / "live_user.wav"
    pcm_i16 = None
    src_label = ""
    if wav_path.exists():
        try:
            with wave.open(str(wav_path), "rb") as w:
                sr_in = w.getframerate()
                nch = w.getnchannels()
                sw = w.getsampwidth()
                if sr_in == _SAMPLE_RATE and nch == 1 and sw == 2:
                    pcm_i16 = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
                    src_label = f"live_user.wav (clean, {sr_in}Hz mono int16)"
                else:
                    LOGGER.warning(
                        "[RERUN] live_user.wav format unexpected (sr=%d ch=%d sw=%d), "
                        "fallback to user_raw.pcm", sr_in, nch, sw,
                    )
        except Exception as e:
            LOGGER.warning("[RERUN] failed to read live_user.wav: %r, fallback to user_raw.pcm", e)
            pcm_i16 = None

    if pcm_i16 is None:
        raw = pcm_path.read_bytes()
        pcm_i16 = np.frombuffer(raw, dtype=np.int16)
        src_label = "user_raw.pcm (may contain ring.slice zero-padding)"

    total_samples = pcm_i16.size
    total_dur = total_samples / _SAMPLE_RATE
    LOGGER.info("[RERUN] audio source: %s", src_label)
    # rerun 用户监听:独立 OutputStream,避免和 AI 共用 ring 导致互相卡顿
    user_mon = None
    if speaker is not None:
        try:
            user_mon = _UserMonitor(sample_rate=speaker_sr)
        except Exception as e:
            LOGGER.warning("[RERUN] failed to create user monitor: %r", e)
            user_mon = None


    LOGGER.info(
        "[RERUN] pcm loaded: %s  samples=%d  dur=%.2fs  speed=%.2fx  prefill=%.1fs",
        pcm_path, total_samples, total_dur, speed, prefill_s,
    )

    packet_interval = (_PACKET_MS / 1000.0) / max(speed, 0.01)
    ts_ms = 0
    cursor = 0
    pkt_count = 0
    prefill_packets = int(prefill_s * 1000 / _PACKET_MS)
    next_tick = time.monotonic()  # ← 改:不再 + prefill_s
    audio_t_start = None
    while cursor < total_samples and not stop_evt.is_set():
        end = min(cursor + _PACKET_SAMPLES, total_samples)
        chunk_i16 = pcm_i16[cursor:end]
        cursor = end
        pkt_count += 1

        try:
            stats["rx_pkts"] = pkt_count
            stats["esp_drops"] = 0
        except Exception:
            pass

        try:
            await ring.write(ts_ms, chunk_i16)
        except Exception as e:
            LOGGER.warning("[RERUN] ring.write err: %r", e)

        if live_rec is not None:
            try:
                # ↓ 关键改动:用"累计音频时长"作为 t 戳,而不是 wall clock
                #   这样 LiveRecorder 写盘时 t * sr 算出来的 i 与 pcm.size 完美对齐
                if audio_t_start is None:
                    audio_t_start = time.monotonic() - live_rec._t0 if live_rec._t0 else 0.0
                audio_t = audio_t_start + ts_ms / 1000.0
                live_rec.feed_user_raw(
                    chunk_i16.astype(np.float32) / 32768.0,
                    t_override=audio_t,
                )
                # rerun 模式:把 user 音频也推给扬声器,这样能听到原录制的用户在说什么
                # (live 模式不这么做,因为用户本人就在那说话,会和扬声器叠加。
                #  rerun 是事后回放,没回声风险,等同 8006 浏览器播 mp4 的体验)
                if user_mon  is not None:
                    try:
                        chunk_f32 = chunk_i16.astype(np.float32) / 32768.0
                        if speaker_sr != _SAMPLE_RATE:
                            ratio = speaker_sr / _SAMPLE_RATE
                            new_len = int(chunk_f32.size * ratio)
                            x_old = np.linspace(0, 1, chunk_f32.size, endpoint=False)
                            x_new = np.linspace(0, 1, new_len, endpoint=False)
                            chunk_resampled = np.interp(x_new, x_old, chunk_f32).astype(np.float32)
                        else:
                            chunk_resampled = chunk_f32
                        # 关键:走原始 enqueue,不走 LiveRecorder hook
                        # (hook 会把这条用户音频错误地累积进 _ai_chunks,
                        #  导致 mp4 的 ai 轨里也有用户声音,听起来像"用户被录了两次")
                        user_mon.enqueue(chunk_resampled)
                    except Exception as e:
                        LOGGER.warning("[RERUN] speaker.enqueue err: %r", e)

                #live_rec.feed_user_raw(chunk_i16.astype(np.float32) / 32768.0)
            except Exception as e:
                LOGGER.warning("[RERUN] live_rec.feed_user_raw err: %r", e)

        ts_ms += _PACKET_MS

        # prefill 阶段:让出事件循环但不真睡
        if pkt_count <= prefill_packets:
            await asyncio.sleep(0)
            continue

        # prefill → realtime 切换那一刻,重置基线
        if pkt_count == prefill_packets + 1:
            LOGGER.info(
                "[RERUN] prefill done (%d packets, %.2fs audio in ring), "
                "switching to realtime pacing",
                prefill_packets, prefill_packets * _PACKET_MS / 1000.0,
            )
            next_tick = time.monotonic()  # ← 关键:基线重置到此刻

        next_tick += packet_interval
        sleep_for = next_tick - time.monotonic()
        if sleep_for > 0:
            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=sleep_for)
                break  # 被外部 stop:跳出推流循环,仍走下面的 drain(等模型说完)
            except asyncio.TimeoutError:
                pass

    try:
        stats["audio_done"] = True
        stats["audio_done_ts"] = time.monotonic()
    except Exception:
        pass

    LOGGER.info(
        "[RERUN] pcm exhausted: pushed=%d packets (%.2fs audio). draining...",
        pkt_count, ts_ms / 1000.0,
    )

    # 音频已推完并标记 audio_done。结束的裁决权交给主 send loop:
    # 它会在 audio_done 后等模型说完(轮询 last_speak_ts)再 set stop_evt。
    # reader 这里只需等待那个信号;带兜底上限防止异常卡死。
    max_wait_s = 120.0
    waited = 0.0
    while waited < max_wait_s and not stop_evt.is_set():
        try:
            await asyncio.wait_for(stop_evt.wait(), timeout=0.2)
            break
        except asyncio.TimeoutError:
            waited += 0.2
    if waited >= max_wait_s:
        LOGGER.warning("[RERUN] drain 达到上限 %.0fs,强制结束", max_wait_s)

    if user_mon is not None:
        user_mon.stop()

    if not stop_evt.is_set():
        LOGGER.info("[RERUN] drain done, signaling stop")
        stop_evt.set()


class LocalImageSource:
    def __init__(self, session_dir: Path):
        self.session_dir = Path(session_dir)
        self.images_dir = self.session_dir / "images"
        self.events_path = self.session_dir / "events.jsonl"

        if not self.images_dir.exists():
            raise FileNotFoundError(f"images/ not found: {self.images_dir}")
        if not self.events_path.exists():
            raise FileNotFoundError(f"events.jsonl not found: {self.events_path}")

        self._map: dict[int, Optional[str]] = {}
        self._build_map()

        n_total = len(self._map)
        n_with_img = sum(1 for v in self._map.values() if v)
        LOGGER.info(
            "[RERUN] image map: total_chunks=%d  with_image=%d  (missing=%d) from %s",
            n_total, n_with_img, n_total - n_with_img, self.events_path.name,
        )
        if n_total == 0:
            # ← 新增:诊断空 events.jsonl
            n_files = len(list(self.images_dir.glob("img_*.jpg")))
            LOGGER.warning(
                "[RERUN] events.jsonl has NO chunk_sent entries! "
                "but images/ has %d files. Falling back to filename-order matching.",
                n_files,
            )
            self._fallback_filename_order(n_files)

    def _build_map(self) -> None:
        with self.events_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except Exception:
                    continue
                # ← 改 1:看实际 events.jsonl 用什么 kind 字段
                kind = evt.get("kind") or evt.get("type") or ""
                if kind != "chunk_sent":
                    continue
                # ← 改 2:chunk_idx 字段名兼容
                idx = evt.get("idx", evt.get("chunk_idx"))
                img = evt.get("img")
                if idx is None:
                    continue
                self._map[int(idx)] = img if img else None

    def _fallback_filename_order(self, n_files: int) -> None:
        """events.jsonl 是空的时候的兜底:按 img_NNNNN.jpg 文件名映射 chunk_idx。"""
        for p in sorted(self.images_dir.glob("img_*.jpg")):
            try:
                # 文件名形如 img_00007.jpg → 7
                idx = int(p.stem.split("_")[-1])
                self._map[idx] = p.name
            except Exception:
                pass
        LOGGER.info("[RERUN] fallback map built: %d chunks (filename-order)",
                    len(self._map))

    async def capture(self, chunk_idx: int) -> Optional[bytes]:
        rel = self._map.get(int(chunk_idx))
        if not rel:
            return None
        name = Path(rel).name
        path = self.images_dir / name
        if not path.exists():
            return None
        try:
            return path.read_bytes()
        except Exception as e:
            LOGGER.warning("[RERUN] image read err: %r", e)
            return None