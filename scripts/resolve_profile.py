#!/usr/bin/env python3
"""Merge generic defaults with external personal, runtime, and project config."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from profile_config import (
    DEFAULT_PROFILE,
    atomic_json,
    canonical_sha256,
    deep_merge,
    load_mapping,
    sha256_file,
    validate_profile,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--profile", type=Path, action="append", default=[])
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--project-config", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.output is None and args.project is None:
        parser.error("pass --output or --project")
    output = args.output or args.project.expanduser().absolute() / ".pipeline/resolved-profile.json"
    output = output.expanduser().absolute()

    ordered = [args.base, *args.profile]
    if args.runtime is not None:
        ordered.append(args.runtime)
    if args.project_config is not None:
        ordered.append(args.project_config)

    merged: dict = {}
    sources = []
    for raw_path in ordered:
        path = raw_path.expanduser().absolute()
        if not path.is_file():
            raise FileNotFoundError(path)
        merged = deep_merge(merged, load_mapping(path))
        sources.append({"path": str(path), "sha256": sha256_file(path)})

    errors = validate_profile(merged)
    if errors:
        raise ValueError("invalid resolved profile: " + "; ".join(errors))

    profile_sha = canonical_sha256(merged)
    merged["_meta"] = {
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "profile_sha256": profile_sha,
        "sources": sources,
    }
    atomic_json(output, merged)
    output.with_suffix(".sha256").write_text(profile_sha + "\n", encoding="utf-8")
    print(f"resolved profile: {merged['profile_id']} -> {output}")
    print(f"sha256: {profile_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
