import os, json, time, base64, re
import pandas as pd
from pathlib import Path
from openai import OpenAI
import json, re
'''
Python requests were hijacked by the system proxy (Privoxy), failing to reach the llama-server at 127.0.0.1:8080,
instead going through the proxy which returned this 500 HTML.
The fix is to bypass the proxy for local requests.
'''
os.environ["NO_PROXY"] = "127.0.0.1,localhost,0.0.0.0"
os.environ["no_proxy"] = "127.0.0.1,localhost,0.0.0.0"
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)


MANIFEST = "manifest_nlp_v6_mixedbest.csv"
OUT = "predictions_nlp_v6_mixedbest_nothink.csv"

BASE_URL = "http://127.0.0.1:8080/v1"
MODEL = "ggml-model-Q4_K_M.gguf"
API_KEY = os.getenv("LLAMA_API_KEY", "sk-no-key-required")

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)

# When thinking is disabled globally, you should NOT fall back to reasoning_content.
ALLOW_REASONING_FALLBACK = False

def resolve_path(p: str) -> Path:
    p = str(p).strip()
    cand = Path(p)
    if cand.exists():
        return cand
    base = Path(MANIFEST).resolve().parent
    cand2 = base / p
    if cand2.exists():
        return cand2
    cand3 = base / p.replace("\\", os.sep).replace("/", os.sep)
    return cand3

def img_to_data_url(img_path: Path) -> str:
    b = img_path.read_bytes()
    b64 = base64.b64encode(b).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"



def extract_text_from_resp(resp) -> str:
    """Compatible with various OpenAI SDK / llama.cpp returns, try to extract text."""
    try:
        msg = resp.choices[0].message
    except Exception:
        return ""

    # 1) Common: content is str
    content = getattr(msg, "content", None)
    if isinstance(content, str) and content.strip():
        return content

    # 2) Compatibility: find from model_dump
    md = msg.model_dump() if hasattr(msg, "model_dump") else {}
    c = md.get("content", None)

    # content could be list[{"type":"text","text":...}, ...]
    if isinstance(c, list):
        texts = []
        for part in c:
            if isinstance(part, dict):
                texts.append(part.get("text", ""))
            elif isinstance(part, str):
                texts.append(part)
        joined = "".join(texts).strip()
        if joined:
            return joined

    if isinstance(c, str) and c.strip():
        return c

    # 3) Some servers put thoughts in reasoning_content. For TTS/eval we usually do NOT want this.
    if ALLOW_REASONING_FALLBACK:
        for k in ("reasoning_content", "reasoning"):
            v = md.get(k, None)
            if isinstance(v, str) and v.strip():
                return v

    # 4) tool_calls case: content being empty is common
    # Return empty here, let upper layer log debug
    return ""


def postprocess_one_sentence(text: str, max_chars: int | None = None) -> str:
    """Remove newlines, compress into one sentence (keep first sentence end), optional char limit."""
    t = (text or "").replace("\r", " ").replace("\n", " ")
    t = re.sub(r"\s+", " ", t).strip()

    # Keep only the first sentence (encountering period/question mark/exclamation mark)
    m = re.split(r"[。！？!?]", t)
    if m and m[0].strip():
        t = m[0].strip()

    if max_chars is not None and len(t) > max_chars:
        t = t[:max_chars].rstrip("，,;；:：")

    return t

def main():
    df = pd.read_csv(MANIFEST)

    # T3A allows categories based on your data (here: Exit/Restroom/Meeting Room)
    t3a_classes = sorted(set(df[df.task=="T3A"]["label"].astype(str).tolist()))
    t3a_classes = [c for c in t3a_classes if c and c.lower() != "nan"]
    if not t3a_classes:
        t3a_classes = ["Exit","Entrance","Elevator","Cashier","Restroom","Meeting Room"]

    # Resume: skip already inferred sample_id if OUT exists
    done = set()
    if Path(OUT).exists():
        old = pd.read_csv(OUT)
        if "sample_id" in old.columns:
            done = set(old["sample_id"].astype(str).tolist())
        print(f"[resume] found {len(done)} done samples in {OUT}")

    rows_out = []
    for _, row in df.iterrows():
        sid = str(row["sample_id"])
        if sid in done:
            continue

        img_path = resolve_path(row["image_path"])
        if not img_path.exists():
            rows_out.append({**row.to_dict(), "pred_raw": "", "pred_json": "", "infer_ms": "", "error": f"missing_image:{img_path}"})
            continue

        # prompt = build_prompt(row, t3a_classes)
        # prompt = str(row["prompt_nl_v2"])
        prompt = str(row["prompt_nl_v3"])
        data_url = img_to_data_url(img_path)

        t0 = time.perf_counter()
        err = ""
        pred_raw = ""
        pred_j = None
        try:
            # Optional char limit per task (use if v3 prompt has requirements; otherwise None)
            MAX_CHARS = {"T1": 40, "T2": 26, "T3A": 30, "T3B": 32, "H1": 22}
            task = str(row["task"])
            max_chars = MAX_CHARS.get(task, None)

            system_msg = (
                "You are a blind glasses assistant."
                "Output only one sentence for the final answer, do not explain the process, do not use newlines."
                "Do not start with 'I need/Let me/First/Analyze/Next'."
            )

            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system_msg},
                    {
                        "role": "user",
                        "content": [
                            {"type":"text", "text": prompt},
                            {"type":"image_url", "image_url":{"url": data_url}}
                        ]
                    }
                ],
                max_tokens=80,        # Key: compress early to avoid long analysis
                temperature=0.0,      # Key: reduce random wordiness
                stop=["\n"],          # Key: prevent newline expansion (use \n, not "." to avoid premature truncation)
                # Belt-and-suspenders: request-level disable thinking (server should also be started with --reasoning-budget 0)
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )

            raw = extract_text_from_resp(resp)
            pred_raw = postprocess_one_sentence(raw, max_chars=max_chars)

            # If pred_raw is still empty: dump original response to help locate "true empty vs false empty"
            if not pred_raw:
                with open("empty_debug.jsonl", "a", encoding="utf-8") as f:
                    try:
                        f.write(resp.model_dump_json() + "\n")
                    except Exception:
                        f.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")

        except Exception as e:
            err = repr(e)
            if len(rows_out) < 5:
                print("ERROR example:", err)

        infer_ms = (time.perf_counter() - t0) * 1000

        rows_out.append({
            **row.to_dict(),
            "pred_raw": pred_raw,
            "infer_ms": f"{infer_ms:.1f}",
            "error": err
        })

        if len(rows_out) % 10 == 0:
            print(f"[{len(rows_out)} new] last={sid} ms={infer_ms:.1f} err={'Y' if err else 'N'}")

    # Output: append if OUT already exists
    out_df = pd.DataFrame(rows_out)
    if Path(OUT).exists():
        base = pd.read_csv(OUT)
        out_df = pd.concat([base, out_df], ignore_index=True)
        out_df = out_df.drop_duplicates(subset=["sample_id"], keep="first")

    out_df.to_csv(OUT, index=False, encoding="utf-8-sig")
    print("saved:", OUT)

if __name__ == "__main__":
    main()
