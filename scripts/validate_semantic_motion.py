#!/usr/bin/env python3
"""Validate a semantic-motion plan and write a machine-readable QC report."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from profile_config import (
    get_in,
    load_resolved_profile,
    resolve_layout_policy,
    resolve_motion_preset,
)


TIER_RANK = {"low": 0, "medium": 1, "high": 2}
REQUIRED_SAFE_ROLES = {"title", "content", "caption"}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    partial = path.with_name(path.name + ".part")
    partial.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial.replace(path)


def scene_ids(data: Any) -> list[str]:
    source = data if isinstance(data, list) else data.get("scenes", []) if isinstance(data, dict) else []
    return [str(item.get("id")) for item in source if isinstance(item, dict)]


def circle_rect_intersects(circle: dict[str, Any], rect: dict[str, Any]) -> bool:
    diameter = float(circle.get("diameter", circle.get("size", 0.0)))
    radius = diameter / 2.0
    cx = float(circle.get("x", 0.0)) + radius
    cy = float(circle.get("y", 0.0)) + radius
    rx = float(rect.get("x", 0.0))
    ry = float(rect.get("y", 0.0))
    rw = float(rect.get("width", 0.0))
    rh = float(rect.get("height", 0.0))
    nearest_x = max(rx, min(cx, rx + rw))
    nearest_y = max(ry, min(cy, ry + rh))
    return (nearest_x - cx) ** 2 + (nearest_y - cy) ** 2 < radius ** 2


def rects_intersect(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return not (
        float(left.get("x", 0)) + float(left.get("width", 0)) <= float(right.get("x", 0))
        or float(right.get("x", 0)) + float(right.get("width", 0)) <= float(left.get("x", 0))
        or float(left.get("y", 0)) + float(left.get("height", 0)) <= float(right.get("y", 0))
        or float(right.get("y", 0)) + float(right.get("height", 0)) <= float(left.get("y", 0))
    )


def validate_boxes(
    scene_id: str,
    boxes: Any,
    errors: list[str],
    warnings: list[str],
    pipeline_profile: dict[str, Any],
    *,
    requires_illustration: bool,
) -> None:
    if not isinstance(boxes, list):
        errors.append(f"{scene_id}: safe_boxes must be a list")
        return
    roles = {str(box.get("role")) for box in boxes if isinstance(box, dict)}
    required_roles = set(REQUIRED_SAFE_ROLES)
    avatar_policy = get_in(pipeline_profile, "layout.avatar_safe_zone", {})
    avatar_policy = avatar_policy if isinstance(avatar_policy, dict) else {}
    avatar_enabled = bool(avatar_policy.get("enabled"))
    if avatar_enabled:
        required_roles.add("avatar")
    if requires_illustration:
        required_roles.add("illustration")
    missing = required_roles - roles
    if missing:
        errors.append(f"{scene_id}: missing safe-box roles {sorted(missing)}")
    avatar = next((box for box in boxes if isinstance(box, dict) and box.get("role") == "avatar"), None)
    if avatar_enabled and avatar:
        canvas_height = float(get_in(pipeline_profile, "layout.canvas.height", 0))
        expected_shape = str(avatar_policy.get("shape", "circle"))
        expected_size = float(avatar_policy.get("size", 0))
        expected_x = float(avatar_policy.get("x", 0))
        expected_y = float(
            avatar_policy.get(
                "y",
                canvas_height - float(avatar_policy.get("bottom", 0)) - expected_size,
            )
        )
        if avatar.get("shape") != expected_shape:
            errors.append(f"{scene_id}: avatar safe-zone shape does not match profile")
        position_matches = (
            math.isclose(float(avatar.get("x", -1)), expected_x, abs_tol=0.5)
            and math.isclose(float(avatar.get("y", -1)), expected_y, abs_tol=0.5)
        )
        if expected_shape == "circle":
            dimensions_match = math.isclose(
                float(avatar.get("diameter", avatar.get("size", -1))), expected_size, abs_tol=0.5
            )
        else:
            dimensions_match = (
                math.isclose(
                    float(avatar.get("width", -1)),
                    float(avatar_policy.get("width", expected_size)),
                    abs_tol=0.5,
                )
                and math.isclose(
                    float(avatar.get("height", -1)),
                    float(avatar_policy.get("height", expected_size)),
                    abs_tol=0.5,
                )
            )
        if not position_matches or not dimensions_match:
            errors.append(f"{scene_id}: avatar safe zone does not match resolved profile")
        for box in boxes:
            if not isinstance(box, dict) or box is avatar or box.get("shape") != "rect":
                continue
            overlap = (
                circle_rect_intersects(avatar, box)
                if expected_shape == "circle"
                else rects_intersect(avatar, box)
            )
            if box.get("protected") and overlap:
                errors.append(f"{scene_id}: protected {box.get('role')} box intersects avatar safe zone")
    protected_rects = [
        box for box in boxes
        if isinstance(box, dict) and box.get("shape") == "rect" and box.get("protected")
    ]
    for index, left in enumerate(protected_rects):
        for right in protected_rects[index + 1 :]:
            pair = {left.get("role"), right.get("role")}
            if pair == {"title", "illustration"} or pair == {"caption", "illustration"} or pair == {"title", "caption"}:
                if rects_intersect(left, right):
                    errors.append(f"{scene_id}: protected boxes overlap: {left.get('role')} and {right.get('role')}")
    caption_x_min = float(get_in(pipeline_profile, "layout.caption_safe_x_min", 0.0) or 0.0)
    if not any(isinstance(box, dict) and box.get("role") == "caption" and float(box.get("x", 0)) >= caption_x_min for box in boxes):
        warnings.append(f"{scene_id}: caption safe box should start at x>={caption_x_min:g}")


def concurrent_count(events: list[tuple[float, float, str]], start: float, end: float, kind: str) -> int:
    points: list[tuple[float, int]] = []
    for event_start, event_end, event_kind in events:
        if event_kind != kind:
            continue
        points.append((event_start, 1))
        points.append((event_end, -1))
    count = maximum = 0
    for _, delta in sorted(points, key=lambda item: (item[0], item[1])):
        count += delta
        maximum = max(maximum, count)
    return maximum


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--scenes", type=Path)
    parser.add_argument("--timeline", type=Path)
    parser.add_argument("--resolved-profile", type=Path, required=True)
    parser.add_argument("--require-approved", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    plan_path = args.plan.expanduser().resolve()
    if not plan_path.is_file():
        raise FileNotFoundError(plan_path)
    plan = load_json(plan_path)
    pipeline_profile, resolved_profile_path = load_resolved_profile(
        args.resolved_profile, None, required=True
    )
    script_dir = Path(__file__).resolve().parent
    catalog_path = (args.catalog or script_dir.parent / "references/motion-catalog.json").expanduser().resolve()
    catalog = load_json(catalog_path)
    report_path = (args.report or plan_path.with_name("motion-qc.json")).expanduser().resolve()

    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, Any] = {"scenes": {}, "transition_families": [], "shader_transitions": 0}

    if plan.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    planner_path = Path(__file__).resolve().with_name("plan_semantic_motion.py")
    compiler = plan.get("compiler", {}) if isinstance(plan.get("compiler"), dict) else {}
    if compiler.get("name") != planner_path.name or compiler.get("sha256") != file_sha256(planner_path):
        errors.append("motion planner changed after the plan was generated; rebuild the plan")
    status = plan.get("status")
    if status not in {"draft", "approved"}:
        errors.append("status must be draft or approved")
    if args.require_approved and status != "approved":
        errors.append("plan is not approved")
    configuration = plan.get("configuration", {}) if isinstance(plan.get("configuration"), dict) else {}
    configuration_sha = get_in(pipeline_profile, "_meta.profile_sha256")
    if configuration.get("sha256") != configuration_sha:
        errors.append("motion plan is stale for the resolved profile")
    profile_id = str(plan.get("profile", {}).get("id", ""))
    if profile_id not in catalog.get("profiles", {}):
        errors.append(f"unknown profile: {profile_id or '<missing>'}")
        profile: dict[str, Any] = {}
    else:
        selectable = get_in(pipeline_profile, "motion.selectable_profiles", [])
        if isinstance(selectable, list) and selectable and profile_id not in selectable:
            errors.append(f"motion preset is not enabled by resolved profile: {profile_id}")
        profile = resolve_motion_preset(catalog, pipeline_profile, profile_id)
        declared_budgets = plan.get("profile", {}).get("budgets")
        if declared_budgets != profile:
            errors.append("effective motion budgets changed after plan generation")
    declared_catalog_hash = str(plan.get("profile", {}).get("catalog_sha256", ""))
    actual_catalog_hash = file_sha256(catalog_path)
    if declared_catalog_hash and declared_catalog_hash != actual_catalog_hash:
        errors.append("motion catalog changed after the plan was generated; rebuild the plan")

    for name, source in plan.get("sources", {}).items() if isinstance(plan.get("sources"), dict) else []:
        if not isinstance(source, dict):
            errors.append(f"source {name} must be an object")
            continue
        path = Path(str(source.get("path", ""))).expanduser()
        if not path.is_file():
            errors.append(f"source is missing: {name}={path}")
            continue
        declared = str(source.get("sha256", ""))
        if not declared or declared != file_sha256(path):
            errors.append(f"source hash changed or missing: {name}")

    expected_ids: list[str] | None = None
    if args.scenes:
        expected_ids = scene_ids(load_json(args.scenes.expanduser().resolve()))
    elif isinstance(plan.get("sources"), dict) and isinstance(plan["sources"].get("scenes"), dict):
        source_path = Path(str(plan["sources"]["scenes"].get("path", "")))
        if source_path.is_file():
            expected_ids = scene_ids(load_json(source_path))
    timeline_ids: list[str] | None = None
    if args.timeline:
        timeline_ids = scene_ids(load_json(args.timeline.expanduser().resolve()))
    elif isinstance(plan.get("sources"), dict) and isinstance(plan["sources"].get("timeline"), dict):
        source_path = Path(str(plan["sources"]["timeline"].get("path", "")))
        if source_path.is_file():
            timeline_ids = scene_ids(load_json(source_path))

    scenes = plan.get("scenes", [])
    if not isinstance(scenes, list) or not scenes:
        errors.append("plan must contain scenes")
        scenes = []
    actual_ids = [str(scene.get("id")) for scene in scenes if isinstance(scene, dict)]
    if len(actual_ids) != len(set(actual_ids)):
        errors.append("scene IDs must be unique")
    if expected_ids is not None and actual_ids != expected_ids:
        errors.append("motion-plan scene IDs/order do not match scenes JSON")
    if timeline_ids is not None and actual_ids != timeline_ids:
        errors.append("motion-plan scene IDs/order do not match timeline JSON")

    clock = plan.get("clock", {}) if isinstance(plan.get("clock"), dict) else {}
    sample_rate = int(clock.get("sample_rate", 48000))
    fps = int(clock.get("fps", get_in(pipeline_profile, "layout.canvas.fps", 30)))
    configured_fps = int(get_in(pipeline_profile, "layout.canvas.fps", fps))
    if fps != configured_fps:
        errors.append("motion-plan FPS does not match resolved profile")
    grammar = plan.get("transition_grammar", [])
    if not isinstance(grammar, list) or not grammar:
        errors.append("transition_grammar must be a non-empty list")
        grammar = []
    if profile and grammar != profile.get("transition_grammar"):
        errors.append("transition_grammar does not match selected profile")

    all_events: list[tuple[float, float, str, float]] = []
    transitions: list[str] = []
    shader_count = 0
    primitives = catalog.get("primitives", {})
    recipes = catalog.get("semantic_recipes", {})
    max_tier = TIER_RANK.get(str(profile.get("max_primitive_tier", "low")), 0) if profile else 0
    allowed_primitives = {
        str(item) for item in profile.get("allowed_primitives", [])
    } if profile else set()
    runtime_implemented: set[str] = set()
    layout_catalog = catalog.get("layout_variants", {})
    layout_policy = resolve_layout_policy(catalog, pipeline_profile)
    declared_layout_policy = plan.get("profile", {}).get("layout_policy")
    if declared_layout_policy != layout_policy:
        errors.append("effective layout policy changed after plan generation")
    allowed_layouts = {
        str(item) for item in layout_policy.get("variants", [])
    } if isinstance(layout_policy.get("variants"), list) else set(layout_catalog)
    layout_ids: list[str] = []
    if profile_id in {"premium-balanced", "cinematic"}:
        audit_source = plan.get("sources", {}).get("runtime_audit", {}) if isinstance(plan.get("sources"), dict) else {}
        audit_path = Path(str(audit_source.get("path", ""))).expanduser() if isinstance(audit_source, dict) else Path()
        if not audit_path.is_file():
            errors.append(f"advanced profile {profile_id} requires a current runtime implementation audit")
        else:
            audit = load_json(audit_path)
            if not isinstance(audit, dict) or audit.get("status") != "pass" or audit.get("profile_id") != profile_id:
                errors.append("runtime implementation audit did not pass or profile differs")
            else:
                runtime_implemented = {str(item) for item in audit.get("implemented_primitives", [])}

    for scene in scenes:
        if not isinstance(scene, dict):
            errors.append("every scene plan must be an object")
            continue
        scene_id = str(scene.get("id", "<missing>"))
        start = float(scene.get("start_s", 0.0))
        end = float(scene.get("end_s", 0.0))
        if end <= start:
            errors.append(f"{scene_id}: end_s must be greater than start_s")
            continue
        if abs(int(scene.get("start_audio_sample", -999)) - round(start * sample_rate)) > 1:
            errors.append(f"{scene_id}: start_audio_sample is not derived from start_s")
        if abs(int(scene.get("end_audio_sample", -999)) - round(end * sample_rate)) > 1:
            errors.append(f"{scene_id}: end_audio_sample is not derived from end_s")
        if abs(int(scene.get("start_render_frame", -999)) - round(start * fps)) > 1:
            errors.append(f"{scene_id}: start_render_frame is not derived from start_s")
        if abs(int(scene.get("end_render_frame", -999)) - round(end * fps)) > 1:
            errors.append(f"{scene_id}: end_render_frame is not derived from end_s")

        role = str(scene.get("semantic_role", ""))
        if role not in recipes:
            errors.append(f"{scene_id}: unknown semantic_role={role}")
        if not str(scene.get("selection_reason", "")).strip():
            errors.append(f"{scene_id}: missing selection_reason")
        layout_id = str(scene.get("layout_variant", ""))
        layout_spec = layout_catalog.get(layout_id)
        if not isinstance(layout_spec, dict):
            errors.append(f"{scene_id}: unknown layout_variant={layout_id or '<missing>'}")
        else:
            if allowed_layouts and layout_id not in allowed_layouts:
                errors.append(f"{scene_id}: layout {layout_id} is not allowed by resolved profile")
            if layout_ids and layout_policy.get("forbid_adjacent_repeat") and layout_ids[-1] == layout_id:
                errors.append(f"{scene_id}: adjacent scenes may not repeat layout {layout_id}")
            if layout_spec.get("requires_asset") and not scene.get("asset_refs"):
                errors.append(f"{scene_id}: layout {layout_id} requires a visual asset")
            if str(scene.get("presenter_anchor", "")) != str(layout_spec.get("presenter_anchor", "")):
                errors.append(f"{scene_id}: presenter_anchor does not match layout catalog")
            if not str(scene.get("layout_selection_reason", "")).strip():
                errors.append(f"{scene_id}: missing layout_selection_reason")
        layout_ids.append(layout_id)
        validate_boxes(
            scene_id,
            scene.get("safe_boxes"),
            errors,
            warnings,
            pipeline_profile,
            requires_illustration=bool(scene.get("asset_refs")),
        )

        hero = scene.get("hero_motion")
        if not isinstance(hero, dict) or not str(hero.get("id", "")):
            errors.append(f"{scene_id}: exactly one hero_motion object is required")
        support = scene.get("supporting_motions", [])
        if not isinstance(support, list):
            errors.append(f"{scene_id}: supporting_motions must be a list")
            support = []
        if profile:
            support_min, support_max = profile.get("support_motion_range", [0, 99])
            if not int(support_min) <= len(support) <= int(support_max):
                errors.append(f"{scene_id}: supporting motion count {len(support)} outside {support_min}-{support_max}")
        motion_ids = [str(hero.get("id"))] if isinstance(hero, dict) else []
        motion_ids.extend(str(item.get("id")) for item in support if isinstance(item, dict))
        if len(motion_ids) != len(set(motion_ids)):
            warnings.append(f"{scene_id}: hero/support motions contain duplicates")
        for motion_id in motion_ids:
            spec = primitives.get(motion_id)
            if not isinstance(spec, dict):
                errors.append(f"{scene_id}: unknown motion primitive {motion_id}")
            elif TIER_RANK.get(str(spec.get("tier")), 99) > max_tier:
                errors.append(f"{scene_id}: primitive {motion_id} exceeds profile tier")
            elif allowed_primitives and motion_id not in allowed_primitives:
                errors.append(f"{scene_id}: primitive {motion_id} is not implemented by profile {profile_id}")
            elif runtime_implemented and motion_id not in runtime_implemented:
                errors.append(f"{scene_id}: primitive {motion_id} is absent from runtime implementation audit")

        transition = scene.get("transition_in", {}) if isinstance(scene.get("transition_in"), dict) else {}
        transition_id = str(transition.get("id", ""))
        transitions.append(transition_id)
        if transition_id != "cut" and transition_id not in grammar:
            errors.append(f"{scene_id}: transition {transition_id} is outside transition grammar")
        transition_spec = catalog.get("transitions", {}).get(transition_id)
        if not isinstance(transition_spec, dict):
            errors.append(f"{scene_id}: unknown transition {transition_id}")
        elif transition_spec.get("shader"):
            shader_count += 1

        beats = scene.get("beats", [])
        if not isinstance(beats, list) or not beats:
            errors.append(f"{scene_id}: beats must be a non-empty list")
            beats = []
        previous_cue = -math.inf
        beat_ids: list[str] = []
        target_refs: list[str] = []
        primary_times: list[float] = []
        motion_windows: list[tuple[float, float, str]] = []
        kinetic_time = 0.0
        for beat in beats:
            if not isinstance(beat, dict):
                errors.append(f"{scene_id}: beat must be an object")
                continue
            beat_id = str(beat.get("id", "<missing beat>"))
            beat_ids.append(beat_id)
            cue = float(beat.get("cue_s", -1.0))
            settle = float(beat.get("settle_s", cue))
            if not start <= cue <= end:
                errors.append(f"{beat_id}: cue_s is outside the scene")
            if settle < cue or settle > end + 1e-6:
                errors.append(f"{beat_id}: settle_s is invalid")
            if cue < previous_cue:
                errors.append(f"{scene_id}: beat cues must be monotonic")
            previous_cue = cue
            if abs(int(beat.get("audio_sample", -999)) - round(cue * sample_rate)) > 1:
                errors.append(f"{beat_id}: audio_sample does not match cue_s")
            if abs(int(beat.get("render_frame", -999)) - round(cue * fps)) > 1:
                errors.append(f"{beat_id}: render_frame does not match cue_s")
            if not str(beat.get("semantic_anchor", "")).strip():
                errors.append(f"{beat_id}: semantic_anchor is required")
            target_ref = str(beat.get("target_ref", "")).strip()
            if not target_ref:
                errors.append(f"{beat_id}: target_ref is required")
            target_refs.append(target_ref)
            visual = beat.get("visual")
            if not isinstance(visual, dict):
                errors.append(f"{beat_id}: visual binding is required")
            else:
                if not str(visual.get("title", "")).strip():
                    errors.append(f"{beat_id}: visual.title is required")
                try:
                    slot = int(visual.get("slot", 0))
                except (TypeError, ValueError):
                    slot = 0
                if slot not in {1, 2, 3, 4}:
                    errors.append(f"{beat_id}: visual.slot must be 1..4")
            if beat.get("cue_source") not in {"scene-start", "caption-word", "prosody-proportional"}:
                errors.append(f"{beat_id}: unsupported cue_source")
            primitive_id = str(beat.get("primitive", ""))
            if primitive_id not in primitives:
                errors.append(f"{beat_id}: unknown primitive {primitive_id}")
            elif TIER_RANK.get(str(primitives[primitive_id].get("tier")), 99) > max_tier:
                errors.append(f"{beat_id}: primitive {primitive_id} exceeds profile tier")
            elif allowed_primitives and primitive_id not in allowed_primitives:
                errors.append(f"{beat_id}: primitive {primitive_id} is not implemented by profile {profile_id}")
            elif runtime_implemented and primitive_id not in runtime_implemented:
                errors.append(f"{beat_id}: primitive {primitive_id} is absent from runtime implementation audit")
            chain = beat.get("fallback_chain", [])
            if not isinstance(chain, list) or not chain or chain[0] != primitive_id or chain[-1] != "static-step":
                errors.append(f"{beat_id}: fallback_chain must start with primitive and end with static-step")
            elif any(item not in primitives for item in chain):
                errors.append(f"{beat_id}: fallback_chain contains an unknown primitive")
            else:
                disallowed_fallbacks = [
                    item for item in chain
                    if TIER_RANK.get(str(primitives[item].get("tier")), 99) > max_tier
                    or (allowed_primitives and item not in allowed_primitives)
                    or (runtime_implemented and item not in runtime_implemented)
                ]
                if disallowed_fallbacks:
                    errors.append(f"{beat_id}: fallback_chain exceeds profile/runtime implementation: {disallowed_fallbacks}")
            if beat.get("seek_safe") is not True or beat.get("loop") is not False:
                errors.append(f"{beat_id}: motion must be seek_safe and non-looping")
            priority = str(beat.get("priority", "support"))
            if priority == "primary":
                primary_times.append(cue)
            kind = "primary" if priority == "primary" else "support"
            motion_windows.append((cue, settle, kind))
            kinetic_time += max(0.0, settle - cue)
            cost = float(beat.get("motion_cost", 0.0))
            all_events.append((cue, settle, kind, cost))

        if len(beat_ids) != len(set(beat_ids)):
            errors.append(f"{scene_id}: beat IDs must be unique")
        if len(target_refs) != len(set(target_refs)):
            errors.append(f"{scene_id}: target_ref values must be unique")

        asset_refs = scene.get("asset_refs", [])
        if not isinstance(asset_refs, list):
            errors.append(f"{scene_id}: asset_refs must be a list")
            asset_refs = []
        visual_source = plan.get("sources", {}).get("visual_assets", {}) if isinstance(plan.get("sources"), dict) else {}
        visual_path = Path(str(visual_source.get("path", ""))) if isinstance(visual_source, dict) else Path()
        if visual_path.is_file():
            visual_data = load_json(visual_path)
            scene_shots = {
                str(item.get("shot_id")) for item in visual_data.get("shot_list", [])
                if isinstance(item, dict) and str(item.get("scene_id", "")) == scene_id
            }
            if scene_shots and not scene_shots.intersection(str(item) for item in asset_refs):
                errors.append(f"{scene_id}: motion plan is not bound to its visual asset shot")

        if profile:
            min_gap = float(profile.get("min_primary_gap_s", 0.0))
            for left, right in zip(primary_times, primary_times[1:]):
                if right - left < min_gap:
                    errors.append(f"{scene_id}: primary beats are only {right-left:.2f}s apart (min {min_gap:.2f}s)")
            peak_primary = concurrent_count(motion_windows, start, end, "primary")
            peak_support = concurrent_count(motion_windows, start, end, "support")
            if peak_primary > int(profile.get("max_primary_concurrent", 1)):
                errors.append(f"{scene_id}: too many concurrent primary motions ({peak_primary})")
            if peak_support > int(profile.get("max_support_concurrent", 1)):
                errors.append(f"{scene_id}: too many concurrent support motions ({peak_support})")
            duty_cycle = kinetic_time / (end - start)
            if duty_cycle > float(profile.get("max_kinetic_duty_cycle", 1.0)) + 0.02:
                warnings.append(f"{scene_id}: kinetic duty cycle {duty_cycle:.2f} exceeds profile target")
        else:
            peak_primary = peak_support = 0
            duty_cycle = 0.0

        holds = scene.get("intentional_holds", [])
        if not isinstance(holds, list):
            errors.append(f"{scene_id}: intentional_holds must be a list")
            holds = []
        for hold in holds:
            if not isinstance(hold, dict):
                errors.append(f"{scene_id}: hold must be an object")
                continue
            if hold.get("intentional") is not True or hold.get("reason_code") in {None, "", "review-required"}:
                message = f"{scene_id}: long hold needs intentional=true and a specific reason_code"
                (errors if args.require_approved or status == "approved" else warnings).append(message)

        metrics["scenes"][scene_id] = {
            "duration_s": round(end - start, 3),
            "events": len(beats),
            "primary_events": len(primary_times),
            "peak_primary_concurrent": peak_primary,
            "peak_support_concurrent": peak_support,
            "kinetic_duty_cycle": round(duty_cycle, 4),
            "holds": len(holds),
        }

    transition_families = sorted({item for item in transitions if item and item != "cut"})
    distinct_layouts = sorted(set(layout_ids))
    metrics["layout_variants"] = layout_ids
    metrics["distinct_layouts"] = distinct_layouts
    minimum_layouts = min(
        len(layout_ids), int(layout_policy.get("minimum_distinct_layouts_per_episode", 1))
    )
    if len(distinct_layouts) < minimum_layouts:
        errors.append(
            f"layout variety too low: {len(distinct_layouts)} distinct, require {minimum_layouts}"
        )
    metrics["transition_families"] = transition_families
    metrics["shader_transitions"] = shader_count
    if profile:
        if len(transition_families) > int(profile.get("max_transition_families", 3)):
            errors.append("too many transition families for the selected profile")
        if shader_count > int(profile.get("max_shader_transitions", 0)):
            errors.append("too many shader transitions for the selected profile")
        rolling_limit = float(profile.get("max_rolling_motion_points_10s", 999.0))
        peak_points = 0.0
        peak_at = 0.0
        event_times = sorted(event[0] for event in all_events)
        for start_time in event_times:
            points = sum(event[3] for event in all_events if start_time <= event[0] < start_time + 10.0)
            if points > peak_points:
                peak_points = points
                peak_at = start_time
        metrics["peak_motion_points_10s"] = round(peak_points, 3)
        metrics["peak_motion_points_at_s"] = round(peak_at, 3)
        if peak_points > rolling_limit + 1e-6:
            errors.append(f"rolling motion density {peak_points:.2f} exceeds profile limit {rolling_limit:.2f}")

    if status == "approved":
        review = plan.get("review", {}) if isinstance(plan.get("review"), dict) else {}
        if not str(review.get("approved_by", "")).strip():
            errors.append("approved plan must record review.approved_by")

    report = {
        "schema_version": 1,
        "status": "pass" if not errors else "fail",
        "plan": {"path": str(plan_path), "sha256": file_sha256(plan_path), "approval_status": status},
        "profile_id": profile_id,
        "configuration": {
            "path": str(resolved_profile_path),
            "id": pipeline_profile.get("profile_id"),
            "sha256": configuration_sha,
        },
        "errors": errors,
        "warnings": warnings,
        "metrics": metrics,
        "gates": {
            "approved_required": args.require_approved,
            "source_hashes": "pass" if not any("source" in error for error in errors) else "fail",
            "semantic_timing": "pass" if not any("cue" in error or "audio_sample" in error or "render_frame" in error for error in errors) else "fail",
            "layout_plan": "pass" if not any("safe" in error or "intersect" in error or "overlap" in error for error in errors) else "fail",
            "density": "pass" if not any("density" in error or "concurrent" in error or "primary beats" in error for error in errors) else "fail",
            "seek_safe": "pass" if not any("seek_safe" in error or "fallback_chain" in error for error in errors) else "fail",
            "visual_binding": "pass" if not any("visual." in error or "visual binding" in error or "target_ref" in error or "visual asset" in error for error in errors) else "fail",
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
