# AI Blind Glasses Project

这是一个专为盲人设计的 AI 智能眼镜项目，结合了 ESP32 硬件端与云端/本地大模型（VLM）推理能力，实现实时的场景描述与语音交互。

## 项目结构

```text
.
├── CameraWebServer_PDM_Audio/   # ESP32 固件代码 (C++)
│   ├── CameraWebServer_PDM_Audio.ino  # 主程序
│   ├── app_httpd.cpp                  # HTTP & WebSocket 服务器逻辑
│   └── camera_pins.h                  # 硬件引脚定义
└── eval_benchmark/              # 实验评估与推理脚本 (Python)
    ├── configs/                 # 实验配置文件 (.yaml)
    ├── scripts/                 # 常用运行脚本 (.py, .sh, .bat)
    ├── src/                     # 核心评估逻辑
    ├── 02_batch_infer_stream_tts_realtime_eval_v6_wifi_capture.py # 实时推理与 TTS 评估
    ├── manifest_nlp_v6_mixedbest.csv  # 评估数据集清单
    └── requirements.txt         # Python 依赖
```

## 1. 硬件端 (ESP32)

`CameraWebServer_PDM_Audio` 文件夹包含了烧录到 ESP32-S3 开发板（如 [Seeed Studio XIAO ESP32S3](https://wiki.seeedstudio.com/cn/xiao_esp32s3_getting_started/)的固件。


### 功能
- **并发模式**：摄像头 (DVP) 与 PDM 麦克风 (I2S) 同时运行。
- **Web 服务**：提供 JPEG 抓图、MJPEG 视频流及 WebSocket PCM16 音频流。

### 烧录说明
1. 使用 [Arduino IDE](https://www.arduino.cc/en/software/) 打开 `CameraWebServer_PDM_Audio.ino`。
2. 开发板选择 `XIAO_ESP32S3`，PSRAM 开启 `OPI PSRAM`, `Tools` 里的其他设置可以参考[Seeed Studio XIAO ESP32S3 文档](https://wiki.seeedstudio.com/xiao_esp32s3_getting_started/)。
3. 填写 WiFi 信息 (`ssid`, `password`)。
4. 编译并烧录。

---

## 2. 实验评估 (eval_benchmark)

该模块用于运行各种大模型（Gemini, Qwen, MiniCPM）的推理实验并评估性能（延迟、质量）。

### 环境准备
```bash
cd eval_benchmark
pip install -r requirements.txt
```

### 常用运行命令

#### 启动 MiniCPM-V 4.5 llama.cpp

参考 [MiniCPM-V-CookBook](https://github.com/OpenSQZ/MiniCPM-V-CookBook/blob/main/deployment/llama.cpp/minicpm-v4_5_llamacpp_zh.md).

```
cd PATH_To_YOUR_LLAMACPP\llama.cpp\build\bin\Release

llama-server.exe -m "PATH_TO_YOUR_MODEL\ggml-model-Q4_K_M.gguf" --mmproj "PATH_TO_YOUR_PROJMODEL\mmproj-model-f16.gguf" -c 4096 -ngl 99 --port 8080 --host 0.0.0.0
```

同理，MiniCPM-o 也支持 Llama.cpp server 的方式进行使用。

#### 开放式推理与评测

```python
python 02_batch_infer_llamacpp_nlp_v3_nothink.py
```

```
02_batch_infer_llamacpp_nlp_v3_nothink.py：非思考模式，有时会拍脑袋给意见，时间延迟更低。
manifest_nlp_v6_mixedbest.csv：根据多次实验选出的各个子任务最适合的prompt的合集
predictions_nlp_v6_mixedbest_nothink.csv：非思考模式模型的输出
```


#### 运行消融实验
```bash
python eval_benchmark/scripts/run_local_only.py
```

#### 运行特定配置 (如 resize 448 消融实验)
```bash
python -m eval_benchmark.src.run_eval --config eval_benchmark/configs/ablation_resize_448.yaml
```

#### 运行 Wi-Fi E2E 实验 (连接 ESP32 摄像头)
```bash
python eval_benchmark/scripts/run_wifi_e2e.py --camera_url http://<ESP32_IP>/capture
```

#### 运行 MiniCPM-o 实验
```
python eval_benchmark/scripts/run_omni_experiments.py
```

#### 调用云端 API (Gemini, GPT, Qwen)
在使用前请设置对应的 API Key 和代理：
```powershell
# Anaconda Prompt 示例
set HTTP_PROXY=http://127.0.0.1:7897
set HTTPS_PROXY=http://127.0.0.1:7897

# 运行 Gemini 2.5 Flash 实验
set GOOGLE_API_KEY="你的_Google_API_KEY"
python eval_benchmark/scripts/run_cloud_api.py --provider gemini25

# 运行 Qwen 实验
set DASHSCOPE_API_KEY="你的_Qwen_API_KEY"
python eval_benchmark/scripts/run_cloud_api.py --provider qwen
```

#### 实时流式推理与 TTS 评估
```bash
python eval_benchmark/02_batch_infer_stream_tts_realtime_eval_v6_wifi_capture.py \
    --manifest manifest_nlp_v6_mixedbest.csv \
    --camera_url http://<ESP32_IP>/capture \
    --use_camera 1 \
    --pack_mode raw \
    --out predictions_v6_wifi_raw.csv \
    --log predictions_v6_wifi_raw_log.txt
```

#### 汇总实验结果
```bash
python -m eval_benchmark.src.aggregate --runs_dir eval_benchmark/runs --out_dir eval_benchmark
```


### 实际使用 Demo 

```
python demo_asr_vlm_stream_tts_glasses_esp32mic_v4_vad.py --mic_ws ws://YOUR_ESP32_IP/ws_audio --camera_url http://YOUR_ESP32_IP/capture --openai_base http://127.0.0.1:8080/v1 --openai_model "ggml-model-Q4_K_M.gguf" --whisper_model tiny --max_edge 896 --rotate 90
```


## 许可证

这个项目遵守的是 [Apache-2.0 License](LICENSE) 协议.
