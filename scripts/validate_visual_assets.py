#!/usr/bin/env python3
"""Validate the mandatory xiaomu illustration stage for a video project."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument(
        "--allow-legacy",
        action="store_true",
        help="Allow an older project created before the mandatory stage was enabled.",
    )
    args = parser.parse_args()

    project = args.project.expanduser().resolve()
    manifest_path = project / "visual-assets.json"
    if not manifest_path.is_file():
        raise SystemExit(f"missing visual asset decision manifest: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid visual asset decision manifest: {exc}") from exc

    if manifest.get("provider") != "ian-xiaomu-illustrations":
        raise SystemExit("visual-assets.json must name provider=ian-xiaomu-illustrations")
    if manifest.get("invocation_required") is not True:
        raise SystemExit("visual-assets.json.invocation_required must be true")
    if not isinstance(manifest.get("skill_invoked"), bool):
        raise SystemExit("visual-assets.json.skill_invoked must be boolean")
    invoked = manifest["skill_invoked"]
    assets = manifest.get("assets", [])
    if not isinstance(assets, list):
        raise SystemExit("visual-assets.json.assets must be a list")
    shot_list = manifest.get("shot_list", [])
    if not isinstance(shot_list, list):
        raise SystemExit("visual-assets.json.shot_list must be a list")
    if not invoked:
        if args.allow_legacy and manifest.get("status") == "legacy_pre_mandatory_rule":
            print(json.dumps({
                "status": "legacy",
                "provider": manifest["provider"],
                "skill_invoked": False,
                "message": "rerun this project through the mandatory xiaomu stage",
            }, ensure_ascii=False))
            return 0
        raise SystemExit("ian-xiaomu-illustrations is mandatory: skill_invoked must be true")
    if not 4 <= len(shot_list) <= 8:
        raise SystemExit("skill_invoked=true requires 4-8 shot_list entries")
    if not 4 <= len(assets) <= 8:
        raise SystemExit("skill_invoked=true requires 4-8 corresponding visual assets")
    if manifest.get("status") not in {"complete", "reused"}:
        raise SystemExit("skill_invoked=true requires status=complete or status=reused")
    for index, asset in enumerate(assets, start=1):
        if not isinstance(asset, dict):
            raise SystemExit(f"visual asset {index} must be an object")
        relative_path = str(asset.get("path", "")).strip()
        if not relative_path:
            raise SystemExit(f"visual asset {index} is missing path")
        asset_path = project / relative_path
        if not asset_path.is_file():
            raise SystemExit(f"visual asset {index} does not exist: {asset_path}")
        if not str(asset.get("sha256", "")).strip():
            raise SystemExit(f"visual asset {index} is missing sha256")

    print(json.dumps({
        "status": "pass",
        "provider": manifest["provider"],
        "skill_invoked": invoked,
        "shot_list": len(shot_list),
        "assets": len(assets),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
