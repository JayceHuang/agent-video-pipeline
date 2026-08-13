#!/usr/bin/env python3
"""Validate a source or resolved Agent video profile."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from profile_config import (
    canonical_sha256,
    load_mapping,
    validate_profile,
    validate_resolved_profile,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args()
    profile = load_mapping(args.profile)
    errors = validate_profile(profile)
    if "_meta" in profile:
        errors.extend(validate_resolved_profile(profile, args.profile.expanduser().absolute()))
    result = {
        "status": "fail" if errors else "pass",
        "profile_id": profile.get("profile_id"),
        "sha256": canonical_sha256({k: v for k, v in profile.items() if k != "_meta"}),
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
