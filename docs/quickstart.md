# Quickstart

This quickstart prioritizes flows that are present in the tracked repository. It avoids private paths, private device addresses, and prototype-only launcher commands.

## A. Repository Prerequisites

Install the basic tools for the part of the repository you want to use:

- Git for cloning and inspecting the repository.
- Arduino IDE or Arduino CLI for ESP32 firmware work.
- Python 3.10 or newer for evaluation scripts.
- A local OpenAI-compatible VLM service if you want to run non-stub local inference.

From the repository root:

```bash
cd eval_benchmark
python -m pip install -r requirements.txt
cd ..
```

## B. ESP32 Sensing Firmware

The current firmware is in `CameraWebServer_PDM_Audio/`.

1. Open `CameraWebServer_PDM_Audio/CameraWebServer_PDM_Audio.ino` in Arduino IDE.
2. Select the ESP32-S3 board profile matching your hardware.
3. Replace `YOUR_WIFI_NAME` and `YOUR_WIFI_PASSWORD` locally; do not commit real credentials.
4. Compile and flash the firmware.
5. Use the serial monitor to read the address printed after `[WiFi] Connected! IP:` and record it as `<ESP32_IP>`.

The sanitized example header in `examples/configs/esp32_wifi.example.h` is documentation-only in Phase 1 and is not yet wired into the current firmware include structure.

The ESP32 and nearby inference host must be able to reach each other. DHCP addresses can change after a restart, so update the local device registry when needed or configure a DHCP reservation. See [`../CameraWebServer_PDM_Audio/README.md`](../CameraWebServer_PDM_Audio/README.md) for endpoint tests and the complete device-registration flow.

## C. Local VLM / Evaluation Environment

The evaluation configs use an OpenAI-compatible local server shape. Start your local model service separately according to the upstream model/runtime you use, then point configs at a base URL such as:

```text
http://127.0.0.1:8080/v1/chat/completions
```

Use placeholders in notes and scripts:

- `<MODEL_PATH>`
- `<MODEL_NAME>`
- `<OPENAI_COMPATIBLE_BASE_URL>`
- `<OUTPUT_PATH>`

Do not commit model weights, model cache paths, or machine-specific paths.

## D. Running an Existing Public Evaluation Command

For a lightweight command that exercises the public evaluation framework without requiring a local model server, run the stub-style config:

```bash
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/cloud_api.yaml
```

When a local OpenAI-compatible VLM server is running, run an existing local config:

```bash
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ours_full.yaml
```

Aggregate generated runs with:

```bash
python -m eval_benchmark.src.aggregate --runs_dir eval_benchmark/runs --out_dir eval_benchmark
```

Generated run outputs should not be committed unless they have been explicitly selected and reviewed.

## E. Connecting to an ESP32 Capture Endpoint

After flashing the firmware and finding the ESP32 address, use:

```bash
python eval_benchmark/scripts/run_wifi_e2e.py --camera_url http://<ESP32_IP>/capture
```

The `<ESP32_IP>` placeholder should be replaced only in your local shell. Do not commit private network addresses.

For the modular ASR/VLM/TTS demo path, inspect the tracked demo script and use placeholders for the microphone WebSocket, camera URL, model name, and local base URL. The model still runs on the nearby host, not on the ESP32.

## F. Optional Cloud Baselines

Cloud adapters are optional evaluation baselines. They are not the default deployment path and may transmit frames or prompts to external services.

Use environment variables rather than hard-coded keys:

```powershell
set GOOGLE_API_KEY=YOUR_GOOGLE_API_KEY
set DASHSCOPE_API_KEY=YOUR_QWEN_API_KEY
```

Then run the relevant public script only after reviewing provider terms and data-handling requirements.

## G. Experimental Omni Runtime Status

A standalone experimental launcher, device parser, ESP32 bridge, and lifecycle manager are now included. They use external MiniCPM-o-Demo and llama.cpp-omni Git checkouts instead of vendoring upstream code.

Create the Git-ignored runtime and device configuration files:

```powershell
Copy-Item runtime\openglass_omni\runtime.example.json runtime\openglass_omni\runtime.local.json
Copy-Item examples\configs\devices.example.json runtime\openglass_omni\devices.local.json
```

Replace the example `esp32_host` in `devices.local.json` with `<ESP32_IP>` from Serial Monitor. The host value must not include `http://`, a port, or an endpoint path. The tracked firmware uses HTTP port `80` and audio endpoint `/ws_audio`.

From the repository root, validate local paths and existing build artifacts without loading the model:

```powershell
python glasses_panel.py --check
```

After configuring `runtime/openglass_omni/runtime.local.json`, launch the panel with:

```powershell
python glasses_panel.py
```

See [`../runtime/openglass_omni/README.md`](../runtime/openglass_omni/README.md) for setup and lifecycle behavior. Clean-machine validation, tracked-firmware endpoint compatibility, long-session behavior, skill switching, and Rokid publication remain unresolved.

## H. Troubleshooting Links

- Architecture boundary: [`architecture.md`](architecture.md)
- Safety and privacy limits: [`safety_privacy.md`](safety_privacy.md)
- Release gates: [`release_checklist.md`](release_checklist.md)
- Runtime status: [`../runtime/README.md`](../runtime/README.md)
