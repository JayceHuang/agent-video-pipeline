#!/usr/bin/env python3
"""Validate the sentence-level prosody contract before TTS generation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


EMOTIONS = {"calm", "curious", "warning", "excited", "warm"}
PITCHES = {"stable", "slightly-up", "slightly-down"}
STRESSES = {"light", "strong"}
TYPES = {
    "statement",
    "question",
    "warning",
    "definition",
    "instruction",
    "contrast",
    "conclusion",
    "cta",
    "excited",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prosody", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-approved", action="store_true")
    args = parser.parse_args()

    data = json.loads(args.prosody.expanduser().resolve().read_text(encoding="utf-8"))
    errors: list[str] = []
    warnings: list[str] = []
    if data.get("schema_version") != 2:
        errors.append("schema_version must be 2")
    if args.require_approved and data.get("status") != "approved":
        errors.append("status must be approved before TTS generation")
    rules = data.get("rules", {})
    if rules.get("acoustic_baseline_locked") is not True:
        errors.append("rules.acoustic_baseline_locked must be true")
    if rules.get("semantic_micro_prosody") is not True:
        errors.append("rules.semantic_micro_prosody must be true")
    if rules.get("scene_state_switching") is not False:
        errors.append("rules.scene_state_switching must be false")
    if rules.get("control_tags_in_tts_text") is not False:
        errors.append("rules.control_tags_in_tts_text must be false")
    baseline = data.get("acoustic_baseline", {})
    for field in (
        "register",
        "vocal_effort",
        "breath_pressure",
        "microphone_distance",
        "timbre_brightness",
        "global_energy",
    ):
        if not str(baseline.get(field, "")).strip():
            errors.append(f"acoustic_baseline.{field} is required")

    scenes = data.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        errors.append("scenes must be a non-empty list")
        scenes = []

    previous_energy: int | None = None
    scene_reports: list[dict[str, object]] = []
    for scene in scenes:
        scene_id = str(scene.get("id", "<missing>"))
        segments = scene.get("segments")
        if not isinstance(segments, list) or not segments:
            errors.append(f"{scene_id}: segments must be a non-empty list")
            continue
        for segment in segments:
            segment_id = str(segment.get("id", "<missing>"))
            prefix = f"{scene_id}/{segment_id}"
            if not str(segment.get("text", "")).strip():
                errors.append(f"{prefix}: text is empty")
            if segment.get("sentence_type") not in TYPES:
                errors.append(f"{prefix}: invalid sentence_type")
            if segment.get("emotion") not in EMOTIONS:
                errors.append(f"{prefix}: invalid emotion")
            if segment.get("pitch") not in PITCHES:
                errors.append(f"{prefix}: invalid pitch")
            if segment.get("stress") not in STRESSES:
                errors.append(f"{prefix}: invalid stress")
            try:
                pause = float(segment.get("pause_after_s"))
                rate = float(segment.get("rate"))
                strength = int(segment.get("emotion_strength"))
            except (TypeError, ValueError):
                errors.append(f"{prefix}: pause/rate/emotion_strength must be numeric")
                continue
            if not 0.05 <= pause <= 0.5:
                errors.append(f"{prefix}: pause_after_s outside 0.05–0.50")
            if not 0.98 <= rate <= 1.02:
                errors.append(f"{prefix}: rate outside semantic micro range 0.98–1.02")
            if not 1 <= strength <= 2:
                errors.append(f"{prefix}: emotion_strength outside bounded range 1–2")
            if segment.get("stress") == "strong" and segment.get("intentional_emphasis") is not True:
                errors.append(f"{prefix}: strong stress requires intentional_emphasis=true")
            if previous_energy is not None and abs(strength - previous_energy) > 1:
                errors.append(f"{prefix}: semantic energy changes by more than 1")
            previous_energy = strength
        scene_reports.append({"id": scene_id, "segments": len(segments)})

    report = {
        "status": "pass" if not errors else "fail",
        "prosody_status": data.get("status"),
        "require_approved": args.require_approved,
        "scenes": scene_reports,
        "warnings": warnings,
        "errors": errors,
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        target = args.output.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
