#!/usr/bin/env python3
"""Validate that every generated episode is self-contained and follows teaser policy."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from profile_config import get_in, load_mapping


TEASER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("上一集", re.compile(r"上一\s*集")),
    ("上集", re.compile(r"上\s*集")),
    ("上一期", re.compile(r"上一\s*期")),
    ("上期", re.compile(r"上\s*期")),
    ("上一章", re.compile(r"上一\s*章")),
    ("上章", re.compile(r"上\s*章")),
    ("前一集", re.compile(r"前一\s*集")),
    ("前一章", re.compile(r"前一\s*章")),
    ("下一集", re.compile(r"下一\s*集")),
    ("下集", re.compile(r"下\s*集")),
    ("下一期", re.compile(r"下一\s*期")),
    ("下期", re.compile(r"下\s*期")),
    ("下回", re.compile(r"下\s*回")),
    ("下一章", re.compile(r"下一\s*章")),
    ("下章", re.compile(r"下\s*章")),
    ("下一部分", re.compile(r"下一\s*部分")),
    ("下一个视频", re.compile(r"下一个视频")),
    ("下条视频", re.compile(r"下条视频")),
    ("敬请期待", re.compile(r"敬请期待")),
    ("下次再讲", re.compile(r"下次(?:再)?讲")),
    ("下次继续", re.compile(r"下次继续")),
    ("后续再讲", re.compile(r"后续(?:再)?讲")),
    ("后面再讲", re.compile(r"后面(?:再)?讲")),
)
def iter_strings(value: Any, location: str = "episode"):
    if isinstance(value, str):
        yield location, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from iter_strings(item, f"{location}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from iter_strings(item, f"{location}.{key}")


def validate_episode(
    episode: dict[str, Any],
    ending_cta: str,
    index: int,
    *,
    require_summary: bool = True,
    allow_cross_episode_teaser: bool = False,
) -> list[str]:
    errors: list[str] = []
    episode_id = episode.get("episode", index)
    if not str(episode.get("title", "")).strip():
        errors.append(f"episode {episode_id}: missing independent title")
    if require_summary and not str(episode.get("summary", "")).strip():
        errors.append(f"episode {episode_id}: missing independent summary")
    scenes = episode.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        errors.append(f"episode {episode_id}: scenes must be a non-empty list")
        return errors

    if not allow_cross_episode_teaser:
        for location, text in iter_strings(episode, f"episode {episode_id}"):
            for label, pattern in TEASER_PATTERNS:
                match = pattern.search(text)
                if match:
                    errors.append(f"{location}: cross-episode teaser '{label}' ({match.group(0)})")

    final_text = str(scenes[-1].get("text", "")).strip() if isinstance(scenes[-1], dict) else ""
    if not final_text:
        errors.append(f"episode {episode_id}: final scene has no narration")
    elif ending_cta and not final_text.endswith(ending_cta):
        errors.append(f"episode {episode_id}: final scene must end with CTA '{ending_cta}'")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", type=Path, required=True, help="series.json or a generated scenes.json")
    parser.add_argument("--ending-cta", default=None, help="override the CTA expected at the end of the final scene")
    parser.add_argument("--profile", type=Path, help="resolved profile JSON")
    args = parser.parse_args()

    try:
        payload = json.loads(args.series.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read {args.series}: {exc}", file=sys.stderr)
        return 2

    profile = load_mapping(args.profile) if args.profile else {}
    configured_cta = get_in(profile, "episode.final_cta", "")
    allow_cross_episode_teaser = bool(
        get_in(profile, "episode.allow_cross_episode_teaser", False)
    )
    ending_cta = str(
        args.ending_cta
        if args.ending_cta is not None
        else payload.get("ending_cta", configured_cta or "")
    ).strip()
    is_series = "episodes" in payload
    if is_series:
        episodes = payload.get("episodes")
    else:
        episodes = [payload]
    if not isinstance(episodes, list) or not episodes:
        print("ERROR: input must contain a non-empty 'episodes' list", file=sys.stderr)
        return 2

    errors: list[str] = []
    for index, episode in enumerate(episodes, start=1):
        if not isinstance(episode, dict):
            errors.append(f"episode {index}: entry must be an object")
            continue
        errors.extend(
            validate_episode(
                episode,
                ending_cta,
                index,
                require_summary=is_series,
                allow_cross_episode_teaser=allow_cross_episode_teaser,
            )
        )

    if errors:
        print("Episode independence validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    ending_policy = "configured CTA present" if ending_cta else "no mandatory CTA"
    teaser_policy = (
        "cross-episode teaser explicitly allowed"
        if allow_cross_episode_teaser
        else "teaser-free"
    )
    print(
        f"Episode independence validation passed: {len(episodes)} episode(s), "
        f"{teaser_policy}, {ending_policy}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
