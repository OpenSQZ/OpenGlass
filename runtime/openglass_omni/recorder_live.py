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
    ):
        self.enabled = session_dir is not None
        self.dir: Optional[Path] = Path(session_dir) if session_dir else None
        self.user_sr = user_sr
        self.ai_sr = ai_sr

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
        self._frames: list[tuple[float, str]] = []

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

        def wrapped_enqueue(pcm_f32):
            orig_enqueue(pcm_f32)
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
        player._orig_enqueue = orig_enqueue  # ← 新增:暴露原始方法,绕过 hook 的场景使用
        LOGGER.info("[LIVE] attached to SPK enqueue (no audio-thread overhead)")

    def start(self) -> None:
        if not self.enabled or self._started.is_set():
            return
        assert self.dir is not None
        self.dir.mkdir(parents=True, exist_ok=True)
        #(self.dir / "live_images").mkdir(exist_ok=True)
        self._t0 = time.monotonic()
        self._started.set()
        LOGGER.info(
            "[LIVE] recording started: %s (user=%dHz ai=%dHz)",
            self.dir, self.user_sr, self.ai_sr,
        )

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
        with self._lock:
            self._frames.append((t, rel))

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
                for i, (rt, p) in enumerate(frames):
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
                for i, (rt, p) in enumerate(frames):
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

