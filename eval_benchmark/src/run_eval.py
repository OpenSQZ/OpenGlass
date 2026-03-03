from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any, Dict

import pandas as pd
import yaml

from .manifest import load_manifest
from .adapter import infer_one
from .rubric import score_sample


def _load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--runs_dir", default="eval_benchmark/runs")
    args = ap.parse_args()

    cfg = _load_yaml(args.config)
    name = cfg["name"]
    manifest_path = cfg["manifest_path"]
    n_samples = cfg.get("n_samples")
    seed = int(cfg.get("seed", 0))

    # Resolve manifest path: if relative, try multiple candidate locations
    if not os.path.isabs(manifest_path):
        # Candidates: relative to config file, cwd, eval_benchmark dir
        config_dir = os.path.dirname(os.path.abspath(args.config))
        candidates = [
            manifest_path,  # cwd
            os.path.join(config_dir, manifest_path),  # relative to config
            os.path.join("eval_benchmark", manifest_path),  # eval_benchmark dir
            os.path.join(os.path.dirname(config_dir), manifest_path),  # parent dir
        ]
        for candidate in candidates:
            if os.path.exists(candidate):
                manifest_path = candidate
                break
        else:
            raise FileNotFoundError(
                f"Cannot find manifest file: {manifest_path}\n"
                f"Tried: {candidates}\n"
                f"Please ensure file exists or use absolute path"
            )

    df = load_manifest(manifest_path, n_samples=n_samples, seed=seed)

    rows = []
    adapter_cfg = (cfg.get("adapter") or {})

    for i, r in df.iterrows():
        sample = r.to_dict()
        out = infer_one(sample, adapter_cfg)
        pred_text = out.get("pred_text", "")
        meta = out.get("meta_json", {}) or {}

        quality_score, is_success, is_abstain, is_highconf_error = score_sample(sample, pred_text)

        rows.append({
            "run_name": name,
            "sample_id": sample.get("sample_id", i),
            "task": sample.get("task", ""),
            "ttft_ms": out.get("ttft_ms"),
            "ttfa_ms": out.get("ttfa_ms"),
            "e2e_ms": out.get("e2e_ms"),
            "backend": meta.get("backend"),
            "vlm_total_ms": meta.get("vlm_total_ms"),
            "error": meta.get("error"),
            "image_read_ms": meta.get("image_read_ms"),
            "image_decode_ms": meta.get("image_decode_ms"),
            "image_resize_ms": meta.get("image_resize_ms"),
            "image_jpeg_ms": meta.get("image_jpeg_ms"),
            "image_b64_ms": meta.get("image_b64_ms"),
            "user_to_capture_ms": meta.get("user_to_capture_ms"),
            "user_to_pack_ms": meta.get("user_to_pack_ms"),
            "user_to_send_ms": meta.get("user_to_send_ms"),
            "token_to_audio_ms": meta.get("token_to_audio_ms"),
            "quality_score": quality_score,
            "is_success": is_success,
            "is_abstain": is_abstain,
            "is_highconf_error": is_highconf_error,
            "pred_text": pred_text,
            "meta_json": json.dumps(out.get("meta_json", {}), ensure_ascii=False),
            "bucket_main": cfg.get("bucket_main"),
            "bucket_ablation": cfg.get("bucket_ablation"),
        })

    os.makedirs(args.runs_dir, exist_ok=True)
    out_csv = os.path.join(args.runs_dir, f"{name}.csv")
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    print(f"Wrote: {out_csv} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
