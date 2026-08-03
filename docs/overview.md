# Project Overview

OpenSQZ OpenGlass is a local-first research platform for real-time visual assistance. It is designed around a sensing-computing split: lightweight wearable hardware captures first-person visual and audio context, while a nearby laptop or edge host performs local multimodal inference and speech interaction.

The project is primarily intended for blind and low-vision visual assistance research, accessibility prototyping, and reproducible evaluation of latency-aware multimodal interaction. It is not a finished consumer product or a certified safety aid.

## Motivation

First-person visual assistance can involve private spaces, screens, documents, bystanders, and time-sensitive tasks. Cloud-only workflows may provide strong model capability, but they can increase data exposure and introduce network delays. Wearable glasses are well suited for sensing, but the ESP32-class device in this repository cannot host large multimodal models.

OpenGlass separates these roles:

- The wearable unit senses and transports data.
- The nearby host runs ASR, VLM or Omni inference, response handling, TTS, and evaluation.
- Optional cloud adapters are retained for benchmarking, not as the default deployment path.

## Current Public Implementation

The tracked repository currently provides:

- ESP32-S3 camera and PDM microphone firmware.
- HTTP camera capture and preview behavior.
- WebSocket audio streaming from the ESP32 firmware.
- Nearby-host ASR, VLM, and TTS experimentation scripts.
- Evaluation configs, metrics, aggregation, and latency tooling.
- Selected Omni experiment harnesses for research evaluation.
- A standalone experimental Omni panel, device parser, ESP32 bridge, recording, and replay source under `runtime/openglass_omni/`.

The tracked repository does not currently provide:

- Complete Rokid source or APK release.
- Verified CAD/STL/BOM release artifacts.
- A release-grade skill runtime.
- Clean-machine validation of the Omni adapter across supported operating systems and accelerators.

## Relationship Among Tracks

**Core** is the public baseline for sensing, local inference experiments, and evaluation.

**Hardware** turns the prototype into a reproducible 3D-printable glasses package, but the public CAD/BOM release still needs human verification.

**OmniRuntime** explores lower-friction audio-visual interaction with MiniCPM-o and llama.cpp-omni. Its standalone adapter is included, but it remains experimental and depends on external upstream components that are not vendored here.

## Intended Research Use

OpenGlass is best treated as a reference platform for:

- Measuring latency and response quality in local-first visual assistance.
- Testing camera/audio sensing pipelines on ESP32-S3 hardware.
- Comparing modular ASR/VLM/TTS and Omni-style interaction paths.
- Preparing reproducible hardware and evaluation artifacts.
- Studying privacy and safety boundaries for first-person camera systems.
