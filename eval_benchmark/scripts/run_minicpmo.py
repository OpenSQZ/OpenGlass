#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
One-click runner for MiniCPM-o evaluation experiments
Compare latency and quality metrics with MiniCPM-V (ours_full)
"""

import os
import sys
import subprocess

# Bypass VPN proxy to ensure local llama.cpp server requests do not go through proxy
os.environ["NO_PROXY"] = "127.0.0.1,localhost,0.0.0.0"
os.environ["no_proxy"] = "127.0.0.1,localhost,0.0.0.0"
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)


def run_cmd(cmd):
    """Run command and print output"""
    print(f"\n{'='*60}")
    print(f"Executing: {cmd}")
    print('='*60)
    result = subprocess.run(cmd, shell=True, check=False)
    if result.returncode != 0:
        print(f"\n❌ Error: Command failed (exit code {result.returncode})")
        sys.exit(1)
    return result


def main():
    print("="*60)
    print("MiniCPM-o Evaluation Experiment")
    print("="*60)
    print("\n⚠️  Please ensure MiniCPM-o llama.cpp server is running:")
    print("   llama-server.exe -m MiniCPM-o-4_5-Q4_K_M.gguf ...")
    print("   Port: 8080")
    print("\n⚠️  Proxy bypass set: NO_PROXY=127.0.0.1,localhost,0.0.0.0")
    print("="*60)

    # ---------------------------------------------------------------
    # MiniCPM-o experiments (configs aligned with MiniCPM-V ours_full)
    # ---------------------------------------------------------------
    configs = [
        ("1/2", "minicpmo_full"),       # thinking=false, fair latency comparison
        ("2/2", "minicpmo_thinking"),    # thinking=true, quality ceiling comparison
    ]

    for step, name in configs:
        print(f"\n{step}: {name}...")
        cmd = f'python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/{name}.yaml'
        run_cmd(cmd)

    # ---------------------------------------------------------------
    # Summary: Merge results of MiniCPM-V and MiniCPM-o
    # ---------------------------------------------------------------
    print("\n" + "="*60)
    print("Generating summary report (including MiniCPM-V + MiniCPM-o comparison)...")
    print("="*60)
    run_cmd('python -m eval_benchmark.src.aggregate --runs_dir eval_benchmark/runs --out_dir eval_benchmark')

    print("\n" + "="*60)
    print("✅ Done!")
    print("="*60)
    print("\nCheck output:")
    print("  - eval_benchmark/runs/minicpmo_full.csv")
    print("  - eval_benchmark/runs/minicpmo_thinking.csv")
    print("  - eval_benchmark/tables/table1_main.csv    ← Incl. Ours vs Ours-O comparison")
    print("  - eval_benchmark/tables/table2_ablation.csv ← Incl. Full-O / Full-O-Think")
    print("  - eval_benchmark/figures/figure1_pareto.png ← Pareto chart")
    print("="*60)
    print("\n💡 Tip: To run a specific config separately:")
    print("   python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/minicpmo_full.yaml")
    print("   python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/minicpmo_thinking.yaml")


if __name__ == "__main__":
    main()
