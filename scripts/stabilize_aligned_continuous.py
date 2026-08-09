#!/usr/bin/env python3
"""Apply a bounded post-alignment gain rider to a continuous episode master.

The VoxCPM2 take is generated and measured-normalized before forced alignment.
Forced alignment then exposes the actual caption and scene boundaries.  This
small remediation pass smooths only local level changes at those boundaries;
it never retimes audio, changes pitch, or runs a second dynamic loudnorm.  A
static target gain plus a true-peak limiter preserves the one measured master
normalization contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

import numpy as np
import soundfile as sf


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def db_rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return -120.0
    value = float(np.sqrt(np.mean(np.asarray(samples, dtype=np.float64) ** 2)))
    return 20.0 * math.log10(max(value, 1e-9))


def active_power_mean_samples(
    samples: np.ndarray,
    sample_rate: int,
    start_s: float,
    end_s: float,
    gate_db: float = -32.0,
    frame_s: float = 0.04,
    hop_s: float = 0.02,
) -> float | None:
    """Match the voice QC boundary meter (40 ms frames, 20 ms hop)."""
    frame = max(2, round(sample_rate * frame_s))
    hop = max(1, round(sample_rate * hop_s))
    start = max(0, round(start_s * sample_rate))
    stop = min(samples.size, round(end_s * sample_rate))
    if stop - start < frame:
        return None
    levels: list[float] = []
    for offset in range(start, stop - frame + 1, hop):
        level = db_rms(samples[offset : offset + frame])
        if level >= gate_db:
            levels.append(level)
    if not levels:
        return None
    return 10.0 * math.log10(float(np.mean(10.0 ** (np.asarray(levels) / 10.0))) + 1e-15)


def active_median_samples(
    samples: np.ndarray,
    sample_rate: int,
    start_s: float,
    end_s: float,
    gate_db: float = -32.0,
    frame_s: float = 0.04,
    hop_s: float = 0.02,
) -> float | None:
    """Match the voice QC caption meter (active-frame median)."""
    frame = max(2, round(sample_rate * frame_s))
    hop = max(1, round(sample_rate * hop_s))
    start = max(0, round(start_s * sample_rate))
    stop = min(samples.size, round(end_s * sample_rate))
    levels = [
        db_rms(samples[offset : offset + frame])
        for offset in range(start, max(start, stop - frame + 1), hop)
    ]
    active = [value for value in levels if value >= gate_db]
    return float(np.median(np.asarray(active))) if active else None


def parse_loudnorm(stderr: str) -> dict[str, Any]:
    matches = re.findall(r'\{\s*"input_i".*?\}', stderr, flags=re.DOTALL)
    if not matches:
        raise RuntimeError("ffmpeg loudnorm did not return JSON")
    return json.loads(matches[-1])


def measure_ebu(path: Path) -> dict[str, float]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
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
    data = parse_loudnorm(result.stderr)
    return {
        "integrated_lufs": float(data["input_i"]),
        "true_peak_dbtp": float(data["input_tp"]),
        "lra_lu": float(data["input_lra"]),
    }


def smoothstep(value: np.ndarray) -> np.ndarray:
    return value * value * (3.0 - 2.0 * value)


def apply_rider(
    samples: np.ndarray,
    sample_rate: int,
    gate_dbfs: float,
    *,
    window_s: float = 0.5,
    hop_s: float = 0.1,
    deadband_db: float = 0.0,
    gain_limit_db: float = 3.0,
    attack_s: float = 0.2,
    release_s: float = 0.6,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Smooth local speech envelope without changing pitch or timing."""
    window = max(2, round(sample_rate * window_s))
    hop = max(1, round(sample_rate * hop_s))
    times: list[float] = []
    levels: list[float] = []
    for start in range(0, max(0, samples.size - window + 1), hop):
        level = db_rms(samples[start : start + window])
        if level >= gate_dbfs:
            times.append((start + window / 2) / sample_rate)
            levels.append(level)
    if len(levels) < 3:
        raise RuntimeError("not enough speech-active windows for aligned gain rider")

    target = float(np.median(np.asarray(levels)))
    desired: list[float] = []
    for level in levels:
        correction = target - level
        if abs(correction) <= deadband_db:
            correction = 0.0
        else:
            correction -= math.copysign(deadband_db, correction)
        desired.append(float(np.clip(correction, -gain_limit_db, gain_limit_db)))

    smoothed: list[float] = []
    current = 0.0
    for wanted in desired:
        tau = attack_s if wanted < current else release_s
        alpha = 1.0 - math.exp(-hop_s / max(tau, 1e-3))
        current += alpha * (wanted - current)
        smoothed.append(float(np.clip(current, -gain_limit_db, gain_limit_db)))

    gain_db = np.interp(
        np.arange(samples.size, dtype=np.float64),
        np.asarray(times, dtype=np.float64) * sample_rate,
        np.asarray(smoothed, dtype=np.float64),
        left=smoothed[0],
        right=smoothed[-1],
    )
    output = samples * np.power(10.0, gain_db / 20.0).astype(np.float32)
    return output, {
        "method": "caption_window_slow_gain_rider",
        "window_s": window_s,
        "hop_s": hop_s,
        "deadband_db": deadband_db,
        "gain_limit_db": gain_limit_db,
        "attack_ms": round(attack_s * 1000),
        "release_ms": round(release_s * 1000),
        "target_active_rms_dbfs": round(target, 3),
        "max_abs_smoothed_gain_db": round(max(abs(value) for value in smoothed), 3),
        "active_window_count": len(levels),
    }


