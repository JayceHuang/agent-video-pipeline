#!/usr/bin/env python3
"""Validate actual storyboard layout boxes and swept animation bounds."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from profile_config import get_in, load_resolved_profile


REQUIRED_ROLES = {"title", "content", "caption"}
OVERLAP_EXEMPT_ROLES = {"background", "transition"}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    partial = path.with_name(path.name + ".part")
    partial.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial.replace(path)


def box(element: dict[str, Any]) -> dict[str, Any]:
    if element.get("animated") is True and isinstance(element.get("swept_bbox"), dict):
        return {**element["swept_bbox"], "shape": "rect"}
    return element


def time_overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        float(left.get("start_s", 0)) < float(right.get("end_s", 0))
        and float(right.get("start_s", 0)) < float(left.get("end_s", 0))
    )


def rect_rect(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return not (
        float(left.get("x", 0)) + float(left.get("width", 0)) <= float(right.get("x", 0))
        or float(right.get("x", 0)) + float(right.get("width", 0)) <= float(left.get("x", 0))
        or float(left.get("y", 0)) + float(left.get("height", 0)) <= float(right.get("y", 0))
        or float(right.get("y", 0)) + float(right.get("height", 0)) <= float(left.get("y", 0))
    )


def circle_circle(left: dict[str, Any], right: dict[str, Any]) -> bool:
    lr = float(left.get("diameter", left.get("size", 0))) / 2
    rr = float(right.get("diameter", right.get("size", 0))) / 2
    lx, ly = float(left.get("x", 0)) + lr, float(left.get("y", 0)) + lr
    rx, ry = float(right.get("x", 0)) + rr, float(right.get("y", 0)) + rr
    return (lx - rx) ** 2 + (ly - ry) ** 2 < (lr + rr) ** 2


def circle_rect(circle: dict[str, Any], rect: dict[str, Any]) -> bool:
    radius = float(circle.get("diameter", circle.get("size", 0))) / 2
    cx, cy = float(circle.get("x", 0)) + radius, float(circle.get("y", 0)) + radius
    rx, ry = float(rect.get("x", 0)), float(rect.get("y", 0))
    rw, rh = float(rect.get("width", 0)), float(rect.get("height", 0))
    nearest_x, nearest_y = max(rx, min(cx, rx + rw)), max(ry, min(cy, ry + rh))
    return (nearest_x - cx) ** 2 + (nearest_y - cy) ** 2 < radius ** 2


def intersects(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_box, right_box = box(left), box(right)
    def geometry_shape(value: Any) -> Any:
        return "rect" if value in {"rect", "rectangle", "rounded-rectangle"} else value

    shapes = (geometry_shape(left_box.get("shape")), geometry_shape(right_box.get("shape")))
    if shapes == ("rect", "rect"):
        return rect_rect(left_box, right_box)
    if shapes == ("circle", "circle"):
        return circle_circle(left_box, right_box)
    if shapes == ("circle", "rect"):
        return circle_rect(left_box, right_box)
    if shapes == ("rect", "circle"):
        return circle_rect(right_box, left_box)
    return False


def within_canvas(element: dict[str, Any], width: float, height: float) -> bool:
    value = box(element)
    x, y = float(value.get("x", -1)), float(value.get("y", -1))
    if value.get("shape") == "circle":
        w = h = float(value.get("diameter", value.get("size", 0)))
    elif value.get("shape") in {"rect", "rectangle", "rounded-rectangle"}:
        w, h = float(value.get("width", 0)), float(value.get("height", 0))
    else:
        return False
    return x >= 0 and y >= 0 and w > 0 and h > 0 and x + w <= width and y + h <= height


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout", type=Path, required=True)
    parser.add_argument("--motion-plan", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True, help="resolved profile JSON")
    parser.add_argument("--require-approved", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    layout_path = args.layout.expanduser().resolve()
    motion_path = args.motion_plan.expanduser().resolve()
    layout = load_json(layout_path)
    motion = load_json(motion_path)
    profile, profile_path = load_resolved_profile(args.profile, None, required=True)
    configured_canvas = get_in(profile, "layout.canvas", {})
    avatar_policy = get_in(profile, "layout.avatar_safe_zone", {})
    avatar_policy = avatar_policy if isinstance(avatar_policy, dict) else {}
    avatar_enabled = bool(avatar_policy.get("enabled"))
    caption_x_min = float(get_in(profile, "layout.caption_safe_x_min", 0.0) or 0.0)
    fps = float(configured_canvas.get("fps", 30.0))
    frame_tolerance = 1.0 / fps
    report_path = (args.report or layout_path.with_name("layout-qc.json")).expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    overlap_count = 0

    if layout.get("schema_version") != 1:
        errors.append("layout schema_version must be 1")
    if args.require_approved and layout.get("status") != "approved":
        errors.append("layout-boxes.json is not approved")
    if args.require_approved and layout.get("actual_dom_verified") is not True:
        errors.append("layout-boxes.json has not been verified against the actual DOM/storyboard")
    if layout.get("status") == "approved" and not str(layout.get("review", {}).get("approved_by", "")).strip():
        errors.append("approved layout-boxes.json must record review.approved_by")
    canvas = layout.get("canvas", {}) if isinstance(layout.get("canvas"), dict) else {}
    width, height = float(canvas.get("width", 0)), float(canvas.get("height", 0))
    expected_width = float(configured_canvas.get("width", 0))
    expected_height = float(configured_canvas.get("height", 0))
    if not math.isclose(width, expected_width, abs_tol=0.5) or not math.isclose(height, expected_height, abs_tol=0.5):
        errors.append(f"layout canvas must match profile: {expected_width:g}x{expected_height:g}")
    profile_sha = get_in(profile, "_meta.profile_sha256")
    if layout.get("profile", {}).get("sha256") != profile_sha:
        errors.append("layout-boxes.json is stale for the resolved profile")
    if layout.get("motion_plan", {}).get("sha256") != sha256(motion_path):
        errors.append("layout-boxes.json is stale for the current motion plan")

    motion_scenes = motion.get("scenes", []) if isinstance(motion.get("scenes"), list) else []
    layout_scenes = layout.get("scenes", []) if isinstance(layout.get("scenes"), list) else []
    motion_ids = [str(scene.get("id")) for scene in motion_scenes if isinstance(scene, dict)]
    layout_ids = [str(scene.get("id")) for scene in layout_scenes if isinstance(scene, dict)]
    if layout_ids != motion_ids:
        errors.append("layout scene IDs/order do not match the motion plan")
    motion_by_id = {str(scene.get("id")): scene for scene in motion_scenes if isinstance(scene, dict)}

    for scene in layout_scenes:
        if not isinstance(scene, dict):
            errors.append("layout scene must be an object")
            continue
        scene_id = str(scene.get("id", "<missing>"))
        planned = motion_by_id.get(scene_id, {})
        start, end = float(scene.get("start_s", 0)), float(scene.get("end_s", 0))
        if not math.isclose(start, float(planned.get("start_s", -1)), abs_tol=frame_tolerance):
            errors.append(f"{scene_id}: start_s does not match motion plan")
        if not math.isclose(end, float(planned.get("end_s", -1)), abs_tol=frame_tolerance):
            errors.append(f"{scene_id}: end_s does not match motion plan")
        elements = scene.get("elements", [])
        if not isinstance(elements, list):
            errors.append(f"{scene_id}: elements must be a list")
            continue
        roles = {str(item.get("role")) for item in elements if isinstance(item, dict)}
        required_roles = set(REQUIRED_ROLES)
        if avatar_enabled:
            required_roles.add("avatar")
        missing = required_roles - roles
        if missing:
            errors.append(f"{scene_id}: missing required roles {sorted(missing)}")
        ids = [str(item.get("id")) for item in elements if isinstance(item, dict)]
        if len(ids) != len(set(ids)):
            errors.append(f"{scene_id}: element IDs must be unique")

        planned_beats = [
            item for item in planned.get("beats", [])
            if isinstance(item, dict) and str(item.get("id", "")).strip()
        ]
        planned_beat_ids = {str(item.get("id")) for item in planned_beats}
        beat_elements = [
            item for item in elements
            if isinstance(item, dict) and str(item.get("semantic_beat_id", "")).strip()
        ]
        implemented_beat_ids = {str(item.get("semantic_beat_id")) for item in beat_elements}
        if implemented_beat_ids != planned_beat_ids:
            errors.append(
                f"{scene_id}: actual DOM beat boxes do not cover the motion plan: "
                f"missing={sorted(planned_beat_ids - implemented_beat_ids)}, "
                f"extra={sorted(implemented_beat_ids - planned_beat_ids)}"
            )
        beat_plan_by_id = {str(item.get("id")): item for item in planned_beats}
        for element in beat_elements:
            beat_id = str(element.get("semantic_beat_id"))
            if element.get("role") != "beat":
                errors.append(f"{scene_id}/{element.get('id')}: semantic beat element role must be beat")
            if element.get("animated") is not True or not isinstance(element.get("swept_bbox"), dict):
                errors.append(f"{scene_id}/{element.get('id')}: semantic beat requires animated=true and swept_bbox")
            cue = float(beat_plan_by_id.get(beat_id, {}).get("cue_s", -1))
            if not float(element.get("start_s", start)) <= cue <= float(element.get("end_s", end)):
                errors.append(f"{scene_id}/{element.get('id')}: active time does not contain its semantic cue")

        if planned.get("asset_refs"):
            required_visual_roles = {"illustration", "face", "hand", "action"}
            missing_visual = required_visual_roles - roles
            if missing_visual:
                errors.append(f"{scene_id}: missing actual illustration protection roles {sorted(missing_visual)}")

        caption = next((item for item in elements if isinstance(item, dict) and item.get("role") == "caption"), None)
        avatar = next((item for item in elements if isinstance(item, dict) and item.get("role") == "avatar"), None)
        if not caption or float(caption.get("x", 0)) < caption_x_min:
            errors.append(f"{scene_id}: caption region must start at x>={caption_x_min:g}")
        if avatar_enabled:
            expected_shape = str(avatar_policy.get("shape", "circle"))
            expected_size = float(avatar_policy.get("size", 0))
            expected_x = float(avatar_policy.get("x", 0))
            expected_y = float(
                avatar_policy.get(
                    "y",
                    height - float(avatar_policy.get("bottom", 0)) - expected_size,
                )
            )
            if not avatar or avatar.get("shape") != expected_shape:
                errors.append(f"{scene_id}: missing avatar safe zone with shape={expected_shape}")
            else:
                position_matches = (
                    math.isclose(float(avatar.get("x", -1)), expected_x, abs_tol=0.5)
                    and math.isclose(float(avatar.get("y", -1)), expected_y, abs_tol=0.5)
                )
                if expected_shape == "circle":
                    dimensions_match = math.isclose(
                        float(avatar.get("diameter", avatar.get("size", -1))),
                        expected_size,
                        abs_tol=0.5,
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

        for element in elements:
            if not isinstance(element, dict):
                errors.append(f"{scene_id}: element must be an object")
                continue
            element_id = str(element.get("id", "<missing>"))
            if args.require_approved and element.get("needs_dom_review") is True:
                errors.append(f"{scene_id}/{element_id}: template box still needs DOM review")
            if element.get("animated") is True and not isinstance(element.get("swept_bbox"), dict):
                errors.append(f"{scene_id}/{element_id}: animated element requires swept_bbox")
            if not within_canvas(element, width, height):
                errors.append(f"{scene_id}/{element_id}: geometry or swept_bbox leaves the canvas")
            active_start = float(element.get("start_s", start))
            active_end = float(element.get("end_s", end))
            if active_start < start - frame_tolerance or active_end > end + frame_tolerance or active_end <= active_start:
                errors.append(f"{scene_id}/{element_id}: active time leaves the scene")
            if element.get("role") == "transition" and caption:
                if int(element.get("z_index", 0)) >= int(caption.get("z_index", 0)):
                    errors.append(f"{scene_id}/{element_id}: transition must be below captions")

        composite_roles: dict[str, set[str]] = {}
        for element in elements:
            if not isinstance(element, dict):
                continue
            composite_id = str(element.get("intentional_composite_id") or "")
            if composite_id:
                composite_roles.setdefault(composite_id, set()).add(str(element.get("role")))
        allowed_composite_roles = {"illustration", "face", "hand", "action"}
        for composite_id, roles_in_group in composite_roles.items():
            if not roles_in_group <= allowed_composite_roles:
                errors.append(
                    f"{scene_id}: intentional composite {composite_id} contains forbidden roles "
                    f"{sorted(roles_in_group - allowed_composite_roles)}"
                )

        protected = [
            item for item in elements
            if isinstance(item, dict)
            and item.get("protected") is True
            and item.get("role") not in OVERLAP_EXEMPT_ROLES
        ]
        for index, left in enumerate(protected):
            for right in protected[index + 1 :]:
                if not time_overlaps(left, right) or not intersects(left, right):
                    continue
                left_composite = str(left.get("intentional_composite_id") or "")
                right_composite = str(right.get("intentional_composite_id") or "")
                if left_composite and left_composite == right_composite:
                    continue
                overlap_count += 1
                errors.append(
                    f"{scene_id}: protected elements overlap: "
                    f"{left.get('id')} ({left.get('role')}) and {right.get('id')} ({right.get('role')})"
                )

    report = {
        "schema_version": 1,
        "status": "pass" if not errors else "fail",
        "layout": {"path": str(layout_path), "sha256": sha256(layout_path), "approval_status": layout.get("status")},
        "motion_plan": {"path": str(motion_path), "sha256": sha256(motion_path)},
        "profile": {"path": str(profile_path), "id": profile.get("profile_id"), "sha256": profile_sha},
        "errors": errors,
        "warnings": warnings,
        "metrics": {"scenes": len(layout_scenes), "protected_overlaps": overlap_count},
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
