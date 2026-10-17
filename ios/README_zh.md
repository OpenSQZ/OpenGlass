# Rokid → iPhone MiniCPM-V 4.6（Windows 开发）

本目录把现有 Rokid 采集协议接入 iPhone，在手机本机运行 MiniCPM-V 4.6。Windows 可以编辑代码、编译眼镜 APK、下载 iOS 构建和协助安装；iOS 编译交给 GitHub Actions 的 macOS/Xcode runner，安装前在 Windows 用自己的 Apple 账号重新签名。不要求自有 MacBook。签名工具的常规入口、当前登录问题与本机认证测试版见第 3 节。

这是开发版本。源码、构建成功、签名安装成功、真实眼镜音视频到达、手机模型回答成功是不同的验证阶段；下方命令不会把“USB 已识别”当成端到端成功。

## 运行路径

无线版本使用 Rokid 采集端 **0.2.0**。眼镜加入 iPhone 个人热点（推荐），或与 iPhone 加入同一可互通的 Wi-Fi：

```text
Rokid 摄像头 / 麦克风 → Wi-Fi → iPhone OpenGlass :18080
  → iPhone 离线 ASR → MiniCPM-V 4.6 → iPhone TTS
  → 自动恢复听音，开始下一轮（每轮清空 KV cache）
```

这条路径不需要 Windows 转发；两端连接正常后可拔掉 USB。iPhone App 需要保持前台。首次配置热点 / Wi-Fi、权限和安装更新仍需操作设备。

双 USB 路径保留为回退方案，眼镜 Wi-Fi 可以保持关闭：

```text
Rokid 摄像头 / 麦克风
  → 眼镜 OpenGlassRokidSensor 0.1.3
  → adb reverse（眼镜 USB）
  → Windows 127.0.0.1:18080
  → pymobiledevice3 usbmux（iPhone USB）
  → iPhone OpenGlass :18080
  → MiniCPM-V 4.6 本机视觉推理 → 系统语音播报
```

Windows 在这个路径只转发数据，不运行视觉模型。拔掉任一 USB 线会中断此路径。确认双 USB 稳定后，可用同一 Wi-Fi 下的直接连接，去掉 Windows 数据中继：

```text
Rokid → HTTP JPEG + WebSocket PCM → iPhone 局域网地址:18080
```

眼镜本身需要支持 Android APK 和 Camera/Mic 权限；ADB 用于 Windows 安装与可选的首次配置，无线运行不需要 ADB。不能仅根据“Rokid”品牌判断所有型号兼容。0.2.0 延续 JPEG / PCM 协议，新增自动寻找手机和统一的断线重连；当前采样是 JPEG 约 1 fps、16 kHz 单声道 PCM16LE。模型按问题读取最新画面，不是对每个视频帧连续推理。

## 1. 准备 Windows 与手机

需要 Android Platform Tools、Python 3.11+、Git；从 Actions 下载产物的脚本还需要 GitHub CLI。Android 采集 APK 已安装时，无需重新编译；需要编译时沿用仓库的 `rokid_app/build_install.cmd`，另备 JDK 17 和 Android SDK。

