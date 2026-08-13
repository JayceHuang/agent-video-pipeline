#!/usr/bin/env python3
"""Shared profile loading, merging, validation, and hashing helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = SKILL_ROOT / "references/default-profile.yaml"
PROFILE_SCHEMA = SKILL_ROOT / "references/profile-schema.json"
ALLOWED_TOP_LEVEL = {
    "profile_version",
    "profile_id",
    "content",
    "production_fast_path",
    "pipeline_runtime",
    "tts_runtime",
    "voice",
    "motion",
    "layout",
    "images",
    "episode",
    "publishing",
    "delivery",
    "avatar",
    "_meta",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_mapping(path: Path) -> dict[str, Any]:
    path = path.expanduser().absolute()
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        value = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "YAML profile support requires PyYAML; install the Skill requirements.txt"
            ) from exc
        value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise ValueError(f"profile root must be an object: {path}")
    return value


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        previous = result.get(key)
        if isinstance(previous, dict) and isinstance(value, dict):
            result[key] = deep_merge(previous, value)
        else:
            result[key] = value
    return result


def get_in(value: dict[str, Any], dotted: str, default: Any = None) -> Any:
    current: Any = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def resolve_motion_preset(
    catalog: dict[str, Any],
    pipeline_profile: dict[str, Any],
    preset_id: str,
) -> dict[str, Any]:
    """Merge a standard catalog preset with external resolved-profile overrides."""
    presets = catalog.get("profiles")
    if not isinstance(presets, dict) or not isinstance(presets.get(preset_id), dict):
        raise ValueError(f"unknown motion preset: {preset_id}")
    resolved = deep_merge({}, presets[preset_id])
    motion = pipeline_profile.get("motion")
    motion = motion if isinstance(motion, dict) else {}

    hierarchy = motion.get("hierarchy")
    hierarchy = hierarchy if isinstance(hierarchy, dict) else {}
    hierarchy_mapping = {
        "supporting_motion_range": "support_motion_range",
        "max_major_camera_moves_per_scene": "max_major_camera_moves_per_scene",
        "max_simultaneous_animated_elements": "max_simultaneous_animated_elements",
        "max_primary_concurrent": "max_primary_concurrent",
        "max_support_concurrent": "max_support_concurrent",
    }
    for source, target in hierarchy_mapping.items():
        if source in hierarchy:
            resolved[target] = hierarchy[source]

    density = motion.get("density")
    density = density if isinstance(density, dict) else {}
    for key in (
        "event_interval_s",
        "max_events_per_scene",
        "max_simultaneous_animated_elements",
        "max_primary_concurrent",
        "max_support_concurrent",
        "max_rolling_motion_points_10s",
        "max_kinetic_duty_cycle",
        "max_continuous_motion_s",
        "min_primary_gap_s",
    ):
        if key in density:
            resolved[key] = density[key]

    transitions = motion.get("transitions")
    transitions = transitions if isinstance(transitions, dict) else {}
    explicit_grammar = transitions.get("grammar")
    if isinstance(explicit_grammar, list) and explicit_grammar:
        resolved["transition_grammar"] = list(dict.fromkeys(str(item) for item in explicit_grammar))
    else:
        grammar = [
            str(transitions[key])
            for key in ("primary", "accent", "soft")
            if str(transitions.get(key, "")).strip()
        ]
        if grammar:
            resolved["transition_grammar"] = list(dict.fromkeys(grammar))
    if "max_families_per_video" in transitions:
        resolved["max_transition_families"] = transitions["max_families_per_video"]

    layout_policy = motion.get("layout_policy")
    if isinstance(layout_policy, dict) and isinstance(layout_policy.get("variants"), list):
        resolved["layout_variants"] = [str(item) for item in layout_policy["variants"]]

    overrides = motion.get("preset_overrides")
    if isinstance(overrides, dict):
        common = overrides.get("common")
        if isinstance(common, dict):
            resolved = deep_merge(resolved, common)
        selected = overrides.get(preset_id)
        if isinstance(selected, dict):
            resolved = deep_merge(resolved, selected)
    resolved["layout_policy"] = resolve_layout_policy(catalog, pipeline_profile)
    return resolved


def resolve_layout_policy(
    catalog: dict[str, Any], pipeline_profile: dict[str, Any]
) -> dict[str, Any]:
    baseline = catalog.get("layout_policy")
    baseline = baseline if isinstance(baseline, dict) else {}
    external = get_in(pipeline_profile, "motion.layout_policy", {})
    external = external if isinstance(external, dict) else {}
    return deep_merge(baseline, external)


def resolved_profile_path(
    explicit: Path | None = None,
    project: Path | None = None,
    *,
    required: bool = True,
) -> Path | None:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit.expanduser().absolute())
    if project is not None:
        candidates.append(project.expanduser().absolute() / ".pipeline/resolved-profile.json")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if required:
        rendered = ", ".join(str(path) for path in candidates) or "<no candidate>"
        raise FileNotFoundError(
            "resolved profile is required; run resolve_profile.py first or pass --profile "
            f"(checked: {rendered})"
        )
    return None


def load_resolved_profile(
    explicit: Path | None = None,
    project: Path | None = None,
    *,
    required: bool = True,
) -> tuple[dict[str, Any], Path | None]:
    path = resolved_profile_path(explicit, project, required=required)
    if path is None:
        return {}, None
    profile = load_mapping(path)
    errors = validate_profile(profile) + validate_resolved_profile(profile, path)
    if errors:
        raise ValueError("invalid resolved profile: " + "; ".join(errors))
    return profile, path


def validate_resolved_profile(profile: dict[str, Any], path: Path) -> list[str]:
    """Validate the frozen hash and all source hashes of a resolved profile."""
    errors: list[str] = []
    meta = profile.get("_meta")
    if not isinstance(meta, dict) or not str(meta.get("profile_sha256", "")).strip():
        errors.append("resolved profile is missing _meta.profile_sha256")
    else:
        expected_sha = str(meta["profile_sha256"])
        actual_sha = canonical_sha256({key: value for key, value in profile.items() if key != "_meta"})
        if expected_sha != actual_sha:
            errors.append("resolved profile content differs from _meta.profile_sha256")
        sidecar = path.with_suffix(".sha256")
        if sidecar.is_file() and sidecar.read_text(encoding="utf-8").strip() != expected_sha:
            errors.append("resolved profile SHA sidecar is stale")
        sources = meta.get("sources")
        if not isinstance(sources, list) or not sources:
            errors.append("resolved profile is missing source provenance")
        else:
            for index, source in enumerate(sources, start=1):
                if not isinstance(source, dict):
                    errors.append(f"resolved profile source {index} is invalid")
                    continue
                source_path = Path(str(source.get("path", ""))).expanduser().absolute()
                if not source_path.is_file():
                    errors.append(f"resolved profile source is missing: {source_path}")
                elif source.get("sha256") != sha256_file(source_path):
                    errors.append(f"resolved profile source is stale: {source_path}")
    return errors


def validate_profile(profile: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    unknown = sorted(set(profile) - ALLOWED_TOP_LEVEL)
    if unknown:
        errors.append(f"unknown top-level fields: {', '.join(unknown)}")
    if not isinstance(profile.get("profile_version"), int):
        errors.append("profile_version must be an integer")
    if not str(profile.get("profile_id", "")).strip():
        errors.append("profile_id must be a non-empty string")

    for section in ("voice", "motion", "layout", "episode"):
        if not isinstance(profile.get(section), dict):
            errors.append(f"{section} must be an object")

    pipeline_runtime = profile.get("pipeline_runtime")
    if pipeline_runtime is not None and not isinstance(pipeline_runtime, dict):
        errors.append("pipeline_runtime must be an object")
    elif isinstance(pipeline_runtime, dict):
        value = pipeline_runtime.get("python")
        if value is not None and (not isinstance(value, str) or not value.strip()):
            errors.append("pipeline_runtime.python must be a non-empty string or null")

    tts_runtime = profile.get("tts_runtime")
    if tts_runtime is not None and not isinstance(tts_runtime, dict):
        errors.append("tts_runtime must be an object")
    elif isinstance(tts_runtime, dict):
        for key in ("generator_python", "model_path", "aligner_python", "generator"):
            value = tts_runtime.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                errors.append(f"tts_runtime.{key} must be a non-empty string or null")

    width = get_in(profile, "layout.canvas.width")
    height = get_in(profile, "layout.canvas.height")
    fps = get_in(profile, "layout.canvas.fps")
    for name, value in (("width", width), ("height", height), ("fps", fps)):
        if not isinstance(value, (int, float)) or value <= 0:
            errors.append(f"layout.canvas.{name} must be positive")

    target = get_in(profile, "voice.target_effective_chinese_chars_per_minute")
    allowed = get_in(profile, "voice.allowed_range")
    if not isinstance(target, (int, float)) or target <= 0:
        errors.append("voice.target_effective_chinese_chars_per_minute must be positive")
    if (
        not isinstance(allowed, list)
        or len(allowed) != 2
        or not all(isinstance(item, (int, float)) for item in allowed)
        or allowed[0] > allowed[1]
    ):
        errors.append("voice.allowed_range must be [minimum, maximum]")

    avatar = get_in(profile, "layout.avatar_safe_zone", {})
    if isinstance(avatar, dict) and avatar.get("enabled"):
        if avatar.get("shape") not in {"circle", "rectangle", "rounded-rectangle"}:
            errors.append("layout.avatar_safe_zone.shape is unsupported")
        if not isinstance(avatar.get("size"), (int, float)) or avatar["size"] <= 0:
            errors.append("layout.avatar_safe_zone.size must be positive when enabled")

    illustration = get_in(profile, "layout.illustration_skill", {})
    if isinstance(illustration, dict) and illustration.get("required"):
        if not str(illustration.get("provider") or illustration.get("name") or "").strip():
            errors.append("required illustration_skill must declare provider or name")
        for key in ("shot_count_range", "asset_count_range"):
            bounds = illustration.get(key)
            if (
                not isinstance(bounds, list)
                or len(bounds) != 2
                or not all(isinstance(item, int) and item >= 0 for item in bounds)
                or bounds[0] > bounds[1]
            ):
                errors.append(f"layout.illustration_skill.{key} must be [minimum, maximum]")

    cta = get_in(profile, "episode.final_cta", "")
    if cta is not None and not isinstance(cta, str):
        errors.append("episode.final_cta must be a string or null")
    cta_separator = get_in(profile, "episode.cta_separator", " ")
    if not isinstance(cta_separator, str):
        errors.append("episode.cta_separator must be a string")
    return errors


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + ".tmp")
    staged.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    staged.replace(path)
