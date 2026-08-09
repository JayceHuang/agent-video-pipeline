#!/usr/bin/env python3
"""Create co-located cover, image description, publishing copy, and manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def default_copy(title: str, summary: str, tags: list[str]) -> dict[str, dict[str, Any]]:
    tag_text = " ".join(tags)
    return {
        "抖音": {
            "title": title,
            "body": f"{summary}\n\n用一条视频把关键概念讲清楚。",
            "tags": tags[:8],
        },
        "小红书": {
            "title": f"{title}｜一条视频讲明白",
            "body": f"{summary}\n\n适合刚开始学习 AI 的朋友，建议收藏后再看一遍。\n\n{tag_text}",
            "tags": tags[:10],
        },
        "视频号": {
            "title": title,
            "body": f"{summary}\n\n本集按教程节奏拆解，方便跟着视频逐步理解。",
            "tags": tags[:6],
        },
    }


def copy_markdown(title: str, copy: dict[str, dict[str, Any]]) -> str:
    lines = ["# 发布文案", "", f"主题：{title}", "", "> 默认只生成素材，不自动发布。", ""]
    for platform in ("抖音", "小红书", "视频号"):
        item = copy.get(platform, {})
        tags = item.get("tags", [])
        lines.extend([
            f"## {platform}", "", f"**标题**：{item.get('title', title)}", "",
            "**正文**：", "", str(item.get("body", "")).rstrip(), "",
            "**标签**：" + (" ".join(str(tag) for tag in tags) if tags else "（待补充）"), "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def load_visual_assets(output_dir: Path) -> dict[str, Any] | None:
    """Read the project-level xiaomu decision without forcing image generation."""
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
                        "path": prompt.get("path", voice_manifest.get("prompt_path")),
                        "sha256": prompt.get("sha256", voice_manifest.get("prompt_sha256")),
                        "text": prompt.get("text", voice_manifest.get("prompt_text")),
                        "manifest_path": prompt.get(
                            "manifest_path", voice_manifest.get("prompt_manifest_path")
                        ),
                        "manifest_sha256": prompt.get(
                            "manifest_sha256", voice_manifest.get("prompt_manifest_sha256")
                        ),
                    },
                    "reference": {
                        "path": reference.get(
                            "path", voice_manifest.get("reference_path")
                        ),
                        "sha256": reference.get(
                            "sha256", voice_manifest.get("reference_sha256")
                        ),
                    },
                    "profile": {
                        "path": profile.get("path", voice_manifest.get("profile_path")),
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
    parser.add_argument("--title", default="Agent 教程视频")
    parser.add_argument("--summary", default="这是一集中文 AI 教程视频。")
    parser.add_argument("--visual-description", default="")
    parser.add_argument("--frame-time", type=float)
    parser.add_argument("--job-id", default="")
    parser.add_argument("--tags", default="#AI工具,#Agent,#人工智能,#教程")
    parser.add_argument("--copy-json", type=Path, help="Optional JSON with 抖音/小红书/视频号 copy")
    parser.add_argument("--force-cover", action="store_true", help="Explicitly replace an existing local cover")
    args = parser.parse_args()

    video = args.video.expanduser().resolve()
    if not video.is_file():
        raise FileNotFoundError(video)
    output_dir = (args.output_dir or video.parent).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

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

    tags = [tag.strip() for tag in args.tags.split(",") if tag.strip()]
    copy = json.loads(args.copy_json.read_text(encoding="utf-8")) if args.copy_json else default_copy(args.title, args.summary, tags)
    visual = args.visual_description or f"从最终视频第 {at:.3f} 秒提取的横屏教程画面，画面内容应与视频主题和标题一致。"
    alt = f"{args.title}：{visual}"
    description_path = output_dir / "cover-description.md"
    if not cover_reused or not description_path.is_file():
        description = "\n".join([
            "# 封面图片描述", "", f"**标题**：{args.title}",
            f"**来源视频**：`{video.name}`", f"**提取时间**：{at:.3f}s", "",
            "## 视觉描述", "", visual, "",
            "## Alt text", "", alt, "",
            "## 构图与安全区", "",
            "- 画布默认为 1920×1080 横屏。",
            "- 左下角预留直径 300px 的圆形数字人区域。",
            "- 描述只陈述画面中实际可见的内容，不虚构图片中没有的文字、人物或数据。", "",
        ])
        atomic_text(description_path, description)
    atomic_text(output_dir / "publishing-copy.md", copy_markdown(args.title, copy))
    visual_assets = load_visual_assets(output_dir)
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
        "avatar_safe_zone": {"x": 42, "bottom": 28, "size": 300, "shape": "circle"},
    }
    if visual_assets is not None:
        manifest["visual_assets"] = visual_assets
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
