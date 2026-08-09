#!/usr/bin/env python3
"""Approve a draft plan/manifest automatically when it carries no risk markers.

The self-approval loop (agent writes draft -> agent flips status=approved) adds
steps without adding safety when the draft is clean. This gate flips
status=approved only when NO risk marker is present anywhere in the document:

  - "needs_dom_review": true
  - "low_confidence": true / "review_required": true
  - any *confidence* value below --min-confidence (default 0.6)

If any marker is found the script exits 1 and lists every location, so a human
review happens exactly where it is needed and nowhere else.

Kinds: prosody | motion | layout (layout additionally requires
actual_dom_verified=true, i.e. geometry came from the real DOM).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

LOGIC_VERSION = 1

BOOL_MARKERS = ("needs_dom_review", "low_confidence", "review_required")


def find_markers(node: Any, min_confidence: float, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}"
            if key in BOOL_MARKERS and value is True:
                found.append(f"{here} = true")
            elif "confidence" in key and isinstance(value, (int, float)) and value < min_confidence:
                found.append(f"{here} = {value} < {min_confidence}")
            found.extend(find_markers(value, min_confidence, here))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(find_markers(value, min_confidence, f"{path}[{index}]"))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--kind", choices=["prosody", "motion", "layout"], required=True)
    parser.add_argument("--min-confidence", type=float, default=0.6)
    parser.add_argument("--approved-by", default="auto-clean-gate")
    args = parser.parse_args()

    path = args.file.expanduser().resolve()
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        print("document root must be a JSON object", file=sys.stderr)
        return 1

    if doc.get("status") == "approved":
        print(f"already approved: {path}")
        return 0

    if args.kind == "layout" and doc.get("actual_dom_verified") is not True:
        print("layout not eligible: actual_dom_verified is not true "
              "(extract real DOM geometry first, e.g. via extract_layout_boxes.js)", file=sys.stderr)
        return 1

    markers = find_markers(doc, args.min_confidence)
    if markers:
        print(f"NOT auto-approved: {len(markers)} risk marker(s) need human review:", file=sys.stderr)
        for row in markers:
            print(f"  - {row}", file=sys.stderr)
        return 1

    doc["status"] = "approved"
    review = doc.get("review") if isinstance(doc.get("review"), dict) else {}
    review["approved_by"] = args.approved_by
    review["approved_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    review["auto_approved"] = True
    review["auto_approve_logic_version"] = LOGIC_VERSION
    review["min_confidence"] = args.min_confidence
    doc["review"] = review
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"approved: {path} (no risk markers, min_confidence={args.min_confidence})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
