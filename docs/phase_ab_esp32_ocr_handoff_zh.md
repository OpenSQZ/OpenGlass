# Phase A / Phase B 与 ESP32 OCR 开发交接（V1.1）

更新时间：2026-08-20
工作分支：`codex/phase-b-v1.1-esp32-ocr-handoff`

本文面向接手 OCR、文字检测和轻量 CV 插件的开发者。它记录当前已经验证的能力、Windows Anaconda Prompt 启动顺序，以及哪些文件可以修改。本文中的命令都在 **Anaconda Prompt（cmd）** 中逐条执行，不使用 PowerShell 的反引号续行。

> 安全边界：实验性避障不是导航或人身安全功能。CV V1.1 只做 shadow 观测和日志，不直接控制会话、播报或运动决策。

## 1. 两个阶段和代码所有权

Phase A 和 Phase B 共用同一个控制核心，只替换设备适配层：

```text
Phase A: Chrome JPEG/PCM -> Browser Adapter --+
                                             +-> Harness Core / FunASR / Router / StateMachine
Phase B: Rokid 或 ESP32 -> Device Adapter ----+               |
                                                             v
llama.cpp-omni -> Worker -> Gateway :8040 -> MiniCPM-o 4.5 -> PC 扬声器
```

- `OpenGlass/extensions/assistive_harness/` 是唯一的命令 Router、状态机、Skill YAML、prompt 和控制协议实现。
- OpenGlass 只负责 ESP32/Rokid 的 JPEG、PCM、播放、录制以及可选 CV provider。不要在设备端再复制一套关键词或状态机。
- RESET 和 Skill 切换只替换 Gateway Session，不能重载 Worker 或模型后端。
- CV provider 只返回统一的 `CVObservation`。V1.1 的 ESP32 接入是旁路日志，不能阻塞主音频链路。

## 2. 当前冻结能力

Phase A 浏览器链路已经人工验证：

- `停一下`：立即停止当前播放；
- `恢复对话`：放开播放门，继续当前 Session；
- `重新开始`：以普通聊天 prompt 热重启 Session；
- `回到普通聊天`：从任意 Skill 切回普通聊天，并更换 Session；
- `帮我找一下手机`、`帮我读一下这个杯子上的字`、`帮我描述一下眼前的场景`、`前面有没有障碍`：分别切换找物、识字、场景描述和实验性避障 Skill。

Phase B Rokid + PC 扬声器已经验证真实 JPEG/PCM、STOP、RESUME、RESET 和四种 Skill 切换。V1.1 新增：

- Session 就绪双音提示默认音量从 `0.16` 提到 `0.32`，可用参数调节；
- EchoGuard 把 PC 播放队列和尾音时间纳入“模型正在说话”，减少扬声器回灌误触发；
- Skill 新 Session 建好后自动注入一次原始任务，例如读字或判断前方障碍，避免只切 prompt 不主动回答；
- 终端输出 `[AssistiveHarness][MODEL]`，同时写入模型文字日志；
- 明确支持口令“帮我描述一下”。

仍然保留的边界：一次 Skill 激活目前是一次任务，不是连续 CV 跟踪；PC 扬声器回声只做软件门控，并非声学回声消除；模型生成文本与实际 TTS 发音可能不同。

## 3. 仓库与环境

协作基线：

```text
仓库：https://github.com/OpenSQZ/OpenGlass
分支：codex/phase-b-v1.1-esp32-ocr-handoff
```

同事第一次获取代码：

```bat
git clone --branch codex/phase-b-v1.1-esp32-ocr-handoff --single-branch https://github.com/OpenSQZ/OpenGlass.git OpenGlass
cd /d OpenGlass
git status --short --branch
```

已有 OpenGlass 工作目录则执行：

```bat
git fetch origin
git switch codex/phase-b-v1.1-esp32-ocr-handoff
git pull --ff-only
```

不要直接在协作基线上长期堆叠两个人的实验代码。OCR 同事从基线创建自己的功能分支：

```bat
git switch codex/phase-b-v1.1-esp32-ocr-handoff
git pull --ff-only
git switch -c feature/esp32-ocr-<backend>
```

项目负责人继续 Phase B/debug 时也从同一基线创建独立分支，例如
`feature/phase-b-rokid-debug`。各自通过 PR 合并回协作基线；确认设备回归后，
再向 OpenGlass `main` 提交上游 PR。

完整 MiniCPM 后端仍是外部依赖，但 Harness 与 OpenGlass 设备/CV 代码现在都由
这一个协作分支提供：

