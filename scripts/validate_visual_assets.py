#!/usr/bin/env python3
"""Validate profile-driven visual-asset decisions for a video project."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from profile_config import get_in, load_resolved_profile


def in_range(count: int, bounds: object) -> bool:
    return (
        isinstance(bounds, list)
        and len(bounds) == 2
        and int(bounds[0]) <= count <= int(bounds[1])
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--profile", type=Path, help="resolved profile JSON")
    parser.add_argument("--allow-legacy", action="store_true")
    args = parser.parse_args()

    project = args.project.expanduser().absolute()
    profile, profile_path = load_resolved_profile(args.profile, project, required=True)
    policy = get_in(profile, "layout.illustration_skill", {})
    policy = policy if isinstance(policy, dict) else {}
    enabled = bool(policy.get("enabled"))
    required = bool(policy.get("required"))
    allow_skip = bool(policy.get("allow_episode_skip"))
    expected_provider = str(policy.get("provider") or policy.get("name") or "").strip()
    shot_range = policy.get("shot_count_range", [0, 999])
    asset_range = policy.get("asset_count_range", [0, 999])

    manifest_path = project / "visual-assets.json"
    if not manifest_path.is_file():
        raise SystemExit(
            f"missing visual asset decision manifest: {manifest_path}; "
            "run init_visual_assets.py before this gate"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid visual asset decision manifest: {exc}") from exc

    provider = str(manifest.get("provider") or "").strip()
    if expected_provider and provider != expected_provider:
        raise SystemExit(
            f"visual-assets.json provider must match profile: {expected_provider!r}, got {provider!r}"
        )
    if not isinstance(manifest.get("skill_invoked"), bool):
        raise SystemExit("visual-assets.json.skill_invoked must be boolean")
    invoked = manifest["skill_invoked"]
    assets = manifest.get("assets", [])
    shot_list = manifest.get("shot_list", [])
    if not isinstance(assets, list) or not isinstance(shot_list, list):
        raise SystemExit("visual-assets.json assets and shot_list must be lists")
    if required and manifest.get("invocation_required") is not True:
        raise SystemExit("profile requires visual provider invocation")
    manifest_profile_sha = manifest.get("profile_sha256")
    if not manifest_profile_sha and isinstance(manifest.get("profile"), dict):
        manifest_profile_sha = manifest["profile"].get("sha256")
    expected_profile_sha = get_in(profile, "_meta.profile_sha256")
    if manifest_profile_sha != expected_profile_sha:
        raise SystemExit("visual-assets.json is stale for the resolved profile")
    if not enabled:
        if manifest.get("status") != "disabled":
            raise SystemExit("disabled visual policy requires status=disabled")
        if invoked or assets or shot_list:
            raise SystemExit("disabled visual policy cannot invoke a provider or retain shots/assets")
        if manifest.get("invocation_required") is not False:
            raise SystemExit("disabled visual policy requires invocation_required=false")
        print(json.dumps({
            "status": "pass",
            "decision": "visual assets disabled by profile",
            "profile_id": profile.get("profile_id"),
        }, ensure_ascii=False))
        return 0
    if required and not invoked:
        if args.allow_legacy and manifest.get("status") == "legacy_pre_mandatory_rule":
            print(json.dumps({"status": "legacy", "provider": provider}, ensure_ascii=False))
            return 0
        raise SystemExit("required visual provider was not invoked")
    if enabled and not invoked and not required:
        if not allow_skip:
            raise SystemExit("visual provider is enabled and this profile does not allow episode skip")
        if manifest.get("status") != "skipped":
            raise SystemExit("non-invoked optional visual provider requires status=skipped")
    if invoked:
        if not in_range(len(shot_list), shot_range):
            raise SystemExit(f"shot_list count must be within profile range {shot_range}")
        if not in_range(len(assets), asset_range):
            raise SystemExit(f"asset count must be within profile range {asset_range}")
        if manifest.get("status") not in {"complete", "reused"}:
            raise SystemExit("invoked visual provider requires status=complete or status=reused")
    elif assets or shot_list:
        raise SystemExit("non-invoked visual provider cannot claim shots or assets")

    for index, asset in enumerate(assets, start=1):
        if not isinstance(asset, dict):
            raise SystemExit(f"visual asset {index} must be an object")
        relative_path = str(asset.get("path", "")).strip()
        if not relative_path:
            raise SystemExit(f"visual asset {index} is missing path")
        asset_path = (project / relative_path).expanduser().absolute()
        try:
            asset_path.relative_to(project)
        except ValueError as exc:
            raise SystemExit(f"visual asset {index} must remain inside the project: {asset_path}") from exc
        if not asset_path.is_file():
            raise SystemExit(f"visual asset {index} does not exist: {asset_path}")
        expected_sha = str(asset.get("sha256", "")).strip()
        if not expected_sha:
            raise SystemExit(f"visual asset {index} is missing sha256")
        if sha256(asset_path) != expected_sha:
            raise SystemExit(f"visual asset {index} hash is stale: {asset_path}")

    print(json.dumps({
        "status": "pass",
        "provider": provider or None,
        "skill_invoked": invoked,
        "shot_list": len(shot_list),
        "assets": len(assets),
        "profile_id": profile.get("profile_id"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
