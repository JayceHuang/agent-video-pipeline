#!/usr/bin/env python3
"""Extract and freeze the complete golden prompt used for VoxCPM2 cloning."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_SOURCE = Path(
    "/Users/jaycehuang/Library/CloudStorage/SynologyDrive-Obsidian/"
    "66_自媒体/木哥原始音频/木哥音频.mp3"
)
DEFAULT_OUTPUT = Path(
    "/Users/jaycehuang/obsidian-proj/videos/voxcpm2-voice-reference/"
    "audio/muge-golden-prompt-v1.wav"
)
DEFAULT_TEXT = (
    "大家在去做GEO的时候，为了让所有的AI平台都去推荐你，"
    "一定是全平台的分发而不是只能铺一个渠道。"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def duration(path: Path) -> float:
    return audio_spec(path)["duration_s"]


def audio_spec(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,sample_rate,channels:format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError(f"no audio stream found: {path}")
    stream = streams[0]
    return {
        "duration_s": float(payload["format"]["duration"]),
        "sample_rate": int(stream["sample_rate"]),
        "channels": int(stream["channels"]),
        "codec": stream["codec_name"],
    }


def verify_frozen_prompt(
    source: Path,
    output: Path,
    manifest: Path,
    start: float,
    requested_duration: float,
    prompt_text: str,
) -> None:
    problems = []
    if not output.is_file():
        problems.append(f"prompt WAV missing: {output}")
    if not manifest.is_file():
        problems.append(f"manifest missing: {manifest}")
    if problems:
        raise RuntimeError(
            "verification failed: " + "; ".join(problems) + "; rerun with --force"
        )

    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"verification failed: unreadable manifest ({exc}); rerun with --force"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            "verification failed: manifest root must be an object; rerun with --force"
        )

    expected_values = {
        "source_sha256": sha256(source),
        "prompt_text": prompt_text,
        "prompt_wav_sha256": sha256(output),
        "sample_rate": 48000,
        "channels": 1,
        "codec": "pcm_s16le",
    }
    for field, actual in expected_values.items():
        if payload.get(field) != actual:
            problems.append(
                f"{field} mismatch (manifest={payload.get(field)!r}, actual={actual!r})"
            )

    for field, requested in (
        ("extract_start_s", start),
        ("extract_duration_s", requested_duration),
    ):
        try:
            recorded = float(payload[field])
        except (KeyError, TypeError, ValueError):
            problems.append(f"{field} is missing or invalid")
        else:
            if abs(recorded - requested) > 1e-6:
                problems.append(
                    f"{field} mismatch (manifest={recorded:.6f}, requested={requested:.6f})"
                )

    try:
        spec = audio_spec(output)
    except (KeyError, TypeError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        problems.append(f"cannot inspect prompt WAV ({exc})")
    else:
        for field in ("sample_rate", "channels", "codec"):
            required = expected_values[field]
            if spec[field] != required:
                problems.append(
                    f"WAV {field} mismatch (actual={spec[field]!r}, required={required!r})"
                )
        if abs(spec["duration_s"] - requested_duration) > 0.05:
            problems.append(
                f"WAV duration mismatch (actual={spec['duration_s']:.3f}, "
                f"requested={requested_duration:.3f})"
            )
        try:
            recorded_actual_duration = float(payload["actual_duration_s"])
        except (KeyError, TypeError, ValueError):
            problems.append("actual_duration_s is missing or invalid")
        else:
            if abs(spec["duration_s"] - recorded_actual_duration) > 0.001:
                problems.append(
                    "actual_duration_s mismatch "
                    f"(manifest={recorded_actual_duration:.6f}, actual={spec['duration_s']:.6f})"
                )

    if problems:
        raise RuntimeError(
            "verification failed: " + "; ".join(problems) + "; rerun with --force"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", type=float, default=310.95)
    parser.add_argument("--duration", type=float, default=8.20)
    parser.add_argument("--prompt-text", default=DEFAULT_TEXT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--force", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    manifest = output.with_suffix(".json")
    prompt_text = args.prompt_text.strip()
    if not source.is_file():
        raise FileNotFoundError(source)
    if not 6.0 <= args.duration <= 15.0:
        raise ValueError("golden prompt must be 6-15 seconds")
    if not prompt_text.endswith(("。", "！", "？", ".", "!", "?")):
        raise ValueError("prompt text must be a complete sentence ending in punctuation")
    if args.verify:
        verify_frozen_prompt(
            source,
            output,
            manifest,
            args.start,
            args.duration,
            prompt_text,
        )
        print(f"verified: {output}")
        return 0
    if output.exists() and not args.force:
        raise FileExistsError(f"refusing to replace frozen prompt without --force: {output}")

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".part.wav")
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{args.start:.6f}",
            "-t",
            f"{args.duration:.6f}",
            "-i",
            str(source),
            "-map_metadata",
            "-1",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(partial),
        ],
        check=True,
    )
    actual_duration = duration(partial)
    if abs(actual_duration - args.duration) > 0.05:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"extracted duration {actual_duration:.3f}s differs from requested {args.duration:.3f}s"
        )
    partial.replace(output)

    payload = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "voxcpm2_ultimate_cloning",
        "source_path": str(source),
        "source_sha256": sha256(source),
        "prompt_wav_path": str(output),
        "reference_wav_path": str(output),
        "prompt_wav_sha256": sha256(output),
        "prompt_text": prompt_text,
        "extract_start_s": args.start,
        "extract_duration_s": args.duration,
        "actual_duration_s": round(actual_duration, 6),
        "sample_rate": 48000,
        "channels": 1,
        "codec": "pcm_s16le",
    }
    manifest_partial = manifest.with_name(manifest.name + ".part")
    manifest_partial.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest_partial.replace(manifest)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
