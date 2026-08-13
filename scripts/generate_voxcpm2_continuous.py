#!/usr/bin/env python3
"""Generate one acoustically continuous VoxCPM2 take per episode.

This is the production path. Visual scenes are mapped onto the selected take
only after full-episode forced alignment. The legacy scene generator remains
available only through an explicit compatibility flag in the wrapper.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# The bundled librosa/numba wheels may live in a read-only cache on this Mac.
# Disabling numba's disk cache changes no model math and keeps prompt encoding
# usable on CPU fallback as well as MPS.
# The bundled librosa/numba wheels may live in a read-only cache on this Mac.
# Pointing numba at a writable cache avoids a locator failure; model math is
# unchanged.
os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/voxcpm2-numba-cache")
Path("/tmp/voxcpm2-numba-cache").mkdir(parents=True, exist_ok=True)

import numpy as np
import soundfile as sf
import torch
from voxcpm import VoxCPM

from profile_config import get_in, load_resolved_profile


ROOT = Path(__file__).resolve().parent  # <skill>/scripts
PIPELINE = ROOT.parent
SCRIPTS = ROOT
PROFILE_PATH = PIPELINE / "references/voice-stability-profile.json"
PROSODY_VALIDATOR = SCRIPTS / "validate_prosody.py"
EPISODE_VALIDATOR = SCRIPTS / "validate_episode_independence.py"
ALIGNER_SCRIPT = ROOT / "align_all_captions.py"

def resolve_project(explicit, series: dict, series_path: Path, ep: str) -> Path:
    """--project wins; otherwise series.json must declare project_dir_template."""
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    template = str(series.get("project_dir_template", "")).strip()
    if not template:
        raise SystemExit(
            "pass --project or set project_dir_template in series.json "
            '(e.g. "../<slug>-ep{ep}", resolved relative to series.json)'
        )
    return (series_path.parent / template.format(ep=ep)).resolve()


ALIGNER_PYTHON: Path | None = None
SERIES_PATH: Path | None = None
SERIES: dict = {}
MODEL: Path | None = None
MODEL_CONFIG: Path | None = None
TARGET_CPM: float | None = None
ENDING_CTA = ""
PROMPT_MANIFEST: Path | None = None
PIPELINE_PROFILE: dict[str, Any] = {}
RESOLVED_PROFILE_PATH: Path | None = None
IGNORED = set(" \t\n，。！？；：、,.!?;:“”‘’（）()—｜")
SEED_BASE = 2026080603
CFG_VALUE = 1.5
INFERENCE_TIMESTEPS = 20

sys.path.insert(0, str(SCRIPTS))
from validate_voice_stability import (  # noqa: E402
    db_rms,
    decode_audio,
    framed_features,
    percentile,
    semitone_delta,
    sha256 as validator_sha256,
)


def sha256(path: Path) -> str:
    return validator_sha256(path)


def cache_key(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def model_fingerprint(model_dir: Path) -> str:
    """Hash model weights once, then reuse only while size/mtime signatures match."""
    files = [
        path for path in (
            model_dir / "config.json",
            model_dir / "model.safetensors",
            model_dir / "audiovae.pth",
            model_dir / "tokenizer.json",
            model_dir / "tokenization_voxcpm2.py",
        )
        if path.is_file()
    ]
    if not files:
        raise FileNotFoundError(f"no VoxCPM2 model files found in {model_dir}")
    signatures = {
        path.name: {"size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}
        for path in files
    }
    manifest_path = model_dir / ".agent-video-model-fingerprint.json"
    if manifest_path.is_file():
        try:
            cached = json.loads(manifest_path.read_text(encoding="utf-8"))
            if cached.get("signatures") == signatures and cached.get("fingerprint_sha256"):
                return str(cached["fingerprint_sha256"])
        except (OSError, json.JSONDecodeError):
            pass
    file_hashes = {path.name: sha256(path) for path in files}
    fingerprint = cache_key(file_hashes)
    atomic_json(
        manifest_path,
        {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "signatures": signatures,
            "file_sha256": file_hashes,
            "fingerprint_sha256": fingerprint,
        },
    )
    return fingerprint


@contextmanager
def exclusive_tts_lock() -> Any:
    """Serialize VoxCPM2 model loads/generation across episode processes."""
    lock_path = Path("/tmp/agent-video-pipeline-voxcpm2.lock")
    with lock_path.open("a+") as handle:
        print(json.dumps({"event": "waiting_for_tts_lock", "path": str(lock_path)}), flush=True)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            print(json.dumps({"event": "tts_lock_acquired", "path": str(lock_path)}), flush=True)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    partial.replace(path)


def record_timing(
    project: Path,
    stage: str,
    started_at: datetime,
    finished_at: datetime,
    elapsed_s: float,
    status: str,
    attempt: int,
    cache_hit: bool,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append one retry-safe timing event without claiming it is wall-clock total."""
    timing_path = project / "pipeline-timings.json"
    payload = (
        json.loads(timing_path.read_text(encoding="utf-8"))
        if timing_path.is_file()
        else {
            "schema_version": 1,
            "wall_clock_started_at": started_at.isoformat(),
            "wall_clock_finished_at": None,
            "wall_clock_elapsed_s": None,
            "events": [],
        }
    )
    event_metadata = metadata or {}
    payload.setdefault("events", []).append(
        {
            "stage": stage,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "elapsed_s": round(float(elapsed_s), 3),
            "status": status,
            "attempt": int(attempt),
            "cache_hit": bool(cache_hit),
            "cache_key": str(event_metadata.get("cache_key", "")),
            "metadata": event_metadata,
        }
    )
    atomic_json(timing_path, payload)


def duration(path: Path) -> float:
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


def visible(text: str) -> int:
    return sum(1 for char in text if char not in IGNORED)


