"""
Experiment B: 5-turn multi-turn conversation stability benchmark.

Sends 5 sequential queries to the same image, accumulating conversation
history. Turn 5 repeats Turn 1 to measure state drift. Measures:
  - Per-turn TTFT/TTFA/E2E
  - Task completion rate (quality >= 1)
  - State confusion rate (Turn 5 contradicts Turn 1)

Usage (from eval_benchmark parent dir):
  python -m eval_benchmark.omni.multiturn_harness [--manifest ...]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.adapter import (
    _load_image_base64, _normalize_cfg_for_v6, TTSWorker,
    _pop_sentence, _sanitize_for_tts, _strip_think_tags,
    _postprocess_one_sentence,
)

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


def _now() -> float:
    return time.perf_counter()


def _run_single_turn(
    client: "OpenAI",
    messages: List[Dict[str, Any]],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Send one turn of conversation and return metrics."""
    model_name = cfg.get("model", "MiniCPM-o")
    enable_thinking = bool(cfg.get("thinking", False))
    max_tokens = 512 if enable_thinking else 80
    stop_seqs = None if enable_thinking else ["\n"]

    tts = TTSWorker(rate=220, enabled=True)
    t_send = _now()
    t_first_token: Optional[float] = None
    full_text = ""
    buf = ""
    error_msg: Optional[str] = None

    try:
        stream = client.chat.completions.create(
            model=model_name,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.0,
            stop=stop_seqs,
            stream=True,
            extra_body={"chat_template_kwargs": {"enable_thinking": enable_thinking}},
        )

        for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            text = getattr(delta, "content", None) or getattr(delta, "reasoning_content", None) or ""
            if not text:
                continue
            if t_first_token is None:
                t_first_token = _now()
            full_text += text
            buf += text
            chunk_text, buf = _pop_sentence(buf)
            if chunk_text:
                tts.speak(_sanitize_for_tts(chunk_text))

        if buf.strip():
            tts.speak(_sanitize_for_tts(buf.strip()))
    except Exception as e:
        error_msg = str(e)

    tts.wait_first_audio(timeout_s=5.0)
    t_audio = tts.first_audio_time
    tts.drain(timeout_s=10.0)
    t_end = _now()
    tts.close()

    if cfg.get("strip_think_tags", False) and full_text:
        full_text = _strip_think_tags(full_text)

    pred_text = _postprocess_one_sentence(full_text, max_chars=None) if full_text else ""
    if not pred_text:
        pred_text = "[EMPTY]" if not error_msg else f"[ERROR] {error_msg}"

    ttft_ms = (t_first_token - t_send) * 1000.0 if t_first_token else -1.0
    ttfa_ms = (t_audio - t_send) * 1000.0 if t_audio else -1.0
    e2e_ms = (t_end - t_send) * 1000.0

    return {
        "pred_text": pred_text,
        "full_text": full_text,
        "ttft_ms": round(ttft_ms, 2),
        "ttfa_ms": round(ttfa_ms, 2),
        "e2e_ms": round(e2e_ms, 2),
        "error": error_msg or "",
    }


def _detect_state_confusion(turn1_pred: str, turn5_pred: str) -> bool:
    """Heuristic: if Turn 5 (repeat of Turn 1) significantly differs, flag as confused."""
    if not turn1_pred or not turn5_pred:
        return False
    t1 = turn1_pred.replace(" ", "").lower()
    t5 = turn5_pred.replace(" ", "").lower()
    if t1 == t5:
        return False
    # check character-level overlap
    common = sum(1 for c in t1 if c in t5)
    max_len = max(len(t1), len(t5), 1)
    similarity = common / max_len
    # < 30% similarity = state confusion
    return similarity < 0.3


