from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runtime.openglass_omni.config import load_devices, load_prompts, load_runtime_config
from runtime.openglass_omni.panel import ProcessManager, preflight


class OmniPanelIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.minicpm = self.root / "MiniCPM-o-Demo"
        self.llama = self.root / "llama.cpp-omni"
        self.model_dir = self.root / "models"
        self.minicpm.mkdir()
        self.model_dir.mkdir()
        (self.llama / "build" / "bin").mkdir(parents=True)
        (self.minicpm / "certs").mkdir()
        for filename in ("worker.py", "gateway.py", "config.py"):
            (self.minicpm / filename).write_text("# test fixture\n", encoding="utf-8")
        (self.llama / "build" / "bin" / "llama-server.exe").write_bytes(b"fixture")
        (self.model_dir / "model.gguf").write_bytes(b"fixture")
        (self.minicpm / "certs" / "cert.pem").write_text("fixture", encoding="utf-8")
        (self.minicpm / "certs" / "key.pem").write_text("fixture", encoding="utf-8")
        self._write_json(
            self.minicpm / "config.json",
            {
                "backend": "cpp",
                "service": {"gateway_port": 8040, "worker_base_port": 22440},
                "cpp_backend": {
                    "llamacpp_root": str(self.llama),
                    "model_dir": str(self.model_dir),
                    "llm_model": "model.gguf",
                },
            },
        )
        self.devices = self.root / "devices.local.json"
        self.prompts = self.root / "prompts.json"
        self._write_json(
            self.devices,
            {"devices": [{"name": "Test Glasses", "esp32_host": "192.0.2.10", "esp32_port": 80}]},
        )
        self._write_json(self.prompts, {"prompts": {"General": "Answer only from current input."}})
        self.config_path = self.root / "runtime.local.json"
        self._write_json(
            self.config_path,
            {
                "minicpm_demo_root": str(self.minicpm),
                "llama_cpp_omni_root": str(self.llama),
                "devices_file": str(self.devices),
                "prompts_file": str(self.prompts),
                "state_dir": str(self.root / "state"),
                "worker": {"host": "127.0.0.1", "port": 22440},
                "gateway": {
                    "host": "127.0.0.1",
                    "bind_host": "0.0.0.0",
                    "port": 8040,
                    "tls": True,
                },
                "demo": {"ui_host": "127.0.0.1", "ui_port": 8080},
            },
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _write_json(path: Path, data: object) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_preflight_accepts_external_upstreams_and_existing_binary(self) -> None:
        cfg = load_runtime_config(self.config_path)
        findings = preflight(cfg)
        self.assertFalse([item for item in findings if item["level"] == "error"])
        self.assertTrue(any("Existing llama.cpp-omni build" in item["message"] for item in findings))

    def test_process_commands_keep_upstream_and_openglass_workdirs_separate(self) -> None:
        cfg = load_runtime_config(self.config_path)
        manager = ProcessManager(cfg, load_prompts(self.prompts), load_devices(self.devices))
        try:
            worker_cmd, worker_cwd = manager._build_command("worker")
            gateway_cmd, gateway_cwd = manager._build_command("gateway")
            demo_cmd, demo_cwd = manager._build_command("demo")
        finally:
            manager.shutdown()

        self.assertEqual(worker_cwd, self.minicpm)
        self.assertEqual(gateway_cwd, self.minicpm)
        self.assertEqual(demo_cwd, self.root / "state")
        self.assertIn(str(self.minicpm / "worker.py"), worker_cmd)
        self.assertIn(str(self.minicpm / "gateway.py"), gateway_cmd)
        self.assertTrue(any(value.endswith("esp32_bridge.py") for value in demo_cmd))
        self.assertIn(str(self.devices), demo_cmd)

    def test_preflight_rejects_mismatched_llama_checkout(self) -> None:
        cfg = load_runtime_config(self.config_path)
        cfg["llama_cpp_omni_root"] = self.root / "different-llama"
        findings = preflight(cfg)
        errors = [item["message"] for item in findings if item["level"] == "error"]
        self.assertTrue(any("different llama.cpp-omni checkouts" in message for message in errors))


if __name__ == "__main__":
    unittest.main()
