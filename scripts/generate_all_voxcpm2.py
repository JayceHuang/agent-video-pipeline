#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from voxcpm import VoxCPM

from profile_config import get_in, load_resolved_profile


ROOT = Path(__file__).resolve().parent  # <skill>/scripts
CONTINUOUS_GENERATOR = ROOT / "generate_voxcpm2_continuous.py"
EPISODE_VALIDATOR = ROOT / "validate_episode_independence.py"
BOUNDARY_STABILIZER = ROOT / "stabilize_audio_boundaries.py"


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


MODEL: Path | None = None
DEFAULT_ALIGNER_PYTHON: Path | None = None
RESOLVED_PROFILE_PATH: Path | None = None
PIPELINE_PROFILE: dict = {}
SERIES: dict = {}
SERIES_PATH: Path | None = None
REFERENCE_SOURCE: Path | None = None
REFERENCE: Path | None = None
REFERENCE_EXTRACT: dict = {}
TARGET_CPM = 0
ENDING_CTA = ""
SEED_BASE = 2026080603
IGNORED = set(" \t\n，。！？；：、,.!?;:“”‘’（）()—｜")
STABLE_TARGET_TOLERANCE_CPM = 10.0
STABLE_RETIME_MIN = 0.90
STABLE_RETIME_MAX = 1.10
STABLE_GENERATION_ATTEMPTS = 3
MASTER_STABILITY_FILTER = (
    "acompressor=threshold=-24dB:ratio=1.6:attack=20:release=180:makeup=1.5:mix=1,"
    "loudnorm=I=-16:TP=-2:LRA=5:linear=true"
)
FIXED_TONE_STYLE = ""


def scenes_with_ending_cta(episode: dict) -> list[dict]:
    """Return episode scenes with the series CTA present exactly once."""
    scenes = [dict(scene) for scene in episode["scenes"]]
    if not ENDING_CTA or not scenes:
        return scenes
    last = scenes[-1]
    text = str(last.get("text", "")).rstrip()
    if not text.endswith(ENDING_CTA):
        last["text"] = f"{text}{ENDING_CTA}"
    return scenes


def run_ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def validate_episode_independence() -> None:
    """Stop TTS before model loading if the source is not a standalone episode set."""
    if not EPISODE_VALIDATOR.is_file():
        raise FileNotFoundError(f"episode independence validator not found: {EPISODE_VALIDATOR}")
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


def ensure_reference_audio() -> None:
    if REFERENCE_SOURCE is None or REFERENCE is None or not REFERENCE_EXTRACT:
        raise ValueError("voice reference settings are required for legacy-scene generation")
    if not REFERENCE_SOURCE.is_file():
        raise FileNotFoundError(f"voice reference source not found: {REFERENCE_SOURCE}")
    refresh = not REFERENCE.is_file() or REFERENCE_SOURCE.stat().st_mtime_ns > REFERENCE.stat().st_mtime_ns
    if refresh:
        REFERENCE.parent.mkdir(parents=True, exist_ok=True)
        run_ffmpeg([
            "-ss", str(REFERENCE_EXTRACT["start_s"]),
            "-i", str(REFERENCE_SOURCE),
            "-t", str(REFERENCE_EXTRACT["duration_s"]),
            "-ar", str(REFERENCE_EXTRACT["sample_rate"]),
            "-ac", str(REFERENCE_EXTRACT["channels"]),
            "-c:a", str(REFERENCE_EXTRACT["codec"]),
            str(REFERENCE),
        ])
    if not REFERENCE.is_file():
        raise FileNotFoundError(f"derived VoxCPM2 reference WAV not found: {REFERENCE}")


