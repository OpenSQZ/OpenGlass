#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import base64
import io
import queue
import sys
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict
from urllib.parse import urlparse
import os

import requests
from PIL import Image

import numpy as np
try:
    from faster_whisper import WhisperModel
except Exception:
    WhisperModel = None

try:
    import websocket
except ImportError:
    websocket = None

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

try:
    import pyttsx3
except Exception:
    pyttsx3 = None


def now() -> float:
    return time.perf_counter()


os.environ["NO_PROXY"] = "127.0.0.1,localhost,0.0.0.0"
os.environ["no_proxy"] = "127.0.0.1,localhost,0.0.0.0"
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)


BENCH_SYSTEM_PROMPT = (
    "You are a blind glasses assistant."
    "Output only one sentence for the final answer, do not explain the process, do not use newlines."
    "Do not start with \"I need/Let me/First/Analyze/Next\"."
)


@dataclass
class ImagePackStats:
    read_ms: float = 0.0
    rotate_ms: float = 0.0
    resize_ms: float = 0.0
    jpeg_ms: float = 0.0
    b64_ms: float = 0.0
    payload_kb: float = 0.0
    w: int = 0
    h: int = 0
    total_pack_ms: float = 0.0


_ROTATE_MAP = {
    0: None,
    90: Image.Transpose.ROTATE_270,   # 90° clockwise
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,    # 270° clockwise = 90° counter-clockwise
}


def pack_resize_reencode(
    jpg_bytes: bytes,
    max_edge: int = 896,
    jpeg_quality: int = 85,
    rotate_cw: int = 0,
) -> Tuple[str, ImagePackStats]:
    """Decode -> optional rotate -> optional resize -> re-encode JPEG -> base64."""
    st = ImagePackStats()
    t_pack_start = now()

    t0 = now()
    img = Image.open(io.BytesIO(jpg_bytes))
    st.read_ms = (now() - t0) * 1000.0

    tr = now()
    transpose_op = _ROTATE_MAP.get(rotate_cw % 360)
    if transpose_op is not None:
        img = img.transpose(transpose_op)
    st.rotate_ms = (now() - tr) * 1000.0

    st.w, st.h = img.size

    t1 = now()
    if max(img.size) > max_edge:
        ratio = max_edge / max(img.size)
        new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
    st.resize_ms = (now() - t1) * 1000.0

    t2 = now()
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
    out = buf.getvalue()
    st.jpeg_ms = (now() - t2) * 1000.0

    t3 = now()
    b64 = base64.b64encode(out).decode("utf-8")
    st.b64_ms = (now() - t3) * 1000.0
    st.payload_kb = len(b64) / 1024.0

    try:
        img2 = Image.open(io.BytesIO(out))
        st.w, st.h = img2.size
    except Exception:
        pass

    st.total_pack_ms = (now() - t_pack_start) * 1000.0
    return f"data:image/jpeg;base64,{b64}", st


