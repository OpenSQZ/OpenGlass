# Lightweight CV shadow providers

This package is the transport-neutral, optional computer-vision side lane for
the OpenGlass ESP32 bridge. V1 has four deliberately small guarantees:

1. providers implement one synchronous `analyze()` contract;
2. inference runs in a dedicated executor, never on the audio event loop;
3. a capacity-one queue drops stale frames and keeps the newest waiting frame;
4. provider errors and timeouts become `CVObservation` log records and never
   control MiniCPM or navigation behavior.

Built-in references are `noop`, `yolo_onnx`, and the intentionally incomplete
`ocr_template`. Copy `ocr_template.py` to a backend-specific module and load it
with `--cv-shadow-provider perception.ocr_<backend>:OcrProvider`. See the
[Chinese Phase A/B handoff](../../../docs/phase_ab_esp32_ocr_handoff_zh.md) for
the complete command and output contract.

The V1 lane is **shadow-only**. Consuming observations in a Skill or generating
spoken guidance is a later, separately tested integration step.
