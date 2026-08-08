#!/usr/bin/env python3
"""Extract the final MP4 audio into the dated delivery package.

The delivery audio is intentionally decoded from final.mp4 rather than copied
from an earlier narration master, so its timing and the title/CTA sound cues
remain identical to the published video.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-of",
            "json",
            "-show_entries",
            "format=duration:stream=codec_name,codec_type,sample_rate,channels",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    duration = float(data.get("format", {}).get("duration", 0.0))
    audio = next(
        (stream for stream in data.get("streams", []) if stream.get("codec_type") == "audio"),
        None,
    )
    if not audio:
        raise RuntimeError(f"no audio stream found in {path}")
    return {
        "duration_s": duration,
        "codec": audio.get("codec_name"),
        "sample_rate": int(audio.get("sample_rate", 0)),
        "channels": int(audio.get("channels", 0)),
    }


def extract(video: Path, output: Path, codec_args: list[str]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{output.stem}-", suffix=output.suffix + ".part", dir=output.parent, delete=False
    ) as handle:
        partial = Path(handle.name)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video),
                "-map",
                "0:a:0",
                "-vn",
                *codec_args,
                "-f",
                output.suffix.lstrip(".") or "wav",
                str(partial),
            ],
            check=True,
        )
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args()

    video = args.video.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not video.is_file():
        raise FileNotFoundError(video)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = (
        args.manifest.expanduser().resolve()
        if args.manifest
        else output_dir / "asset-manifest.json"
    )
    if not manifest_path.is_file():
        raise FileNotFoundError(f"delivery asset manifest not found: {manifest_path}")

    source = probe(video)
    if source["sample_rate"] != 48000 or source["channels"] != 2:
        raise RuntimeError(
            "final MP4 audio must be 48kHz stereo before delivery extraction: "
            f"{source['sample_rate']}Hz/{source['channels']}ch"
        )

    wav_path = output_dir / "audio/final-audio.wav"
    mp3_path = output_dir / "audio/final-audio.mp3"
    extract(video, wav_path, ["-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le"])
    extract(video, mp3_path, ["-ar", "48000", "-ac", "2", "-c:a", "libmp3lame", "-q:a", "2"])
    wav = probe(wav_path)
    mp3 = probe(mp3_path)
    for label, row in (("WAV", wav), ("MP3", mp3)):
        if row["sample_rate"] != 48000 or row["channels"] != 2:
            raise RuntimeError(f"{label} extraction did not produce 48kHz stereo: {row}")
        if abs(row["duration_s"] - source["duration_s"]) > 0.02:
            raise RuntimeError(
                f"{label} duration {row['duration_s']:.6f}s differs from video "
                f"{source['duration_s']:.6f}s"
            )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["delivery_audio"] = {
        "extracted_from_final_mp4": True,
        "source_video": {
            "path": video.name,
            "sha256": sha256(video),
            "duration_s": round(source["duration_s"], 6),
            "sample_rate": source["sample_rate"],
            "channels": source["channels"],
            "codec": source["codec"],
        },
        "wav": {
            "path": "audio/final-audio.wav",
            "sha256": sha256(wav_path),
            "duration_s": round(wav["duration_s"], 6),
            "sample_rate": wav["sample_rate"],
            "channels": wav["channels"],
            "codec": wav["codec"],
        },
        "mp3": {
            "path": "audio/final-audio.mp3",
            "sha256": sha256(mp3_path),
            "duration_s": round(mp3["duration_s"], 6),
            "sample_rate": mp3["sample_rate"],
            "channels": mp3["channels"],
            "codec": mp3["codec"],
        },
    }
    partial_manifest = manifest_path.with_name(manifest_path.name + ".part")
    partial_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    partial_manifest.replace(manifest_path)
    print(json.dumps({"status": "pass", "manifest": str(manifest_path), "delivery_audio": manifest["delivery_audio"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
