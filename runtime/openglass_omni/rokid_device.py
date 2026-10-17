"""ADB startup for the Rokid capture app.

设计目标：**首次接 USB 完成授权与开启无线调试后，此后完全走 WiFi**。
断开 USB、关面板、重启电脑，只要眼镜不断电、IP 不变，都能自动跑通。

流程：
  首次（USB 在场）：
    1) `adb devices` 里挑到 USB 上的 Rokid（serial 不含冒号）。
    2) 用它读 wlan0 IP。
    3) `adb -s <usb> tcpip 5555` 让 adbd 起 TCP 监听；再顺手
       `setprop persist.adb.tcp.port 5555`（能持久化更好，不支持就无害）。
    4) `adb connect <ip>:5555`，验证进入 device 状态。
    5) 把 <ip>:5555 写回 runtime.local.json 的 `rokid_adb_addr` 键。
    6) 走 `adb reverse tcp:<port> tcp:<port>`（无线 ADB 一样支持 reverse）。
    7) 拉起眼镜 APK，`pc_base_url=http://127.0.0.1:<port>`。
    8) 至此 USB 可以拔。

  以后（USB 不在场）：
    1) 读 rokid_adb_addr，`adb connect` → device 状态。
    2) 同样 reverse + 拉 APK，全过程零 USB。

  自愈：无线连不上但恰好接着 USB → 自动重跑首次流程（IP 换了会重新保存）。
        无线连不上且没 USB → 报"请接一次 USB"，不再无声失败。
"""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
from urllib.parse import urlsplit