1. 将眼镜和 iPhone 都用支持数据传输的 USB 线连接 Windows。
2. iPhone 解锁，手动同意“信任此电脑”；眼镜确认 USB 调试授权。
3. 按 [AltStore 官方 Windows 安装说明](https://faq.altstore.io/altstore-classic/how-to-install-altstore-windows)安装 AltServer 及所要求的 Apple iTunes / iCloud 组件。官方默认流程使用 Apple 直接分发的版本；已安装 Store 版时，先参照官方故障排查，不要随意删除 Apple 驱动或服务。
4. 在 iPhone 的“隐私与安全性”开启“开发者模式”，按系统要求重启并确认。签名配置的信任按系统提示在“通用 → VPN 与设备管理”中处理。

Apple 账号、密码及验证码只由本人输入所选签名工具的界面，不放入脚本、GitHub Secrets、日志或仓库。安装 iTunes / iCloud 组件与登录它们的云服务是两件事；本机认证测试版使用已安装的 Apple 组件，不要求分别登录 iCloud 和 iTunes。

在仓库根目录创建独立 Python 环境：

```powershell
python -m venv .\ios\windows\.venv
.\ios\windows\.venv\Scripts\python.exe -m pip install -r .\ios\windows\requirements.txt
```

Python 命令未安装或打开 Microsoft Store 时，先安装真正的 Python，或者用其完整 `python.exe` 路径替换命令。

运行只读诊断：

```powershell
.\ios\windows\diagnose.ps1 `
  -Adb "$env:USERPROFILE\Downloads\platform-tools\adb.exe" `
  -Python .\ios\windows\.venv\Scripts\python.exe
```

若有多个 Android 设备，在命令后加 `-Serial '<adb devices -l 中的眼镜序列号>'`。若同时安装 debug/release 两个采集 App，加 `-Package 'org.opensqz.openglass.rokid.debug'` 或实际已安装的 release 包。脚本会核验真实安装包、0.1.3 版本和 launcher Activity，不默认猜包名。分享诊断输出前删除设备序列号/UDID。

## 2. 获取 iOS IPA

构建锁定 [MiniCPM-V-Apps](https://github.com/OpenBMB/MiniCPM-V-Apps) 与其 llama.cpp-omni 子模块的具体提交，见 `upstream-lock.json`；模型下载页沿用上游 MiniCPM-V 4.6 页面。iOS 工程由 `scripts/prepare_upstream.py` 在构建目录生成，不覆盖旧 Mac 上的项目。

在包含当前分支的 GitHub 仓库里打开 **Actions → Build OpenGlass iOS IPA**，选择该分支运行。工作流是 `.github/workflows/ios-ipa.yml`；首次编译原生推理框架会较慢。等待实际显示成功，再下载名为 `OpenGlass-iOS-unsigned-<运行编号>` 的 artifact，解压得到：

- `OpenGlass-1.0.ipa`：未签名的真机 arm64 App。
- `build-info.json`：构建来源信息。

也可在 Windows 用指定仓库和运行 ID 下载：

```powershell
.\ios\windows\download-artifact.ps1 `
  -Repository '你的GitHub用户名/OpenGlass' `
  -RunId 123456789
```

`RunId` 是 Actions 运行 URL 的数值 ID，不是 artifact 后缀的运行编号；示例值必须替换。脚本核验工作流、成功状态和未过期产物，再检查 `build-info.json` 的源提交与该运行一致、`signed=false`、bundle ID正确，以及 IPA中存在且可读取 `Payload/OpenGlass.app/Info.plist`。它不把IPA中的任意路径解压到磁盘，并输出源提交与 IPA SHA-256；目标目录存在时退出，避免覆盖旧产物。GitHub CLI 未登录时，由本人先运行 `gh auth login`。

## 3. 用免费 Apple 账号签名安装

CI 产物不能直接作为已签名 App 安装。`ideviceinstaller`、`pymobiledevice3 apps install` 和 USB 端口转发都不会把 unsigned IPA 变成可运行的签名 App。

**常规入口：从 Windows AltServer 直接侧载。** 本次现场的 AltServer 1.8 在验证码阶段出现 `-22411`，因此以下入口尚未在本机成功完成安装；不要把它视为已验证的解决办法。手机已经有旧 OpenGlass 和自己编译的 MiniCPM 时，先装 AltStore 可能占用第三个免费侧载名额，使新 OpenGlass 成为第四个而无法安装。直接侧载无需增加 AltStore 这个手机 App：

1. 保持 iPhone 通过 USB 连接、解锁并已信任电脑；确认 Apple Mobile Device Service 可用。Windows能看到iPhone图标或iTunes已安装，不等于该服务已准备好。
2. 启动 Windows 的 **AltServer**，找到任务栏右下角托盘里的图标。
3. **按住 Shift，再点击 AltServer 托盘图标**，选择 **Sideload .ipa…**，并选择目标 iPhone（界面有设备子菜单时）。
4. 在 Windows 文件选择框选中构建下载得到的 **OpenGlass-1.0.ipa**。
5. 本人在 AltServer 界面输入自己的 Apple 账号并完成验证，等待签名和安装结果；随后在 iPhone 按提示信任签名配置、确认开发者模式，再打开新 App。

这条 Windows 直接侧载入口及其无需预装 AltStore 的行为见 [AltServer 官方发布说明的 Direct .ipa Sideloading](https://faq.altstore.io/release-notes/altserver)。保留下载的 IPA；免费签名需要每 7 天用同一账号、同一 IPA 的上述步骤重新签名安装。此路径没有 AltStore 的自动刷新，不需要先把 IPA 转到手机“文件”App。

源 bundle ID 为 `org.opensqz.openglass.ios`；重签工具可能附加账号相关标识。显示名称是 **OpenGlass**。旧手机 App 若由另一账号/Team 签名，不能保证覆盖安装、共享模型文件或保留原数据；先保留旧 App，核对错误提示与签名来源，避免通过卸载来试错。

免费账号的活跃侧载 App 上限为 3 个，AltStore 本身也占名额；直接侧载只是省去 AltStore 的名额，并不会提高上限。若仍提示超限，先核对当前签名/活跃 App 清单，**不要卸载旧 OpenGlass 或 MiniCPM 来试错**。官方限制见 [AltStore Classic 开发者说明](https://developer.altstore.io/)及[错误码](https://faq.altstore.io/altstore-classic/error-codes)。如果原 OpenGlass“点不开”，签名过期只是可能原因之一，还应区分未信任、开发者模式、闪退和模型内存问题。

**后续可选：用 AltStore 管理自动刷新。** 确认有空余名额后，按 [官方安装流程](https://faq.altstore.io/altstore-classic/how-to-install-altstore-windows)从 AltServer 安装 AltStore Classic，再把 IPA 保存到 iPhone“文件”App，在 AltStore **My Apps → +** 中安装并管理。直接用 AltServer 安装的 App 不能假定会自动出现在 AltStore 的管理列表；要以 AltStore 里实际显示的 App 和到期时间为准。自动刷新仍依赖可达的 AltServer及系统后台条件，临近到期应主动检查。

**本次本地交付：OpenGlass Local Signer 测试版。** Sideloadly 0.70.1 使用 Local Anisette 后可以输入验证码，但本机仍收到 GSA HTTP 503。这个错误本身不能证明密码错误或 TLS 被拦截。为继续只使用本地认证组件，准备了基于 [iloader v2.3.9](https://github.com/nab138/iloader/tree/aa9aa3779e15498180e0205158b7ff91536877c6) 和 isideload 0.4.6 的修改版，保存于本机 `%USERPROFILE%\Downloads\OpenGlass-Local-Signer`。它不是官方 iloader 发布；目录内源码包及 `BUILD_WINDOWS.md` 提供构建输入和说明。

主程序旁必须保留 `local-apple-identity.exe` 与 `local-coreadi-helper.exe`。它们核验已安装 Apple DLL 的固定 SHA256 和签名，通过本机管道取得原生身份及认证数据；没有远程 Anisette 备用服务，不自动擦除或重新注册原生身份。当前构建锁定本机 Apple 组件版本，不保证适用于其他版本的电脑。登录、验证码验证、申请签名证书和描述文件仍需要联网到 Apple；“本地认证”不等于离线签名。

1. 运行 `OpenGlass-Local-Signer.exe`，本人在 Apple ID 区域登录并填写验证码；保存密码默认不勾选。
2. 选择 USB 连接的 iPhone，点击 **Import IPA**，选择本次构建的 IPA；选中文件后即开始签名安装。
3. 若提示开发证书已满，先取消并确认哪些旧 App 依赖已有证书；程序不会默认选择全部撤销。
4. 用手机安装列表核对新 App 的实际 bundle ID、版本和文件共享状态，再导入模型。仅有“账号登录成功”或 Windows 程序已打开不代表 iPhone 安装完成。

测试版已通过 6 项管道协议测试、前后端构建、本机组件调用，以及携带公共客户端描述的 Apple URL-bag GET / TLS 校验。2026-10-16，本人操作后 Apple 账号登录和签名成功；首次安装因已有 3 个免费开发 App 被手机拒绝。用户明确选择卸载旧 OpenGlass 后，复用已签名文件安装新版成功，USB 查询确认版本 `1.0`、build `3`、文件共享可用，MiniCPM 示例与 CyanGlass 均保留。签名工具会给新 bundle ID 追加 Team ID；只改 IPA 的原始 bundle ID 不能假定能覆盖旧 Xcode App。自动更新已关闭，避免被上游的不同认证配置替换。

## 4. 在 iPhone 准备模型

1. 打开新 **OpenGlass**，进入右上角“模型下载”。
2. 在 MiniCPM-V 4.6 下载页完成 **LLM 与 VPM 两个 GGUF 文件**。这个 App 有独立沙盒；其他 TestFlight/App 已下载的模型不会自动出现在这里。下载与首次资源准备需要网络。
3. 模型已准备好时，打开 App 会自动启动接收并加载模型。首次刚从下载页返回时点一次“启动接收”。初次验证可先关闭“Rokid 离线语音提问”，用文字问题排除语音权限影响。
4. 保持 OpenGlass 在前台。进入后台会挂起接收和语音；返回后自动恢复此前运行状态。主动点“停止接收”或“暂停语音”后不会自动重启。本实现不保证锁屏长期运行。

模型文件不能混用 MiniCPM-o 4.5、其他尺寸或未经验证的 GGUF 转换。加载会检查文件存在、GGUF 头和锁定上游对应的 MD5；首次完整校验在后台进行，后续复用文件大小/修改时间匹配的校验记录。文件名与预期校验值由锁定上游的 MiniCPM-V 4.6 常量决定；模型通过校验仍需真机推理验证。

## 5A. 双 USB 接入（先验证这条）

确认 iPhone 已启动接收后，在 Windows 仓库根运行：

```powershell
.\ios\windows\start-usb-relay.ps1 `
  -Adb "$env:USERPROFILE\Downloads\platform-tools\adb.exe" `
  -Python .\ios\windows\.venv\Scripts\python.exe
```

Windows PowerShell 若提示“在此系统上禁止运行脚本 / PSSecurityException”，可仅为这次启动允许本地脚本，使用下面的命令；把路径替换为实际仓库绝对路径。`-ExecutionPolicy RemoteSigned` 只影响新进程，不永久修改用户或系统设置，也不会覆盖组织的组策略：

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File 'C:\path\to\OpenGlass\ios\windows\start-usb-relay.ps1'
```

本项目生成或通过 Git 检出的本地脚本可以使用这条命令。若仍报执行策略错误，先检查 `Get-ExecutionPolicy -List` 和文件的来源标记，不全局降低策略。作用范围见 [Microsoft 官方执行策略说明](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_execution_policies?view=powershell-5.1)。

仅连接一副授权眼镜和一台 iPhone 时自动选择；多设备时显式加 `-Serial '<眼镜USB序列号>'`、`-IPhoneUdid '<iPhone UDID>'`。用下列只读命令查询 iPhone USB UDID：

```powershell
.\ios\windows\.venv\Scripts\python.exe -m pymobiledevice3 usbmux list --usb --simple
```

脚本先检查既有 ADB reverse 映射，随后在 Windows `127.0.0.1` 绑定端口，通过 iPhone USB 查询 `/health` 并核验 `service: openglass-ios`；通过后才建立独占反向映射并启动眼镜采集 App。App 无需 Wi-Fi 地址，实际保存的采集目标是眼镜自己的 `http://127.0.0.1:18080`。

Windows 原 Python 面板占用了 18080 时，先在该面板正常停止；也可以为本次中继换 Windows 端口，不改变 iPhone 监听端口：

```powershell
.\ios\windows\start-usb-relay.ps1 `
  -Python .\ios\windows\.venv\Scripts\python.exe -LocalPort 18081
```

如果眼镜18080已有其他会话的 reverse 映射，脚本会退出，不抢占。可结束原会话，或显式加 `-RokidPort 18081`；脚本会同步传入正确采集 URL。不要同时启动旧电脑面板去重配同一眼镜。

运行期间另开终端查看手机接收计数（修改了 `LocalPort` 就使用对应端口）：

```powershell
Invoke-RestMethod http://127.0.0.1:18080/health
```

`image_count`、`audio_packets_in` 应随采集增长。主终端按 **Ctrl+C** 正常停止，会关闭本次转发器，并移除本次创建、且未被其他会话改变的 reverse 映射。不会杀死所有 Python/ADB 进程，不会卸载 App。眼镜采集 App 可能继续重连，必要时在眼镜界面手动停止。

底层使用官方 [pymobiledevice3 v11.32.0 TCP forwarder](https://github.com/doronz88/pymobiledevice3/blob/v11.32.0/pymobiledevice3/tcp_forwarder.py)，限定 USB。其 CLI 对应 `usbmux forward <Windows端口> <iPhone端口>`，这里的包装脚本另外处理设备选择、健康检查和清理。普通 App TCP 转发不使用 iOS 开发调试隧道，不要求将 Apple 配对文件上传到任何服务。

## 5B. Wi-Fi 直连（推荐 iPhone 个人热点）

先安装新 iOS IPA 和 Rokid 0.2.0 APK。iOS 使用同一 Apple 账号、同一 bundle ID 覆盖安装，保留现有模型；无需卸载或重新下载模型。本机旧眼镜 APK 的原签名文件不可用，因此交付并存包 **OpenGlass WiFi**（`org.opensqz.openglass.rokid.wifi`，`0.2.0-wifi`），保留旧采集 App。使用新版前先在旧 App 中停止采集，避免两个程序争用相机和麦克风；新 App 首次打开需允许相机和麦克风。

1. iPhone“设置 → 个人热点 → 允许其他人加入”。眼镜找不到热点时可开启“最大兼容性”。首次连接时停留在热点页面，直到眼镜已加入。具体开关见 [Apple 个人热点说明](https://support.apple.com/zh-cn/111785)。热点依赖运营商支持；它分享的是蜂窝网络，不是酒店 Wi-Fi 的中继。
2. 通过 Rokid 官方 App / 设备的 Wi-Fi 设置，让眼镜加入这个热点；密码仅在设备设置内输入。OpenGlass 不修改 Wi-Fi 密码和系统网络。
3. iPhone 打开 OpenGlass，允许“本地网络”和语音识别；已有模型时自动启动接收，等待模型就绪即可。页面显示固定的 `OpenGlass-xxxxxxxx` 接收名称。请保持“Rokid 离线语音提问”和“朗读回答”开启。
4. 启动眼镜 OpenGlassRokidSensor，使用“自动连接”。眼镜寻找 `_openglass._tcp` 服务，并探测当前 Wi-Fi 网关的 `:18080/health`；确认是 OpenGlass iOS 接收端后才发送音视频。地址变化后会重新发现，不把热点地址写死。
5. 确认手机出现新画面、自动语音状态后，拔掉两根 USB，等待十秒并连续问三个问题。回答播完后再说下一个问题，不需要点“结束语句”或“用最新画面提问”。

眼镜暂留 USB 时，可在 Windows 手动切到自动发现模式；这条命令只做一次配置与启动，不是数据中继：

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File 'C:\path\to\OpenGlass\ios\windows\configure-rokid.ps1' -AutoDiscover -Package org.opensqz.openglass.rokid.wifi
```

普通 Wi-Fi / 酒店 Wi-Fi 也可用：将两台设备加入同一网络。网络必须允许设备互访；若酒店启用了客户端隔离，或眼镜无法完成网页认证，应改用个人热点。Bonjour 被阻止但设备仍能直连时，可从 iPhone 页面取 IPv4 地址手动指定：

```powershell
.\ios\windows\configure-rokid.ps1 `
  -IPhoneAddress '192.168.1.42' `
  -Adb "$env:USERPROFILE\Downloads\platform-tools\adb.exe"
```

替换示例 IP。手动地址模式用于明确指定接收设备，换网后可重新选“自动连接”。脚本不打开/修改眼镜 Wi-Fi。若 Windows 未加入手机所在网络而眼镜已加入，Windows 的接收端检查可能失败；可使用自动发现，或确认原因后加 `-SkipReceiverCheck`。

无线连通性须在这副眼镜和这台 iPhone 上实测，不能把“已编译”当成“热点已跑通”。检查时必须拔掉 USB，并确认新画面仍在更新，防止误把原 USB 链路当成无线成功。

## 6. 验证对话和音频

先确认预览方向正确；主界面可选0/90/180/270°旋转，默认270°沿用原电脑路径。输入“我面前有什么？”后点“用最新画面提问”，验证手机能读到当前眼镜画面并生成回答。

然后启用“Rokid 离线语音提问”，选择中文或英文，允许系统语音识别权限。输入仍来自眼镜的 PCM 流，App 不打开 iPhone 麦克风。Apple 设备端识别资源可用时执行离线识别；不可用会显示错误并保留文字提问，不改走云端。说完停顿后，识别文本与当前图片自动送给 MiniCPM-V 4.6。回答由系统 TTS 朗读，结束后自动恢复听音；每轮推理前清空 KV cache，不保留此前问题或图片。可在 iPhone 控制中心选择已配对的眼镜作为声音输出，前提是该型号支持相应蓝牙音频功能。

自动循环为“听问题 → 看图推理 → 朗读回答 → 等待回声消退 → 继续听问题”。推理/播报时丢弃输入语音，不积压旧问题；空白识别、短暂识别错误、音频重连与新画面恢复均由状态机处理。权限拒绝、离线语言资源缺失、模型错误仍会明确显示，主动暂停必须由用户恢复。这是逐轮交互，暂不支持边播报边打断。

`/health` 的 `conversation_phase` 显示 `waiting/listening/thinking/speaking/paused`，`completed_turns` 记录本次接收会话中完成的回答数；不返回识别文本或回答正文。测试至少三轮，并留空静音约30秒后再提问，确认没有空问题、重复回答或需要手动唤醒。

## 排障

| 现象 | 先检查 |
| --- | --- |
| Windows看得到iPhone，usbmux列表为空/报错 | 数据线、手机解锁与信任、Apple Mobile Device Service；PnP识别不等于配对完成 |
| USB中继提示手机 `/health` 不可达 | 新App是否成功签名安装并已启动接收；是否进入后台；模型加载失败是否导致接收停止 |
| 端口或ADB reverse已存在 | 在原会话中正常停止，或指定其他本机/眼镜端口；脚本不会杀进程抢占 |
| `/health` 成功但计数不增长 | 眼镜相机/麦克风权限、采集App版本、ADB授权、所选设备；Wi-Fi模式另查网络互通 |
| 画面到达但模型未就绪 | 两个正确的4.6模型是否下载完成、磁盘空间、模型加载报错；其他App模型沙盒不共享 |
| 有文本回答但没声音 | “朗读回答”开关、iPhone音量/音频路由、蓝牙配对；看界面实际输出设备 |
| 语音不可用但文字正常 | 系统语音识别权限与该语言的设备端资源；该功能不向云识别降级 |
| 切后台后断流 | 预期行为；回前台自动恢复此前运行意图；主动停止不会自动恢复 |
| 免费签名几天后点不开 | 直接侧载方式每7天用AltServer重签；AltStore管理方式查看剩余天数并刷新。不要先删除App及模型数据 |

## 验证记录

- 本地脚本检查：PowerShell语法解析通过；USB中继使用伪设备测试了端口占用、已有reverse、并发映射冲突、接收端不可达、成功启动与只清理自身映射等情况。
- [CI运行38890873970](https://github.com/OpenSQZ/OpenGlass/actions/runs/38890873970)成功，源提交为 `c11c87b39b5798f8b4218d3a47d5b6cc68e56091`：1399项wire protocol检查、7项工程准备测试、7项USB中继测试通过，并生成未签名的 `OpenGlass-1.0.ipa`（artifact `OpenGlass-iOS-unsigned-3`）。IPA SHA256 为 `DA76A28A87195C43EA80ECF11366724180480B5D6502DEA27A4D1AF800AD00DB`。
- 现场设备识别为 `iPhone18,2`、iOS `26.6.2`。新版 `1.0` / build `3` 已通过本机认证签名并安装；两个对应 4.6 模型已校验并导入。2026-10-18 用户反馈：USB 连接下眼镜图像、声音和 iPhone MiniCPM 推理已跑通，仍需手动进行单轮交互。本次无线与自动多轮改动另行验证，不能继承旧版的端到端结论。
- 无线/自动语音 iOS 源提交 `087766314c0d817a78b65c6e6a93d46b281d5576` 通过 [CI 39057876255](https://github.com/OpenSQZ/OpenGlass/actions/runs/39057876255)，生成 build `5`。1399 项协议检查、2087 项语音循环断言、7 项工程准备测试、7 项 USB relay 测试通过。IPA SHA256：`D5A8F689048DFE09E1C4F4AF4EE67E770E5FE01BE3883A0A4B414804B39BFCB7`。Windows 免费签名更新后，USB 查询已确认手机安装 build `5`，两份模型文件仍完整保留。
- Rokid `0.2.0-wifi` / code `5` 的 `assembleWifi`、7 项 `testWifiUnitTest` 与原 `assembleDebug` 通过；APK SHA256：`A5A64849FEAC796D4688B60833E647FA3DACE1CB3F5A16BD8935AA0EA5623A13`。并存包 `org.opensqz.openglass.rokid.wifi` 已安装，旧 debug 采集 App 保留。眼镜已获得过 iPhone 热点 DHCP 地址；新 App 的权限、无线音视频和拔线后的自动连续对话仍需现场测试。
- 不在此文档写入个人设备序列号、UDID、Apple账号或内网凭据。现场更新请用设备类型和验证结果描述。

可重复执行无设备测试：

```powershell
python -m unittest discover -s .\ios\windows -p test_usb_relay.py -v
```
