"""recorder_live.py — 录屏式实时录制器 v5.0
=================================================

v5.0 改动:user 轨从"ring buffer 切片拼接"改成"WS 原始包直录"。
和蓝牙耳机录通话语义一致:丢包 = 该段缺失,不再补零去对齐墙钟。
和 AI 轨完全对称——AI 轨录的是 PortAudio DAC 实际写出的样本。

调用顺序:
    live_rec = LiveRecorder(session_dir)
    live_rec.attach_to_player(speaker)   # 必须在 speaker.start() 之前
    speaker.start()
    live_rec.start()                     # 此后 user/ai/frame 才会被记录

    # ESP32 reader 每收一包就调:
    live_rec.feed_user_raw(pcm_f32)      # 16kHz 原始,丢包就丢

    # 每发一个 chunk 配的图:
    live_rec.on_frame(jpeg, chunk_idx)

    live_rec.stop()
    live_rec.finalize_mp4()
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np

LOGGER = logging.getLogger("recorder_live")


class LiveRecorder:
    def __init__(
        self,
        session_dir: Optional[Path],
        user_sr: int = 16000,
        ai_sr: int = 24000,
        no_media: bool = False,
    ):
        self.enabled = session_dir is not None
        self.dir: Optional[Path] = Path(session_dir) if session_dir else None
        self.user_sr = user_sr
        self.ai_sr = ai_sr
        # no_media：批量 rerun 评分只需要 wav + jsonl + transcript，
        #   跳过 jpg 落盘和 mp4 拼接（150 次 rerun 会重复复制整簇图、跑 450 次 ffmpeg）。
        self.no_media = bool(no_media)

        self._t0: Optional[float] = None

        # User 轨:ESP32 WS 来的原始 PCM,丢包 = 缺失,不补零
        self._user_t_start: Optional[float] = None
        # self._user_chunks: list[np.ndarray] = []
        self._user_chunks: list[tuple[float, np.ndarray]] = []
        # AI 轨:PortAudio DAC 实际输出(含 underrun 时填的零)
        self._ai_t_start: Optional[float] = None
        #self._ai_chunks: list[np.ndarray] = []
        self._ai_chunks: list[tuple[float, np.ndarray]] = []
        # Frames
        self._frames: list[tuple[float, str, int]] = []
        self._funnel_rounds: list[dict] = []   # 每轮整簇多帧+判定（统一record）
        self._round_counter: int = -1          # on_funnel_round 轮次计数（qid，每轮+1）
        self._transcript = None                # transcript.txt 句柄
        self._subtitles = None                 # subtitles.jsonl 句柄
        self._chunks_f = None                  # model_chunks.jsonl 句柄

        self._lock = threading.Lock()
        self._started = threading.Event()
        self._stopping = threading.Event()

        self._user_calls = 0
        self._ai_calls = 0

    # ------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------

    def attach_to_player(self, player) -> None:
        if not self.enabled:
            return
        orig_enqueue = player.enqueue
        rec = self

        # PCSpeaker.enqueue 是 async，签名 (pcm, generation)。包装必须同签名 + async，
        # 否则模型音频 enqueue 报 "takes 1 positional argument but 2 were given"，
        # 导致 AI 音播不出、也录不进（live 听不到声音）。
        async def wrapped_enqueue(pcm_f32, generation=0):
            await orig_enqueue(pcm_f32, generation)
            if (not rec._started.is_set() or rec._stopping.is_set()
                    or rec._t0 is None or pcm_f32 is None or pcm_f32.size == 0):
                return
            try:
                pcm = pcm_f32 if pcm_f32.dtype == np.float32 else pcm_f32.astype(np.float32)
                pcm = pcm.copy()
                t = max(0.0, time.monotonic() - rec._t0)
                with rec._lock:
                    if rec._ai_t_start is None:
                        rec._ai_t_start = t
                    rec._ai_chunks.append((t, pcm))
                    rec._ai_calls += 1
            except Exception as e:
                LOGGER.warning("[LIVE] enqueue hook err: %r", e)

        player.enqueue = wrapped_enqueue
        player._orig_enqueue = orig_enqueue  # 暴露原始方法，绕过 hook 的场景使用
        LOGGER.info("[LIVE] attached to SPK enqueue (async, pcm+generation)")

    def start(self) -> None:
        if not self.enabled or self._started.is_set():
            return
        assert self.dir is not None
        self.dir.mkdir(parents=True, exist_ok=True)
        #(self.dir / "live_images").mkdir(exist_ok=True)
        self._t0 = time.monotonic()
        self._started.set()
        # 评分用：模型/用户的成段文本 + 时间轴（移植自 demo_esp32_duplex_0703 的
        #   SessionRecorder.log_turn_text / log_subtitle）。
        #   transcript.txt   —— 人读，[AI #n] / [LISTEN #n] 整段
        #   subtitles.jsonl  —— 机读，{start_ms,end_ms,text,is_listen,turn_idx}
        #   is_listen=True 的 turn 就是 8021 ASR 识别出的用户说话 → 提问结束时刻 t0 从这拿
        try:
            self._transcript = (self.dir / "transcript.txt").open("w", encoding="utf-8")
            self._transcript.write(f"# session {self.dir.name}\n")
            self._transcript.flush()
            self._subtitles = (self.dir / "subtitles.jsonl").open("w", encoding="utf-8")
            self._chunks_f = (self.dir / "model_chunks.jsonl").open("w", encoding="utf-8")
        except Exception as e:
            LOGGER.warning("[LIVE] transcript/subtitles 打开失败: %s", e)
            self._transcript = None
            self._subtitles = None
            self._chunks_f = None
        LOGGER.info(
            "[LIVE] recording started: %s (user=%dHz ai=%dHz)",
            self.dir, self.user_sr, self.ai_sr,
        )

    def session_ms(self) -> int:
        """session 内相对毫秒（与 frames.jsonl 的 t 同一条时间轴）。"""
        if self._t0 is None:
            return 0
        return int((time.monotonic() - self._t0) * 1000.0)

    def log_model_chunk(self, text: str, is_listen: bool, end_of_turn: bool,
                        audio_ms: float = 0.0) -> None:
        """逐 chunk 原始记录 —— **一条都不过滤**。

        这是诊断用的原始流水：-o 是 1Hz 决策，listen 期间大量 chunk 是
        text="" 且 end_of_turn=False。之前这里写了
            if not text and not end_of_turn: return
        把它们全扔了，结果 24 秒的会话只剩 5 条，看上去像"模型 5.7 秒才出一个
        chunk / 输入分块坏了"——那是记录函数造出来的假象，不是事实。
        要判断模型节奏是否正常，必须看到每一个 chunk。
        """
        if not self.enabled or not self._started.is_set() or self.dir is None:
            return
        if self._chunks_f is None:
            return
        try:
            self._chunks_f.write(json.dumps({
                "session_ms": self.session_ms(),
                "text": text,
                "is_listen": bool(is_listen),
                "end_of_turn": bool(end_of_turn),
                "audio_ms": round(float(audio_ms), 1),
            }, ensure_ascii=False) + "\n")
            self._chunks_f.flush()
        except Exception:
            pass

    def log_event(self, tag: str, text: str) -> None:
        """把漏斗侧事件写进 transcript，和 USER/AI 同一条时间轴。

        评分时要判"这次拦截对应哪次回答"，三者必须在同一条人读时间轴上：
            [USER   @12480ms] 这上面写的是什么
            [REJECT @13520ms] severe_shake 画面晃得厉害，请先保持不动
            [HINT   @13900ms] severe_shake (2.52s)
            [AI #3  @16100ms] 上面写着阿莫西林胶囊...
        机读的一份仍在 frames.jsonl（每轮 send/reason）。
        """
        if not self.enabled or not self._started.is_set() or self._transcript is None:
            return
        try:
            self._transcript.write(f"[{tag} @{self.session_ms()}ms] {text}\n")
            self._transcript.flush()
        except Exception:
            pass

    def log_asr(self, utterance: str, final_at_ms=None, confidence=None) -> None:
        """8021 ASR 识别出的用户说话。评分取 t0 用。

        写两处：transcript.txt 的 [USER] 行（人读）、subtitles.jsonl 的
        is_listen=true 记录（机读，与模型 turn 同一条时间轴、同一个 session_ms 基准）。
        final_at_ms 是 8021 侧的时钟，和本 session 的 _t0 不同源，只作参考存进
        asr_events.jsonl；对齐一律用本地 session_ms()。
        """
        if not self.enabled or not self._started.is_set():
            return
        t_ms = self.session_ms()
        try:
            if self._transcript is not None:
                self._transcript.write(f"[USER @{t_ms}ms] {utterance}\n")
                self._transcript.flush()
        except Exception:
            pass
        # 复用 subtitles 的 turn 序列（负数 turn_idx 标识 ASR，避免和模型 turn 撞号）
        self._asr_seq = getattr(self, "_asr_seq", 0) + 1
        self.log_subtitle(start_ms=t_ms, end_ms=t_ms, text=utterance,
                          is_listen=True, turn_idx=-self._asr_seq)
        try:
            if self.dir is not None:
                with (self.dir / "asr_events.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "session_ms": t_ms,
                        "utterance": utterance,
                        "harness_final_at_ms": final_at_ms,
                        "confidence": confidence,
                    }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def log_turn_text(self, turn_idx: int, text: str, is_listen: bool) -> None:
        if not self.enabled or self._transcript is None:
            return
        tag = "LISTEN" if is_listen else "AI"
        try:
            self._transcript.write(f"[{tag} #{turn_idx} @{self.session_ms()}ms] {text}\n")
            self._transcript.flush()
        except Exception:
            pass

    def log_subtitle(self, start_ms: int, end_ms: int, text: str,
                     is_listen: bool, turn_idx: int) -> None:
        if not self.enabled or self._subtitles is None:
            return
        try:
            self._subtitles.write(json.dumps({
                "start_ms": int(start_ms),
                "end_ms": int(end_ms),
                "text": text,
                "is_listen": bool(is_listen),
                "turn_idx": int(turn_idx),
            }, ensure_ascii=False) + "\n")
            self._subtitles.flush()
        except Exception:
            pass

    def stop(self) -> None:
        if not self.enabled or not self._started.is_set() or self._stopping.is_set():
            return
        self._stopping.set()
        assert self._t0 is not None and self.dir is not None
        total = time.monotonic() - self._t0

        with self._lock:
            user_chunks = list(self._user_chunks)
            user_t_start = self._user_t_start
            ai_chunks = list(self._ai_chunks)
            ai_t_start = self._ai_t_start
            n_frames = len(self._frames)

        u_path = self.dir / "live_user.wav"
        a_path = self.dir / "live_ai.wav"
        u_dur = self._write_track_wav(u_path, user_t_start, user_chunks,
                                      self.user_sr, total)
        a_dur = self._write_track_wav(a_path, ai_t_start, ai_chunks,
                                      self.ai_sr, total)

        u_size = u_path.stat().st_size if u_path.exists() else 0
        a_size = a_path.stat().st_size if a_path.exists() else 0

        # 写 events.jsonl（供 rerun 的 LocalImageSource 读：chunk_idx → img 映射）
        try:
            import json as _json
            with self._lock:
                frames_snap = list(self._frames)
            ev_path = self.dir / "events.jsonl"
            with ev_path.open("w", encoding="utf-8") as ef:
                for (ft, frel, fidx) in frames_snap:
                    ef.write(_json.dumps(
                        {"kind": "chunk_sent", "idx": int(fidx),
                         "img": Path(frel).name, "t": round(ft, 3)},
                        ensure_ascii=False) + "\n")
            LOGGER.info("[LIVE] events.jsonl written: %d frames", len(frames_snap))
        except Exception as e:
            LOGGER.warning("[LIVE] write events.jsonl err: %r", e)

        # 写 frames.jsonl（整簇多帧 + 判定，供 funnel rerun 重判 / 无漏斗 rerun 取代表帧）
        try:
            import json as _json2
            with self._lock:
                rounds_snap = list(self._funnel_rounds)
            fr_path = self.dir / "frames.jsonl"
            with fr_path.open("w", encoding="utf-8") as ff:
                for rec in rounds_snap:
                    ff.write(_json2.dumps(rec, ensure_ascii=False) + "\n")
            LOGGER.info("[LIVE] frames.jsonl written: %d rounds", len(rounds_snap))
        except Exception as e:
            LOGGER.warning("[LIVE] write frames.jsonl err: %r", e)

        u_nz = self._nonzero_ratio(user_chunks)
        a_nz = self._nonzero_ratio(ai_chunks)

        LOGGER.info(
            "[LIVE] stopped: wall=%.2fs "
            "user(calls=%d audio=%.2fs t_start=%.2fs nonzero=%.1f%%) "
            "ai(calls=%d audio=%.2fs t_start=%.2fs nonzero=%.1f%%) "
            "frames=%d user_wav=%dB ai_wav=%dB",
            total,
            self._user_calls, u_dur,
            user_t_start if user_t_start is not None else -1, u_nz * 100,
            self._ai_calls, a_dur,
            ai_t_start if ai_t_start is not None else -1, a_nz * 100,
            n_frames, u_size, a_size,
        )

        if self._user_calls == 0:
            LOGGER.warning(
                "[LIVE] feed_user_raw() was NEVER called — "
                "esp32_audio_reader 没把 live_rec 接进来。"
            )
        if self._ai_calls == 0:
            LOGGER.warning(
                "[LIVE] AI callback NEVER fired — attach_to_player 没装上、"
                "speaker 未启动、或开了 --no-play。"
            )

        # 关闭评分用文本落盘
        for _fh in (self._transcript, self._subtitles, self._chunks_f):
            try:
                if _fh is not None:
                    _fh.close()
            except Exception:
                pass
        self._transcript = None
        self._subtitles = None
        self._chunks_f = None

        # 结束自动落盘 mp4（不用手动）：
        #   ① 整簇多帧 + 角落 reject 标注（演示主用，流畅 + 可视化漏斗决策）
        #   ② best 1fps 版（兼容旧用途）
        try:
            self.finalize_multiframe_mp4()
        except Exception as e:
            LOGGER.warning("[LIVE] multiframe mp4 finalize failed: %s", e)
        try:
            self.finalize_mp4()
        except Exception as e:
            LOGGER.warning("[LIVE] mp4 finalize failed: %s", e)

    # ------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------

    def feed_user_raw(self, pcm_f32: np.ndarray,t_override: Optional[float] = None) -> None:
        if (not self.enabled or not self._started.is_set()
                or self._stopping.is_set() or self._t0 is None):
            return
        if pcm_f32 is None or pcm_f32.size == 0:
            return
        if pcm_f32.dtype != np.float32:
            pcm_f32 = pcm_f32.astype(np.float32)
        # 新增:允许调用者提供精确的音频时间轴 t 戳(rerun 场景);
        # 否则按 wall clock 来(ESP32 场景)
        if t_override is not None:
            t = max(0.0, t_override)
        else:
            t = max(0.0, time.monotonic() - self._t0)
        #t = max(0.0, time.monotonic() - self._t0)
        with self._lock:
            if self._user_t_start is None:
                self._user_t_start = t
                rms = float(np.sqrt(np.mean(pcm_f32 ** 2)))
                LOGGER.info("[LIVE] first feed_user_raw(): %d samples rms=%.4f t=%.2fs",
                            pcm_f32.size, rms, t)
            self._user_chunks.append((t, pcm_f32.copy()))
            self._user_calls += 1

    def on_frame(self, jpeg_bytes: Optional[bytes], chunk_idx: int) -> None:
        if (not self.enabled or not self._started.is_set()
                or self._stopping.is_set() or not jpeg_bytes
                or self._t0 is None or self.dir is None):
            return
        t = time.monotonic() - self._t0
        rel = f"images/img_{chunk_idx:05d}.jpg"
        # 实际写 jpg 落盘（供 rerun 的 LocalImageSource 读）。
        try:
            img_dir = self.dir / "images"
            img_dir.mkdir(parents=True, exist_ok=True)
            if not self.no_media:
                (self.dir / rel).write_bytes(jpeg_bytes)
        except Exception as e:
            LOGGER.warning("[LIVE] write frame jpg err: %r", e)
            return
        with self._lock:
            self._frames.append((t, rel, int(chunk_idx)))

    def on_funnel_round(self, decision, chunk_idx: int, text: str = "") -> None:
        """存漏斗一轮：整簇多帧(筛前原始)落盘 q{NNN}_f{MM}.jpg + frames.jsonl 记一条。
        统一 record 的核心：一份 record 供三种 rerun——
          无漏斗 rerun(每轮取代表帧)/ funnel rerun(整簇重判)/ -o rerun(取当时best)。
        """
        if (not self.enabled or not self._started.is_set()
                or self._stopping.is_set() or self._t0 is None or self.dir is None):
            return
        t = time.monotonic() - self._t0
        frames = getattr(decision, "frames", None)
        if not frames:
            b = getattr(decision, "best", None) or getattr(decision, "best_jpg", None)
            frames = [b] if b else []
        # qid 用独立轮次计数（每轮 +1），不能用 latest_frame.sequence——
        # 那个只在 send 时递增，一直 reject（晃动/糊）时不变 → qid 不变 → 整簇 jpg
        # 反复用同名 q{同值}_fNN 覆盖旧图，record 存不下、rerun 没图。
        self._round_counter = getattr(self, "_round_counter", -1) + 1
        qid = self._round_counter
        frame_names = []
        try:
            img_dir = self.dir / "images"
            img_dir.mkdir(parents=True, exist_ok=True)
            for i, jpg in enumerate(frames):
                if not jpg:
                    continue
                name = f"q{qid:05d}_f{i:02d}.jpg"
                if not self.no_media:
                    (img_dir / name).write_bytes(jpg)
                frame_names.append(name)
        except Exception as e:
            LOGGER.warning("[LIVE] on_funnel_round write err: %r", e)
            return
        rec = {
            "qid": qid,
            "t": round(t, 3),
            "chunk_idx": int(chunk_idx),
            "frames": frame_names,
            "best_index": getattr(decision, "best_index", None),
            "send": bool(getattr(decision, "send", False)),
            "reason": getattr(decision, "reason", ""),
            "text": text,
        }
        with self._lock:
            self._funnel_rounds.append(rec)

    # ------------------------------------------------------------
    # WAV writing
    # ------------------------------------------------------------

    @staticmethod
    def _nonzero_ratio(chunks: list[tuple[float, np.ndarray]]) -> float:
        if not chunks:
            return 0.0
        total, nz = 0, 0
        for _, pcm in chunks:
            total += pcm.size
            nz += int(np.sum(np.abs(pcm) > 1e-4))
        return (nz / total) if total > 0 else 0.0

    @staticmethod
    def _write_track_wav(
            path: Path,
            t_start: Optional[float],  # 仅用于日志兼容,不再决定起点
            chunks: list[tuple[float, np.ndarray]],
            sr: int,
            wall_dur: float,
    ) -> float:
        """录屏式写盘:每个 chunk 落在它真实的 arrival 时间上,空隙留 0。"""
        if not chunks:
            n_total = max(int(wall_dur * sr), 1)
            track = np.zeros(n_total, dtype=np.float32)
            audio_dur = 0.0
        else:
            # 计算总长 = max(最后一段尾巴, wall_dur)
            end_time = max(t + pcm.size / sr for t, pcm in chunks)
            n_total = max(int(max(end_time, wall_dur) * sr), 1)
            track = np.zeros(n_total, dtype=np.float32)
            audio_dur = 0.0  # 这里改为"有效样本"的累计,不再是 concat 长度
            prev_end_samp = 0
            for t, pcm in chunks:
                i = max(0, int(t * sr))
                # 防止下一段的 arrival time 小于上一段结束——重叠时按上一段尾巴顺延
                i = max(i, prev_end_samp)
                j = min(i + pcm.size, n_total)
                if j > i:
                    track[i:j] = pcm[: j - i]
                    prev_end_samp = j
                    audio_dur += (j - i) / sr

        i16 = np.clip(track * 32768.0, -32768, 32767).astype(np.int16)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1);
            w.setsampwidth(2);
            w.setframerate(sr)
            w.writeframes(i16.tobytes())
        return audio_dur

    # ------------------------------------------------------------
    # MP4 finalize
    # ------------------------------------------------------------

    def finalize_mp4(self) -> Optional[Path]:
        if self.no_media:
            return None
        if not self.enabled or self.dir is None:
            return None
        if shutil.which("ffmpeg") is None:
            LOGGER.warning("[LIVE] ffmpeg not found. WAV/帧已保存在 %s。", self.dir)
            return None
        main_out = None


        try:
            main_out = self._do_finalize_mp4()
        except Exception as e:
            LOGGER.warning("[LIVE] mp4 finalize failed: %s", e)
        # 额外合成一份 user-only,用于 gateway 8006 视频输入测试
        try:
            self._do_finalize_useronly_mp4()
        except Exception as e:
            LOGGER.warning("[LIVE] useronly mp4 finalize failed: %s", e)
        return main_out

    def _do_finalize_useronly_mp4(self) -> Optional[Path]:
        """额外合成一份只含 user 音轨的 mp4,用于给 gateway 的 8006 视频输入端做 fixture 测试。"""
        d = self.dir
        assert d is not None
        user_wav = d / "live_user.wav"
        if not user_wav.exists():
            LOGGER.info("[LIVE] no user wav, skip useronly mp4")
            return None

        with wave.open(str(user_wav), "rb") as w:
            u_dur = w.getnframes() / w.getframerate()
        if u_dur <= 0.5:
            LOGGER.info("[LIVE] user too short (%.2fs), skip useronly mp4", u_dur)
            return None

        with self._lock:
            frames = list(self._frames)

        concat_txt = d / "_useronly_frames.txt"
        if frames:
            with concat_txt.open("w", encoding="utf-8") as f:
                f.write("ffconcat version 1.0\n")
                first_t = frames[0][0]
                if first_t > 0.05:
                    f.write(f"file '{frames[0][1]}'\n")
                    f.write(f"duration {first_t:.3f}\n")
                for i, (rt, p, _idx) in enumerate(frames):
                    f.write(f"file '{p}'\n")
                    if i + 1 < len(frames):
                        dur = max(0.04, frames[i + 1][0] - rt)
                    else:
                        dur = max(0.5, u_dur - rt)
                    f.write(f"duration {dur:.3f}\n")
                f.write(f"file '{frames[-1][1]}'\n")

        if not frames:
            out = d / "live_useronly.m4a"
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
                "-i", str(user_wav),
                "-c:a", "aac", "-b:a", "192k",
                "-ac", "1",
                "-t", f"{u_dur:.3f}",
                str(out),
            ]
        else:
            out = d / "live_useronly.mp4"
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
                "-f", "concat", "-safe", "0", "-i", str(concat_txt),
                "-i", str(user_wav),
                "-map", "0:v", "-map", "1:a",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                "-vsync", "vfr",
                "-c:a", "aac", "-b:a", "192k",
                "-ac", "1",
                "-t", f"{u_dur:.3f}",
                str(out),
            ]

        LOGGER.info(
            "[LIVE] assembling useronly: frames=%d u_dur=%.1fs → %s",
            len(frames), u_dur, out.name,
        )
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                LOGGER.warning("[LIVE] useronly ffmpeg rc=%d stderr tail:\n%s",
                               r.returncode, r.stderr[-1200:])
                return None
            if out.exists():
                LOGGER.info("[LIVE] ✅ saved %s (%.1f MB, %.1fs)",
                            out, out.stat().st_size / 1e6, u_dur)
            return out
        except subprocess.TimeoutExpired:
            LOGGER.warning("[LIVE] useronly ffmpeg timeout (>10min)")
            return None
        finally:
            try:
                if concat_txt.exists():
                    concat_txt.unlink()
            except Exception:
                pass
    def _do_finalize_mp4(self) -> Optional[Path]:
        d = self.dir
        assert d is not None
        user_wav = d / "live_user.wav"
        ai_wav = d / "live_ai.wav"
        if not (user_wav.exists() and ai_wav.exists()):
            LOGGER.info("[LIVE] no wav files, skip mp4")
            return None

        with self._lock:
            frames = list(self._frames)

        with wave.open(str(user_wav), "rb") as w:
            u_dur = w.getnframes() / w.getframerate()
        with wave.open(str(ai_wav), "rb") as w:
            a_dur = w.getnframes() / w.getframerate()
        total_dur = max(u_dur, a_dur)
        if total_dur <= 0.5:
            LOGGER.info("[LIVE] session too short (%.2fs), skip mp4", total_dur)
            return None

        concat_txt = d / "_live_frames.txt"
        if frames:
            with concat_txt.open("w", encoding="utf-8") as f:
                f.write("ffconcat version 1.0\n")
                first_t = frames[0][0]
                if first_t > 0.05:
                    f.write(f"file '{frames[0][1]}'\n")
                    f.write(f"duration {first_t:.3f}\n")
                for i, (rt, p, _idx) in enumerate(frames):
                    f.write(f"file '{p}'\n")
                    if i + 1 < len(frames):
                        dur = max(0.04, frames[i + 1][0] - rt)
                    else:
                        dur = max(0.5, total_dur - rt)
                    f.write(f"duration {dur:.3f}\n")
                f.write(f"file '{frames[-1][1]}'\n")

        # apad + -t 让短的一轨自动补尾静音对齐到 mp4 总时长
        if not frames:
            out = d / "live_session.m4a"
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
                "-i", str(user_wav), "-i", str(ai_wav),
                "-filter_complex",
                "[0:a]aresample=24000,aformat=channel_layouts=mono,apad[u];"
                "[1:a]aformat=channel_layouts=mono,apad[a];"
                "[u][a]amerge=inputs=2[aout]",
                "-map", "[aout]",
                "-c:a", "aac", "-b:a", "192k",
                "-t", f"{total_dur:.3f}",
                str(out),
            ]
        else:
            out = d / "live_session.mp4"
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
                "-f", "concat", "-safe", "0", "-i", str(concat_txt),
                "-i", str(user_wav), "-i", str(ai_wav),
                "-filter_complex",
                "[1:a]aresample=24000,aformat=channel_layouts=mono,apad[u];"
                "[2:a]aformat=channel_layouts=mono,apad[a];"
                "[u][a]amerge=inputs=2[aout]",
                "-map", "0:v", "-map", "[aout]",
                #"-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                "-vsync", "vfr",
                "-c:a", "aac", "-b:a", "192k",
                "-t", f"{total_dur:.3f}",
                str(out),
            ]

        LOGGER.info(
            "[LIVE] assembling: frames=%d total=%.1fs (u=%.1fs a=%.1fs) → %s",
            len(frames), total_dur, u_dur, a_dur, out.name,
        )
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                LOGGER.warning(
                    "[LIVE] ffmpeg rc=%d stderr tail:\n%s",
                    r.returncode, r.stderr[-1200:],
                )
                return None
            if out.exists():
                LOGGER.info(
                    "[LIVE] ✅ saved %s (%.1f MB, %.1fs)",
                    out, out.stat().st_size / 1e6, total_dur,
                )
            return out
        except subprocess.TimeoutExpired:
            LOGGER.warning("[LIVE] ffmpeg timeout (>10min)")
            return None
        finally:
            try:
                if concat_txt.exists():
                    concat_txt.unlink()
            except Exception:
                pass


    # ------------------------------------------------------------
    # 整簇多帧 mp4（演示用）：用每轮整簇多帧拼，比 1fps best 流畅；
    #   角落用 ass 字幕标注每轮 send/reject + reason，直接可视化漏斗决策。
    #   对比无漏斗/有漏斗两份 mp4，即可证明"模糊图被拒、不进模型"的正确性。
    # ------------------------------------------------------------
    def _ass_escape(self, s: str) -> str:
        return (s or "").replace("\\", "\\\\").replace("{", "(").replace("}", ")")

    def _gen_reject_ass(self, rounds, total_dur: float) -> Optional[Path]:
        """生成角落标注字幕：每轮时间段显示 SEND / REJECT:reason。"""
        d = self.dir
        if d is None or not rounds:
            return None
        def _fmt(t):
            t = max(0.0, t)
            h = int(t // 3600); m = int((t % 3600) // 60)
            s = t % 60
            return f"{h:d}:{m:02d}:{s:05.2f}"
        ass = d / "_reject_notes.ass"
        header = (
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 1280\nPlayResY: 720\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, Bold, Alignment, "
            "MarginL, MarginR, MarginV, BorderStyle, Outline, Shadow\n"
            # Alignment 9 = 右上角；红=&H000000FF 绿=&H0000FF00 (ASS 是 &HAABBGGRR)
            "Style: REJ,Arial,36,&H000000FF,1,9,20,20,20,1,2,0\n"
            "Style: SND,Arial,36,&H0000FF00,1,9,20,20,20,1,2,0\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Text\n"
        )
        lines = []
        for i, rec in enumerate(rounds):
            t0 = float(rec.get("t", i))
            t1 = float(rounds[i + 1].get("t", t0 + 1.0)) if i + 1 < len(rounds) else total_dur
            if t1 <= t0:
                t1 = t0 + 0.3
            send = bool(rec.get("send"))
            reason = self._ass_escape(rec.get("reason", ""))
            if send:
                style, txt = "SND", "SEND ✓"
            else:
                style, txt = "REJ", f"REJECT ✗ {reason}"
            lines.append(f"Dialogue: 0,{_fmt(t0)},{_fmt(t1)},{style},,{txt}")
        try:
            ass.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
            return ass
        except Exception as e:
            LOGGER.warning("[LIVE] write reject ass err: %r", e)
            return None

    def finalize_multiframe_mp4(self) -> Optional[Path]:
        if self.no_media:
            return None
        """整簇多帧 mp4 + 角落 reject 标注。演示用，比 best 1fps 流畅。"""
        if not self.enabled or self.dir is None:
            return None
        if shutil.which("ffmpeg") is None:
            LOGGER.warning("[LIVE] ⚠ ffmpeg 未安装，无法生成 mp4！"
                           "装法: conda install -c conda-forge ffmpeg。"
                           "帧和 wav 已存在 %s，可手动拼。", self.dir)
            return None
        d = self.dir
        user_wav = d / "live_user.wav"
        ai_wav = d / "live_ai.wav"
        if not (user_wav.exists() and ai_wav.exists()):
            return None
        with self._lock:
            rounds = list(self._funnel_rounds)
        if not rounds:
            LOGGER.info("[LIVE] no funnel rounds, skip multiframe mp4")
            return None

        with wave.open(str(user_wav), "rb") as w:
            u_dur = w.getnframes() / w.getframerate()
        with wave.open(str(ai_wav), "rb") as w:
            a_dur = w.getnframes() / w.getframerate()
        total_dur = max(u_dur, a_dur)
        if total_dur <= 0.5:
            return None

        # 整簇多帧 concat：每轮的 t 到下一轮的 t 之间，均分给该轮的 N 帧（高 fps）
        concat_txt = d / "_multiframe.txt"
        with concat_txt.open("w", encoding="utf-8") as f:
            f.write("ffconcat version 1.0\n")
            first_t = float(rounds[0].get("t", 0.0))
            if first_t > 0.05 and rounds[0].get("frames"):
                f.write(f"file 'images/{Path(rounds[0]['frames'][0]).name}'\n")
                f.write(f"duration {first_t:.3f}\n")
            for i, rec in enumerate(rounds):
                names = rec.get("frames") or []
                if not names:
                    continue
                t0 = float(rec.get("t", i))
                t1 = float(rounds[i + 1].get("t", t0 + 1.0)) if i + 1 < len(rounds) else total_dur
                round_dur = max(0.12, t1 - t0)
                per = round_dur / len(names)   # 整簇内多帧均分 → 高 fps
                for nm in names:
                    f.write(f"file 'images/{Path(nm).name}'\n")
                    f.write(f"duration {max(0.03, per):.3f}\n")
            # 末帧兜底
            last_names = rounds[-1].get("frames") or []
            if last_names:
                f.write(f"file 'images/{Path(last_names[-1]).name}'\n")

        ass = self._gen_reject_ass(rounds, total_dur)
        vf = "format=yuv420p"
        if ass is not None:
            safe = str(ass.name)  # 相对 cwd 需在 dir 下运行；用绝对更稳
            abs_ass = str(ass.resolve()).replace("\\", "/").replace(":", "\\:")
            vf = f"ass='{abs_ass}',format=yuv420p"

        out = d / "live_multiframe.mp4"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-f", "concat", "-safe", "0", "-i", str(concat_txt),
            "-i", str(user_wav), "-i", str(ai_wav),
            "-filter_complex",
            "[1:a]aresample=24000,aformat=channel_layouts=mono,apad[u];"
            "[2:a]aformat=channel_layouts=mono,apad[a];"
            "[u][a]amerge=inputs=2[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-vsync", "vfr",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{total_dur:.3f}",
            str(out),
        ]
        LOGGER.info("[LIVE] assembling multiframe mp4: rounds=%d total=%.1fs → %s",
                    len(rounds), total_dur, out.name)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                LOGGER.warning("[LIVE] multiframe ffmpeg rc=%d stderr:\n%s",
                               r.returncode, r.stderr[-1200:])
                return None
            if out.exists():
                LOGGER.info("[LIVE] ✅ multiframe mp4 saved %s (%.1f MB)",
                            out, out.stat().st_size / 1e6)
            return out
        except subprocess.TimeoutExpired:
            LOGGER.warning("[LIVE] multiframe ffmpeg timeout")
            return None
        finally:
            for p in (concat_txt, ass):
                try:
                    if p is not None and p.exists():
                        p.unlink()
                except Exception:
                    pass
