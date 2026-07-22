# Roadmap

This roadmap is milestone-oriented rather than date-driven. Items can move between categories as artifacts are verified.

## Available

- ESP32-S3 camera and PDM microphone firmware.
- HTTP capture and preview behavior.
- WebSocket audio streaming from the public firmware path.
- Evaluation configs, adapter code, metrics, aggregation, and local/cloud baseline hooks.
- Selected Omni experiment harnesses for think-strategy, multiturn, and barge-in style analysis.
- Standalone experimental Omni panel, ESP32 bridge, device parser, prompt presets, recording, and replay source.
- Locally observed upstream commit lock and successful local start/stop smoke test.
- Phase 1 documentation for architecture, safety, hardware, runtime, and release review.

## In Progress

- Core documentation and reproducibility cleanup.
- Clear quickstart paths for public evaluation scripts.
- Hardware artifact verification for CAD, BOM, print settings, wiring, and assembly.
- Public asset review for figures and photos.
- Privacy and release checklist refinement.
- Alignment between public firmware endpoints and prototype runtime expectations.

## Planned

- Sanitized hardware release package with verified STEP/STL/3MF status.
- Sanitized BOM with checked component names, quantities, sources, and alternatives.
- Wiring diagrams, pin maps, soldering notes, and battery safety documentation.
- Clean-machine Omni runtime validation against recorded upstream commits.
- Failure-injection tests for launcher lifecycle and process ownership.
- One-click session rerun UI and skill restart/import interface.
- Rokid adapter publication review if source, protocol, and distribution rights are available.
- Privacy controls for logging, retention, and redaction.
- Accessibility feedback from blind and low-vision users.
- Exploration of mobile or lower-power nearby edge hosts.

## Requires External Validation

- Battery capacity, runtime, charging behavior, and thermal safety.
- Wear comfort across users and session lengths.
- Camera autofocus behavior and final camera module naming.
- Final print material, slicer profile, orientation, support, and post-processing settings.
- Evaluation image consent and publication rights.
- Study protocols and accessibility evaluation with BLV participants.
- Reproducible Omni setup across clean machines.

## Publication Boundaries

- Publishing unreviewed CAD, BOM spreadsheets, photos, videos, logs, or private runtime files.
- Vendoring model weights or upstream runtime repositories.
- Presenting cloud APIs as the default OpenGlass deployment.
