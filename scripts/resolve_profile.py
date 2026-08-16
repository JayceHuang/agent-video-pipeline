#!/usr/bin/env python3
"""Merge generic defaults with external workspace, runtime, and project config."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from profile_config import (
    CONFIG_CONTRACT_VERSION,
    DEFAULT_PROFILE,
    atomic_json,
    canonical_sha256,
    deep_merge,
    discover_config_root,
    get_in,
    load_mapping,
    require_config_path,
    sha256_file,
    validate_profile,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--config-root", type=Path)
    parser.add_argument("--profile", type=Path, action="append", default=[])
    parser.add_argument("--profile-id")
    parser.add_argument("--runtime", type=Path, help="must resolve to <config-root>/runtime.local.yaml")
    parser.add_argument("--project-config", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.output is None and args.project is None:
        parser.error("pass --output or --project")
    if args.project is None:
        parser.error("--project is required by the centralized config contract")
    if args.profile and args.profile_id:
        parser.error("use --profile or --profile-id, not both")
    if args.base.expanduser().resolve() != DEFAULT_PROFILE.resolve():
        parser.error("--base cannot override the Skill's neutral default profile")

    project = args.project.expanduser().resolve()
    config_root = discover_config_root(project, args.config_root)
    runtime_path = (args.runtime or config_root / "runtime.local.yaml").expanduser().resolve()
    if runtime_path != config_root / "runtime.local.yaml":
        raise ValueError(f"runtime config must be {config_root / 'runtime.local.yaml'}")
    if not runtime_path.is_file():
        raise FileNotFoundError(runtime_path)

    profile_paths: list[Path]
    if args.profile:
        profile_paths = [
            require_config_path(item, config_root, "profiles", "workspace profile")
            for item in args.profile
        ]
    elif args.profile_id:
        profile_paths = [
            require_config_path(
                config_root / "profiles" / f"{args.profile_id}.yaml",
                config_root,
                "profiles",
                "workspace profile",
            )
        ]
    else:
        available = sorted((config_root / "profiles").glob("*.yaml"))
        if len(available) != 1:
            raise ValueError(
                "centralized config requires exactly one discoverable workspace profile when "
                f"--profile/--profile-id is omitted; found {len(available)} in {config_root / 'profiles'}"
            )
        profile_paths = available

    for profile_path in profile_paths:
        if profile_path.suffix.lower() != ".yaml":
            raise ValueError(f"workspace profile must use the .yaml extension: {profile_path}")

    project_config_path = None
    if args.project_config is not None:
        project_config_path = require_config_path(
            args.project_config, config_root, "projects", "project config"
        )
    else:
        inferred = config_root / "projects" / f"{project.name}.yaml"
        if inferred.is_file():
            project_config_path = inferred

    if project_config_path is not None and project_config_path.suffix.lower() != ".yaml":
        raise ValueError(f"project config must use the .yaml extension: {project_config_path}")

    required_output = project / ".pipeline/resolved-profile.json"
    output = (args.output or required_output).expanduser().resolve()
    if output != required_output.resolve():
        raise ValueError(f"resolved profile output must be {required_output}")

    ordered: list[tuple[str, Path]] = [
        ("base", args.base),
        *[("profile", item) for item in profile_paths],
        ("runtime", runtime_path),
    ]
    if project_config_path is not None:
        ordered.append(("project", project_config_path))

    merged: dict = {}
    sources = []
    for role, raw_path in ordered:
        path = raw_path.expanduser().absolute()
        if not path.is_file():
            raise FileNotFoundError(path)
        merged = deep_merge(merged, load_mapping(path))
        sources.append({"role": role, "path": str(path), "sha256": sha256_file(path)})

    for workspace_profile_path in profile_paths:
        workspace_profile = load_mapping(workspace_profile_path)
        if not str(workspace_profile.get("profile_id", "")).strip():
            raise ValueError(f"workspace profile must declare profile_id: {workspace_profile_path}")

    runtime = load_mapping(runtime_path)
    runtime_python = get_in(runtime, "pipeline_runtime.python")
    if not isinstance(runtime_python, str) or not runtime_python.strip():
        raise ValueError(
            f"runtime.local.yaml must declare a non-empty pipeline_runtime.python: {runtime_path}"
        )
    runtime_python_path = Path(runtime_python).expanduser()
    if not runtime_python_path.is_absolute() or not runtime_python_path.is_file():
        raise ValueError(
            "pipeline_runtime.python must be an existing absolute interpreter path: "
            f"{runtime_python}"
        )

    errors = validate_profile(merged)
    if errors:
        raise ValueError("invalid resolved profile: " + "; ".join(errors))

    profile_sha = canonical_sha256(merged)
    merged["_meta"] = {
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "profile_sha256": profile_sha,
        "config_contract_version": CONFIG_CONTRACT_VERSION,
        "config_root": str(config_root),
        "sources": sources,
    }
    atomic_json(output, merged)
    output.with_suffix(".sha256").write_text(profile_sha + "\n", encoding="utf-8")
    print(f"resolved profile: {merged['profile_id']} -> {output}")
    print(f"sha256: {profile_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
