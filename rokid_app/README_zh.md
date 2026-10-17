# Rokid 采集 App

此目录保存 OpenGlass Rokid 采集 App 0.2.0（versionCode 7）：发送 JPEG 与 PCM 音频，可通过 Wi-Fi 直接连接 iPhone OpenGlass，也保留电脑面板和显式 USB 中继模式。它不是眼镜系统固件。

## 文件布局

- `OpenGlassRokidSensor/`：唯一 Android 源码工程，包含 Gradle Wrapper。
- `scripts/build_install.ps1`：构建，可选安装；不启动模型或旧 bridge。
- `build_install.cmd`：同一脚本的 Windows 便捷入口。

安装后可以直接打开眼镜上的采集 App；电脑后端仍可使用仓库根的 `glasses_panel.py`。

## 手机设置

手机下载安装乐奇官方 App Rokid AI，连接眼镜后在设置中开启开发者模式和眼镜 ADB 调试授权。

## 首次准备

安装 JDK 17（或与 Gradle 8.7 / Android 插件 8.5.2 兼容的 JDK）、Android SDK（API 34、构建工具和 Platform Tools）。设置 `JAVA_HOME` 和 `ANDROID_HOME`，或通过下方 `-Sdk` 指定 SDK。首次构建需要下载 Gradle/Maven 依赖。

推荐在 OpenGlass 根目录运行整理后的脚本。**先只编译**：

```powershell
.\rokid_app\build_install.cmd -Sdk "<ANDROID_SDK_PATH>" -Variant Wifi
```

**编译并安装**：连接 USB、确认授权，用 `adb devices -l` 取得设备序列号，再运行：

```powershell
.\rokid_app\build_install.cmd -Sdk "<ANDROID_SDK_PATH>" -Variant Wifi -Install -Serial "<USB_SERIAL>"
```

需要指定 ADB 时增加 `-Adb "<ADB_EXE_PATH>"`。也可直接调用 `scripts/build_install.ps1`，参数相同。脚本确认编译成功后才安装；设备不唯一时要求指定序列号。USB 和无线 ADB 同时在线会显示两项。

Wi-Fi 版显示名为 **OpenGlass WiFi**，包名 `org.opensqz.openglass.rokid.wifi`，版本 `0.2.0-wifi`。它可与其他电脑签名的旧采集 App 并存，不需要卸载或清除旧数据；同一时间只运行一个采集 App。新包的设置和权限需要单独配置。原 `-Variant Debug`（默认）与 release 的包名保留不变。

Wi-Fi APK 输出：`OpenGlassRokidSensor/app/build/outputs/apk/wifi/app-wifi.apk`。这是生成物，不提交 Git。签名不匹配时脚本会停止，**不会自动卸载**。安装后允许相机和麦克风权限。

### 等价手动命令

已配置好 SDK/JDK 时，从 Android 工程目录执行：

```powershell
.\gradlew.bat :app:assembleWifi :app:testWifiUnitTest
# 上一步成功后再安装
adb -s <USB_SERIAL> install -r .\app\build\outputs\apk\wifi\app-wifi.apk
```

## iPhone 热点或同一个 Wi-Fi

先通过乐奇官方 App 或眼镜网络设置，将眼镜连到 iPhone 个人热点；也可以让两台设备加入同一 Wi-Fi。采集 App 不修改 Wi-Fi 配置。打开 iPhone OpenGlass，加载模型并启动接收服务，再打开眼镜上的采集 App。相机和麦克风授权后自动开始采集。两端 App 保持前台；安装完成后，无线传输不需要电脑或无线 ADB。

默认 **Auto 自动模式**会探测当前 Wi-Fi 默认网关的 18080 端口，并用 Bonjour 发现 `_openglass._tcp.` 服务；保存过的非回环手动地址可作为后备。每个自动候选必须先通过 `/health` 检查：`service=openglass-ios`、`protocol=1`，以及稳定的 `receiver_name`。首次选中的手机名称会保存；换网络可以更新地址，但不会自动改连另一台手机。首次发现多台未知手机时需要明确选择地址。酒店网络可能隔离终端，无法互通时使用 iPhone 热点。

自动模式**不回退 USB**，会忽略旧版保存的 `127.0.0.1`。图像和音频共用一个已选地址；运行中断线按 1 秒起步、最多 30 秒的间隔重试，换网后重新发现。这不替代系统连接热点的步骤。

眼镜主界面采用黑底、较暗的居中单行提示：`OpenGlass · 已连接`、`OpenGlass · 连接中` 或 `OpenGlass · 已暂停`。只在状态变化时更新，不显示计数、地址或滚动调试文字；诊断信息仍可从 logcat 查看。“已连接”表示音频连接已建立，不代表 iPhone 此刻已准备好识别。

**长按界面或按 Menu** 打开设置，提供 Auto、Manual address、USB relay、Choose phone again，以及明确的“暂停采集 / 恢复采集”选项。点击**关闭**或按 Back 可关闭设置弹窗。手动地址格式为 `http://<地址>:18080`；USB 模式使用 `http://127.0.0.1:18080`，仍需要电脑上的既有 USB 中继。Choose phone again 会忘记已选择的手机。短按界面或 Enter/DPAD 仅用于恢复已暂停的采集，运行中不会因普通点击而停止；按住 Enter/DPAD 也不会打开菜单。需要暂停时从菜单选择“暂停采集”。主界面 Back 仍先停止，再退出。全屏状态文字关闭系统默认焦点高亮，窗口与系统栏均使用不透明黑底。

Android 10 及以上的音频诊断日志会读取当前 AudioRecord 的配置，记录 `silenced=true/false/unknown`；`unknown` 表示配置不可用，不能当作 `false`。仅系统明确返回 `true` 时，单行界面提示“系统提示麦克风静音”。诊断不会改动录音参数或声音路由，也不会仅凭 RMS 很低就断定麦克风被系统静音。

启动参数仍兼容：显式 `pc_base_url` 会在本次启动固定连接该地址，并保存为以后自动模式的后备地址；`auto_discover=true` 显式切回自动，覆盖旧保存的 USB 地址。设置界面选择的模式会保存，仅传入一次 URL 不会把以后的普通启动永久绑定到 USB。

## 电脑面板工作流

**编译时不填写电脑 IP。** 面板启动时传入的 `pc_base_url` 仍固定直连，不要求电脑提供 iPhone 的 health 字段。按[电脑中文启动指南](../runtime/openglass_omni/STARTUP_zh.md)设置 `runtime.local.json` 中的 `rokid_adb`、`rokid_mode`、`rokid_pc_url`，然后运行 `glasses_panel.py`。电脑面板的无线 ADB 启动流程与上述 iPhone 直连传输是两件事。

iPhone 模型、签名安装和多轮对话见 [iPhone 中文指南](../ios/README_zh.md)。采集 App 不承担 ASR、TTS 或模型推理。
