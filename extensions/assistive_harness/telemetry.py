from __future__ import annotations

import csv
import json
import threading
import time
from pathlib import Path
from typing import Any

import yaml


class TelemetryWriter:
    FILES = {
        "asr": "asr_events.jsonl",
        "control": "control_events.jsonl",
        "session": "session_events.jsonl",
        "skill": "skill_events.jsonl",
        "cv": "cv_events.jsonl",
        "echo": "echo_events.jsonl",
        "model": "model_events.jsonl",
    }

    def __init__(self, root: str | Path, run_id: str, config: dict[str, Any]):
        self.run_id = run_id
        self.run_dir = Path(root) / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        (self.run_dir / "config_snapshot.yaml").write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=True),
            encoding="utf-8",
        )
        self._metrics_path = self.run_dir / "metrics.csv"
        with self._metrics_path.open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerow(["timestamp_ms", "metric", "value", "event_id"])

    def write(self, stream: str, payload: dict[str, Any]) -> None:
        filename = self.FILES[stream]
        record = {"logged_at_ms": time.time() * 1000, **payload}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with (self.run_dir / filename).open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def metric(self, name: str, value: float, event_id: int | None = None) -> None:
        with self._lock:
            with self._metrics_path.open("a", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerow([time.time() * 1000, name, value, event_id or ""])

    def write_summary(self, summary: dict[str, Any]) -> None:
        (self.run_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def append_model_transcript(self, turn: dict[str, Any]) -> None:
        text = str(turn.get("text") or "").replace("\r", " ").replace("\n", " ")
        line = (
            f"turn={turn.get('turn_index')} role={turn.get('role')} "
            f"generation={turn.get('generation')} skill={turn.get('skill_id')} "
            f"audio_ms={turn.get('audio_ms')} text={text}\n"
        )
        with self._lock:
            with (self.run_dir / "model_transcript.txt").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(line)
