#!/usr/bin/env python3
"""Create a draft layout-box manifest from an approved semantic-motion plan."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from profile_config import get_in, load_resolved_profile


Z_INDEX = {"background": 0, "content": 10, "illustration": 20, "title": 30, "transition": 40, "caption": 90, "avatar": 100}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def element_from_box(value: dict[str, Any], start: float, end: float) -> dict[str, Any]:
    role = str(value.get("role", "content"))
    item = {
        **value,
        "start_s": start,
        "end_s": end,
        "protected": role not in {"background", "transition"},
        "animated": False,
        "z_index": Z_INDEX.get(role, 10),
        "intentional_composite_id": None,
        "needs_dom_review": True,
    }
    return item


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--motion-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True, help="resolved profile JSON")
    args = parser.parse_args()

    motion_path = args.motion_plan.expanduser().resolve()
    motion = json.loads(motion_path.read_text(encoding="utf-8"))
    if motion.get("status") != "approved":
        raise SystemExit("motion plan must be approved before layout initialization")
    scenes: list[dict[str, Any]] = []
    for scene in motion.get("scenes", []):
        start, end = float(scene["start_s"]), float(scene["end_s"])
        boxes = scene.get("safe_boxes", [])
        scenes.append({
            "id": scene["id"],
            "start_s": start,
            "end_s": end,
            "elements": [element_from_box(box, start, end) for box in boxes],
        })
    profile, profile_path = load_resolved_profile(args.profile, None, required=True)
    canvas = get_in(profile, "layout.canvas", {})
    output = {
        "schema_version": 1,
        "status": "draft",
        "canvas": {"width": int(canvas["width"]), "height": int(canvas["height"])},
        "profile": {
            "id": profile.get("profile_id"),
            "path": str(profile_path),
            "sha256": get_in(profile, "_meta.profile_sha256"),
        },
        "motion_plan": {"path": str(motion_path), "sha256": sha256(motion_path)},
        "actual_dom_verified": False,
        "scenes": scenes,
        "review": {
            "required": True,
            "approved_by": None,
            "instructions": "Replace template boxes with actual DOM geometry, add swept_bbox for animated elements, clear needs_dom_review, then approve.",
        },
    }
    target = args.output.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(target), "status": "draft", "scenes": len(scenes)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