```text
<任意目录>\MiniCPM-o-Demo-Comni    # 外部 Worker/Gateway/网页后端
<任意目录>\OpenGlass               # 本分支：Harness + Phase B + CV/OCR
```

OpenGlass 分支检查：

```bat
cd /d C:\path\to\OpenGlass
git switch codex/phase-b-v1.1-esp32-ocr-handoff
git status --short --branch
```

本分支已经包含完整 Harness Core、Rokid Phase B 入口、浏览器薄适配资产和
ESP32/CV 插件骨架。同事不再需要接收本机 MiniCPM 工作目录归档。只有
`llama.cpp-omni -> Worker -> Gateway` 后端和模型权重继续作为外部依赖。

本次文件边界如下：

```text
OpenGlass/
  extensions/assistive_harness/model_log.py                 # 新增模型回合聚合
  extensions/assistive_harness/server.py                    # MODEL/playback telemetry
  extensions/assistive_harness/telemetry.py                 # 模型日志文件
  extensions/assistive_harness/registry.py                  # task_trigger 渲染
  extensions/assistive_harness/config/skills.example.yaml   # 四类一次性任务/口令
  extensions/assistive_harness/phase_b/rokid_runtime.py      # V1.1 播放与触发
  extensions/assistive_harness/phase_b/README.md             # Rokid 使用说明
  extensions/assistive_harness/tests/                        # 回归测试
  demo_rokid_phase_b_harness.py                              # Rokid 稳定入口
  integrations/minicpm_browser/                             # Phase A 浏览器薄适配资产
  runtime/openglass_omni/esp32_bridge.py                     # 非阻塞 CV 挂点/CLI
  runtime/openglass_omni/perception/                         # provider/pipeline/YOLO/OCR
  runtime/openglass_omni/requirements-cv.txt                 # 可选 CV 依赖
  runtime/openglass_omni/tests/test_perception.py            # 插件隔离/背压测试
  docs/phase_ab_esp32_ocr_handoff_zh.md                      # 本文
```

ESP32 已烧录连接 `CUDY-D102` 的固件。电脑也要连接同一网络，并从串口日志或路由器页面取得 ESP32 的当前 IPv4 地址。运行参数只使用 IP；不要把 SSID 密码、私人 IP 或模型权重提交到 Git。

安装基础与可选 CV 依赖：

```bat
conda activate ai_glasses
cd /d C:\path\to\OpenGlass
python -m pip install -r runtime\openglass_omni\requirements.txt
python -m pip install -r runtime\openglass_omni\requirements-cv.txt
python -m pip install -r extensions\assistive_harness\requirements.txt
python -m pip install -r extensions\assistive_harness\phase_b\requirements-phase-b.txt
```

OCR 后端若需要 PaddleOCR、ONNX Runtime、TensorRT 等额外依赖，请单独增加 `requirements-ocr-<backend>.txt`，不要把重型 OCR 依赖塞入基础 requirements。

## 4. 启动 MiniCPM-o、Harness 和 Phase B

### 4.1 后端：llama.cpp-omni -> Worker -> Gateway :8040

打开第一个 Anaconda Prompt：

```bat
conda activate ai_glasses
cd /d C:\path\to\MiniCPM-o-Demo-Comni
start_all.cmd --http
```

等待后端、Worker 和 Gateway 就绪。Phase B 本机链路使用 `ws://localhost:8040`；不要在未启用 TLS 时改成 `wss://`。

### 4.2 Harness Core :8021

`--model-path` 表示本机目录，不是 ModelScope 模型 ID，也不会触发隐式下载。
第一次使用时，在 OpenGlass 根目录显式下载公开模型：

```bat
conda activate ai_glasses
cd /d C:\path\to\OpenGlass
python -m extensions.assistive_harness.download_modelscope_model
```

ModelScope 默认下载到 `%USERPROFILE%\.cache\modelscope\hub`，命令末尾会打印
实际 `MODEL_PATH` 和可直接复制的启动命令。之后打开第二个 Anaconda Prompt：

```bat
conda activate ai_glasses
cd /d C:\path\to\OpenGlass
python -m extensions.assistive_harness.server --enabled --model-path "%USERPROFILE%\.cache\modelscope\hub\models\iic\speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online" --port 8021
```

成功后会看到 FunASR ready 和 Uvicorn `127.0.0.1:8021`。控制与模型日志位于：

```text
extensions/assistive_harness/runs/<run-id>/control_events.jsonl
extensions/assistive_harness/runs/<run-id>/session_events.jsonl
extensions/assistive_harness/runs/<run-id>/model_events.jsonl
extensions/assistive_harness/runs/<run-id>/model_transcript.txt
```

