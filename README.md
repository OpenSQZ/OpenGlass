# AI Blind Glasses Project

An AI-powered smart glasses project designed for the visually impaired, combining ESP32 hardware with cloud/local Large Vision-Language Model (VLM) inference capabilities to enable real-time scene description and voice interaction.

## Project Structure

```text
.
├── CameraWebServer_PDM_Audio/   # ESP32 firmware code (C++)
│   ├── CameraWebServer_PDM_Audio.ino  # Main program
│   ├── app_httpd.cpp                  # HTTP & WebSocket server logic
│   └── camera_pins.h                  # Hardware pin definitions
└── eval_benchmark/              # Experiment evaluation & inference scripts (Python)
    ├── configs/                 # Experiment configuration files (.yaml)
    ├── scripts/                 # Common run scripts (.py, .sh, .bat)
    ├── src/                     # Core evaluation logic
    ├── 02_batch_infer_stream_tts_realtime_eval_v6_wifi_capture.py # Real-time inference & TTS evaluation
    ├── manifest_nlp_v6_mixedbest.csv  # Evaluation dataset manifest
    └── requirements.txt         # Python dependencies
```

## 1. Hardware (ESP32)

The `CameraWebServer_PDM_Audio` folder contains firmware to be flashed to an ESP32-S3 development board (e.g., [Seeed Studio XIAO ESP32S3](https://wiki.seeedstudio.com/xiao_esp32s3_getting_started/)).


### Features
- **Concurrent Mode**: Camera (DVP) and PDM microphone (I2S) run simultaneously.
- **Web Services**: Provides JPEG capture, MJPEG video stream, and WebSocket PCM16 audio stream.

### Flashing Instructions
1. Open `CameraWebServer_PDM_Audio.ino` in [Arduino IDE](https://www.arduino.cc/en/software/).
2. Select board `XIAO_ESP32S3`, enable `OPI PSRAM`. For other settings in `Tools`, refer to the [Seeed Studio XIAO ESP32S3 documentation](https://wiki.seeedstudio.com/xiao_esp32s3_getting_started/).
3. Fill in WiFi credentials (`ssid`, `password`).
4. Compile and flash.

---

## 2. Experiment Evaluation (eval_benchmark)

This module is used to run inference experiments with various large models (Gemini, Qwen, MiniCPM) and evaluate performance (latency, quality).

### Environment Setup
```bash
cd eval_benchmark
pip install -r requirements.txt
```

### Common Commands

#### Start MiniCPM-V 4.5 llama.cpp

Refer to [MiniCPM-V-CookBook](https://github.com/OpenSQZ/MiniCPM-V-CookBook/blob/main/deployment/llama.cpp/minicpm-v4_5_llamacpp.md).

```
cd PATH_To_YOUR_LLAMACPP\llama.cpp\build\bin\Release

llama-server.exe -m "PATH_TO_YOUR_MODEL\ggml-model-Q4_K_M.gguf" --mmproj "PATH_TO_YOUR_PROJMODEL\mmproj-model-f16.gguf" -c 4096 -ngl 99 --port 8080 --host 0.0.0.0
```

Similarly, MiniCPM-o also supports the Llama.cpp server deployment method.

#### Open-ended Inference & Evaluation

```python
python 02_batch_infer_llamacpp_nlp_v3_nothink.py
```

```
02_batch_infer_llamacpp_nlp_v3_nothink.py: Non-thinking mode, sometimes gives quick judgments, lower latency.
manifest_nlp_v6_mixedbest.csv: Collection of best prompts for each subtask selected through multiple experiments.
predictions_nlp_v6_mixedbest_nothink.csv: Model output in non-thinking mode.
```


#### Run Ablation Experiments
```bash
python eval_benchmark/scripts/run_local_only.py
```

#### Run Specific Configuration (e.g., resize 448 ablation)
```bash
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ablation_resize_448.yaml
```

#### Run Wi-Fi E2E Experiment (connect to ESP32 camera)
```bash
python eval_benchmark/scripts/run_wifi_e2e.py --camera_url http://<ESP32_IP>/capture
```

#### Run MiniCPM-o Experiments
```
python eval_benchmark/scripts/run_omni_experiments.py
```

#### Call Cloud APIs (Gemini, GPT, Qwen)
Before using, set the corresponding API Key and proxy:
```powershell
# Anaconda Prompt example
set HTTP_PROXY=http://127.0.0.1:7897
set HTTPS_PROXY=http://127.0.0.1:7897

# Run Gemini 2.5 Flash experiment
set GOOGLE_API_KEY="YOUR_GOOGLE_API_KEY"
python eval_benchmark/scripts/run_cloud_api.py --provider gemini25

# Run Qwen experiment
set DASHSCOPE_API_KEY="YOUR_QWEN_API_KEY"
python eval_benchmark/scripts/run_cloud_api.py --provider qwen
```

#### Real-time Streaming Inference & TTS Evaluation
```bash
python eval_benchmark/02_batch_infer_stream_tts_realtime_eval_v6_wifi_capture.py \
    --manifest manifest_nlp_v6_mixedbest.csv \
    --camera_url http://<ESP32_IP>/capture \
    --use_camera 1 \
    --pack_mode raw \
    --out predictions_v6_wifi_raw.csv \
    --log predictions_v6_wifi_raw_log.txt
```

#### Aggregate Experiment Results
```bash
python -m eval_benchmark.src.aggregate --runs_dir eval_benchmark/runs --out_dir eval_benchmark
```


### Practical Usage Demo

```
python demo_asr_vlm_stream_tts_glasses_esp32mic_v4_vad.py --mic_ws ws://YOUR_ESP32_IP/ws_audio --camera_url http://YOUR_ESP32_IP/capture --openai_base http://127.0.0.1:8080/v1 --openai_model "ggml-model-Q4_K_M.gguf" --whisper_model tiny --max_edge 896 --rotate 90
```


## License

This project is served under the [Apache-2.0 License](LICENSE).