def fade(samples: np.ndarray, sample_rate: int, ms: float = 12.0) -> np.ndarray:
    count = min(int(sample_rate * ms / 1000), samples.size // 3)
    if count < 2:
        return samples
    curve = np.sin(np.linspace(0, np.pi / 2, count, dtype=np.float32)) ** 2
    output = samples.copy()
    output[:count] *= curve
    output[-count:] *= curve[::-1]
    return output


def scenes_with_ending_cta(episode: dict[str, Any]) -> list[dict[str, Any]]:
    scenes = [dict(scene) for scene in episode["scenes"]]
    if not scenes or not ENDING_CTA:
        return scenes
    text = str(scenes[-1].get("text", "")).rstrip()
    if not text.endswith(ENDING_CTA):
        scenes[-1]["text"] = f"{text}{ENDING_CTA}"
    return scenes


def load_prompt() -> dict[str, Any]:
    if PROMPT_MANIFEST is None:
        raise ValueError("voice prompt manifest is required")
    if not PROMPT_MANIFEST.is_file():
        raise FileNotFoundError(f"missing frozen VoxCPM2 prompt manifest: {PROMPT_MANIFEST}")
    prompt = json.loads(PROMPT_MANIFEST.read_text(encoding="utf-8"))
    prompt_wav = Path(prompt["prompt_wav_path"])
    source = Path(prompt["source_path"])
    if not prompt_wav.is_file() or not source.is_file():
        raise FileNotFoundError("frozen prompt or original voice source is missing")
    if sha256(prompt_wav) != prompt.get("prompt_wav_sha256"):
        raise RuntimeError("frozen prompt WAV hash differs from its manifest")
    if sha256(source) != prompt.get("source_sha256"):
        raise RuntimeError("original voice source changed; rebuild and review the golden prompt")
    expected_text = str(
        SERIES.get("voice_prompt_text", get_in(PIPELINE_PROFILE, "voice.voice_prompt_text", ""))
        or ""
    ).strip()
    if expected_text and prompt.get("prompt_text") != expected_text:
        raise RuntimeError("series voice_prompt_text differs from the frozen prompt manifest")
    return prompt


def load_prosody(project: Path, scenes: list[dict[str, Any]]) -> dict[str, Any]:
    path = project / "audio/prosody.json"
    if not path.is_file():
        raise FileNotFoundError(f"approved v2 prosody required before TTS: {path}")
    subprocess.run(
        [
            sys.executable,
            str(PROSODY_VALIDATOR),
            "--prosody",
            str(path),
            "--require-approved",
        ],
        check=True,
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = {str(item["id"]): item for item in data.get("scenes", [])}
    for scene in scenes:
        item = rows.get(str(scene["id"]))
        if item is None:
            raise ValueError(f"prosody missing scene: {scene['id']}")
        if str(item.get("source_text", "")).strip() != str(scene["text"]).strip():
            raise ValueError(f"prosody source_text is stale for scene: {scene['id']}")
    return data


def allowed_cpm_range(target: float) -> list[float]:
    nominal = float(get_in(PIPELINE_PROFILE, "voice.target_effective_chinese_chars_per_minute", target))
    if abs(target - nominal) < 1e-6:
        configured = get_in(PIPELINE_PROFILE, "voice.allowed_range", [target - 10, target + 10])
        return [float(configured[0]), float(configured[1])]
    fast = get_in(PIPELINE_PROFILE, "voice.fast_trial", {})
    if isinstance(fast, dict) and abs(
        target - float(fast.get("nominal_target_effective_chinese_chars_per_minute", -1))
    ) < 1e-6:
        configured = fast.get("allowed_range", [target - 10, target + 10])
        return [float(configured[0]), float(configured[1])]
    return [target - 10.0, target + 10.0]


def baseline_metrics(audio: Path, profile: dict[str, Any]) -> dict[str, float]:
    analysis = profile["analysis"]
    sample_rate = int(analysis["sample_rate"])
    gate = float(analysis["speech_gate_dbfs"])
    window_s = float(analysis["baseline_window_s"])
    hop_s = float(analysis["baseline_hop_s"])
    samples = decode_audio(audio, sample_rate)
    features = framed_features(samples, sample_rate, 0.04, 0.02, gate)

    one_second: list[float] = []
    frame = sample_rate
    hop = max(1, round(sample_rate * 0.1))
    for start in range(0, max(0, samples.size - frame + 1), hop):
        value = db_rms(samples[start : start + frame])
        if value >= gate:
            one_second.append(value)

    rows: list[tuple[float, float]] = []
    total_duration = samples.size / sample_rate
    for start in np.arange(0.0, max(0.0, total_duration - window_s), hop_s):
        mask = (
            (features["time_s"] >= start)
            & (features["time_s"] < start + window_s)
            & (features["rms_dbfs"] >= gate)
        )
        voiced = mask & np.isfinite(features["f0_hz"])
        if int(np.count_nonzero(mask)) < 30 or int(np.count_nonzero(voiced)) < 20:
            continue
        rows.append(
            (
                float(np.median(features["f0_hz"][voiced])),
                float(np.median(features["centroid_hz"][mask])),
            )
        )
    f0_steps = [semitone_delta(a[0], b[0]) for a, b in zip(rows, rows[1:])]
    centroid_steps = [semitone_delta(a[1], b[1]) for a, b in zip(rows, rows[1:])]
    return {
        "active_1s_p90_p10_db": percentile(one_second, 90) - percentile(one_second, 10),
        "baseline_f0_median_hz": float(np.median([row[0] for row in rows])) if rows else 0.0,
        "baseline_centroid_median_hz": float(np.median([row[1] for row in rows])) if rows else 0.0,
        "baseline_f0_max_step_semitones": max(f0_steps, default=0.0),
        "baseline_f0_p95_step_semitones": percentile(f0_steps, 95),
        "baseline_centroid_max_step_semitones": max(centroid_steps, default=0.0),
        "baseline_centroid_p95_step_semitones": percentile(centroid_steps, 95),
    }


def required_retime(raw_cpm: float, allowed: list[float]) -> float:
    if raw_cpm < allowed[0]:
        return allowed[0] / raw_cpm
    if raw_cpm > allowed[1]:
        return allowed[1] / raw_cpm
    return 1.0


def score_candidate(
    metrics: dict[str, float],
    raw_cpm: float,
    retime_factor: float,
    profile: dict[str, Any],
    prompt_metrics: dict[str, float],
    target_cpm: float,
) -> tuple[list[str], float]:
    limits = profile["hard_limits"]
    errors: list[str] = []
    checks = (
        ("active_1s_p90_p10_db", "active_1s_p90_p10_db"),
        ("baseline_f0_max_step_semitones", "baseline_f0_max_step_semitones"),
        ("baseline_f0_p95_step_semitones", "baseline_f0_p95_step_semitones"),
        ("baseline_centroid_max_step_semitones", "baseline_centroid_max_step_semitones"),
        ("baseline_centroid_p95_step_semitones", "baseline_centroid_p95_step_semitones"),
    )
    for metric_key, limit_key in checks:
        if metrics[metric_key] > float(limits[limit_key]):
            errors.append(
                f"{metric_key}={metrics[metric_key]:.3f} exceeds {float(limits[limit_key]):.3f}"
            )
    hard_retime = [float(value) for value in profile["generation"]["hard_global_retime_range"]]
    if not hard_retime[0] <= retime_factor <= hard_retime[1]:
        errors.append(
            f"global_retime_factor={retime_factor:.4f} outside {hard_retime[0]:.2f}-{hard_retime[1]:.2f}"
        )

    f0_distance = semitone_delta(
        prompt_metrics["baseline_f0_median_hz"], metrics["baseline_f0_median_hz"]
    )
    centroid_distance = semitone_delta(
        prompt_metrics["baseline_centroid_median_hz"],
        metrics["baseline_centroid_median_hz"],
    )
    score = (
        1.5 * f0_distance
        + 0.5 * centroid_distance
        + metrics["baseline_f0_p95_step_semitones"]
        + 0.5 * metrics["baseline_centroid_p95_step_semitones"]
        + 0.35 * metrics["active_1s_p90_p10_db"]
        + 2.0 * abs(raw_cpm - target_cpm) / max(target_cpm, 1.0)
    )
    return errors, score


def parse_loudnorm(stderr: str) -> dict[str, Any]:
    matches = re.findall(r'\{\s*"input_i".*?\}', stderr, flags=re.DOTALL)
    if not matches:
        raise RuntimeError("ffmpeg loudnorm did not return JSON")
    return json.loads(matches[-1])


def slow_speech_gain_ride(
    source: Path,
    target: Path,
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Apply a bounded, slow gain envelope without touching pitch or timing."""
    samples, sample_rate = sf.read(source, dtype="float32")
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    hop_s = 0.1
    window_s = 1.0
    hop = max(1, round(sample_rate * hop_s))
    window = max(2, round(sample_rate * window_s))
    times: list[float] = []
    levels: list[float] = []
    gate = float(profile["analysis"]["speech_gate_dbfs"])
    for start in range(0, max(0, samples.size - window + 1), hop):
        value = db_rms(samples[start : start + window])
        if value >= gate:
            times.append((start + window / 2) / sample_rate)
            levels.append(value)
    if len(levels) < 3:
        raise RuntimeError("not enough speech-active windows for slow gain riding")
    target_level = float(np.median(np.asarray(levels)))
    limit = float(profile["post_processing"]["sentence_gain_limit_db"])
    # Keep the rider slow, but do not leave a full 0.6 dB dead zone at every
    # sentence onset.  That gap was large enough for short phrases (especially
    # dates and one-line answers) to remain audibly louder than their neighbors.
    # A 0.25 dB deadband stays below the profile's gain limit while tightening
    # caption-to-caption and scene-boundary RMS drift without touching pitch or
    # timing.
    deadband = 0.25
    desired = []
    for value in levels:
        correction = target_level - value
        if abs(correction) <= deadband:
            correction = 0.0
        else:
            correction -= math.copysign(deadband, correction)
        desired.append(float(np.clip(correction, -limit, limit)))

    attack = float(profile["post_processing"]["gain_rider_attack_ms"]) / 1000.0
    release = float(profile["post_processing"]["gain_rider_release_ms"]) / 1000.0
    smoothed: list[float] = []
    current = 0.0
    for wanted in desired:
        tau = attack if wanted < current else release
        alpha = 1.0 - math.exp(-hop_s / max(tau, 1e-3))
        current += alpha * (wanted - current)
        smoothed.append(float(np.clip(current, -limit, limit)))

    anchors = np.asarray(times, dtype=np.float64) * sample_rate
    gain_db = np.interp(
        np.arange(samples.size, dtype=np.float64),
        anchors,
        np.asarray(smoothed, dtype=np.float64),
        left=smoothed[0],
        right=smoothed[-1],
    ).astype(np.float32)
    output = samples * np.power(10.0, gain_db / 20.0).astype(np.float32)
    peak = float(np.max(np.abs(output))) if output.size else 0.0
    safety_gain_db = 0.0
    if peak > 0.98:
        scale = 0.98 / peak
        output *= scale
        safety_gain_db = 20.0 * math.log10(scale)
    target.parent.mkdir(parents=True, exist_ok=True)
    sf.write(target, output, sample_rate, subtype="PCM_24")

    after_levels: list[float] = []
    for start in range(0, max(0, output.size - window + 1), hop):
        value = db_rms(output[start : start + window])
        if value >= gate:
            after_levels.append(value)
    return {
        "method": "speech_active_slow_gain_rider",
        "source_path": str(source),
        "source_sha256": sha256(source),
        "output_path": str(target),
        "output_sha256": sha256(target),
        "window_s": window_s,
        "hop_s": hop_s,
        "attack_ms": round(attack * 1000),
        "release_ms": round(release * 1000),
        "deadband_db": deadband,
        "gain_limit_db": limit,
        "target_active_rms_dbfs": round(target_level, 3),
        "max_abs_smoothed_gain_db": round(max(abs(value) for value in smoothed), 3),
        "safety_global_gain_db": round(safety_gain_db, 3),
        "active_1s_p90_p10_before_db": round(
            percentile(levels, 90) - percentile(levels, 10), 3
        ),
        "active_1s_p90_p10_after_db": round(
            percentile(after_levels, 90) - percentile(after_levels, 10), 3
        ),
    }


def measured_two_pass_master(
    source: Path,
    target: Path,
    sample_rate: int,
    retime_factor: float,
    profile: dict[str, Any],
) -> dict[str, Any]:
    staging = target.with_name(target.stem + ".pre-normalize.part.wav")
    filters = (
        f"highpass=f=65,atempo={retime_factor:.8f},"
        "acompressor=threshold=-20dB:ratio=1.3:attack=40:release=300:makeup=1:mix=0.85"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-filter:a",
            filters,
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-c:a",
            "pcm_s24le",
            str(staging),
        ],
        check=True,
    )
    post = profile["post_processing"]
    target_i = float(post["integrated_lufs"])
    target_tp = float(post["true_peak_dbtp"])
    first = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(staging),
            "-filter:a",
            f"loudnorm=I={target_i}:TP={target_tp}:LRA=6:print_format=json",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    measured = parse_loudnorm(first.stderr)
    second_filter = (
        f"loudnorm=I={target_i}:TP={target_tp}:LRA=6:"
        f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
        f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
        f"offset={measured['target_offset']}:linear=true:print_format=json"
    )
    partial = target.with_name(target.name + ".part.wav")
    second = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-y",
            "-i",
            str(staging),
            "-filter:a",
            second_filter,
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-c:a",
            "pcm_s24le",
            str(partial),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    applied = parse_loudnorm(second.stderr)
    normalization_type = str(applied.get("normalization_type", "")).lower()
    if normalization_type == "linear":
        partial.replace(target)
        staging.unlink(missing_ok=True)
        return {
            "scope": "master_once",
            "method": "ebu_r128_measured_two_pass",
            "application_count": 1,
            "pre_filter": filters,
            "first_pass": measured,
            "second_pass": applied,
        }

    # A linear loudnorm pass is mathematically impossible when the measured
    # true peak is already close to the requested ceiling: the gain needed to
    # reach -16 LUFS would push peaks over -2 dBTP.  Do not accept FFmpeg's
    # dynamic fallback here—the short-term gain riding is exactly the pumping
    # artefact this pipeline is designed to avoid.  Apply the measured target
    # gain once, followed only by a static true-peak limiter.
    partial.unlink(missing_ok=True)
    input_i = float(measured["input_i"])
    static_gain_db = float(target_i) - input_i
    limiter_amplitude = 10.0 ** (float(target_tp) / 20.0)
    static_filter = (
        f"volume={static_gain_db:.6f}dB,"
        f"alimiter=limit={limiter_amplitude:.6f}:attack=5:release=100:level=false"
    )
    static_result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-y",
            "-i",
            str(staging),
            "-filter:a",
            static_filter,
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-c:a",
            "pcm_s24le",
            str(partial),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    partial.replace(target)
    staging.unlink(missing_ok=True)
    return {
        "scope": "master_once",
        "method": "measured_static_gain_true_peak_limiter",
        "application_count": 1,
        "pre_filter": filters,
        "first_pass": measured,
        "linear_pass": applied,
        "fallback_reason": "linear_loudnorm_would_exceed_true_peak_ceiling",
        "static_filter": static_filter,
        "static_gain_db": round(static_gain_db, 6),
        "static_result_stderr": static_result.stderr[-400:] if static_result.stderr else "",
    }


def provisional_timeline(
    episode: dict[str, Any],
    scenes: list[dict[str, Any]],
    master_duration: float,
    raw_cpm: float,
    final_cpm: float,
    retime_factor: float,
    target_cpm: float,
    allowed: list[float],
    selected_seed: int,
    profile_sha: str,
) -> dict[str, Any]:
    total_chars = sum(visible(str(scene["text"])) for scene in scenes)
    rows: list[dict[str, Any]] = []
    cursor = 0.0
    effective_cursor = 0
    for index, scene in enumerate(scenes):
        chars = visible(str(scene["text"]))
        end = master_duration if index == len(scenes) - 1 else cursor + master_duration * chars / total_chars
        scene_duration = end - cursor
        rows.append(
            {
                **scene,
                "path": "audio/output/narration_master.wav",
                "source": "master_slice",
                "start_s": round(cursor, 6),
                "end_s": round(end, 6),
                "duration_s": round(scene_duration, 6),
                "spoken_duration_s": round(scene_duration, 6),
                "effective_chars": chars,
                "effective_cpm": round(chars * 60 / scene_duration, 3),
                "requested_target_cpm": target_cpm,
                "selected_target_cpm": round(final_cpm, 3),
                "retime_factor": round(retime_factor, 8),
                "effective_char_start": effective_cursor,
                "effective_char_end": effective_cursor + chars,
                "alignment_status": "provisional_requires_forced_alignment",
                "source_gap_after_s": scene.get("gap_after_s"),
                "gap_after_s": 0.0,
            }
        )
        effective_cursor += chars
        cursor = end
    return {
        "schema_version": 2,
        "title": episode["title"],
        "audio": "audio/output/narration_master.wav",
        "sample_rate": None,
        "channels": 1,
        "generation_mode": "continuous_episode_take",
        "alignment_status": "required_before_voice_qc_or_render",
        "candidate_selection_status": "acoustic_pass_alignment_pending",
        "selected_seed": selected_seed,
        "normalization_scope": "master_once",
        "target_cpm": target_cpm,
        "allowed_cpm_range": allowed,
        "speed_override": target_cpm != TARGET_CPM,
        "raw_effective_cpm": round(raw_cpm, 3),
        "episode_effective_cpm": round(final_cpm, 3),
        "effective_cpm": round(final_cpm, 3),
        "global_retime_factor": round(retime_factor, 8),
        "total_effective_chars": total_chars,
        "total_spoken_duration_s": round(master_duration, 6),
        "total_duration_s": round(master_duration, 6),
        "voice_stability_profile_sha256": profile_sha,
        "scenes": rows,
    }


def align_candidate(project: Path, row: dict[str, Any], text_path: Path) -> None:
    """Run coverage alignment immediately so adaptive generation can stop safely."""
    if not ALIGNER_PYTHON.is_file():
        raise FileNotFoundError(f"candidate alignment Python not found: {ALIGNER_PYTHON}")
    audio_path = project / str(row["path"])
    report_path = audio_path.with_suffix(".alignment.json")
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    result = subprocess.run(
        [
            str(ALIGNER_PYTHON),
            str(ALIGNER_SCRIPT),
            "--series",
            str(SERIES_PATH),
            "--candidate-audio",
            str(audio_path),
            "--candidate-text-file",
            str(text_path),
            "--candidate-report",
            str(report_path),
        ]
    )
    alignment = (
        json.loads(report_path.read_text(encoding="utf-8"))
        if report_path.is_file()
        else {"status": "fail", "errors": ["alignment report missing"]}
    )
    row["alignment"] = alignment
    row["alignment_gate"] = (
        "pass" if result.returncode == 0 and alignment.get("status") == "pass" else "fail"
    )
    if row["alignment_gate"] != "pass":
        row["errors"].extend(alignment.get("errors", ["candidate alignment failed"]))
    finished_at = datetime.now(timezone.utc)
    record_timing(
        project,
        "candidate_forced_alignment",
        started_at,
        finished_at,
        time.perf_counter() - started,
        row["alignment_gate"],
        int(row.get("attempt", 1)),
        False,
        {
            "seed": row.get("actual_seed"),
            "candidate_sha256": row.get("sha256"),
            "cache_key": cache_key(
                {
                    "candidate_sha256": row.get("sha256"),
                    "text_sha256": hashlib.sha256(text_path.read_bytes()).hexdigest(),
                    "aligner_sha256": sha256(ALIGNER_SCRIPT),
                }
            ),
        },
    )


def qualifies_for_adaptive_early_stop(row: dict[str, Any], profile: dict[str, Any]) -> bool:
    """Use a stricter-than-hard-gate band before accepting a single candidate."""
    config = profile.get("generation", {}).get("adaptive_early_stop", {})
    if not config.get("enabled", False):
        return False
    if row.get("acoustic_gate") != "pass" or row.get("alignment_gate") != "pass":
        return False
    retime_min, retime_max = [float(value) for value in config["global_retime_range"]]
    retime = float(row["required_global_retime_factor"])
    if not retime_min <= retime <= retime_max:
        return False
    metrics = row.get("acoustic_metrics", {})
    metric_limits = config.get("metric_limits", {})
    return all(
        float(metrics.get(metric, math.inf)) <= float(limit)
        for metric, limit in metric_limits.items()
    )


def prepare_candidates(
    args: argparse.Namespace,
    project: Path,
    candidate_dir: Path,
    episode_text: str,
    char_count: int,
    prompt: dict[str, Any],
    prompt_metrics: dict[str, float],
    profile: dict[str, Any],
    profile_sha: str,
    allowed: list[float],
) -> tuple[list[dict[str, Any]], dict[str, Any], Path, int]:
    """Generate candidates or re-score a previously completed candidate set."""
    candidate_index_path = project / "audio/raw/candidate-index.json"
    prompt_wav = Path(prompt["prompt_wav_path"])
    text_sha = hashlib.sha256(episode_text.encode("utf-8")).hexdigest()
    shared_cache_inputs = {
        "text_sha256": text_sha,
        "prompt_manifest_sha256": sha256(PROMPT_MANIFEST),
        "prompt_wav_sha256": sha256(prompt_wav),
        "prosody_sha256": sha256(project / "audio/prosody.json"),
        "profile_sha256": profile_sha,
        "pipeline_profile_sha256": get_in(PIPELINE_PROFILE, "_meta.profile_sha256"),
        "model_config_sha256": sha256(MODEL_CONFIG),
        "model_fingerprint_sha256": model_fingerprint(MODEL),
        "generator_sha256": sha256(Path(__file__).resolve()),
        "cfg_value": CFG_VALUE,
        "inference_timesteps": INFERENCE_TIMESTEPS,
        "target_cpm": float(args.target_cpm),
    }
    text_path = candidate_dir / "episode-text.txt"
    if not args.skip_candidate_alignment:
        text_path.write_text(episode_text + "\n", encoding="utf-8")
    if args.reuse_candidates:
        if not candidate_index_path.is_file():
            raise FileNotFoundError(f"candidate index not found: {candidate_index_path}")
        index = json.loads(candidate_index_path.read_text(encoding="utf-8"))
        if index.get("text_sha256") != text_sha:
            raise RuntimeError("saved candidate text does not match the current episode")
        if index.get("prompt_manifest_sha256") != sha256(PROMPT_MANIFEST):
            raise RuntimeError("saved candidates use a different frozen prompt manifest")
        if index.get("prompt_wav_sha256") != sha256(prompt_wav):
            raise RuntimeError("saved candidates use a different prompt WAV")
        required_cache_fields = {
            "prosody_sha256": shared_cache_inputs["prosody_sha256"],
            "profile_sha256": shared_cache_inputs["profile_sha256"],
            "pipeline_profile_sha256": shared_cache_inputs["pipeline_profile_sha256"],
            "model_config_sha256": shared_cache_inputs["model_config_sha256"],
            "model_fingerprint_sha256": shared_cache_inputs["model_fingerprint_sha256"],
            "cfg_value": CFG_VALUE,
            "inference_timesteps": INFERENCE_TIMESTEPS,
        }
        stale = [key for key, value in required_cache_fields.items() if index.get(key) != value]
        if stale:
            raise RuntimeError(f"saved candidates are stale for current cache inputs: {stale}")
        candidates: list[dict[str, Any]] = []
        sample_rate = 48000
        for old in index.get("candidates", [])[: args.candidate_count]:
            timing_now = datetime.now(timezone.utc)
            path = project / str(old["path"])
            if not path.is_file():
                raise FileNotFoundError(path)
            sample_rate = int(sf.info(path).samplerate)
            raw_duration = duration(path)
            raw_cpm = char_count * 60 / raw_duration
            retime = required_retime(raw_cpm, allowed)
            metrics = baseline_metrics(path, profile)
            errors, score = score_candidate(
                metrics, raw_cpm, retime, profile, prompt_metrics, args.target_cpm
            )
            row = {
                **old,
                "attempt": len(candidates) + 1,
                "cache_key": cache_key({**shared_cache_inputs, "seed": old.get("requested_seed")}),
                "duration_s": round(raw_duration, 6),
                "raw_effective_cpm": round(raw_cpm, 3),
                "required_global_retime_factor": round(retime, 8),
                "sha256": sha256(path),
                "acoustic_metrics": {key: round(value, 4) for key, value in metrics.items()},
                "acoustic_gate": "pass" if not errors else "fail",
                "alignment_gate": "pending",
                "selection_score": round(score, 6),
                "errors": errors,
            }
            candidates.append(row)
            record_timing(
                project,
                "tts_candidate",
                timing_now,
                timing_now,
                0.0,
                "cache_hit",
                len(candidates),
                True,
                {
                    "seed": row.get("actual_seed"),
                    "sha256": row["sha256"],
                    "cache_key": row["cache_key"],
                },
            )
            print(json.dumps({"event": "candidate_rescored", **row}, ensure_ascii=False), flush=True)
        if not candidates:
            raise RuntimeError("reuse-candidates requires at least one completed WAV candidate")
        index.update({
            "created_at": datetime.now(timezone.utc).isoformat(),
            "profile_sha256": profile_sha,
            "generator_sha256": shared_cache_inputs["generator_sha256"],
            "target_cpm": float(args.target_cpm),
            "candidates": candidates,
        })
        return candidates, index, candidate_index_path, sample_rate

    model_started_at = datetime.now(timezone.utc)
    loaded = time.perf_counter()
    # The local desktop may expose no MPS device (for example in a sandbox or
    # after a macOS runtime reset). VoxCPM2's auto resolver keeps the same
    # model/seed path and falls back to CPU instead of silently using legacy
    # scene synthesis.
    model = VoxCPM.from_pretrained(str(MODEL), load_denoiser=False, device="auto")
    sample_rate = int(model.tts_model.sample_rate)
    model_load_elapsed = time.perf_counter() - loaded
    record_timing(
        project,
        "tts_model_load",
        model_started_at,
        datetime.now(timezone.utc),
        model_load_elapsed,
        "pass",
        1,
        False,
        {
            "model_fingerprint_sha256": shared_cache_inputs["model_fingerprint_sha256"],
            "cache_key": shared_cache_inputs["model_fingerprint_sha256"],
        },
    )
    print(
        json.dumps(
            {
                "event": "model_loaded",
                "seconds": round(model_load_elapsed, 3),
                "sample_rate": sample_rate,
                "generation_mode": "continuous_episode_take",
            }
        ),
        flush=True,
    )
    candidates = []
    base_seed = SEED_BASE + args.seed_offset + args.episode * 1000
    for index in range(args.candidate_count):
        requested_seed = base_seed + index * 17
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        # VoxCPM releases before the seed-aware API do not accept ``seed`` in
        # generate(). Seed the RNGs at the framework boundary instead. This
        # preserves deterministic candidates without coupling the pipeline to
        # one voxcpm package signature.
        np.random.seed(requested_seed % (2**32))
        torch.manual_seed(requested_seed)
        if torch.backends.mps.is_available():
            torch.mps.manual_seed(requested_seed)
        wav = model.generate(
            text=episode_text,
            prompt_wav_path=str(prompt_wav),
            prompt_text=str(prompt["prompt_text"]),
            reference_wav_path=str(prompt_wav),
            cfg_value=CFG_VALUE,
            inference_timesteps=INFERENCE_TIMESTEPS,
            normalize=False,
            denoise=False,
            retry_badcase=False,
        )
        samples = np.asarray(wav, dtype=np.float32).reshape(-1)
        peak = float(np.max(np.abs(samples))) if samples.size else 0.0
        if peak > 0.98:
            samples = samples * (0.98 / peak)
        samples = fade(samples, sample_rate)
        candidate_path = candidate_dir / f"episode-seed-{requested_seed}.wav"
        sf.write(candidate_path, samples, sample_rate, subtype="PCM_24")
        raw_duration = duration(candidate_path)
        raw_cpm = char_count * 60 / raw_duration
        retime = required_retime(raw_cpm, allowed)
        metrics = baseline_metrics(candidate_path, profile)
        errors, score = score_candidate(
            metrics, raw_cpm, retime, profile, prompt_metrics, args.target_cpm
        )
        actual_seed = getattr(getattr(model, "tts_model", None), "last_successful_seed", None)
        row = {
            "requested_seed": requested_seed,
            "actual_seed": int(actual_seed) if actual_seed is not None else requested_seed,
            "attempt": index + 1,
            "cache_key": cache_key({**shared_cache_inputs, "seed": requested_seed}),
            "path": str(candidate_path.relative_to(project)),
            "sha256": sha256(candidate_path),
            "duration_s": round(raw_duration, 6),
            "raw_effective_cpm": round(raw_cpm, 3),
            "required_global_retime_factor": round(retime, 8),
            "peak_before_safety_scale": round(peak, 6),
            "acoustic_metrics": {key: round(value, 4) for key, value in metrics.items()},
            "acoustic_gate": "pass" if not errors else "fail",
            "alignment_gate": "pending",
            "selection_score": round(score, 6),
            "errors": errors,
            "generation_seconds": round(time.perf_counter() - started, 3),
        }
        candidates.append(row)
        synthesis_finished_at = datetime.now(timezone.utc)
        record_timing(
            project,
            "tts_candidate",
            started_at,
            synthesis_finished_at,
            float(row["generation_seconds"]),
            row["acoustic_gate"],
            index + 1,
            False,
            {
                "seed": row["actual_seed"],
                "sha256": row["sha256"],
                "acoustic_gate": row["acoustic_gate"],
                "cache_key": row["cache_key"],
            },
        )
        if row["acoustic_gate"] == "pass" and not args.skip_candidate_alignment:
            align_candidate(project, row, text_path)
        elif row["acoustic_gate"] == "pass":
            row["alignment_gate"] = "diagnostic_skip"
        print(json.dumps({"event": "candidate_generated", **row}, ensure_ascii=False), flush=True)
        if args.adaptive_candidates and qualifies_for_adaptive_early_stop(row, profile):
            print(
                json.dumps(
                    {
                        "event": "adaptive_candidate_early_stop",
                        "candidate_limit": args.candidate_count,
                        "actual_candidate_count": len(candidates),
                        "selected_seed": row["actual_seed"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            break

    candidate_index = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "generation_mode": "continuous_episode_take",
        "episode": args.episode,
        "text_sha256": text_sha,
        "effective_chars": char_count,
        "candidate_strategy": "adaptive_sequential" if args.adaptive_candidates else "fixed_batch",
        "candidate_limit": args.candidate_count,
        "candidate_count": len(candidates),
        "prompt_manifest_sha256": sha256(PROMPT_MANIFEST),
        "prompt_wav_sha256": sha256(prompt_wav),
        "prosody_sha256": sha256(project / "audio/prosody.json"),
        "profile_sha256": profile_sha,
        "model_config_sha256": sha256(MODEL_CONFIG),
        "model_fingerprint_sha256": shared_cache_inputs["model_fingerprint_sha256"],
        "generator_sha256": shared_cache_inputs["generator_sha256"],
        "cfg_value": CFG_VALUE,
        "inference_timesteps": INFERENCE_TIMESTEPS,
        "target_cpm": float(args.target_cpm),
        "candidates": candidates,
    }
    return candidates, candidate_index, candidate_index_path, sample_rate


def main() -> int:
    global ALIGNER_PYTHON, ENDING_CTA, MODEL, MODEL_CONFIG, PROMPT_MANIFEST
    global SERIES, SERIES_PATH, TARGET_CPM, PROFILE_PATH
    global PIPELINE_PROFILE, RESOLVED_PROFILE_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", type=int, required=True)
    parser.add_argument("--target-cpm", type=float)
    parser.add_argument("--series", type=Path, required=True)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--aligner-python", type=Path)
    parser.add_argument("--profile", type=Path, help="resolved pipeline profile JSON")
    parser.add_argument("--stability-profile", type=Path)
    parser.add_argument("--candidate-count", type=int, default=3)
    parser.add_argument(
        "--fixed-candidate-batch",
        action="store_false",
        dest="adaptive_candidates",
        help="Disable adaptive early stop and always generate candidate-count takes.",
    )
    parser.set_defaults(adaptive_candidates=True)
    parser.add_argument("--seed-offset", type=int, default=0)
    parser.add_argument(
        "--reuse-candidates",
        action="store_true",
        help="Re-score a completed candidate-index after a threshold/profile calibration; never synthesizes new audio",
    )
    parser.add_argument(
        "--skip-candidate-alignment",
        action="store_true",
        help="Diagnostic only: select on acoustic gates before ASR coverage is available",
    )
    args = parser.parse_args()
    if not 1 <= args.candidate_count <= 5:
        raise ValueError("candidate-count is the maximum candidate limit and must be 1-5")

    SERIES_PATH = args.series.expanduser().resolve()
    if not SERIES_PATH.is_file():
        raise FileNotFoundError(f"series file not found: {SERIES_PATH}")
    SERIES = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
    ep = str(args.episode).zfill(2)
    project = resolve_project(args.project, SERIES, SERIES_PATH, ep)
    PIPELINE_PROFILE, RESOLVED_PROFILE_PATH = load_resolved_profile(
        args.profile, project, required=True
    )
    TARGET_CPM = float(
        SERIES.get(
            "target_effective_chars_per_minute",
            get_in(PIPELINE_PROFILE, "voice.target_effective_chinese_chars_per_minute"),
        )
    )
    ENDING_CTA = str(
        SERIES.get("ending_cta", get_in(PIPELINE_PROFILE, "episode.final_cta", "")) or ""
    ).strip()
    prompt_value = SERIES.get(
        "voice_prompt_manifest",
        get_in(PIPELINE_PROFILE, "voice.voice_prompt_manifest"),
    )
    model_value = args.model or get_in(PIPELINE_PROFILE, "tts_runtime.model_path")
    aligner_value = args.aligner_python or get_in(PIPELINE_PROFILE, "tts_runtime.aligner_python")
    if not prompt_value or not model_value or not aligner_value:
        raise ValueError(
            "prompt manifest, model path, and aligner Python must come from series, CLI, or resolved profile"
        )
    PROMPT_MANIFEST = Path(str(prompt_value)).expanduser().absolute()
    MODEL = Path(str(model_value)).expanduser().absolute()
    MODEL_CONFIG = MODEL / "config.json"
    # Keep the virtualenv launcher path intact. Path.resolve() follows the
    # symlink to the base interpreter and silently drops the venv site-packages
    # (including mlx_audio), which makes forced alignment fail.
    ALIGNER_PYTHON = Path(str(aligner_value)).expanduser().absolute()
    stability_value = args.stability_profile or get_in(
        PIPELINE_PROFILE,
        "voice.voice_stability.profile",
        "references/voice-stability-profile.json",
    )
    PROFILE_PATH = Path(str(stability_value)).expanduser()
    if not PROFILE_PATH.is_absolute():
        PROFILE_PATH = PIPELINE / PROFILE_PATH
    args.target_cpm = float(args.target_cpm if args.target_cpm is not None else TARGET_CPM)
    if not MODEL_CONFIG.is_file():
        raise FileNotFoundError(f"VoxCPM2 model config not found: {MODEL_CONFIG}")

    subprocess.run(
        [
            sys.executable,
            str(EPISODE_VALIDATOR),
            "--series",
            str(SERIES_PATH),
            "--profile",
            str(RESOLVED_PROFILE_PATH),
        ],
        check=True,
    )
    episode = next(
        (item for item in SERIES["episodes"] if int(item["episode"]) == args.episode),
        None,
    )
    if episode is None:
        raise ValueError(f"episode not found: {args.episode}")
    scenes = scenes_with_ending_cta(episode)
    prosody = load_prosody(project, scenes)
    prompt = load_prompt()
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    profile_sha = sha256(PROFILE_PATH)
    prompt_wav = Path(prompt["prompt_wav_path"])
    prompt_text = str(prompt["prompt_text"])
    episode_text = "".join(str(scene["text"]).strip() for scene in scenes)
    char_count = visible(episode_text)
    allowed = allowed_cpm_range(args.target_cpm)

    candidate_dir = project / "audio/raw/candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    prompt_metrics = baseline_metrics(prompt_wav, profile)
    with exclusive_tts_lock():
        candidates, candidate_index, candidate_index_path, sample_rate = prepare_candidates(
            args,
            project,
            candidate_dir,
            episode_text,
            char_count,
            prompt,
            prompt_metrics,
            profile,
            profile_sha,
            allowed,
        )
    atomic_json(candidate_index_path, candidate_index)

    if not args.skip_candidate_alignment:
        if not ALIGNER_PYTHON.is_file():
            raise FileNotFoundError(f"candidate alignment Python not found: {ALIGNER_PYTHON}")
        text_path = candidate_dir / "episode-text.txt"
        text_path.write_text(episode_text + "\n", encoding="utf-8")
        for row in candidates:
            if row["acoustic_gate"] != "pass":
                continue
            if row.get("alignment_gate") in {"pass", "fail", "diagnostic_skip"}:
                continue
            align_candidate(project, row, text_path)
        candidate_index["candidates"] = candidates
        atomic_json(candidate_index_path, candidate_index)
    else:
        for row in candidates:
            if row["acoustic_gate"] == "pass":
                row["alignment_gate"] = "diagnostic_skip"
        candidate_index["candidates"] = candidates
        atomic_json(candidate_index_path, candidate_index)

    eligible = [
        row
        for row in candidates
        if row["acoustic_gate"] == "pass"
        and row["alignment_gate"] in {"pass", "diagnostic_skip"}
    ]
    if not eligible:
        raise RuntimeError(
            f"all {len(candidates)} generated continuous candidates failed; inspect {candidate_index_path}"
        )
    selected = min(eligible, key=lambda item: float(item["selection_score"]))
    candidate_index["selected_seed"] = selected["actual_seed"]
    candidate_index["selected_candidate_path"] = selected["path"]
    candidate_index["selection_status"] = (
        "acoustic_and_alignment_pass"
        if selected["alignment_gate"] == "pass"
        else "diagnostic_alignment_skip"
    )
    atomic_json(candidate_index_path, candidate_index)
    selected_path = project / selected["path"]
    retime_factor = float(selected["required_global_retime_factor"])
    leveled = project / "audio/raw/processed/episode-continuous-leveled.wav"
    leveling = slow_speech_gain_ride(selected_path, leveled, profile)
    atomic_json(project / "audio/leveling-plan.json", leveling)
    master = project / "audio/output/narration_master.wav"
    master.parent.mkdir(parents=True, exist_ok=True)
    normalization = measured_two_pass_master(
        leveled, master, sample_rate, retime_factor, profile
    )
    master_duration = duration(master)
    final_cpm = char_count * 60 / master_duration
    timeline = provisional_timeline(
        episode,
        scenes,
        master_duration,
        float(selected["raw_effective_cpm"]),
        final_cpm,
        retime_factor,
        args.target_cpm,
        allowed,
        int(selected["actual_seed"]),
        profile_sha,
    )
    timeline["sample_rate"] = sample_rate
    timeline["pipeline_profile"] = {
        "id": PIPELINE_PROFILE.get("profile_id"),
        "sha256": get_in(PIPELINE_PROFILE, "_meta.profile_sha256"),
    }
    timeline_path = project / "audio/timeline.json"
    atomic_json(timeline_path, timeline)

    manifest = {
        "schema_version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "engine": "VoxCPM2",
        "generation_mode": "continuous_episode_take",
        "clone_mode": "voxcpm2_ultimate_cloning",
        "model_path": str(MODEL),
        "model_revision": candidate_index.get("model_fingerprint_sha256"),
        "model_fingerprint_sha256": candidate_index.get("model_fingerprint_sha256"),
        "model_config_sha256": sha256(MODEL_CONFIG),
        "generator_sha256": sha256(Path(__file__).resolve()),
        "cfg_value": CFG_VALUE,
        "inference_timesteps": INFERENCE_TIMESTEPS,
        "candidate_strategy": "adaptive_sequential" if args.adaptive_candidates else "fixed_batch",
        "candidate_limit": args.candidate_count,
        "candidate_count": len(candidates),
        "selected_seed": int(selected["actual_seed"]),
        "selected_candidate_path": selected["path"],
        "selected_candidate_sha256": selected["sha256"],
        "candidate_index_path": str(candidate_index_path.relative_to(project)),
        "candidate_index_sha256": sha256(candidate_index_path),
        "global_retime_factor": round(retime_factor, 8),
        "target_cpm": args.target_cpm,
        "allowed_cpm_range": allowed,
        "raw_effective_cpm": selected["raw_effective_cpm"],
        "episode_effective_cpm": round(final_cpm, 3),
        "prompt": {
            "path": str(prompt_wav),
            "sha256": sha256(prompt_wav),
            "text": prompt_text,
            "manifest_path": str(PROMPT_MANIFEST),
            "manifest_sha256": sha256(PROMPT_MANIFEST),
        },
        "reference": {"path": str(prompt_wav), "sha256": sha256(prompt_wav)},
        "reference_source_path": prompt["source_path"],
        "reference_source_sha256": prompt["source_sha256"],
        "prosody_path": "audio/prosody.json",
        "prosody_sha256": sha256(project / "audio/prosody.json"),
        "profile": {
            "path": str(PROFILE_PATH),
            "sha256": profile_sha,
            "profile_id": profile.get("profile_id"),
        },
        "pipeline_profile": {
            "path": str(RESOLVED_PROFILE_PATH),
            "sha256": get_in(PIPELINE_PROFILE, "_meta.profile_sha256"),
            "profile_id": PIPELINE_PROFILE.get("profile_id"),
        },
        "normalization": normalization,
        "leveling": leveling,
        "normalization_scope": normalization["scope"],
        "master_path": str(master.relative_to(project)),
        "master_sha256": sha256(master),
        "duration_s": round(master_duration, 6),
        "sample_rate": sample_rate,
        "channels": 1,
        "alignment_status": "required_before_voice_qc_or_render",
    }
    atomic_json(project / "audio/voice-manifest.json", manifest)
    atomic_json(
        project / "audio/alignment-required.json",
        {
            "status": "required",
            "reason": "continuous take must be forced-aligned before voice QC or rendering",
            "master_sha256": manifest["master_sha256"],
            "timeline_sha256": sha256(timeline_path),
            "command": (
                f"{ALIGNER_PYTHON} {ALIGNER_SCRIPT} --series {SERIES_PATH} "
                f"--project {project} --episode {args.episode} --source-master "
                f"--timings {project / 'pipeline-timings.json'}"
            ),
        },
    )
    print(
        json.dumps(
            {
                "event": "continuous_take_selected",
                "episode": args.episode,
                "selected_seed": manifest["selected_seed"],
                "global_retime_factor": manifest["global_retime_factor"],
                "episode_effective_cpm": manifest["episode_effective_cpm"],
                "master": str(master),
                "next": "run full-episode forced alignment",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
