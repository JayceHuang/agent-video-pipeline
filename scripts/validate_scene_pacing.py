#!/usr/bin/env python3
"""Validate effective Chinese narration speed for a timeline."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from profile_config import get_in, load_resolved_profile


IGNORED = set(" \t\n，。！？；：、,.!?;:“”‘’（）()—｜")
CONTINUOUS_MODE = "continuous_episode_take"
GLOBAL_RETIME_PREFERRED_RANGE = (0.97, 1.03)
GLOBAL_RETIME_HARD_RANGE = (0.95, 1.05)
MAX_ADJACENT_RETIME_DELTA = 0.05
FLOAT_TOLERANCE = 1e-6


def effective_chars(text: str) -> int:
    return sum(1 for char in text if char not in IGNORED)


def scene_row(scene: dict) -> dict:
    count = int(scene.get("effective_chars") or effective_chars(str(scene.get("text", ""))))
    duration = float(scene.get("spoken_duration_s") or scene.get("duration_s") or 0.0)
    cpm = count * 60 / duration if duration > 0 else 0.0
    return {
        "id": scene.get("id"),
        "effective_chars": count,
        "spoken_duration_s": round(duration, 6),
        "effective_cpm": round(cpm, 3),
        "retime_factor": scene.get("retime_factor"),
    }


def continuous_report(timeline: dict, min_cpm: float, max_cpm: float) -> dict:
    boundary_tolerance_cpm = 0.25
    rows = [scene_row(scene) for scene in timeline.get("scenes", [])]
    errors = []
    diagnostics = []

    episode_chars = int(
        timeline.get("effective_chars")
        or timeline.get("total_effective_chars")
        or sum(int(row["effective_chars"]) for row in rows)
    )
    episode_duration_value = (
        timeline.get("spoken_duration_s")
        or timeline.get("total_spoken_duration_s")
        or timeline.get("duration_s")
        or timeline.get("total_duration_s")
    )
    episode_duration = float(
        episode_duration_value
        if episode_duration_value is not None
        else sum(float(row["spoken_duration_s"]) for row in rows)
    )
    supplied_episode_cpm = timeline.get("effective_cpm")
    if supplied_episode_cpm is None:
        supplied_episode_cpm = timeline.get("episode_effective_cpm")
    episode_cpm = (
        float(supplied_episode_cpm)
        if supplied_episode_cpm is not None
        else episode_chars * 60 / episode_duration
        if episode_duration > 0
        else 0.0
    )
    if not min_cpm - boundary_tolerance_cpm <= episode_cpm <= max_cpm + boundary_tolerance_cpm:
        errors.append(
            f"episode: {episode_cpm:.3f} CPM outside {min_cpm:.0f}-{max_cpm:.0f}"
        )

    for row in rows:
        cpm = float(row["effective_cpm"])
        if not min_cpm - boundary_tolerance_cpm <= cpm <= max_cpm + boundary_tolerance_cpm:
            diagnostics.append(
                f"{row['id']}: visual-scene CPM {cpm:.3f} outside "
                f"episode range {min_cpm:.0f}-{max_cpm:.0f} (diagnostic only)"
            )

    global_retime_raw = timeline.get("global_retime_factor")
    global_retime = None
    if global_retime_raw is None:
        errors.append("global_retime_factor is required for continuous_episode_take")
    else:
        try:
            global_retime = float(global_retime_raw)
        except (TypeError, ValueError):
            errors.append("global_retime_factor must be numeric")
        else:
            hard_min, hard_max = GLOBAL_RETIME_HARD_RANGE
            if not math.isfinite(global_retime):
                errors.append("global_retime_factor must be finite")
            elif not hard_min <= global_retime <= hard_max:
                errors.append(
                    f"global retime {global_retime:.3f} outside hard range "
                    f"{hard_min:.2f}-{hard_max:.2f}"
                )
            else:
                preferred_min, preferred_max = GLOBAL_RETIME_PREFERRED_RANGE
                if not preferred_min <= global_retime <= preferred_max:
                    diagnostics.append(
                        f"global retime {global_retime:.3f} outside preferred range "
                        f"{preferred_min:.2f}-{preferred_max:.2f} (diagnostic only)"
                    )

    scene_retimes = []
    for row in rows:
        raw_retime = row["retime_factor"]
        if raw_retime is None:
            errors.append(f"{row['id']}: retime_factor is required")
            continue
        try:
            retime = float(raw_retime)
        except (TypeError, ValueError):
            errors.append(f"{row['id']}: retime_factor must be numeric")
            continue
        if not math.isfinite(retime):
            errors.append(f"{row['id']}: retime_factor must be finite")
            continue
        scene_retimes.append((row["id"], retime))

    all_match_global = bool(scene_retimes) and global_retime is not None and all(
        abs(retime - global_retime) <= FLOAT_TOLERANCE for _, retime in scene_retimes
    )
    adjacent_deltas = [
        {
            "from": previous_id,
            "to": current_id,
            "delta": round(abs(current - previous), 6),
        }
        for (previous_id, previous), (current_id, current) in zip(
            scene_retimes, scene_retimes[1:]
        )
    ]
    adjacent_within_limit = len(scene_retimes) >= 2 and all(
        item["delta"] <= MAX_ADJACENT_RETIME_DELTA + FLOAT_TOLERANCE
        for item in adjacent_deltas
    )
    if len(scene_retimes) == len(rows) and rows and not (
        all_match_global or adjacent_within_limit
    ):
        errors.append(
            "scene retime factors must all equal global_retime_factor or every "
            f"adjacent delta must be <= {MAX_ADJACENT_RETIME_DELTA:.2f}"
        )

    return {
        "status": "pass" if not errors else "fail",
        "generation_mode": CONTINUOUS_MODE,
        "allowed_range": [min_cpm, max_cpm],
        "boundary_tolerance_cpm": boundary_tolerance_cpm,
        "episode": {
            "effective_chars": episode_chars,
            "spoken_duration_s": round(episode_duration, 6),
            "effective_cpm": round(episode_cpm, 3),
        },
        "global_retime_factor": global_retime,
        "global_retime_preferred_range": list(GLOBAL_RETIME_PREFERRED_RANGE),
        "global_retime_hard_range": list(GLOBAL_RETIME_HARD_RANGE),
        "scene_retime_consistency": {
            "all_match_global": all_match_global,
            "max_adjacent_delta": MAX_ADJACENT_RETIME_DELTA,
            "adjacent_deltas": adjacent_deltas,
        },
        "scenes": rows,
        "diagnostics": diagnostics,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--min-cpm", type=float)
    parser.add_argument("--max-cpm", type=float)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", type=Path, help="resolved profile JSON")
    args = parser.parse_args()

    timeline = json.loads(args.timeline.expanduser().resolve().read_text(encoding="utf-8"))
    profile, _ = load_resolved_profile(args.profile, None, required=args.profile is not None)
    configured_range = timeline.get("allowed_cpm_range") or get_in(
        profile, "voice.allowed_range", [0.0, float("inf")]
    )
    min_cpm = float(args.min_cpm if args.min_cpm is not None else configured_range[0])
    max_cpm = float(args.max_cpm if args.max_cpm is not None else configured_range[1])
    if timeline.get("generation_mode") == CONTINUOUS_MODE:
        report = continuous_report(timeline, min_cpm, max_cpm)
        payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            target = args.output.expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload, encoding="utf-8")
        print(payload, end="")
        return 0 if not report["errors"] else 1

    # A few milliseconds of FFmpeg retime/PCM rounding can move a boundary
    # scene by a fraction of a CPM; do not reject an otherwise stable take for
    # that encoding noise.
    boundary_tolerance_cpm = 0.25
    stability_profile = timeline.get("stability_profile") or {}
    retime_range = stability_profile.get("retime_factor_range")
    rows = []
    errors = []
    for scene in timeline.get("scenes", []):
        count = int(scene.get("effective_chars") or effective_chars(str(scene.get("text", ""))))
        duration = float(scene.get("spoken_duration_s") or scene.get("duration_s") or 0.0)
        cpm = count * 60 / duration if duration > 0 else 0.0
        row = {
            "id": scene.get("id"),
            "effective_chars": count,
            "spoken_duration_s": round(duration, 6),
            "effective_cpm": round(cpm, 3),
            "retime_factor": scene.get("retime_factor"),
        }
        rows.append(row)
        if not min_cpm - boundary_tolerance_cpm <= cpm <= max_cpm + boundary_tolerance_cpm:
            errors.append(f"{scene.get('id')}: {cpm:.3f} CPM outside {min_cpm:.0f}-{max_cpm:.0f}")
        if retime_range and scene.get("retime_factor") is not None:
            retime = float(scene["retime_factor"])
            if not float(retime_range[0]) <= retime <= float(retime_range[1]):
                errors.append(
                    f"{scene.get('id')}: retime {retime:.3f} outside "
                    f"{float(retime_range[0]):.2f}-{float(retime_range[1]):.2f}"
                )

    report = {
        "status": "pass" if not errors else "fail",
        "allowed_range": [min_cpm, max_cpm],
        "boundary_tolerance_cpm": boundary_tolerance_cpm,
        "retime_factor_range": retime_range,
        "scenes": rows,
        "errors": errors,
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        target = args.output.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
