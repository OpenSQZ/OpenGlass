"""时序探针（纯旁路，不改链路行为）。

只做一件事：把 ESP32 -o 链路里"取图 / 音频 / 相位"的时间点写进一个 CSV，
供事后分析漏斗该插在哪、跨几个 chunk、音图怎么抢。

不接对焦、不接漏斗判定 —— 这是纯时序探针。

列名尽量沿用 -v 的 latency CSV 风格（grab_ms / n_frames 等），
额外加 -o 特有的 chunk / 音频相位列，方便和 -v 数据对比、也让分析工具链复用。

用法（在 esp32_runtime 里）：
    from .timing_probe import TimingProbe
    probe = TimingProbe(enabled=args.timing_probe, path=args.timing_csv, chunk_ms=1000)
    # 取图：
    probe.mark_grab(seq, grab_ms, jpeg_bytes, since_last_ms)
    # 音频（每包调，内部按秒聚合）：
    probe.mark_audio(seq, n_samples, drops, rms)
    # 关闭：probe.close()
"""
from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Optional


class TimingProbe:
    def __init__(self, enabled: bool = True, path: Optional[str] = None,
                 chunk_ms: int = 1000):
        self.enabled = enabled
        self.chunk_ms = chunk_ms
        self._t0 = time.monotonic()          # 进程起点，用于算 rel_ms / chunk_id
        self._f = None
        self._w = None

        # ── 音频活动/静默相位判定（用于给每帧图标 phase）──
        # 简单能量门限：rms > 阈值算"活动"，否则"静默"。阈值可调。
        self._voice_rms_gate = 0.01
        self._last_audio_active_rel_ms = -1e9   # 最近一次音频活动的相对时刻
        self._audio_active = False

        # ── 音频按秒聚合缓存 ──
        self._agg_bucket = -1                 # 当前聚合到第几秒
        self._agg_pkts = 0
        self._agg_seqgap = 0                  # 本秒内 seq 跳变累计（= 真丢包数）
        self._agg_rms_sum = 0.0
        self._agg_active_pkts = 0
        self._last_seq = None                 # 上一包 seq，用于算 gap

        if self.enabled:
            p = Path(path or f"timing_{time.strftime('%Y%m%d_%H%M%S')}.csv")
            self._f = open(p, "w", newline="", encoding="utf-8")
            self._w = csv.writer(self._f)
            # 统一表头：kind 区分 grab / audio 两类行
            self._w.writerow([
                "kind",          # "grab" 或 "audio"
                "rel_ms",        # 相对进程启动的毫秒（对齐两类事件的时间轴）
                "chunk_id",      # rel_ms // chunk_ms，看事件落在第几个 chunk
                "seq",
                # grab 专用
                "grab_ms",       # 本次取图/一轮漏斗耗时
                "jpeg_bytes",
                "since_last_grab_ms",
                "audio_phase",   # 取这帧图时音频是 active / silent（相位）
                "ms_since_voice",# 距最近一次音频活动多久（找静默缝）
                # grab 专用 —— 漏斗字段
                "reason",        # 漏斗决策：send/severe_shake/unstable/need_focus/orient...
                "judge_ms",      # run_funnel 判定耗时
                "af_ms",         # 对焦触发+settle+重抓耗时（0=没触发对焦）
                "n_frames",      # 本轮抓帧数
                # audio 专用（按秒聚合）
                "audio_pps",     # 这一秒的包数
                "audio_seqgap",  # 这一秒 seq 跳变累计 = 真丢包数（不是固件 reserved）
                "audio_rms",     # 这一秒平均 rms
                "audio_active_pps",  # 这一秒里"活动"包数
            ])
            print(f"[TimingProbe] writing {p}")

    def _rel_ms(self) -> float:
        return (time.monotonic() - self._t0) * 1000.0

    # ── 取图事件 ──
    def mark_grab(self, seq: int, grab_ms: float, jpeg_bytes: int,
                  since_last_ms: float, reason: str = "", judge_ms=None,
                  af_ms=None, n_frames=None) -> None:
        if not self.enabled:
            return
        rel = self._rel_ms()
        phase = "active" if self._audio_active else "silent"
        ms_since_voice = rel - self._last_audio_active_rel_ms
        if ms_since_voice > 1e8:
            ms_since_voice = -1  # 还没出现过音频活动
        self._w.writerow([
            "grab", round(rel, 1), int(rel // self.chunk_ms), seq,
            round(grab_ms, 1), jpeg_bytes, round(since_last_ms, 1),
            phase, round(ms_since_voice, 1),
            reason, judge_ms if judge_ms is not None else "",
            af_ms if af_ms is not None else "",
            n_frames if n_frames is not None else "",
            "", "", "", "",   # audio 专用列留空
        ])
        self._f.flush()

    # ── 音频事件（每包调，内部按秒聚合写一行）──
    #   注意：固件包头第4字段是 reserved(pad)，不是丢包数；真丢包用 seq 跳变推断。
    def mark_audio(self, seq: int, n_samples: int, rms: float) -> None:
        if not self.enabled:
            return
        rel = self._rel_ms()

        # 更新相位状态（供 mark_grab 读）
        self._audio_active = rms > self._voice_rms_gate
        if self._audio_active:
            self._last_audio_active_rel_ms = rel

        # seq 跳变 = 真丢包（PC 视角：应收 seq 连续，缺号即丢）
        gap = 0
        if self._last_seq is not None:
            d = seq - self._last_seq - 1
            if 0 < d < 10000:   # 合理范围，排除重连/回绕
                gap = d
        self._last_seq = seq

        # 按秒聚合
        bucket = int(rel // 1000)
        if self._agg_bucket == -1:
            self._agg_bucket = bucket
        if bucket != self._agg_bucket:
            self._flush_audio_bucket()
            self._agg_bucket = bucket
        self._agg_pkts += 1
        self._agg_seqgap += gap
        self._agg_rms_sum += rms
        if rms > self._voice_rms_gate:
            self._agg_active_pkts += 1

    def _flush_audio_bucket(self) -> None:
        if self._agg_pkts == 0:
            return
        rel = self._agg_bucket * 1000.0
        avg_rms = self._agg_rms_sum / self._agg_pkts
        self._w.writerow([
            "audio", round(rel, 1), int(rel // self.chunk_ms), "",
            "", "", "", "", "",          # grab 基础列留空
            "", "", "", "",              # grab 漏斗列留空
            self._agg_pkts, self._agg_seqgap, round(avg_rms, 4), self._agg_active_pkts,
        ])
        self._f.flush()
        self._agg_pkts = 0
        self._agg_seqgap = 0
        self._agg_rms_sum = 0.0
        self._agg_active_pkts = 0

    def close(self) -> None:
        if not self.enabled:
            return
        try:
            self._flush_audio_bucket()
            if self._f:
                self._f.close()
        except Exception:
            pass
