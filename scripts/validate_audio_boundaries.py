#!/usr/bin/env python3
"""Validate narration loudness and every inter-scene audio boundary."""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def resolve_path(project: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (project / path).resolve()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def media_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def loudness(
    path: Path,
    *,
    start_s: float | None = None,
    duration_s: float | None = None,
) -> dict[str, float]:
    input_args = ["-i", str(path)]
    if start_s is not None:
        input_args = ["-ss", f"{start_s:.9f}", *input_args]
    if duration_s is not None:
        input_args.extend(["-t", f"{duration_s:.9f}"])
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            *input_args,
            "-filter:a",
            "loudnorm=I=-16:TP=-2:LRA=6:print_format=json",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    matches = re.findall(r"\{\s*\"input_i\".*?\}", result.stderr, flags=re.DOTALL)
    if not matches:
        raise RuntimeError(f"ffmpeg loudnorm did not return JSON for {path}")
    values = json.loads(matches[-1])
    return {
        "integrated_lufs": float(values["input_i"]),
        "true_peak_dbtp": float(values["input_tp"]),
        "lra_lu": float(values["input_lra"]),
    }


def decode_mono_s16(
    path: Path,
    sample_rate: int,
    *,
    start_s: float | None = None,
    duration_s: float | None = None,
) -> array.array[int]:
    input_args = ["-i", str(path)]
    if start_s is not None:
        input_args = ["-ss", f"{start_s:.9f}", *input_args]
    if duration_s is not None:
        input_args.extend(["-t", f"{duration_s:.9f}"])
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            *input_args,
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-f",
            "s16le",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    samples: array.array[int] = array.array("h")
    samples.frombytes(result.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def power_to_db(power: float) -> float:
    if power <= 0:
        return -120.0
    return 10.0 * math.log10(power)


def mean_frame_db(frames: list[dict[str, float | int]]) -> float:
    if not frames:
        return -120.0
    power = sum(10.0 ** (float(frame["db"]) / 10.0) for frame in frames) / len(frames)
    return power_to_db(power)


def envelope_metrics(
    path: Path,
    sample_rate: int,
    gate_db: float,
    *,
    start_s: float | None = None,
    duration_s: float | None = None,
) -> dict[str, float]:
    samples = decode_mono_s16(
        path,
        sample_rate,
        start_s=start_s,
        duration_s=duration_s,
    )
    frame_size = max(1, round(sample_rate * 0.02))
    frames: list[dict[str, float | int]] = []
    peak = 0
    full_scale_sq = float(32768 * 32768)
    for start in range(0, len(samples), frame_size):
        stop = min(len(samples), start + frame_size)
        if stop <= start:
            continue
        square_sum = 0.0
        for value in samples[start:stop]:
            absolute = abs(value)
            if absolute > peak:
                peak = absolute
            square_sum += float(value) * float(value)
        power = square_sum / (stop - start) / full_scale_sq
        frames.append({"start": start, "stop": stop, "db": power_to_db(power)})

    active = [frame for frame in frames if float(frame["db"]) >= gate_db]
    if not active:
        return {
            "active_rms_dbfs": -120.0,
            "onset_rms_dbfs": -120.0,
            "tail_rms_dbfs": -120.0,
            "early_rms_dbfs": -120.0,
            "peak_dbfs": power_to_db((peak / 32768.0) ** 2),
            "speech_start_s": 0.0,
            "speech_end_s": 0.0,
        }

    speech_start = int(active[0]["start"])
    speech_end = int(active[-1]["stop"])
    onset_stop = speech_start + round(sample_rate * 0.40)
    early_stop = speech_start + round(sample_rate * 1.00)
    tail_start = speech_end - round(sample_rate * 0.40)
    onset = [frame for frame in active if int(frame["start"]) < onset_stop]
    early = [frame for frame in active if int(frame["start"]) < early_stop]
    tail = [frame for frame in active if int(frame["stop"]) > tail_start]
    return {
        "active_rms_dbfs": round(mean_frame_db(active), 3),
        "onset_rms_dbfs": round(mean_frame_db(onset), 3),
        "tail_rms_dbfs": round(mean_frame_db(tail), 3),
        "early_rms_dbfs": round(mean_frame_db(early), 3),
        "peak_dbfs": round(power_to_db((peak / 32768.0) ** 2), 3),
        "speech_start_s": round(speech_start / sample_rate, 6),
        "speech_end_s": round(speech_end / sample_rate, 6),
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.part")
    partial.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--timeline", default="audio/timeline.json")
    parser.add_argument("--master", default=None)
    parser.add_argument("--report", default="audio/boundary-qc.json")
    parser.add_argument("--gap-min", type=float, default=0.12)
    parser.add_argument("--gap-max", type=float, default=0.20)
    parser.add_argument("--max-scene-lufs-delta", type=float, default=1.5)
    parser.add_argument("--max-boundary-rms-delta", type=float, default=6.0)
    parser.add_argument("--max-onset-rms-delta", type=float, default=8.0)
    parser.add_argument("--gate-db", type=float, default=-46.0)
    args = parser.parse_args()

    project = args.project.expanduser().resolve()
    timeline_path = resolve_path(project, args.timeline)
    report_path = resolve_path(project, args.report)
    errors: list[str] = []
    warnings: list[str] = []
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    sample_rate = int(timeline.get("sample_rate", 48000))
    master_path = resolve_path(project, args.master or timeline["audio"])
    continuous_take = timeline.get("generation_mode") == "continuous_episode_take"

    scene_rows: list[dict[str, Any]] = []
    for scene in timeline.get("scenes", []):
        start_s = float(scene["start_s"])
        end_s = float(scene["end_s"])
        duration_s = end_s - start_s
        if continuous_take:
            if not master_path.is_file():
                continue
            scene_path = master_path
            scene_loudness = loudness(
                master_path,
                start_s=start_s,
                duration_s=duration_s,
            )
            envelope = envelope_metrics(
                master_path,
                sample_rate,
                args.gate_db,
                start_s=start_s,
                duration_s=duration_s,
            )
        else:
            scene_path = resolve_path(project, scene["path"])
            if not scene_path.is_file():
                errors.append(f"missing scene audio: {scene_path}")
                continue
            scene_loudness = loudness(scene_path)
            envelope = envelope_metrics(scene_path, sample_rate, args.gate_db)
        row = {
            "id": scene["id"],
            "path": str(scene_path),
            "start_s": start_s,
            "end_s": end_s,
            "duration_s": float(scene["duration_s"]),
            **scene_loudness,
            **envelope,
        }
        if continuous_take:
            row["analysis_source"] = "master_slice"
        scene_rows.append(row)
        if envelope["active_rms_dbfs"] <= -119:
            errors.append(f"{scene['id']}: no speech detected above {args.gate_db:.1f} dBFS")
        if scene_loudness["true_peak_dbtp"] > -1.8:
            errors.append(
                f"{scene['id']}: true peak {scene_loudness['true_peak_dbtp']:.2f} dBTP exceeds -1.8 dBTP"
            )
        onset_delta = envelope["onset_rms_dbfs"] - envelope["early_rms_dbfs"]
        if abs(onset_delta) > args.max_onset_rms_delta:
            errors.append(
                f"{scene['id']}: onset changes {onset_delta:+.2f} dB inside the first second; "
                f"limit is ±{args.max_onset_rms_delta:.1f} dB"
            )

    boundaries: list[dict[str, Any]] = []
    for index in range(min(len(scene_rows), len(timeline.get("scenes", []))) - 1):
        current = scene_rows[index]
        following = scene_rows[index + 1]
        current_meta = timeline["scenes"][index]
        gap = float(following["start_s"]) - float(current["end_s"])
        intentional_pause = bool(current_meta.get("intentional_audio_pause", False)) or (
            current_meta.get("intentional_audio_pause_s") is not None
        )
        scene_lufs_delta = float(following["integrated_lufs"]) - float(current["integrated_lufs"])
        boundary_rms_delta = float(following["onset_rms_dbfs"]) - float(current["tail_rms_dbfs"])
        boundary = {
            "from": current["id"],
            "to": following["id"],
            "gap_s": round(gap, 6),
            "intentional_pause": intentional_pause,
            "scene_lufs_delta": round(scene_lufs_delta, 3),
            "boundary_rms_delta": round(boundary_rms_delta, 3),
            "status": "pass",
        }
        boundary_errors: list[str] = []
        if continuous_take:
            # A continuous episode has one uninterrupted master WAV.  Scene
            # timestamps are analytical caption/visual cut points, not audio
            # files concatenated with an editor-imposed pause.  Requiring the
            # legacy 120–200 ms splice gap here creates false failures for a
            # perfectly natural 80 ms punctuation pause.  Only overlapping
            # scene ranges are unsafe; retain the measured boundary RMS/LUFS
            # checks below for audible jumps.
            if gap < -0.001:
                boundary_errors.append(
                    f"continuous scene ranges overlap by {-gap:.3f}s"
                )
            boundary["gap_policy"] = "continuous_master_diagnostic"
        elif not intentional_pause and not (args.gap_min - 0.001 <= gap <= args.gap_max + 0.001):
            boundary_errors.append(
                f"gap {gap:.3f}s outside {args.gap_min:.2f}-{args.gap_max:.2f}s"
            )
        if abs(scene_lufs_delta) > args.max_scene_lufs_delta:
            boundary_errors.append(
                f"adjacent scene loudness changes {scene_lufs_delta:+.2f} LU; "
                f"limit is ±{args.max_scene_lufs_delta:.1f} LU"
            )
        if abs(boundary_rms_delta) > args.max_boundary_rms_delta:
            boundary_errors.append(
                f"speech boundary changes {boundary_rms_delta:+.2f} dB; "
                f"limit is ±{args.max_boundary_rms_delta:.1f} dB"
            )
        if boundary_errors:
            boundary["status"] = "fail"
            boundary["errors"] = boundary_errors
            errors.extend(f"{current['id']} → {following['id']}: {message}" for message in boundary_errors)
        boundaries.append(boundary)

    master_stats: dict[str, Any] = {}
    if not master_path.is_file():
        errors.append(f"missing master audio: {master_path}")
    else:
        master_stats = {
            "path": str(master_path),
            "sha256": sha256(master_path),
            "duration_s": round(media_duration(master_path), 6),
            **loudness(master_path),
        }
        expected_duration = float(timeline.get("total_duration_s", 0.0))
        if abs(master_stats["duration_s"] - expected_duration) > 0.02:
            errors.append(
                f"master duration {master_stats['duration_s']:.6f}s differs from timeline "
                f"{expected_duration:.6f}s"
            )
        if not (-17.5 <= master_stats["integrated_lufs"] <= -14.5):
            errors.append(
                f"master loudness {master_stats['integrated_lufs']:.2f} LUFS outside -17.5 to -14.5 LUFS"
            )
        if master_stats["true_peak_dbtp"] > -1.8:
            errors.append(
                f"master true peak {master_stats['true_peak_dbtp']:.2f} dBTP exceeds -1.8 dBTP"
            )
        if master_stats["lra_lu"] > 8.0:
            warnings.append(f"master LRA {master_stats['lra_lu']:.2f} LU is wider than 8 LU")

    report = {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "pass" if not errors else "fail",
        "thresholds": {
            "gap_range_s": [args.gap_min, args.gap_max],
            "max_adjacent_scene_lufs_delta": args.max_scene_lufs_delta,
            "max_boundary_rms_delta_db": args.max_boundary_rms_delta,
            "max_onset_rms_delta_db": args.max_onset_rms_delta,
            "master_integrated_lufs_range": [-17.5, -14.5],
            "true_peak_ceiling_dbtp": -1.8,
            "speech_gate_dbfs": args.gate_db,
        },
        "master": master_stats,
        "scenes": scene_rows,
        "boundaries": boundaries,
        "errors": errors,
        "warnings": warnings,
    }
    write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
