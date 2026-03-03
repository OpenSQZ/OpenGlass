@echo off
REM Windows batch script: run only local experiments (skip Cloud API)

echo === Running local experiments (skip Cloud API) ===

echo 1/6: naive_on_device...
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/naive_on_device.yaml
if errorlevel 1 goto error

echo 2/6: ours_full...
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ours_full.yaml
if errorlevel 1 goto error

echo 3/6: ablation_wo_streaming...
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ablation_wo_streaming.yaml
if errorlevel 1 goto error

echo 4/6: ablation_wo_parallel_tts...
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ablation_wo_parallel_tts.yaml
if errorlevel 1 goto error

echo 5/6: ablation_wo_resize...
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ablation_wo_resize.yaml
if errorlevel 1 goto error

echo 6/6: ablation_wo_safety...
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ablation_wo_safety.yaml
if errorlevel 1 goto error

echo === Generating summary report ===
python -m eval_benchmark.src.aggregate --runs_dir eval_benchmark/runs --out_dir eval_benchmark
if errorlevel 1 goto error

echo === Done! Check eval_benchmark/tables/ and eval_benchmark/figures/ ===
goto end

:error
echo Error: A step failed, please check the error messages above
exit /b 1

:end