`model_transcript.txt` 是模型生成文字，不是对 TTS 波形重新做的识别。核对实际播报仍应保留录音或 Session WAV。

### 4.3 Chrome Phase A 回归

前两个 Prompt 就绪后，用 Chrome 打开带显式开关的页面。浏览器薄适配资产
位于 `integrations/minicpm_browser/`，必须先按其中 README 接入目标版本的
MiniCPM-o-Demo；不要用一个完整 `omni-app.js` 覆盖不同上游版本。

```text
http://127.0.0.1:8040/omni?assistive_harness=1&v=phase-b-v1-1
```

Harness 默认关闭，不能省略 `assistive_harness=1`。只保留一个 Live 页面，依次复测
STOP、RESUME、RESET、四种 Skill 和 Skill 互切。原生回归则另开
`http://127.0.0.1:8040/omni`，确认不带参数时仍是原始 Demo 行为。

### 4.4 Rokid Phase B（项目负责人继续测试）

打开第三个 Anaconda Prompt：

```bat
conda activate ai_glasses
cd /d C:\path\to\OpenGlass
python demo_rokid_phase_b_harness.py --gateway localhost:8040 --harness-url ws://127.0.0.1:8021/ws/control --input-gain 12 --image-rotate-cw 270 --session-ready-chime-volume 0.32 --playback-echo-tail-s 0.80
```

第一轮仍使用电脑扬声器。音量参数范围为 `0.0` 到 `1.0`；先用 `0.32`，不要为盖过回声无上限增大。`--playback-echo-tail-s` 是播放队列排空后的额外抑制时间，不是声学 AEC。

### 4.5 ESP32 主链路 smoke test（OCR 同事）

先验证 ESP32 输入与 MiniCPM 主链路；把 `<ESP32_IP>` 替换为设备当前地址。
这里的 `SenseVoiceSmall` 是 ESP32 bridge 的可选本地 ASR 模型，与 Harness
使用的 Paraformer 模型不是同一个目录。第一次使用时显式下载：

```bat
conda activate ai_glasses
cd /d C:\path\to\OpenGlass
python -m extensions.assistive_harness.download_modelscope_model --model-id iic/SenseVoiceSmall
```

下载程序会打印实际目录。把该目录传给 `--funasr-model`：

```bat
conda activate ai_glasses
cd /d C:\path\to\OpenGlass
python runtime\openglass_omni\esp32_bridge.py --esp32-host <ESP32_IP> --esp32-port 80 --gateway localhost:8040 --no-tls --rotate 180 --enable-funasr --funasr-model "%USERPROFILE%\.cache\modelscope\hub\models\iic\SenseVoiceSmall" --funasr-echo-suppress-s 4.0 --prompt "你是智能眼镜助手。用户问什么就简短回答什么；只有用户要求描述场景或寻找物体时才看图回答，不要主动描述。"
```

若现有固件、Gateway revision 或端口与本机验证环境不同，以当前健康检查和设备日志为准，但不要改写 Router/状态机。先确认音频连续、JPEG 在更新、Gateway 正常收到数据，再叠加 OCR。

## 5. YOLO 参考插件 smoke test

YOLO 只是验证 provider 接口、异常隔离和非阻塞队列，不是最终找物策略。模型路径使用本机实际位置：

```bat
conda activate ai_glasses
cd /d C:\path\to\OpenGlass
python runtime\openglass_omni\esp32_bridge.py --esp32-host <ESP32_IP> --esp32-port 80 --gateway localhost:8040 --no-tls --rotate 180 --cv-shadow-provider yolo_onnx --cv-shadow-options-json "{\"model_path\":\"C:\\path\\to\\OpenGlass\\models\\yolo26n.onnx\",\"device\":\"cpu\"}" --cv-shadow-skill find_object --cv-shadow-slots-json "{\"target\":\"手机\"}" --cv-shadow-timeout-ms 2000 --cv-shadow-log logs\cv_yolo.jsonl
```

预期：终端持续出现 `[CV]`，`logs\cv_yolo.jsonl` 每行是一条 `CVObservation`；即使模型加载失败或超时，ESP32 PCM 与 MiniCPM 音频仍应继续。队列容量固定为 1：推理忙时丢弃旧帧，只保留最新等待帧。

## 6. OCR provider 的唯一改动路径

目录：`runtime/openglass_omni/perception/`

