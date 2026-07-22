"""Configuration loading for the standalone OpenGlass Omni launcher."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


RUNTIME_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = RUNTIME_DIR / "runtime.local.json"
EXAMPLE_CONFIG_PATH = RUNTIME_DIR / "runtime.example.json"


class ConfigError(RuntimeError):
    """Raised when the local runtime configuration is missing or invalid."""


def _resolve_path(value: str, base_dir: Path, *, allow_empty: bool = False) -> Path | None:
    expanded = os.path.expanduser(os.path.expandvars(str(value).strip()))
    if not expanded:
        if allow_empty:
            return None
        raise ConfigError("A required path is empty")
    if "$" in expanded or "%" in expanded:
        raise ConfigError(f"Path contains an unresolved environment variable: {value}")
    path = Path(expanded)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _int(section: dict[str, Any], key: str, default: int) -> int:
    value = int(section.get(key, default))
    if not 1 <= value <= 65535:
        raise ConfigError(f"Invalid TCP port for {key}: {value}")
    return value


def load_runtime_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load a local adapter config and resolve paths relative to that file."""
    config_path = Path(path).resolve() if path else DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        raise ConfigError(
            f"Runtime config not found: {config_path}. "
            f"Copy {EXAMPLE_CONFIG_PATH.name} to {DEFAULT_CONFIG_PATH.name} and edit local paths."
        )

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read runtime config {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Runtime config root must be a JSON object")

    base = config_path.parent
    gateway = raw.get("gateway") or {}
    worker = raw.get("worker") or {}
    demo = raw.get("demo") or {}
    if not all(isinstance(item, dict) for item in (gateway, worker, demo)):
        raise ConfigError("gateway, worker, and demo must be JSON objects")

    python_value = str(raw.get("python") or "").strip()
    python_path = _resolve_path(python_value, base) if python_value else Path(sys.executable).resolve()
    minicpm_root = _resolve_path(str(raw.get("minicpm_demo_root", "")), base)
    llama_root = _resolve_path(str(raw.get("llama_cpp_omni_root", "")), base)
    devices_file = _resolve_path(str(raw.get("devices_file", "")), base)
    prompts_file = _resolve_path(str(raw.get("prompts_file", "prompts.json")), base)
    state_dir = _resolve_path(str(raw.get("state_dir", "state")), base)

    cfg: dict[str, Any] = {
        "config_path": config_path,
        "python": python_path,
        "minicpm_demo_root": minicpm_root,
        "llama_cpp_omni_root": llama_root,
        "devices_file": devices_file,
        "prompts_file": prompts_file,
        "state_dir": state_dir,
        "worker": {
            "host": str(worker.get("host", "127.0.0.1")),
            "port": _int(worker, "port", 22440),
            "ready_timeout_s": float(worker.get("ready_timeout_s", 180.0)),
        },
        "gateway": {
            "host": str(gateway.get("host", "127.0.0.1")),
            "bind_host": str(gateway.get("bind_host", "0.0.0.0")),
            "port": _int(gateway, "port", 8040),
            "tls": bool(gateway.get("tls", True)),
            "certfile": str(gateway.get("certfile", "certs/cert.pem")),
            "keyfile": str(gateway.get("keyfile", "certs/key.pem")),
            "ready_timeout_s": float(gateway.get("ready_timeout_s", 45.0)),
        },
        "demo": {
            "ui_host": str(demo.get("ui_host", "127.0.0.1")),
            "ui_port": _int(demo, "ui_port", 8080),
            "audio_endpoint": str(demo.get("audio_endpoint", "/ws_audio_v2")),
            "image_min_interval_s": float(demo.get("image_min_interval_s", 0.8)),
            "player_hostapi": str(demo.get("player_hostapi", "wasapi")),
            "player_prebuffer_ms": int(demo.get("player_prebuffer_ms", 200)),
            "connect_wait_s": float(demo.get("connect_wait_s", 120.0)),
            "ready_timeout_s": float(demo.get("ready_timeout_s", 20.0)),
        },
    }

    if not cfg["demo"]["audio_endpoint"].startswith("/"):
        raise ConfigError("demo.audio_endpoint must start with '/'")
    return cfg


def load_prompts(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read prompts file {path}: {exc}") from exc
    prompts = data.get("prompts") if isinstance(data, dict) else None
    if not isinstance(prompts, dict) or not prompts:
        raise ConfigError("Prompts file must contain a non-empty 'prompts' object")
    cleaned = {str(name): str(prompt).strip() for name, prompt in prompts.items() if str(prompt).strip()}
    if not cleaned:
        raise ConfigError("Prompts file contains no usable prompts")
    return cleaned


def load_devices(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read devices file {path}: {exc}") from exc
    devices = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(devices, list) or not devices:
        raise ConfigError("Devices file must contain a non-empty 'devices' array")
    result = []
    for index, device in enumerate(devices):
        if not isinstance(device, dict) or not device.get("name") or not device.get("esp32_host"):
            raise ConfigError(f"Invalid device entry at index {index}")
        result.append(device)
    return result
