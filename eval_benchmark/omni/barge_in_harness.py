"""
Experiment A: Barge-in / Interruption benchmark.

Simulates the scenario where the system is responding to query A,
and the user interrupts with a new query B. Measures:
  - T_stop:  time from barge-in trigger to old TTS stopping
  - T_resume: time from barge-in trigger to new TTS starting
  - Barge-in success rate
  - Carry-over error rate (new answer contaminated by old)

Usage (from eval_benchmark parent dir):
  python -m eval_benchmark.omni.barge_in_harness [--manifest ...]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional

# -- project imports (run from exp_opus/) --
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.adapter import _load_image_base64, _normalize_cfg_for_v6, TTSWorker, \
    _pop_sentence, _sanitize_for_tts, _strip_think_tags, _postprocess_one_sentence

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    from src.rubric import score_sample
except ImportError:
    score_sample = None


def _now() -> float:
    return time.perf_counter()


def _run_one_scenario(
    scenario: Dict[str, Any],
    delay_ms: int,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Run a single barge-in trial: initial query -> interrupt at delay -> new query."""

    cfg = _normalize_cfg_for_v6(cfg)
    server_url = cfg.get("server_url", "http://127.0.0.1:8080/v1/chat/completions")
    base_url = server_url.replace("/chat/completions", "").replace("/v1/chat/completions", "/v1")
    if not base_url.endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"
    model_name = cfg.get("model", "MiniCPM-o")
    enable_thinking = bool(cfg.get("thinking", False))
    max_tokens = 512 if enable_thinking else 80
    stop_seqs = None if enable_thinking else ["\n"]
    resize_896 = bool(cfg.get("resize_896", True))
    max_edge = int(cfg.get("max_edge", 896))

    system_msg = (
        "\u4f60\u662f\u76f2\u4eba\u773c\u955c\u52a9\u624b\u3002"
        "\u53ea\u8f93\u51fa\u6700\u7ec8\u7b54\u6848\u7684\u4e00\u53e5\u8bdd\uff0c\u4e0d\u8981\u89e3\u91ca\u8fc7\u7a0b\uff0c\u4e0d\u8981\u4f7f\u7528\u6362\u884c\u3002"
        "\u7981\u6b62\u4ee5\u2018\u6211\u9700\u8981/\u8ba9\u6211/\u9996\u5148/\u5206\u6790/\u63a5\u4e0b\u6765\u2019\u5f00\u5934\u3002"
    )

    initial = scenario["initial"]
    interrupt = scenario["interrupt"]

    # load images
    base_dir = os.path.join(os.path.dirname(__file__), "..")
    init_img_path = os.path.join(base_dir, initial["image_path"])
    intr_img_path = os.path.join(base_dir, interrupt["image_path"])

    try:
        init_b64 = _load_image_base64(init_img_path, resize_896=resize_896, max_edge=max_edge)
        intr_b64 = _load_image_base64(intr_img_path, resize_896=resize_896, max_edge=max_edge)
    except FileNotFoundError as e:
        return {"error": str(e), "barge_in_success": False}

    client = OpenAI(base_url=base_url, api_key="sk-no-key-required")
    tts = TTSWorker(rate=220, enabled=True)

    cancelled = threading.Event()
    init_text_parts: List[str] = []
    t_initial_send: Optional[float] = None
    t_initial_first_token: Optional[float] = None
    t_initial_audio_start: Optional[float] = None

    # --- Phase 1: send initial query in a background thread ---
    def stream_initial():
        nonlocal t_initial_send, t_initial_first_token
        t_initial_send = _now()
        try:
            stream = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": [
                        {"type": "text", "text": initial["prompt"]},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{init_b64}"}},
                    ]},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
                stop=stop_seqs,
                stream=True,
                extra_body={"chat_template_kwargs": {"enable_thinking": enable_thinking}},
            )
            buf = ""
            for chunk in stream:
                if cancelled.is_set():
                    break
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                text = getattr(delta, "content", None) or getattr(delta, "reasoning_content", None) or ""
                if not text:
                    continue
                if t_initial_first_token is None:
                    t_initial_first_token = _now()
                init_text_parts.append(text)
                buf += text
                chunk_text, buf = _pop_sentence(buf)
                if chunk_text:
                    tts.speak(_sanitize_for_tts(chunk_text))
        except Exception:
            pass

    init_thread = threading.Thread(target=stream_initial, daemon=True)
    init_thread.start()

    # wait for TTS to start playing (= initial audio start)
    tts.wait_first_audio(timeout_s=10.0)
    t_initial_audio_start = tts.first_audio_time

    # wait the specified delay from initial audio start
    if t_initial_audio_start is not None:
        wait_until = t_initial_audio_start + delay_ms / 1000.0
        sleep_time = wait_until - _now()
        if sleep_time > 0:
            time.sleep(sleep_time)

    # --- Phase 2: BARGE-IN ---
    t_barge_in_trigger = _now()

    # cancel old stream
    cancelled.set()
    t_old_stream_cancel = _now()

    # stop old TTS: drain queue, signal stop, kill engine
    if tts.stop_event is not None:
        tts.stop_event.set()
    if tts.q is not None:
        # flush pending items so thread doesn't block
        while not tts.q.empty():
            try:
                tts.q.get_nowait()
                tts.q.task_done()
            except Exception:
                break
    if tts.engine is not None:
        try:
            tts.engine.stop()
        except Exception:
            pass
    t_old_audio_stop = _now()

    # cleanup old TTS
    tts.close()

    # --- Phase 3: send new query ---
    tts2 = TTSWorker(rate=220, enabled=True)
    new_text_parts: List[str] = []
    t_new_send: Optional[float] = None
    t_new_first_token: Optional[float] = None
    t_new_audio_start: Optional[float] = None
    new_error: Optional[str] = None

    t_new_send = _now()
    try:
        stream2 = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": [
                    {"type": "text", "text": interrupt["prompt"]},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{intr_b64}"}},
                ]},
            ],
            max_tokens=max_tokens,
            temperature=0.0,
            stop=stop_seqs,
            stream=True,
            extra_body={"chat_template_kwargs": {"enable_thinking": enable_thinking}},
        )
        buf2 = ""
        for chunk in stream2:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            text = getattr(delta, "content", None) or getattr(delta, "reasoning_content", None) or ""
            if not text:
                continue
            if t_new_first_token is None:
                t_new_first_token = _now()
            new_text_parts.append(text)
            buf2 += text
            chunk_text, buf2 = _pop_sentence(buf2)
            if chunk_text:
                tts2.speak(_sanitize_for_tts(chunk_text))

        if buf2.strip():
            tts2.speak(_sanitize_for_tts(buf2.strip()))
    except Exception as e:
        new_error = str(e)

    tts2.wait_first_audio(timeout_s=10.0)
    t_new_audio_start = tts2.first_audio_time
    tts2.drain(timeout_s=15.0)
    t_new_end = _now()
    tts2.close()

    # wait for init thread to finish
    init_thread.join(timeout=2.0)

    # --- compute metrics ---
    full_new_text = "".join(new_text_parts)
    if cfg.get("strip_think_tags", False):
        full_new_text = _strip_think_tags(full_new_text)
    new_pred = _postprocess_one_sentence(full_new_text, max_chars=None) if full_new_text else ""

    t_stop_ms = (t_old_audio_stop - t_barge_in_trigger) * 1000.0
    t_resume_ms = (t_new_audio_start - t_barge_in_trigger) * 1000.0 if t_new_audio_start else -1.0
    new_ttft_ms = (t_new_first_token - t_new_send) * 1000.0 if t_new_first_token else -1.0
    new_ttfa_ms = (t_new_audio_start - t_new_send) * 1000.0 if t_new_audio_start else -1.0
    new_e2e_ms = (t_new_end - t_new_send) * 1000.0

    barge_in_success = (new_pred and not new_pred.startswith("[ERROR]")
                        and not new_pred.startswith("[EMPTY]")
                        and t_new_audio_start is not None)

    # carry-over: check if initial text leaked into new answer
    init_full = "".join(init_text_parts)
    carry_over = False
    if barge_in_success and init_full and new_pred:
        # if >50% of initial answer appears in new answer, flag carry-over
        init_words = set(init_full)
        overlap = sum(1 for c in new_pred if c in init_words)
        if len(new_pred) > 0 and overlap / len(new_pred) > 0.7:
            carry_over = True

    return {
        "scenario_id": scenario["id"],
        "interrupt_delay_ms": delay_ms,
        "t_stop_ms": round(t_stop_ms, 2),
        "t_resume_ms": round(t_resume_ms, 2),
        "barge_in_success": barge_in_success,
        "carry_over_error": carry_over,
        "new_pred_text": new_pred,
        "new_ttft_ms": round(new_ttft_ms, 2),
        "new_ttfa_ms": round(new_ttfa_ms, 2),
        "new_e2e_ms": round(new_e2e_ms, 2),
        "initial_pred_text": init_full.strip()[:200],
        "error": new_error or "",
    }


