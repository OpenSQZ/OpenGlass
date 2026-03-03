from __future__ import annotations

import argparse
import os
from typing import Dict, List

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from .metrics import percentiles


def _read_all_runs(runs_dir: str) -> pd.DataFrame:
    files = [os.path.join(runs_dir, f) for f in os.listdir(runs_dir) if f.endswith(".csv")]
    if not files:
        raise FileNotFoundError(f"No run CSV found in {runs_dir}")
    dfs = [pd.read_csv(p) for p in files]
    return pd.concat(dfs, ignore_index=True)


def _summarize_group(df: pd.DataFrame) -> Dict[str, float]:
    out = {}
    for k in ["ttft_ms", "ttfa_ms", "e2e_ms"]:
        ps = percentiles(df[k].astype(float), ps=(50, 90, 95))
        out.update({f"{k}_{p}": v for p, v in ps.items()})
    out["quality_mean"] = float(np.nanmean(df["quality_score"].astype(float)))
    out["success_rate"] = float(np.nanmean(df["is_success"].astype(float)))
    out["abstain_rate"] = float(np.nanmean(df["is_abstain"].astype(float)))
    out["highconf_error_rate"] = float(np.nanmean(df["is_highconf_error"].astype(float)))
    out["n"] = int(len(df))
    return out


def _make_table1(all_df: pd.DataFrame) -> pd.DataFrame:
    """Main table: compare different baseline approaches"""
    rows = []
    for bucket in ["Cloud", "Cloud25", "CloudCN", "NaiveOnDevice", "Ours", "Ours-O", "Ours-O-Think"]:
        if bucket == "Ours":
            # For "Ours", only count Full configuration, exclude ablation experiments
            sub = all_df[(all_df["bucket_main"] == bucket) & (all_df["bucket_ablation"] == "Full")]
        elif bucket == "Ours-O":
            sub = all_df[(all_df["bucket_main"] == bucket) & (all_df["bucket_ablation"] == "Full-O")]
        elif bucket == "Ours-O-Think":
            sub = all_df[(all_df["bucket_main"] == bucket) & (all_df["bucket_ablation"] == "Full-O-Think")]
        else:
            sub = all_df[all_df["bucket_main"] == bucket]
        if len(sub) == 0:
            continue
        row = {"baseline": bucket}
        row.update(_summarize_group(sub))
        rows.append(row)
    return pd.DataFrame(rows)


def _make_table2(all_df: pd.DataFrame) -> pd.DataFrame:
    """Ablation study table: filter all rows with bucket_ablation tag"""
    # Filter rows with bucket_ablation (no longer dependent on bucket_main)
    sub = all_df[all_df["bucket_ablation"].notna()].copy()
    rows = []
    for ab in ["Full", "w/o Streaming", "w/o ParallelTTS", "w/o Resize", "w/o Safety", "Resize448", "Full-O", "Full-O-Think"]:
        g = sub[sub["bucket_ablation"] == ab]
        if len(g) == 0:
            continue
        row = {"ablation": ab}
        row.update(_summarize_group(g))
        rows.append(row)
    return pd.DataFrame(rows)


def _plot_pareto(all_df: pd.DataFrame, out_png: str, x_key: str = "ttfa_ms"):
    # One point per run_name
    g = all_df.groupby("run_name").agg({
        x_key: "median",
        "quality_score": "mean",
        "bucket_main": "first",
        "bucket_ablation": "first",
    }).reset_index()

    plt.figure()
    plt.scatter(g[x_key], g["quality_score"])
    for _, r in g.iterrows():
        label = r["run_name"]
        plt.annotate(label, (r[x_key], r["quality_score"]), textcoords="offset points", xytext=(5, 5))
    plt.xlabel(x_key)
    plt.ylabel("quality_score (mean)")
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    plt.savefig(out_png, dpi=200, bbox_inches="tight")
    plt.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs_dir", required=True)
    ap.add_argument("--out_dir", required=True)
    args = ap.parse_args()

    all_df = _read_all_runs(args.runs_dir)

    os.makedirs(os.path.join(args.out_dir, "tables"), exist_ok=True)
    os.makedirs(os.path.join(args.out_dir, "figures"), exist_ok=True)

    t1 = _make_table1(all_df)
    t2 = _make_table2(all_df)

    t1_path = os.path.join(args.out_dir, "tables", "table1_main.csv")
    t2_path = os.path.join(args.out_dir, "tables", "table2_ablation.csv")
    t1.to_csv(t1_path, index=False)
    t2.to_csv(t2_path, index=False)

    fig_path = os.path.join(args.out_dir, "figures", "figure1_pareto.png")
    _plot_pareto(all_df, fig_path, x_key="ttfa_ms")

    print(f"Wrote: {t1_path}")
    print(f"Wrote: {t2_path}")
    print(f"Wrote: {fig_path}")


if __name__ == "__main__":
    main()
