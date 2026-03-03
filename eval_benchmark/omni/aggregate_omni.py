"""
Aggregate omni experiment results into summary tables.

Reads:
  - runs/barge_in_results.csv   -> table3_bargein.csv
  - runs/multiturn_results.csv  -> table4_multiturn.csv
  - runs/think_strategy_analysis.csv + think_strategy_pertask.csv -> table5_think_strategy.csv

Usage (from eval_benchmark parent dir):
  python -m eval_benchmark.omni.aggregate_omni [--runs_dir ...] [--out_dir ...]
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List


def _percentile(arr: List[float], p: float) -> float:
    if not arr:
        return 0.0
    s = sorted(arr)
    idx = int(len(s) * p / 100.0)
    return s[min(idx, len(s) - 1)]


def _load_csv(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def aggregate_bargein(runs_dir: str, out_dir: str):
    """Table 3: barge-in results grouped by interrupt_delay_ms."""
    path = os.path.join(runs_dir, "barge_in_results.csv")
    rows = _load_csv(path)
    if not rows:
        print(f"  [SKIP] {path} not found or empty")
        return

    groups: Dict[int, List[Dict]] = defaultdict(list)
    for r in rows:
        delay = int(_safe_float(r.get("interrupt_delay_ms", 0)))
        groups[delay].append(r)

    out_rows = []
    for delay in sorted(groups.keys()):
        g = groups[delay]
        n = len(g)
        successes = [r for r in g if str(r.get("barge_in_success", "")).lower() == "true"]
        success_rate = len(successes) / n if n else 0

        t_stops = [_safe_float(r["t_stop_ms"]) for r in successes if _safe_float(r.get("t_stop_ms", -1)) >= 0]
        t_resumes = [_safe_float(r["t_resume_ms"]) for r in successes if _safe_float(r.get("t_resume_ms", -1)) > 0]
        carry_overs = [r for r in successes if str(r.get("carry_over_error", "")).lower() == "true"]

        out_rows.append({
            "interrupt_delay_ms": delay,
            "n": n,
            "success_rate": round(success_rate, 4),
            "t_stop_p50": round(_percentile(t_stops, 50), 1),
            "t_stop_p95": round(_percentile(t_stops, 95), 1),
            "t_resume_p50": round(_percentile(t_resumes, 50), 1),
            "t_resume_p95": round(_percentile(t_resumes, 95), 1),
            "carry_over_rate": round(len(carry_overs) / max(len(successes), 1), 4),
        })

    # also add an "all" row
    all_successes = [r for r in rows if str(r.get("barge_in_success", "")).lower() == "true"]
    all_t_stops = [_safe_float(r["t_stop_ms"]) for r in all_successes if _safe_float(r.get("t_stop_ms", -1)) >= 0]
    all_t_resumes = [_safe_float(r["t_resume_ms"]) for r in all_successes if _safe_float(r.get("t_resume_ms", -1)) > 0]
    all_carry = [r for r in all_successes if str(r.get("carry_over_error", "")).lower() == "true"]

    out_rows.append({
        "interrupt_delay_ms": "all",
        "n": len(rows),
        "success_rate": round(len(all_successes) / max(len(rows), 1), 4),
        "t_stop_p50": round(_percentile(all_t_stops, 50), 1),
        "t_stop_p95": round(_percentile(all_t_stops, 95), 1),
        "t_resume_p50": round(_percentile(all_t_resumes, 50), 1),
        "t_resume_p95": round(_percentile(all_t_resumes, 95), 1),
        "carry_over_rate": round(len(all_carry) / max(len(all_successes), 1), 4),
    })

    tables_dir = os.path.join(out_dir, "tables")
    os.makedirs(tables_dir, exist_ok=True)
    out_path = os.path.join(tables_dir, "table3_bargein.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        writer.writeheader()
        writer.writerows(out_rows)
    print(f"  Wrote: {out_path}")


def aggregate_multiturn(runs_dir: str, out_dir: str):
    """Table 4: multiturn results grouped by turn_idx."""
    path = os.path.join(runs_dir, "multiturn_results.csv")
    rows = _load_csv(path)
    if not rows:
        print(f"  [SKIP] {path} not found or empty")
        return

    groups: Dict[int, List[Dict]] = defaultdict(list)
    for r in rows:
        tidx = int(_safe_float(r.get("turn_idx", 0)))
        groups[tidx].append(r)

    out_rows = []
    for tidx in sorted(groups.keys()):
        g = groups[tidx]
        n = len(g)
        ttfts = [_safe_float(r["ttft_ms"]) for r in g if _safe_float(r.get("ttft_ms", -1)) > 0]
        ttfas = [_safe_float(r["ttfa_ms"]) for r in g if _safe_float(r.get("ttfa_ms", -1)) > 0]
        e2es = [_safe_float(r["e2e_ms"]) for r in g if _safe_float(r.get("e2e_ms", 0)) > 0]
        confused = [r for r in g if str(r.get("state_confusion", "")).lower() == "true"]

        out_rows.append({
            "turn_idx": tidx,
            "n": n,
            "ttft_p50": round(_percentile(ttfts, 50), 1),
            "ttft_p95": round(_percentile(ttfts, 95), 1),
            "ttfa_p50": round(_percentile(ttfas, 50), 1),
            "ttfa_p95": round(_percentile(ttfas, 95), 1),
            "e2e_p50": round(_percentile(e2es, 50), 1),
            "e2e_p95": round(_percentile(e2es, 95), 1),
            "state_confusion_rate": round(len(confused) / max(n, 1), 4),
        })

    tables_dir = os.path.join(out_dir, "tables")
    os.makedirs(tables_dir, exist_ok=True)
    out_path = os.path.join(tables_dir, "table4_multiturn.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        writer.writeheader()
        writer.writerows(out_rows)
    print(f"  Wrote: {out_path}")


def aggregate_think_strategy(runs_dir: str, out_dir: str):
    """Table 5: copy think strategy CSVs to tables dir for convenience."""
    tables_dir = os.path.join(out_dir, "tables")
    os.makedirs(tables_dir, exist_ok=True)

    src1 = os.path.join(runs_dir, "think_strategy_analysis.csv")
    src2 = os.path.join(runs_dir, "think_strategy_pertask.csv")

    for src, dst_name in [(src1, "table5_think_strategy.csv"), (src2, "table5_think_pertask.csv")]:
        if os.path.exists(src):
            rows = _load_csv(src)
            if rows:
                out_path = os.path.join(tables_dir, dst_name)
                with open(out_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                    writer.writeheader()
                    writer.writerows(rows)
                print(f"  Wrote: {out_path}")
        else:
            print(f"  [SKIP] {src} not found")


def main():
    parser = argparse.ArgumentParser(description="Aggregate omni experiment results")
    parser.add_argument("--runs_dir", default=os.path.join(
        os.path.dirname(__file__), "..", "runs"))
    parser.add_argument("--out_dir", default=os.path.join(
        os.path.dirname(__file__), ".."))
    args = parser.parse_args()

    print("=== Aggregating Omni Experiment Results ===")
    aggregate_bargein(args.runs_dir, args.out_dir)
    aggregate_multiturn(args.runs_dir, args.out_dir)
    aggregate_think_strategy(args.runs_dir, args.out_dir)
    print("\nDone.")


if __name__ == "__main__":
    main()