def apply_boundary_corrections(
    samples: np.ndarray,
    sample_rate: int,
    timeline: dict[str, Any],
    *,
    target_delta_db: float = 1.0,
    # Keep the boundary rider within the same ±3 dB gain contract as the
    # caption-window rider. A 2 dB cap can leave an audible residual jump
    # when a scene tail is intentionally quiet and the following onset is
    # speech-active.
    max_adjust_db: float = 3.0,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """Gently match scene onset/tail energy in a continuous master."""
    output = samples.copy()
    corrections: list[dict[str, Any]] = []
    scenes = list(timeline.get("scenes", []))
    for previous, following in zip(scenes, scenes[1:]):
        tail_start = max(0, round((float(previous["end_s"]) - 0.4) * sample_rate))
        tail_end = min(output.size, round(float(previous["end_s"]) * sample_rate))
        onset_start = max(0, round(float(following["start_s"]) * sample_rate))
        onset_end = min(output.size, round((float(following["start_s"]) + 0.4) * sample_rate))
        tail = active_power_mean_samples(
            output, sample_rate, float(previous["end_s"]) - 0.4, float(previous["end_s"])
        )
        onset = active_power_mean_samples(
            output, sample_rate, float(following["start_s"]), float(following["start_s"]) + 0.4
        )
        if tail is None or onset is None:
            corrections.append({
                "from": previous.get("id"),
                "to": following.get("id"),
                "before_delta_db": None,
                "adjustment_db": 0.0,
                "applied": False,
                "reason": "no_speech_active_boundary_frames",
            })
            continue
        delta = onset - tail
        excess = math.copysign(max(0.0, abs(delta) - target_delta_db), delta)
        adjustment = float(np.clip(-excess, -max_adjust_db, max_adjust_db))
        row = {
            "from": previous.get("id"),
            "to": following.get("id"),
            "before_delta_db": round(delta, 3),
            "adjustment_db": round(adjustment, 3),
        }
        if abs(adjustment) < 0.05:
            row["applied"] = False
            corrections.append(row)
            continue

        start = max(0, round(float(following["start_s"]) * sample_rate))
        attack_end = min(output.size, round((float(following["start_s"]) + 0.12) * sample_rate))
        hold_end = min(output.size, round((float(following["start_s"]) + 0.48) * sample_rate))
        release_end = min(output.size, round((float(following["start_s"]) + 0.65) * sample_rate))
        if start < attack_end:
            curve = smoothstep(np.linspace(0.0, 1.0, attack_end - start, endpoint=False))
            output[start:attack_end] *= np.power(10.0, adjustment * curve / 20.0).astype(np.float32)
        if attack_end < hold_end:
            output[attack_end:hold_end] *= 10.0 ** (adjustment / 20.0)
        if hold_end < release_end:
            curve = smoothstep(np.linspace(1.0, 0.0, release_end - hold_end, endpoint=False))
            output[hold_end:release_end] *= np.power(10.0, adjustment * curve / 20.0).astype(np.float32)
        # A quiet scene tail can make a following onset sound like an
        # explosion even after the onset is attenuated. Share part of the
        # correction with the tail, using the same short, seek-safe envelope;
        # this keeps the total local rider within the ±3 dB contract while
        # avoiding a hard boundary step.
        tail_adjustment = float(np.clip(-adjustment * 0.75, -max_adjust_db, max_adjust_db))
        tail_attack_start = tail_start
        tail_attack_end = min(tail_end, tail_attack_start + round(0.12 * sample_rate))
        tail_hold_start = min(tail_end, tail_attack_end)
        tail_hold_end = max(tail_hold_start, tail_end - round(0.12 * sample_rate))
        if tail_attack_start < tail_attack_end:
            curve = smoothstep(np.linspace(0.0, 1.0, tail_attack_end - tail_attack_start, endpoint=False))
            output[tail_attack_start:tail_attack_end] *= np.power(10.0, tail_adjustment * curve / 20.0).astype(np.float32)
        if tail_hold_start < tail_hold_end:
            output[tail_hold_start:tail_hold_end] *= 10.0 ** (tail_adjustment / 20.0)
        if tail_hold_end < tail_end:
            curve = smoothstep(np.linspace(1.0, 0.0, tail_end - tail_hold_end, endpoint=False))
            output[tail_hold_end:tail_end] *= np.power(10.0, tail_adjustment * curve / 20.0).astype(np.float32)
        row["tail_adjustment_db"] = round(tail_adjustment, 3)
        row["applied"] = True
        corrections.append(row)
    return output, corrections


def apply_caption_smoothing(
    samples: np.ndarray,
    sample_rate: int,
    captions: list[dict[str, Any]],
    gate_db: float,
    *,
    max_adjacent_step_db: float = 1.2,
    gain_limit_db: float = 2.6,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Project aligned caption levels onto one continuous safe gain curve.

    The previous window-only rider could leave a short phrase 2–3 dB away
    from its neighbour, requiring repeated full passes.  This projection uses
    the validator's 40ms/20ms speech-active meter and corrects all captions in
    the same single pass without creating per-caption gain discontinuities.
    """
    centers: list[float] = []
    levels: list[float] = []
    for row in captions:
        start = float(row.get("start", 0.0))
        end = float(row.get("end", 0.0))
        level = active_median_samples(samples, sample_rate, start, end, gate_db)
        if level is None:
            continue
        centers.append((start + end) / 2.0)
        levels.append(level)
    if len(levels) < 2:
        return samples, {
            "method": "aligned_caption_continuous_gain_curve",
            "caption_count": len(levels),
            "max_abs_gain_db": 0.0,
            "applied": False,
        }
    target = np.asarray(levels, dtype=np.float64)
    for _ in range(8):
        for index in range(1, target.size):
            target[index] = np.clip(
                target[index],
                target[index - 1] - max_adjacent_step_db,
                target[index - 1] + max_adjacent_step_db,
            )
        for index in range(target.size - 2, -1, -1):
            target[index] = np.clip(
                target[index],
                target[index + 1] - max_adjacent_step_db,
                target[index + 1] + max_adjacent_step_db,
            )
    corrections = np.clip(
        target - np.asarray(levels, dtype=np.float64), -gain_limit_db, gain_limit_db
    )
    sample_times = np.arange(samples.size, dtype=np.float64) / sample_rate
    control_times = np.concatenate(([0.0], np.asarray(centers), [samples.size / sample_rate]))
    control_gain = np.concatenate(([corrections[0]], corrections, [corrections[-1]]))
    gain_db = np.interp(sample_times, control_times, control_gain)
    output = samples * np.power(10.0, gain_db / 20.0).astype(np.float32)
    return output, {
        "method": "aligned_caption_continuous_gain_curve",
        "caption_count": len(levels),
        "max_adjacent_step_target_db": max_adjacent_step_db,
        "gain_limit_db": gain_limit_db,
        "max_abs_gain_db": round(float(np.max(np.abs(corrections))), 3),
        "applied": bool(np.max(np.abs(corrections)) >= 0.01),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--audio", default="audio/output/narration_master.wav")
    parser.add_argument("--output", default=None)
    parser.add_argument("--timeline", default="audio/timeline.json")
    parser.add_argument("--captions", default="audio/caption-groups.json")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--report", default="audio/aligned-stabilization.json")
    parser.add_argument("--target-lufs", type=float, default=-16.0)
    args = parser.parse_args()

    project = args.project.expanduser().resolve()
    source = Path(args.audio).expanduser()
    source = source if source.is_absolute() else project / source
    target = Path(args.output).expanduser() if args.output else source
    target = target if target.is_absolute() else project / target
    timeline_path = Path(args.timeline).expanduser()
    timeline_path = timeline_path if timeline_path.is_absolute() else project / timeline_path
    captions_path = Path(args.captions).expanduser()
    captions_path = captions_path if captions_path.is_absolute() else project / captions_path
    profile_path = Path(args.profile).expanduser() if args.profile else (
        Path(__file__).resolve().parents[1] / "references/voice-stability-profile.json"
    )
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    caption_doc = json.loads(captions_path.read_text(encoding="utf-8"))
    if timeline.get("generation_mode") != "continuous_episode_take":
        raise RuntimeError("aligned stabilization requires generation_mode=continuous_episode_take")
    if str(timeline.get("alignment_status", "")) != "forced_aligned":
        raise RuntimeError("forced alignment must pass before aligned stabilization")
    if not source.is_file():
        raise FileNotFoundError(source)
    if not captions_path.is_file():
        raise FileNotFoundError(captions_path)

    samples, sample_rate = sf.read(source, dtype="float32")
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    gate = float(profile["analysis"]["speech_gate_dbfs"])
    smoothed, rider = apply_rider(samples, sample_rate, gate)
    caption_smoothed, caption_smoothing = apply_caption_smoothing(
        smoothed, sample_rate, list(caption_doc.get("groups", [])), gate
    )
    corrected, boundary_corrections = apply_boundary_corrections(
        caption_smoothed, sample_rate, timeline
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=".aligned-stabilization-", dir=target.parent))
    pre_path = temp_dir / "post-rider.wav"
    final_path = temp_dir / "final.wav"
    try:
        sf.write(pre_path, corrected, sample_rate, subtype="PCM_24")
        measured = measure_ebu(pre_path)
        global_gain_db = float(np.clip(args.target_lufs - measured["integrated_lufs"], -1.0, 1.0))
        # Leave 0.2 dB of headroom for true-peak overshoot after PCM export.
        true_peak_limit = 10.0 ** (-2.2 / 20.0)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(pre_path),
                "-filter:a",
                f"volume={global_gain_db:.6f}dB,alimiter=limit={true_peak_limit:.6f}:attack=5:release=100:level=disabled",
                "-ar",
                str(sample_rate),
                "-ac",
                "1",
                "-c:a",
                "pcm_s24le",
                str(final_path),
            ],
            check=True,
        )
        # Replace atomically even when source == target.
        final_path.replace(target)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    output_stats = measure_ebu(target)
    report = {
        "schema_version": 1,
        "status": "pass",
        "method": "post_alignment_bounded_gain_rider",
        "source_path": str(source),
        "source_sha256": sha256(source) if source.resolve() != target.resolve() else None,
        "output_path": str(target),
        "output_sha256": sha256(target),
        "sample_rate": sample_rate,
        "duration_s": round(samples.size / sample_rate, 6),
        "rider": rider,
        "caption_smoothing": caption_smoothing,
        "boundary_corrections": boundary_corrections,
        "static_target_gain_db": round(global_gain_db, 3),
        "true_peak_limiter": {"limit": round(true_peak_limit, 6), "ceiling_dbtp": -2.2},
        "measured_before_static_gain": measured,
        "measured_output": output_stats,
        "timeline_sha256": sha256(timeline_path),
        "captions_sha256": sha256(captions_path),
        "profile_sha256": sha256(profile_path),
    }
    report_path = Path(args.report).expanduser()
    report_path = report_path if report_path.is_absolute() else project / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
