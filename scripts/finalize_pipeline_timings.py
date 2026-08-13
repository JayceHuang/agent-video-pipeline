#!/usr/bin/env python3
"""Close a pipeline timing trace and compute wall-clock/compute summaries."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("wall_clock_started_at must be an ISO date-time string")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    partial = path.with_name(path.name + ".part")
    partial.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timings", type=Path, required=True)
    args = parser.parse_args()

    path = args.timings.expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    events = payload.get("events", []) if isinstance(payload, dict) else []
    if payload.get("schema_version") != 1 or not isinstance(events, list) or not events:
        raise ValueError("timing trace must be schema 1 with non-empty events")
    started = parse_time(payload.get("wall_clock_started_at"))
    finished = datetime.now(timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    compute_elapsed = sum(
        max(0.0, float(event.get("elapsed_s", 0.0)))
        for event in events
        if isinstance(event, dict) and event.get("cache_hit") is not True
    )
    failed_event_count = sum(
        1 for event in events
        if isinstance(event, dict) and event.get("status") == "fail"
    )
    terminal_events = [
        event for event in events
        if isinstance(event, dict) and event.get("stage") == "pipeline_complete"
    ]
    terminal_status = terminal_events[-1].get("status") if terminal_events else None
    payload.update(
        {
            "wall_clock_finished_at": finished.isoformat(),
            "wall_clock_elapsed_s": round(max(0.0, (finished - started).total_seconds()), 3),
            "compute_elapsed_s": round(compute_elapsed, 3),
            "cache_hit_count": sum(
                1 for event in events
                if isinstance(event, dict) and event.get("cache_hit") is True
            ),
            "failed_event_count": failed_event_count,
            "recovered_failure_count": failed_event_count if terminal_status == "pass" else 0,
        }
    )
    # Candidate rejection and failed attempts are expected parts of an
    # adaptive pipeline. An explicit terminal event records whether all final
    # gates and delivery passed; historical failures remain counted for cost
    # and reliability analysis instead of poisoning the final status.
    payload["status"] = (
        terminal_status
        if terminal_status in {"pass", "fail"}
        else ("pass" if failed_event_count == 0 else "fail")
    )
    atomic_json(path, payload)
    print(
        json.dumps(
            {
                "timings": str(path),
                "status": payload["status"],
                "wall_clock_elapsed_s": payload["wall_clock_elapsed_s"],
                "compute_elapsed_s": payload["compute_elapsed_s"],
                "cache_hit_count": payload["cache_hit_count"],
            },
            ensure_ascii=False,
        )
    )
    return 1 if payload["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
