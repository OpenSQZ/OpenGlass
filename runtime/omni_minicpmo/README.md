# MiniCPM-o Omni Runtime

This directory documents the release boundary for the experimental OpenGlass Omni runtime. A standalone runnable adapter is now available at [`../openglass_omni/`](../openglass_omni/); MiniCPM-o-Demo and llama.cpp-omni remain external upstream checkouts.

## Scope

The experimental direction integrates:

- MiniCPM-o 4.5 for audio-visual interaction research.
- `llama.cpp-omni` as an upstream model-serving dependency.
- MiniCPM-o-Demo as an upstream worker/gateway dependency.
- ESP32 sensing input from OpenGlass firmware.
- Prototype Rokid input, pending source and distribution review.

The goal is to explore lower-friction spoken visual assistance, not to present a finalized public runtime.

## Conceptual Process Roles

| Role | Conceptual responsibility | Public status |
| --- | --- | --- |
| Model server | Runs the local MiniCPM-o backend through an upstream runtime | Existing external llama.cpp-omni build |
| Worker | Connects the upstream demo layer to the model backend | Not included as OpenGlass-owned code |
| Gateway | Manages duplex runtime communication | Public port and lifecycle not yet verified |
| OpenGlass bridge | Connects ESP32 input to the gateway | Included under `runtime/openglass_omni/` |

Local paths and device addresses must remain in ignored local configuration:

- `<LLAMA_OMNI_COMMIT>`
- `<MINICPM_O_DEMO_COMMIT>`
- `<MODEL_PATH>`
- `<BACKEND_PORT>`
- `<WORKER_PORT>`
- `<GATEWAY_PORT>`
- `<ESP32_IP>`

## ESP32 Input Direction

The public firmware provides camera capture and WebSocket audio behavior. Runtime endpoint compatibility still needs verification, so documentation should not assume that all prototype endpoint names match the tracked firmware.

## Rokid Integration Status

Rokid integration is not a complete public release in Phase 1. Source code, APK distribution rights, permissions, protocol details, and safety/privacy review must be completed before publishing it as a supported path.

## Prompt Switching and Interaction Research

The Omni direction studies:

- Prompt switching.
- Multiturn interaction.
- Barge-in behavior.
- Runtime prompt bundles.
- Text-injection style control.

These are research topics with known limitations. Do not describe prompt injection, long sessions, or lifecycle management as settled until public code and tests demonstrate the behavior.

## Known Limitations

- The launcher and device parser are present but not clean-machine validated.
- The locally working gateway and worker ports are configurable; do not treat them as universal upstream defaults.
- Process lifecycle behavior is implemented in OpenGlass, including stopping the worker process tree that owns llama-server.
- Upstream commits are recorded as locally observed, not yet release-validated.
- Endpoint compatibility between firmware and prototype bridge needs verification.
- Rokid source/APK is not public.
- Model weights are not included and should not be committed.

## Future Publication Checklist

- Pin `<LLAMA_OMNI_COMMIT>` and `<MINICPM_O_DEMO_COMMIT>`.
- Publish or link exact dependency versions.
- Validate the standalone launcher on a clean machine.
- Verify ESP32 image and audio endpoint compatibility on tracked firmware.
- Verify process ownership and lifecycle behavior under failure and restart conditions.
- Provide clean-machine setup validation.
- Review all logs, prompts, and assets for privacy and publication rights.
