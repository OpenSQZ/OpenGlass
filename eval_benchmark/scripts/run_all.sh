#!/usr/bin/env bash
set -e

# Run main baselines
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/cloud_api.yaml
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/naive_on_device.yaml
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ours_full.yaml

# Run ablations
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ablation_wo_streaming.yaml
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ablation_wo_parallel_tts.yaml
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ablation_wo_resize.yaml
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ablation_wo_safety.yaml

# Aggregate
python -m eval_benchmark.src.aggregate --runs_dir eval_benchmark/runs --out_dir eval_benchmark
