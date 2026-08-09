#!/usr/bin/env python3
"""Run pipeline gates for a stage in the correct order with one command.

Stages:
  audio  -> prosody, audio boundaries, voice stability, scene pacing
  motion -> visual assets, semantic motion, layout boxes, AV alignment
  final  -> final video output QC
  all    -> audio + motion + final (final is skipped when no render exists)

Stops at the first failing gate unless --keep-going is passed, prints a JSON
summary, and exits non-zero on any failure. This replaces hand-typing each
validator command and forgetting one.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

LOGIC_VERSION = 1

SCRIPTS = Path(__file__).resolve().parent


def gate_commands(project: Path, stage: str, render_dir: Path) -> list[tuple[str, list[str], Path | None]]:
    """(name, argv, required_input) — gate is skipped when required_input is missing."""
    py = sys.executable
    audio = [
        (
            "prosody",
            [py, str(SCRIPTS / "validate_prosody.py"), "--prosody", str(project / "audio/prosody.json"), "--require-approved"],
            project / "audio/prosody.json",
        ),
        (
            "audio_boundaries",
            [py, str(SCRIPTS / "validate_audio_boundaries.py"), "--project", str(project)],
            project / "audio/output/narration_master.wav",
        ),
        (
            "voice_stability",
            [py, str(SCRIPTS / "validate_voice_stability.py"), "--project", str(project)],
            project / "audio/output/narration_master.wav",
        ),
        (
            "scene_pacing",
            [py, str(SCRIPTS / "validate_scene_pacing.py"), "--timeline", str(project / "audio/timeline.json")],
            project / "audio/timeline.json",
        ),
    ]
    motion = [
        (
            "visual_assets",
            [py, str(SCRIPTS / "validate_visual_assets.py"), "--project", str(project)],
            project / "visual-assets.json",
        ),
        (
            "semantic_motion",
            [
                py, str(SCRIPTS / "validate_semantic_motion.py"),
                "--plan", str(project / ".hyperframes/semantic-motion.json"),
                "--require-approved",
                "--report", str(project / ".hyperframes/motion-qc.json"),
            ],
            project / ".hyperframes/semantic-motion.json",
        ),
        (
            "layout_boxes",
            [
                py, str(SCRIPTS / "validate_layout_boxes.py"),
                "--layout", str(project / ".hyperframes/layout-boxes.json"),
                "--motion-plan", str(project / ".hyperframes/semantic-motion.json"),
                "--require-approved",
                "--report", str(project / ".hyperframes/layout-qc.json"),
            ],
            project / ".hyperframes/layout-boxes.json",
        ),
        (
            "av_alignment",
            [py, str(SCRIPTS / "validate_av_alignment.py"), "--project", str(project)],
            project / ".hyperframes/semantic-motion.json",
        ),
    ]
    final = [
        (
            "video_output",
            [py, str(SCRIPTS / "validate_video_output.py"), "--dir", str(render_dir)],
            render_dir / "final.mp4",
        ),
    ]
    if stage == "audio":
        return audio
    if stage == "motion":
        return motion
    if stage == "final":
        return final
    return audio + motion + final


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--stage", choices=["audio", "motion", "final", "all"], default="all")
    parser.add_argument("--render-dir", type=Path, default=None, help="defaults to <project>/renders")
    parser.add_argument("--keep-going", action="store_true", help="run remaining gates after a failure")
    parser.add_argument("--report", type=Path, default=None, help="defaults to <project>/.pipeline/gates-report.json")
    args = parser.parse_args()

    project = args.project.expanduser().resolve()
    render_dir = (args.render_dir or project / "renders").expanduser().resolve()
    results = []
    failed = False

    for name, argv, required in gate_commands(project, args.stage, render_dir):
        if required is not None and not required.is_file():
            results.append({"gate": name, "status": "skipped", "reason": f"missing input: {required}"})
            print(f"[skip] {name}: missing {required}")
            continue
        started = time.monotonic()
        proc = subprocess.run(argv, capture_output=True, text=True)
        elapsed = round(time.monotonic() - started, 2)
        ok = proc.returncode == 0
        results.append(
            {
                "gate": name,
                "status": "pass" if ok else "fail",
                "elapsed_s": elapsed,
                "returncode": proc.returncode,
                "stdout_tail": proc.stdout[-2000:],
                "stderr_tail": proc.stderr[-2000:],
            }
        )
        print(f"[{'pass' if ok else 'FAIL'}] {name} ({elapsed}s)")
        if not ok:
            failed = True
            if not args.keep_going:
                break

    ran_any = any(row.get("status") in ("pass", "fail") for row in results)
    summary = {
        "logic_version": LOGIC_VERSION,
        "project": str(project),
        "stage": args.stage,
        "status": "fail" if failed else ("pass" if ran_any else "all_skipped"),
        "gates": results,
    }
    report_path = args.report or project / ".pipeline/gates-report.json"
    report_path = report_path.expanduser()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("stage", "status")}, ensure_ascii=False))
    if failed:
        for row in results:
            if row.get("status") == "fail":
                sys.stderr.write(f"\n--- {row['gate']} output ---\n{row['stdout_tail']}\n{row['stderr_tail']}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