def duration(path: Path) -> float:
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)], text=True).strip())


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def fade(samples: np.ndarray, sample_rate: int, ms: float = 24.0) -> np.ndarray:
    count = min(int(sample_rate * ms / 1000), samples.size // 3)
    if count < 2:
        return samples
    curve = np.sin(np.linspace(0, np.pi / 2, count, dtype=np.float32)) ** 2
    out = samples.copy()
    out[:count] *= curve
    out[-count:] *= curve[::-1]
    return out


def visible(text: str) -> int:
    return sum(1 for char in text if char not in IGNORED)


def load_approved_prosody(project: Path) -> dict[str, dict]:
    path = project / "audio/prosody.json"
    if not path.is_file():
        raise FileNotFoundError(f"approved prosody file required before TTS: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("status") != "approved":
        raise ValueError(f"prosody gate failed: {path} status={data.get('status')!r}")
    scenes = data.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError(f"prosody gate failed: {path} has no scenes")
    return {str(scene["id"]): scene for scene in scenes}


def allowed_cpm_range(target_cpm: float, stability_mode: str) -> list[float]:
    """Return the acceptance band; explicit fast trials use a soft target."""
    nominal = float(get_in(PIPELINE_PROFILE, "voice.target_effective_chinese_chars_per_minute", target_cpm))
    configured = get_in(PIPELINE_PROFILE, "voice.allowed_range", [target_cpm - 10, target_cpm + 10])
    if abs(target_cpm - nominal) < 1e-6:
        return [float(configured[0]), float(configured[1])]
    fast = get_in(PIPELINE_PROFILE, "voice.fast_trial", {})
    if isinstance(fast, dict) and abs(target_cpm - float(fast.get("nominal_target_effective_chinese_chars_per_minute", -1))) < 1e-6:
        allowed = fast.get("allowed_range", [target_cpm - 10, target_cpm + 10])
        return [float(allowed[0]), float(allowed[1])]
    if stability_mode == "stable":
        return [target_cpm - STABLE_TARGET_TOLERANCE_CPM, target_cpm + STABLE_TARGET_TOLERANCE_CPM]
    return [target_cpm - 5.0, target_cpm + 5.0]


def pace_hint(target_cpm: float, allowed: list[float], attempt: int, raw_cpm: float | None = None) -> str:
    """Give VoxCPM2 a conservative pace hint without forcing an integer speed."""
    hint = (
        f"口播速度以每分钟约{target_cpm:.0f}个有效字为参考，允许自然浮动在"
        f"{allowed[0]:.0f}到{allowed[1]:.0f}之间；不要赶读或快于上限，标点处保留自然停顿；"
        "优先保持稳定，不要为了追求整数速度而忽快忽慢。"
    )
    if attempt == 0 or raw_cpm is None:
        return hint
    direction = "放慢" if raw_cpm > target_cpm else "放快"
    return f"{hint}上一版约{raw_cpm:.0f}字/分钟，当前请自然{direction}一点，但不要改变音量和音高。"


def stabilize_master(master: Path, sample_rate: int) -> None:
    """Apply one conservative full-episode dynamics pass after scene assembly."""
    partial = master.with_name(master.stem + ".stabilized.part.wav")
    run_ffmpeg([
        "-i", str(master),
        "-filter:a", MASTER_STABILITY_FILTER,
        "-ar", str(sample_rate), "-ac", "1", "-c:a", "pcm_s24le",
        str(partial),
    ])
    partial.replace(master)


def style_for_scene(scene: dict, preset: str, prosody_scene: dict | None = None) -> str:
    if preset != "fixed":
        raise ValueError("tone switching has been removed; use the fixed tone preset")
    if prosody_scene is None:
        raise ValueError(f"missing approved prosody for {scene['id']}")
    return FIXED_TONE_STYLE


def target_for_scene(scene: dict, preset: str, fallback: float) -> float:
    # Tone is fixed for every scene. Speed remains independently configurable;
    # an explicit --target-cpm override is never silently changed.
    return fallback


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["episode-take", "legacy-scene"],
        default="episode-take",
        help="production default is one continuous acoustic take; legacy scene mode is explicit",
    )
    parser.add_argument("--series", type=Path, required=True)
    parser.add_argument("--project", type=Path, help="Explicit episode project directory for production mode")
    parser.add_argument("--model", type=Path)
    parser.add_argument("--aligner-python", type=Path)
    parser.add_argument("--profile", type=Path, help="resolved profile JSON")
    parser.add_argument("--candidate-count", type=int, default=3)
    parser.add_argument(
        "--fixed-candidate-batch",
        action="store_true",
        help="Diagnostic/benchmark mode: generate the full candidate-count batch without early stop.",
    )
    parser.add_argument(
        "--reuse-candidates",
        action="store_true",
        help="Re-score completed continuous candidates after a profile calibration",
    )
    parser.add_argument("--skip-candidate-alignment", action="store_true")
    parser.add_argument("--episode", type=int, help="只生成指定集数")
    parser.add_argument("--scene-id", help="只生成指定场景的 raw 音频（需配合 --raw-only）")
    parser.add_argument("--raw-only", action="store_true", help="生成 raw scene WAV 后停止，不重建 master 时间轴")
    parser.add_argument(
        "--style-preset",
        choices=["fixed"],
        default="fixed",
        help="固定单一中性语气；场景/句子级语气切换已移除",
    )
    parser.add_argument("--target-cpm", type=float)
    parser.add_argument(
        "--seed-offset",
        type=int,
        default=0,
        help="固定语气重采样时改变确定性种子；不改变语气状态或时间轴规则",
    )
    parser.add_argument(
        "--stability-mode",
        choices=["stable", "legacy"],
        default="stable",
        help="stable 允许目标附近自然浮动并限制大幅变速；legacy 保留旧的精确目标行为",
    )
    parser.add_argument(
        "--allow-unstable-retime",
        action="store_true",
        help="仅在排查失败稿时允许超过稳定变速边界；默认拒绝可能造成大幅波动的音频",
    )
    parser.add_argument(
        "--allow-legacy-diagnostics",
        action="store_true",
        help="Required acknowledgment for the non-production legacy-scene path.",
    )
    args = parser.parse_args()

    global SERIES, SERIES_PATH, REFERENCE_SOURCE, REFERENCE, REFERENCE_EXTRACT
    global TARGET_CPM, ENDING_CTA, MODEL, DEFAULT_ALIGNER_PYTHON
    global RESOLVED_PROFILE_PATH, PIPELINE_PROFILE, FIXED_TONE_STYLE
    SERIES_PATH = args.series.expanduser().resolve()
    if not SERIES_PATH.is_file():
        raise FileNotFoundError(f"series file not found: {SERIES_PATH}")
    SERIES = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
    profile_project: Path | None = None
    if args.episode is not None:
        profile_project = resolve_project(
            args.project, SERIES, SERIES_PATH, str(args.episode).zfill(2)
        )
    PIPELINE_PROFILE, RESOLVED_PROFILE_PATH = load_resolved_profile(
        args.profile, profile_project, required=True
    )
    TARGET_CPM = int(
        SERIES.get(
            "target_effective_chars_per_minute",
            get_in(PIPELINE_PROFILE, "voice.target_effective_chinese_chars_per_minute"),
        )
    )
    ENDING_CTA = str(
        SERIES.get("ending_cta", get_in(PIPELINE_PROFILE, "episode.final_cta", "")) or ""
    ).strip()
    FIXED_TONE_STYLE = str(get_in(PIPELINE_PROFILE, "voice.style_instruction", "") or "").strip()
    if not FIXED_TONE_STYLE:
        raise ValueError("voice.style_instruction must come from the resolved profile")
    model_value = args.model or get_in(PIPELINE_PROFILE, "tts_runtime.model_path")
    aligner_value = args.aligner_python or get_in(PIPELINE_PROFILE, "tts_runtime.aligner_python")
    if not model_value or not aligner_value:
        raise ValueError("VoxCPM2 model and aligner Python must come from CLI or resolved profile")
    MODEL = Path(str(model_value)).expanduser().absolute()
    DEFAULT_ALIGNER_PYTHON = Path(str(aligner_value)).expanduser().absolute()
    args.model = MODEL
    args.aligner_python = DEFAULT_ALIGNER_PYTHON
    if "voice_reference_source" in SERIES:
        REFERENCE_SOURCE = Path(SERIES["voice_reference_source"])
        REFERENCE = Path(SERIES["voice_reference_wav"])
        REFERENCE_EXTRACT = dict(SERIES["voice_reference_extract"])
    else:
        source_value = get_in(PIPELINE_PROFILE, "voice.voice_reference_source")
        reference_value = get_in(PIPELINE_PROFILE, "voice.voice_reference_wav")
        extract_value = get_in(PIPELINE_PROFILE, "voice.voice_reference_extract")
        if source_value and reference_value and isinstance(extract_value, dict):
            REFERENCE_SOURCE = Path(str(source_value)).expanduser().absolute()
            REFERENCE = Path(str(reference_value)).expanduser().absolute()
            REFERENCE_EXTRACT = dict(extract_value)

    if args.mode == "episode-take":
        if args.episode is None:
            raise ValueError("episode-take mode requires --episode")
        if args.scene_id or args.raw_only or args.allow_unstable_retime:
            raise ValueError(
                "scene-id/raw-only/allow-unstable-retime are legacy-scene options and cannot be used in production mode"
            )
        command = [
            sys.executable,
            str(CONTINUOUS_GENERATOR),
            "--episode",
            str(args.episode),
            "--series",
            str(args.series.expanduser().resolve()),
            "--model",
            str(args.model.expanduser().resolve()),
            "--aligner-python",
            # Preserve the virtualenv launcher path. Resolving its symlink
            # jumps to the base Python and drops mlx_audio from site-packages.
            str(args.aligner_python.expanduser().absolute()),
            "--candidate-count",
            str(args.candidate_count),
            "--seed-offset",
            str(args.seed_offset),
            "--profile",
            str(RESOLVED_PROFILE_PATH),
        ]
        if args.target_cpm is not None:
            command.extend(["--target-cpm", str(args.target_cpm)])
        if args.project is not None:
            command.extend(["--project", str(args.project.expanduser().resolve())])
        if args.skip_candidate_alignment:
            command.append("--skip-candidate-alignment")
        if args.reuse_candidates:
            command.append("--reuse-candidates")
        if args.fixed_candidate_batch:
            command.append("--fixed-candidate-batch")
        subprocess.run(command, check=True)
        return

    if not args.allow_legacy_diagnostics:
        raise ValueError(
            "legacy-scene is non-production and requires --allow-legacy-diagnostics"
        )
    if args.target_cpm is None:
        args.target_cpm = TARGET_CPM

    validate_episode_independence()
    ensure_reference_audio()
    loaded = time.perf_counter()
    model = VoxCPM.from_pretrained(str(MODEL), load_denoiser=False, device="mps")
    sample_rate = int(model.tts_model.sample_rate)
    print(json.dumps({"event": "model_loaded", "seconds": round(time.perf_counter() - loaded, 3), "sample_rate": sample_rate}), flush=True)
    episodes = [item for item in SERIES["episodes"] if args.episode is None or item["episode"] == args.episode]
    if not episodes:
        raise ValueError(f"episode not found: {args.episode}")
    for episode in episodes:
        ep = str(episode["episode"]).zfill(2)
        project = resolve_project(args.project, SERIES, SERIES_PATH, ep)
        episode_scenes = scenes_with_ending_cta(episode)
        if args.scene_id:
            episode_scenes = [scene for scene in episode_scenes if scene["id"] == args.scene_id]
            if not episode_scenes:
                raise ValueError(f"scene not found in episode {episode['episode']}: {args.scene_id}")
        prosody_by_id = load_approved_prosody(project)
        raw_dir = project / "audio/raw/model"
        processed_dir = project / "audio/raw/processed"
        output_dir = project / "audio/output"
        for directory in [raw_dir, processed_dir, output_dir]:
            directory.mkdir(parents=True, exist_ok=True)
        raw_rows = []
        raw_durations = []
        raw_effective_cpms = []
        allowed_range = allowed_cpm_range(args.target_cpm, args.stability_mode)
        raw_safe_min = allowed_range[0] / STABLE_RETIME_MAX
        raw_safe_max = allowed_range[1] / STABLE_RETIME_MIN
        scene_target_map = {
            scene["id"]: target_for_scene(scene, args.style_preset, args.target_cpm)
            for scene in episode_scenes
        }
        for index, scene in enumerate(episode_scenes):
            started = time.perf_counter()
            style_instruction = style_for_scene(scene, args.style_preset, prosody_by_id.get(scene["id"]))
            char_count = visible(scene["text"])
            base_seed = SEED_BASE + args.seed_offset + episode["episode"] * 1000 + index * 103
            best = None
            last_raw_cpm = None
            max_attempts = STABLE_GENERATION_ATTEMPTS if args.stability_mode == "stable" else 1
            for attempt in range(max_attempts):
                hint = pace_hint(args.target_cpm, allowed_range, attempt, last_raw_cpm)
                seed = base_seed + attempt * 17
                wav = model.generate(
                    text=f"({style_instruction} {hint}){scene['text']}",
                    reference_wav_path=str(REFERENCE),
                    cfg_value=1.5,
                    inference_timesteps=20,
                    seed=seed,
                )
                candidate = np.asarray(wav, dtype=np.float32).reshape(-1)
                peak = float(np.max(np.abs(candidate))) if candidate.size else 0.0
                if peak > .86:
                    candidate *= .86 / peak
                candidate = fade(candidate, sample_rate, ms=40.0)
                raw_duration = candidate.size / sample_rate
                raw_cpm = char_count * 60 / raw_duration if raw_duration > 0 else 0.0
                distance = max(raw_safe_min - raw_cpm, 0.0, raw_cpm - raw_safe_max)
                if best is None or distance < best["distance"]:
                    best = {
                        "samples": candidate,
                        "peak": peak,
                        "raw_duration": raw_duration,
                        "raw_cpm": raw_cpm,
                        "attempts": attempt + 1,
                        "distance": distance,
                    }
                last_raw_cpm = raw_cpm
                if args.stability_mode != "stable" or distance <= 0:
                    break
            if best is None:
                raise RuntimeError(f"no audio candidate generated for {scene['id']}")
            if args.stability_mode == "stable" and best["distance"] > 0 and not args.allow_unstable_retime:
                raise RuntimeError(
                    f"stable voice gate failed for {scene['id']}: raw CPM {best['raw_cpm']:.1f}; "
                    f"expected {raw_safe_min:.1f}-{raw_safe_max:.1f} before bounded retime. "
                    "Regenerate the scene or pass --allow-unstable-retime only for diagnosis."
                )
            samples = best["samples"]
            peak = best["peak"]
            raw_duration = best["raw_duration"]
            raw_path = raw_dir / f"{scene['id']}.wav"
            sf.write(raw_path, samples, sample_rate, subtype="PCM_16")
            raw_durations.append(raw_duration)
            raw_effective_cpms.append(best["raw_cpm"])
            raw_rows.append({
                "id": scene["id"],
                "style_instruction": style_instruction,
                "style_preset": args.style_preset,
                "duration_s": round(raw_duration, 6),
                "raw_effective_cpm": round(best["raw_cpm"], 3),
                "peak_before_limit": round(peak, 6),
                "generation_attempts": best["attempts"],
                "generation_seconds": round(time.perf_counter() - started, 3),
            })
            print(json.dumps({"event": "scene_generated", "episode": episode["episode"], "scene": scene["id"], "raw_duration_s": round(raw_duration, 3), "raw_cpm": round(best["raw_cpm"], 1), "attempts": best["attempts"]}, ensure_ascii=False), flush=True)
        if args.raw_only:
            print(json.dumps({"event": "raw_scene_ready", "episode": episode["episode"], "scene": args.scene_id}, ensure_ascii=False), flush=True)
            continue
        tracks = []
        cursor = 0.0
        timeline_scenes = []
        scene_retime = []
        for index, scene in enumerate(episode_scenes):
            source = raw_dir / f"{scene['id']}.wav"
            target = processed_dir / f"{scene['id']}.wav"
            char_count = visible(scene["text"])
            requested_target_cpm = scene_target_map[scene["id"]]
            raw_cpm = raw_effective_cpms[index]
            if args.stability_mode == "stable":
                # Keep a naturally generated scene when it is already near the
                # requested speed; only move it to the nearest edge of the
                # soft band when it falls outside. This avoids making every
                # scene sound like it was forced to an identical integer CPM.
                target_cpm = min(max(raw_cpm, allowed_range[0]), allowed_range[1])
            else:
                target_cpm = requested_target_cpm
            target_scene_duration = char_count / target_cpm * 60
            raw_tempo = raw_durations[index] / target_scene_duration
            if args.stability_mode == "stable":
                if not STABLE_RETIME_MIN <= raw_tempo <= STABLE_RETIME_MAX and not args.allow_unstable_retime:
                    raise RuntimeError(
                        f"stable retime gate failed for {scene['id']}: factor {raw_tempo:.3f}; "
                        f"expected {STABLE_RETIME_MIN:.2f}-{STABLE_RETIME_MAX:.2f}"
                    )
                scene_tempo = max(STABLE_RETIME_MIN, min(STABLE_RETIME_MAX, raw_tempo))
            else:
                scene_tempo = max(.65, min(1.45, raw_tempo))
            filters = f"highpass=f=70,afftdn=nr=8:nf=-52:tn=1,equalizer=f=202:t=q:w=2:g=-4,alimiter=limit=0.88:attack=5:release=100:level=disabled,atempo={scene_tempo:.8f},loudnorm=I=-16:TP=-2:LRA=7:linear=true"
            run_ffmpeg(["-i", str(source), "-filter:a", filters, "-ar", str(sample_rate), "-ac", "1", "-c:a", "pcm_s24le", str(target)])
            samples, sr = sf.read(target, dtype="float32")
            if sr != sample_rate:
                raise RuntimeError(f"sample rate mismatch: {target}")
            tracks.append(np.asarray(samples, dtype=np.float32).reshape(-1))
            scene_duration = duration(target)
            effective_cpm = char_count * 60 / scene_duration
            start = cursor
            end = start + scene_duration
            timeline_style = style_for_scene(scene, args.style_preset, prosody_by_id.get(scene["id"]))
            timeline_scenes.append({**scene, "style": timeline_style, "style_preset": args.style_preset, "path": str(target.relative_to(project)), "start_s": round(start, 6), "end_s": round(end, 6), "duration_s": round(scene_duration, 6), "spoken_duration_s": round(scene_duration, 6), "effective_chars": char_count, "effective_cpm": round(effective_cpm, 3), "requested_target_cpm": requested_target_cpm, "selected_target_cpm": round(target_cpm, 3), "retime_factor": round(scene_tempo, 8)})
            scene_retime.append({"id": scene["id"], "raw_duration_s": round(raw_durations[index], 6), "raw_effective_cpm": round(raw_cpm, 3), "target_duration_s": round(target_scene_duration, 6), "requested_target_cpm": requested_target_cpm, "selected_target_cpm": round(target_cpm, 3), "retime_factor": round(scene_tempo, 8), "final_duration_s": round(scene_duration, 6), "effective_cpm": round(effective_cpm, 3)})
            cursor = end
            if index < len(episode_scenes) - 1:
                gap_samples = round(sample_rate * float(scene["gap_after_s"]))
                tracks.append(np.zeros(gap_samples, dtype=np.float32))
                cursor += gap_samples / sample_rate
        combined = np.concatenate(tracks)
        master = output_dir / "narration_master.wav"
        sf.write(master, combined, sample_rate, subtype="PCM_24")
        stabilize_master(master, sample_rate)
        stabilized_duration = duration(master)
        if abs(stabilized_duration - cursor) > 0.02:
            raise RuntimeError(
                f"master stabilization changed duration unexpectedly: {stabilized_duration:.6f} vs {cursor:.6f}"
            )
        timeline = {
            "title": episode["title"],
            "audio": "audio/output/narration_master.wav",
            "sample_rate": sample_rate,
            "channels": 1,
            "normalization_scope": "scene_then_master",
            "stability_mode": args.stability_mode,
            "stability_profile": {
                "target_cpm_is_nominal": True,
                "allowed_cpm_range": allowed_range,
                "retime_factor_range": [STABLE_RETIME_MIN, STABLE_RETIME_MAX],
                "master_filter": MASTER_STABILITY_FILTER,
            },
            "target_cpm": args.target_cpm,
            "seed_offset": args.seed_offset,
            "allowed_cpm_range": allowed_range,
            "speed_override": args.target_cpm != TARGET_CPM,
            "total_duration_s": round(cursor, 6),
            "scenes": timeline_scenes,
        }
        (project / "audio/timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest = {
            "engine": "VoxCPM2",
            "pipeline_profile": {
                "path": str(RESOLVED_PROFILE_PATH),
                "id": PIPELINE_PROFILE.get("profile_id"),
                "sha256": get_in(PIPELINE_PROFILE, "_meta.profile_sha256"),
            },
            "model_path": str(MODEL),
            "reference_source_path": str(REFERENCE_SOURCE),
            "reference_source_sha256": sha256(REFERENCE_SOURCE),
            "reference_path": str(REFERENCE),
            "reference_sha256": sha256(REFERENCE),
            "reference_duration_s": round(duration(REFERENCE), 6),
            "reference_extraction": REFERENCE_EXTRACT,
            "master_path": str(master.relative_to(project)),
            "master_sha256": sha256(master),
            "sample_rate": sample_rate,
            "channels": 1,
            "style_preset": args.style_preset,
            "style_instruction": FIXED_TONE_STYLE,
            "target_cpm": args.target_cpm,
            "seed_offset": args.seed_offset,
            "allowed_cpm_range": allowed_range,
            "speed_override": args.target_cpm != TARGET_CPM,
            "scene_targets_cpm": scene_target_map,
            "prosody_path": "audio/prosody.json",
            "prosody_status": "approved",
            "normalization_scope": "scene_then_master",
            "stability_mode": args.stability_mode,
            "stability_profile": {
                "target_cpm_is_nominal": True,
                "retime_factor_range": [STABLE_RETIME_MIN, STABLE_RETIME_MAX],
                "master_filter": MASTER_STABILITY_FILTER,
            },
            "duration_s": round(cursor, 6),
            "post_filter": "per-scene retime; highpass 70Hz; conservative denoise; 202Hz hum cut; limiter; conservative master compressor; loudnorm I=-16 TP=-2 LRA=5",
            "raw_scenes": raw_rows,
            "scene_retime": scene_retime,
        }
        (project / "audio/voice-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not BOUNDARY_STABILIZER.is_file():
            raise FileNotFoundError(f"audio boundary stabilizer not found: {BOUNDARY_STABILIZER}")
        subprocess.run(
            [
                sys.executable,
                str(BOUNDARY_STABILIZER),
                "--project",
                str(project),
                "--gap",
                "0.18",
                "--fade",
                "0.012",
            ],
            check=True,
        )
        stabilized_timeline = json.loads((project / "audio/timeline.json").read_text(encoding="utf-8"))
        print(json.dumps({"event": "episode_mastered", "episode": episode["episode"], "duration_s": stabilized_timeline["total_duration_s"], "normalization_scope": "scene_then_master", "stability_mode": args.stability_mode, "allowed_cpm_range": allowed_range, "boundary_qc": "audio/boundary-qc.json", "master": str(master)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
