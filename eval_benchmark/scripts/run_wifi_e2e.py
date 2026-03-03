#!/usr/bin/env python3
"""
Run Wi-Fi E2E breakdown experiment (Table 3)

Used to prove that the glasses end-to-end pipeline remains <2s under real capture conditions,
and to quantify capture+pack overhead.

Usage:
1. Ensure ESP32 CameraWebServer is running
2. Ensure llama.cpp server is running
3. Run:
   python eval_benchmark/scripts/run_wifi_e2e.py --camera_url http://192.168.4.1/capture

Output:
- eval_benchmark/runs/wifi_e2e_raw.csv (raw mode)
- eval_benchmark/runs/wifi_e2e_resize.csv (resize mode)
- eval_benchmark/tables/table3_wifi_e2e.csv (summary)
"""
import os
import sys
import argparse
import subprocess
from pathlib import Path

import pandas as pd
import numpy as np

# Ensure execution in project root directory
project_root = Path(__file__).resolve().parent.parent.parent
os.chdir(project_root)


def run_experiment(
    mode: str,
    camera_url: str,
    n_samples: int,
    use_camera: bool = True,
    manifest: str = "manifest_nlp_v6_mixedbest.csv"
):
    """Run a single experiment"""
    script = "02_batch_infer_stream_tts_realtime_eval_v6_wifi_capture.py"
    script_path = project_root / "eval_benchmark" / script
    
    if not script_path.exists():
        print(f"❌ Script not found: {script_path}")
        return None
    
    out_name = f"wifi_e2e_{mode}" if use_camera else f"disk_e2e_{mode}"
    out_csv = project_root / "eval_benchmark" / "runs" / f"{out_name}.csv"
    out_log = project_root / "eval_benchmark" / "runs" / f"{out_name}_log.txt"
    
    # Build command
    cmd = [
        sys.executable,
        str(script_path),
        "--manifest", str(project_root / "eval_benchmark" / manifest),
        "--out", str(out_csv),
        "--log", str(out_log),
        "--pack_mode", mode,
        "--use_camera", "1" if use_camera else "0",
    ]
    
    if use_camera:
        cmd.extend(["--camera_url", camera_url])
    
    # If only partial samples needed, modify manifest or add parameters
    # v6 script runs all by default, here we use first n_samples
    
    print(f"\n{'='*60}")
    print(f"Running experiment: {out_name}")
    print(f"Mode: {'Wi-Fi live capture' if use_camera else 'Disk playback'}")
    print(f"pack_mode: {mode}")
    print(f"Output: {out_csv}")
    print(f"{'='*60}")
    
    try:
        result = subprocess.run(cmd, check=True, cwd=str(project_root / "eval_benchmark"))
        return out_csv
    except subprocess.CalledProcessError as e:
        print(f"❌ Experiment failed: {e}")
        return None


def analyze_results(csv_path: Path) -> dict:
    """Analyze single experiment results"""
    if not csv_path.exists():
        return {}
    
    df = pd.read_csv(csv_path)
    
    # Key metrics
    metrics = {}
    
    for col in ["user_to_capture_ms", "user_to_pack_ms", "ttft_send_to_token_ms", 
                "user_to_token_ms", "user_to_audio_ms", "token_to_audio_ms"]:
        if col in df.columns:
            vals = df[col].dropna()
            if len(vals) > 0:
                metrics[f"{col}_p50"] = vals.quantile(0.5)
                metrics[f"{col}_p90"] = vals.quantile(0.9)
                metrics[f"{col}_min"] = vals.min()
                metrics[f"{col}_max"] = vals.max()
                metrics[f"{col}_std"] = vals.std()
    
    # Count PASS/FAIL
    if "status" in df.columns:
        metrics["pass_rate"] = (df["status"] == "PASS(<2s)").mean()
        metrics["n_samples"] = len(df)
    
    return metrics


def generate_table3(results: dict, out_path: Path):
    """Generate Table 3: Wi-Fi E2E breakdown"""
    rows = []
    
    for name, metrics in results.items():
        if not metrics:
            continue
        
        row = {"baseline": name}
        
        # Add metrics
        for key in ["user_to_capture_ms", "user_to_pack_ms", "ttft_send_to_token_ms",
                    "user_to_audio_ms"]:
            row[f"{key}_p50"] = metrics.get(f"{key}_p50", 0)
            row[f"{key}_p90"] = metrics.get(f"{key}_p90", 0)
        
        row["pass_rate_2s"] = metrics.get("pass_rate", 0)
        row["n_samples"] = metrics.get("n_samples", 0)
        
        rows.append(row)
    
    df = pd.DataFrame(rows)
    
    # Save
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    print(f"\n✅ Saved: {out_path}")
    
    # Print table
    print("\n" + "="*80)
    print("Table 3: Wi-Fi E2E breakdown (unit: ms)")
    print("="*80)
    print(df.to_string(index=False))
    print("="*80)
    
    return df


def main():
    parser = argparse.ArgumentParser(description="Run Wi-Fi E2E breakdown experiment")
    parser.add_argument(
        "--camera_url",
        default="http://192.168.4.1/capture",
        help="ESP32 CameraWebServer capture URL"
    )
    parser.add_argument(
        "--n_samples",
        type=int,
        default=30,
        help="Number of samples per experiment (default 30)"
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        default=["raw", "resize"],
        choices=["raw", "resize"],
        help="pack mode (default: raw resize)"
    )
    parser.add_argument(
        "--disk_only",
        action="store_true",
        help="Use disk images only (skip Wi-Fi capture)"
    )
    args = parser.parse_args()
    
    print("="*60)
    print("Wi-Fi E2E Breakdown Experiment (Table 3)")
    print("="*60)
    
    if not args.disk_only:
        print(f"\n⚠️  Please ensure:")
        print(f"   1. ESP32 CameraWebServer is running: {args.camera_url}")
        print(f"   2. llama.cpp server is running: http://127.0.0.1:8080")
        print(f"\nAbout {args.n_samples} samples per experiment")
        
        confirm = input("\nConfirm start? (y/N): ")
        if confirm.lower() != 'y':
            print("Cancelled")
            return
    
    results = {}
    
    for mode in args.modes:
        if args.disk_only:
            # Disk playback mode
            csv_path = run_experiment(
                mode=mode,
                camera_url=args.camera_url,
                n_samples=args.n_samples,
                use_camera=False
            )
            if csv_path:
                results[f"Disk_{mode}"] = analyze_results(csv_path)
        else:
            # Wi-Fi live capture mode
            csv_path = run_experiment(
                mode=mode,
                camera_url=args.camera_url,
                n_samples=args.n_samples,
                use_camera=True
            )
            if csv_path:
                results[f"WiFi_{mode}"] = analyze_results(csv_path)
    
    # Generate Table 3
    if results:
        table3_path = project_root / "eval_benchmark" / "tables" / "table3_wifi_e2e.csv"
        generate_table3(results, table3_path)
    
    print("\n" + "="*60)
    print("✅ Done!")
    print("="*60)
    print("\nCheck output:")
    print(f"  - eval_benchmark/runs/wifi_e2e_*.csv")
    print(f"  - eval_benchmark/tables/table3_wifi_e2e.csv")


if __name__ == "__main__":
    main()