def _run_scenario(
    scenario: Dict[str, Any],
    cfg: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Run all 5 turns of a scenario, returning per-turn results."""
    cfg = _normalize_cfg_for_v6(cfg)
    server_url = cfg.get("server_url", "http://127.0.0.1:8080/v1/chat/completions")
    base_url = server_url.replace("/chat/completions", "").replace("/v1/chat/completions", "/v1")
    if not base_url.endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"

    resize_896 = bool(cfg.get("resize_896", True))
    max_edge = int(cfg.get("max_edge", 896))

    system_msg = (
        "\u4f60\u662f\u76f2\u4eba\u773c\u955c\u52a9\u624b\u3002"
        "\u53ea\u8f93\u51fa\u6700\u7ec8\u7b54\u6848\u7684\u4e00\u53e5\u8bdd\uff0c\u4e0d\u8981\u89e3\u91ca\u8fc7\u7a0b\uff0c\u4e0d\u8981\u4f7f\u7528\u6362\u884c\u3002"
        "\u7981\u6b62\u4ee5\u2018\u6211\u9700\u8981/\u8ba9\u6211/\u9996\u5148/\u5206\u6790/\u63a5\u4e0b\u6765\u2019\u5f00\u5934\u3002"
    )

    base_dir = os.path.join(os.path.dirname(__file__), "..")
    img_path = os.path.join(base_dir, scenario["image_path"])

    try:
        img_b64 = _load_image_base64(img_path, resize_896=resize_896, max_edge=max_edge)
    except FileNotFoundError as e:
        return [{
            "scenario_id": scenario["id"], "turn_idx": i,
            "error": str(e), "pred_text": "", "ttft_ms": -1,
            "ttfa_ms": -1, "e2e_ms": -1,
        } for i in range(5)]

    data_url = f"data:image/jpeg;base64,{img_b64}"
    client = OpenAI(base_url=base_url, api_key="sk-no-key-required")

    # build conversation history incrementally
    messages = [{"role": "system", "content": system_msg}]
    results = []
    turn1_pred = ""

    for turn_idx, turn in enumerate(scenario["turns"]):
        user_msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": turn["prompt"]},
                {"type": "image_url", "image_url": {"url": data_url}},
            ] if turn_idx == 0 else turn["prompt"],
        }
        # after first turn, send text-only (image already in context)
        if turn_idx > 0:
            user_msg = {"role": "user", "content": turn["prompt"]}

        messages.append(user_msg)

        turn_result = _run_single_turn(client, messages, cfg)

        # add assistant response to history
        messages.append({"role": "assistant", "content": turn_result["full_text"] or turn_result["pred_text"]})

        if turn_idx == 0:
            turn1_pred = turn_result["pred_text"]

        state_confusion = False
        if turn_idx == 4:
            state_confusion = _detect_state_confusion(turn1_pred, turn_result["pred_text"])

        # estimate token count
        history_chars = sum(
            len(str(m.get("content", ""))) for m in messages
        )

        results.append({
            "scenario_id": scenario["id"],
            "turn_idx": turn_idx,
            "prompt": turn["prompt"],
            "gt_answer": turn.get("gt_answer", ""),
            "pred_text": turn_result["pred_text"],
            "ttft_ms": turn_result["ttft_ms"],
            "ttfa_ms": turn_result["ttfa_ms"],
            "e2e_ms": turn_result["e2e_ms"],
            "history_tokens": history_chars // 2,
            "state_confusion": state_confusion,
            "error": turn_result["error"],
        })

        print(f"    Turn {turn_idx}: TTFT={turn_result['ttft_ms']:.0f}ms "
              f"E2E={turn_result['e2e_ms']:.0f}ms "
              f"pred=\"{turn_result['pred_text'][:50]}...\"")

    return results


def main():
    parser = argparse.ArgumentParser(description="Multi-turn conversation stability harness")
    parser.add_argument("--manifest", default=os.path.join(
        os.path.dirname(__file__), "manifests", "multiturn_scenarios.json"))
    parser.add_argument("--out_csv", default=os.path.join(
        os.path.dirname(__file__), "..", "runs", "multiturn_results.csv"))
    args = parser.parse_args()

    with open(args.manifest, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    cfg = manifest["adapter_config"]
    scenarios = manifest["scenarios"]

    os.makedirs(os.path.dirname(args.out_csv), exist_ok=True)

    fieldnames = [
        "scenario_id", "turn_idx", "prompt", "gt_answer", "pred_text",
        "ttft_ms", "ttfa_ms", "e2e_ms", "history_tokens",
        "state_confusion", "error",
    ]

    all_results: List[Dict[str, Any]] = []

    for i, scenario in enumerate(scenarios):
        print(f"\n[{i+1}/{len(scenarios)}] Scenario {scenario['id']} ({scenario['base_task']})")
        rows = _run_scenario(scenario, cfg)
        all_results.extend(rows)

    with open(args.out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_results)

    print(f"\nWrote {len(all_results)} rows to {args.out_csv}")

    # quick summary by turn index
    print(f"\n=== Multi-turn Summary ===")
    for tidx in range(5):
        turn_rows = [r for r in all_results if r["turn_idx"] == tidx and r.get("ttft_ms", -1) > 0]
        if not turn_rows:
            continue
        ttfts = sorted(r["ttft_ms"] for r in turn_rows)
        e2es = sorted(r["e2e_ms"] for r in turn_rows)
        print(f"  Turn {tidx}: n={len(turn_rows)} "
              f"TTFT_p50={ttfts[len(ttfts)//2]:.0f}ms "
              f"E2E_p50={e2es[len(e2es)//2]:.0f}ms")

    confused = [r for r in all_results if r.get("state_confusion")]
    total_t5 = [r for r in all_results if r["turn_idx"] == 4]
    print(f"  State confusion: {len(confused)}/{len(total_t5)} "
          f"({100*len(confused)/max(len(total_t5),1):.1f}%)")


if __name__ == "__main__":
    main()
