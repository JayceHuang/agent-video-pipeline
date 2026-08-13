#!/usr/bin/env python3
"""Convert an approved generic spoken-script package into pipeline inputs."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from profile_config import atomic_json, get_in, load_resolved_profile, sha256_file


SLUG_RE = re.compile(r"[^a-z0-9]+")
DEFAULT_SCRIPT_VALIDATOR = (
    Path(__file__).resolve().parents[2]
    / "adapt-longform-for-speech/scripts/validate_spoken_script.py"
)


def slugify(value: str, fallback: str) -> str:
    slug = SLUG_RE.sub("-", value.lower()).strip("-")
    return slug or fallback


def append_cta(text: str, cta: str, separator: str) -> str:
    value = text.rstrip()
    if not cta or value.endswith(cta):
        return value
    joiner = "" if value.endswith(("。", "！", "？", ".", "!", "?", "；", ";")) else separator
    return value + joiner + cta


def scene_from_segment(segment: dict[str, Any], index: int, gap_s: float) -> dict[str, Any]:
    segment_id = str(segment.get("id", "")).strip() or f"segment-{index:02d}"
    role = str(segment.get("role", "")).strip() or "narration"
    text = str(segment.get("spoken_text", "")).strip()
    if not text:
        raise ValueError(f"segment {segment_id} has empty spoken_text")
    scene: dict[str, Any] = {
        "id": segment_id,
        "kicker": role,
        "title": str(segment.get("title", "")).strip() or role,
        "text": text,
        "focus": [str(item) for item in segment.get("focus", []) if str(item).strip()],
        "gap_after_s": gap_s,
        "source_refs": segment.get("source_refs", []),
    }
    notes = segment.get("production_notes")
    if notes not in (None, "", []):
        scene["production_notes"] = notes
    return scene


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", type=Path, required=True, help="spoken-script.json")
    parser.add_argument("--script-qc", type=Path, required=True, help="passing script-qc.json")
    parser.add_argument("--script-validator", type=Path, default=DEFAULT_SCRIPT_VALIDATOR)
    parser.add_argument("--profile", type=Path, required=True, help="resolved profile JSON")
    parser.add_argument("--series-output", type=Path, required=True)
    parser.add_argument("--projects-root", type=Path, help="optionally write one <slug>/scenes.json per episode")
    parser.add_argument("--allow-draft", action="store_true")
    args = parser.parse_args()

    script_path = args.script.expanduser().absolute()
    script = json.loads(script_path.read_text(encoding="utf-8"))
    qc_path = args.script_qc.expanduser().absolute()
    qc = json.loads(qc_path.read_text(encoding="utf-8"))
    if not isinstance(qc, dict) or qc.get("status") != "pass":
        raise ValueError("script QC must exist and have status=pass")
    if qc.get("script_sha256") != sha256_file(script_path):
        raise ValueError("script QC is stale for spoken-script.json")
    markdown_value = str(qc.get("markdown", "")).strip()
    markdown_sha = str(qc.get("markdown_sha256", "")).strip()
    markdown_path = Path(markdown_value).expanduser().absolute() if markdown_value else None
    if markdown_path is None or not markdown_path.is_file() or sha256_file(markdown_path) != markdown_sha:
        raise ValueError("script QC companion Markdown is missing or stale")
    validator_path = args.script_validator.expanduser().absolute()
    if not validator_path.is_file():
        raise FileNotFoundError(f"spoken-script validator is missing: {validator_path}")
    validator_record = qc.get("validator") if isinstance(qc.get("validator"), dict) else {}
    if validator_record.get("sha256") != sha256_file(validator_path):
        raise ValueError("script QC was produced by a stale spoken-script validator")
    with tempfile.TemporaryDirectory(prefix="spoken-script-recheck-") as temp_dir:
        command = [
            sys.executable,
            str(validator_path),
            "--script",
            str(script_path),
            "--markdown",
            str(markdown_path),
            "--report",
            str(Path(temp_dir) / "script-qc.json"),
        ]
        if not args.allow_draft:
            command.append("--require-approved")
        recheck = subprocess.run(command, capture_output=True, text=True)
        if recheck.returncode != 0:
            raise ValueError(
                "spoken-script revalidation failed: "
                + (recheck.stderr or recheck.stdout).strip()
            )
    if not isinstance(script, dict) or script.get("schema_version") != 1:
        raise ValueError("spoken script must use schema_version 1")
    if not args.allow_draft and script.get("status") != "approved":
        raise ValueError("spoken script must be approved before pipeline import")
    episodes = script.get("episodes")
    if not isinstance(episodes, list) or not episodes:
        raise ValueError("spoken script must contain at least one episode")

    profile, profile_path = load_resolved_profile(args.profile, None, required=True)
    profile_sha = get_in(profile, "_meta.profile_sha256")
    cta = str(get_in(profile, "episode.final_cta", "") or "").strip()
    cta_separator = str(get_in(profile, "episode.cta_separator", " "))
    gap_s = float(get_in(profile, "voice.boundary_stability.default_gap_s", 0.18))
    canvas = get_in(profile, "layout.canvas", {})
    converted: list[dict[str, Any]] = []
    used_slugs: set[str] = set()

    for episode_index, episode in enumerate(episodes, start=1):
        if not isinstance(episode, dict):
            raise ValueError(f"episode {episode_index} must be an object")
        raw_segments = episode.get("segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            raise ValueError(f"episode {episode_index} has no segments")
        scenes = [scene_from_segment(item, index, gap_s) for index, item in enumerate(raw_segments, start=1)]
        scenes[-1]["text"] = append_cta(str(scenes[-1]["text"]), cta, cta_separator)
        scenes[-1]["gap_after_s"] = 0.0
        episode_id = str(episode.get("id", "")).strip() or f"episode-{episode_index:02d}"
        slug = slugify(str(episode.get("slug", episode_id)), f"episode-{episode_index:02d}")
        if slug in used_slugs:
            slug = f"{slug}-{episode_index:02d}"
        used_slugs.add(slug)
        converted.append({
            "episode": episode_index,
            "id": episode_id,
            "slug": slug,
            "title": str(episode.get("title", "")).strip(),
            "summary": str(episode.get("summary", "")).strip(),
            "scenes": scenes,
        })

    source = script.get("source") if isinstance(script.get("source"), dict) else {}
    series = {
        "schema_version": 2,
        "series_title": str(script.get("title", "")).strip() or converted[0]["title"],
        "source": source,
        "spoken_script": {"path": str(script_path), "sha256": sha256_file(script_path)},
        "script_qc": {"path": str(qc_path), "sha256": sha256_file(qc_path)},
        "script_validator": {"path": str(validator_path), "sha256": sha256_file(validator_path)},
        "profile": {"path": str(profile_path), "id": profile.get("profile_id"), "sha256": profile_sha},
        "format": f"{int(canvas['width'])}x{int(canvas['height'])}",
        "fps": float(canvas["fps"]),
        "target_effective_chars_per_minute": get_in(
            profile, "voice.target_effective_chinese_chars_per_minute"
        ),
        "ending_cta": cta,
        "episodes": converted,
    }
    series_output = args.series_output.expanduser().absolute()
    atomic_json(series_output, series)

    projects = []
    if args.projects_root:
        projects_root = args.projects_root.expanduser().absolute()
        for episode in converted:
            target = projects_root / str(episode["slug"]) / "scenes.json"
            payload = {
                "schema_version": 2,
                "episode": episode["episode"],
                "id": episode["id"],
                "slug": episode["slug"],
                "title": episode["title"],
                "summary": episode["summary"],
                "profile": series["profile"],
                "spoken_script": series["spoken_script"],
                "script_qc": series["script_qc"],
                "script_validator": series["script_validator"],
                "scenes": episode["scenes"],
            }
            atomic_json(target, payload)
            projects.append(str(target.parent))

    print(json.dumps({
        "status": "pass",
        "series": str(series_output),
        "episodes": len(converted),
        "projects": projects,
        "profile_sha256": profile_sha,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
