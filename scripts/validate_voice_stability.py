#!/usr/bin/env python3
"""Validate short-time voice-state stability before captions and rendering.

The existing boundary validator measures integrated loudness and scene edges.
This gate measures the perceptual failures that can remain after loudnorm:
slow-window loudness, baseline pitch/register, spectral brightness, clause
level energy, scene boundaries, and adjacent retime-factor changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


def resolve_path(project: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (project / path).resolve()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def decode_audio(path: Path, sample_rate: int, start_offset_s: float = 0.0) -> np.ndarray:
    seek = ["-ss", f"{start_offset_s:.6f}"] if start_offset_s > 0 else []
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            *seek,
            "-i",
            str(path),
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-f",
            "f32le",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    return np.frombuffer(result.stdout, dtype="<f4").copy()


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


def ebu_loudness(path: Path, start_offset_s: float = 0.0) -> dict[str, float]:
    seek = ["-ss", f"{start_offset_s:.6f}"] if start_offset_s > 0 else []
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            *seek,
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
    matches = re.findall(r'\{\s*"input_i".*?\}', result.stderr, flags=re.DOTALL)
    if not matches:
        raise RuntimeError(f"ffmpeg loudnorm did not return JSON for {path}")
    values = json.loads(matches[-1])
    return {
        "integrated_lufs": float(values["input_i"]),
        "true_peak_dbtp": float(values["input_tp"]),
        "lra_lu": float(values["input_lra"]),
    }


def db_rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return -120.0
    rms = math.sqrt(float(np.mean(samples.astype(np.float64) ** 2)) + 1e-15)
    return 20.0 * math.log10(rms + 1e-12)


def framed_features(
    samples: np.ndarray,
    sample_rate: int,
    frame_s: float,
    hop_s: float,
    gate_db: float,
) -> dict[str, np.ndarray]:
    frame_size = max(2, round(sample_rate * frame_s))
    hop_size = max(1, round(sample_rate * hop_s))
    if samples.size < frame_size:
        raise ValueError("audio is shorter than one analysis frame")
    frames = np.lib.stride_tricks.sliding_window_view(samples, frame_size)[::hop_size]
    frames64 = frames.astype(np.float64)
    frames64 -= frames64.mean(axis=1, keepdims=True)
    rms = np.sqrt(np.mean(frames64**2, axis=1) + 1e-15)
    rms_db = 20.0 * np.log10(rms + 1e-12)
    times = (np.arange(frames64.shape[0]) * hop_size + frame_size / 2) / sample_rate

    window = np.hanning(frame_size)
    spectrum = np.abs(np.fft.rfft(frames64 * window, axis=1))
    frequencies = np.fft.rfftfreq(frame_size, d=1.0 / sample_rate)
    centroid_band = (frequencies >= 80.0) & (frequencies <= 5000.0)
    centroid = (
        (spectrum[:, centroid_band] * frequencies[centroid_band]).sum(axis=1)
        / (spectrum[:, centroid_band].sum(axis=1) + 1e-12)
    )

    f0 = np.full(frames64.shape[0], np.nan, dtype=np.float64)
    min_lag = max(1, round(sample_rate / 350.0))
    max_lag = min(frame_size - 2, round(sample_rate / 65.0))
    for index, frame in enumerate(frames64):
        if rms_db[index] < gate_db:
            continue
        autocorrelation = np.correlate(frame, frame, mode="full")[frame_size - 1 : frame_size + max_lag]
        if autocorrelation[0] <= 1e-12:
            continue
        candidates = autocorrelation[min_lag : max_lag + 1] / autocorrelation[0]
        best = int(np.argmax(candidates))
        if float(candidates[best]) >= 0.28:
            f0[index] = sample_rate / float(min_lag + best)
    return {
        "time_s": times,
        "rms_dbfs": rms_db,
        "centroid_hz": centroid,
        "f0_hz": f0,
    }


def semitone_delta(a: float, b: float) -> float:
    if a <= 0 or b <= 0:
        return 0.0
    return abs(12.0 * math.log2(b / a))


def percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q)) if values else 0.0


def nearest_caption(captions: list[dict[str, Any]], timestamp: float) -> str:
    if not captions:
        return ""
    row = min(
        captions,
        key=lambda item: abs((float(item.get("start", 0.0)) + float(item.get("end", 0.0))) / 2 - timestamp),
    )
    return str(row.get("text", ""))


def active_median(
    features: dict[str, np.ndarray],
    start: float,
    end: float,
    gate_db: float,
) -> float | None:
    times = features["time_s"]
    rms = features["rms_dbfs"]
    mask = (times >= start) & (times <= end) & (rms >= gate_db)
    return float(np.median(rms[mask])) if np.any(mask) else None


def active_power_mean(
    features: dict[str, np.ndarray],
    start: float,
    end: float,
    gate_db: float,
) -> float | None:
    """Return a gated power mean for short boundary windows.

    A median over 0.8 seconds hid the actual +2 to +3 dB take changes in the
    reference episode. Power averaging over the final/first 0.4 seconds is
    deliberately sensitive to the audible hand-off while still excluding
    silence below the speech gate.
    """
    times = features["time_s"]
    rms = features["rms_dbfs"]
    mask = (times >= start) & (times <= end) & (rms >= gate_db)
    if not np.any(mask):
        return None
    power = np.mean(10.0 ** (rms[mask] / 10.0))
    return 10.0 * math.log10(float(power) + 1e-15)


def load_profile(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if int(data.get("schema_version", 0)) != 1:
        raise ValueError(f"unsupported voice-stability profile: {path}")
    return data


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial.replace(path)


def main() -> int:
    default_profile = (
        Path(__file__).resolve().parent.parent
        / "references"
        / "voice-stability-profile.json"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--audio", default="audio/output/narration_master.wav")
    parser.add_argument("--timeline", default="audio/timeline.json")
    parser.add_argument("--captions", default="audio/caption-groups.json")
    parser.add_argument("--prosody", default="audio/prosody.json")
    parser.add_argument("--voice-manifest", default="audio/voice-manifest.json")
    parser.add_argument("--report", default="audio/voice-stability-qc.json")
    parser.add_argument("--stage", choices=["raw", "master", "final"], default="master")
    parser.add_argument("--profile", type=Path, default=default_profile)
    parser.add_argument("--analysis-offset-s", type=float, default=0.0)
    parser.add_argument("--sample-rate", type=int)
    parser.add_argument("--speech-gate-db", type=float)
    parser.add_argument("--max-active-1s-spread-db", type=float)
    parser.add_argument("--max-caption-rms-delta-db", type=float)
    parser.add_argument("--max-boundary-rms-delta-db", type=float)
    parser.add_argument("--max-adjacent-retime-delta", type=float)
    parser.add_argument("--max-baseline-f0-step-st", type=float)
    parser.add_argument("--max-baseline-f0-p95-step-st", type=float)
    parser.add_argument("--max-baseline-centroid-step-st", type=float)
    parser.add_argument("--max-baseline-centroid-p95-step-st", type=float)
    parser.add_argument("--baseline-window-s", type=float)
    parser.add_argument("--baseline-hop-s", type=float)
    parser.add_argument("--boundary-window-s", type=float)
    args = parser.parse_args()

    project = args.project.expanduser().resolve()
    profile_path = args.profile.expanduser().resolve()
    profile = load_profile(profile_path)
    analysis_profile = profile["analysis"]
    limits = profile["hard_limits"]
    args.sample_rate = args.sample_rate or int(analysis_profile["sample_rate"])
    args.speech_gate_db = (
        args.speech_gate_db
        if args.speech_gate_db is not None
        else float(analysis_profile["speech_gate_dbfs"])
    )
    args.baseline_window_s = args.baseline_window_s or float(
        analysis_profile["baseline_window_s"]
    )
    args.baseline_hop_s = args.baseline_hop_s or float(analysis_profile["baseline_hop_s"])
    args.boundary_window_s = args.boundary_window_s or float(
        analysis_profile["boundary_window_s"]
    )
    args.max_active_1s_spread_db = (
        args.max_active_1s_spread_db
        if args.max_active_1s_spread_db is not None
        else float(limits["active_1s_p90_p10_db"])
    )
    args.max_caption_rms_delta_db = (
        args.max_caption_rms_delta_db
        if args.max_caption_rms_delta_db is not None
        else float(limits["adjacent_caption_rms_delta_db"])
    )
    args.max_boundary_rms_delta_db = (
        args.max_boundary_rms_delta_db
        if args.max_boundary_rms_delta_db is not None
        else float(limits["scene_boundary_rms_delta_db"])
    )
    args.max_adjacent_retime_delta = (
        args.max_adjacent_retime_delta
        if args.max_adjacent_retime_delta is not None
        else float(limits["adjacent_retime_factor_delta"])
    )
    args.max_baseline_f0_step_st = (
        args.max_baseline_f0_step_st
        if args.max_baseline_f0_step_st is not None
        else float(limits["baseline_f0_max_step_semitones"])
    )
    args.max_baseline_f0_p95_step_st = (
        args.max_baseline_f0_p95_step_st
        if args.max_baseline_f0_p95_step_st is not None
        else float(limits["baseline_f0_p95_step_semitones"])
    )
    args.max_baseline_centroid_step_st = (
        args.max_baseline_centroid_step_st
        if args.max_baseline_centroid_step_st is not None
        else float(limits["baseline_centroid_max_step_semitones"])
    )
    args.max_baseline_centroid_p95_step_st = (
        args.max_baseline_centroid_p95_step_st
        if args.max_baseline_centroid_p95_step_st is not None
        else float(limits["baseline_centroid_p95_step_semitones"])
    )
    audio_path = resolve_path(project, args.audio)
    timeline_path = resolve_path(project, args.timeline)
    captions_path = resolve_path(project, args.captions)
    prosody_path = resolve_path(project, args.prosody)
    voice_manifest_path = resolve_path(project, args.voice_manifest)
    report_path = resolve_path(project, args.report)
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    caption_doc = json.loads(captions_path.read_text(encoding="utf-8")) if captions_path.is_file() else {}
    captions = list(caption_doc.get("groups", []))
    samples = decode_audio(audio_path, args.sample_rate, args.analysis_offset_s)
    features = framed_features(samples, args.sample_rate, 0.04, 0.02, args.speech_gate_db)
    errors: list[str] = []
    warnings: list[str] = []

    voice_manifest: dict[str, Any] = {}
    if voice_manifest_path.is_file():
        voice_manifest = json.loads(voice_manifest_path.read_text(encoding="utf-8"))
    elif args.stage == "master":
        errors.append("missing audio/voice-manifest.json")
    if args.stage == "master":
        if not prosody_path.is_file():
            errors.append("missing audio/prosody.json")
        if str(timeline.get("alignment_status", "")).startswith("required"):
            errors.append("continuous take has not completed forced alignment")
        if voice_manifest:
            if voice_manifest.get("master_sha256") != sha256(audio_path):
                errors.append("voice manifest master_sha256 is stale")
            recorded_prosody = voice_manifest.get("prosody_sha256")
            if not recorded_prosody:
                errors.append("voice manifest does not bind prosody_sha256")
            elif prosody_path.is_file() and recorded_prosody != sha256(prosody_path):
                errors.append("voice manifest prosody_sha256 is stale")
            manifest_profile = voice_manifest.get("profile", {}).get("sha256")
            if not manifest_profile:
                errors.append("voice manifest does not bind the voice-stability profile")
            elif manifest_profile != sha256(profile_path):
                errors.append("voice manifest voice-stability profile hash is stale")
            if voice_manifest.get("clone_mode") == "voxcpm2_ultimate_cloning":
                for field in ("prompt", "reference"):
                    row = voice_manifest.get(field, {})
                    bound_path = Path(str(row.get("path", ""))).expanduser()
                    if not bound_path.is_file():
                        errors.append(f"voice manifest {field} file is missing")
                    elif row.get("sha256") != sha256(bound_path):
                        errors.append(f"voice manifest {field} hash is stale")
                prompt = voice_manifest.get("prompt", {})
                if not str(prompt.get("text", "")).strip():
                    errors.append("voice manifest does not bind exact prompt text")

    one_second_db: list[float] = []
    window_size = round(args.sample_rate * 1.0)
    hop_size = round(args.sample_rate * 0.1)
    for start in range(0, max(0, samples.size - window_size + 1), hop_size):
        value = db_rms(samples[start : start + window_size])
        if value >= args.speech_gate_db:
            one_second_db.append(value)
    active_1s_spread = percentile(one_second_db, 90) - percentile(one_second_db, 10)
    if active_1s_spread > args.max_active_1s_spread_db:
        errors.append(
            f"speech-active 1s loudness P90-P10 is {active_1s_spread:.2f} dB; "
            f"limit is {args.max_active_1s_spread_db:.2f} dB"
        )

    baseline_rows: list[dict[str, Any]] = []
    duration_s = samples.size / args.sample_rate
    for start in np.arange(0.0, max(0.0, duration_s - args.baseline_window_s), args.baseline_hop_s):
        end = float(start + args.baseline_window_s)
        mask = (
            (features["time_s"] >= start)
            & (features["time_s"] < end)
            & (features["rms_dbfs"] >= args.speech_gate_db)
        )
        voiced = mask & np.isfinite(features["f0_hz"])
        if int(np.count_nonzero(mask)) < 30 or int(np.count_nonzero(voiced)) < 20:
            continue
        center = float(start + args.baseline_window_s / 2)
        baseline_rows.append(
            {
                "center_s": round(center, 3),
                "f0_hz": round(float(np.median(features["f0_hz"][voiced])), 3),
                "centroid_hz": round(float(np.median(features["centroid_hz"][mask])), 3),
                "rms_dbfs": round(float(np.median(features["rms_dbfs"][mask])), 3),
                "near_caption": nearest_caption(captions, center),
            }
        )

    f0_steps: list[dict[str, Any]] = []
    centroid_steps: list[dict[str, Any]] = []
    for previous, current in zip(baseline_rows, baseline_rows[1:]):
        if float(current["center_s"]) - float(previous["center_s"]) > args.baseline_hop_s + 0.05:
            continue
        f0_delta = semitone_delta(float(previous["f0_hz"]), float(current["f0_hz"]))
        centroid_delta = semitone_delta(
            float(previous["centroid_hz"]), float(current["centroid_hz"])
        )
        common = {
            "at_s": current["center_s"],
            "near_caption": current["near_caption"],
        }
        f0_steps.append(
            {
                **common,
                "delta_st": round(f0_delta, 3),
                "from_hz": previous["f0_hz"],
                "to_hz": current["f0_hz"],
            }
        )
        centroid_steps.append(
            {
                **common,
                "delta_st": round(centroid_delta, 3),
                "from_hz": previous["centroid_hz"],
                "to_hz": current["centroid_hz"],
            }
        )

    f0_values = [float(row["delta_st"]) for row in f0_steps]
    centroid_values = [float(row["delta_st"]) for row in centroid_steps]
    max_f0_step = max(f0_values, default=0.0)
    p95_f0_step = percentile(f0_values, 95)
    max_centroid_step = max(centroid_values, default=0.0)
    p95_centroid_step = percentile(centroid_values, 95)
    if max_f0_step > args.max_baseline_f0_step_st:
        errors.append(
            f"baseline F0 step reaches {max_f0_step:.2f} semitones; "
            f"limit is {args.max_baseline_f0_step_st:.2f}"
        )
    if p95_f0_step > args.max_baseline_f0_p95_step_st:
        errors.append(
            f"baseline F0 step P95 is {p95_f0_step:.2f} semitones; "
            f"limit is {args.max_baseline_f0_p95_step_st:.2f}"
        )
    if max_centroid_step > args.max_baseline_centroid_step_st:
        errors.append(
            f"spectral-centroid step reaches {max_centroid_step:.2f} semitones; "
            f"limit is {args.max_baseline_centroid_step_st:.2f}"
        )
    if p95_centroid_step > args.max_baseline_centroid_p95_step_st:
        errors.append(
            f"spectral-centroid step P95 is {p95_centroid_step:.2f} semitones; "
            f"limit is {args.max_baseline_centroid_p95_step_st:.2f}"
        )

    caption_rows: list[dict[str, Any]] = []
    for caption in captions:
        median = active_median(
            features,
            float(caption.get("start", 0.0)),
            float(caption.get("end", 0.0)),
            args.speech_gate_db,
        )
        if median is not None:
            caption_rows.append(
                {
                    "id": caption.get("id"),
                    "start_s": float(caption.get("start", 0.0)),
                    "end_s": float(caption.get("end", 0.0)),
                    "text": str(caption.get("text", "")),
                    "active_median_rms_dbfs": round(median, 3),
                }
            )
    caption_steps: list[dict[str, Any]] = []
    for previous, current in zip(caption_rows, caption_rows[1:]):
        delta = float(current["active_median_rms_dbfs"]) - float(
            previous["active_median_rms_dbfs"]
        )
        caption_steps.append(
            {
                "at_s": current["start_s"],
                "delta_db": round(delta, 3),
                "from_text": previous["text"],
                "to_text": current["text"],
            }
        )
    max_caption_delta = max((abs(float(row["delta_db"])) for row in caption_steps), default=0.0)
    if max_caption_delta > args.max_caption_rms_delta_db:
        errors.append(
            f"adjacent caption RMS changes by {max_caption_delta:.2f} dB; "
            f"limit is {args.max_caption_rms_delta_db:.2f} dB"
        )

    scene_rows = list(timeline.get("scenes", []))
    boundaries: list[dict[str, Any]] = []
    retime_steps: list[dict[str, Any]] = []
    for previous, current in zip(scene_rows, scene_rows[1:]):
        previous_end = float(previous["end_s"])
        current_start = float(current["start_s"])
        tail = active_power_mean(
            features,
            max(float(previous["start_s"]), previous_end - args.boundary_window_s),
            previous_end,
            args.speech_gate_db,
        )
        onset = active_power_mean(
            features,
            current_start,
            min(float(current["end_s"]), current_start + args.boundary_window_s),
            args.speech_gate_db,
        )
        delta = (onset - tail) if tail is not None and onset is not None else 0.0
        boundaries.append(
            {
                "from": previous.get("id"),
                "to": current.get("id"),
                "at_s": round(current_start, 3),
                "tail_rms_dbfs": round(tail, 3) if tail is not None else None,
                "onset_rms_dbfs": round(onset, 3) if onset is not None else None,
                "delta_db": round(delta, 3),
            }
        )
        previous_retime = float(previous.get("retime_factor", 1.0))
        current_retime = float(current.get("retime_factor", 1.0))
        retime_steps.append(
            {
                "from": previous.get("id"),
                "to": current.get("id"),
                "from_factor": previous_retime,
                "to_factor": current_retime,
                "delta": round(current_retime - previous_retime, 6),
            }
        )
    max_boundary_delta = max((abs(float(row["delta_db"])) for row in boundaries), default=0.0)
    max_retime_delta = max((abs(float(row["delta"])) for row in retime_steps), default=0.0)
    if max_boundary_delta > args.max_boundary_rms_delta_db:
        errors.append(
            f"scene-boundary RMS changes by {max_boundary_delta:.2f} dB; "
            f"limit is {args.max_boundary_rms_delta_db:.2f} dB"
        )
    if max_retime_delta > args.max_adjacent_retime_delta:
        errors.append(
            f"adjacent retime factors differ by {max_retime_delta:.3f}; "
            f"limit is {args.max_adjacent_retime_delta:.3f}"
        )

    report = {
        "schema_version": 2,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "pass" if not errors else "fail",
        "stage": args.stage,
        "profile": {
            "path": str(profile_path),
            "sha256": sha256(profile_path),
            "profile_id": profile.get("profile_id"),
        },
        "inputs": {
            "timeline_path": str(timeline_path),
            "timeline_sha256": sha256(timeline_path),
            "captions_path": str(captions_path) if captions_path.is_file() else None,
            "captions_sha256": sha256(captions_path) if captions_path.is_file() else None,
            "prosody_path": str(prosody_path) if prosody_path.is_file() else None,
            "prosody_sha256": sha256(prosody_path) if prosody_path.is_file() else None,
            "voice_manifest_path": str(voice_manifest_path)
            if voice_manifest_path.is_file()
            else None,
            "voice_manifest_sha256": sha256(voice_manifest_path)
            if voice_manifest_path.is_file()
            else None,
            "analysis_offset_s": args.analysis_offset_s,
        },
        "audio": {
            "path": str(audio_path),
            "sha256": sha256(audio_path),
            "duration_s": round(max(0.0, media_duration(audio_path) - args.analysis_offset_s), 6),
            **ebu_loudness(audio_path, args.analysis_offset_s),
        },
        "thresholds": {
            "speech_gate_dbfs": args.speech_gate_db,
            "max_active_1s_p90_p10_db": args.max_active_1s_spread_db,
            "max_adjacent_caption_rms_delta_db": args.max_caption_rms_delta_db,
            "max_scene_boundary_rms_delta_db": args.max_boundary_rms_delta_db,
            "max_adjacent_retime_factor_delta": args.max_adjacent_retime_delta,
            "max_baseline_f0_step_semitones": args.max_baseline_f0_step_st,
            "max_baseline_f0_p95_step_semitones": args.max_baseline_f0_p95_step_st,
            "max_baseline_centroid_step_semitones": args.max_baseline_centroid_step_st,
            "max_baseline_centroid_p95_step_semitones": args.max_baseline_centroid_p95_step_st,
            "baseline_window_s": args.baseline_window_s,
            "baseline_hop_s": args.baseline_hop_s,
            "boundary_window_s": args.boundary_window_s,
        },
        "metrics": {
            "active_1s_p90_p10_db": round(active_1s_spread, 3),
            "baseline_f0_max_step_semitones": round(max_f0_step, 3),
            "baseline_f0_p95_step_semitones": round(p95_f0_step, 3),
            "baseline_centroid_max_step_semitones": round(max_centroid_step, 3),
            "baseline_centroid_p95_step_semitones": round(p95_centroid_step, 3),
            "adjacent_caption_rms_max_delta_db": round(max_caption_delta, 3),
            "scene_boundary_rms_max_delta_db": round(max_boundary_delta, 3),
            "adjacent_retime_factor_max_delta": round(max_retime_delta, 6),
        },
        "hotspots": {
            "f0_steps": sorted(f0_steps, key=lambda row: float(row["delta_st"]), reverse=True)[:12],
            "centroid_steps": sorted(
                centroid_steps, key=lambda row: float(row["delta_st"]), reverse=True
            )[:12],
            "caption_rms_steps": sorted(
                caption_steps, key=lambda row: abs(float(row["delta_db"])), reverse=True
            )[:12],
            "scene_boundaries": boundaries,
            "retime_steps": retime_steps,
        },
        "errors": errors,
        "warnings": warnings,
    }
    atomic_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
