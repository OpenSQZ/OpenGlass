from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def nearest_rank(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return ordered[index]


def load_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def resolve_run(value: str | None) -> Path:
    runs_root = Path(__file__).resolve().parent / "runs"
    if value:
        candidate = Path(value).expanduser().resolve()
        if not candidate.is_dir():
            raise SystemExit(f"Run directory does not exist: {candidate}")
        return candidate
    candidates = [path for path in runs_root.iterdir() if path.is_dir()]
    if not candidates:
        raise SystemExit(f"No run directories found under {runs_root}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize real browser RESET telemetry")
    parser.add_argument("--run", help="specific run directory; defaults to the newest run")
    args = parser.parse_args()

    run_dir = resolve_run(args.run)
    events = load_events(run_dir / "session_events.jsonl")
    resets = [
        event
        for event in events
        if event.get("type") == "control.ack"
        and event.get("intent") == "reset_session"
    ]
    successes = [event for event in resets if event.get("ok") is True]
    latencies = [
        float(event["restart_latency_ms"])
        for event in successes
        if isinstance(event.get("restart_latency_ms"), (int, float))
    ]
    changed_ids = [
        event
        for event in successes
        if event.get("old_session_id")
        and event.get("new_session_id")
        and event.get("old_session_id") != event.get("new_session_id")
    ]

    print(f"Run: {run_dir}")
    print(f"RESET success: {len(successes)}/{len(resets)}")
    print(f"Session ID changed: {len(changed_ids)}/{len(successes)}")
    if latencies:
        print(f"Restart P50: {nearest_rank(latencies, 0.50):.1f} ms")
        print(f"Restart P90: {nearest_rank(latencies, 0.90):.1f} ms")
        print(f"Restart max: {max(latencies):.1f} ms")
    else:
        print("Restart P50/P90: no RESET latency samples")
    print(
        "Dropped stale callbacks (cumulative): "
        f"text={max((int(event.get('dropped_old_text') or 0) for event in successes), default=0)}, "
        f"audio={max((int(event.get('dropped_old_audio') or 0) for event in successes), default=0)}"
    )
    print("Audible old-output pollution: manual observation required")
    for index, event in enumerate(successes, start=1):
        print(
            f"{index:02d}. generation={event.get('generation')} "
            f"session={event.get('old_session_id') or '-'}->{event.get('new_session_id') or '-'} "
            f"cleanup={event.get('cleanup_mode') or '-'} "
            f"latency={float(event.get('restart_latency_ms') or 0):.1f} ms"
        )


if __name__ == "__main__":
    main()
