"""Session 录制器（对齐 -v 的 SessionRecorder 格式）。

沿用 pc_vlm_v5_funnel.SessionRecorder 的落盘结构，让 -o 链路录出的 session
和 -v 完全一致，可用 -v 的分析 / rerun 工具链直接读。

    sessions/<sid>/
        images/q{NNN}_f{MM}.jpg      每轮一簇帧（整簇存）
        queries.jsonl                每行一条，-v 原字段 + 漏斗判定字段
        meta.json

启动标志：-v 是「ASR 出 query」，-o 里换成「链路启动」——从 start 起持续录，
每轮漏斗决策 append 一条。qid 单调递增（一轮 = 一个 qid）。

queries.jsonl 每行（保留 -v 原字段 qid/text/frames/ts，兼容；追加漏斗字段）：
    {
      "qid": 3, "ts": 1699...,           # -v 原字段
      "text": "",                        # -v 原字段（-o 无 ASR query 文本，留空/可后填）
      "frames": ["q003_f00.jpg", ...],   # -v 原字段（整簇帧文件名）
      "reason": "severe_shake",          # 漏斗判定
      "send": false,
      "hint": "晃得厉害，请拿稳一下",
      "best_index": 1,
      "af_triggered": true, "af_ok": true,
      "timings": {...}
    }
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional


class SessionRecorder:
    def __init__(self, sessions_root: str = "sessions", sid: Optional[str] = None):
        sid = sid or time.strftime("%Y%m%d_%H%M%S")
        self.dir = Path(sessions_root) / sid
        self.images = self.dir / "images"
        self.images.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.dir / "queries.jsonl"
        (self.dir / "meta.json").write_text(
            json.dumps({"sid": sid, "source": "esp32_runtime", "created": time.time()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
        self._qid = 0
        print(f"[SESSION] recording to {self.dir}")

    def record(self, decision, text: str = "") -> None:
        """存一轮漏斗决策：整簇帧落盘 + queries.jsonl append 一条。

        decision: FunnelDecision（含 frames / best / reason / hint /
                  best_index / af_triggered / af_ok / timings）
        text:     可选，-o 无 ASR query 文本，留空；如上层有 ASR 结果可传入。
        """
        self._qid += 1
        qid = self._qid
        frame_names = []
        frames = decision.frames or ([] if decision.best is None else [decision.best])
        for i, jpg in enumerate(frames):
            name = f"q{qid:03d}_f{i:02d}.jpg"
            try:
                (self.images / name).write_bytes(jpg)
                frame_names.append(name)
            except Exception:
                pass

        rec = {
            # -v 原字段（兼容 -v 工具链）
            "qid": qid,
            "ts": time.time(),
            "text": text,
            "frames": frame_names,
            # 漏斗判定字段
            "send": bool(decision.send),
            "reason": decision.reason,
            "hint": decision.hint,
            "best_index": decision.best_index,
            "af_triggered": decision.af_triggered,
            "af_ok": decision.af_ok,
            "timings": decision.timings,
        }
        try:
            with self.jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def close(self) -> None:
        print(f"[SESSION] recorded {self._qid} queries -> {self.dir}")
