#!/usr/bin/env python3
"""Validate a final video and the co-located publishing assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe(video: Path) -> dict[str, Any]:
    command = [
        "ffprobe", "-v", "error", "-of", "json",
        "-show_entries", "format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
        str(video),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    data = json.loads(result.stdout)
    return data


def fps(value: str | None) -> float:
    if not value or "/" not in value:
        return 0.0
    numerator, denominator = value.split("/", 1)
    return float(numerator) / float(denominator)


def loudness(video: Path) -> dict[str, float]:
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(video),
            "-filter:a", "loudnorm=I=-16:TP=-2:LRA=6:print_format=json",
            "-f", "null", "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    matches = re.findall(r'\{\s*"input_i".*?\}', result.stderr, flags=re.DOTALL)
    if not matches:
        raise RuntimeError("ffmpeg loudnorm did not return final-mix JSON")
    values = json.loads(matches[-1])
    return {
        "integrated_lufs": float(values["input_i"]),
        "true_peak_dbtp": float(values["input_tp"]),
        "lra_lu": float(values["input_lra"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--video", default="final.mp4")
    parser.add_argument("--max-duration", type=float, default=180.0)
    args = parser.parse_args()

    root = args.dir.expanduser().resolve()
    video = root / args.video
    errors: list[str] = []
    warnings: list[str] = []
    required = [
        "cover.png",
        "cover.jpg",
        "cover-description.md",
        "publishing-copy.md",
        "asset-manifest.json",
        "visual-assets.json",
        "semantic-motion.json",
        "motion-qc.json",
        "layout-boxes.json",
        "layout-qc.json",
        "alignment-qc.json",
    ]
    for name in required:
        if not (root / name).is_file():
            errors.append(f"missing {name}")

    stats: dict[str, Any] = {}
    if not video.is_file():
        errors.append(f"missing video: {video.name}")
    else:
        try:
            data = probe(video)
            streams = data.get("streams", [])
            video_stream = next((item for item in streams if item.get("codec_type") == "video"), None)
            audio_stream = next((item for item in streams if item.get("codec_type") == "audio"), None)
            duration = float(data.get("format", {}).get("duration", 0.0))
            actual_fps = fps(video_stream.get("r_frame_rate")) if video_stream else 0.0
            stats = {"duration_s": duration, "fps": actual_fps, "width": video_stream.get("width") if video_stream else None, "height": video_stream.get("height") if video_stream else None, "audio": audio_stream}
            if not video_stream:
                errors.append("missing video stream")
            else:
                if video_stream.get("width") != 1920 or video_stream.get("height") != 1080:
                    errors.append(f"expected 1920x1080, got {video_stream.get('width')}x{video_stream.get('height')}")
                if abs(actual_fps - 30.0) > 0.15:
                    errors.append(f"expected ~30fps CFR, got {actual_fps:.3f}fps")
            if not audio_stream:
                errors.append("missing audio stream")
            else:
                final_loudness = loudness(video)
                stats["loudness"] = final_loudness
                if not (-17.5 <= final_loudness["integrated_lufs"] <= -14.5):
                    errors.append(
                        f"final mix loudness {final_loudness['integrated_lufs']:.2f} LUFS "
                        "outside -17.5 to -14.5 LUFS"
                    )
                if final_loudness["true_peak_dbtp"] > -1.8:
                    errors.append(
                        f"final mix true peak {final_loudness['true_peak_dbtp']:.2f} dBTP exceeds -1.8 dBTP"
                    )
            if duration > args.max_duration:
                warnings.append(f"duration {duration:.2f}s exceeds {args.max_duration:.2f}s")
        except Exception as exc:  # noqa: BLE001 - report the failed artifact
            errors.append(f"ffprobe failed: {exc}")

    copy_path = root / "publishing-copy.md"
    if copy_path.is_file():
        copy = copy_path.read_text(encoding="utf-8")
        for section in ("抖音", "小红书", "视频号"):
            if f"## {section}" not in copy:
                errors.append(f"publishing-copy.md missing section: {section}")

    description_path = root / "cover-description.md"
    if description_path.is_file() and "## Alt text" not in description_path.read_text(encoding="utf-8"):
        errors.append("cover-description.md missing Alt text")

    project = root.parent
    master_path = project / "audio/output/narration_master.wav"
    boundary_path = project / "audio/boundary-qc.json"
    if not boundary_path.is_file():
        errors.append("missing project audio/boundary-qc.json")
    else:
        try:
            boundary = json.loads(boundary_path.read_text(encoding="utf-8"))
            if boundary.get("status") != "pass":
                errors.append("audio/boundary-qc.json did not pass")
            elif not master_path.is_file():
                errors.append("missing project audio/output/narration_master.wav")
            elif boundary.get("master", {}).get("sha256") != sha256(master_path):
                errors.append("audio/boundary-qc.json is stale for the current narration master")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid audio/boundary-qc.json: {exc}")

    profile_path = (
        Path(__file__).resolve().parent.parent
        / "references"
        / "voice-stability-profile.json"
    )
    voice_path = project / "audio/voice-stability-qc.json"
    master_voice: dict[str, Any] | None = None
    if not voice_path.is_file():
        errors.append("missing project audio/voice-stability-qc.json")
    else:
        try:
            master_voice = json.loads(voice_path.read_text(encoding="utf-8"))
            if master_voice.get("status") != "pass":
                errors.append("audio/voice-stability-qc.json did not pass")
            if master_path.is_file() and master_voice.get("audio", {}).get("sha256") != sha256(master_path):
                errors.append("audio/voice-stability-qc.json is stale for the current narration master")
            if master_voice.get("profile", {}).get("sha256") != sha256(profile_path):
                errors.append("audio/voice-stability-qc.json uses a stale voice-stability profile")
            for key, relative in (
                ("timeline_sha256", "audio/timeline.json"),
                ("captions_sha256", "audio/caption-groups.json"),
            ):
                current = project / relative
                recorded = master_voice.get("inputs", {}).get(key)
                if not current.is_file():
                    errors.append(f"missing project {relative}")
                elif recorded != sha256(current):
                    errors.append(f"audio/voice-stability-qc.json is stale for {relative}")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid audio/voice-stability-qc.json: {exc}")

    final_voice: dict[str, Any] | None = None
    if video.is_file() and profile_path.is_file():
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            offset = float(profile["final_mix"]["narration_offset_s"])
            final_report_path = root / "voice-stability-final-qc.json"
            validator = Path(__file__).resolve().with_name("validate_voice_stability.py")
            result = subprocess.run(
                [
                    sys.executable,
                    str(validator),
                    "--project",
                    str(project),
                    "--audio",
                    str(video),
                    "--analysis-offset-s",
                    str(offset),
                    "--stage",
                    "final",
                    "--report",
                    str(final_report_path),
                    "--profile",
                    str(profile_path),
                ],
                capture_output=True,
                text=True,
            )
            if final_report_path.is_file():
                final_voice = json.loads(final_report_path.read_text(encoding="utf-8"))
            if result.returncode != 0 or not final_voice or final_voice.get("status") != "pass":
                errors.append("final MP4 voice-stability gate did not pass")
                for item in (final_voice or {}).get("errors", [])[:4]:
                    errors.append(f"final voice: {item}")
            if final_voice and master_voice:
                drift_limit = float(profile["final_mix"]["max_local_metric_drift_db"])
                metric_keys = (
                    "active_1s_p90_p10_db",
                    "adjacent_caption_rms_max_delta_db",
                    "scene_boundary_rms_max_delta_db",
                )
                local_drift = {
                    key: abs(
                        float(final_voice.get("metrics", {}).get(key, 0.0))
                        - float(master_voice.get("metrics", {}).get(key, 0.0))
                    )
                    for key in metric_keys
                }
                stats["voice_stability_local_metric_drift_db"] = local_drift
                for key, value in local_drift.items():
                    if value > drift_limit:
                        errors.append(
                            f"final MP4 local metric drift {key}={value:.2f} dB exceeds {drift_limit:.2f} dB"
                        )
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            errors.append(f"final MP4 voice-stability validation failed: {exc}")

    stats["master_voice_stability"] = (
        master_voice.get("metrics") if master_voice else None
    )
    stats["final_voice_stability"] = final_voice.get("metrics") if final_voice else None

    motion_plan_path = project / ".hyperframes/semantic-motion.json"
    motion_qc_path = project / ".hyperframes/motion-qc.json"
    layout_boxes_path = project / ".hyperframes/layout-boxes.json"
    layout_qc_path = project / ".hyperframes/layout-qc.json"
    alignment_qc_path = project / ".hyperframes/alignment-qc.json"
    motion_plan: dict[str, Any] | None = None
    motion_qc: dict[str, Any] | None = None
    layout_boxes: dict[str, Any] | None = None
    layout_qc: dict[str, Any] | None = None
    alignment_qc: dict[str, Any] | None = None
    if not motion_plan_path.is_file():
        errors.append("missing project .hyperframes/semantic-motion.json")
    else:
        try:
            motion_plan = json.loads(motion_plan_path.read_text(encoding="utf-8"))
            if motion_plan.get("status") != "approved":
                errors.append("semantic-motion.json is not approved")
            if not str(motion_plan.get("review", {}).get("approved_by", "")).strip():
                errors.append("semantic-motion.json is missing review.approved_by")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid semantic-motion.json: {exc}")
    if not motion_qc_path.is_file():
        errors.append("missing project .hyperframes/motion-qc.json")
    else:
        try:
            motion_qc = json.loads(motion_qc_path.read_text(encoding="utf-8"))
            if motion_qc.get("status") != "pass":
                errors.append("motion-qc.json did not pass")
            elif motion_plan_path.is_file() and motion_qc.get("plan", {}).get("sha256") != sha256(motion_plan_path):
                errors.append("motion-qc.json is stale for the current semantic-motion.json")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid motion-qc.json: {exc}")
    if not layout_boxes_path.is_file():
        errors.append("missing project .hyperframes/layout-boxes.json")
    else:
        try:
            layout_boxes = json.loads(layout_boxes_path.read_text(encoding="utf-8"))
            if layout_boxes.get("status") != "approved":
                errors.append("layout-boxes.json is not approved")
            if motion_plan_path.is_file() and layout_boxes.get("motion_plan", {}).get("sha256") != sha256(motion_plan_path):
                errors.append("layout-boxes.json is stale for the current semantic-motion.json")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid layout-boxes.json: {exc}")
    if not layout_qc_path.is_file():
        errors.append("missing project .hyperframes/layout-qc.json")
    else:
        try:
            layout_qc = json.loads(layout_qc_path.read_text(encoding="utf-8"))
            if layout_qc.get("status") != "pass":
                errors.append("layout-qc.json did not pass")
            if layout_boxes_path.is_file() and layout_qc.get("layout", {}).get("sha256") != sha256(layout_boxes_path):
                errors.append("layout-qc.json is stale for the current layout-boxes.json")
            if motion_plan_path.is_file() and layout_qc.get("motion_plan", {}).get("sha256") != sha256(motion_plan_path):
                errors.append("layout-qc.json is stale for the current semantic-motion.json")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid layout-qc.json: {exc}")
    if not alignment_qc_path.is_file():
        errors.append("missing project .hyperframes/alignment-qc.json")
    else:
        try:
            alignment_qc = json.loads(alignment_qc_path.read_text(encoding="utf-8"))
            if alignment_qc.get("status") != "pass":
                errors.append("alignment-qc.json did not pass")
            recorded_motion = alignment_qc.get("inputs", {}).get("motion", {}).get("sha256")
            if motion_plan_path.is_file() and recorded_motion != sha256(motion_plan_path):
                errors.append("alignment-qc.json is stale for the current semantic-motion.json")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid alignment-qc.json: {exc}")
    for output_name, project_path in (
        ("semantic-motion.json", motion_plan_path),
        ("motion-qc.json", motion_qc_path),
        ("layout-boxes.json", layout_boxes_path),
        ("layout-qc.json", layout_qc_path),
        ("alignment-qc.json", alignment_qc_path),
    ):
        output_path = root / output_name
        if output_path.is_file() and project_path.is_file() and sha256(output_path) != sha256(project_path):
            errors.append(f"co-located {output_name} is stale")
    stats["semantic_motion"] = {
        "profile_id": motion_plan.get("profile", {}).get("id") if motion_plan else None,
        "seed": motion_plan.get("seed") if motion_plan else None,
        "scenes": len(motion_plan.get("scenes", [])) if motion_plan else 0,
        "motion_qc_metrics": motion_qc.get("metrics") if motion_qc else None,
        "layout_qc_metrics": layout_qc.get("metrics") if layout_qc else None,
        "alignment_qc_metrics": alignment_qc.get("metrics") if alignment_qc else None,
    }

    manifest_path = root / "asset-manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            recorded = manifest.get("semantic_motion", {})
            if not recorded:
                errors.append("asset-manifest.json is missing semantic_motion traceability")
            elif not all((root / name).is_file() for name in ("semantic-motion.json", "motion-qc.json", "layout-boxes.json", "layout-qc.json", "alignment-qc.json")):
                errors.append("asset-manifest.json references missing co-located motion artifacts")
            elif recorded.get("plan", {}).get("sha256") != sha256(root / "semantic-motion.json"):
                errors.append("asset-manifest.json has stale semantic_motion plan hash")
            elif recorded.get("qc", {}).get("sha256") != sha256(root / "motion-qc.json"):
                errors.append("asset-manifest.json has stale motion QC hash")
            elif recorded.get("layout", {}).get("sha256") != sha256(root / "layout-boxes.json"):
                errors.append("asset-manifest.json has stale layout box hash")
            elif recorded.get("layout_qc", {}).get("sha256") != sha256(root / "layout-qc.json"):
                errors.append("asset-manifest.json has stale layout QC hash")
            elif recorded.get("alignment_qc", {}).get("sha256") != sha256(root / "alignment-qc.json"):
                errors.append("asset-manifest.json has stale alignment QC hash")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid asset-manifest.json: {exc}")

    index_path = project / "index.html"
    if not index_path.is_file():
        errors.append("missing project index.html")
    else:
        html = index_path.read_text(encoding="utf-8")
        first_sfx = re.search(
            r'<audio\s+id="first-frame-sfx"[^>]*data-start="([0-9.]+)"[^>]*data-duration="([0-9.]+)"',
            html,
        )
        if not first_sfx:
            errors.append("missing first-frame SFX audio element")
        else:
            if abs(float(first_sfx.group(1))) > 0.001:
                errors.append("first-frame SFX must start at t=0")
            if float(first_sfx.group(2)) > 0.6:
                errors.append("first-frame SFX must not exceed 0.6 seconds")
        for selector in ("#intro-follow", "#follow-end"):
            if selector not in html:
                errors.append(f"missing deterministic follow animation: {selector}")

    report = {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "pass" if not errors else "fail",
        "video": stats,
        "errors": errors,
        "warnings": warnings,
        "required_assets": required,
    }
    partial = root / "qc-report.json.part"
    partial.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial.replace(root / "qc-report.json")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
