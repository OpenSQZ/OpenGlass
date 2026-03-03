"""
One-click runner for all three omni-specific experiments.

Runs in order:
  C. Think strategy analysis (offline, no server needed)
  A. Barge-in experiment (needs llama.cpp server with MiniCPM-o)
  B. Multi-turn experiment (needs llama.cpp server with MiniCPM-o)
  Aggregate all results

Usage (from exp_opus/):
  python eval_benchmark/scripts/run_omni_experiments.py [--skip-server-tests]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time


def _run(cmd: list, cwd: str, label: str) -> int:
    print(f"\n{'='*70}")
    print(f"  [{label}] Running: {' '.join(cmd)}")
    print(f"{'='*70}\n")
    t0 = time.time()
    result = subprocess.run(cmd, cwd=cwd)
    elapsed = time.time() - t0
    status = "OK" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
    print(f"\n  [{label}] {status} in {elapsed:.1f}s\n")
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description="Run all omni experiments")
    parser.add_argument("--skip-server-tests", action="store_true",
                        help="Skip experiments A & B (require running llama.cpp server)")
    parser.add_argument("--skip-think-analysis", action="store_true",
                        help="Skip experiment C (think strategy, offline)")
    parser.add_argument("--skip-aggregate", action="store_true",
                        help="Skip final aggregation")
    args = parser.parse_args()

    # project root = eval_benchmark/..  (exp_opus/)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    benchmark_dir = os.path.dirname(script_dir)
    project_dir = os.path.dirname(benchmark_dir)

    python = sys.executable

    # --- C. Think strategy analysis (offline) ---
    if not args.skip_think_analysis:
        _run(
            [python, "-m", "eval_benchmark.omni.analyze_think_strategy"],
            cwd=project_dir,
            label="Exp C: Think Strategy",
        )

    # --- A. Barge-in experiment ---
    if not args.skip_server_tests:
        _run(
            [python, "-m", "eval_benchmark.omni.barge_in_harness"],
            cwd=project_dir,
            label="Exp A: Barge-in",
        )

    # --- B. Multi-turn experiment ---
    if not args.skip_server_tests:
        _run(
            [python, "-m", "eval_benchmark.omni.multiturn_harness"],
            cwd=project_dir,
            label="Exp B: Multi-turn",
        )

    # --- Aggregate ---
    if not args.skip_aggregate:
        _run(
            [python, "-m", "eval_benchmark.omni.aggregate_omni"],
            cwd=project_dir,
            label="Aggregate Omni",
        )

    print(f"\n{'='*70}")
    print(f"  All omni experiments complete!")
    print(f"{'='*70}")
    print(f"\nOutput files:")
    runs_dir = os.path.join(benchmark_dir, "runs")
    tables_dir = os.path.join(benchmark_dir, "tables")
    for d, label in [(runs_dir, "runs"), (tables_dir, "tables")]:
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if "barge_in" in f or "multiturn" in f or "think_strategy" in f or "table3" in f or "table4" in f or "table5" in f:
                    print(f"  {label}/{f}")


if __name__ == "__main__":
    main()
