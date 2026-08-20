# MiniCPM-o 浏览器 Phase A 集成资产

本目录保存已经验证过的浏览器薄适配层；Harness Core 位于仓库根目录的
`extensions/assistive_harness/`。这些文件不是一个独立网页，也不会替代外部
MiniCPM-o-Demo 后端。

接入已有 MiniCPM-o-Demo 时，将 `static/assistive_harness/` 整个复制到目标
仓库的同名 `static/assistive_harness/`，并在目标版本的
`static/omni/omni-app.js` 中接入以下边界：

1. 导入并创建 `createAssistiveHarnessIntegration()`；
2. 把浏览器麦克风的 Web Audio source 镜像给 `attachAudioMirror()`；
3. 以不高于约 1 fps 把 JPEG 传给 `mirrorFrame()`；
4. 每次创建 Duplex Session 后调用 `bindSession(session)`；
5. 页面销毁时调用 `close()`。

不要直接用本目录覆盖不同版本的完整 `omni-app.js`。MiniCPM-o-Demo 的
Duplex Session 私有接口可能随上游版本改变；浏览器 Hook 应在它自己的
分支中审阅和回归。本 OpenGlass 分支中的 Phase B 设备链路无需这些网页
文件即可运行。
