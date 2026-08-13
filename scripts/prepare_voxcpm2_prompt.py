#!/usr/bin/env python3
"""Extract and freeze the complete golden prompt used for VoxCPM2 cloning."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from profile_config import get_in, load_resolved_profile


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
    sample_rate: int,
    channels: int,
    codec: str,
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
        "sample_rate": sample_rate,
        "channels": channels,
        "codec": codec,
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
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--start", type=float)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--prompt-text")
    parser.add_argument("--sample-rate", type=int)
    parser.add_argument("--channels", type=int)
    parser.add_argument("--codec")
    parser.add_argument("--profile", type=Path, help="resolved profile JSON")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--force", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    profile, profile_path = load_resolved_profile(args.profile, None, required=args.profile is not None)
    extract = get_in(profile, "voice.voice_reference_extract", {})
    extract = extract if isinstance(extract, dict) else {}
    source_value = args.source or get_in(profile, "voice.voice_reference_source")
    output_value = args.output or get_in(profile, "voice.voice_prompt_wav")
    prompt_value = args.prompt_text or get_in(profile, "voice.voice_prompt_text")
    start = float(args.start if args.start is not None else extract.get("start_s", 0.0))
    requested_duration = float(
        args.duration if args.duration is not None else extract.get("duration_s", 0.0)
    )
    sample_rate = int(args.sample_rate or extract.get("sample_rate", 48000))
    channels = int(args.channels or extract.get("channels", 1))
    codec = str(args.codec or extract.get("codec", "pcm_s16le"))
    if not source_value or not output_value or not prompt_value:
        raise ValueError(
            "voice source, prompt output, and prompt text must come from CLI or a resolved profile"
        )
    source = Path(str(source_value)).expanduser().absolute()
    output = Path(str(output_value)).expanduser().absolute()
    manifest = output.with_suffix(".json")
    prompt_text = str(prompt_value).strip()
    if not source.is_file():
        raise FileNotFoundError(source)
    if not 6.0 <= requested_duration <= 15.0:
        raise ValueError("golden prompt must be 6-15 seconds")
    if not prompt_text.endswith(("。", "！", "？", ".", "!", "?")):
        raise ValueError("prompt text must be a complete sentence ending in punctuation")
    if args.verify:
        verify_frozen_prompt(
            source,
            output,
            manifest,
            start,
            requested_duration,
            prompt_text,
            sample_rate,
            channels,
            codec,
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
            f"{start:.6f}",
            "-t",
            f"{requested_duration:.6f}",
            "-i",
            str(source),
            "-map_metadata",
            "-1",
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            "-c:a",
            codec,
            str(partial),
        ],
        check=True,
    )
    actual_duration = duration(partial)
    if abs(actual_duration - requested_duration) > 0.05:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"extracted duration {actual_duration:.3f}s differs from requested {requested_duration:.3f}s"
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
        "extract_start_s": start,
        "extract_duration_s": requested_duration,
        "actual_duration_s": round(actual_duration, 6),
        "sample_rate": sample_rate,
        "channels": channels,
        "codec": codec,
        "profile": {
            "path": str(profile_path) if profile_path else None,
            "id": profile.get("profile_id") if profile else None,
            "sha256": get_in(profile, "_meta.profile_sha256") if profile else None,
        },
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