class TTSWorker:
    """Sentence-level streaming TTS on PC."""

    def __init__(self, rate: int = 180):
        self.rate = rate
        self.q: "queue.Queue[Optional[str]]" = queue.Queue()
        self.first_audio_event = threading.Event()
        self.first_audio_time: Optional[float] = None
        self.stop_event = threading.Event()

        self._use_sapi = False
        try:
            import pythoncom as _pc        # noqa: F401
            import win32com.client as _wc  # noqa: F401
            self._use_sapi = True
            print("[TTS] Backend: Windows SAPI5 (win32com)")
        except ImportError:
            if pyttsx3 is None:
                raise RuntimeError(
                    "No TTS backend. Install pywin32 or pyttsx3")
            print("[TTS] Backend: pyttsx3")

        self._sapi_rate = max(-10, min(10, (rate - 200) // 20))

        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        if self._use_sapi:
            self._run_sapi()
        else:
            self._run_pyttsx3()

    def _run_sapi(self):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            speaker.Rate = self._sapi_rate
            while not self.stop_event.is_set():
                item = self.q.get()
                try:
                    if item is None:
                        return
                    if not self.first_audio_event.is_set():
                        self.first_audio_time = now()
                        self.first_audio_event.set()
                    t0 = now()
                    speaker.Speak(item)
                    dur_ms = (now() - t0) * 1000
                    print(f"[TTS] Spoke ({dur_ms:.0f}ms): "
                          f"{item[:30]}{'...' if len(item)>30 else ''}")
                except Exception as e:
                    print(f"[TTS] WARNING: {e}")
                finally:
                    self.q.task_done()
        finally:
            pythoncom.CoUninitialize()

    def _run_pyttsx3(self):
        self._engine = pyttsx3.init()
        try:
            self._engine.setProperty("rate", self.rate)
        except Exception:
            pass
        while not self.stop_event.is_set():
            item = self.q.get()
            try:
                if item is None:
                    return
                if not self.first_audio_event.is_set():
                    self.first_audio_time = now()
                    self.first_audio_event.set()
                self._engine.say(item)
                self._engine.runAndWait()
            except Exception as e:
                print(f"[TTS] WARNING: {e}")
            finally:
                self.q.task_done()

    def speak(self, text: str) -> None:
        if text and text.strip():
            self.q.put(text.strip())

    def wait_first_audio(self, timeout_s: float = 30.0) -> bool:
        return self.first_audio_event.wait(timeout=timeout_s)

    def get_first_audio_time(self) -> Optional[float]:
        return self.first_audio_time

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
        self.stop_event.set()
        try:
            self.q.put(None)
        except Exception:
            pass
        try:
            self.thread.join(timeout=2.0)
        except Exception:
            pass


def derive_http_base(ws_url: str) -> str:
    parsed = urlparse(ws_url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    if port and port != 80:
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


class ESP32AudioRecorder:
    """Persistent WS with background drain thread."""

    def __init__(self, mic_ws_url: str, esp_base_url: str,
                 samplerate: int = 16000):
        self.mic_ws_url = mic_ws_url
        self.esp_base_url = esp_base_url
        self.samplerate = samplerate

        self._ws: Optional["websocket.WebSocket"] = None
        self._alive = False
        self._recording = False
        self._lock = threading.Lock()
        self._pcm_buf = bytearray()
        self._thread: Optional[threading.Thread] = None
        self._ws_error: Optional[str] = None

    def connect(self) -> None:
        if websocket is None:
            raise RuntimeError("websocket-client not installed")

        begin_url = f"{self.esp_base_url}/audio_begin"
        print(f"[ESP32_MIC] Requesting {begin_url} ...")
        resp = requests.get(begin_url, timeout=10)
        resp.raise_for_status()
        print(f"[ESP32_MIC] audio_begin OK: {resp.text}")

        time.sleep(0.3)

        print(f"[ESP32_MIC] Connecting to {self.mic_ws_url} ...")
        ws = websocket.WebSocket()
        ws.settimeout(5.0)
        ws.connect(self.mic_ws_url)
        self._ws = ws
        self._alive = True
        print("[ESP32_MIC] WebSocket connected (persistent)")

        self._thread = threading.Thread(target=self._recv_loop,
                                        name="ws_drain", daemon=True)
        self._thread.start()
        print("[ESP32_MIC] Background drain thread started")

    def _recv_loop(self) -> None:
        consecutive_errors = 0
        while self._alive:
            try:
                self._ws.settimeout(1.0)
                data = self._ws.recv()

                if isinstance(data, bytes) and len(data) > 0:
                    consecutive_errors = 0
                    if self._recording:
                        with self._lock:
                            self._pcm_buf.extend(data)
                elif data is None or \
                     (isinstance(data, bytes) and len(data) == 0):
                    self._ws_error = "WS closed by ESP32"
                    break

            except websocket.WebSocketTimeoutException:
                continue
            except websocket.WebSocketConnectionClosedException:
                self._ws_error = "WS connection closed"
                break
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= 100:
                    self._ws_error = f"Too many errors: {e}"
                    break
                time.sleep(0.01)
                continue

        self._alive = False
        print(f"[ESP32_MIC] Drain thread exited"
              f"{': ' + self._ws_error if self._ws_error else ''}")

    # ------------------------------------------------------------------
    # Fixed duration recording (v3 compatible)
    # ------------------------------------------------------------------
    def record(self, record_sec: float) -> Tuple[bytes, float, int]:
        if not self._alive:
            err = self._ws_error or "drain thread not running"
            raise RuntimeError(f"Recorder dead — {err}")

        target_bytes = int(record_sec * self.samplerate * 2)

        with self._lock:
            self._pcm_buf.clear()
        self._recording = True

        t0 = now()
        timeout_sec = record_sec + 3.0
        print("[ESP32_MIC] Recording (fixed)...")

        while True:
            if not self._alive:
                break
            with self._lock:
                collected = len(self._pcm_buf)
            if collected >= target_bytes:
                break
            if now() - t0 > timeout_sec:
                print(f"[ESP32_MIC] Timeout after {now() - t0:.1f}s")
                break
            time.sleep(0.02)

        self._recording = False
        record_ms = (now() - t0) * 1000.0

        with self._lock:
            pcm_data = bytes(self._pcm_buf[:target_bytes])

        print(f"[ESP32_MIC] Received {len(pcm_data)} bytes in {record_ms:.0f}ms "
              f"({len(pcm_data) / 1024:.1f} KB, "
              f"~{len(pcm_data) / 2 / self.samplerate:.2f}s audio)")

        if len(pcm_data) < 100:
            raise RuntimeError(
                f"Recording got only {len(pcm_data)} bytes"
            )

        wav_buf = io.BytesIO()
        with wave.open(wav_buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.samplerate)
            wf.writeframes(bytes(pcm_data))
        return wav_buf.getvalue(), record_ms, len(pcm_data)

    # ------------------------------------------------------------------
    # VAD recording: auto-detect speech end
    # ------------------------------------------------------------------
    def record_vad(
        self,
        max_sec: float = 6.0,
        silence_ms: float = 700.0,
        min_speech_ms: float = 300.0,
        energy_threshold: float = 0.0,
        chunk_ms: float = 30.0,
    ) -> Tuple[bytes, float, int, Dict]:
        """
        Energy-based VAD recording.

        Flow:
          CALIBRATE (0.3s) -> WAIT for speech -> IN_SPEECH -> silence >= silence_ms -> STOP

        Returns: (wav_bytes, record_wall_ms, pcm_byte_count, vad_info_dict)
        """
        if not self._alive:
            err = self._ws_error or "drain thread not running"
            raise RuntimeError(f"Recorder dead — {err}")

        chunk_samples = int(chunk_ms / 1000.0 * self.samplerate)
        chunk_bytes = chunk_samples * 2

        calibration_sec = 0.3
        calibration_chunks = max(1, int(calibration_sec * 1000.0 / chunk_ms))
        silence_chunks_needed = max(1, int(silence_ms / chunk_ms))
        min_speech_chunks = max(1, int(min_speech_ms / chunk_ms))

        with self._lock:
            self._pcm_buf.clear()
        self._recording = True

        t0 = now()
        read_pos = 0

        # calibration state
        cal_rms_list: List[float] = []
        calibrated = (energy_threshold > 0)
        threshold = energy_threshold if energy_threshold > 0 else 500.0

        # VAD state
        speech_started = False
        speech_chunk_count = 0
        silence_chunk_count = 0

        stop_reason = "max_time"

        print("[ESP32_MIC] Recording (VAD)...")

        while True:
            if not self._alive:
                stop_reason = "ws_dead"
                break

            elapsed = now() - t0
            if elapsed > max_sec:
                stop_reason = "max_time"
                break

            with self._lock:
                buf_len = len(self._pcm_buf)

            processed_any = False
            while read_pos + chunk_bytes <= buf_len:
                processed_any = True
                with self._lock:
                    chunk = bytes(self._pcm_buf[read_pos:read_pos + chunk_bytes])
                read_pos += chunk_bytes

                samples = np.frombuffer(chunk, dtype=np.int16)
                rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))

                # --- calibration phase ---
                if not calibrated:
                    cal_rms_list.append(rms)
                    if len(cal_rms_list) >= calibration_chunks:
                        bg_mean = sum(cal_rms_list) / len(cal_rms_list)
                        threshold = max(bg_mean * 3.0, 300.0)
                        threshold = min(threshold, 5000.0)
                        calibrated = True
                        print(f"[VAD] Calibrated: bg_rms={bg_mean:.0f}, "
                              f"threshold={threshold:.0f}")
                    continue

                # --- VAD logic ---
                if rms > threshold:
                    if not speech_started:
                        speech_started = True
                        speech_at = read_pos / (self.samplerate * 2)
                        print(f"[VAD] Speech start at {speech_at:.2f}s "
                              f"(RMS={rms:.0f})")
                    speech_chunk_count += 1
                    silence_chunk_count = 0
                elif speech_started:
                    silence_chunk_count += 1

                if (speech_started
                        and speech_chunk_count >= min_speech_chunks
                        and silence_chunk_count >= silence_chunks_needed):
                    stop_reason = "vad_silence"
                    break

            if stop_reason == "vad_silence":
                break

            if not processed_any:
                time.sleep(0.015)

        self._recording = False
        record_ms = (now() - t0) * 1000.0

        with self._lock:
            pcm_data = bytes(self._pcm_buf)

        audio_sec = len(pcm_data) / 2.0 / self.samplerate
        print(f"[ESP32_MIC] VAD: {len(pcm_data)} bytes in {record_ms:.0f}ms "
              f"({len(pcm_data)/1024:.1f} KB, ~{audio_sec:.2f}s audio) "
              f"[{stop_reason}]")

        if len(pcm_data) < 100:
            raise RuntimeError(
                f"Recording got only {len(pcm_data)} bytes")

        wav_buf = io.BytesIO()
        with wave.open(wav_buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.samplerate)
            wf.writeframes(pcm_data)

        vad_info: Dict = {
            "stop_reason": stop_reason,
            "speech_detected": speech_started,
            "threshold": threshold,
            "audio_sec": audio_sec,
        }
        return wav_buf.getvalue(), record_ms, len(pcm_data), vad_info

    def close(self) -> None:
        self._alive = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        try:
            requests.get(f"{self.esp_base_url}/audio_end", timeout=5)
        except Exception:
            pass


def transcribe_faster_whisper(
    wav_bytes: bytes,
    model_name: str = "tiny",
    device: str = "cpu",
    compute_type: str = "int8",
    language: Optional[str] = "zh",
) -> Tuple[str, float]:
    if WhisperModel is None:
        raise RuntimeError("faster-whisper not installed")

    if not hasattr(transcribe_faster_whisper, "_model") or \
       getattr(transcribe_faster_whisper, "_model_name", None) != model_name:
        print(f"[ASR] Loading faster-whisper model '{model_name}'...")
        transcribe_faster_whisper._model = WhisperModel(model_name, device=device, compute_type=compute_type)
        transcribe_faster_whisper._model_name = model_name

    t0 = now()
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
    audio_i16 = np.frombuffer(frames, dtype=np.int16)
    audio_f32 = (audio_i16.astype(np.float32) / 32768.0)

    segments, info = transcribe_faster_whisper._model.transcribe(
        audio_f32, language=language, vad_filter=True, beam_size=1,
    )
    text_parts: List[str] = []
    for seg in segments:
        if seg.text:
            text_parts.append(seg.text.strip())
    asr_ms = (now() - t0) * 1000.0
    text = " ".join([t for t in text_parts if t])
    return text, asr_ms


def fetch_camera_jpeg(camera_url: str, timeout_s: float = 10.0) -> Tuple[bytes, float]:
    t0 = now()
    r = requests.get(camera_url, timeout=timeout_s)
    r.raise_for_status()
    ms = (now() - t0) * 1000.0
    return r.content, ms


def sentence_streamer(token_iter):
    """Yield sentences/clauses from a token stream."""
    buf = ""
    seps = set("。！？!?;；\n")
    for tok in token_iter:
        if not tok:
            continue
        buf += tok
        while True:
            cut = -1
            for i, ch in enumerate(buf):
                if ch in seps:
                    cut = i
                    break
            if cut >= 0:
                out = buf[: cut + 1].strip()
                buf = buf[cut + 1:]
                if out:
                    yield out
            else:
                break
    tail = buf.strip()
    if tail:
        yield tail


def vlm_warmup(client: "OpenAI", model: str) -> float:
    """Send a minimal dummy request to warm up model weights / KV cache."""
    tiny_img = Image.new("RGB", (8, 8), color=(128, 128, 128))
    buf = io.BytesIO()
    tiny_img.save(buf, format="JPEG", quality=50)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    img_url = f"data:image/jpeg;base64,{b64}"

    t0 = now()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "image_url", "image_url": {"url": img_url}},
                ],
            }],
            max_tokens=1,
            temperature=0.0,
            stream=False,
            extra_body={
                "chat_template_kwargs": {"enable_thinking": False}
            },
        )
        warmup_ms = (now() - t0) * 1000.0
        print(f"[WARMUP] VLM warmup OK ({warmup_ms:.0f}ms)")
        return warmup_ms
    except Exception as e:
        warmup_ms = (now() - t0) * 1000.0
        print(f"[WARMUP] VLM warmup failed ({warmup_ms:.0f}ms): {e}")
        return warmup_ms


