#!/usr/bin/env python3
"""Validate the narration -> captions -> motion -> DOM -> image binding chain."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
import wave
from pathlib import Path
from typing import Any


TRAILING_CLOSERS = "”’」』）)]】"
CAPTION_BOUNDARIES = "，。！？；,!?;"
NORMALIZE_RE = re.compile(r"\s+")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize(text: str) -> str:
    return NORMALIZE_RE.sub("", str(text)).lower()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    partial = path.with_name(path.name + ".part")
    partial.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial.replace(path)


def resolve_project_path(project: Path, raw: str) -> Path:
    value = Path(str(raw)).expanduser()
    return value.resolve() if value.is_absolute() else (project / value).resolve()


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def scene_records(data: Any) -> list[dict[str, Any]]:
    source = data if isinstance(data, list) else data.get("scenes", []) if isinstance(data, dict) else []
    return [item for item in source if isinstance(item, dict)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    project = args.project.expanduser().resolve()
    paths = {
        "scenes": project / "scenes.json",
        "timeline": project / "audio/timeline.json",
        "captions": project / "audio/caption-groups.json",
        "words": project / "audio/caption-words.json",
        "visual_assets": project / "visual-assets.json",
        "motion": project / ".hyperframes/semantic-motion.json",
        "bindings": project / ".hyperframes/composition-bindings.json",
    }
    report_path = (args.report or project / ".hyperframes/alignment-qc.json").expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    for name, path in paths.items():
        if not path.is_file():
            errors.append(f"missing {name}: {path}")
    if errors:
        report = {"schema_version": 1, "status": "fail", "errors": errors, "warnings": warnings}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    scenes_doc = load_json(paths["scenes"])
    timeline = load_json(paths["timeline"])
    captions = load_json(paths["captions"])
    words_doc = load_json(paths["words"])
    visual_assets = load_json(paths["visual_assets"])
    motion = load_json(paths["motion"])
    bindings_doc = load_json(paths["bindings"])

    scenes = scene_records(scenes_doc)
    timings = scene_records(timeline)
    motion_scenes = scene_records(motion)
    scene_ids = [str(item.get("id")) for item in scenes]
    timing_ids = [str(item.get("id")) for item in timings]
    motion_ids = [str(item.get("id")) for item in motion_scenes]
    if not scene_ids or scene_ids != timing_ids or scene_ids != motion_ids:
        errors.append("scene IDs/order differ across scenes, timeline, and semantic motion")

    scenes_by_id = {str(item.get("id")): item for item in scenes}
    timing_by_id = {str(item.get("id")): item for item in timings}
    motion_by_id = {str(item.get("id")): item for item in motion_scenes}

    audio_path = resolve_project_path(project, str(timeline.get("audio", "")))
    expected_duration = float(timeline.get("total_duration_s", 0.0))
    actual_duration = None
    if not audio_path.is_file():
        errors.append(f"timeline audio is missing: {audio_path}")
    elif audio_path.suffix.lower() != ".wav":
        errors.append("timeline audio must be the narration master WAV")
    else:
        actual_duration = wav_duration(audio_path)
        if not math.isclose(actual_duration, expected_duration, abs_tol=1 / 30):
            errors.append(
                f"narration master duration {actual_duration:.6f}s != timeline {expected_duration:.6f}s"
            )

    caption_groups = captions.get("groups", []) if isinstance(captions, dict) else []
    caption_ids = [str(item.get("id")) for item in caption_groups if isinstance(item, dict)]
    if len(caption_ids) != len(set(caption_ids)):
        errors.append("caption group IDs must be unique")
    captions_by_scene: dict[str, list[dict[str, Any]]] = {scene_id: [] for scene_id in scene_ids}
    for group in caption_groups if isinstance(caption_groups, list) else []:
        if not isinstance(group, dict):
            errors.append("caption group must be an object")
            continue
        scene_id = str(group.get("scene", ""))
        if scene_id not in timing_by_id:
            errors.append(f"caption {group.get('id')} references unknown scene {scene_id}")
            continue
        start, end = float(group.get("start", -1)), float(group.get("end", -1))
        scene_start = float(timing_by_id[scene_id].get("start_s", 0))
        scene_end = float(timing_by_id[scene_id].get("end_s", 0))
        if start < scene_start - 1 / 30 or end > scene_end + 1 / 30 or end <= start:
            errors.append(f"caption {group.get('id')} timing leaves scene {scene_id}")
        text = str(group.get("text", "")).strip()
        boundary_text = text.rstrip(TRAILING_CLOSERS)
        if not boundary_text or boundary_text[-1] not in CAPTION_BOUNDARIES:
            errors.append(f"caption {group.get('id')} does not end at punctuation: {text}")
        captions_by_scene[scene_id].append(group)

    for scene_id, groups in captions_by_scene.items():
        groups.sort(key=lambda item: float(item.get("start", 0)))
        for left, right in zip(groups, groups[1:]):
            if float(right.get("start", 0)) < float(left.get("end", 0)) - 1e-6:
                errors.append(f"{scene_id}: caption groups overlap")
        caption_text = normalize("".join(str(item.get("text", "")) for item in groups))
        source_text = normalize(str(scenes_by_id.get(scene_id, {}).get("text", "")))
        if caption_text != source_text:
            errors.append(f"{scene_id}: caption text does not exactly cover narration text")

    words = words_doc.get("words", []) if isinstance(words_doc, dict) else []
    word_by_id: dict[str, dict[str, Any]] = {}
    words_by_scene: dict[str, list[dict[str, Any]]] = {scene_id: [] for scene_id in scene_ids}
    for word in words if isinstance(words, list) else []:
        if not isinstance(word, dict):
            errors.append("caption word must be an object")
            continue
        word_id = str(word.get("id", ""))
        scene_id = str(word.get("scene", word.get("scene_id", "")))
        if not word_id or word_id in word_by_id:
            errors.append(f"caption word ID is missing or duplicated: {word_id or '<missing>'}")
            continue
        word_by_id[word_id] = word
        if scene_id not in timing_by_id:
            errors.append(f"word {word_id} references unknown scene {scene_id}")
            continue
        start = float(word.get("start", word.get("start_s", -1)))
        end = float(word.get("end", word.get("end_s", -1)))
        scene_start = float(timing_by_id[scene_id].get("start_s", 0))
        scene_end = float(timing_by_id[scene_id].get("end_s", 0))
        if start < scene_start - 1 / 30 or end > scene_end + 1 / 30 or end < start:
            errors.append(f"word {word_id} timing leaves scene {scene_id}")
        words_by_scene[scene_id].append(word)

    for scene_id, scene_words in words_by_scene.items():
        scene_words.sort(key=lambda item: float(item.get("start", item.get("start_s", 0))))
        word_text = normalize("".join(str(item.get("text", "")) for item in scene_words))
        source_text = normalize(str(scenes_by_id.get(scene_id, {}).get("text", "")))
        if word_text != source_text:
            errors.append(f"{scene_id}: caption words do not exactly cover narration text")

    planned_beats: dict[str, dict[str, Any]] = {}
    for scene_id in scene_ids:
        scene = motion_by_id.get(scene_id, {})
        asset_refs = scene.get("asset_refs", []) if isinstance(scene, dict) else []
        for beat in scene.get("beats", []) if isinstance(scene, dict) else []:
            if not isinstance(beat, dict):
                continue
            beat_id = str(beat.get("id", ""))
            if not beat_id or beat_id in planned_beats:
                errors.append(f"semantic beat ID is missing or duplicated: {beat_id or '<missing>'}")
                continue
            planned_beats[beat_id] = {**beat, "scene_id": scene_id}
            anchor = str(beat.get("semantic_anchor", ""))
            if normalize(anchor) not in normalize(str(scenes_by_id.get(scene_id, {}).get("text", ""))):
                errors.append(f"{beat_id}: semantic anchor is absent from narration text")
            word_id = str(beat.get("word_id", ""))
            aligned_word = word_by_id.get(word_id)
            if not aligned_word:
                errors.append(f"{beat_id}: word_id is absent from caption words")
            else:
                word_start = float(aligned_word.get("start", aligned_word.get("start_s", -1)))
                if not math.isclose(float(beat.get("cue_s", -1)), word_start, abs_tol=1 / 30):
                    errors.append(f"{beat_id}: cue_s does not match aligned word start")
            visual = beat.get("visual")
            if not isinstance(visual, dict) or not str(visual.get("title", "")).strip():
                errors.append(f"{beat_id}: visual title binding is missing")
            if not str(beat.get("target_ref", "")).strip():
                errors.append(f"{beat_id}: target_ref is missing")
        scene_shots = {
            str(item.get("shot_id")) for item in visual_assets.get("shot_list", [])
            if isinstance(item, dict) and str(item.get("scene_id", "")) == scene_id
        }
        if scene_shots and not scene_shots.intersection(str(item) for item in asset_refs):
            errors.append(f"{scene_id}: semantic motion is not linked to the scene image shot")

    assets_by_scene: dict[str, list[dict[str, Any]]] = {scene_id: [] for scene_id in scene_ids}
    for asset in visual_assets.get("assets", []) if isinstance(visual_assets, dict) else []:
        if not isinstance(asset, dict):
            continue
        scene_id = str(asset.get("scene_id", ""))
        if scene_id in assets_by_scene:
            assets_by_scene[scene_id].append(asset)
        asset_path = resolve_project_path(project, str(asset.get("path", "")))
        if not asset_path.is_file():
            errors.append(f"image asset is missing: {asset_path}")
        elif str(asset.get("sha256", "")) != sha256(asset_path):
            errors.append(f"image asset hash changed: {asset_path}")
    for scene_id, scene_assets in assets_by_scene.items():
        if len(scene_assets) != 1:
            errors.append(f"{scene_id}: expected exactly one scene image asset, found {len(scene_assets)}")

    if str(bindings_doc.get("semantic_motion_sha256", "")) != sha256(paths["motion"]):
        errors.append("composition bindings are stale for the semantic motion plan")
    bindings = bindings_doc.get("bindings", []) if isinstance(bindings_doc, dict) else []
    binding_by_beat: dict[str, dict[str, Any]] = {}
    for binding in bindings if isinstance(bindings, list) else []:
        if not isinstance(binding, dict):
            errors.append("composition binding must be an object")
            continue
        beat_id = str(binding.get("beat_id", ""))
        if not beat_id or beat_id in binding_by_beat:
            errors.append(f"composition binding beat is missing or duplicated: {beat_id or '<missing>'}")
            continue
        binding_by_beat[beat_id] = binding
    if set(binding_by_beat) != set(planned_beats):
        missing = sorted(set(planned_beats) - set(binding_by_beat))
        extra = sorted(set(binding_by_beat) - set(planned_beats))
        errors.append(f"composition bindings do not cover every planned beat: missing={missing}, extra={extra}")

    for beat_id, beat in planned_beats.items():
        binding = binding_by_beat.get(beat_id)
        if not binding:
            continue
        scene_id = str(beat.get("scene_id"))
        if str(binding.get("scene_id")) != scene_id:
            errors.append(f"{beat_id}: binding scene differs from motion plan")
        if not math.isclose(float(binding.get("cue_s", -1)), float(beat.get("cue_s", -2)), abs_tol=1e-6):
            errors.append(f"{beat_id}: binding cue differs from motion plan")
        if binding.get("visual") != beat.get("visual"):
            errors.append(f"{beat_id}: binding visual text differs from motion plan")
        selector = str(binding.get("selector", ""))
        composition_path = resolve_project_path(project, str(binding.get("composition", "")))
        if not composition_path.is_file():
            errors.append(f"{beat_id}: composition is missing: {composition_path}")
            continue
        source = composition_path.read_text(encoding="utf-8")
        selector_id = selector.removeprefix("#")
        if not selector_id or f'id="{selector_id}"' not in source:
            errors.append(f"{beat_id}: selector is absent from composition: {selector}")
        visual = beat.get("visual", {}) if isinstance(beat.get("visual"), dict) else {}
        for field in ("title", "detail"):
            value = str(visual.get(field, "")).strip()
            if value and html.escape(value, quote=True) not in source:
                errors.append(f"{beat_id}: visual {field} is absent from composition DOM")
        sidecar_path = composition_path.with_suffix(".motion.json")
        if not sidecar_path.is_file():
            errors.append(f"{beat_id}: motion sidecar is missing")
        else:
            sidecar = load_json(sidecar_path)
            appears = {
                str(item.get("selector")) for item in sidecar.get("assertions", [])
                if isinstance(item, dict) and item.get("kind") == "appearsBy"
            }
            if selector not in appears:
                errors.append(f"{beat_id}: motion sidecar does not assert the bound selector")

    report = {
        "schema_version": 1,
        "status": "pass" if not errors else "fail",
        "project": str(project),
        "inputs": {name: {"path": str(path), "sha256": sha256(path)} for name, path in paths.items()},
        "audio": {
            "path": str(audio_path),
            "duration_s": round(actual_duration, 6) if actual_duration is not None else None,
            "timeline_duration_s": expected_duration,
        },
        "metrics": {
            "scenes": len(scene_ids),
            "caption_groups": len(caption_groups) if isinstance(caption_groups, list) else 0,
            "caption_words": len(words) if isinstance(words, list) else 0,
            "semantic_beats": len(planned_beats),
            "composition_bindings": len(binding_by_beat),
            "image_assets": sum(len(items) for items in assets_by_scene.values()),
        },
        "errors": errors,
        "warnings": warnings,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
