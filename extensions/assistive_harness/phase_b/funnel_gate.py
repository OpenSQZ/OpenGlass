"""漏斗闸门（接入 -o 链路）。

把 cam_pipeline_v2 的 run_funnel / process_orientation / CamControl 封装成
"一轮漏斗判定"，供 esp32_runtime 在图像循环里调用：

    gate = FunnelGate(esp32_host, scene="medicine", n_frames=3)
    result = await gate.run_once(capture_fn)   # capture_fn: async ()->Optional[bytes]

返回 FunnelDecision：
    send   : bool           是否放行给模型
    best   : bytes|None     放行的最佳帧（已做倒置纠正）
    reason : str            决策原因（accepted/severe_shake/unstable/need_focus/orient...）
    hint   : str|None       拒绝时给用户的提示文本（先输出 text，TTS 通道待定）
    timings: dict           grab_ms / judge_ms / orient_ms / af_ms / n_frames

设计依据（防漂移文档 + cam_pipeline_v2）：
  - 判定唯一 = run_funnel 的 frame_score 三出口 + argmin 归因
  - need_focus → 触发单次 AF(0x3022) → 等 AF_SETTLE_MS → 重抓一簇 → 重判
  - 倒置 = process_orientation，flipped 时纠正或提示
  - 拒绝 reason → HINTS 文本
本模块只做"输入端把关+引导"，不碰模型输出侧（那是 harness 的事）。
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

from . import cam_pipeline_v2 as cp

AF_SETTLE_MS = 120   # OV5640 单次对焦 settle（与 v5 一致）

# reject_reason -> 用户提示（先输出 text；TTS 通道确定后接同一份文本）
HINTS = {
    "no_frames":    "没有拿到画面，请稍等",
    "severe_shake": "画面晃得厉害，请先保持不动",
    "unstable":     "画面还在晃动，请保持不动一会儿",
    "too_dark":     "光线太暗，请到亮一点的地方",
    "aimed_wrong":  "好像没对准，请对准目标",
    "need_focus":   "对焦中，请拿稳一下",
    "orient":       "画面好像反了，请倒过来",
}


class FunnelDecision:
    __slots__ = ("send", "best", "reason", "hint", "timings",
                 "frames", "best_index", "af_triggered", "af_ok")

    def __init__(self, send, best, reason, hint, timings,
                 frames=None, best_index=-1, af_triggered=False, af_ok=None):
        self.send = send
        self.best = best
        self.reason = reason
        self.hint = hint
        self.timings = timings
        self.frames = frames or []       # 整簇帧（存 session 用）
        self.best_index = best_index     # best 在簇里的下标
        self.af_triggered = af_triggered # 本轮有没有触发 AF
        self.af_ok = af_ok               # AF /reg 请求是否返回成功（None=没触发）


class FunnelGate:
    def __init__(self, esp32_host: str, scene: str = "medicine",
                 n_frames: int = 3, frame_gap_s: float = 0.0,
                 enable_focus: bool = True):
        self.scene = scene
        self.n_frames = n_frames
        self.frame_gap_s = frame_gap_s
        self.enable_focus = enable_focus
        self._cfg = (cp.make_scene_config(scene)
                     if hasattr(cp, "make_scene_config") else cp.FunnelConfig())
        # 相机 HTTP 控制走 /control、/reg 到 ESP32（同 esp32_host）。
        # camctl 总是构造：分辨率(set_resolution)独立于对焦，即使不对焦也要设 HD。
        # 对焦(trigger_af)另受 enable_focus 控制。
        self._camctl = cp.CamControl(esp32_host)
        self._last_af_mono = 0.0   # 上次真正触发对焦的时刻（冷却用）
        self._af_cooldown_s = 2.0  # 对焦冷却：2s 内最多触发 1 次（避免每秒对焦）

    def warmup_orient(self) -> float:
        """方向分类器预热（同步，调用方放线程里）。

        原设计（-v pc_vlm_v5_funnel）：首次 process_orientation 含模型加载 + oneDNN
        编译开销（实测 ~2s，本次日志里甚至 5.76s），启动时先跑一张假图，把这笔一次性
        开销挪到启动阶段，运行时 orient_ms 就是纯推理（md 记录：2143ms → 9ms）。
        -o 迁移时漏了这步 → orient 直到第一次 accept 才现场加载，卡住那一轮，
        且在此之前链路出不了 send。返回首次耗时(ms)。
        """
        import numpy as _np
        import cv2 as _cv2
        _warm = _np.full((480, 640, 3), 255, dtype=_np.uint8)
        ok, buf = _cv2.imencode(".jpg", _warm)
        if not ok:
            return -1.0
        t0 = time.monotonic()
        cp.process_orientation(buf.tobytes())
        return (time.monotonic() - t0) * 1000.0

    def set_resolution_hd(self, retries: int = 5, gap_s: float = 0.6) -> bool:
        """设 1280×720（UXGA档，这块 OV5640 枚举非标准，HD(11)无效、UXGA(13)才 720p）。
        live 启动时 ESP32 可能刚就绪，/control 偶发失败 → 重试几次，避免要手动进网页设。
        失败时打详细原因（status/异常），不再静默。"""
        import logging as _lg
        _log = _lg.getLogger("funnel_gate")
        for i in range(max(1, retries)):
            try:
                ok = self._camctl.set_resolution("UXGA")
                if ok:
                    if i > 0:
                        _log.info("[funnel_gate] set_resolution(UXGA) 第%d次重试成功", i + 1)
                    return True
                _log.warning("[funnel_gate] set_resolution(UXGA) 返回非200 (第%d/%d次)",
                             i + 1, retries)
            except Exception as e:
                _log.warning("[funnel_gate] set_resolution(UXGA) 异常 (第%d/%d次): %r",
                             i + 1, retries, e)
            time.sleep(gap_s)
        return False

    async def _grab_burst(
        self, capture_fn: Callable[[], Awaitable[Optional[bytes]]], n: int
    ) -> list[bytes]:
        frames: list[bytes] = []
        for i in range(n):
            jpeg = await capture_fn()
            if jpeg:
                frames.append(jpeg)
            if self.frame_gap_s > 0 and i < n - 1:
                await asyncio.sleep(self.frame_gap_s)
        return frames

    async def run_once(
        self, capture_fn: Callable[[], Awaitable[Optional[bytes]]]
    ) -> FunnelDecision:
        """跑一轮漏斗：抓 N 帧 -> run_funnel -> (need_focus 则对焦重抓) -> 倒置 -> 决策。"""
        timings: dict = {}

        # 1) 抓一簇
        t0 = time.monotonic()
        frames = await self._grab_burst(capture_fn, self.n_frames)
        timings["grab_ms"] = round((time.monotonic() - t0) * 1000, 1)
        timings["n_frames"] = len(frames)
        if not frames:
            return FunnelDecision(False, None, "no_frames", HINTS["no_frames"], timings)

        # 2) run_funnel 判定（run_funnel 是同步的，放线程池避免阻塞事件循环）
        tj = time.monotonic()
        res = await asyncio.to_thread(cp.run_funnel, frames, self._cfg)
        timings["judge_ms"] = round((time.monotonic() - tj) * 1000, 1)
        timings["af_ms"] = 0.0
        af_triggered = False
        af_ok = None

        # 3) need_focus -> 触发单次 AF -> 等 settle -> 重抓 -> 重判
        #    对焦冷却：need_focus 但距上次对焦 < _af_cooldown_s(2s) 时不重复触发，
        #    给对焦时间生效（每秒对焦太快，马达反复动反而对不好）。冷却内仍 need_focus
        #    就用当前帧判定（该 reject 就 reject，不重抓）。
        if res.need_focus and self.enable_focus and self._camctl is not None:
            _now = time.monotonic()
            if _now - self._last_af_mono >= self._af_cooldown_s:
                self._last_af_mono = _now
                taf = _now
                af_triggered = True
                af_ok = await asyncio.to_thread(self._camctl.trigger_af)  # True/False
                await asyncio.sleep(AF_SETTLE_MS / 1000.0)
                frames2 = await self._grab_burst(capture_fn, self.n_frames)
                if frames2:
                    res = await asyncio.to_thread(cp.run_funnel, frames2, self._cfg)
                    frames = frames2
                timings["af_ms"] = round((time.monotonic() - taf) * 1000, 1)
                timings["n_frames"] = len(frames)
            else:
                # 冷却期内：不重复对焦，用当前判定结果
                timings["af_cooldown"] = round(self._af_cooldown_s - (_now - self._last_af_mono), 2)
        timings["af_ok"] = af_ok

        def _mk(send, best, reason, hint):
            bi = frames.index(best) if (best in frames) else -1
            return FunnelDecision(send, best, reason, hint, timings,
                                  frames=frames, best_index=bi,
                                  af_triggered=af_triggered, af_ok=af_ok)

        # 4) 拒绝出口
        if not res.accepted:
            reason = res.reject_reason or "reject"
            # cam_pipeline 的拒绝出口只设 best_index（注释：仅供参考, 不喂模型），
            # 不设 best_jpg —— 这是原设计意图：拒绝就不送图。
            # 但消融 arm（--no-reject：只选 best、永远放行）需要拿到这一帧，
            # 所以这里按 best_index 把帧补出来。**send 仍然是 False**，
            # 正常链路完全不受影响：只有显式开了 --no-reject 才会去用它。
            _bj = res.best_jpg
            if _bj is None:
                bi = getattr(res, "best_index", -1)
                if isinstance(bi, int) and 0 <= bi < len(frames):
                    _bj = frames[bi]
            return _mk(False, _bj, reason,
                       HINTS.get(reason, "看不清楚，请调整一下"))

        # 5) 接受帧的方向检测（OCR 方向分类器优先，见 cam_pipeline.process_orientation）
        #    原则「宁拒绝不念错」：方向不正 -> 拒绝 + 提示用户转正，绝不自动纠正、绝不送倒图。
        #    process_orientation 只诊断方向、返回的 jpg 未旋转，故不能拿它当"纠正后"送模型。
        best = res.best_jpg
        to_ = time.monotonic()
        try:
            _jpg, geom = await asyncio.to_thread(cp.process_orientation, best)
        except Exception:
            geom = {"ok": False}
        timings["orient_ms"] = round((time.monotonic() - to_) * 1000, 1)

        orient_state = geom.get("orient_state") if isinstance(geom, dict) else None
        orient_hint = geom.get("orient_hint") if isinstance(geom, dict) else None

        # upright 才放行；flipped/sideways 拒绝并提示；uncertain 也放行（不硬拦，避免误拒）
        if orient_state in ("flipped", "sideways"):
            hint = orient_hint or HINTS["orient"]
            return _mk(False, best, "orient_" + orient_state, hint)

        # upright / uncertain / 检测不可用 -> 放行原图（不做任何旋转）
        return _mk(True, best, "send", None)
