# Rokid sensor app

[中文说明](README_zh.md)

This directory contains OpenGlass Rokid sensor app 0.2.0 (version code 5). It captures JPEG/PCM and can connect directly to OpenGlass on iPhone over Wi-Fi. The existing PC panel and explicit USB relay modes remain available.

## Companion setup

Install the official Rokid AI phone app and connect the glasses. Enable developer mode and authorize glasses ADB debugging in settings.

## Build and install

Prepare JDK 17 (or a JDK compatible with Gradle 8.7 / AGP 8.5.2), Android SDK API 34, build tools and Platform Tools. Set JAVA_HOME and ANDROID_HOME or pass `-Sdk`. Initial builds download Gradle/Maven dependencies. From the OpenGlass root:

```powershell
# Build only
.\rokid_app\build_install.cmd -Sdk "<ANDROID_SDK_PATH>" -Variant Wifi
# Build and install after connecting/authorizing USB
.\rokid_app\build_install.cmd -Sdk "<ANDROID_SDK_PATH>" -Variant Wifi -Install -Serial "<USB_SERIAL>"
```

Use `adb devices -l` to obtain the serial. Optional `-Adb` selects an executable. The CMD delegates to `scripts/build_install.ps1`, which can also be called directly with the same options. A failed build never triggers installation. Multiple online entries require a serial; USB and wireless connections appear separately. Signature mismatches stop installation; the script never uninstalls an existing app. Allow camera/microphone permissions on the device.

The Wi-Fi variant is **OpenGlass WiFi**, package `org.opensqz.openglass.rokid.wifi`, version `0.2.0-wifi`. It can coexist with a collector signed on another computer, without uninstalling it or clearing its data. Run only one collector at a time. Its settings and permissions start separately. The original `-Variant Debug` (default) and release package IDs remain unchanged.

Generated Wi-Fi APK: `OpenGlassRokidSensor/app/build/outputs/apk/wifi/app-wifi.apk` (ignored by Git). Equivalent manual commands from the Android project root, with SDK/JDK configured:

```powershell
.\gradlew.bat :app:assembleWifi :app:testWifiUnitTest
# Only after a successful build:
adb -s <USB_SERIAL> install -r .\app\build\outputs\apk\wifi\app-wifi.apk
```

## iPhone hotspot or shared Wi-Fi

Join the glasses to the iPhone hotspot using the official Rokid companion app/device settings, or join both devices to the same Wi-Fi. This app does not change Wi-Fi settings. Open iPhone OpenGlass, load its model and start its receiver, then launch this sensor app on the glasses. After camera/microphone permission is granted it starts capturing automatically. Leave both apps in the foreground. No computer or wireless ADB is needed for transport after installation.

Default **Auto** mode checks the current Wi-Fi default gateway on port 18080 and discovers `_openglass._tcp.` with Bonjour. A saved non-loopback manual address is a fallback. Before sending sensor data, every automatic candidate must return `service=openglass-ios`, `protocol=1`, and its stable `receiver_name` from `/health`. The receiver name is remembered: moving networks may change its address, but does not silently select a different phone. Multiple unknown Bonjour receivers require manual selection/address entry. Hotel networks may isolate clients; use the iPhone hotspot if that happens.

Auto mode never falls back to USB, and ignores loopback addresses saved by older versions. Audio and images share one selected endpoint. Reconnect delays increase from 1 second to a maximum of 30 seconds while the app remains running; network changes trigger discovery again. This is bounded backoff, not a promise to bypass a hotspot that the system disconnected.

The visible **Connection / 连接设置** button (also Menu or a long press on the status text) offers Auto, Manual address, USB relay, and Choose phone again. Manual accepts `http://<address>:18080`; USB selects `http://127.0.0.1:18080` and requires the existing computer USB relay. Choose phone again forgets the remembered receiver. Tap the status text or press Enter/DPAD to stop/start; Back stops, then exits.

Launch intents remain compatible: an explicit `pc_base_url` fixes the endpoint for that launch and persists the URL as a future fallback. `auto_discover=true` explicitly selects Auto and overrides the old saved USB address. The connection-settings mode persists, while an intent URL alone does not turn a future launcher start into USB mode.

## Existing PC workflow

No PC IP is needed at build time. The PC panel supplies `pc_base_url` at launch, which remains a fixed direct connection without requiring the iPhone health schema. Configure `rokid_adb`, `rokid_mode`, and `rokid_pc_url` in ignored `runtime/openglass_omni/runtime.local.json`, then run `python glasses_panel.py`. The PC workflow may use wireless ADB for launch; it is separate from the direct iPhone Wi-Fi transport above.

See [iPhone setup](../ios/README_zh.md) or [PC runtime setup](../runtime/openglass_omni/STARTUP_en.md). Model inference, speech recognition and multi-turn responses belong to the receiving iPhone/PC app, not this sensor collector.
