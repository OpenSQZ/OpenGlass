from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import time
from typing import Any, Dict, Optional

import requests

# Bypass VPN proxy to ensure local llama.cpp server requests do not go through proxy
# Set at module load time to avoid global VPN affecting local requests
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,0.0.0.0")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost,0.0.0.0")
# Remove potentially existing proxy settings
for proxy_var in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
    os.environ.pop(proxy_var, None)

# Optional dependencies, import on demand
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import edge_tts
    HAS_EDGE_TTS = True
except ImportError:
    HAS_EDGE_TTS = False

# pyttsx3 local TTS (consistent with v6 script)
try:
    import pyttsx3
    import queue
    import threading
    HAS_PYTTSX3 = True
except ImportError:
    HAS_PYTTSX3 = False

# OpenAI SDK (consistent with v3, solves streaming parsing issues)
try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

# Google Generative AI SDK (Gemini)
try:
    import google.generativeai as genai
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False


def _load_image_base64(image_path: str, resize_896: bool = False, max_edge: int = 896) -> str:
    """
    Load image and convert to base64, optional resize
    
    Args:
    - resize_896: Whether to enable resize (backward compatibility)
    - max_edge: Maximum edge length (default 896)
      - 896: MiniCPM-V uses 3 slices
      - 448: MiniCPM-V uses 1 slice (faster but lower quality)
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image not found: {image_path}")
    
    if resize_896 and HAS_PIL:
        img = Image.open(image_path)
        # Scale to max edge while maintaining aspect ratio
        max_side = max(img.size)
        if max_side > max_edge:
            ratio = max_edge / max_side
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(new_size, Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    else:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")


def _extract_text_from_response(data: Dict[str, Any]) -> str:
    """
    Robustly extract text from response, compatible with multiple formats (ref v3 implementation)
    Avoids "false empty output" problem: content may be None, list, or in other fields
    """
    try:
        choices = data.get("choices", [])
        if not choices:
            return ""
        
        msg = choices[0].get("message", {})
        if not msg:
            return ""
        
        # 1) Common: content is str
        content = msg.get("content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
        
        # 2) content could be list[{"type":"text","text":...}, ...]
        if isinstance(content, list):
            texts = []
            for part in content:
                if isinstance(part, dict):
                    texts.append(part.get("text", ""))
                elif isinstance(part, str):
                    texts.append(part)
            joined = "".join(texts).strip()
            if joined:
                return joined
        
        # 3) Some implementations put answer in reasoning
        for k in ("reasoning_content", "reasoning"):
            v = msg.get(k, None)
            if isinstance(v, str) and v.strip():
                return v.strip()
        
        # 4) tool_calls case: content being empty is common, return empty to let upper layer log debug
        return ""
    except Exception:
        return ""


# Non-thinking mode: prohibit fallback to reasoning_content (consistent with v3_nothink)
# When thinking is disabled, content should not be taken from reasoning_content
ALLOW_REASONING_FALLBACK = False


def _extract_text_from_openai_resp(resp) -> str:
    """
    Extract text from OpenAI SDK response (identical to v3_nothink)
    Compatible with various OpenAI SDK / llama.cpp returns, try to extract text
    
    Note: When ALLOW_REASONING_FALLBACK=False, content will not be taken from reasoning_content
    """
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

    # 3) Some servers put thoughts in reasoning_content.
    # For TTS/eval we usually do NOT want this (consistent with v3_nothink)
    if ALLOW_REASONING_FALLBACK:
        for k in ("reasoning_content", "reasoning"):
            v = md.get(k, None)
            if isinstance(v, str) and v.strip():
                return v

    # 4) tool_calls case: content being empty is common
    # Return empty here, let upper layer log debug
    return ""


def _strip_think_tags(text: str) -> str:
    """
    Strip MiniCPM-o <think>...</think> chain-of-thought tags.
    Keeps only the actual answer after </think>.
    If no <think> tags, returns text unchanged.
    """
    if not text:
        return text
    import re
    # Match <think>...</think> blocks (including newlines), remove them
    cleaned = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    # Fallback: unclosed <think> tag (generation truncated)
    cleaned = re.sub(r"<think>.*$", "", cleaned, flags=re.DOTALL)
    return cleaned.strip()


def _postprocess_one_sentence(text: str, max_chars: int = None) -> str:
    """Remove newlines, compress into one sentence (keep first sentence end), optional char limit (consistent with v3)"""
    import re
    t = (text or "").replace("\r", " ").replace("\n", " ")
    t = re.sub(r"\s+", " ", t).strip()

    # Keep only the first sentence (encountering period/question mark/exclamation mark)
    m = re.split(r"[。！？!?]", t)
    if m and m[0].strip():
        t = m[0].strip()

    if max_chars is not None and len(t) > max_chars:
        t = t[:max_chars].rstrip("，,;；:：")

    return t


# ============================================================================
# Cloud API Call Functions (Cloud-based baselines)
# ============================================================================

def _call_openai_gpt4o_streaming(
    prompt: str,
    img_b64: str,
    api_key: str = None,
    model: str = "gpt-4o",
    timeout: float = 60.0
) -> Dict[str, Any]:
    """
    Perform streaming VLM inference using OpenAI GPT-4o API
    Measure TTFT (Time To First Token)
    
    Returns {"pred_text": str, "ttft_ms": float, "total_ms": float}
    """
    if not HAS_OPENAI:
        return {
            "pred_text": "[ERROR] OpenAI SDK not installed",
            "ttft_ms": 0.0,
            "total_ms": 0.0,
            "error": "OpenAI SDK not installed"
        }
    
    api_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {
            "pred_text": "[ERROR] OPENAI_API_KEY not set",
            "ttft_ms": 0.0,
            "total_ms": 0.0,
            "error": "OPENAI_API_KEY not set"
        }
    
    t0 = time.perf_counter()
    ttft_ms = None
    pred_text = ""
    
    try:
        client = OpenAI(api_key=api_key, timeout=timeout)
        
        system_msg = (
            "You are a blind glasses assistant."
            "Output only one sentence for the final answer, do not explain the process, do not use newlines."
            "Do not start with 'I need/Let me/First/Analyze/Next'."
        )
        
        data_url = f"data:image/jpeg;base64,{img_b64}"
        
        stream = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_msg},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}}
                    ]
                }
            ],
            max_tokens=80,
            temperature=0.0,
            stream=True,
            timeout=timeout,
        )
        
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                if ttft_ms is None:
                    ttft_ms = (time.perf_counter() - t0) * 1000
                pred_text += content
        
        total_ms = (time.perf_counter() - t0) * 1000
        pred_text = _postprocess_one_sentence(pred_text, max_chars=80)
        
        if not pred_text.strip():
            pred_text = "[EMPTY]"
        
        return {
            "pred_text": pred_text,
            "ttft_ms": ttft_ms or total_ms,
            "total_ms": total_ms
        }
        
    except Exception as e:
        total_ms = (time.perf_counter() - t0) * 1000
        return {
            "pred_text": f"[ERROR] {str(e)[:100]}",
            "ttft_ms": total_ms,
            "total_ms": total_ms,
            "error": str(e)
        }


def _call_qwen_vl_streaming(
    prompt: str,
    img_b64: str,
    api_key: str = None,
    model: str = "qwen-vl-max",  # or qwen-vl-plus (cheaper)
    timeout: float = 60.0,
    max_retries: int = 3,
    retry_delay: float = 3.0
) -> Dict[str, Any]:
    """
    Perform streaming VLM inference using Alibaba Cloud Qwen-VL API
    Domestic API, no VPN required, lower latency
    
    Supported models:
    - qwen-vl-max (Strongest, recommended)
    - qwen-vl-plus (Cheaper)
    - qwen2.5-vl-72b-instruct (Latest)
    
    API Key: https://dashscope.console.aliyun.com/
    
    Returns {"pred_text": str, "ttft_ms": float, "total_ms": float}
    """
    if not HAS_OPENAI:
        return {
            "pred_text": "[ERROR] OpenAI SDK not installed (required for Qwen API)",
            "ttft_ms": 0.0,
            "total_ms": 0.0,
            "error": "OpenAI SDK not installed"
        }
    
    # Qwen API Key: Prioritize DASHSCOPE_API_KEY, then QWEN_API_KEY
    api_key = api_key or os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY")
    if not api_key:
        return {
            "pred_text": "[ERROR] DASHSCOPE_API_KEY not set",
            "ttft_ms": 0.0,
            "total_ms": 0.0,
            "error": "DASHSCOPE_API_KEY not set. Get it from https://dashscope.console.aliyun.com/"
        }
    
    # Qwen API uses OpenAI compatible format
    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    
    last_error = None
    
    for attempt in range(max_retries):
        t0 = time.perf_counter()
        ttft_ms = None
        pred_text = ""
        
        try:
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout
            )
            
            system_msg = (
                "You are a blind glasses assistant."
                "Output only one sentence for the final answer, do not explain the process, do not use newlines."
                "Do not start with 'I need/Let me/First/Analyze/Next'."
            )
            
            data_url = f"data:image/jpeg;base64,{img_b64}"
            
            stream = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}}
                        ]
                    }
                ],
                max_tokens=256,
                temperature=0.1,
                stream=True,
            )
            
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    if ttft_ms is None:
                        ttft_ms = (time.perf_counter() - t0) * 1000
                    pred_text += content
            
            total_ms = (time.perf_counter() - t0) * 1000
            pred_text = _postprocess_one_sentence(pred_text, max_chars=80)
            
            if not pred_text.strip():
                pred_text = "[EMPTY]"
            
            return {
                "pred_text": pred_text,
                "ttft_ms": ttft_ms or total_ms,
                "total_ms": total_ms,
                "retries": attempt
            }
            
        except Exception as e:
            last_error = str(e)
            error_str = str(e).lower()
            
            # Check if it's a rate limit error
            is_rate_limit = "429" in error_str or "rate" in error_str or "quota" in error_str or "limit" in error_str
            
            if is_rate_limit and attempt < max_retries - 1:
                wait_time = retry_delay * (attempt + 1)
                print(f"    ⚠️ Rate limit hit, waiting {wait_time:.1f}s before retry ({attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
                continue
            else:
                total_ms = (time.perf_counter() - t0) * 1000
                return {
                    "pred_text": f"[ERROR] {last_error[:100]}",
                    "ttft_ms": total_ms,
                    "total_ms": total_ms,
                    "error": last_error,
                    "retries": attempt
                }
    
    return {
        "pred_text": f"[ERROR] Max retries exceeded: {last_error[:100] if last_error else 'Unknown'}",
        "ttft_ms": 0.0,
        "total_ms": 0.0,
        "error": last_error,
        "retries": max_retries
    }


def _call_gemini_flash_streaming(
    prompt: str,
    img_b64: str,
    api_key: str = None,
    model: str = "gemini-2.0-flash",  # Latest available Flash model
    timeout: float = 60.0,
    max_retries: int = 3,
    retry_delay: float = 5.0
) -> Dict[str, Any]:
    """
    Perform streaming VLM inference using Google Gemini API
    Measure TTFT (Time To First Token)
    
    Supported models (Jan 2026):
    - gemini-2.0-flash - Stable version
    - gemini-2.5-flash (Recommended) - Latest version
    - gemini-2.5-pro (Strongest reasoning)
    
    Includes auto-retry mechanism: automatically waits and retries on 429 (Rate Limit) errors
    
    Returns {"pred_text": str, "ttft_ms": float, "total_ms": float}
    """
    if not HAS_GEMINI:
        return {
            "pred_text": "[ERROR] google-generativeai not installed",
            "ttft_ms": 0.0,
            "total_ms": 0.0,
            "error": "google-generativeai not installed"
        }
    
    api_key = api_key or os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {
            "pred_text": "[ERROR] GOOGLE_API_KEY not set",
            "ttft_ms": 0.0,
            "total_ms": 0.0,
            "error": "GOOGLE_API_KEY not set"
        }
    
    genai.configure(api_key=api_key)
    
    # Create model (outside retry loop to avoid repeated creation)
    # Note: temperature=0 on Gemini 2.5 often leads to truncation
    # Increasing max_output_tokens and setting temperature > 0 can improve this
    generation_config = genai.GenerationConfig(
        max_output_tokens=256,      # Increased from 80 to 256 to avoid truncation after thinking tokens
        temperature=0.1,            # Changed from 0.0 to 0.1 to avoid truncation issues at temperature=0
    )
    
    model_obj = genai.GenerativeModel(
        model_name=model,
        generation_config=generation_config,
        system_instruction=(
            "You are a blind glasses assistant."
            "Output only one sentence for the final answer, do not explain the process, do not use newlines."
            "Do not start with 'I need/Let me/First/Analyze/Next'."
        )
    )
    
    # Decode image (outside retry loop to avoid repeated decoding)
    img_bytes = base64.b64decode(img_b64)
    image_part = {
        "mime_type": "image/jpeg",
        "data": img_bytes
    }
    
    last_error = None
    
    for attempt in range(max_retries):
        t0 = time.perf_counter()
        ttft_ms = None
        pred_text = ""
        
        try:
            # Streaming generation
            response = model_obj.generate_content(
                [prompt, image_part],
                stream=True
            )
            
            for chunk in response:
                if chunk.text:
                    if ttft_ms is None:
                        ttft_ms = (time.perf_counter() - t0) * 1000
                    pred_text += chunk.text
            
            total_ms = (time.perf_counter() - t0) * 1000
            pred_text = _postprocess_one_sentence(pred_text, max_chars=80)
            
            if not pred_text.strip():
                pred_text = "[EMPTY]"
            
            return {
                "pred_text": pred_text,
                "ttft_ms": ttft_ms or total_ms,
                "total_ms": total_ms,
                "retries": attempt
            }
            
        except Exception as e:
            last_error = str(e)
            error_str = str(e).lower()
            
            # Check if it's a 429 (Rate Limit) error
            is_rate_limit = "429" in error_str or "resource exhausted" in error_str or "quota" in error_str
            
            if is_rate_limit and attempt < max_retries - 1:
                # Wait and retry, increasing wait time each time
                wait_time = retry_delay * (attempt + 1)
                print(f"    ⚠️ Rate limit hit, waiting {wait_time:.1f}s before retry ({attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
                continue
            else:
                # Non-Rate Limit error or max retries reached
                total_ms = (time.perf_counter() - t0) * 1000
                return {
                    "pred_text": f"[ERROR] {last_error[:100]}",
                    "ttft_ms": total_ms,
                    "total_ms": total_ms,
                    "error": last_error,
                    "retries": attempt
                }
    
    # Theoretically unreachable, but as a safety net
    return {
        "pred_text": f"[ERROR] Max retries exceeded: {last_error[:100] if last_error else 'Unknown'}",
        "ttft_ms": 0.0,
        "total_ms": 0.0,
        "error": last_error,
        "retries": max_retries
    }


def _call_llama_cpp_openai_sdk_streaming(
    server_url: str,
    prompt: str,
    img_b64: str,
    timeout: float = 60.0,
    enable_thinking: bool = False,
    model_name: str = "MiniCPM-V",
) -> Dict[str, Any]:
    """
    Streaming call using OpenAI SDK (consistent with v5)
    Truly measure TTFT (Time To First Token)
    
    Returns {"pred_text": str, "ttft_ms": float, "total_ms": float}
    """
    if not HAS_OPENAI:
        return {
            "pred_text": "[ERROR] OpenAI SDK not installed",
            "ttft_ms": 0.0,
            "total_ms": 0.0,
            "error": "OpenAI SDK not installed"
        }
    
    t0 = time.perf_counter()
    ttft_ms: Optional[float] = None
    
    # Extract base_url from server_url
    base_url = server_url.replace("/chat/completions", "").replace("/v1/chat/completions", "/v1")
    if not base_url.endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"
    
    try:
        client = OpenAI(base_url=base_url, api_key="sk-no-key-required")
        
        system_msg = (
            "You are a blind glasses assistant."
            "Output only one sentence for the final answer, do not explain the process, do not use newlines."
            "Do not start with 'I need/Let me/First/Analyze/Next'."
        )
        
        data_url = f"data:image/jpeg;base64,{img_b64}"
        
        # MiniCPM-o with thinking needs more tokens and no \n stop
        max_tokens = 512 if enable_thinking else 80
        stop_seqs = None if enable_thinking else ["\n"]

        # Streaming call (consistent with v5)
        stream = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_msg},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}}
                    ]
                }
            ],
            max_tokens=max_tokens,
            temperature=0.0,
            stop=stop_seqs,
            stream=True,
            extra_body={"chat_template_kwargs": {"enable_thinking": enable_thinking}},
        )
        
        full_text = ""
        for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            
            # Extract text (consistent with pick_delta_text in v5)
            ans = getattr(delta, "content", None) or ""
            rea = getattr(delta, "reasoning_content", None) or ""
            new_text = ans if ans else rea  # Prioritize content
            
            if not new_text:
                continue
            
            # TTFT: Time when first token arrives
            if ttft_ms is None:
                ttft_ms = (time.perf_counter() - t0) * 1000
            
            full_text += new_text
        
        # Post-processing
        pred_text = _postprocess_one_sentence(full_text, max_chars=None)
        
        if not pred_text:
            pred_text = "[EMPTY]"
        
    except Exception as e:
        return {
            "pred_text": f"[ERROR] {e}",
            "ttft_ms": 0.0,
            "total_ms": (time.perf_counter() - t0) * 1000,
            "error": str(e)
        }
    
    total_ms = (time.perf_counter() - t0) * 1000
    
    return {
        "pred_text": pred_text.strip(),
        "ttft_ms": ttft_ms or total_ms,
        "total_ms": total_ms
    }


def _call_llama_cpp_openai_sdk(
    server_url: str,
    prompt: str,
    img_b64: str,
    timeout: float = 60.0
) -> Dict[str, Any]:
    """
    Non-streaming call using OpenAI SDK (identical to v3_nothink)
    This is the most reliable way to avoid various streaming parsing issues
    
    Returns {"pred_text": str, "ttft_ms": float, "total_ms": float}
    """
    if not HAS_OPENAI:
        return {
            "pred_text": "[ERROR] OpenAI SDK not installed",
            "ttft_ms": 0.0,
            "total_ms": 0.0,
            "error": "OpenAI SDK not installed"
        }
    
    t0 = time.perf_counter()
    
    # Extract base_url from server_url (remove /chat/completions suffix)
    base_url = server_url.replace("/chat/completions", "").replace("/v1/chat/completions", "/v1")
    if not base_url.endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"
    
    try:
        client = OpenAI(base_url=base_url, api_key="sk-no-key-required")
        
        # Parameters identical to v3
        system_msg = (
            "You are a blind glasses assistant."
            "Output only one sentence for the final answer, do not explain the process, do not use newlines."
            "Do not start with 'I need/Let me/First/Analyze/Next'."
        )
        
        data_url = f"data:image/jpeg;base64,{img_b64}"
        
        resp = client.chat.completions.create(
            model="MiniCPM-V",  # llama.cpp ignores this but it's required
            messages=[
                {"role": "system", "content": system_msg},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}}
                    ]
                }
            ],
            max_tokens=80,        # Consistent with v3
            temperature=0.0,      # Consistent with v3
            stop=["\n"],          # Consistent with v3
            timeout=timeout,
            # Non-thinking mode (consistent with v3_nothink)
            # Belt-and-suspenders: request-level disable thinking
            # (server should also be started with --reasoning-budget 0)
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        
        # Use extraction logic identical to v3
        raw = _extract_text_from_openai_resp(resp)
        pred_text = _postprocess_one_sentence(raw, max_chars=None)
        
        # If still empty, dump original response for debugging
        if not pred_text:
            try:
                debug_file = os.path.join(os.path.dirname(__file__), "..", "empty_debug_openai_sdk.jsonl")
                with open(debug_file, "a", encoding="utf-8") as f:
                    try:
                        f.write(resp.model_dump_json() + "\n")
                    except Exception:
                        f.write(json.dumps(str(resp), ensure_ascii=False) + "\n")
            except Exception:
                pass
            pred_text = "[EMPTY]"
        
    except Exception as e:
        return {
            "pred_text": f"[ERROR] {e}",
            "ttft_ms": 0.0,
            "total_ms": (time.perf_counter() - t0) * 1000,
            "error": str(e)
        }
    
    total_ms = (time.perf_counter() - t0) * 1000
    
    return {
        "pred_text": pred_text.strip(),
        "ttft_ms": total_ms,  # Non-streaming: TTFT = total time
        "total_ms": total_ms
    }


def _call_llama_cpp_streaming(
    server_url: str,
    prompt: str,
    img_b64: str,
    timeout: float = 60.0
) -> Dict[str, Any]:
    """
    Call llama.cpp server (OpenAI-compatible API) streaming interface
    Returns {"pred_text": str, "ttft_ms": float, "total_ms": float}
    
    Key: Use streaming delta to measure TTFT, avoids "false empty output" issue
    """
    t0 = time.perf_counter()
    ttft_ms: Optional[float] = None
    pred_text = ""
    last_chunk_data = None  # Save last complete chunk for fallback extraction
    
    # Ref v3 config: add system message and stop, limit max_tokens
    payload = {
        "model": "MiniCPM-V",
        "messages": [
            {
                "role": "system",
                "content": "You are a blind glasses assistant. Output only one sentence for the final answer, do not explain the process, do not use newlines. Do not start with \"I need/Let me/First/Analyze/Next\"."
            },
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                    {"type": "text", "text": prompt}
                ]
            }
        ],
        "stream": True,
        "max_tokens": 80,  # Ref v3: limit length to avoid long analysis
        "temperature": 0.0,  # Ref v3: deterministic output
        "stop": ["\n"],  # Ref v3: prevent newline expansion
    }
    
    try:
        # Explicitly set proxies=None to ensure no proxy
        resp = requests.post(
            server_url,
            json=payload,
            stream=True,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
            proxies={"http": None, "https": None}  # Force no proxy
        )
        resp.raise_for_status()
        
        for line in resp.iter_lines():
            if not line:
                continue
            line_str = line.decode("utf-8").strip()
            if line_str.startswith("data: "):
                line_str = line_str[6:]
            if line_str == "[DONE]":
                break
            if not line_str:
                continue
            
            try:
                chunk = json.loads(line_str)
                last_chunk_data = chunk  # Save for fallback
                choices = chunk.get("choices", [])
                if choices:
                    choice = choices[0]
                    # Streaming response can have two formats:
                    # 1. delta: {"content": "text"} (incremental update)
                    # 2. message: {"content": "full_text"} (last chunk contains full message)
                    delta = choice.get("delta", {})
                    message = choice.get("message", {})
                    
                    # Prioritize extraction from delta (streaming incremental)
                    content = delta.get("content")
                    if content is not None and isinstance(content, str) and content:
                        # First delta with content -> record TTFT
                        if ttft_ms is None:
                            ttft_ms = (time.perf_counter() - t0) * 1000
                        pred_text += content
                    # If delta is empty, try extracting from message (last full message)
                    elif message:
                        msg_content = message.get("content")
                        if msg_content is not None and isinstance(msg_content, str) and msg_content:
                            if ttft_ms is None:
                                ttft_ms = (time.perf_counter() - t0) * 1000
                            # If delta content already accumulated, prioritize it; otherwise use full message
                            if not pred_text:
                                pred_text = msg_content
                            else:
                                # If both delta and message exist, message might be complete, use it
                                pred_text = msg_content
            except json.JSONDecodeError:
                continue
        
        # Fallback: If streaming extraction fails, try extracting full message from last chunk
        if not pred_text.strip() and last_chunk_data:
            fallback_text = _extract_text_from_response(last_chunk_data)
            if fallback_text:
                pred_text = fallback_text
                if ttft_ms is None:
                    ttft_ms = (time.perf_counter() - t0) * 1000
                
    except requests.exceptions.RequestException as e:
        return {
            "pred_text": f"[ERROR] {e}",
            "ttft_ms": 0.0,
            "total_ms": (time.perf_counter() - t0) * 1000,
            "error": str(e)
        }
    
    total_ms = (time.perf_counter() - t0) * 1000
    
    # If still empty, log debug info
    if not pred_text.strip():
        pred_text = "[EMPTY]"
        # Save original response for debugging
        try:
            debug_file = os.path.join(os.path.dirname(__file__), "..", "empty_debug_streaming.jsonl")
            with open(debug_file, "a", encoding="utf-8") as f:
                if last_chunk_data:
                    f.write(json.dumps(last_chunk_data, ensure_ascii=False) + "\n")
        except Exception:
            pass
    
    return {
        "pred_text": pred_text.strip(),
        "ttft_ms": ttft_ms or total_ms,
        "total_ms": total_ms
    }


def _call_llama_cpp_non_streaming(
    server_url: str,
    prompt: str,
    img_b64: str,
    timeout: float = 60.0
) -> Dict[str, Any]:
    """Non-streaming call (for ablation: w/o streaming)"""
    t0 = time.perf_counter()
    
    # Ref v3 config: add system message and stop, limit max_tokens
    payload = {
        "model": "MiniCPM-V",
        "messages": [
            {
                "role": "system",
                "content": "You are a blind glasses assistant. Output only one sentence for the final answer, do not explain the process, do not use newlines. Do not start with \"I need/Let me/First/Analyze/Next\"."
            },
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                    {"type": "text", "text": prompt}
                ]
            }
        ],
        "stream": False,
        "max_tokens": 80,  # Ref v3: limit length
        "temperature": 0.0,  # Ref v3: deterministic output
        "stop": ["\n"],  # Ref v3: prevent newline expansion
    }
    
    try:
        # Explicitly set proxies=None to ensure no proxy
        resp = requests.post(
            server_url,
            json=payload,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
            proxies={"http": None, "https": None}  # Force no proxy
        )
        resp.raise_for_status()
        data = resp.json()
        # Use robust text extraction function to avoid "false empty output"
        pred_text = _extract_text_from_response(data)
        
        # If still empty, log debug info
        if not pred_text.strip():
            pred_text = "[EMPTY]"
            # Save original response for debugging
            try:
                debug_file = os.path.join(os.path.dirname(__file__), "..", "empty_debug_nonstreaming.jsonl")
                with open(debug_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(data, ensure_ascii=False) + "\n")
            except Exception:
                pass
            
    except requests.exceptions.RequestException as e:
        return {
            "pred_text": f"[ERROR] {e}",
            "ttft_ms": 0.0,
            "total_ms": (time.perf_counter() - t0) * 1000,
            "error": str(e)
        }
    
    total_ms = (time.perf_counter() - t0) * 1000
    
    return {
        "pred_text": pred_text.strip(),
        "ttft_ms": total_ms,  # Non-streaming: TTFT = total request time
        "total_ms": total_ms
    }


# =============================================================================
# TTS Worker (Identical to v6 script, uses pyttsx3 local TTS)
# =============================================================================

class TTSWorker:
    """
    Dedicated TTS worker that timestamps when speaking begins.
    Identical to v6 script (02_batch_infer_stream_tts_realtime_eval_v6_wifi_capture.py).
    """

    def __init__(self, rate: int = 220, enabled: bool = True):
        self.enabled = enabled and HAS_PYTTSX3
        self.q: "queue.Queue[Optional[str]]" = queue.Queue() if HAS_PYTTSX3 else None
        self.stop_event = threading.Event() if HAS_PYTTSX3 else None
        self.first_audio_time: Optional[float] = None
        self.first_audio_event = threading.Event() if HAS_PYTTSX3 else None
        self._started = False

        self.engine = None
        if self.enabled:
            try:
                self.engine = pyttsx3.init()
                self.engine.setProperty("rate", rate)
            except Exception:
                self.engine = None
                self.enabled = False

        if HAS_PYTTSX3:
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
                self.first_audio_time = time.perf_counter()  # Record first audio moment
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
        if self.q is not None:
            self.q.put(text)

    def wait_first_audio(self, timeout_s: float = 2.0) -> bool:
        if self.first_audio_event is not None:
            return self.first_audio_event.wait(timeout=timeout_s)
        return False

    def drain(self, timeout_s: Optional[float] = None) -> None:
        if self.q is None:
            return
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
            if self.stop_event is not None:
                self.stop_event.set()
            if self.q is not None:
                self.q.put(None)
            if hasattr(self, 'thread'):
                self.thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            if self.engine is not None:
                self.engine.stop()
        except Exception:
            pass


def _compute_tts_ttfa(text: str, cfg: Dict[str, Any]) -> float:
    """
    Measure TTFA using pyttsx3 local TTS (identical to v6 script)
    
    Note: Regardless of whether parallel_tts is true or false, TTS will be executed and TTS internal duration returned.
    The difference for parallel_tts lies in how t_audio is calculated in infer_one:
    - parallel_tts=true: t_audio = t_first_token + tts_internal_ms (parallel, starting from first token)
    - parallel_tts=false: t_audio = t_vlm_done + tts_internal_ms (serial, starting from VLM completion)
    
    Returns: TTS internal processing time (from text enqueue to start of playback), usually ~0-50 ms
    """
    # Skip empty or error text
    if not text or text.startswith("[ERROR]") or text.startswith("[EMPTY]"):
        return 0.0
    
    if not HAS_PYTTSX3:
        # No pyttsx3, return simulated value
        return 50.0  # Simulated local TTS startup time
    
    tts_rate = cfg.get("tts_rate", 220)
    
    t0 = time.perf_counter()
    tts = TTSWorker(rate=tts_rate, enabled=True)
    
    try:
        # Feed text
        tts.speak(text)
        
        # Wait for first audio event (max 2 seconds)
        if tts.wait_first_audio(timeout_s=2.0):
            # Calculate time from feeding text to start of playback
            if tts.first_audio_time is not None:
                ttfa_ms = (tts.first_audio_time - t0) * 1000
            else:
                ttfa_ms = (time.perf_counter() - t0) * 1000
        else:
            # Timeout, use current time
            ttfa_ms = (time.perf_counter() - t0) * 1000
        
        # Wait for playback completion (max 10 seconds)
        tts.drain(timeout_s=10.0)
        
    except Exception:
        ttfa_ms = (time.perf_counter() - t0) * 1000
    finally:
        tts.close()
    
    return ttfa_ms


def _normalize_cfg_for_v6(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Make config keys backward/forward compatible across variants.

    Supported aliases:
      - server_url <-> base_url
      - resize_896 (bool) <-> pack_mode ("resize"/"raw") + max_edge
    """
    out = dict(cfg or {})

    # ---- server_url / base_url ----
    server_url = out.get("server_url")
    base_url = out.get("base_url")

    if not server_url and base_url:
        # base_url may be like:
        #   http://127.0.0.1:8080/v1
        #   http://127.0.0.1:8080/v1/chat/completions
        #   http://127.0.0.1:8080
        bu = str(base_url).rstrip("/")
        if bu.endswith("/v1/chat/completions"):
            server_url = bu
        elif bu.endswith("/v1"):
            server_url = bu + "/chat/completions"
        else:
            # assume root; add /v1/chat/completions
            server_url = bu + "/v1/chat/completions"
        out["server_url"] = server_url

    # also accept legacy key "server" or "endpoint"
    if not out.get("server_url"):
        for k in ("server", "endpoint", "url"):
            if out.get(k):
                out["server_url"] = out[k]
                break

    # ---- resize_896 / pack_mode + max_edge ----
    if "pack_mode" in out and "resize_896" not in out:
        pm = str(out.get("pack_mode") or "").lower()
        if pm in ("raw", "no", "none", "off"):
            out["resize_896"] = False
        else:
            out["resize_896"] = True

    # map resize_896 to pack_mode for downstream readability
    if "resize_896" in out and "pack_mode" not in out:
        out["pack_mode"] = "resize" if bool(out.get("resize_896")) else "raw"

    # default max_edge
    if "max_edge" not in out:
        # legacy: resize_896=True implies max_edge=896 unless overridden
        out["max_edge"] = 896

    # allow legacy resize_448 style
    if out.get("resize_448", False):
        out["resize_896"] = True
        out["pack_mode"] = "resize"
        out["max_edge"] = 448

    return out


