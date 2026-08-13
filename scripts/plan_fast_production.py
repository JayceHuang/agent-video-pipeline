#!/usr/bin/env python3
"""Plan an incremental video build before expensive TTS/image/render work.

The planner is read-only except for its JSON report. It validates the requested
CPM up front, detects reusable artifacts by content hash/QC, distinguishes the
cheap "reuse raw candidates, rescore/retime/realign only" path from full TTS,
calibrates stage estimates from the project's own timing history, and models
that illustration and TTS run in parallel.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path
from typing import Any

from profile_config import get_in, load_resolved_profile

# Participates in cache keys instead of the script file SHA; bump only when
# planning logic meaningfully changes.
LOGIC_VERSION = 2

PUNCT = set("，。！？；：、,.!?;:（）()【】[]《》<>—…·‘’“” \t\r\n-")

DEFAULT_ESTIMATES_S = {
    "visual_assets": 600,
    "tts": 240,
    "alignment_audio_qc": 120,
    "rescore_realign": 60,
    "motion_layout_check": 240,
    "render": 60,
    "delivery": 60,
}

# Substrings used to map pipeline-timings stage names onto planner buckets.
STAGE_KEYWORDS = {
    "visual_assets": ("illustration", "visual", "imagegen", "插图"),
    "tts": ("tts", "voxcpm", "candidate", "语音"),
    "alignment_audio_qc": ("align", "boundary", "stability", "audio_qc", "对齐"),
    "motion_layout_check": ("motion", "layout", "storyboard", "hyperframes", "check", "动效", "布局"),
    "render": ("render", "渲染"),
    "delivery": ("delivery", "交付", "finalize"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def narration_text(scenes: dict[str, Any]) -> str:
    rows = scenes.get("scenes", [])
    return "".join(str(row.get("text", "")) for row in rows if isinstance(row, dict))


def scene_budget(scenes: dict[str, Any], target_cpm: float) -> list[dict[str, Any]]:
    """Per-scene effective chars + predicted seconds, to catch over-long scenes
    at the script stage instead of after an expensive TTS take."""
    rows = []
    for index, row in enumerate(scenes.get("scenes", [])):
        if not isinstance(row, dict):
            continue
        chars = effective_chars(str(row.get("text", "")))
        rows.append(
            {
                "id": str(row.get("id", f"scene-{index + 1}")),
                "effective_chars": chars,
                "predicted_s": round(chars * 60.0 / target_cpm, 1),
            }
        )
    return rows


def effective_chars(text: str) -> int:
    return sum(1 for char in text if char not in PUNCT and not char.isspace())


def profile_cpm_ranges(profile: dict[str, Any]) -> list[tuple[float, tuple[float, float]]]:
    """Read configured nominal CPM and acceptance ranges."""
    pairs: list[tuple[float, tuple[float, float]]] = []
    target = get_in(profile, "voice.target_effective_chinese_chars_per_minute")
    allowed = get_in(profile, "voice.allowed_range")
    if isinstance(target, (int, float)) and isinstance(allowed, list) and len(allowed) == 2:
        pairs.append((float(target), (float(allowed[0]), float(allowed[1]))))
    fast = get_in(profile, "voice.fast_trial", {})
    if isinstance(fast, dict):
        target = fast.get("nominal_target_effective_chinese_chars_per_minute")
        allowed = fast.get("allowed_range")
        if isinstance(target, (int, float)) and isinstance(allowed, list) and len(allowed) == 2:
            pairs.append((float(target), (float(allowed[0]), float(allowed[1]))))
    return pairs


def allowed_range_for(target: float, profile: dict[str, Any]) -> tuple[tuple[float, float], str]:
    for nominal, allowed in profile_cpm_ranges(profile):
        if abs(target - nominal) < 1e-6:
            return allowed, f"profile range for nominal {nominal:.0f}"
    fallback = (target - 10.0, target + 10.0) if target >= 320 else (target - 5.0, target + 5.0)
    return fallback, "fallback heuristic (target not declared in profile)"


def history_estimates(project: Path) -> dict[str, float]:
    """Median non-cached successful elapsed per planner bucket from pipeline-timings.json."""
    doc = load(project / "pipeline-timings.json")
    samples: dict[str, list[float]] = {key: [] for key in STAGE_KEYWORDS}
    for event in doc.get("events", []):
        if not isinstance(event, dict) or event.get("cache_hit") or event.get("status") != "pass":
            continue
        stage = str(event.get("stage", "")).lower()
        elapsed = event.get("elapsed_s")
        if not isinstance(elapsed, (int, float)) or elapsed <= 0:
            continue
        for bucket, keywords in STAGE_KEYWORDS.items():
            if any(keyword in stage for keyword in keywords):
                samples[bucket].append(float(elapsed))
                break
    return {bucket: statistics.median(rows) for bucket, rows in samples.items() if rows}


def visual_cache(project: Path, profile: dict[str, Any]) -> tuple[bool, str]:
    policy = get_in(profile, "layout.illustration_skill", {})
    policy = policy if isinstance(policy, dict) else {}
    if not policy.get("enabled") and not policy.get("required"):
        return True, "visual provider disabled by resolved profile"
    doc = load(project / "visual-assets.json")
    assets = doc.get("assets", [])
    minimum = int((policy.get("asset_count_range") or [0, 999])[0])
    if doc.get("skill_invoked") is not True or len(assets) < minimum:
        return False, f"missing approved visual asset set (minimum {minimum})"
    for row in assets:
        path = project / str(row.get("path", ""))
        if not path.is_file() or row.get("sha256") != sha256(path):
            return False, f"visual asset missing or stale: {path.name}"
    return True, "asset paths and hashes match"


def raw_candidate_cache(project: Path, source_text: str) -> tuple[bool, str]:
    """Raw WAV reuse is legal whenever text/prosody/prompt inputs are unchanged,
    even if the previous master targeted a different CPM."""
    candidate_index = load(project / "audio/raw/candidate-index.json")
    if not candidate_index:
        return False, "no raw candidate index"
    expected_text_sha = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    if candidate_index.get("text_sha256") != expected_text_sha:
        return False, "raw candidates were generated from different narration text"
    prosody_path = project / "audio/prosody.json"
    recorded_prosody = candidate_index.get("prosody_sha256")
    if recorded_prosody and (not prosody_path.is_file() or recorded_prosody != sha256(prosody_path)):
        return False, "raw candidates bound to a different prosody.json"
    rows = candidate_index.get("candidates", [])
    if isinstance(rows, list) and rows:
        for row in rows:
            path = project / str(row.get("path", ""))
            recorded = row.get("sha256")
            if recorded and (not path.is_file() or recorded != sha256(path)):
                return False, f"raw candidate missing or stale: {path.name}"
    return True, "raw candidate WAVs reusable; only rescoring/retime/alignment needed"


def audio_cache(
    project: Path,
    allowed: tuple[float, float],
    source_text: str,
) -> tuple[bool, str]:
    master = project / "audio/output/narration_master.wav"
    timeline = load(project / "audio/timeline.json")
    manifest = load(project / "audio/voice-manifest.json")
    candidate_index = load(project / "audio/raw/candidate-index.json")
    boundary = load(project / "audio/boundary-qc.json")
    stability = load(project / "audio/voice-stability-qc.json")
    if not master.is_file():
        return False, "narration master missing"
    expected_text_sha = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    if candidate_index.get("text_sha256") != expected_text_sha:
        return False, "cached candidate text differs from current scenes narration"
    prosody_path = project / "audio/prosody.json"
    if not prosody_path.is_file() or manifest.get("prosody_sha256") != sha256(prosody_path):
        return False, "voice manifest prosody hash is stale"
    if timeline.get("alignment_status") != "forced_aligned":
        return False, "master has not completed forced alignment"
    try:
        cpm = float(timeline.get("episode_effective_cpm", timeline.get("effective_cpm", 0.0)))
    except (TypeError, ValueError):
        return False, "timeline has no readable effective CPM"
    if not allowed[0] <= cpm <= allowed[1]:
        return False, f"cached CPM {cpm:.2f} outside requested {allowed[0]:.0f}-{allowed[1]:.0f}"
    current = sha256(master)
    if manifest.get("master_sha256") != current:
        return False, "voice manifest master hash is stale"
    if boundary.get("status") != "pass" or boundary.get("master", {}).get("sha256") != current:
        return False, "boundary QC missing or stale"
    if stability.get("status") != "pass" or stability.get("audio", {}).get("sha256") != current:
        return False, "voice-stability QC missing or stale"
    return True, f"forced-aligned master passes QC at {cpm:.2f} CPM"


def motion_cache(project: Path) -> tuple[bool, str]:
    plan_path = project / ".hyperframes/semantic-motion.json"
    plan = load(plan_path)
    qc = load(project / ".hyperframes/motion-qc.json")
    if plan.get("status") != "approved" or qc.get("status") != "pass":
        return False, "approved motion plan/QC missing"
    if qc.get("plan", {}).get("sha256") != sha256(plan_path):
        return False, "motion QC is stale"
    for row in plan.get("sources", {}).values():
        path = Path(str(row.get("path", ""))).expanduser()
        if not path.is_file() or row.get("sha256") != sha256(path):
            return False, f"motion source stale: {path.name}"
    return True, "approved plan and source hashes match"


def layout_cache(project: Path) -> tuple[bool, str]:
    layout_path = project / ".hyperframes/layout-boxes.json"
    plan_path = project / ".hyperframes/semantic-motion.json"
    layout = load(layout_path)
    qc = load(project / ".hyperframes/layout-qc.json")
    if layout.get("status") != "approved" or layout.get("actual_dom_verified") is not True:
        return False, "DOM-verified layout is missing"
    if layout.get("motion_plan", {}).get("sha256") != sha256(plan_path):
        return False, "layout plan binding is stale"
    if qc.get("status") != "pass" or qc.get("layout", {}).get("sha256") != sha256(layout_path):
        return False, "layout QC is missing or stale"
    return True, "approved swept boxes and QC match"


def render_cache(project: Path) -> tuple[bool, str]:
    candidates = [project / "renders/final.mp4", *sorted((project / "renders").glob("*.mp4"))]
    video = next((path for path in candidates if path.is_file()), None)
    qc = load(project / "renders/qc-report.json")
    if not video or qc.get("status") != "pass":
        return False, "validated final render missing"
    manifest = load(project / "renders/asset-manifest.json")
    if manifest.get("video", {}).get("sha256") != sha256(video):
        return False, "render manifest is stale"
    return True, f"validated render reusable: {video.name}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--target-cpm", type=float, required=True)
    parser.add_argument("--duration-target-s", type=float)
    parser.add_argument("--profile", type=Path, help="resolved profile JSON")
    parser.add_argument("--output", default=".pipeline/fast-production-plan.json")
    args = parser.parse_args()
    project = args.project.expanduser().resolve()
    profile, profile_path = load_resolved_profile(args.profile, project, required=True)
    scenes_path = project / "scenes.json"
    scenes = load(scenes_path)
    text = narration_text(scenes)
    chars = effective_chars(text)
    if not chars:
        raise ValueError("scenes.json has no narration text")
    target = float(args.target_cpm)
    allowed, allowed_source = allowed_range_for(target, profile)
    predicted = chars * 60.0 / target

    visual_ok, visual_reason = visual_cache(project, profile)
    audio_ok, audio_reason = audio_cache(project, allowed, text)
    raw_ok, raw_reason = raw_candidate_cache(project, text)
    motion_ok, motion_reason = motion_cache(project)
    layout_ok, layout_reason = layout_cache(project)
    render_ok, render_reason = render_cache(project)

    # Cheap audio path: full audio cache missed (e.g. CPM retarget) but raw
    # candidates are still bound to identical text/prosody -> no VoxCPM2 call.
    if audio_ok:
        tts_decision = "reuse"
    elif raw_ok:
        tts_decision = "reuse_raw_rescore_retime_realign"
    else:
        tts_decision = "run_one_then_expand_on_failure"

    decisions = {
        "script": "reuse" if scenes_path.is_file() else "run",
        "visual_assets": "reuse" if visual_ok else "run",
        "tts": tts_decision,
        "forced_alignment": "reuse" if audio_ok else "run",
        "audio_qc": "reuse" if audio_ok else "run_once_then_single_stabilization_max",
        "semantic_motion": "reuse" if motion_ok and visual_ok and audio_ok else "run",
        "layout_and_storyboard": "reuse" if layout_ok and motion_ok and visual_ok and audio_ok else "run",
        "render": "reuse" if render_ok and layout_ok and motion_ok and visual_ok and audio_ok else "run",
        "delivery": "run_if_render_or_delivery_manifest_changed",
    }

    history = history_estimates(project)
    defaults = DEFAULT_ESTIMATES_S

    def estimate(bucket: str, cached: bool, cached_s: float) -> tuple[float, str]:
        if cached:
            return cached_s, "cache"
        if bucket in history:
            return round(history[bucket], 1), "project timing history (median)"
        return float(defaults[bucket]), "default"

    visual_s, visual_src = estimate("visual_assets", visual_ok, 20)
    if audio_ok:
        tts_s, tts_src = 10.0, "cache"
        align_s, align_src = 10.0, "cache"
    elif raw_ok:
        tts_s, tts_src = estimate("rescore_realign", False, 0)
        align_s, align_src = 0.0, "included in rescore_realign"
    else:
        tts_s, tts_src = estimate("tts", False, 0)
        align_s, align_src = estimate("alignment_audio_qc", False, 0)
    motion_s, motion_src = estimate("motion_layout_check", layout_ok and motion_ok, 20)
    render_s, render_src = estimate("render", render_ok, 5)
    delivery_s, delivery_src = estimate("delivery", False, 0)

    # Illustration and TTS(+alignment) are independent and may run in parallel;
    # wall clock is the slower branch plus the strictly serial downstream stages.
    audio_branch_s = tts_s + align_s
    wall_clock_s = max(visual_s, audio_branch_s) + motion_s + render_s + delivery_s
    compute_s = visual_s + audio_branch_s + motion_s + render_s + delivery_s

    report = {
        "schema_version": 2,
        "logic_version": LOGIC_VERSION,
        "project": str(project),
        "profile": {
            "path": str(profile_path),
            "id": profile.get("profile_id"),
            "sha256": get_in(profile, "_meta.profile_sha256"),
        },
        "target_cpm": target,
        "allowed_cpm_range": list(allowed),
        "allowed_cpm_range_source": allowed_source,
        "effective_chars": chars,
        "predicted_narration_duration_s": round(predicted, 2),
        "scene_budget": scene_budget(scenes, target),
        "duration_target_s": args.duration_target_s,
        "duration_delta_s": round(predicted - args.duration_target_s, 2) if args.duration_target_s else None,
        "cache": {
            "visual_assets": {"valid": visual_ok, "reason": visual_reason},
            "audio": {"valid": audio_ok, "reason": audio_reason},
            "raw_candidates": {"valid": raw_ok, "reason": raw_reason},
            "semantic_motion": {"valid": motion_ok, "reason": motion_reason},
            "layout": {"valid": layout_ok, "reason": layout_reason},
            "render": {"valid": render_ok, "reason": render_reason},
        },
        "decisions": decisions,
        "estimated_wall_clock_s": round(wall_clock_s, 1),
        "estimated_total_compute_s": round(compute_s, 1),
        "estimates": {
            "visual_assets_s": visual_s,
            "tts_s": tts_s,
            "alignment_audio_qc_s": align_s,
            "motion_layout_check_s": motion_s,
            "render_s": render_s,
            "delivery_s": delivery_s,
        },
        "estimate_sources": {
            "visual_assets": visual_src,
            "tts": tts_src,
            "alignment_audio_qc": align_src,
            "motion_layout_check": motion_src,
            "render": render_src,
            "delivery": delivery_src,
        },
        "parallelism": {
            "visual_and_audio_branches_parallel": True,
            "wall_clock_model": "max(visual, tts+alignment) + motion + render + delivery",
        },
        "hard_rules": {
            "tts_candidates": "start with one; expand only after failed acoustic/alignment early-stop",
            "aligned_stabilization_max_runs": 1,
            "hyperframes_full_check_max_runs_before_render": 1,
            "reuse_raw_candidates_when_only_target_cpm_changes": True,
        },
    }
    output = Path(args.output).expanduser()
    output = output if output.is_absolute() else project / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
