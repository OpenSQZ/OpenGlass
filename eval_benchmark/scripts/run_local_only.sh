#!/usr/bin/env bash
set -e

# Run only local experiments (skip cloud_api to avoid charges)
echo "=== Running local experiments (skip Cloud API) ==="

# Run main baselines (local)
echo "1/5: naive_on_device..."
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/naive_on_device.yaml

echo "2/5: ours_full..."
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ours_full.yaml

# Run ablations
echo "3/5: ablation_wo_streaming..."
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ablation_wo_streaming.yaml

echo "4/5: ablation_wo_parallel_tts..."
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ablation_wo_parallel_tts.yaml

echo "5/5: ablation_wo_resize..."
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ablation_wo_resize.yaml

echo "6/6: ablation_wo_safety..."
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ablation_wo_safety.yaml

# Aggregate
echo "=== Generating summary report ==="
python -m eval_benchmark.src.aggregate --runs_dir eval_benchmark/runs --out_dir eval_benchmark

echo "=== Done! Check eval_benchmark/tables/ and eval_benchmark/figures/ ==="
