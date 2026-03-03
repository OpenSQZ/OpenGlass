#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
One-click runner for all local experiments (skip Cloud API)
Works in Anaconda Prompt or any Python environment
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
    print(f"Execute: {cmd}")
    print('='*60)
    result = subprocess.run(cmd, shell=True, check=False)
    if result.returncode != 0:
        print(f"\n❌ Error: Command failed (exit code {result.returncode})")
        sys.exit(1)
    return result

def main():
    print("="*60)
    print("Running local experiments (skip Cloud API)")
    print("="*60)
    print("\n⚠️  Please ensure llama.cpp server is running:")
    print("   llama-server.exe -m <model> --mmproj <mmproj> --port 8080")
    print("\n⚠️  Proxy bypass set: NO_PROXY=127.0.0.1,localhost,0.0.0.0")
    print("="*60)
    
    configs = [
        ("1/6", "naive_on_device"),
        ("2/6", "ours_full"),
        ("3/6", "ablation_wo_streaming"),
        ("4/6", "ablation_wo_parallel_tts"),
        ("5/6", "ablation_wo_resize"),
        ("6/6", "ablation_wo_safety"),
    ]
    
    for step, name in configs:
        print(f"\n{step}: {name}...")
        cmd = f'python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/{name}.yaml'
        run_cmd(cmd)
    
    print("\n" + "="*60)
    print("Generating summary report...")
    print("="*60)
    run_cmd('python -m eval_benchmark.src.aggregate --runs_dir eval_benchmark/runs --out_dir eval_benchmark')
    
    print("\n" + "="*60)
    print("✅ Done!")
    print("="*60)
    print("\nCheck output:")
    print("  - eval_benchmark/runs/*.csv")
    print("  - eval_benchmark/tables/table1_main.csv")
    print("  - eval_benchmark/tables/table2_ablation.csv")
    print("  - eval_benchmark/figures/figure1_pareto.png")
    print("="*60)

if __name__ == "__main__":
    main()