class RokidDevice:
    def __init__(self, config, log):
        self.config = config
        self.log = log
        self.prefix = None      # 一旦 select() 成功，指向 [adb, "-s", <serial>]
        self._adb = None        # adb 可执行文件路径

    # ------------------------------------------------------------------ helpers
    def _run_bare(self, *args, timeout=10):
        """不带 -s 的 adb（用于 devices / connect / disconnect）。"""
        if not self._adb:
            raise RuntimeError("adb 未定位")
        try:
            p = subprocess.run([self._adb] + list(args), capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=timeout)
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, "adb 命令超时"

    def _run_with_prefix(self, prefix, *args, timeout=10):
        """指定 prefix 跑 adb；shell 参数会用 shlex.join 保留空格/元字符。"""
        argv = ["shell", shlex.join(args[1:])] if args and args[0] == "shell" else list(args)
        try:
            p = subprocess.run(prefix + argv, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout)
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, "adb 命令超时"

    def run(self, *args, timeout=10):
        if not self.prefix:
            raise RuntimeError("Rokid 尚未选定")
        return self._run_with_prefix(self.prefix, *args, timeout=timeout)

    # ---------------------------------------------------------- adb / devices
    def _resolve_adb(self):
        candidate = self.config.get("rokid_adb")
        if not candidate or candidate == "adb":
            candidate = os.environ.get("ROKID_ADB") or shutil.which("adb")
        if not candidate:
            roots = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                     str(Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk")]
            candidate = next(
                (str(Path(r) / "platform-tools" / "adb.exe") for r in roots
                 if r and (Path(r) / "platform-tools" / "adb.exe").is_file()),
                None)
        if not candidate:
            raise RuntimeError("未找到 adb，请在 runtime.local.json 设置 rokid_adb")
        return candidate

    def _list_devices(self):
        """返回状态为 device 的行；每行是 split 后的字段列表。"""
        rc, output = self._run_bare("devices", "-l")
        if rc:
            raise RuntimeError("adb devices 失败")
        rows = []
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                rows.append(parts)
        return rows

    # ------------------------------------------------------- wireless workflow
    def _save_addr(self, addr):
        """把 rokid_adb_addr 持久化到 runtime.local.json（保留其它键不变）。

        panel.py 启动时读的就是这份文件；下次启动会通过 config["rokid_adb_addr"]
        拿到，跳过 USB 直接 adb connect。
        """
        path = self.config.get("runtime_local_path")
        if not path:
            self.log("[PRE] 未提供 runtime_local_path，无线地址无法持久化"
                     "（下次仍需接 USB）")
            return
        try:
            data = {}
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f) or {}
            if data.get("rokid_adb_addr") == addr:
                return
            data["rokid_adb_addr"] = addr
            # 原子写：先写临时文件再替换，避免中途失败留半截。
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp, path)
            self.log(f"[PRE] 已把 rokid_adb_addr={addr} 写入 {os.path.basename(path)}")
        except Exception as exc:
            self.log(f"[PRE] !! 保存 rokid_adb_addr 失败: {exc}")

    def _try_wireless(self, addr, verify_tries=6):
        """`adb connect addr`；靠 `adb devices` 里出现同名 device 来确认成功。

        不看 connect 命令自己的输出：不同 adb 版本对成功/失败的 stdout 措辞不一，
        rc=0 也可能是失败。列表验证最稳。
        """
        self._run_bare("connect", addr, timeout=8)
        for _ in range(verify_tries):
            time.sleep(0.5)
            try:
                rows = self._list_devices()
            except RuntimeError:
                continue
            if any(r[0] == addr for r in rows):
                return True
        return False

    def _enable_wireless_from_usb(self, usb_prefix):
        """借当前 USB 会话把无线 ADB 打开、连上、地址存回配置。

        成功后返回 "ip:port"；失败抛 RuntimeError。
        """
        port = int(self.config.get("rokid_adb_tcpip_port", 5555))
        # 1) USB 在场时恢复 Wi-Fi，等待已保存网络分配地址。
        # svc enable 不是永久锁定；厂商系统仍可能再次关闭 Wi-Fi。
        rc, out = self._run_with_prefix(usb_prefix, "shell", "svc", "wifi", "enable")
        if rc:
            raise RuntimeError("无法开启眼镜 Wi-Fi，请检查眼镜调试权限")
        self.log("[PRE] 已请求开启眼镜 Wi-Fi，等待连接已保存的网络")
        m = None
        for attempt in range(21):
            rc, out = self._run_with_prefix(usb_prefix, "shell", "ip", "addr", "show", "wlan0")
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out) if rc == 0 else None
            if m:
                break
            if attempt < 20:
                time.sleep(1.0)
        if not m:
            raise RuntimeError("已开启 Wi-Fi，但等待后仍无 IP；请在配套 App 配置眼镜网络，暂勿拔 USB")
        ip = m.group(1)
        # 2) 尝试让端口在眼镜重启后仍监听。开发版 Rokid 可写 persist.*，
        #    普通消费版会静默忽略——无害。
        self._run_with_prefix(usb_prefix, "shell", "setprop",
                              "persist.adb.tcp.port", str(port))
        # 3) 让本次会话立即切到 TCP 监听。这一步会让 USB 上的 adbd 重启，
        #    USB 侧可能短暂 offline，之后我们不再依赖 USB。
        rc, out = self._run_with_prefix(usb_prefix, "tcpip", str(port), timeout=15)
        if rc:
            raise RuntimeError(f"adb tcpip {port} 失败: {out.strip()}")
        time.sleep(2.0)   # 给 adbd 一点重启时间
        addr = f"{ip}:{port}"
        # 4) 反复尝试无线连接。首次网络协商略慢，给到 ~30s。
        for _ in range(8):
            if self._try_wireless(addr):
                self._save_addr(addr)
                self.log(f"[PRE] 无线 ADB 已建立: {addr}（USB 现在可以拔）")
                return addr
            time.sleep(1.0)
        raise RuntimeError(f"adb connect {addr} 无响应；确认 PC 和眼镜在同一 WiFi")

    # ---------------------------------------------------------------- select
    def select(self):
        """挑一台 Rokid 来控制。优先无线（已保存地址），失败回退到 USB，
        USB 在场时自动完成首次无线初始化。

        成功时 self.prefix 已就绪；失败会抛可操作的中文提示。
        """
        self.prefix = None
        self._adb = self._resolve_adb()
        wifi_mode = self.config.get("rokid_mode", "wifi") == "wifi"

        # 1) 优先尝试上次保存的无线地址。
        saved = (self.config.get("rokid_adb_addr")
                 or (self.config.get("local") or {}).get("rokid_adb_addr"))
        if wifi_mode and saved:
            self.log(f"[PRE] 尝试上次保存的无线地址: {saved}")
            if self._try_wireless(saved):
                self.prefix = [self._adb, "-s", saved]
                self.log(f"[PRE] 无线 ADB 已就绪: {saved}")
                return
            self.log(f"[PRE] 无线连接 {saved} 失败，回退检测 USB")

        # 2) 找一台 USB 上的 Rokid（无线 serial 含冒号，先排除）。
        rows = self._list_devices()
        wanted = self.config.get("rokid_serial")
        if wanted:
            matches = [d for d in rows if d[0] == wanted]
        else:
            matches = [d for d in rows if any(x == "model:RG_glasses" for x in d)
                       and ":" not in d[0]]
            # 兜底：如果只有无线设备（比如用户手动 adb connect 上来的），也允许。
            if not matches:
                matches = [d for d in rows if any(x == "model:RG_glasses" for x in d)]
        if len(matches) != 1:
            if saved:
                hint = ("无线连接失败且未检测到 USB Rokid。"
                        "请检查眼镜是否与 PC 同 WiFi 且 IP 未变；"
                        "或接一次 USB 让 panel 自动重建无线通道。")
            else:
                hint = ("未检测到 Rokid。首次使用请接一次 USB 完成授权，"
                        "panel 会自动开启无线调试并保存 IP，之后即可拔 USB。")
            raise RuntimeError(hint)

        serial = matches[0][0]
        prefix = [self._adb, "-s", serial]
        self.log(f"[PRE] 检测到 Rokid: {serial}")

        # 3) wifi 模式 + USB 在场 → 借这次 USB 把无线打开并保存。
        if wifi_mode and ":" not in serial:
            try:
                addr = self._enable_wireless_from_usb(prefix)
                self.prefix = [self._adb, "-s", addr]
                return
            except Exception as exc:
                self.log(f"[PRE] 无线 ADB 初始化失败: {exc}；本次继续走 USB，不能拔线")

        # 4) 已经是无线 serial 但没保存过？顺手存一下，下次直接连。
        if wifi_mode and ":" in serial and saved != serial:
            self._save_addr(serial)

        self.prefix = prefix

    # ---------------------------------------------------------------- prepare
    def prepare(self):
        """选设备 + 建 adb reverse。无论 USB 还是无线，之后 launch() 都用
        127.0.0.1，不再依赖用户手填 PC LAN IP。
        """
        self.select()
        port = str(self.config.get("rokid_port", 18080))
        # adb reverse 走 adbd 隧道，USB / TCP 传输都支持。
        rc, out = self.run("reverse", "tcp:" + port, "tcp:" + port)
        if rc:
            # USB 明确 reverse 失败 → 断链；无线模式下万一 reverse 失败，
            # 还能靠 rokid_pc_url 兜底，所以只警告。
            if self.config.get("rokid_mode", "wifi") == "usb":
                raise RuntimeError(f"USB 端口转发失败: {out.strip()}")
            self.log(f"[PRE] adb reverse 失败({out.strip()})；"
                     f"如未设置 rokid_pc_url 则本次不通")
        else:
            self.log(f"[PRE] adb reverse 已建立: 眼镜 127.0.0.1:{port} -> PC")

        # 顺手记录一下眼镜 WiFi 状态，便于事后排查"眼镜没上 WiFi"这类问题。
        rc, out = self.run("shell", "ip", "addr", "show", "wlan0")
        if rc == 0:
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
            if m:
                self.log(f"[PRE] 眼镜 Wi-Fi IP: {m.group(1)}")
        return True

    # ---------------------------------------------------------------- launch
    def launch(self):
        activity = self.config["rokid_activity"]
        port = int(self.config.get("rokid_port", 18080))
        # 默认 127.0.0.1（走 adb reverse）；仅当 runtime.local.json 明确设了
        # rokid_pc_url 才用之。
        url = (self.config.get("rokid_pc_url") or f"http://127.0.0.1:{port}").rstrip("/")
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise RuntimeError("rokid_pc_url 非法；示例 http://127.0.0.1:18080")
        if (parsed.port or 80) != port:
            raise RuntimeError("rokid_pc_url 端口与 rokid_port 不一致")
        rc, info = self.run("shell", "dumpsys", "package",
                            self.config.get("rokid_package",
                                            "org.opensqz.openglass.rokid.debug"))
        if rc or not re.search(r"versionName=(?:0\.1\.3|0\.2\.0)(?:-(?:debug|wifi))?(?:\s|$)", info):
            raise RuntimeError("尚未确认动态地址版本 0.1.3 / 0.2.0 已安装；"
                               "旧版会忽略 pc_base_url，请先升级采集程序")
        self.run("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        rc, out = self.run("shell", "am", "start", "-S", "-W", "-n", activity,
                           "--es", "pc_base_url", url, timeout=20)
        if rc or "Error:" in out or "Exception" in out:
            raise RuntimeError("眼镜采集程序启动失败")
        self.log(f"[PRE] 已启动眼镜采集程序，PC 目标地址: {url}")
