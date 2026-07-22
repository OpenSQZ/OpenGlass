# OpenGlass Runtime

OpenGlass currently has two software paths:

1. A public modular ASR/VLM/TTS path backed by the tracked evaluation and demo scripts.
2. An experimental Omni path around MiniCPM-o 4.5 and llama.cpp-omni, now with a standalone OpenGlass adapter and panel.

The runtime documentation distinguishes the included standalone ESP32/Omni adapter from still-private or unavailable prototype packaging. The launcher, device registry parser, and lifecycle manager are included; the Rokid package is not.

## Current Modular Path

The public repository supports experiments in which a nearby host runs:

- Optional ASR for spoken input.
- Local OpenAI-compatible VLM inference.
- Streaming text response handling.
- Local TTS or audio output.
- Evaluation, latency logging, and aggregation.

The ESP32-side unit captures camera and microphone data. The model runs on the nearby host.

## Experimental Omni Path

The Omni path explores direct audio-visual interaction with MiniCPM-o 4.5 through upstream runtime components. It is intended for research on:

- Streaming audio and vision input.
- Multiturn interaction.
- Barge-in behavior.
- Prompt switching.
- Runtime prompt bundles.

The standalone adapter lives in [`openglass_omni/`](openglass_omni/). It launches external upstream checkouts without copying OpenGlass code into MiniCPM-o-Demo. Known limitations and release gates remain documented in [`omni_minicpmo/README.md`](omni_minicpmo/README.md).

## Public Code vs Prototype Packaging

Available in this repository:

- ESP32 sensing firmware.
- Evaluation framework and configs.
- Public modular demo/evaluation scripts.
- Selected Omni experiment harnesses.
- Standalone worker/gateway/ESP32 bridge control panel.
- Public device registry parser and sanitized example schema.
- Session recording, playback pages, and rerun input support in the bridge.
- Documented process lifecycle and a locally observed upstream lock record.

Not yet included:

- Complete Rokid source or APK.
- One-click session rerun UI and a verified skill restart/import interface.
- Clean-machine reproducibility evidence across supported environments.
- Verified endpoint compatibility between every tracked firmware and runtime variant.

## Upstream Dependency Policy

OpenGlass should not vendor upstream runtime repositories or model weights by default. Public runtime documentation should link to upstream projects, pin exact commits for reproducibility, and keep model paths as local placeholders such as `<MODEL_PATH>`.

## Release Status

The modular path remains the public baseline. The Omni path is experimental even though the adapter and lifecycle manager are now present. Clean-machine validation, firmware endpoint compatibility, long-session behavior, skill switching, and Rokid publication review remain open.
