# eval_benchmark (ACL Demo Track eval skeleton)

This folder provides a **reproducible evaluation interface** for the AI glasses demo:

- Input: a manifest CSV (e.g. `manifest_nlp_v6_mixedbest.csv`)
- Output: per-run CSV under `runs/`
- Auto reports:
  - `tables/table1_main.csv` (Cloud / Naive / Ours)
  - `tables/table2_ablation.csv` (Ours ablations)
  - `figures/figure1_pareto.png` (Quality vs Latency)

## Quick start

1) Install deps

```bash
pip install -r requirements.txt
```

2) Create / edit configs in `configs/`.

3) Run one config

```bash
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ours_full.yaml
```

4) Aggregate reports (reads all `runs/*.csv`)

```bash
python -m eval_benchmark.src.aggregate --runs_dir eval_benchmark/runs --out_dir eval_benchmark
```

## How to integrate your real inference

Edit `src/adapter.py`:

- Implement `infer_one(sample: dict, cfg: dict) -> dict` returning:
  - `pred_text` (str)
  - `ttft_ms` (float)
  - `ttfa_ms` (float)
  - `e2e_ms` (float)
  - optional: `meta_json` (json-serializable)

This skeleton intentionally keeps the adapter thin so you can plug in:
- llama.cpp streaming text
- streaming TTS
- optional Wi-Fi capture timing (v6)

## Rubric scoring

Implement your rubric in `src/rubric.py`:

- `score_sample(sample, pred_text) -> (quality_score, is_success, is_abstain, is_highconf_error)`

## Usage

```
# Run all experiments
python eval_benchmark/scripts/run_local_only.py

# Run Wi-Fi E2E experiments (if ESP32 camera is available)
python eval_benchmark/scripts/run_wifi_e2e.py --camera_url http://10.100.6.79/capture

# Manual aggregation
python -m eval_benchmark.src.aggregate --runs_dir eval_benchmark/runs --out_dir eval_benchmark
```

Expected output structure:
```
eval_benchmark/
├── tables/
│   ├── table1_main.csv       ✅ Main experiments (Cloud/Naive/Ours)
│   ├── table2_ablation.csv   ✅ Ablation studies
│   └── table3_wifi_e2e.csv   ✅ Wi-Fi E2E breakdown
├── figures/
│   └── figure1_pareto.png    ✅ Pareto curve
└── runs/
    ├── wifi_e2e_raw.csv      ✅ Wi-Fi raw mode detailed data
    └── wifi_e2e_resize.csv   ✅ Wi-Fi resize mode detailed data
```