from __future__ import annotations

import unittest
from pathlib import Path

import cv2
import numpy as np

from extensions.assistive_harness.cv.yolo_onnx import YoloOnnxProvider


MODEL = (
    Path(__file__).resolve().parents[4]
    / "OmniHarness"
    / "mini_omni_harness"
    / "models"
    / "yolo26n.onnx"
)


@unittest.skipUnless(MODEL.is_file(), "local YOLO reference weights are unavailable")
class YoloOnnxSmokeTests(unittest.TestCase):
    def test_blank_jpeg_runs_real_onnx_inference(self) -> None:
        ok, encoded = cv2.imencode(".jpg", np.zeros((480, 640, 3), dtype=np.uint8))
        self.assertTrue(ok)
        observation = YoloOnnxProvider(MODEL).analyze(
            encoded.tobytes(),
            "blank",
            1.0,
            "find_object",
            {"target": "手机"},
        )
        self.assertEqual(observation.provider, "yolo_onnx")
        self.assertEqual(observation.values["status"], "ok")
        self.assertEqual(observation.values["canonical_label"], "cell phone")
        self.assertEqual(observation.values["model"], "yolo26n.onnx")
        self.assertIsInstance(observation.values["detections"], list)


if __name__ == "__main__":
    unittest.main()
