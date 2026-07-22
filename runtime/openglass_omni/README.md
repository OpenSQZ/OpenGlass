# OpenGlass Omni 独立运行时适配层

这个目录保存 OpenGlass 自己的控制面板、ESP32 音视频 bridge、session 录制与回放代码。MiniCPM-o-Demo 和 llama.cpp-omni 保持为外部 Git 仓库，不需要把 OpenGlass 文件复制进任何上游目录。

当前状态是实验性研究集成，不代表生产可用、医疗器械认证、认证导航安全或无限时长稳定会话。

## 进程结构

```text
OpenGlass glasses_panel.py
  ├─ MiniCPM-o-Demo/worker.py
  │    └─ existing llama.cpp-omni/build/.../llama-server
  ├─ MiniCPM-o-Demo/gateway.py
  └─ OpenGlass/runtime/openglass_omni/esp32_bridge.py
       ├─ ESP32 camera/audio input
       ├─ local session recording and rerun source
       └─ local first-person Web UI
```

大型模型运行在附近笔记本或边缘主机上。ESP32 只负责感知与通信。

## 获取外部依赖

两个上游项目都可直接从 GitHub 克隆到任意外部目录：

```powershell
git clone --branch feat/web-demo https://github.com/tc-mb/llama.cpp-omni.git
git clone --branch Comni https://github.com/OpenBMB/MiniCPM-o-Demo.git
```

OpenGlass 不会自动下载、覆盖或编译 llama.cpp-omni。当前本机适配会直接使用已经存在的 `llama-server.exe`。其他机器需按 llama.cpp-omni 上游说明自行完成一次编译。

本次迁移时观察到的上游分支与 commit 记录在 [`upstream-lock.json`](upstream-lock.json)。该记录尚未完成干净机器验证，不等同于正式兼容性保证。

## 环境准备

在同一个 Python 环境中安装 MiniCPM-o-Demo 自身依赖和 OpenGlass bridge 依赖：

```powershell
pip install -r <MINICPM_O_DEMO_ROOT>\requirements.txt
pip install -r runtime\openglass_omni\requirements.txt
```

在 MiniCPM-o-Demo 外部仓库中从其示例创建本地 `config.json`，并按上游字段配置：

- `backend` 为 `cpp`；
- `cpp_backend.llamacpp_root` 指向已有 llama.cpp-omni checkout；
- `cpp_backend.model_dir` 与 `llm_model` 指向本地 GGUF；
- C++ server、worker 与 gateway 端口互不冲突；
- HTTPS 模式需要本地证书和私钥。

这个 `config.json` 由 MiniCPM-o-Demo 自己读取。OpenGlass 只做只读检查，不生成或改写它。

## OpenGlass 本地配置

复制示例：

```powershell
Copy-Item runtime\openglass_omni\runtime.example.json runtime\openglass_omni\runtime.local.json
Copy-Item examples\configs\devices.example.json runtime\openglass_omni\devices.local.json
```

编辑被 Git 忽略的 `runtime.local.json`：

- `minicpm_demo_root`：外部 MiniCPM-o-Demo checkout；
- `llama_cpp_omni_root`：外部 llama.cpp-omni checkout；
- `devices_file`：本地设备表；示例默认读取同目录下的 `devices.local.json`；
- `python`：留空时使用启动面板的当前 Python；
- worker/gateway 端口应与 MiniCPM-o-Demo 本地配置一致；
- `audio_endpoint` 必须与 ESP32 固件协议匹配，可选 `/ws_audio` 或 `/ws_audio_v2`。

设备表示例位于 [`../../examples/configs/devices.example.json`](../../examples/configs/devices.example.json)。示例使用 TEST-NET 地址，不能直接连接真实设备。

### 配置眼镜 IP

烧录 ESP32 固件前，在 `CameraWebServer_PDM_Audio/CameraWebServer_PDM_Audio.ino` 中只在本地填写 `YOUR_WIFI_NAME` 和 `YOUR_WIFI_PASSWORD`。烧录后打开串口监视器，把 `[WiFi] Connected! IP:` 后面的地址记为 `<ESP32_IP>`。

编辑被 Git 忽略的 `runtime/openglass_omni/devices.local.json`：

```json
{
  "devices": [
    {
      "name": "My Glasses",
      "esp32_host": "<ESP32_IP>",
      "esp32_port": 80,
      "rotate": 270
    }
  ]
}
```

`esp32_host` 只填写 IP 或可解析的主机名，不要带 `http://`、路径或端口。`name` 会显示在面板中，`rotate` 按摄像头安装方向设置。仓库内固件提供 `/ws_audio`；只有烧录了对应的实验固件时才选择 `/ws_audio_v2`。

确保 `runtime.local.json` 包含：

```json
"devices_file": "devices.local.json"
```

眼镜和运行 Runtime 的主机必须处在可互相访问的网络中。DHCP 地址可能在设备或路由器重启后变化，此时需要更新 `devices.local.json`，也可以在路由器中配置 DHCP 地址保留。完整固件配置见 [`../../CameraWebServer_PDM_Audio/README.md`](../../CameraWebServer_PDM_Audio/README.md)。真实 Wi-Fi 凭据和设备 IP 都不能提交到 Git。

## 启动

先做不会加载模型的检查：

```powershell
conda activate ai_glasses
python glasses_panel.py --check
```

检查通过后，从 OpenGlass 根目录启动控制面板：

```powershell
python glasses_panel.py
```

当前本机的 `runtime.local.json` 已指向 `WAIC_use` 中现有的两个上游 checkout，并复用已有 llama.cpp-omni 编译产物。

## 面板按钮语义

| 操作 | 实际行为 |
| --- | --- |
| 一键启动 | 依次启动 upstream worker、upstream gateway、OpenGlass ESP32 bridge |
| 重启 | 只重启 OpenGlass bridge，并应用当前眼镜与 Prompt；模型和 gateway 保持运行 |
| 停止 | 只优雅停止 OpenGlass bridge，以便 session 落盘 |
| 全部重启 | 逆序停止三个托管进程，再按启动顺序重开 |
| 全部停止 | 停止 bridge、gateway 和 worker；worker 的进程树包含它启动的 llama-server |
| 关闭窗口 | 同步执行全部停止 |

面板拒绝接管已经占用目标端口但不属于本面板的进程，避免误杀手工启动的服务。

## Session 和隐私

本地运行输出写入 `runtime/openglass_omni/state/`，包括日志、图像、音频和 session 元数据。该目录已被 Git 忽略。发布或共享前必须人工检查其中是否包含人脸、环境画面、声音、设备地址、个人信息或未公开实验记录。

## 当前边界

- ESP32 路径已迁入，Rokid 源码和 APK 未提供，因此本面板不显示虚假的 Rokid 可用状态。
- Prompt 切换通过只重启 OpenGlass bridge 生效，不重启 worker、gateway 或模型。
- bridge 已保留本地 session rerun CLI，但面板中的 session 选择与一键 rerun 控件仍待实现。
- skill 导入与 session 重启链路仍是实验计划，不能描述为已解决的技能平台。
- tracked 固件当前公开端点与现场 `/ws_audio_v2` 协议仍需实机对齐。
