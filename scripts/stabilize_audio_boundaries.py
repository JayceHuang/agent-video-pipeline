#!/usr/bin/env python3
"""Reassemble scene WAVs with short pauses, soft edges, and a hard QC gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_MASTER_FILTER = (
    "highpass=f=82,"
    "equalizer=f=146.5:t=q:w=7:g=-4.5,"
    "equalizer=f=205:t=q:w=6:g=-4,"
    "equalizer=f=293:t=q:w=5:g=-2.5,"
    "equalizer=f=2500:t=q:w=1:g=1.2,"
    "lowpass=f=14500,"
    "acompressor=threshold=-18dB:ratio=1.2:attack=25:release=220:makeup=1:mix=.55,"
    "loudnorm=I=-16:TP=-2:LRA=6:linear=true"
)


def resolve_path(project: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (project / path).resolve()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def media_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.part")
    partial.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial.replace(path)


def shifted_caption_doc(document: dict[str, Any], deltas: dict[str, float]) -> dict[str, Any]:
    cloned = json.loads(json.dumps(document, ensure_ascii=False))
    collections: list[list[dict[str, Any]]] = []
    if isinstance(cloned.get("groups"), list):
        collections.append(cloned["groups"])
    if isinstance(cloned.get("words"), list):
        collections.append(cloned["words"])
    for collection in collections:
        for item in collection:
            delta = deltas.get(str(item.get("scene")), 0.0)
            if "start" in item:
                item["start"] = round(float(item["start"]) + delta, 6)
            if "end" in item:
                item["end"] = round(float(item["end"]) + delta, 6)
            for word in item.get("words", []):
                word_delta = deltas.get(str(word.get("scene", item.get("scene"))), delta)
                word["start"] = round(float(word["start"]) + word_delta, 6)
                word["end"] = round(float(word["end"]) + word_delta, 6)
    return cloned


def backup_files(project: Path, backup_dir: Path, paths: list[Path]) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    for source in paths:
        if not source.is_file():
            continue
        try:
            relative = source.relative_to(project)
        except ValueError:
            relative = Path(source.name)
        destination = backup_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--timeline", default="audio/timeline.json")
    parser.add_argument("--captions", default="audio/caption-groups.json")
    parser.add_argument("--words", default="audio/caption-words.json")
    parser.add_argument("--manifest", default="audio/voice-manifest.json")
    parser.add_argument("--master", default=None)
    parser.add_argument("--gap", type=float, default=0.18)
    parser.add_argument("--fade", type=float, default=0.012)
    parser.add_argument("--master-filter", default=DEFAULT_MASTER_FILTER)
    parser.add_argument("--backup-dir", type=Path, default=None)
    args = parser.parse_args()

    if not 0.12 <= args.gap <= 0.20:
        raise ValueError("--gap must stay inside the normal 0.12-0.20 second boundary band")
    if not 0.0 <= args.fade <= 0.05:
        raise ValueError("--fade must stay inside 0-0.05 seconds")

    project = args.project.expanduser().resolve()
    timeline_path = resolve_path(project, args.timeline)
    captions_path = resolve_path(project, args.captions)
    words_path = resolve_path(project, args.words)
    manifest_path = resolve_path(project, args.manifest)
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    master_path = resolve_path(project, args.master or timeline["audio"])
    sample_rate = int(timeline.get("sample_rate", 48000))
    scenes = timeline.get("scenes", [])
    if len(scenes) < 1:
        raise ValueError("timeline has no scenes")

    scene_paths: list[Path] = []
    scene_durations: list[float] = []
    old_starts: dict[str, float] = {}
    for scene in scenes:
        path = resolve_path(project, scene["path"])
        if not path.is_file():
            raise FileNotFoundError(path)
        duration = media_duration(path)
        if duration <= args.fade * 2:
            raise ValueError(f"scene too short for edge fade: {scene['id']} ({duration:.4f}s)")
        scene_paths.append(path)
        scene_durations.append(duration)
        old_starts[str(scene["id"])] = float(scene["start_s"])

    new_timeline = json.loads(json.dumps(timeline, ensure_ascii=False))
    cursor = 0.0
    for index, scene in enumerate(new_timeline["scenes"]):
        duration = scene_durations[index]
        start = cursor
        end = start + duration
        scene["start_s"] = round(start, 6)
        scene["end_s"] = round(end, 6)
        scene["duration_s"] = round(duration, 6)
        scene["spoken_duration_s"] = round(duration, 6)
        if index < len(new_timeline["scenes"]) - 1:
            intentional = scene.get("intentional_audio_pause_s")
            boundary_gap = float(intentional) if intentional is not None else args.gap
            scene["gap_after_s"] = round(boundary_gap, 6)
            cursor = end + boundary_gap
        else:
            cursor = end
    new_timeline["total_duration_s"] = round(cursor, 6)
    new_timeline["boundary_stability"] = {
        "version": 1,
        "status": "pending",
        "default_gap_s": args.gap,
        "allowed_gap_range_s": [0.12, 0.20],
        "edge_fade_s": args.fade,
        "master_filter": args.master_filter,
        "qc_report": "audio/boundary-qc.json",
    }
    deltas = {
        str(scene["id"]): float(scene["start_s"]) - old_starts[str(scene["id"])]
        for scene in new_timeline["scenes"]
    }

    captions = (
        shifted_caption_doc(json.loads(captions_path.read_text(encoding="utf-8")), deltas)
        if captions_path.is_file()
        else None
    )
    words = (
        shifted_caption_doc(json.loads(words_path.read_text(encoding="utf-8")), deltas)
        if words_path.is_file()
        else None
    )

    with tempfile.TemporaryDirectory(prefix="boundary-stage-", dir=str(master_path.parent)) as temp_name:
        stage_dir = Path(temp_name)
        stage_master = stage_dir / "narration_master.wav"
        filter_parts: list[str] = []
        concat_labels: list[str] = []
        for index, duration in enumerate(scene_durations):
            fade_out_start = max(0.0, duration - args.fade)
            filter_parts.append(
                f"[{index}:a]aresample={sample_rate},"
                "aformat=sample_fmts=fltp:channel_layouts=mono,"
                "asetpts=PTS-STARTPTS,"
                f"afade=t=in:st=0:d={args.fade:.6f},"
                f"afade=t=out:st={fade_out_start:.6f}:d={args.fade:.6f}[scene{index}]"
            )
            concat_labels.append(f"[scene{index}]")
            if index < len(scene_durations) - 1:
                gap = float(new_timeline["scenes"][index]["gap_after_s"])
                filter_parts.append(
                    f"anullsrc=r={sample_rate}:cl=mono,"
                    f"atrim=start=0:end={gap:.6f},asetpts=PTS-STARTPTS[gap{index}]"
                )
                concat_labels.append(f"[gap{index}]")
        filter_parts.append(
            "".join(concat_labels)
            + f"concat=n={len(concat_labels)}:v=0:a=1[joined]"
        )
        filter_parts.append(f"[joined]{args.master_filter}[master]")
        command = ["ffmpeg", "-y", "-v", "error"]
        for scene_path in scene_paths:
            command.extend(["-i", str(scene_path)])
        command.extend(
            [
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                "[master]",
                "-ar",
                str(sample_rate),
                "-ac",
                "1",
                "-c:a",
                "pcm_s24le",
                str(stage_master),
            ]
        )
        subprocess.run(command, check=True)
        actual_duration = media_duration(stage_master)
        if abs(actual_duration - cursor) > 0.02:
            raise RuntimeError(
                f"boundary assembly changed duration: {actual_duration:.6f}s vs {cursor:.6f}s"
            )

        stage_timeline = stage_dir / "timeline.json"
        atomic_json(stage_timeline, new_timeline)
        stage_report = stage_dir / "boundary-qc.json"
        validator = Path(__file__).with_name("validate_audio_boundaries.py")
        validation = subprocess.run(
            [
                sys.executable,
                str(validator),
                "--project",
                str(project),
                "--timeline",
                str(stage_timeline),
                "--master",
                str(stage_master),
                "--report",
                str(stage_report),
            ],
            capture_output=True,
            text=True,
        )
        final_report_path = project / "audio/boundary-qc.json"
        if stage_report.is_file():
            final_report_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(stage_report, final_report_path)
        if validation.returncode != 0:
            if validation.stdout:
                print(validation.stdout, end="")
            if validation.stderr:
                print(validation.stderr, file=sys.stderr, end="")
            raise RuntimeError("audio boundary QC failed; original master and timeline were not replaced")

        if args.backup_dir is not None:
            backup_dir = args.backup_dir.expanduser()
            if not backup_dir.is_absolute():
                backup_dir = (project / backup_dir).resolve()
            backup_files(
                project,
                backup_dir,
                [master_path, timeline_path, captions_path, words_path, manifest_path],
            )

        master_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stage_master, master_path)
        new_timeline["boundary_stability"]["status"] = "pass"
        atomic_json(timeline_path, new_timeline)
        if captions is not None:
            atomic_json(captions_path, captions)
        if words is not None:
            atomic_json(words_path, words)

    final_validation = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("validate_audio_boundaries.py")),
            "--project",
            str(project),
            "--timeline",
            str(timeline_path),
            "--master",
            str(master_path),
            "--report",
            str(project / "audio/boundary-qc.json"),
        ],
        capture_output=True,
        text=True,
    )
    if final_validation.returncode != 0:
        if final_validation.stdout:
            print(final_validation.stdout, end="")
        if final_validation.stderr:
            print(final_validation.stderr, file=sys.stderr, end="")
        raise RuntimeError("final audio boundary QC failed after installing the staged master")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    manifest.update(
        {
            "master_path": str(master_path.relative_to(project)),
            "master_sha256": sha256(master_path),
            "duration_s": round(media_duration(master_path), 6),
            "boundary_stability": {
                "status": "pass",
                "default_gap_s": args.gap,
                "allowed_gap_range_s": [0.12, 0.20],
                "edge_fade_s": args.fade,
                "max_adjacent_scene_lufs_delta": 1.5,
                "max_boundary_rms_delta_db": 6.0,
                "qc_report": "audio/boundary-qc.json",
            },
        }
    )
    post_filter = str(manifest.get("post_filter", "")).rstrip("; ")
    suffix = "short scene gaps; soft boundary fades; master de-resonance; hard boundary QC"
    manifest["post_filter"] = f"{post_filter}; {suffix}" if post_filter else suffix
    atomic_json(manifest_path, manifest)
    print(
        json.dumps(
            {
                "status": "pass",
                "project": str(project),
                "master": str(master_path),
                "duration_s": round(media_duration(master_path), 6),
                "gap_s": args.gap,
                "fade_s": args.fade,
                "master_sha256": sha256(master_path),
                "caption_scene_shifts_s": {key: round(value, 6) for key, value in deltas.items()},
                "qc_report": str(project / "audio/boundary-qc.json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
