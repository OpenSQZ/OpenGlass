# Lightweight CV provider framework (V1)

This directory is the transport-neutral CV shadow path used by the Assistive
Harness. Browser and future OpenGlass Adapters only provide JPEG frames. They do
not call YOLO, OCR, depth estimation, or control rules directly.

## V1 data flow

```text
Browser / ESP32 Adapter
        |
        | FrameEnvelope(JPEG, timestamp, skill_id, slots)
        v
CVPipeline.submit()                 # returns immediately
        |
        | capacity-one latest-frame queue (old queued frame is dropped)
        v
single background worker
        |
        | provider.analyze() in a dedicated CPU thread
        v
CVObservation -> cv_events.jsonl    # shadow only; no control decision
```

The Router, state machine, Session Adapter, and MiniCPM audio/video path do not
depend on a concrete CV model.

## Guarantees and deliberate limits

- `submit()` never waits for inference.
- The queue is bounded and latest-frame wins; overload cannot grow memory
  without limit.
- Only one inference call runs at a time per client pipeline.
- Provider errors become `CVObservation.error` and never escape to the control
  socket.
- A timeout is recorded without blocking STOP/RESET. Python cannot kill an
  already-running native ONNX call safely, so that one worker lane remains
  occupied until the native call returns; new queued frames continue to
  collapse to the latest frame.
- V1 accepts only `disabled` and `shadow`. Shadow observations never switch a
  Skill, suppress MiniCPM, or speak to the user.
- Temporal voting, result fusion, navigation decisions, and accuracy tuning are
  intentionally outside V1.

## Configuration

Providers are declared once under `cv.providers`. Each Skill selects one with
`cv_provider`:

```yaml
cv:
  max_fps: 1
  queue_size: 1
  inference_timeout_ms: 2000
  providers:
    noop:
      type: noop
    yolo_onnx:
      type: yolo_onnx
      model_path: path/to/yolo.onnx
      device: cpu
      confidence: 0.25
      image_size: 640

skills:
  find_object:
    cv_mode: shadow
    cv_provider: yolo_onnx
```

Relative model paths are resolved against the YAML directory. Weights are not
owned by the Core. The shipped sample resolves to the Git-ignored local file
`OpenGlass/models/yolo26n.onnx`.

## Provider contract

A provider is synchronous. `CVPipeline` is responsible for running it outside
the asyncio/control loop:

Provider instances are shared so model weights load only once. If a backend is
not safe for concurrent calls from multiple connected clients, the provider
must protect that backend with its own lock, as `YoloOnnxProvider` does.

```python
from typing import Any
from extensions.assistive_harness.cv.base import CVObservation


class OcrOnnxProvider:
    def __init__(self, model_path):
        self.model_path = model_path
        self._session = None                 # lazy-load on first frame

    def analyze(
        self,
        frame: bytes | None,
        frame_id: str,
        timestamp_ms: float,
        skill_id: str,
        slots: dict[str, Any],
    ) -> CVObservation:
        if not frame:
            raise ValueError("OCR requires a JPEG frame")
        # 1. Decode JPEG.
        # 2. Lazy-load and run the local model.
        # 3. Return JSON-serializable values only.
        return CVObservation(
            frame_id=frame_id,
            timestamp_ms=timestamp_ms,
            skill_id=skill_id,
            provider="ocr_onnx",
            values={
                "status": "ok",
                "text": "recognized text",
                "regions": [],
                "latency_ms": 12.3,
                "model": self.model_path.name,
                "device": "cpu",
            },
        )
```

To add OCR:

1. Put the implementation in `cv/ocr_onnx.py` and keep model-specific imports
   inside that plugin.
2. Add one `ocr_onnx` factory branch in `build_provider_registry()` in
   `cv/registry.py`.
3. Add its config under `cv.providers`.
4. Set `read_text.cv_provider: ocr_onnx`; leave `cv_mode: shadow` during tests.
5. Add a blank/synthetic inference smoke plus one recorded real-frame test.

No changes are required in `router.py`, `state_machine.py`, the browser Adapter,
or the future ESP32 Frame Adapter.

## Observation schema

Every plugin produces the same envelope:

```json
{
  "frame_id": "browser_123",
  "timestamp_ms": 123.0,
  "skill_id": "find_object",
  "provider": "shadow:yolo_onnx",
  "values": {
    "status": "ok",
    "found": true,
    "detections": [],
    "latency_ms": 31.1,
    "pipeline_latency_ms": 42.0
  },
  "error": null
}
```

Plugin-specific data belongs under `values`; the outer keys stay stable for
logging, replay, and later OpenGlass migration.

## Validation

```powershell
python -m unittest `
  extensions.assistive_harness.tests.test_core `
  extensions.assistive_harness.tests.test_cv_yolo
```

The tests cover failure isolation, latest-frame dropping, timeout reporting,
non-blocking submission, and a real ONNX Runtime YOLO inference. A browser
acceptance run should additionally verify that `find_object` produces
`provider=shadow:yolo_onnx` records while STOP/RESET remain responsive.
