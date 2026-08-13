#!/usr/bin/env python3
"""Initialize a profile-bound visual-assets decision without invoking a provider."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from profile_config import atomic_json, get_in, load_resolved_profile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True, help="resolved profile JSON")
    parser.add_argument("--force", action="store_true", help="replace an existing decision manifest")
    args = parser.parse_args()

    project = args.project.expanduser().absolute()
    profile, profile_path = load_resolved_profile(args.profile, project, required=True)
    policy = get_in(profile, "layout.illustration_skill", {})
    policy = policy if isinstance(policy, dict) else {}
    enabled = bool(policy.get("enabled"))
    required = bool(policy.get("required"))
    target = project / "visual-assets.json"
    if target.exists() and not args.force:
        raise FileExistsError(f"visual asset decision already exists; review it or pass --force: {target}")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": "planned" if enabled else "disabled",
        "provider": policy.get("provider") or policy.get("name"),
        "invocation_required": required,
        "skill_invoked": False,
        "shot_list": [],
        "assets": [],
        "profile": {
            "path": str(profile_path),
            "id": profile.get("profile_id"),
            "sha256": get_in(profile, "_meta.profile_sha256"),
        },
        "profile_sha256": get_in(profile, "_meta.profile_sha256"),
    }
    atomic_json(target, payload)
    print(json.dumps({
        "status": payload["status"],
        "manifest": str(target),
        "provider": payload["provider"],
        "invocation_required": required,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