def main():
    ap = argparse.ArgumentParser(
        description="AI Glasses v4 (VAD): ESP32 Mic -> ASR -> VLM -> TTS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example (VAD auto-truncate, default):
  python demo_asr_vlm_stream_tts_glasses_esp32mic_v4_vad.py ^
    --mic_ws ws://10.100.7.160/ws_audio ^
    --camera_url http://10.100.7.160/capture ^
    --openai_base http://127.0.0.1:8080/v1 ^
    --openai_model "ggml-model-Q4_K_M.gguf" ^
    --whisper_model tiny ^
    --max_edge 896

Example (fixed duration, consistent with v3):
  ... --no_vad --record_sec 3.0 ...
""")

    ap.add_argument("--mic_ws", type=str, required=True,
                    help="ESP32 WebSocket audio URL")
    ap.add_argument("--camera_url", type=str, required=True,
                    help="ESP32 camera capture URL")
    ap.add_argument("--max_edge", type=int, default=896)
    ap.add_argument("--jpeg_quality", type=int, default=85,
                    help="JPEG quality (benchmark default=85)")
    ap.add_argument("--openai_base", type=str, default="http://127.0.0.1:8080/v1")
    ap.add_argument("--openai_key", type=str, default="sk-no-key-required")
    ap.add_argument("--openai_model", type=str, default="MiniCPM-V",
                    help="Model name (benchmark default=MiniCPM-V)")
    ap.add_argument("--system_prompt", type=str, default=BENCH_SYSTEM_PROMPT,
                    help="System prompt (defaults to benchmark version)")
    ap.add_argument("--record_sec", type=float, default=6.0,
                    help="Max recording duration (safety limit in VAD mode, default 6.0)")
    ap.add_argument("--asr_lang", type=str, default="zh")
    ap.add_argument("--whisper_model", type=str, default="tiny")
    ap.add_argument("--whisper_device", type=str, default="cpu", choices=["cpu", "cuda"])
    ap.add_argument("--whisper_compute", type=str, default="int8")
    ap.add_argument("--tts_rate", type=int, default=180)
    ap.add_argument("--no_mic", action="store_true",
                    help="Disable mic; use keyboard input instead")
    ap.add_argument("--rounds", type=int, default=0,
                    help="Auto run rounds (0=interactive mode, >0=auto mode)")
    ap.add_argument("--cam_delay", type=float, default=0.0,
                    help="Seconds to wait for camera after recording (default 0)")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                    help="Camera frame clockwise rotation angle (default 0, use 90 for side-mounted glasses)")
    ap.add_argument("--no_warmup", action="store_true",
                    help="Skip VLM warmup (for debugging)")

    # --- VAD Parameters ---
    ap.add_argument("--no_vad", action="store_true",
                    help="Disable VAD, use fixed duration recording (consistent with v3 behavior)")
    ap.add_argument("--vad_silence_ms", type=float, default=700.0,
                    help="Silence duration threshold after speech ends (ms), stop recording if exceeded (default 700)")
    ap.add_argument("--vad_min_speech_ms", type=float, default=300.0,
                    help="Minimum speech duration (ms), prevent noise false triggers (default 300)")
    ap.add_argument("--vad_threshold", type=float, default=0.0,
                    help="RMS energy threshold, 0=auto-calibration (default 0)")

    args = ap.parse_args()

    use_vad = not args.no_vad

    if OpenAI is None:
        print("Missing dependency: openai", file=sys.stderr)
        sys.exit(2)
    if websocket is None and not args.no_mic:
        print("Missing dependency: websocket-client", file=sys.stderr)
        sys.exit(2)

    esp_base_url = derive_http_base(args.mic_ws)

    print(f"[CONFIG] ESP32 base URL: {esp_base_url}")
    print(f"[CONFIG] Camera URL:     {args.camera_url}")
    print(f"[CONFIG] WS Audio URL:   {args.mic_ws}")
    print(f"[CONFIG] VLM server:     {args.openai_base}")
    print(f"[CONFIG] VLM model:      {args.openai_model}")
    print(f"[CONFIG] Max edge:       {args.max_edge}")
    print(f"[CONFIG] JPEG quality:   {args.jpeg_quality}")
    print(f"[CONFIG] Rotate CW:      {args.rotate}°"
          f"{'  (camera sideways correction)' if args.rotate else ''}")
    if use_vad:
        print(f"[CONFIG] VAD mode:       ON  (silence={args.vad_silence_ms}ms, "
              f"min_speech={args.vad_min_speech_ms}ms, "
              f"threshold={'auto' if args.vad_threshold == 0 else args.vad_threshold})")
        print(f"[CONFIG] Record max:     {args.record_sec}s  (VAD safety limit)")
    else:
        print(f"[CONFIG] VAD mode:       OFF (fixed duration)")
        print(f"[CONFIG] Record sec:     {args.record_sec}")
    print(f"[CONFIG] System prompt:  {args.system_prompt[:60]}...")
    print(f"[CONFIG] enable_thinking: False  (benchmark-aligned)")
    print(f"[CONFIG] max_tokens=80, stop=['\\n'], temperature=0.0")

    client = OpenAI(base_url=args.openai_base, api_key=args.openai_key)
    tts = TTSWorker(rate=args.tts_rate)

    # ---- VLM Warmup ----
    if not args.no_warmup:
        print("\n[INIT] Warming up VLM (eliminates cold-start on Round 1)...")
        vlm_warmup(client, args.openai_model)

    # ESP32: Connectivity test & establish persistent WS audio connection
    recorder: Optional[ESP32AudioRecorder] = None
    print("\n[INIT] Testing ESP32 connectivity...")
    try:
        resp = requests.get(f"{esp_base_url}/status", timeout=5)
        print(f"[INIT] ESP32 /status OK (HTTP {resp.status_code})")
    except Exception as e:
        print(f"[INIT] WARNING: ESP32 /status failed: {e}")

    if not args.no_mic:
        recorder = ESP32AudioRecorder(args.mic_ws, esp_base_url)
        recorder.connect()

    # ASR Warmup
    if WhisperModel is not None:
        print("[INIT] Pre-loading ASR model...")
        t_asr_load = now()
        _dummy_wav = io.BytesIO()
        with wave.open(_dummy_wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00" * 3200)
        try:
            transcribe_faster_whisper(
                _dummy_wav.getvalue(),
                model_name=args.whisper_model,
                device=args.whisper_device,
                compute_type=args.whisper_compute,
                language=args.asr_lang if args.asr_lang else None,
            )
        except Exception:
            pass
        print(f"[INIT] ASR model loaded ({(now() - t_asr_load)*1000:.0f}ms)")

    mode_str = "VAD auto-stop" if use_vad else "fixed duration"
    print(f"\n=== AI Glasses v4 ({mode_str} + bench-aligned) ===")
    print("Optimizations: VLM Warmup | ASR+Camera Parallel | No Camera Wait | VAD Auto Truncation")
    print("VLM parameters are identical to eval_benchmark: enable_thinking=False, max_tokens=80, stop=['\\n']")
    print("Press Ctrl+C to exit.\n")

    round_idx = 0
    all_metrics: List[dict] = []
    pool = ThreadPoolExecutor(max_workers=2)

    try:
        while True:
            round_idx += 1

            if args.rounds > 0 and round_idx > args.rounds:
                break

            # ============================================
            # 1) Get user voice input
            # ============================================
            vad_info: Dict = {}

            if args.no_mic:
                user_text = input("Please enter your question (Enter to start inference): ").strip()
                asr_ms = 0.0
                rec_ms = 0.0
                audio_bytes = 0
                t_enter = now()
                t_post_record = now()

                print(f"[CAM] Fetching {args.camera_url} ...")
                try:
                    jpg, cap_ms = fetch_camera_jpeg(args.camera_url)
                except Exception as e:
                    print(f"[CAM] ERROR: {e} — retrying in 2s...")
                    time.sleep(2.0)
                    try:
                        jpg, cap_ms = fetch_camera_jpeg(args.camera_url)
                    except Exception as e2:
                        print(f"[CAM] Still failed: {e2}, skipping")
                        continue
                parallel_ms = (now() - t_post_record) * 1000.0
            else:
                if use_vad:
                    input(f"Press Enter to start recording (VAD auto truncation, max {args.record_sec:.0f}s) ...")
                else:
                    input(f"Press Enter to start recording {args.record_sec:.1f}s (ESP32 Mic) ...")

                t_enter = now()

                try:
                    if use_vad:
                        wav_bytes, rec_ms, audio_bytes, vad_info = recorder.record_vad(
                            max_sec=args.record_sec,
                            silence_ms=args.vad_silence_ms,
                            min_speech_ms=args.vad_min_speech_ms,
                            energy_threshold=args.vad_threshold,
                        )
                    else:
                        wav_bytes, rec_ms, audio_bytes = recorder.record(
                            record_sec=args.record_sec,
                        )
                except RuntimeError as e:
                    print(f"[ERROR] Recording failed: {e}")
                    continue

                if audio_bytes < 100:
                    print("[ASR] Received too little audio data, skipping this round")
                    continue

                # ==================================================
                # ASR + Camera Parallel
                # ==================================================
                if args.cam_delay > 0:
                    time.sleep(args.cam_delay)

                t_post_record = now()
                print(f"[PARALLEL] ASR + Camera fetch starting...")

                asr_future = pool.submit(
                    transcribe_faster_whisper,
                    wav_bytes,
                    model_name=args.whisper_model,
                    device=args.whisper_device,
                    compute_type=args.whisper_compute,
                    language=args.asr_lang if args.asr_lang else None,
                )
                cam_future = pool.submit(
                    fetch_camera_jpeg, args.camera_url
                )

                try:
                    user_text, asr_ms = asr_future.result(timeout=30)
                except Exception as e:
                    print(f"[ASR] ERROR: {e}")
                    try:
                        cam_future.result(timeout=15)
                    except Exception:
                        pass
                    continue

                if not user_text:
                    try:
                        cam_future.result(timeout=15)
                    except Exception:
                        pass
                    print("[ASR] No text recognized, skipping")
                    continue

                try:
                    jpg, cap_ms = cam_future.result(timeout=15)
                except Exception as e:
                    print(f"[CAM] ERROR: {e} — retrying...")
                    try:
                        jpg, cap_ms = fetch_camera_jpeg(args.camera_url)
                    except Exception as e2:
                        print(f"[CAM] Still failed: {e2}, skipping")
                        continue

                parallel_ms = (now() - t_post_record) * 1000.0
                print(f"[PARALLEL] Done in {parallel_ms:.0f}ms "
                      f"(ASR={asr_ms:.0f}ms, Cam={cap_ms:.0f}ms, "
                      f"saved ~{asr_ms + cap_ms - parallel_ms:.0f}ms)")

            print(f"[ASR] text: {user_text}")

            # ============================================
            # 2) Image packing
            # ============================================
            img_b64, st = pack_resize_reencode(
                jpg, max_edge=args.max_edge, jpeg_quality=args.jpeg_quality,
                rotate_cw=args.rotate,
            )

            # ============================================
            # 3) VLM streaming — benchmark-aligned
            # ============================================
            messages = [
                {"role": "system", "content": args.system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": img_b64}},
                    ],
                },
            ]

            tts.first_audio_event.clear()
            tts.first_audio_time = None

            t_vlm_start = now()
            first_token_t = None
            collected = ""

            stream = client.chat.completions.create(
                model=args.openai_model,
                messages=messages,
                max_tokens=80,
                temperature=0.0,
                stop=["\n"],
                stream=True,
                extra_body={
                    "chat_template_kwargs": {"enable_thinking": False}
                },
            )

            def token_iter():
                nonlocal first_token_t, collected
                for event in stream:
                    delta = getattr(event.choices[0].delta, "content", None) or ""
                    if not delta:
                        delta = getattr(event.choices[0].delta, "reasoning_content", None) or ""
                    if delta:
                        if first_token_t is None:
                            first_token_t = now()
                        collected += delta
                        yield delta

            for sent in sentence_streamer(token_iter()):
                print(sent, end="", flush=True)
                tts.speak(sent)

            t_vlm_end = now()
            vlm_total_ms = (t_vlm_end - t_vlm_start) * 1000.0
            ttft_ms = (first_token_t - t_vlm_start) * 1000.0 if first_token_t else -1.0

            tts.wait_first_audio(timeout_s=20.0)
            tts_first_audio_t = tts.get_first_audio_time()
            tts.drain(timeout_s=30)

            if tts_first_audio_t is not None and first_token_t is not None:
                tts_ttfa_ms = (tts_first_audio_t - first_token_t) * 1000.0
            else:
                tts_ttfa_ms = -1.0

            first_audio_est_ms = cap_ms + st.total_pack_ms + ttft_ms + max(tts_ttfa_ms, 0)
            e2e_from_rec_ms = parallel_ms + st.total_pack_ms + ttft_ms + max(tts_ttfa_ms, 0)
            e2e_from_enter_ms = (
                rec_ms + parallel_ms + st.total_pack_ms + ttft_ms + max(tts_ttfa_ms, 0)
            )

            # ============================================
            # 4) Metrics
            # ============================================
            metrics = {
                "round": round_idx,
                "rec_ms": rec_ms if not args.no_mic else 0,
                "audio_bytes": audio_bytes if not args.no_mic else 0,
                "asr_ms": asr_ms,
                "capture_ms": cap_ms,
                "parallel_ms": parallel_ms,
                "pack_total_ms": st.total_pack_ms,
                "pack_read_ms": st.read_ms,
                "pack_resize_ms": st.resize_ms,
                "pack_jpeg_ms": st.jpeg_ms,
                "pack_b64_ms": st.b64_ms,
                "image_w": st.w,
                "image_h": st.h,
                "payload_kb": st.payload_kb,
                "vlm_ttft_ms": ttft_ms,
                "vlm_total_ms": vlm_total_ms,
                "tts_ttfa_ms": tts_ttfa_ms,
                "first_audio_est_ms": first_audio_est_ms,
                "e2e_from_rec_ms": e2e_from_rec_ms,
                "e2e_from_enter_ms": e2e_from_enter_ms,
                "vad_stop": vad_info.get("stop_reason", "n/a"),
                "vad_audio_sec": vad_info.get("audio_sec", 0),
                "user_text": user_text,
                "pred_text": collected.strip(),
            }
            all_metrics.append(metrics)

            stop_tag = ""
            if vad_info:
                stop_tag = f"  [VAD: {vad_info.get('stop_reason', '?')}]"

            print(f"\n\n{'='*60}")
            print(f"  Round {round_idx} Metrics (v4 {'VAD' if use_vad else 'fixed'}){stop_tag}")
            print(f"{'='*60}")
            if not args.no_mic:
                audio_sec_str = f" (~{vad_info['audio_sec']:.2f}s)" if vad_info else ""
                print(f"  Recording:        {rec_ms:>7.0f} ms | "
                      f"{audio_bytes} bytes{audio_sec_str}")
            print(f"  ASR:              {asr_ms:>7.0f} ms")
            print(f"  Wi-Fi capture:    {cap_ms:>7.0f} ms")
            print(f"  ASR+Cam parallel: {parallel_ms:>7.0f} ms  "
                  f"(saved ~{asr_ms + cap_ms - parallel_ms:.0f}ms)")
            rot_str = f"rot {st.rotate_ms:.1f} + " if args.rotate else ""
            print(f"  Image pack:       {st.total_pack_ms:>7.1f} ms  "
                  f"(read {st.read_ms:.1f} + {rot_str}resize {st.resize_ms:.1f} "
                  f"+ jpeg {st.jpeg_ms:.1f} + b64 {st.b64_ms:.1f})")
            print(f"  Image:         {st.w}x{st.h} | payload ~ {st.payload_kb:.1f} KB")
            print(f"  VLM TTFT:         {ttft_ms:>7.0f} ms  ← Comparable with benchmark")
            print(f"  VLM total:        {vlm_total_ms:>7.0f} ms")
            print(f"  TTS TTFA:         {tts_ttfa_ms:>7.0f} ms  (token -> first audio)")
            print(f"  First Audio (est): {first_audio_est_ms:>7.0f} ms  "
                  f"(cap+pack+TTFT+TTS, comparable with v2/v3)")
            print(f"  Post-rec → First Audio: {e2e_from_rec_ms:>7.0f} ms  "
                  f"(parallel+pack+TTFT+TTS)")
            print(f"  Enter → First Audio:    {e2e_from_enter_ms:>7.0f} ms  "
                  f"(rec+parallel+pack+TTFT+TTS)")
            print(f"{'='*60}\n")

    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        pool.shutdown(wait=False)
        try:
            if recorder:
                recorder.close()
        except Exception:
            pass
        try:
            tts.close()
        except Exception:
            pass

    # ============================================
    # Summary stats
    # ============================================
    if all_metrics:
        print(f"\n{'='*60}")
        print(f"  Summary ({len(all_metrics)} rounds, "
              f"{'VAD' if use_vad else 'fixed'})")
        print(f"{'='*60}")

        valid = [m for m in all_metrics if m["vlm_ttft_ms"] > 0]
        if valid:
            def p50(arr):
                s = sorted(arr)
                return s[len(s) // 2]

            def mean(arr):
                return sum(arr) / len(arr)

            headers = [
                ("Recording", [m["rec_ms"] for m in valid]),
                ("ASR", [m["asr_ms"] for m in valid]),
                ("Wi-Fi capture", [m["capture_ms"] for m in valid]),
                ("ASR+Cam parallel", [m["parallel_ms"] for m in valid]),
                ("Image pack", [m["pack_total_ms"] for m in valid]),
                ("VLM TTFT", [m["vlm_ttft_ms"] for m in valid]),
                ("VLM total", [m["vlm_total_ms"] for m in valid]),
                ("First Audio (est)", [m["first_audio_est_ms"] for m in valid]),
                ("Post-rec → First Audio", [m["e2e_from_rec_ms"] for m in valid]),
                ("Enter → First Audio", [m["e2e_from_enter_ms"] for m in valid]),
            ]

            print(f"  {'Metric':<20} {'Mean':>10} {'P50':>10} {'Min':>10} {'Max':>10}")
            print(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")

            for label, arr in headers:
                print(f"  {label:<20} {mean(arr):>9.0f}ms {p50(arr):>9.0f}ms "
                      f"{min(arr):>9.0f}ms {max(arr):>9.0f}ms")

            if use_vad:
                vad_stops = [m["vad_stop"] for m in valid]
                silence_count = vad_stops.count("vad_silence")
                maxtime_count = vad_stops.count("max_time")
                print(f"\n  VAD stats: {silence_count} silence-stop, "
                      f"{maxtime_count} max-time-stop")

        print(f"{'='*60}")


if __name__ == "__main__":
    main()
