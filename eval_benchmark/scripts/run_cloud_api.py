#!/usr/bin/env python3
"""
Run cloud API experiments (Gemini 3 Flash or GPT-5.2)

Usage:
1. Set environment variables:
   - Gemini: set GOOGLE_API_KEY=xxx
   - GPT-5.2: set OPENAI_API_KEY=sk-xxx

2. Run:
   python eval_benchmark/scripts/run_cloud_api.py --provider gemini
   Or
   python eval_benchmark/scripts/run_cloud_api.py --provider gpt5

Note: Cloud APIs incur costs, please confirm before running!
"""
import os
import subprocess
import sys
import argparse

# Ensure execution in project root directory
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.chdir(project_root)

def check_api_key(provider: str) -> bool:
    """Check if API key is set"""
    if provider in ("gemini", "gemini25"):
        key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not key:
            print("❌ Error: GOOGLE_API_KEY environment variable not set")
            print("   Please run: set GOOGLE_API_KEY=your_api_key")
            print("   Get key at: https://aistudio.google.com/")
            return False
    elif provider in ("gpt5", "gpt4o"):
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            print("❌ Error: OPENAI_API_KEY environment variable not set")
            print("   Please run: set OPENAI_API_KEY=sk-xxx")
            print("   Get key at: https://platform.openai.com/api-keys")
            return False
    elif provider == "qwen":
        key = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY")
        if not key:
            print("❌ Error: DASHSCOPE_API_KEY environment variable not set")
            print("   Please run: set DASHSCOPE_API_KEY=sk-xxx")
            print("   Get key at: https://dashscope.console.aliyun.com/")
            return False
    return True


def estimate_cost(provider: str, n_samples: int = 121) -> str:
    """Estimate cost"""
    # Assume ~1000 input tokens per image, 80 output tokens
    input_tokens_per_sample = 1000
    output_tokens_per_sample = 80
    
    total_input = n_samples * input_tokens_per_sample
    total_output = n_samples * output_tokens_per_sample
    
    if provider == "gemini":
        # Gemini 2.0 Flash: ~$0.075/1M input, $0.30/1M output (may be lower within free tier)
        cost = (total_input / 1_000_000 * 0.075) + (total_output / 1_000_000 * 0.30)
        return f"Gemini 2.0 Flash estimated cost: ~${cost:.3f} ({n_samples} samples)"
    elif provider == "gemini25":
        # Gemini 2.5 Flash: ~$0.15/1M input, $0.60/1M output
        cost = (total_input / 1_000_000 * 0.15) + (total_output / 1_000_000 * 0.60)
        return f"Gemini 2.5 Flash estimated cost: ~${cost:.3f} ({n_samples} samples) ⭐Latest"
    elif provider == "gpt5":
        # GPT-5.2: $1.75/1M input, $14/1M output
        cost = (total_input / 1_000_000 * 1.75) + (total_output / 1_000_000 * 14)
        return f"GPT-5.2 estimated cost: ~${cost:.3f} ({n_samples} samples)"
    elif provider == "gpt4o":
        # GPT-4o: $5/1M input, $15/1M output
        cost = (total_input / 1_000_000 * 5) + (total_output / 1_000_000 * 15)
        return f"GPT-4o estimated cost: ~${cost:.2f} ({n_samples} samples)"
    elif provider == "qwen":
        # Qwen-VL-Max: ¥0.02/thousand tokens (~$0.003/thousand tokens)
        cost_rmb = (total_input + total_output) / 1000 * 0.02
        return f"Qwen-VL-Max estimated cost: ~¥{cost_rmb:.2f} ({n_samples} samples) ⭐No VPN needed in China"
    return ""


def main():
    parser = argparse.ArgumentParser(description="Run cloud API experiments")
    parser.add_argument(
        "--provider",
        choices=["gemini", "gemini25", "gpt5", "gpt4o", "qwen", "both"],
        default="qwen",
        help="Cloud API: qwen (no VPN needed in China), gemini/gemini25, gpt5/gpt4o, both (default: qwen)"
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompt"
    )
    args = parser.parse_args()
    
    print("=" * 60)
    print("Run cloud API experiments (2026 latest models)")
    print("=" * 60)
    
    providers = []
    if args.provider == "both":
        providers = ["gemini25", "gpt5"]  # Default to latest gemini25
    else:
        providers = [args.provider]
    
    # Check API key
    for provider in providers:
        if not check_api_key(provider):
            sys.exit(1)
    
    # Show cost estimation
    print("\n💰 Cost estimation (Jan 2026 pricing):")
    for provider in providers:
        print(f"   {estimate_cost(provider)}")
    
    if not args.yes:
        print("\n⚠️  Cloud APIs incur costs!")
        confirm = input("Confirm run? (y/N): ")
        if confirm.lower() != 'y':
            print("Cancelled")
            sys.exit(0)
    
    print()
    
    PYTHON_CMD = sys.executable
    CONFIGS_DIR = "eval_benchmark/configs"
    RUN_EVAL_SCRIPT = "eval_benchmark.src.run_eval"
    AGGREGATE_SCRIPT = "eval_benchmark.src.aggregate"
    RUNS_DIR = "eval_benchmark/runs"
    OUT_DIR = "eval_benchmark"
    
    config_map = {
        "qwen": "cloud_qwen.yaml",          # Alibaba Cloud Qwen-VL (China, recommended)
        "gemini": "cloud_gemini.yaml",      # Gemini 2.0 Flash
        "gemini25": "cloud_gemini25.yaml",  # Gemini 2.5 Flash
        "gpt5": "cloud_gpt5.yaml",          # GPT-5.2
        "gpt4o": "cloud_gpt4o.yaml",        # GPT-4o (legacy)
    }
    
    for i, provider in enumerate(providers):
        config_file = config_map[provider]
        print(f"{i+1}/{len(providers)}: {config_file}...")
        print("=" * 60)
        print(f"Execute: {PYTHON_CMD} -m {RUN_EVAL_SCRIPT} --config {CONFIGS_DIR}/{config_file}")
        print("=" * 60)
        
        command = [
            PYTHON_CMD,
            "-m", RUN_EVAL_SCRIPT,
            "--config", os.path.join(CONFIGS_DIR, config_file)
        ]
        subprocess.run(command, check=True)
        print()
    
    # Regenerate summary report
    print("\nGenerating summary report...")
    print("=" * 60)
    print(f"Execute: {PYTHON_CMD} -m {AGGREGATE_SCRIPT} --runs_dir {RUNS_DIR} --out_dir {OUT_DIR}")
    print("=" * 60)
    
    command = [
        PYTHON_CMD,
        "-m", AGGREGATE_SCRIPT,
        "--runs_dir", RUNS_DIR,
        "--out_dir", OUT_DIR
    ]
    subprocess.run(command, check=True)
    
    print("\n" + "=" * 60)
    print("✅ Done!")
    print("=" * 60)
    print("\nCheck output:")
    print(f"  - {RUNS_DIR}/cloud_*.csv")
    print(f"  - {OUT_DIR}/tables/table1_main.csv")
    print(f"  - {OUT_DIR}/figures/figure1_pareto.png")


if __name__ == "__main__":
    main()