1. 复制 `ocr_template.py` 为 `ocr_<backend>.py`。
2. 实现一个同步的 `OcrProvider.analyze()`。模型可延迟加载，但不能创建第二套音频/会话逻辑。
3. 返回 `CVObservation`。建议 `values` 至少包含：

```python
{
    "status": "ok",
    "text": "识别到的完整文本",
    "regions": [
        {"text": "局部文字", "confidence": 0.96, "bbox_xyxy": [x1, y1, x2, y2]}
    ],
    "latency_ms": 83.4,
    "model": "模型名称或版本",
    "device": "cpu"
}
```

4. 用动态路径启动，不必修改 registry：

```bat
python runtime\openglass_omni\esp32_bridge.py --esp32-host <ESP32_IP> --gateway localhost:8040 --no-tls --rotate 180 --cv-shadow-provider perception.ocr_<backend>:OcrProvider --cv-shadow-options-json "{\"model_path\":\"D:\\models\\ocr\"}" --cv-shadow-skill read_text --cv-shadow-log logs\cv_ocr.jsonl
```

5. 模型依赖、权重、缓存和样例图片不要提交；只提交 provider、最小测试、依赖清单和不含隐私的示例结果。

OCR V1 完成标准：真实 ESP32 JPEG 能产出统一结果；异常和超时只形成 observation；音频主链不中断；连续输入发生背压时 `dropped_frames` 增长而内存不增长；至少有一张含中英文的固定测试图和预期文本。

## 7. GitHub 不包含的文件及解决办法

代码、配置模板、测试和启动入口都在本分支中；以下大文件或本机状态有意不进入
Git，不属于代码遗漏：

| 类别 | 原因 | 获取/放置方式 |
|---|---|---|
| Harness Paraformer ASR 权重 | 公开模型体积较大 | 运行 `download_modelscope_model`，使用程序打印的本地路径 |
| ESP32 `SenseVoiceSmall` 权重 | 可选 bridge ASR | 用同一下载程序加 `--model-id iic/SenseVoiceSmall` |
| YOLO 权重 | CV 参考插件的可替换资产 | 自行取得兼容 ONNX 权重，放入 `OpenGlass\models\`，不要提交 |
| OCR 权重/字典 | 由具体 OCR 后端决定 | 放在开发者本机模型目录，通过 provider options 传路径 |
| MiniCPM-o 4.5/llama.cpp-omni 权重和 Worker/Gateway | 独立后端、体积大 | 按后端仓库安装并启动 `:8040`，OpenGlass 不复制模型 |
| Wi-Fi 密码、设备 IP、日志、录音 | 私密或运行时数据 | 只保存在本机，通过命令行参数传入 |

因此，克隆本分支后可以直接安装依赖、运行单元测试、启动 Harness 和设备适配层；
要得到完整 AI 回答，还必须在本机准备公开 ASR 权重，并启动外部 MiniCPM
Worker/Gateway。OCR 同事只需额外提供其选择的 OCR 依赖和权重。

## 8. 测试

OpenGlass CV 骨架：

```bat
conda activate ai_glasses
cd /d C:\path\to\OpenGlass
python -m unittest discover runtime\openglass_omni\tests -v
```

Harness 核心与 Rokid Phase B：

```bat
conda activate ai_glasses
cd /d C:\path\to\OpenGlass
python -m unittest extensions.assistive_harness.tests.test_core extensions.assistive_harness.tests.test_phase_b_rokid -v
```

设备验收顺序：普通问答 -> `停一下` -> `恢复对话` -> `重新开始` -> 找物 -> 识字 -> 场景描述 -> 实验性避障 -> Skill 直接互切 -> 断网重连。每次 RESET/Skill 切换应看到 generation 增加、新 Session ID、`restart_complete`，Skill 还应看到一次 `task_trigger_sent`。

## 9. 协作与后续集成

- OCR 同事只在本 OpenGlass 分支新增 provider、测试和依赖文件；不要修改 Phase A 的关键词和状态机。
- 项目负责人继续在 Rokid 真实设备上校准回声、提示音和 Session 稳定性。
- OCR observation 反馈给 Skill、连续读字和主动播报属于下一阶段。先冻结 shadow 输出格式，再设计结果消费者，避免模型实现与控制策略耦合。
- 当前 timeout 会及时产生日志并让音频继续，但 Python 线程不能强制终止已经进入原生库的推理调用；需要硬隔离的模型应在后续改成独立进程 provider。
- `codex/phase-b-v1.1-esp32-ocr-handoff` 是 Phase B/ESP32/OCR 的共同起点；
  新实验通过短期功能分支和 PR 回合，不直接重写基线历史。
