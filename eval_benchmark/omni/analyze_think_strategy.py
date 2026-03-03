"""
Experiment C: Think switch strategy analysis.

Reads existing minicpmo_full.csv (Think OFF) and minicpmo_thinking.csv
(Think ON) from runs/, groups by task type, and simulates an adaptive
strategy that uses Think OFF for urgent tasks and Think ON for reasoning tasks.

Usage (from eval_benchmark parent dir):
  python -m eval_benchmark.omni.analyze_think_strategy [--runs_dir ...]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List, Tuple

import numpy as np


def _percentile(arr: List[float], p: float) -> float:
    if not arr:
        return 0.0
    s = sorted(arr)
    idx = int(len(s) * p / 100.0)
    idx = min(idx, len(s) - 1)
    return s[idx]


def _load_run_csv(path: str) -> List[Dict[str, Any]]:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            for k in ("ttft_ms", "ttfa_ms", "e2e_ms", "quality_score"):
                if k in row and row[k]:
                    try:
                        row[k] = float(row[k])
                    except (ValueError, TypeError):
                        row[k] = 0.0
            for k in ("is_success", "is_abstain", "is_highconf_error"):
                if k in row and row[k]:
                    try:
                        row[k] = int(float(row[k]))
                    except (ValueError, TypeError):
                        row[k] = 0
            rows.append(row)
    return rows


def _task_prefix(task: str) -> str:
    """T1_v3 -> T1, T3A_v5 -> T3A, etc."""
    if not task:
        return "unknown"
    # task column already has just "T1", "T2", etc.
    return task.split("_")[0]


def _summarize(rows: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    """Compute summary stats for a group of rows."""
    n = len(rows)
    if n == 0:
        return {"label": label, "n": 0}
    ttfts = [r["ttft_ms"] for r in rows if r.get("ttft_ms", 0) > 0]
    ttfas = [r["ttfa_ms"] for r in rows if r.get("ttfa_ms", 0) > 0]
    e2es = [r["e2e_ms"] for r in rows if r.get("e2e_ms", 0) > 0]
    quals = [r["quality_score"] for r in rows]
    successes = [r.get("is_success", 0) for r in rows]
    abstains = [r.get("is_abstain", 0) for r in rows]
    hces = [r.get("is_highconf_error", 0) for r in rows]

    return {
        "label": label,
        "n": n,
        "ttft_p50": _percentile(ttfts, 50) if ttfts else 0,
        "ttft_p95": _percentile(ttfts, 95) if ttfts else 0,
        "ttfa_p50": _percentile(ttfas, 50) if ttfas else 0,
        "ttfa_p95": _percentile(ttfas, 95) if ttfas else 0,
        "e2e_p50": _percentile(e2es, 50) if e2es else 0,
        "e2e_p95": _percentile(e2es, 95) if e2es else 0,
        "quality_mean": sum(quals) / n if n else 0,
        "success_rate": sum(successes) / n if n else 0,
        "abstain_rate": sum(abstains) / n if n else 0,
        "hce_rate": sum(hces) / n if n else 0,
    }


# Adaptive strategy: use Think OFF for urgent tasks, Think ON for reasoning
URGENT_TASKS = {"T1", "T2", "H1"}
REASONING_TASKS = {"T3A", "T3B"}


def main():
    parser = argparse.ArgumentParser(description="Think switch strategy analysis")
    parser.add_argument("--runs_dir", default=os.path.join(
        os.path.dirname(__file__), "..", "runs"))
    parser.add_argument("--out_csv", default=os.path.join(
        os.path.dirname(__file__), "..", "runs", "think_strategy_analysis.csv"))
    parser.add_argument("--out_pertask_csv", default=os.path.join(
        os.path.dirname(__file__), "..", "runs", "think_strategy_pertask.csv"))
    args = parser.parse_args()

    off_path = os.path.join(args.runs_dir, "minicpmo_full.csv")
    on_path = os.path.join(args.runs_dir, "minicpmo_thinking.csv")

    if not os.path.exists(off_path):
        print(f"ERROR: {off_path} not found. Run minicpmo_full experiment first.")
        sys.exit(1)
    if not os.path.exists(on_path):
        print(f"ERROR: {on_path} not found. Run minicpmo_thinking experiment first.")
        sys.exit(1)

    off_rows = _load_run_csv(off_path)
    on_rows = _load_run_csv(on_path)

    # index by sample_id for pairing
    off_by_id = {r["sample_id"]: r for r in off_rows}
    on_by_id = {r["sample_id"]: r for r in on_rows}
    common_ids = sorted(set(off_by_id.keys()) & set(on_by_id.keys()))

    print(f"Loaded {len(off_rows)} Think-OFF rows, {len(on_rows)} Think-ON rows")
    print(f"Common sample_ids: {len(common_ids)}")

    # Group by task type
    task_groups: Dict[str, List[str]] = defaultdict(list)
    for sid in common_ids:
        task = _task_prefix(off_by_id[sid].get("task", ""))
        task_groups[task].append(sid)

    # Per-task comparison
    print(f"\n{'='*80}")
    print(f"  Per-task Think ON vs OFF comparison")
    print(f"{'='*80}")

    pertask_rows: List[Dict[str, Any]] = []

    for task in sorted(task_groups.keys()):
        sids = task_groups[task]
        off_group = [off_by_id[s] for s in sids]
        on_group = [on_by_id[s] for s in sids]

        off_stats = _summarize(off_group, f"{task}_OFF")
        on_stats = _summarize(on_group, f"{task}_ON")

        quality_delta = on_stats["quality_mean"] - off_stats["quality_mean"]
        e2e_penalty_pct = (
            (on_stats["e2e_p50"] / off_stats["e2e_p50"] - 1) * 100
            if off_stats["e2e_p50"] > 0 else 0
        )

        print(f"\n  Task {task} (n={len(sids)}):")
        print(f"    OFF: quality={off_stats['quality_mean']:.3f} "
              f"success={off_stats['success_rate']:.1%} "
              f"E2E_p50={off_stats['e2e_p50']:.0f}ms "
              f"HCE={off_stats['hce_rate']:.1%}")
        print(f"    ON:  quality={on_stats['quality_mean']:.3f} "
              f"success={on_stats['success_rate']:.1%} "
              f"E2E_p50={on_stats['e2e_p50']:.0f}ms "
              f"HCE={on_stats['hce_rate']:.1%}")
        print(f"    Delta: quality={quality_delta:+.3f}  "
              f"E2E penalty={e2e_penalty_pct:+.1f}%")

        pertask_rows.append({
            "task_type": task,
            "n": len(sids),
            "off_quality": round(off_stats["quality_mean"], 4),
            "on_quality": round(on_stats["quality_mean"], 4),
            "quality_delta": round(quality_delta, 4),
            "off_e2e_p50": round(off_stats["e2e_p50"], 1),
            "on_e2e_p50": round(on_stats["e2e_p50"], 1),
            "e2e_penalty_pct": round(e2e_penalty_pct, 1),
            "off_success": round(off_stats["success_rate"], 4),
            "on_success": round(on_stats["success_rate"], 4),
            "off_hce": round(off_stats["hce_rate"], 4),
            "on_hce": round(on_stats["hce_rate"], 4),
            "strategy": "urgent" if task in URGENT_TASKS else "reasoning",
        })

    # Adaptive strategy simulation
    adaptive_rows = []
    for sid in common_ids:
        task = _task_prefix(off_by_id[sid].get("task", ""))
        if task in URGENT_TASKS:
            adaptive_rows.append(off_by_id[sid])
        else:
            adaptive_rows.append(on_by_id[sid])

    all_off = [off_by_id[s] for s in common_ids]
    all_on = [on_by_id[s] for s in common_ids]

    off_total = _summarize(all_off, "All_OFF")
    on_total = _summarize(all_on, "All_ON")
    adaptive_total = _summarize(adaptive_rows, "Adaptive")

    print(f"\n{'='*80}")
    print(f"  Strategy Comparison (all {len(common_ids)} samples)")
    print(f"{'='*80}")

    strategy_rows = []
    for label, stats in [("Think_OFF", off_total), ("Think_ON", on_total), ("Adaptive", adaptive_total)]:
        print(f"\n  {label}:")
        print(f"    quality={stats['quality_mean']:.3f} "
              f"success={stats['success_rate']:.1%} "
              f"abstain={stats['abstain_rate']:.1%} "
              f"HCE={stats['hce_rate']:.1%}")
        print(f"    TTFT_p50={stats['ttft_p50']:.0f}ms "
              f"TTFA_p50={stats['ttfa_p50']:.0f}ms "
              f"E2E_p50={stats['e2e_p50']:.0f}ms "
              f"E2E_p95={stats['e2e_p95']:.0f}ms")

        strategy_rows.append({
            "strategy": label,
            "n": stats["n"],
            "quality_mean": round(stats["quality_mean"], 4),
            "success_rate": round(stats["success_rate"], 4),
            "abstain_rate": round(stats["abstain_rate"], 4),
            "hce_rate": round(stats["hce_rate"], 4),
            "ttft_p50": round(stats["ttft_p50"], 1),
            "ttfa_p50": round(stats["ttfa_p50"], 1),
            "e2e_p50": round(stats["e2e_p50"], 1),
            "e2e_p95": round(stats["e2e_p95"], 1),
        })

    adaptive_gain = adaptive_total["quality_mean"] - off_total["quality_mean"]
    adaptive_overhead = (
        (adaptive_total["e2e_p50"] / off_total["e2e_p50"] - 1) * 100
        if off_total["e2e_p50"] > 0 else 0
    )
    print(f"\n  Adaptive vs All-OFF: quality gain={adaptive_gain:+.3f}, "
          f"E2E overhead={adaptive_overhead:+.1f}%")

    # write CSVs
    os.makedirs(os.path.dirname(args.out_csv), exist_ok=True)

    with open(args.out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(strategy_rows[0].keys()))
        writer.writeheader()
        writer.writerows(strategy_rows)
    print(f"\nWrote strategy comparison to {args.out_csv}")

    with open(args.out_pertask_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(pertask_rows[0].keys()))
        writer.writeheader()
        writer.writerows(pertask_rows)
    print(f"Wrote per-task analysis to {args.out_pertask_csv}")


if __name__ == "__main__":
    main()