def _sanitize_for_tts(text: str) -> str:
    return (text or "").replace("\n", " ").strip()


def _pop_sentence(buffer: str) -> (Optional[str], str):
    """
    Pop a speakable chunk if buffer ends with sentence boundary.
    Returns (chunk_or_none, remaining_buffer).
    """
    if not buffer:
        return None, buffer
    seps = ["。", "！", "？", ".", "!", "?", "；", ";", "：", ":"]
    # flush on the last boundary occurrence
    last = -1
    for s in seps:
        idx = buffer.rfind(s)
        if idx > last:
            last = idx
    if last >= 0:
        chunk = buffer[: last + 1].strip()
        rest = buffer[last + 1 :].lstrip()
        return (chunk if chunk else None), rest
    return None, buffer


def infer_one(sample: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    ADAPTER ENTRYPOINT - Call server for VLM inference and align with v6 timing criteria.

    v6 Key Points:
      1) TTFT: Based on t_send, truly mark first token arrival in streaming loop
      2) Parallel TTS: Enqueue by sentence during streaming, rather than one-shot enqueue after VLM completion
      3) Config Compatibility: Support server_url/base_url and resize_896/pack_mode+max_edge

    Return dict:
      pred_text, ttft_ms, ttfa_ms, e2e_ms, meta_json
    """
    cfg = _normalize_cfg_for_v6(cfg)

    # Stub mode: return simulated data (for testing framework)
    if cfg.get("mode") == "stub":
        return _stub_infer(sample, cfg)

    provider = cfg.get("provider", "local")  # local, gpt-4o, gemini-...
    server_url = cfg.get("server_url", "http://127.0.0.1:8080/v1/chat/completions")
    streaming = bool(cfg.get("streaming", True))
    parallel_tts = bool(cfg.get("parallel_tts", True))

    resize_896 = bool(cfg.get("resize_896", True))
    max_edge = int(cfg.get("max_edge", 896))

    # inputs
    image_path = sample.get("image_path", "")
    prompt = sample.get("prompt_nl_v3", "")

    # relative path support
    if image_path and not os.path.isabs(image_path):
        candidates = [
            image_path,
            os.path.join("eval_benchmark", image_path),
            os.path.join(os.path.dirname(__file__), "..", image_path),
        ]
        for candidate in candidates:
            if os.path.exists(candidate):
                image_path = candidate
                break

    # ==================== timing starts at "user done" ====================
    t_user_done = time.perf_counter()

    # TTS worker: same behavior as v6 (created per sample, drained to avoid backlog)
    tts_rate = int(cfg.get("tts_rate", 220))
    tts_enabled = bool(cfg.get("tts", True)) and HAS_PYTTSX3
    tts = TTSWorker(rate=tts_rate, enabled=tts_enabled) if HAS_PYTTSX3 else None

    # 1) capture+pack (disk path here; wifi capture is in v6_wifi_capture script)
    try:
        img_b64 = _load_image_base64(image_path, resize_896=resize_896, max_edge=max_edge)
    except FileNotFoundError as e:
        if tts is not None:
            tts.close()
        return {
            "pred_text": f"[ERROR] Image not found: {image_path}",
            "ttft_ms": 0.0,
            "ttfa_ms": 0.0,
            "e2e_ms": 0.0,
            "meta_json": {"error": str(e)},
        }
    t_capture_end = time.perf_counter()
    t_pack_end = t_capture_end  # disk: merge

    # 2) VLM call
    t_send = time.perf_counter()

    # placeholders
    pred_text = ""
    send_to_token_ms = 0.0
    t_first_token: Optional[float] = None
    t_first_enqueue: Optional[float] = None
    t_audio: Optional[float] = None
    tts_internal_ms = 0.0
    backend = "unknown"
    error_msg: Optional[str] = None

    # For non-local providers keep old behavior (no incremental TTS here)
    if provider not in ("local", "llamacpp", "llama.cpp"):
        # fall back to existing code paths
        if provider in ("gpt-4o", "gpt-4o-mini", "gpt-5.2", "gpt-5", "openai"):
            api_key = cfg.get("api_key") or os.environ.get("OPENAI_API_KEY")
            model = cfg.get("model", "gpt-4o")
            vlm_result = _call_openai_gpt4o_streaming(prompt, img_b64, api_key=api_key, model=model)
            backend = f"cloud_{model}"
        elif provider in ("gemini", "gemini-flash", "gemini-2.0-flash", "gemini-2.5-flash",
                          "gemini-2.5-pro", "gemini-1.5-flash", "gemini-1.5-pro", "google"):
            api_key = cfg.get("api_key") or os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
            model = cfg.get("model", "gemini-2.0-flash")
            vlm_result = _call_gemini_flash_streaming(prompt, img_b64, api_key=api_key, model=model)
            backend = f"cloud_{model}"
        elif provider in ("qwen", "qwen-vl", "qwen-vl-max", "qwen-vl-plus", "dashscope", "aliyun"):
            api_key = cfg.get("api_key") or os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY")
            model = cfg.get("model", "qwen-vl-max")
            vlm_result = _call_qwen_vl_streaming(prompt, img_b64, api_key=api_key, model=model)
            backend = f"cloud_{model}"
        else:
            vlm_result = {"pred_text": "[ERROR] unknown provider", "ttft_ms": 0.0, "total_ms": 0.0, "error": "unknown provider"}

        pred_text = vlm_result.get("pred_text", "")
        # Strip <think> tags if configured (MiniCPM-o support)
        if cfg.get("strip_think_tags", False) and pred_text:
            pred_text = _strip_think_tags(pred_text)
        send_to_token_ms = float(vlm_result.get("ttft_ms", 0.0))
        # NOTE: for these providers we cannot guarantee absolute t_first_token; keep legacy approximation
        t_first_token = t_send + send_to_token_ms / 1000.0 if send_to_token_ms else None
        error_msg = vlm_result.get("error")

        # TTS after VLM done (same as old)
        t_vlm_done = time.perf_counter()
        if tts is not None and pred_text and not pred_text.startswith("[ERROR]") and not pred_text.startswith("[EMPTY]"):
            t_first_enqueue = time.perf_counter()
            tts.speak(_sanitize_for_tts(pred_text))
            if tts.wait_first_audio(timeout_s=2.5):
                t_audio = tts.first_audio_time
            if t_audio is not None:
                tts_internal_ms = (t_audio - t_vlm_done) * 1000.0
            tts.drain(timeout_s=10.0)
        if tts is not None:
            tts.close()

        t_end = time.perf_counter()

    else:
        # ========= local llama.cpp with v6-aligned streaming + incremental TTS =========
        # Determine whether to use requests or OpenAI SDK
        use_requests = bool(cfg.get("use_requests", False))
        # MiniCPM-o support: thinking mode and model name
        enable_thinking = bool(cfg.get("thinking", False))
        model_name = cfg.get("model", "MiniCPM-V")
        if not streaming:
            # non-streaming fall back to existing helpers
            if use_requests:
                vlm_result = _call_llama_cpp_non_streaming(server_url, prompt, img_b64)
                backend = "requests_non_streaming"
            else:
                vlm_result = _call_llama_cpp_openai_sdk(server_url, prompt, img_b64)
                backend = "openai_sdk_non_streaming"
            pred_text = vlm_result.get("pred_text", "")
            # Strip <think> tags if configured (MiniCPM-o support)
            if cfg.get("strip_think_tags", False) and pred_text:
                pred_text = _strip_think_tags(pred_text)
            send_to_token_ms = float(vlm_result.get("ttft_ms", 0.0))
            t_first_token = t_send + send_to_token_ms / 1000.0 if send_to_token_ms else None
            error_msg = vlm_result.get("error")

            # TTS after VLM done
            t_vlm_done = time.perf_counter()
            if tts is not None and pred_text and not pred_text.startswith("[ERROR]") and not pred_text.startswith("[EMPTY]"):
                t_first_enqueue = time.perf_counter()
                tts.speak(_sanitize_for_tts(pred_text))
                if tts.wait_first_audio(timeout_s=2.5):
                    t_audio = tts.first_audio_time
                if t_audio is not None:
                    tts_internal_ms = (t_audio - t_vlm_done) * 1000.0
                tts.drain(timeout_s=10.0)
            if tts is not None:
                tts.close()
            t_end = time.perf_counter()
        else:
            # Streaming path: this is the critical fix
            full_text = ""
            buf = ""  # sentence buffer for incremental TTS
            # We want TTS to overlap with decoding:
            do_incremental_tts = parallel_tts and (tts is not None) and tts_enabled

            try:
                if use_requests:
                    # requests SSE streaming (OpenAI-compatible)
                    headers = {"Content-Type": "application/json"}
                    data_url = f"data:image/jpeg;base64,{img_b64}"
                    payload = {
                        "model": "MiniCPM-V",
                        "messages": [
                            {"role": "system", "content": (
                                "You are a blind glasses assistant."
                                "Output only one sentence for the final answer, do not explain the process, do not use newlines."
                                "Do not start with 'I need/Let me/First/Analyze/Next'."
                            )},
                            {"role": "user", "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": data_url}},
                            ]},
                        ],
                        "max_tokens": 80,
                        "temperature": 0.0,
                        "stop": ["\n"],
                        "stream": True,
                        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
                    }
                    with requests.post(server_url, headers=headers, json=payload, stream=True, timeout=60) as r:
                        r.raise_for_status()
                        for raw_line in r.iter_lines(decode_unicode=True):
                            if not raw_line:
                                continue
                            line = raw_line.strip()
                            if not line.startswith("data:"):
                                continue
                            data = line[len("data:"):].strip()
                            if data == "[DONE]":
                                break
                            try:
                                obj = json.loads(data)
                            except Exception:
                                continue
                            choices = obj.get("choices", [])
                            if not choices:
                                continue
                            delta = choices[0].get("delta", {}) or {}
                            ans = delta.get("content") or ""
                            rea = delta.get("reasoning_content") or ""
                            new_text = ans if ans else rea
                            if not new_text:
                                continue
                            if t_first_token is None:
                                t_first_token = time.perf_counter()
                                send_to_token_ms = (t_first_token - t_send) * 1000.0
                            full_text += new_text
                            if do_incremental_tts:
                                buf += new_text
                                chunk, buf = _pop_sentence(buf)
                                if chunk:
                                    if t_first_enqueue is None:
                                        t_first_enqueue = time.perf_counter()
                                    tts.speak(_sanitize_for_tts(chunk))
                else:
                    # OpenAI SDK streaming
                    if not HAS_OPENAI:
                        raise RuntimeError("OpenAI SDK not installed")
                    base_url = server_url.replace("/chat/completions", "").replace("/v1/chat/completions", "/v1")
                    if not base_url.endswith("/v1"):
                        base_url = base_url.rstrip("/") + "/v1"
                    client = OpenAI(base_url=base_url, api_key="sk-no-key-required")

                    data_url = f"data:image/jpeg;base64,{img_b64}"
                    system_msg = (
                        "You are a blind glasses assistant."
                        "Output only one sentence for the final answer, do not explain the process, do not use newlines."
                        "Do not start with 'I need/Let me/First/Analyze/Next'."
                    )
                    # MiniCPM-o with thinking needs more tokens and no \n stop
                    max_tokens = 512 if enable_thinking else 80
                    stop_seqs = None if enable_thinking else ["\n"]

                    stream_obj = client.chat.completions.create(
                        model=model_name,
                        messages=[
                            {"role": "system", "content": system_msg},
                            {"role": "user", "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": data_url}},
                            ]},
                        ],
                        max_tokens=max_tokens,
                        temperature=0.0,
                        stop=stop_seqs,
                        stream=True,
                        extra_body={"chat_template_kwargs": {"enable_thinking": enable_thinking}},
                    )

                    for chunk in stream_obj:
                        if not getattr(chunk, "choices", None):
                            continue
                        delta = chunk.choices[0].delta
                        ans = getattr(delta, "content", None) or ""
                        rea = getattr(delta, "reasoning_content", None) or ""
                        new_text = ans if ans else rea
                        if not new_text:
                            continue
                        if t_first_token is None:
                            t_first_token = time.perf_counter()
                            send_to_token_ms = (t_first_token - t_send) * 1000.0
                        full_text += new_text
                        if do_incremental_tts:
                            buf += new_text
                            chunk_text, buf = _pop_sentence(buf)
                            if chunk_text:
                                if t_first_enqueue is None:
                                    t_first_enqueue = time.perf_counter()
                                tts.speak(_sanitize_for_tts(chunk_text))

                backend = "requests_streaming" if use_requests else "openai_sdk_streaming"
            except Exception as e:
                error_msg = str(e)

            # After stream ends: strip <think> tags if configured (MiniCPM-o support)
            if cfg.get("strip_think_tags", False) and full_text:
                full_text = _strip_think_tags(full_text)
            # postprocess for eval
            pred_text = _postprocess_one_sentence(full_text, max_chars=None) if full_text else ""
            if not pred_text:
                pred_text = "[EMPTY]" if not error_msg else f"[ERROR] {error_msg}"

            # flush remaining buffer to TTS
            if do_incremental_tts and buf.strip():
                if t_first_enqueue is None:
                    t_first_enqueue = time.perf_counter()
                tts.speak(_sanitize_for_tts(buf.strip()))
                buf = ""

            # If no incremental (e.g. parallel_tts False), do one-shot enqueue after full text
            if (not do_incremental_tts) and (tts is not None) and tts_enabled:
                if pred_text and not pred_text.startswith("[ERROR]") and not pred_text.startswith("[EMPTY]"):
                    t_first_enqueue = time.perf_counter()
                    tts.speak(_sanitize_for_tts(pred_text))

            # wait first audio (proxy for first audible)
            t_vlm_done = time.perf_counter()
            if tts is not None and tts_enabled:
                if tts.wait_first_audio(timeout_s=2.5):
                    t_audio = tts.first_audio_time
                if t_audio is not None:
                    # internal overlap measure: from "VLM done" to "TTS started speaking"
                    tts_internal_ms = (t_audio - t_vlm_done) * 1000.0
                tts.drain(timeout_s=10.0)

            if tts is not None:
                tts.close()

            t_end = time.perf_counter()

    # ==================== metric computation (v6-aligned) ====================
    user_to_capture_ms = (t_capture_end - t_user_done) * 1000.0
    user_to_pack_ms = (t_pack_end - t_user_done) * 1000.0
    user_to_send_ms = (t_send - t_user_done) * 1000.0

    user_to_token_ms = (t_first_token - t_user_done) * 1000.0 if t_first_token else 0.0

    # If TTS disabled, fall back to enqueue time to avoid hard 0 (matches v6 comment intent)
    if t_audio is None and t_first_enqueue is not None and (tts is None or not tts_enabled):
        t_audio = t_first_enqueue

    if t_audio is not None:
        user_to_audio_ms = (t_audio - t_user_done) * 1000.0
        token_to_audio_ms = (t_audio - t_first_token) * 1000.0 if t_first_token else 0.0
    else:
        user_to_audio_ms = 0.0
        token_to_audio_ms = 0.0

    e2e_ms = (t_end - t_user_done) * 1000.0 if "t_end" in locals() else 0.0

    status = "PASS(<2s)" if (user_to_audio_ms and user_to_audio_ms < 2000.0) else "FAIL"

    return {
        "pred_text": pred_text,
        "ttft_ms": float(send_to_token_ms),
        "ttfa_ms": float(user_to_audio_ms),
        "e2e_ms": float(e2e_ms),
        "meta_json": {
            "backend": backend,
            "provider": provider,
            "streaming": streaming,
            "parallel_tts": parallel_tts,
            # config normalized
            "server_url": server_url,
            "base_url": cfg.get("base_url"),
            "resize_896": resize_896,
            "pack_mode": cfg.get("pack_mode"),
            "max_edge": max_edge,
            # errors
            "error": error_msg,
            # detailed breakdown
            "user_to_capture_ms": user_to_capture_ms,
            "user_to_pack_ms": user_to_pack_ms,
            "user_to_send_ms": user_to_send_ms,
            "send_to_token_ms": float(send_to_token_ms),
            "user_to_token_ms": float(user_to_token_ms),
            "user_to_first_enqueue_ms": float((t_first_enqueue - t_user_done) * 1000.0) if t_first_enqueue else 0.0,
            "user_to_audio_ms": float(user_to_audio_ms),
            "token_to_audio_ms": float(token_to_audio_ms),
            "tts_internal_ms": float(tts_internal_ms),
            "status": status,
        },
    }

def _stub_infer(sample: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Stub implementation: used for testing framework flow, returns simulated data"""
    t0 = time.time()
    
    streaming = bool(cfg.get("streaming", True))
    parallel_tts = bool(cfg.get("parallel_tts", True))
    resize_896 = bool(cfg.get("resize_896", True))
    
    # Simulate latency
    ttft_ms = 120.0 if streaming else 450.0
    ttfa_ms = 650.0 if (streaming and parallel_tts) else 2200.0
    if not resize_896:
        ttfa_ms += 300.0
    
    # Use GT answer as stub output (convenient for testing rubric)
    gt_answer = sample.get("gt_answer_v3", "(stub) Result output.")
    pred_text = gt_answer if gt_answer else "(stub) Result output."
    
    e2e_ms = max(ttfa_ms + 200.0, (time.time() - t0) * 1000.0)
    
    return {
        "pred_text": pred_text,
        "ttft_ms": float(ttft_ms),
        "ttfa_ms": float(ttfa_ms),
        "e2e_ms": float(e2e_ms),
        "meta_json": {"adapter": "stub", "streaming": streaming}
    }