def main():
    parser = argparse.ArgumentParser(description="Barge-in experiment harness")
    parser.add_argument("--manifest", default=os.path.join(
        os.path.dirname(__file__), "manifests", "barge_in_scenarios.json"))
    parser.add_argument("--out_csv", default=os.path.join(
        os.path.dirname(__file__), "..", "runs", "barge_in_results.csv"))
    args = parser.parse_args()

    with open(args.manifest, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    cfg = manifest["adapter_config"]
    delays = manifest["interrupt_delays_ms"]
    scenarios = manifest["scenarios"]

    os.makedirs(os.path.dirname(args.out_csv), exist_ok=True)

    fieldnames = [
        "scenario_id", "interrupt_delay_ms", "t_stop_ms", "t_resume_ms",
        "barge_in_success", "carry_over_error", "new_pred_text",
        "new_ttft_ms", "new_ttfa_ms", "new_e2e_ms",
        "initial_pred_text", "error",
    ]

    results: List[Dict[str, Any]] = []
    total = len(scenarios) * len(delays)
    idx = 0

    for scenario in scenarios:
        for delay in delays:
            idx += 1
            print(f"\n[{idx}/{total}] Scenario {scenario['id']} | delay={delay}ms")
            try:
                row = _run_one_scenario(scenario, delay, cfg)
            except Exception as e:
                row = {
                    "scenario_id": scenario["id"],
                    "interrupt_delay_ms": delay,
                    "t_stop_ms": -1, "t_resume_ms": -1,
                    "barge_in_success": False, "carry_over_error": False,
                    "new_pred_text": "", "new_ttft_ms": -1,
                    "new_ttfa_ms": -1, "new_e2e_ms": -1,
                    "initial_pred_text": "", "error": str(e),
                }
            results.append(row)
            status = "OK" if row.get("barge_in_success") else "FAIL"
            print(f"  [{status}] T_stop={row.get('t_stop_ms', -1):.0f}ms "
                  f"T_resume={row.get('t_resume_ms', -1):.0f}ms "
                  f"new_TTFT={row.get('new_ttft_ms', -1):.0f}ms")

    with open(args.out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    print(f"\nWrote {len(results)} rows to {args.out_csv}")

    # quick summary
    successes = [r for r in results if r.get("barge_in_success")]
    if successes:
        stops = sorted(r["t_stop_ms"] for r in successes)
        resumes = sorted(r["t_resume_ms"] for r in successes if r["t_resume_ms"] > 0)
        print(f"\n=== Barge-in Summary ===")
        print(f"  Success rate: {len(successes)}/{len(results)} "
              f"({100*len(successes)/len(results):.1f}%)")
        print(f"  T_stop  p50={stops[len(stops)//2]:.0f}ms  "
              f"p95={stops[int(len(stops)*0.95)]:.0f}ms")
        if resumes:
            print(f"  T_resume p50={resumes[len(resumes)//2]:.0f}ms  "
                  f"p95={resumes[int(len(resumes)*0.95)]:.0f}ms")
        carry = sum(1 for r in successes if r.get("carry_over_error"))
        print(f"  Carry-over errors: {carry}/{len(successes)}")


if __name__ == "__main__":
    main()
