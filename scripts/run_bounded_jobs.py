#!/usr/bin/env python3
"""Run TTS or render jobs with the pipeline's fixed concurrency and timing contract."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LIMITS = {"tts": 1, "render": 2}


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial.replace(path)


def validate_job(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"job {index} must be an object")
    job_id = str(raw.get("id", "")).strip()
    command = raw.get("command")
    if not job_id or not isinstance(command, list) or not command or not all(
        isinstance(item, str) and item for item in command
    ):
        raise ValueError(f"job {index} requires id and a non-empty string command array")
    cwd = Path(str(raw.get("cwd", "."))).expanduser().resolve()
    if not cwd.is_dir():
        raise FileNotFoundError(f"job {job_id} cwd is missing: {cwd}")
    return {
        "id": job_id,
        "command": command,
        "cwd": cwd,
        "attempt": int(raw.get("attempt", 1)),
        "cache_hit": raw.get("cache_hit") is True,
        "cache_key": str(raw.get("cache_key", "")),
        "metadata": raw.get("metadata", {}) if isinstance(raw.get("metadata"), dict) else {},
    }


def run_job(job: dict[str, Any], kind: str, dry_run: bool) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    if job["cache_hit"]:
        status, returncode = "cache_hit", 0
    elif dry_run:
        status, returncode = "dry_run", 0
    else:
        result = subprocess.run(job["command"], cwd=job["cwd"], check=False)
        returncode = result.returncode
        status = "pass" if returncode == 0 else "fail"
    finished_at = datetime.now(timezone.utc)
    return {
        "job_id": job["id"],
        "stage": f"{kind}_job",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_s": round(time.perf_counter() - started, 3),
        "status": status,
        "attempt": job["attempt"],
        "cache_hit": job["cache_hit"],
        "cache_key": job["cache_key"],
        "returncode": returncode,
        "metadata": {**job["metadata"], "command": job["command"], "cwd": str(job["cwd"])},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--kind", choices=sorted(LIMITS), required=True)
    parser.add_argument("--timings", type=Path, required=True)
    parser.add_argument("--max-workers", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    jobs_path = args.jobs.expanduser().resolve()
    data = json.loads(jobs_path.read_text(encoding="utf-8"))
    raw_jobs = data.get("jobs", []) if isinstance(data, dict) else []
    jobs = [validate_job(raw, index) for index, raw in enumerate(raw_jobs, start=1)]
    if not jobs:
        raise ValueError("jobs manifest must contain at least one job")
    worker_limit = LIMITS[args.kind]
    workers = args.max_workers if args.max_workers is not None else worker_limit
    if not 1 <= workers <= worker_limit:
        raise ValueError(f"{args.kind} max-workers must be 1-{worker_limit}")

    batch_started = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(run_job, job, args.kind, args.dry_run): job for job in jobs}
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: next(i for i, job in enumerate(jobs) if job["id"] == item["job_id"]))

    timing_path = args.timings.expanduser().resolve()
    timing = (
        json.loads(timing_path.read_text(encoding="utf-8"))
        if timing_path.is_file()
        else {
            "schema_version": 1,
            "wall_clock_started_at": batch_started.isoformat(),
            "wall_clock_finished_at": None,
            "wall_clock_elapsed_s": None,
            "events": [],
        }
    )
    timing.setdefault("events", []).extend(results)
    atomic_json(timing_path, timing)
    print(json.dumps({"kind": args.kind, "workers": workers, "results": results}, ensure_ascii=False))
    return 1 if any(item["status"] == "fail" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
