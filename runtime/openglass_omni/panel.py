"""Standalone process panel for OpenGlass + upstream MiniCPM-o-Demo."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .config import ConfigError, load_devices, load_prompts, load_runtime_config


RUNTIME_DIR = Path(__file__).resolve().parent
PROCESS_NAMES = ("worker", "gateway", "demo")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read JSON file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"JSON root must be an object: {path}")
    return data


def _resolve_upstream_path(value: str, upstream_root: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    if not path.is_absolute():
        path = upstream_root / path
    return path.resolve()


def preflight(cfg: dict[str, Any]) -> list[dict[str, str]]:
    """Validate the adapter boundary without importing or modifying upstream code."""
    findings: list[dict[str, str]] = []

    def add(level: str, message: str) -> None:
        findings.append({"level": level, "message": message})

    python_path: Path = cfg["python"]
    minicpm_root: Path = cfg["minicpm_demo_root"]
    expected_llama_root: Path = cfg["llama_cpp_omni_root"]
    devices_file: Path = cfg["devices_file"]

    if not python_path.is_file():
        add("error", "Configured Python executable does not exist")
    for filename in ("worker.py", "gateway.py", "config.py"):
        if not (minicpm_root / filename).is_file():
            add("error", f"MiniCPM-o-Demo is missing required file: {filename}")

    upstream_config_path = minicpm_root / "config.json"
    if not upstream_config_path.is_file():
        add("error", "MiniCPM-o-Demo/config.json is missing; create it from the upstream example")
    else:
        try:
            upstream = _read_json(upstream_config_path)
            if upstream.get("backend") != "cpp":
                add("error", "MiniCPM-o-Demo config.json must use backend='cpp' for llama.cpp-omni")
            cpp = upstream.get("cpp_backend") or {}
            service = upstream.get("service") or {}
            actual_llama_value = str(cpp.get("llamacpp_root") or "")
            if not actual_llama_value:
                add("error", "MiniCPM-o-Demo config.json has no cpp_backend.llamacpp_root")
            else:
                actual_llama_root = _resolve_upstream_path(actual_llama_value, minicpm_root)
                if actual_llama_root != expected_llama_root:
                    add("error", "OpenGlass and MiniCPM-o-Demo point to different llama.cpp-omni checkouts")
            if int(service.get("gateway_port", cfg["gateway"]["port"])) != cfg["gateway"]["port"]:
                add("warning", "Gateway port differs from MiniCPM-o-Demo config; launcher CLI override will be used")
            if int(service.get("worker_base_port", cfg["worker"]["port"])) != cfg["worker"]["port"]:
                add("warning", "Worker port differs from MiniCPM-o-Demo config; launcher CLI override will be used")
            model_dir_value = str(cpp.get("model_dir") or "")
            model_name = str(cpp.get("llm_model") or "")
            if not model_dir_value or not model_name:
                add("error", "MiniCPM-o-Demo config.json is missing C++ model directory or GGUF filename")
            elif not (_resolve_upstream_path(model_dir_value, minicpm_root) / model_name).is_file():
                add("error", "Configured MiniCPM-o GGUF model file does not exist")
        except (ConfigError, TypeError, ValueError) as exc:
            add("error", str(exc))

    binary_candidates = (
        expected_llama_root / "build" / "bin" / "llama-server.exe",
        expected_llama_root / "build" / "bin" / "Release" / "llama-server.exe",
        expected_llama_root / "build" / "bin" / "llama-server",
    )
    if not any(path.is_file() for path in binary_candidates):
        add("error", "No existing llama-server binary was found; this launcher will not download or compile it")
    else:
        add("ok", "Existing llama.cpp-omni build detected")

    if cfg["gateway"]["tls"]:
        cert = _resolve_upstream_path(cfg["gateway"]["certfile"], minicpm_root)
        key = _resolve_upstream_path(cfg["gateway"]["keyfile"], minicpm_root)
        if not cert.is_file() or not key.is_file():
            add("error", "Gateway TLS certificate/key are missing in MiniCPM-o-Demo")

    try:
        load_devices(devices_file)
        add("ok", "Device registry loaded")
    except ConfigError as exc:
        add("error", str(exc))
    try:
        load_prompts(cfg["prompts_file"])
        add("ok", "Prompt presets loaded")
    except ConfigError as exc:
        add("error", str(exc))

    if not (RUNTIME_DIR / "esp32_bridge.py").is_file():
        add("error", "OpenGlass ESP32 bridge is missing")
    if not any(item["level"] == "error" for item in findings):
        add("ok", "OpenGlass adapter preflight passed")
    return findings


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class ProcessManager:
    def __init__(self, cfg: dict[str, Any], prompts: dict[str, str], devices: list[dict[str, Any]]):
        self.cfg = cfg
        self.prompts = prompts
        self.devices = devices
        self.current_prompt = next(iter(prompts.values()))
        self.current_device = str(devices[0]["name"])
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.status = {name: "stopped" for name in PROCESS_NAMES}
        self.logs = {name: deque(maxlen=500) for name in ("system", *PROCESS_NAMES)}
        self._operation_lock = threading.Lock()
        self._cancel = threading.Event()
        self._closing = False
        self._log_files: dict[str, Any] = {}
        cfg["state_dir"].mkdir(parents=True, exist_ok=True)
        (cfg["state_dir"] / "logs").mkdir(parents=True, exist_ok=True)
        (cfg["state_dir"] / "sessions").mkdir(parents=True, exist_ok=True)
        for item in preflight(cfg):
            marker = item["level"].upper()
            self._log("system", f"{marker}: {item['message']}")
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _log(self, name: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.logs[name].append(f"[{stamp}] {message.rstrip()}")

    def _owned_alive(self, name: str) -> bool:
        proc = self.processes.get(name)
        return proc is not None and proc.poll() is None

    def _port_for(self, name: str) -> tuple[str, int]:
        if name == "worker":
            return self.cfg["worker"]["host"], self.cfg["worker"]["port"]
        if name == "gateway":
            return self.cfg["gateway"]["host"], self.cfg["gateway"]["port"]
        return self.cfg["demo"]["ui_host"], self.cfg["demo"]["ui_port"]

    def _build_command(self, name: str) -> tuple[list[str], Path]:
        python = str(self.cfg["python"])
        minicpm = self.cfg["minicpm_demo_root"]
        worker = self.cfg["worker"]
        gateway = self.cfg["gateway"]
        demo = self.cfg["demo"]
        if name == "worker":
            return [
                python, str(minicpm / "worker.py"),
                "--host", worker["host"], "--port", str(worker["port"]),
            ], minicpm
        if name == "gateway":
            cmd = [
                python, str(minicpm / "gateway.py"),
                "--host", gateway["bind_host"], "--port", str(gateway["port"]),
                "--workers", f"{worker['host']}:{worker['port']}",
            ]
            if gateway["tls"]:
                cmd.extend([
                    "--https",
                    "--ssl-certfile", str(_resolve_upstream_path(gateway["certfile"], minicpm)),
                    "--ssl-keyfile", str(_resolve_upstream_path(gateway["keyfile"], minicpm)),
                ])
            else:
                cmd.append("--http")
            return cmd, minicpm

        cmd = [
            python, str(RUNTIME_DIR / "esp32_bridge.py"),
            "--device-config", str(self.cfg["devices_file"]),
            "--device", self.current_device,
            "--gateway", f"{gateway['host']}:{gateway['port']}",
            "--audio-endpoint", demo["audio_endpoint"],
            "--image-min-interval-s", str(demo["image_min_interval_s"]),
            "--image-transport", "http",
            "--player-hostapi", demo["player_hostapi"],
            "--player-prebuffer-ms", str(demo["player_prebuffer_ms"]),
            "--connect-wait-s", str(demo["connect_wait_s"]),
            "--connect-retry",
            "--ui-port", str(demo["ui_port"]),
            "--save-session", "sessions",
            "--prompt", self.current_prompt,
        ]
        cmd.append("--gateway-tls" if gateway["tls"] else "--no-tls")
        return cmd, self.cfg["state_dir"]

    def _pump_output(self, name: str, proc: subprocess.Popen[str]) -> None:
        log_path = self.cfg["state_dir"] / "logs" / f"{name}_{time.strftime('%Y%m%d-%H%M%S')}.log"
        try:
            with log_path.open("a", encoding="utf-8", errors="replace") as stream:
                for line in iter(proc.stdout.readline, "") if proc.stdout else ():
                    self._log(name, line)
                    stream.write(line)
                    stream.flush()
        except OSError as exc:
            self._log(name, f"Log file error: {exc}")

    def _spawn(self, name: str) -> bool:
        host, port = self._port_for(name)
        if _port_open(host, port) and not self._owned_alive(name):
            self.status[name] = "crashed"
            self._log(name, f"Port {port} is already owned by another process; refusing to take it over")
            return False
        command, cwd = self._build_command(name)
        display = ["<prompt>" if arg == self.current_prompt else arg for arg in command]
        self._log(name, "$ " + subprocess.list2cmdline(display))
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **kwargs,
            )
        except OSError as exc:
            self.status[name] = "crashed"
            self._log(name, f"Launch failed: {exc}")
            return False
        self.processes[name] = proc
        self.status[name] = "starting"
        threading.Thread(target=self._pump_output, args=(name, proc), daemon=True).start()
        return True

    def _wait_ready(self, name: str, timeout: float) -> bool:
        host, port = self._port_for(name)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._cancel.is_set():
                return False
            proc = self.processes.get(name)
            if proc is None or proc.poll() is not None:
                code = None if proc is None else proc.returncode
                self.status[name] = "crashed"
                self._log(name, f"Exited before ready (code={code})")
                return False
            if _port_open(host, port):
                self.status[name] = "running"
                self._log(name, f"Ready on {host}:{port}")
                return True
            self._cancel.wait(0.4)
        self.status[name] = "crashed"
        self._log(name, f"Ready timeout after {timeout:.0f}s")
        return False

    def _start_one(self, name: str) -> bool:
        if self._owned_alive(name):
            self.status[name] = "running"
            self._log(name, "Already running")
            return True
        if not self._spawn(name):
            return False
        timeout = float(self.cfg[name]["ready_timeout_s"])
        if self._wait_ready(name, timeout):
            return True
        self._stop_one(name, graceful=False)
        return False

    def _preflight_ok(self) -> bool:
        findings = preflight(self.cfg)
        for item in findings:
            self._log("system", f"{item['level'].upper()}: {item['message']}")
        return not any(item["level"] == "error" for item in findings)

    def _start_all_sync(self) -> None:
        if not self._preflight_ok():
            self._log("system", "Start blocked by preflight errors")
            return
        for name in PROCESS_NAMES:
            if self._cancel.is_set() or not self._start_one(name):
                self._log("system", f"Startup stopped at {name}")
                return
        self._log("system", "OpenGlass runtime is ready")

    def start_all(self, device: str | None = None) -> None:
        if device:
            self.current_device = device

        def run() -> None:
            if not self._operation_lock.acquire(blocking=False):
                return
            try:
                self._cancel.clear()
                self._start_all_sync()
            finally:
                self._operation_lock.release()

        threading.Thread(target=run, daemon=True).start()

    def _stop_one(self, name: str, *, graceful: bool) -> None:
        proc = self.processes.get(name)
        if proc is None or proc.poll() is not None:
            self.status[name] = "stopped"
            return
        self.status[name] = "stopping"
        try:
            if os.name == "nt":
                if graceful:
                    try:
                        proc.send_signal(signal.CTRL_BREAK_EVENT)
                        proc.wait(timeout=45)
                    except (OSError, subprocess.TimeoutExpired):
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            capture_output=True,
                            check=False,
                        )
                else:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                        check=False,
                    )
            else:
                os.killpg(proc.pid, signal.SIGINT if graceful else signal.SIGTERM)
                try:
                    proc.wait(timeout=45 if graceful else 8)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
        except OSError as exc:
            self._log(name, f"Stop error: {exc}")
        self.status[name] = "stopped"
        self._log(name, "Stopped")

    def _stop_all_sync(self) -> None:
        for name in reversed(PROCESS_NAMES):
            self._stop_one(name, graceful=name == "demo")
        self._log("system", "All managed processes stopped; worker process tree includes llama-server")

    def stop_all(self) -> None:
        self._cancel.set()

        def run() -> None:
            with self._operation_lock:
                self._stop_all_sync()
                self._cancel.clear()

        threading.Thread(target=run, daemon=True).start()

    def restart_all(self, device: str | None = None) -> None:
        if device:
            self.current_device = device
        self._cancel.set()

        def run() -> None:
            with self._operation_lock:
                self._stop_all_sync()
                self._cancel.clear()
                time.sleep(1)
                self._start_all_sync()

        threading.Thread(target=run, daemon=True).start()

    def stop_demo(self) -> None:
        def run() -> None:
            if not self._operation_lock.acquire(blocking=False):
                return
            try:
                self._stop_one("demo", graceful=True)
            finally:
                self._operation_lock.release()

        threading.Thread(target=run, daemon=True).start()

    def restart_demo(self, prompt: str, device: str | None = None) -> None:
        if prompt:
            self.current_prompt = prompt
        if device:
            self.current_device = device

        def run() -> None:
            if not self._operation_lock.acquire(blocking=False):
                return
            try:
                if not (_port_open(*self._port_for("worker")) and _port_open(*self._port_for("gateway"))):
                    self._log("system", "Worker or gateway is unavailable; use Start All")
                    return
                self._stop_one("demo", graceful=True)
                time.sleep(1)
                self._start_one("demo")
            finally:
                self._operation_lock.release()

        threading.Thread(target=run, daemon=True).start()

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": dict(self.status),
            "logs": {name: list(lines) for name, lines in self.logs.items()},
            "device": self.current_device,
            "prompt": self.current_prompt,
        }

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._cancel.set()
        with self._operation_lock:
            self._stop_all_sync()

    def _poll_loop(self) -> None:
        while not self._closing:
            for name, proc in list(self.processes.items()):
                if proc.poll() is not None and self.status[name] in {"starting", "running"}:
                    self.status[name] = "crashed"
                    self._log(name, f"Process exited (code={proc.returncode})")
            time.sleep(1)


class PanelApi:
    def __init__(self, manager: ProcessManager):
        self.manager = manager

    def get_config(self) -> dict[str, Any]:
        cfg = self.manager.cfg
        scheme = "http"
        return {
            "prompts": self.manager.prompts,
            "devices": [str(device["name"]) for device in self.manager.devices],
            "fpv_url": f"{scheme}://{cfg['demo']['ui_host']}:{cfg['demo']['ui_port']}",
            "preflight": preflight(cfg),
        }

    def start_all(self, device: str | None = None) -> str:
        self.manager.start_all(device)
        return "ok"

    def stop_all(self) -> str:
        self.manager.stop_all()
        return "ok"

    def restart_all(self, device: str | None = None) -> str:
        self.manager.restart_all(device)
        return "ok"

    def stop_demo(self) -> str:
        self.manager.stop_demo()
        return "ok"

    def restart_demo(self, prompt: str, device: str | None = None) -> str:
        self.manager.restart_demo(prompt, device)
        return "ok"

    def poll(self) -> dict[str, Any]:
        return self.manager.snapshot()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenGlass Omni control panel")
    parser.add_argument("--config", help="Path to runtime.local.json")
    parser.add_argument("--check", action="store_true", help="Validate local integration without starting the GUI")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_runtime_config(args.config)
        prompts = load_prompts(cfg["prompts_file"])
        devices = load_devices(cfg["devices_file"])
    except ConfigError as exc:
        print(f"OpenGlass configuration error: {exc}", file=sys.stderr)
        return 2

    findings = preflight(cfg)
    if args.check:
        for item in findings:
            print(f"[{item['level'].upper()}] {item['message']}")
        return 1 if any(item["level"] == "error" for item in findings) else 0

    try:
        import webview
    except ImportError:
        print("pywebview is required. Install runtime/openglass_omni/requirements.txt", file=sys.stderr)
        return 2

    manager = ProcessManager(cfg, prompts, devices)
    api = PanelApi(manager)
    html = (RUNTIME_DIR / "panel.html").read_text(encoding="utf-8")
    webview.create_window(
        "OpenGlass Omni Runtime",
        html=html,
        js_api=api,
        width=1420,
        height=900,
        min_size=(980, 680),
    )
    try:
        webview.start()
    finally:
        manager.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
