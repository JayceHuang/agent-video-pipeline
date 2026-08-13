#!/usr/bin/env python3
"""Create co-located cover, image description, publishing copy, and manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from profile_config import get_in, load_resolved_profile


def atomic_text(path: Path, text: str) -> None:
    partial = path.with_name(path.name + ".part")
    partial.write_text(text, encoding="utf-8")
    partial.replace(path)


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def atomic_copy(source: Path, target: Path) -> None:
    """Copy a traceability artifact byte-for-byte, then replace atomically."""
    partial = target.with_name(target.name + ".part")
    shutil.copyfile(source, partial)
    partial.replace(target)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe(video: Path) -> dict[str, Any]:
    command = [
        "ffprobe", "-v", "error", "-of", "json",
        "-show_entries", "format=duration,size:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
        str(video),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    video_stream = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio_stream = next((item for item in streams if item.get("codec_type") == "audio"), {})
    return {
        "duration_s": round(float(payload.get("format", {}).get("duration", 0.0)), 6),
        "size_bytes": int(payload.get("format", {}).get("size", 0) or 0),
        "video": {
            "codec": video_stream.get("codec_name"),
            "width": video_stream.get("width"),
            "height": video_stream.get("height"),
            "r_frame_rate": video_stream.get("r_frame_rate"),
        },
        "audio": {
            "codec": audio_stream.get("codec_name"),
            "sample_rate": audio_stream.get("sample_rate"),
            "channels": audio_stream.get("channels"),
        } if audio_stream else None,
    }


def frame_time(duration: float, requested: float | None) -> float:
    if requested is not None:
        return max(0.0, min(requested, max(0.0, duration - 0.1)))
    # Avoid the title-only first frame while remaining safe for short clips.
    return round(max(0.8, min(duration * 0.08, max(0.8, duration - 0.5))), 3)


def _convert_existing_image(source: Path, target: Path, extra: list[str]) -> None:
    partial = target.with_name(target.stem + ".part" + target.suffix)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-frames:v", "1", *extra, str(partial),
    ]
    subprocess.run(command, check=True)
    partial.replace(target)


def extract_cover(video: Path, output_dir: Path, at: float, *, force: bool = False) -> tuple[Path, Path, bool]:
    png = output_dir / "cover.png"
    jpg = output_dir / "cover.jpg"
    if not force and (png.is_file() or jpg.is_file()):
        # Reuse the existing visual instead of silently replacing a user's
        # chosen cover when only the narration/video is being regenerated.
        if not png.is_file() and jpg.is_file():
            _convert_existing_image(jpg, png, ["-compression_level", "3"])
        if not jpg.is_file() and png.is_file():
            _convert_existing_image(png, jpg, ["-q:v", "2"])
        return png, jpg, True
    for target, extra in ((png, ["-compression_level", "3"]), (jpg, ["-q:v", "2"])):
        # Keep the image suffix on the temporary path so FFmpeg can select the
        # correct muxer for the local macOS render.
        partial = target.with_name(target.stem + ".part" + target.suffix)
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1",
            *extra, str(partial),
        ]
        subprocess.run(command, check=True)
        partial.replace(target)
    return png, jpg, False


def render_template(value: Any, context: dict[str, Any], field: str) -> str:
    try:
        return str(value).format_map(context)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"invalid publishing template {field}: {exc}") from exc


def configured_copy(
    title: str,
    summary: str,
    tags: list[str],
    platforms: list[str],
    templates: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    tag_text = " ".join(tags)
    context = {"title": title, "summary": summary, "tag_text": tag_text}
    result: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for platform in platforms:
        raw = templates.get(platform, {}) if isinstance(templates, dict) else {}
        template = raw if isinstance(raw, dict) else {}
        display_name = str(template.get("display_name", platform))
        limit = template.get("tag_limit")
        selected_tags = tags
        if isinstance(limit, int) and limit >= 0:
            selected_tags = tags[:limit]
        result[display_name] = {
            "title": render_template(template.get("title", "{title}"), context, f"{platform}.title"),
            "body": render_template(template.get("body", "{summary}"), context, f"{platform}.body"),
            "tags": selected_tags,
        }
        order.append(display_name)
    return result, order


def copy_markdown(
    title: str,
    copy: dict[str, dict[str, Any]],
    display_order: list[str],
) -> str:
    lines = ["# 发布文案", "", f"主题：{title}", "", "> 默认只生成素材，不自动发布。", ""]
    if not display_order:
        display_order = list(copy)
    for platform_name in display_order:
        item = copy.get(platform_name, {})
        tags = item.get("tags", [])
        lines.extend([
            f"## {platform_name}", "", f"**标题**：{item.get('title', title)}", "",
            "**正文**：", "", str(item.get("body", "")).rstrip(), "",
            "**标签**：" + (" ".join(str(tag) for tag in tags) if tags else "（待补充）"), "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def load_visual_assets(output_dir: Path) -> dict[str, Any] | None:
    """Read the project-level visual-provider decision without forcing generation."""
    # Prefer the project manifest: the co-located render copy can belong to an
    # older finalization and must not silently override the current plan.
    for candidate in (output_dir.parent / "visual-assets.json", output_dir / "visual-assets.json"):
        if not candidate.is_file():
            continue
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            target = output_dir / "visual-assets.json"
            if candidate.resolve() != target.resolve():
                atomic_copy(candidate, target)
            return value
    return None


def load_motion_assets(output_dir: Path) -> dict[str, Any] | None:
    """Copy motion/layout plans and QC beside the final video and record hashes."""
    project = output_dir.parent
    sources = {
        "plan": project / ".hyperframes/semantic-motion.json",
        "qc": project / ".hyperframes/motion-qc.json",
        "layout": project / ".hyperframes/layout-boxes.json",
        "layout_qc": project / ".hyperframes/layout-qc.json",
        "alignment_qc": project / ".hyperframes/alignment-qc.json",
    }
    target_names = {
        "plan": "semantic-motion.json",
        "qc": "motion-qc.json",
        "layout": "layout-boxes.json",
        "layout_qc": "layout-qc.json",
        "alignment_qc": "alignment-qc.json",
    }
    if not all(path.is_file() for path in sources.values()):
        return None
    parsed: dict[str, dict[str, Any]] = {}
    for key, source in sources.items():
        try:
            value = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        parsed[key] = value
        target_name = target_names[key]
        # Validators compare these immutable artifacts by SHA-256. Rewriting
        # parsed JSON changes whitespace and therefore breaks traceability.
        atomic_copy(source, output_dir / target_name)
    return {
        "plan": {
            "path": "semantic-motion.json",
            "sha256": sha256(output_dir / "semantic-motion.json"),
            "status": parsed["plan"].get("status"),
            "profile_id": parsed["plan"].get("profile", {}).get("id"),
            "seed": parsed["plan"].get("seed"),
            "compiler_sha256": parsed["plan"].get("compiler", {}).get("sha256"),
        },
        "qc": {
            "path": "motion-qc.json",
            "sha256": sha256(output_dir / "motion-qc.json"),
            "status": parsed["qc"].get("status"),
            "errors": len(parsed["qc"].get("errors", [])),
            "warnings": len(parsed["qc"].get("warnings", [])),
        },
        "layout": {
            "path": "layout-boxes.json",
            "sha256": sha256(output_dir / "layout-boxes.json"),
            "status": parsed["layout"].get("status"),
        },
        "layout_qc": {
            "path": "layout-qc.json",
            "sha256": sha256(output_dir / "layout-qc.json"),
            "status": parsed["layout_qc"].get("status"),
            "errors": len(parsed["layout_qc"].get("errors", [])),
            "warnings": len(parsed["layout_qc"].get("warnings", [])),
        },
        "alignment_qc": {
            "path": "alignment-qc.json",
            "sha256": sha256(output_dir / "alignment-qc.json"),
            "status": parsed["alignment_qc"].get("status"),
            "errors": len(parsed["alignment_qc"].get("errors", [])),
            "warnings": len(parsed["alignment_qc"].get("warnings", [])),
        },
    }


def load_pipeline_timings(output_dir: Path) -> dict[str, Any] | None:
    """Copy the append-only timing trace beside the final deliverables."""
    source = output_dir.parent / "pipeline-timings.json"
    if not source.is_file():
        return None
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        return None
    target = output_dir / "pipeline-timings.json"
    atomic_copy(source, target)
    return {
        "path": target.name,
        "sha256": sha256(target),
        "wall_clock_elapsed_s": payload.get("wall_clock_elapsed_s"),
        "event_count": len(payload["events"]),
        "cache_hits": sum(
            1 for event in payload["events"]
            if isinstance(event, dict) and event.get("cache_hit") is True
        ),
    }


def load_intro_card(output_dir: Path) -> dict[str, Any] | None:
    """Read the generated opening-card timing from the assembled composition."""
    index_path = output_dir.parent / "index.html"
    if not index_path.is_file():
        return None
    try:
        html = index_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'data-intro-duration="([0-9.]+)"', html)
    if not match:
        return None
    def read_attr(name: str, fallback: float) -> float:
        value = re.search(rf'data-intro-{name}="([0-9.]+)"', html)
        return float(value.group(1)) if value else fallback

    reveal = read_attr("reveal", 0.2)
    hold = read_attr("hold", 0.6)
    fade = read_attr("fade", 0.2)
    return {
        "enabled": True,
        "duration_s": float(match.group(1)),
        "position": "center",
        "reveal_s": reveal,
        "hold_s": hold,
        "fade_s": fade,
        "narration_starts_after_fade": True,
    }


def load_audio_pipeline_assets(output_dir: Path) -> dict[str, Any] | None:
    """Record the audio provenance and gates used by the render."""
    project = output_dir.parent
    result: dict[str, Any] = {}
    boundary_path = project / "audio/boundary-qc.json"
    if boundary_path.is_file():
        try:
            boundary = json.loads(boundary_path.read_text(encoding="utf-8"))
            result["boundary_qc"] = {
                "path": "audio/boundary-qc.json",
                "status": boundary.get("status"),
                "master_sha256": boundary.get("master", {}).get("sha256"),
                "boundaries": len(boundary.get("boundaries", [])),
            }
        except (OSError, json.JSONDecodeError):
            pass
    stability_path = project / "audio/voice-stability-qc.json"
    if stability_path.is_file():
        try:
            stability = json.loads(stability_path.read_text(encoding="utf-8"))
            if isinstance(stability, dict):
                audio = stability.get("audio")
                audio = audio if isinstance(audio, dict) else {}
                profile = stability.get("profile")
                profile = profile if isinstance(profile, dict) else {}
                metrics = stability.get("metrics")
                metrics = metrics if isinstance(metrics, dict) else {}
                master_sha256 = audio.get("sha256")
                result["voice_stability_qc"] = {
                    "path": "audio/voice-stability-qc.json",
                    "status": stability.get("status"),
                    "audio": {"sha256": master_sha256},
                    "master_sha256": master_sha256,
                    "profile": {
                        "profile_id": profile.get("profile_id"),
                        "sha256": profile.get("sha256"),
                    },
                    "metrics": metrics,
                }
        except (OSError, json.JSONDecodeError):
            pass
    voice_manifest_path = project / "audio/voice-manifest.json"
    if voice_manifest_path.is_file():
        try:
            voice_manifest = json.loads(voice_manifest_path.read_text(encoding="utf-8"))
            if isinstance(voice_manifest, dict):
                prompt = voice_manifest.get("prompt")
                prompt = prompt if isinstance(prompt, dict) else {}
                reference = voice_manifest.get("reference")
                reference = reference if isinstance(reference, dict) else {}
                profile = voice_manifest.get("profile")
                profile = profile if isinstance(profile, dict) else {}
                normalization = voice_manifest.get("normalization")
                normalization = normalization if isinstance(normalization, dict) else {}
                result["voice_manifest"] = {
                    "path": "audio/voice-manifest.json",
                    "generation_mode": voice_manifest.get("generation_mode"),
                    "clone_mode": voice_manifest.get("clone_mode"),
                    "candidate_strategy": voice_manifest.get("candidate_strategy"),
                    "candidate_limit": voice_manifest.get("candidate_limit"),
                    "candidate_count": voice_manifest.get("candidate_count"),
                    "selected_seed": voice_manifest.get("selected_seed"),
                    "global_retime_factor": voice_manifest.get("global_retime_factor"),
                    "master_sha256": voice_manifest.get("master_sha256"),
                    "normalization": {
                        "scope": normalization.get(
                            "scope", voice_manifest.get("normalization_scope")
                        ),
                        "method": normalization.get(
                            "method", voice_manifest.get("normalization_method")
                        ),
                        "application_count": normalization.get(
                            "application_count", voice_manifest.get("normalization_count")
                        ),
                    },
                    "prompt": {
                        "sha256": prompt.get("sha256", voice_manifest.get("prompt_sha256")),
                        "manifest_sha256": prompt.get(
                            "manifest_sha256", voice_manifest.get("prompt_manifest_sha256")
                        ),
                    },
                    "reference": {
                        "sha256": reference.get(
                            "sha256", voice_manifest.get("reference_sha256")
                        ),
                    },
                    "profile": {
                        "sha256": profile.get(
                            "sha256", voice_manifest.get("profile_sha256")
                        ),
                        "profile_id": profile.get(
                            "profile_id", voice_manifest.get("profile_id")
                        ),
                    },
                }
        except (OSError, json.JSONDecodeError):
            pass
    cue_path = project / "audio/sfx-cues.json"
    if cue_path.is_file():
        try:
            cues = json.loads(cue_path.read_text(encoding="utf-8"))
            first = cues.get("first_frame")
            if isinstance(first, dict):
                media_path = project / str(first.get("file", ""))
                result["first_frame_sfx"] = {
                    **first,
                    "sha256": sha256(media_path) if media_path.is_file() else None,
                }
        except (OSError, json.JSONDecodeError):
            pass
    return result or None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--title", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--visual-description", default="")
    parser.add_argument("--frame-time", type=float)
    parser.add_argument("--job-id", default="")
    parser.add_argument("--tags", help="Comma-separated override; defaults to publishing.tags")
    parser.add_argument("--copy-json", type=Path, help="Optional platform-copy JSON override")
    parser.add_argument("--force-cover", action="store_true", help="Explicitly replace an existing local cover")
    parser.add_argument("--profile", type=Path, required=True, help="resolved profile JSON")
    args = parser.parse_args()

    video = args.video.expanduser().resolve()
    if not video.is_file():
        raise FileNotFoundError(video)
    output_dir = (args.output_dir or video.parent).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    profile, profile_path = load_resolved_profile(args.profile, output_dir.parent, required=True)
    visual_validator = Path(__file__).resolve().with_name("validate_visual_assets.py")
    visual_gate = subprocess.run(
        [
            sys.executable,
            str(visual_validator),
            "--project",
            str(output_dir.parent),
            "--profile",
            str(profile_path),
        ],
        capture_output=True,
        text=True,
    )
    if visual_gate.returncode != 0:
        details = (visual_gate.stderr or visual_gate.stdout).strip()
        raise RuntimeError(f"visual asset gate must pass before finalization: {details}")

    stats = probe(video)
    at = frame_time(stats["duration_s"], args.frame_time)
    cover_png, cover_jpg, cover_reused = extract_cover(video, output_dir, at, force=args.force_cover)
    if cover_reused:
        previous_manifest = output_dir / "asset-manifest.json"
        if previous_manifest.is_file():
            try:
                previous = json.loads(previous_manifest.read_text(encoding="utf-8"))
                previous_at = previous.get("cover", {}).get("frame_time_s")
                if previous_at is not None:
                    at = float(previous_at)
            except (OSError, ValueError, TypeError):
                pass

    configured_tags = get_in(profile, "publishing.tags", [])
    tags = (
        [tag.strip() for tag in args.tags.split(",") if tag.strip()]
        if args.tags is not None
        else [str(tag) for tag in configured_tags if str(tag).strip()]
        if isinstance(configured_tags, list)
        else []
    )
    configured_platforms = get_in(profile, "publishing.platforms", [])
    platforms = [str(item) for item in configured_platforms] if isinstance(configured_platforms, list) else []
    templates = get_in(profile, "publishing.copy_templates", {})
    templates = templates if isinstance(templates, dict) else {}
    if args.copy_json:
        copy = json.loads(args.copy_json.read_text(encoding="utf-8"))
        if not isinstance(copy, dict):
            raise ValueError("--copy-json must contain an object")
        display_order = [
            str(templates.get(platform, {}).get("display_name", platform))
            if isinstance(templates.get(platform, {}), dict) else platform
            for platform in platforms
        ]
    else:
        copy, display_order = configured_copy(args.title, args.summary, tags, platforms, templates)
    cover_context = {
        "title": args.title,
        "summary": args.summary,
        "frame_time_s": at,
        "cover_style": get_in(profile, "publishing.cover_style", "neutral"),
    }
    visual = args.visual_description or render_template(
        get_in(profile, "publishing.cover_description_template", "Frame at {frame_time_s:.3f} seconds."),
        cover_context,
        "cover_description_template",
    )
    alt = render_template(
        get_in(profile, "publishing.alt_text_template", "{title}: {visual_description}"),
        {**cover_context, "visual_description": visual},
        "alt_text_template",
    )
    description_path = output_dir / "cover-description.md"
    if not cover_reused or not description_path.is_file():
        description = "\n".join([
            "# 封面图片描述", "", f"**标题**：{args.title}",
            f"**来源视频**：`{video.name}`", f"**提取时间**：{at:.3f}s", "",
            "## 视觉描述", "", visual, "",
            "## Alt text", "", alt, "",
            "## 构图与安全区", "",
            f"- 画布为 {get_in(profile, 'layout.canvas.width')}×{get_in(profile, 'layout.canvas.height')}。",
            (
                f"- 数字人安全区由 Profile 定义：{get_in(profile, 'layout.avatar_safe_zone.shape')}，"
                f"尺寸 {get_in(profile, 'layout.avatar_safe_zone.size')}。"
                if get_in(profile, "layout.avatar_safe_zone.enabled", False)
                else "- 当前 Profile 未启用数字人安全区。"
            ),
            "- 描述只陈述画面中实际可见的内容，不虚构图片中没有的文字、人物或数据。", "",
        ])
        atomic_text(description_path, description)
    atomic_text(output_dir / "publishing-copy.md", copy_markdown(args.title, copy, display_order))
    visual_assets = load_visual_assets(output_dir)
    if visual_assets is None:
        raise RuntimeError("passing project visual-assets.json disappeared during finalization")
    motion_assets = load_motion_assets(output_dir)
    intro_card = load_intro_card(output_dir)
    audio_pipeline_assets = load_audio_pipeline_assets(output_dir)
    pipeline_timings = load_pipeline_timings(output_dir)

    manifest = {
        "schema_version": 1,
        "job_id": args.job_id or video.stem,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "video": {"path": video.name, "sha256": sha256(video), **stats},
        "cover": {
            "png": cover_png.name, "jpg": cover_jpg.name,
            "frame_time_s": at,
            "description": "cover-description.md",
            "reused_existing": cover_reused,
        },
        "publishing_copy": "publishing-copy.md",
        "publishing": {
            "platforms": platforms,
            "cover_style": get_in(profile, "publishing.cover_style", "neutral"),
        },
        "profile": {
            "id": profile.get("profile_id"),
            "sha256": get_in(profile, "_meta.profile_sha256"),
        },
    }
    if get_in(profile, "layout.avatar_safe_zone.enabled", False):
        manifest["avatar_safe_zone"] = get_in(profile, "layout.avatar_safe_zone")
    if visual_assets is not None:
        manifest["visual_assets"] = visual_assets
        manifest["visual_assets_manifest"] = {
            "path": "visual-assets.json",
            "sha256": sha256(output_dir / "visual-assets.json"),
        }
    if motion_assets is not None:
        manifest["semantic_motion"] = motion_assets
    if intro_card is not None:
        manifest["opening_title_card"] = intro_card
    if audio_pipeline_assets is not None:
        manifest["audio_pipeline"] = audio_pipeline_assets
    if pipeline_timings is not None:
        manifest["pipeline_timings"] = pipeline_timings
    atomic_json(output_dir / "asset-manifest.json", manifest)
    print(json.dumps({"output_dir": str(output_dir), "cover": cover_png.name, "reused_existing": cover_reused, "description": "cover-description.md", "copy": "publishing-copy.md"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
