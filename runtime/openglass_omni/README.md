# OpenGlass Omni Runtime

This directory holds OpenGlass's own control panel, the ESP32 audio/video bridge, the Rokid link, and the local session recording / replay code. The MiniCPM-o-Demo and llama.cpp-omni projects stay **external** — nothing here is copied into an upstream directory, and this panel never downloads, builds, or rewrites upstream config.

This is an experimental research integration. It is not production-ready, not a certified navigation aid, and not validated for unbounded-length sessions.

> For first-time setup (building llama.cpp-omni, downloading model weights, bringing up `worker.py` / `gateway.py`, flashing the ESP32), follow the **[top-level README](../../README.md)**. This page describes what lives in this directory and how it behaves once the upstream services are in place.

## What's in here

| File | Role |
| --- | --- |
| `panel.py` | Control-panel logic: a process manager that starts/stops the launch chain, polls readiness, collects per-process logs, and hosts the pywebview window. |
| `panel.html` | The panel UI (kept as a standalone file, like `templates/live.html`). Loaded by `panel.py` from the same directory. |
| `esp32_bridge.py` | The host-side ESP32 duplex bridge: pulls camera JPEG + PDM audio from the glasses, streams to the gateway over `/v1/realtime`, plays back TTS, records the session, and serves the live first-person view. |
| `rokid_minicpm_v8.py` | The Rokid link (APK connects in; no device selection). Shares the same worker/gateway front stages. |
| `bridge_ui.py` | Local web server (default `http://localhost:8080`) for the live first-person view embedded in the panel, plus a `/replay` session browser. Without it, the panel's right pane is blank. |
| `recorder_live.py` | Records every session to `sessions/` (video, user/AI audio tracks, `events.jsonl` subtitles, `meta.json`). |
| `rerun_source.py` | Replays a recorded session back through the model (see [Rerun mode](#rerun-mode-command-line)). Not wired into the panel. |
| `devices.json` | Glasses IP / rotation table. The panel's device dropdown follows this file. |
| `templates/` | `live.html`, `replay.html`, `replay_index.html` — served by `bridge_ui.py`. |

The entry point is the repository-root `glasses_panel.py`, an 8-line shim that calls `runtime.openglass_omni.panel:main`.

## Process chain

```text
glasses_panel.py  (root shim → runtime.openglass_omni.panel:main)
  └─ panel.py starts, in order:
       1. llama-omni-server      (llama.cpp-omni build; C++ backend, port 22500)
       2. MiniCPM-o-Demo/worker.py   (--backend-server-url http://127.0.0.1:22500, port 22400)
       3. MiniCPM-o-Demo/gateway.py  (port 8006)
       4. esp32_bridge.py  (or rokid_minicpm_v8.py)
            ├─ ESP32 camera / audio input   (or Rokid APK)
            ├─ local session recording (recorder_live.py) → sessions/
            └─ local first-person web UI (bridge_ui.py, :8080)
```

The large model runs on the nearby host. The ESP32 only senses and streams. The panel launches the backend first, waits for its `/health` to return 200, then starts worker, gateway, and the bridge.

## Running it

Install both dependency sets into one environment (see the top-level README for details), then launch from the repository root:

```bash
python glasses_panel.py
```

Pick a device and prompt, click **Start**, wait for four green indicators, and the right pane shows the glasses' first-person view.

> **Use a named conda environment, not `base`.** The panel launches the bridge via `conda run -n <env> python esp32_bridge.py --prompt "..."`. With `base`, `conda run` can truncate a multi-line `--prompt`, so the system prompt is silently dropped and the model falls back to a generic default. A named environment avoids this. The bridge logs the prompt it actually received as `[PROMPT] len=… head=…` at startup — check that line if the model ignores your prompt.

## Panel controls

| Control | Behavior |
| --- | --- |
| **Start (一键启动)** | Starts `llama-omni-server` → `worker` → `gateway` → `demo` in order, waiting for each to be ready. Uses the prompt currently shown in the panel. |
| **Stop (停止)** | Gracefully stops only the bridge (so the session flushes to disk); backend/worker/gateway stay warm. |
| **Start again** | After Stop, brings the bridge back up quickly (front stages still running). |
| **Stop All (全部停止)** | Stops bridge → gateway → worker → llama-omni-server. |
| **Chain dropdown** | Switches between the **ESP32** and **Rokid** links; the front three stages are shared, only the fourth process differs. Rokid hides the device dropdown (the APK connects inbound). |
| **Close window** | Runs Stop All. |

The panel refuses to adopt a process that already occupies a target port but wasn't started by the panel, so it won't kill a service you launched by hand.

## Configuration

Edit the `CONFIG` dict at the top of `panel.py`, plus `devices.json`. See the top-level README's Configuration table for the full list; the entries specific to this module:

| What | Where |
| --- | --- |
| Backend `.exe` + model `.gguf` paths | `procs["llama"]` in `panel.py` (`<PATH_TO>` placeholders — must edit) |
| Glasses IP / rotation | `devices.json` (one entry per pair; the dropdown follows it) |
| Conda environment | `conda_env` (use a **named** env, not `base`) |
| Worker ready port | `worker_ready_port` (must match your `worker.py` port; default `22400`) |
| MiniCPM-o-Demo directory | `minicpm_demo_dir` (absolute path to your MiniCPM-o-Demo clone; worker/gateway start there) |
| Working directory | `cwd` (optional; only affects llama/demo/rokid, which use absolute paths — normally empty) |

Model weights, backend paths, and the C++ config live in the **upstream** MiniCPM-o-Demo / llama.cpp-omni projects. The panel only launches and supervises processes and shows the first-person view — it does not read or validate upstream model configuration.

## Session recording and replay

Every session is written to `sessions/<timestamp>/` by `recorder_live.py`: the composed video, separate user/AI audio tracks, `events.jsonl` (subtitles/events), and `meta.json`. `bridge_ui.py` serves:

- `http://localhost:8080/` — the live first-person view (also embedded in the panel).
- `http://localhost:8080/replay` — a browser of past sessions with video + synced subtitles.

You can also run `bridge_ui.py` standalone as a replay-only server (no glasses, no model):

```bash
python runtime/openglass_omni/bridge_ui.py --sessions sessions --port 8080
```

## Rerun mode (command line)

`rerun_source.py` feeds a previously recorded session back into the model instead of a live ESP32 — the same bridge, but audio/images come from disk. This is the most convenient way to re-test the model repeatedly against a fixed input, without the glasses. It is a command-line workflow, not a panel button.

Bring up the front stages first (via the panel, or manually: `llama-omni-server` → `worker.py` → `gateway.py`), then run the bridge in rerun mode:

```bash
python runtime/openglass_omni/esp32_bridge.py \
  --rerun-from sessions/<session-id> \
  --gateway localhost:8006 \
  --prompt "your prompt"
```

Notes:

- **Gateway TLS**: the V2 gateway (`8006`) accepts `wss`, and the bridge defaults to it — do **not** pass `--no-tls`.
- **No device flags**: rerun does not connect to glasses, so omit `--device` / `--device-config`.
- **Inputs**: the session directory must contain `user_raw.pcm` (or `live_user.wav`), `images/`, and `events.jsonl` — all produced by a normal recorded run.
- **Optional**: `--rerun-speed` (default `1.0`), `--rerun-drain-s` (default `5.0`), `--ui-port` (default `8080`, change it if a live session is already using 8080).
- **Requires `sounddevice`**: rerun plays the recorded user audio through a separate output stream. Any machine that can run a live session already has it.

Watch the rerun via the bridge's own live view at `http://localhost:<ui-port>/`.

## Current boundaries

- The panel starts and supervises processes and shows the first-person view; it does not own model weights, backend paths, or upstream configuration.
- `worker.py` / `gateway.py` and the model weights come from external upstream projects and are not vendored here.
- The Rokid link is included, but its gateway protocol may differ from the ESP32 link depending on your build; treat the ESP32 link as the primary supported path.
- One-click rerun from within the panel is not implemented; rerun is the command-line workflow above.
- Session output under `sessions/` may contain faces, surroundings, voices, and device addresses. Review it before sharing or publishing.
