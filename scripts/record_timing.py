#!/usr/bin/env python3
"""Append one timing event to pipeline-timings.json — stop hand-editing JSON.

Two modes:

  # Wrap a command: times it, derives status from the exit code, appends event.
  python scripts/record_timing.py --timings <p>/pipeline-timings.json \
      --stage tts_candidate_1 --cache-key <key> -- <command> [args...]

  # Record a known result (e.g. a cache hit or an externally-timed stage).
  python scripts/record_timing.py --timings <p>/pipeline-timings.json \
      --stage illustrations --status cache_hit --elapsed-s 0.5 --cache-key <key>

Creates the trace (schema 1, wall_clock_started_at=now) when missing. Events
are append-only; retries should pass --attempt N instead of overwriting. The
wrapped command's exit code is propagated so this can wrap gate commands too.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGIC_VERSION = 1

STATUSES = ("pass", "fail", "cache_hit", "dry_run", "skipped")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    partial = path.with_name(path.name + ".part")
    partial.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial.replace(path)


def load_or_init(path: Path) -> dict[str, Any]:
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError(f"{path} is not a schema 1 timing trace")
        if not isinstance(payload.get("events"), list):
            payload["events"] = []
        return payload
    return {
        "schema_version": 1,
        "wall_clock_started_at": now_iso(),
        "wall_clock_finished_at": None,
        "wall_clock_elapsed_s": None,
        "events": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timings", type=Path, required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--status", choices=STATUSES, default=None,
                        help="manual mode; omit when wrapping a command")
    parser.add_argument("--elapsed-s", type=float, default=None, help="manual mode elapsed seconds")
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--cache-key", default="")
    parser.add_argument("--cache-hit", action="store_true")
    parser.add_argument("--metadata-json", default=None, help="optional JSON object string")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="command to wrap, preceded by --")
    args = parser.parse_args()

    command = args.command
    if command and command[0] == "--":
        command = command[1:]

    metadata = {}
    if args.metadata_json:
        metadata = json.loads(args.metadata_json)
        if not isinstance(metadata, dict):
            raise ValueError("--metadata-json must be a JSON object")

    returncode = 0
    if command:
        if args.status is not None or args.elapsed_s is not None:
            raise SystemExit("wrap mode and manual --status/--elapsed-s are mutually exclusive")
        started_at = now_iso()
        started = time.monotonic()
        proc = subprocess.run(command)
        elapsed = round(time.monotonic() - started, 2)
        finished_at = now_iso()
        status = "pass" if proc.returncode == 0 else "fail"
        returncode = proc.returncode
        if metadata is not None:
            metadata.setdefault("command", command)
    else:
        if args.status is None:
            raise SystemExit("manual mode requires --status (or pass a command after --)")
        status = args.status
        elapsed = float(args.elapsed_s if args.elapsed_s is not None else 0.0)
        finished_at = now_iso()
        started_at = finished_at

    path = args.timings.expanduser().resolve()
    payload = load_or_init(path)
    payload["events"].append(
        {
            "stage": args.stage,
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_s": elapsed,
            "status": status,
            "attempt": args.attempt,
            "cache_hit": bool(args.cache_hit or status == "cache_hit"),
            "cache_key": args.cache_key,
            "metadata": metadata,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, payload)
    print(f"recorded: {args.stage} status={status} elapsed_s={elapsed} -> {path}")
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
