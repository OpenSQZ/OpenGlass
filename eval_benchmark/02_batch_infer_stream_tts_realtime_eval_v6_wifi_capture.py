"""Realtime stream-TTS evaluation (v6) with optional ESP32 CameraWebServer Wi‑Fi capture.

Goal
----
Simulate AI glasses as closely as possible:
Mic (user asks) -> (optional Wi‑Fi image capture) -> llama.cpp OpenAI-compatible server (MiniCPM-V)
-> streaming text -> sentence buffering -> parallel TTS -> *first audible word*.

Key differences vs. v5
----------------------
1) Adds a realistic **camera capture phase**:
   - GET http://<ESP32_IP>/capture (CameraWebServer) to fetch the latest JPEG.
   - Measures capture latency and bytes.
2) Lets you choose what to send to the VLM:
   - send the camera JPEG **as-is** (no resize/re-encode)
   - OR resize/re-encode on the laptop (to explore latency/quality trade-off)
3) Keeps the improved streaming TTFT measurement:
   - counts first delta from either `content` or `reasoning_content`

Usage examples
--------------
# 1) Offline (disk images), resize+re-encode (fast, optimistic)
python 02_batch_infer_stream_tts_realtime_eval_v6_wifi_capture.py \
  --manifest manifest_nlp_v6_mixedbest.csv \
  --out predictions_v6_disk.csv \
  --log predictions_v6_disk_log.txt

# 2) Wi‑Fi capture, send raw camera JPEG (more realistic)
python 02_batch_infer_stream_tts_realtime_eval_v6_wifi_capture.py \
  --manifest manifest_nlp_v6_mixedbest.csv \
  --camera_url http://192.168.4.1/capture \
  --use_camera 1 \
  --pack_mode raw \
  --out predictions_v6_wifi_raw.csv \
  --log predictions_v6_wifi_raw_log.txt

# 3) Wi‑Fi capture, but resize on laptop (often enough to hit <2s)
python 02_batch_infer_stream_tts_realtime_eval_v6_wifi_capture.py \
  --manifest manifest_nlp_v6_mixedbest.csv \
  --camera_url http://192.168.4.1/capture \
  --use_camera 1 \
  --pack_mode resize \
  --max_edge 896 --jpeg_quality 80

Notes
-----
- This script uses `pyttsx3` if available. If not, it still logs the *enqueue time*;
  you can wire in your real TTS player by replacing TTSWorker.
- `first_audio` timestamp is when the TTS worker *starts* speaking the first segment.
  If you want a stricter definition (speaker starts output waveform), integrate with
  your audio backend callback.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

import pandas as pd
import requests
from openai import OpenAI

try:
    from PIL import Image
except Exception as e:  # pragma: no cover
    raise RuntimeError("PIL/Pillow is required for this script") from e

try:
    import pyttsx3
except Exception:
    pyttsx3 = None

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


# -----------------------------
# Time + logging
# -----------------------------

def now() -> float:
    return time.perf_counter()


class TeeLogger:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.f = self.log_path.open("w", encoding="utf-8")

    def log(self, msg: str) -> None:
        print(msg, flush=True)
        self.f.write(msg + "\n")
        self.f.flush()

    def close(self) -> None:
        try:
            self.f.close()
        except Exception:
            pass


# -----------------------------
# Image capture + packing
# -----------------------------

def resolve_path(manifest_path: Path, p: str) -> Path:
    p = str(p).strip()
    cand = Path(p)
    if cand.exists():
        return cand
    base = manifest_path.resolve().parent
    cand2 = base / p
    if cand2.exists():
        return cand2
    return base / p.replace("\\", os.sep).replace("/", os.sep)


@dataclass
class CaptureStats:
    fetch_ms: float = 0.0
    bytes_kb: float = 0.0
    status: str = ""


@dataclass
class ImagePackStats:
    read_ms: float = 0.0
    resize_ms: float = 0.0
    jpeg_ms: float = 0.0
    b64_ms: float = 0.0
    payload_kb: float = 0.0


def fetch_camera_jpeg(camera_url: str, timeout_s: float = 2.0) -> Tuple[bytes, CaptureStats]:
    st = CaptureStats()
    t0 = now()
    r = requests.get(camera_url, timeout=timeout_s)
    st.fetch_ms = (now() - t0) * 1000.0
    st.status = f"{r.status_code}"
    r.raise_for_status()
    b = r.content
    st.bytes_kb = len(b) / 1024.0
    return b, st


def _b64_data_url_from_jpeg_bytes(jpg_bytes: bytes) -> Tuple[str, ImagePackStats]:
    st = ImagePackStats()
    t0 = now()
    b64 = base64.b64encode(jpg_bytes).decode("utf-8")
    st.b64_ms = (now() - t0) * 1000.0
    st.payload_kb = len(b64) / 1024.0
    return f"data:image/jpeg;base64,{b64}", st


def pack_resize_reencode(
    jpg_bytes: bytes,
    max_edge: int = 896,
    jpeg_quality: int = 80,
) -> Tuple[str, ImagePackStats]:
    """Decode -> optional resize -> re-encode JPEG -> base64.

    This is *not* the real ESP32 camera pipeline, but is useful for exploring
    latency/quality trade-offs.
    """
    st = ImagePackStats()

    # decode
    t0 = now()
    img = Image.open(io.BytesIO(jpg_bytes))
    st.read_ms = (now() - t0) * 1000.0

    # resize
    t1 = now()
    if max(img.size) > max_edge:
        ratio = max_edge / max(img.size)
        new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
    st.resize_ms = (now() - t1) * 1000.0

    # jpeg
    t2 = now()
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
    out = buf.getvalue()
    st.jpeg_ms = (now() - t2) * 1000.0

    # base64
    t3 = now()
    b64 = base64.b64encode(out).decode("utf-8")
    st.b64_ms = (now() - t3) * 1000.0
    st.payload_kb = len(b64) / 1024.0

    return f"data:image/jpeg;base64,{b64}", st


# -----------------------------
# TTS worker
# -----------------------------

class TTSWorker:
    """Dedicated TTS worker that timestamps when speaking begins."""

    def __init__(self, rate: int = 220, enabled: bool = True):
        self.enabled = enabled and (pyttsx3 is not None)
        self.q: "queue.Queue[Optional[str]]" = queue.Queue()
        self.stop_event = threading.Event()
        self.first_audio_time: Optional[float] = None
        self.first_audio_event = threading.Event()
        self._started = False

        self.engine = None
        if self.enabled:
            try:
                self.engine = pyttsx3.init()
                self.engine.setProperty("rate", rate)
            except Exception:
                self.engine = None
                self.enabled = False

        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                text = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            if text is None:
                self.q.task_done()
                break

            if not self._started:
                self._started = True
                self.first_audio_time = now()
                self.first_audio_event.set()

            if self.enabled and self.engine is not None:
                try:
                    self.engine.say(text)
                    self.engine.runAndWait()
                except Exception:
                    pass

            self.q.task_done()

    def speak(self, text: str) -> None:
        if not text or not text.strip():
            return
        self.q.put(text)

    def wait_first_audio(self, timeout_s: float = 2.0) -> bool:
        return self.first_audio_event.wait(timeout=timeout_s)

    def drain(self, timeout_s: Optional[float] = None) -> None:
        if timeout_s is None:
            self.q.join()
            return
        deadline = time.time() + float(timeout_s)
        while time.time() < deadline:
            if self.q.unfinished_tasks == 0:
                return
            time.sleep(0.05)

    def close(self) -> None:
        try:
            self.stop_event.set()
            self.q.put(None)
            self.thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            if self.engine is not None:
                self.engine.stop()
        except Exception:
            pass


# -----------------------------
# Streaming helpers
# -----------------------------

PUNCT_RE = re.compile(r"[，。！？,!.?;；:：\n]")
SPLIT_RE = re.compile(r"([，。！？,!.?;；:：\n]+)")


def pick_delta_text(delta: Any) -> Tuple[str, str]:
    ans = getattr(delta, "content", None) or ""
    rea = getattr(delta, "reasoning_content", None) or ""
    return ans, rea


def sanitize_for_tts(s: str) -> str:
    s = re.sub(r"```.*?```", " ", s, flags=re.DOTALL)
    s = s.replace("#", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


@dataclass
class LatencyMetrics:
    user_to_capture_ms: float = 0.0
    user_to_pack_ms: float = 0.0
    user_to_send_ms: float = 0.0
    send_to_token_ms: float = 0.0
    user_to_token_ms: float = 0.0
    user_to_first_enqueue_ms: float = 0.0
    user_to_audio_ms: float = 0.0
    token_to_audio_ms: float = 0.0
    status: str = ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=str, default="manifest_nlp_v6_mixedbest.csv")
    ap.add_argument("--out", type=str, default="predictions_stream_tts_realtime_eval_v6.csv")
    ap.add_argument("--log", type=str, default="predictions_stream_tts_realtime_eval_v6_log.txt")

    ap.add_argument("--base_url", type=str, default="http://127.0.0.1:8080/v1")
    ap.add_argument("--model", type=str, default="ggml-model-Q4_K_M.gguf")
    ap.add_argument("--api_key", type=str, default=os.getenv("LLAMA_API_KEY", "sk-no-key-required"))

    # camera capture
    ap.add_argument("--use_camera", type=int, default=0, help="1=fetch image from ESP32 camera_url for every sample")
    ap.add_argument("--camera_url", type=str, default="http://10.100.6.79/capture")
    ap.add_argument("--camera_timeout", type=float, default=2.0)

    # packing mode
    ap.add_argument(
        "--pack_mode",
        choices=["raw", "resize"],
        default="raw",
        help="raw: base64 the camera JPEG as-is; resize: decode+resize+re-encode on laptop",
    )
    ap.add_argument("--max_edge", type=int, default=896)
    ap.add_argument("--jpeg_quality", type=int, default=80)

    # streaming
    ap.add_argument("--max_tokens", type=int, default=256)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--enable_thinking", type=int, default=1, help="1=allow thinking, 0=disable (if server supports)")

    # sentence buffering
    ap.add_argument("--flush_wait_ms", type=int, default=250, help="if no punctuation within this time, flush anyway")
    ap.add_argument("--flush_min_chars", type=int, default=12, help="flush when buffer exceeds this chars")

    # tts
    ap.add_argument("--tts", type=int, default=1, help="1=enable pyttsx3 TTS (if installed), 0=disable")
    ap.add_argument("--tts_rate", type=int, default=220)

    args = ap.parse_args()

    manifest_path = Path(args.manifest)
    out_path = Path(args.out)
    log_path = Path(args.log)

    log = TeeLogger(log_path)

    df = pd.read_csv(manifest_path)
    log.log(f"Loaded {len(df)} samples from {manifest_path}.")

    client = OpenAI(api_key=args.api_key, base_url=args.base_url)

    rows_out = []

    for i, row in df.iterrows():
        sid = str(row.get("sample_id", ""))
        prompt = str(row.get("prompt_nl_v3", row.get("prompt_nl_v2", row.get("prompt", ""))))

        # Simulate: user finishes speaking now.
        t_user_done = now()

        # fresh TTS per request to avoid backlog
        tts = TTSWorker(rate=args.tts_rate, enabled=bool(args.tts))

        cap_stats = CaptureStats(status="skipped")
        pack_stats = ImagePackStats()

        # 1) get JPEG bytes
        jpg_bytes: bytes
        try:
            if args.use_camera:
                jpg_bytes, cap_stats = fetch_camera_jpeg(args.camera_url, timeout_s=args.camera_timeout)
                t_capture_end = now()
            else:
                img_path = resolve_path(manifest_path, row["image_path"])
                if not Path(img_path).exists():
                    log.log(f"[WARN] image not found: {img_path} (skip)")
                    tts.close()
                    continue
                t0 = now()
                jpg_bytes = Path(img_path).read_bytes()
                cap_stats.fetch_ms = (now() - t0) * 1000.0
                cap_stats.bytes_kb = len(jpg_bytes) / 1024.0
                cap_stats.status = "disk"
                t_capture_end = now()
        except Exception as e:
            log.log(f"\n[{i}] sample_id={sid}  [CAPTURE ERROR] {e}")
            tts.close()
            continue

        # 2) pack into data_url
        try:
            if args.pack_mode == "raw":
                data_url, st_b64 = _b64_data_url_from_jpeg_bytes(jpg_bytes)
                pack_stats = st_b64
            else:
                data_url, pack_stats = pack_resize_reencode(
                    jpg_bytes,
                    max_edge=args.max_edge,
                    jpeg_quality=args.jpeg_quality,
                )
            t_pack_end = now()
        except Exception as e:
            log.log(f"\n[{i}] sample_id={sid}  [PACK ERROR] {e}")
            tts.close()
            continue

        # 3) send streaming request
        t_send = now()
        t_first_token: Optional[float] = None
        t_first_enqueue: Optional[float] = None

        buffer = ""
        flushed_once = False
        flush_wait_s = max(0.0, args.flush_wait_ms / 1000.0)
        flush_deadline = t_send + flush_wait_s

        full_answer = ""
        full_reasoning = ""

        log.log("\n" + "=" * 72)
        log.log(f"[{i+1}] sample_id={sid}")
        log.log(f"prompt={prompt}")
        if args.use_camera:
            log.log(f"[CAP] url={args.camera_url} status={cap_stats.status} fetch={cap_stats.fetch_ms:.1f}ms bytes≈{cap_stats.bytes_kb:.1f}KB")
        else:
            log.log(f"[CAP] source=disk read={cap_stats.fetch_ms:.1f}ms bytes≈{cap_stats.bytes_kb:.1f}KB")
        log.log(
            f"[IMG] mode={args.pack_mode} read={pack_stats.read_ms:.1f}ms resize={pack_stats.resize_ms:.1f}ms "
            f"jpeg={pack_stats.jpeg_ms:.1f}ms b64={pack_stats.b64_ms:.1f}ms payload≈{pack_stats.payload_kb:.0f}KB"
        )

        try:
            stream = client.chat.completions.create(
                model=args.model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                stream=True,
                extra_body={
                    # llama.cpp accepts this; harmless if ignored.
                    "enable_thinking": bool(args.enable_thinking),
                },
            )

            for chunk in stream:
                try:
                    delta = chunk.choices[0].delta
                except Exception:
                    continue

                part_ans, part_rea = pick_delta_text(delta)
                if (part_ans or part_rea) and (t_first_token is None):
                    t_first_token = now()
                    log.log(f"[TS] TTFT(send->token) {(t_first_token - t_send)*1000.0:.1f} ms")
                    log.log(f"[TS] user->token       {(t_first_token - t_user_done)*1000.0:.1f} ms")

                if part_rea:
                    full_reasoning += part_rea
                if part_ans:
                    full_answer += part_ans

                # feed only answer text to TTS by default; but if answer is empty for long,
                # you can choose to speak reasoning by changing this.
                feed_text = part_ans
                if not feed_text:
                    # if model streams only reasoning first, we still want *some* early audio
                    # to reflect what the user hears. Keep it minimal.
                    feed_text = part_rea

                if not feed_text:
                    continue

                buffer += feed_text

                # Flush conditions:
                # A) punctuation in buffer
                # B) buffer is long enough
                # C) no punctuation arrives quickly (deadline)
                should_flush = False
                if PUNCT_RE.search(buffer):
                    should_flush = True
                elif len(buffer) >= args.flush_min_chars:
                    should_flush = True
                elif (not flushed_once) and (now() >= flush_deadline) and buffer.strip():
                    should_flush = True

                if not should_flush:
                    continue

                # split by punctuation to keep natural phrasing
                parts = SPLIT_RE.split(buffer)
                if len(parts) <= 1:
                    to_speak = buffer
                    buffer = ""
                else:
                    to_speak = "".join(parts[:-1])
                    buffer = parts[-1]

                to_speak = sanitize_for_tts(to_speak)
                if to_speak:
                    if t_first_enqueue is None:
                        t_first_enqueue = now()
                        log.log(f"[TS] first_enqueue   {(t_first_enqueue - t_user_done)*1000.0:.1f} ms")
                    tts.speak(to_speak)
                    flushed_once = True

        except Exception as e:
            log.log(f"[ERROR] LLM stream failed: {e}")

        # Flush remaining buffer
        rem = sanitize_for_tts(buffer)
        if rem:
            if t_first_enqueue is None:
                t_first_enqueue = now()
                log.log(f"[TS] first_enqueue   {(t_first_enqueue - t_user_done)*1000.0:.1f} ms")
            tts.speak(rem)

        # Wait a bit for TTS to trigger first audio
        t_audio = None
        if tts.wait_first_audio(timeout_s=2.5):
            t_audio = tts.first_audio_time

        # metrics
        m = LatencyMetrics()
        m.user_to_capture_ms = (t_capture_end - t_user_done) * 1000.0
        m.user_to_pack_ms = (t_pack_end - t_user_done) * 1000.0
        m.user_to_send_ms = (t_send - t_user_done) * 1000.0

        if t_first_token is not None:
            m.send_to_token_ms = (t_first_token - t_send) * 1000.0
            m.user_to_token_ms = (t_first_token - t_user_done) * 1000.0

        if t_first_enqueue is not None:
            m.user_to_first_enqueue_ms = (t_first_enqueue - t_user_done) * 1000.0

        if t_audio is not None:
            m.user_to_audio_ms = (t_audio - t_user_done) * 1000.0
            if t_first_token is not None:
                m.token_to_audio_ms = (t_audio - t_first_token) * 1000.0

        m.status = "PASS(<2s)" if (m.user_to_audio_ms and m.user_to_audio_ms < 2000.0) else "FAIL"

        log.log(
            f"[PERF] user->cap={m.user_to_capture_ms:.0f}ms | user->send={m.user_to_send_ms:.0f}ms | "
            f"send->token={m.send_to_token_ms:.0f}ms | user->audio={m.user_to_audio_ms:.0f}ms | "
            f"token->audio={m.token_to_audio_ms:.0f}ms | {m.status}"
        )

        # write row
        rows_out.append({
            "sample_id": sid,
            "prompt": prompt,
            "use_camera": int(bool(args.use_camera)),
            "camera_url": args.camera_url if args.use_camera else "",
            "camera_fetch_ms": cap_stats.fetch_ms,
            "camera_bytes_kb": cap_stats.bytes_kb,
            "pack_mode": args.pack_mode,
            "pack_read_ms": pack_stats.read_ms,
            "pack_resize_ms": pack_stats.resize_ms,
            "pack_jpeg_ms": pack_stats.jpeg_ms,
            "pack_b64_ms": pack_stats.b64_ms,
            "payload_kb": pack_stats.payload_kb,
            "ttft_send_to_token_ms": m.send_to_token_ms,
            "user_to_token_ms": m.user_to_token_ms,
            "user_to_audio_ms": m.user_to_audio_ms,
            "token_to_audio_ms": m.token_to_audio_ms,
            "user_to_capture_ms": m.user_to_capture_ms,
            "user_to_pack_ms": m.user_to_pack_ms,
            "status": m.status,
            "pred_answer": full_answer,
            "pred_reasoning": full_reasoning,
        })

        # avoid backlogging audio across samples
        tts.drain(timeout_s=10.0)
        tts.close()

    # Save CSV
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows_out).to_csv(out_path, index=False, encoding="utf-8-sig")
    log.log("\nDone.")
    log.log(f"Wrote: {out_path}")
    log.log(f"Wrote: {log_path}")
    log.close()


if __name__ == "__main__":
    main()
