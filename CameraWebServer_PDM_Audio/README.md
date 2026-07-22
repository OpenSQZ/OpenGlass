# ESP32 Sensing Firmware

This firmware turns the ESP32-S3 glasses into a camera and microphone sensing device. The multimodal model does not run on the ESP32; inference runs locally on a nearby laptop or edge host.

## 1. Configure Wi-Fi Before Flashing

Open `CameraWebServer_PDM_Audio.ino` and replace the two placeholders locally:

```cpp
const char *ssid = "YOUR_WIFI_NAME";
const char *password = "YOUR_WIFI_PASSWORD";
```

Use a Wi-Fi network that the ESP32 and inference host can both reach. The credentials are compiled into the firmware, so never commit real values or share the flashed binary without considering that risk.

## 2. Flash and Find the Glasses IP

1. Open `CameraWebServer_PDM_Audio.ino` in Arduino IDE.
2. Select the XIAO ESP32-S3 board profile and the matching camera configuration.
3. Compile and flash the firmware.
4. Open Serial Monitor and restart the board.
5. Record the address printed after `[WiFi] Connected! IP:` as `<ESP32_IP>`.

The address is normally assigned by DHCP and may change after a router or device restart. Update the local device file when it changes, or configure a DHCP reservation in your router.

## 3. Test the Firmware Endpoints

From a computer on the same reachable network, test:

| Input | Local endpoint |
| --- | --- |
| Single JPEG | `http://<ESP32_IP>/capture` |
| MJPEG preview | `http://<ESP32_IP>:81/stream` |
| PCM16 microphone | `ws://<ESP32_IP>/ws_audio` |

Do not include `http://` in the `esp32_host` value described below. Store only the IP address or resolvable host name.

## 4. Register the Glasses in OpenGlass

From the repository root, create a private device registry from the public example:

```powershell
Copy-Item examples\configs\devices.example.json runtime\openglass_omni\devices.local.json
```

Edit `runtime/openglass_omni/devices.local.json`:

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

- `name`: label shown in the control panel.
- `esp32_host`: IP from Serial Monitor, without a URL scheme or port.
- `esp32_port`: camera/control HTTP port; the tracked firmware uses `80`.
- `rotate`: frame rotation in degrees; use the value that matches the mounted camera.

Then set these fields in the Git-ignored `runtime/openglass_omni/runtime.local.json`:

```json
{
  "devices_file": "devices.local.json",
  "demo": {
    "audio_endpoint": "/ws_audio"
  }
}
```

Keep the other fields from `runtime.example.json`; the snippet above is not a complete runtime configuration. Both local files are ignored by Git so that real device addresses and machine-specific paths are not published.

Validate the configuration without loading the model:

```powershell
python glasses_panel.py --check
```

## 中文配置摘要

1. 在 `.ino` 中把 `YOUR_WIFI_NAME`、`YOUR_WIFI_PASSWORD` 换成本地 Wi-Fi 名称和密码，不要提交真实值。
2. 烧录后从串口日志 `[WiFi] Connected! IP:` 读取眼镜 IP。
3. 用浏览器测试 `<ESP32_IP>/capture` 和 `<ESP32_IP>:81/stream`。
4. 复制公开设备表示例为 `runtime/openglass_omni/devices.local.json`，把 `esp32_host` 改成眼镜 IP。
5. 让 `runtime.local.json` 的 `devices_file` 指向 `devices.local.json`。使用本目录固件时，`audio_endpoint` 设为 `/ws_audio`。
6. IP 由 DHCP 分配时可能变化；变化后更新设备表，或在路由器中为眼镜保留固定租约。

