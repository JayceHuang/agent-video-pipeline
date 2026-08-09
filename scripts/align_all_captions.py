#!/usr/bin/env python3
from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import hashlib
import json
import math
import os
import re
from pathlib import Path
import shutil
import tempfile
import time
from datetime import datetime, timezone

from mlx_audio.stt.utils import load_model


ROOT = Path(__file__).resolve().parent  # <skill>/scripts
SERIES: dict = {}
MODEL_ID = "mlx-community/Qwen3-ForcedAligner-0.6B-8bit"
ENDING_CTA = ""


def resolve_project(explicit, series: dict, series_path: Path, ep: str) -> Path:
    """--project wins; otherwise series.json must declare project_dir_template."""
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    template = str(series.get("project_dir_template", "")).strip()
    if not template:
        raise SystemExit(
            "pass --project or set project_dir_template in series.json "
            '(e.g. "../<slug>-ep{ep}", resolved relative to series.json)'
        )
    return (series_path.parent / template.format(ep=ep)).resolve()

PUNCT = set("，。！？；：、,.!?;:“”‘’（）()— ")
# Prefer complete clauses. Semicolons are boundaries too so one caption does
# not accumulate several long parallel clauses.
CAPTION_BREAKS = set("，,。！？!?；;")
ENUMERATOR_ONLY_RE = re.compile(r"^第(?:[一二三四五六七八九十百]+|\d+)[，,]$")
CONTINUOUS_GENERATION_MODE = "continuous_episode_take"
MIN_EFFECTIVE_COVERAGE = 0.98
SENTENCE_BREAKS = set("。！？!?")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def append_timing(path: Path, event: dict) -> None:
    payload = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.is_file()
        else {
            "schema_version": 1,
            "wall_clock_started_at": event["started_at"],
            "wall_clock_finished_at": None,
            "wall_clock_elapsed_s": None,
            "events": [],
        }
    )
    payload.setdefault("events", []).append(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_batch({path: payload})


def scenes_with_ending_cta(episode: dict) -> list[dict]:
    """Keep alignment text identical to TTS text and append the CTA once."""
    scenes = [dict(scene) for scene in episode["scenes"]]
    if not ENDING_CTA or not scenes:
        return scenes
    last = scenes[-1]
    text = str(last.get("text", "")).rstrip()
    if not text.endswith(ENDING_CTA):
        last["text"] = f"{text}{ENDING_CTA}"
    return scenes


def map_chars(items, scene, offset):
    aligned = []
    for item in items:
        token = str(item.text)
        if not token:
            continue
        for index, char in enumerate(token):
            aligned.append({"text": char, "start": offset + item.start_time + (item.end_time - item.start_time) * index / len(token), "end": offset + item.start_time + (item.end_time - item.start_time) * (index + 1) / len(token)})
    result = []
    cursor = 0
    last = offset
    for index, char in enumerate(scene["text"]):
        if char in PUNCT or char.isspace():
            start = end = last
        elif cursor < len(aligned):
            start, end = aligned[cursor]["start"], aligned[cursor]["end"]
            cursor += 1
            last = end
        else:
            start = end = last
        result.append({"id": f"w-{scene['id']}-{index + 1:03d}", "scene": scene["id"], "text": char, "start": round(start, 3), "end": round(end, 3)})
    return result


def is_effective(char: str) -> bool:
    return bool(char) and char not in PUNCT and not char.isspace() and char != "｜"


def effective_positions(text: str) -> list[tuple[int, str]]:
    return [(index, char) for index, char in enumerate(text) if is_effective(char)]


def aligned_effective_chars(items) -> list[dict]:
    """Expand aligner tokens to positive-duration effective characters."""
    aligned = []
    for item in items:
        token_chars = [char for char in str(item.text) if is_effective(char)]
        if not token_chars:
            continue
        start = float(item.start_time)
        end = float(item.end_time)
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        # Qwen's forced aligner occasionally emits a real character with a
        # zero-length span (for example, the first character in a repeated
        # syllable).  Dropping that token shifts every subsequent character
        # in the SequenceMatcher map and can make an otherwise exact take
        # fail the hard coverage gate.  Preserve the character with a tiny,
        # explicit epsilon span; downstream caption grouping still uses the
        # neighbouring voiced word times, while every effective character
        # remains positively timed and traceable.
        if end <= start:
            end = start + 0.001
        step = (end - start) / len(token_chars)
        for index, char in enumerate(token_chars):
            aligned.append({
                "text": char,
                "start": start + step * index,
                "end": start + step * (index + 1),
            })
    return aligned


def serialized_alignment_items(items) -> list[dict]:
    """Return compact raw token timings that can be retimed without re-aligning."""
    serialized = []
    for item in items:
        text = str(item.text)
        start = float(item.start_time)
        end = float(item.end_time)
        if not text or not math.isfinite(start) or not math.isfinite(end) or end <= start:
            continue
        serialized.append({
            "text": text,
            "start_s": round(start, 6),
            "end_s": round(end, 6),
        })
    return serialized


def alignment_maps(items, source_text: str) -> tuple[dict[int, dict], set[int], dict]:
    """Map one master alignment back to source offsets and measure coverage."""
    source = effective_positions(source_text)
    aligned = aligned_effective_chars(items)
    if not source:
        raise ValueError("alignment source has no effective characters")
    if not aligned:
        raise RuntimeError("forced alignment returned no positive-duration characters")

    source_chars = [char.casefold() for _, char in source]
    aligned_chars = [item["text"].casefold() for item in aligned]
    matcher = SequenceMatcher(None, source_chars, aligned_chars, autojunk=False)
    timing_by_offset: dict[int, dict] = {}
    exact_offsets: set[int] = set()
    for tag, source_start, source_end, aligned_start, aligned_end in matcher.get_opcodes():
        if tag == "equal":
            for source_index, aligned_index in zip(
                range(source_start, source_end), range(aligned_start, aligned_end)
            ):
                offset = source[source_index][0]
                timing_by_offset[offset] = aligned[aligned_index]
                exact_offsets.add(offset)
        elif tag == "replace" and source_end > source_start and aligned_end > aligned_start:
            # A forced aligner can normalize a Latin letter or digit. Keep the
            # actual aligned span for timing, but not for exact-text coverage.
            span_start = float(aligned[aligned_start]["start"])
            span_end = float(aligned[aligned_end - 1]["end"])
            count = source_end - source_start
            step = (span_end - span_start) / count
            if step <= 0:
                continue
            for relative, source_index in enumerate(range(source_start, source_end)):
                offset = source[source_index][0]
                timing_by_offset[offset] = {
                    "text": source[source_index][1],
                    "start": span_start + step * relative,
                    "end": span_start + step * (relative + 1),
                }

    expected = len(source)
    matched = len(exact_offsets)
    report = {
        "expected_effective_chars": expected,
        "matched_effective_chars": matched,
        "effective_char_coverage": matched / expected,
        "aligned_effective_chars": len(aligned),
    }
    return timing_by_offset, exact_offsets, report


def coverage_for_range(
    source_text: str,
    exact_offsets: set[int],
    timing_by_offset: dict[int, dict],
    start: int,
    end: int,
) -> dict:
    offsets = [index for index in range(start, end) if is_effective(source_text[index])]
    exact = [index for index in offsets if index in exact_offsets]
    timed = [timing_by_offset[index] for index in offsets if index in timing_by_offset]
    first = min((float(item["start"]) for item in timed), default=None)
    last = max((float(item["end"]) for item in timed), default=None)
    return {
        "expected_effective_chars": len(offsets),
        "matched_effective_chars": len(exact),
        "coverage": len(exact) / len(offsets) if offsets else 0.0,
        "first_time_s": round(first, 6) if first is not None else None,
        "last_time_s": round(last, 6) if last is not None else None,
        "has_nonzero_time": first is not None and last is not None and last > first,
    }


def last_sentence_range(text: str) -> tuple[int, int]:
    end = len(text)
    while end > 0 and text[end - 1].isspace():
        end -= 1
    search_end = end
    while search_end > 0 and text[search_end - 1] in SENTENCE_BREAKS:
        search_end -= 1
    start = max(
        (index + 1 for index, char in enumerate(text[:search_end]) if char in SENTENCE_BREAKS),
        default=0,
    )
    return start, end


def build_master_words(
    source_text: str,
    scenes: list[dict],
    scene_ranges: list[tuple[int, int]],
    timing_by_offset: dict[int, dict],
) -> tuple[list[dict], dict[str, list[dict]]]:
    all_words = []
    words_by_scene = {}
    for scene, (scene_start, scene_end) in zip(scenes, scene_ranges):
        words = []
        for local_index, offset in enumerate(range(scene_start, scene_end), start=1):
            char = source_text[offset]
            timing = timing_by_offset.get(offset)
            if is_effective(char):
                if timing is None or float(timing["end"]) <= float(timing["start"]):
                    raise RuntimeError(
                        f"alignment has no nonzero timing for {scene['id']} character "
                        f"{local_index}: {char!r}"
                    )
                start = float(timing["start"])
                end = float(timing["end"])
            else:
                previous = next(
                    (
                        timing_by_offset[index]
                        for index in range(offset - 1, scene_start - 1, -1)
                        if index in timing_by_offset
                    ),
                    None,
                )
                following = next(
                    (
                        timing_by_offset[index]
                        for index in range(offset + 1, scene_end)
                        if index in timing_by_offset
                    ),
                    None,
                )
                boundary = (
                    float(previous["end"])
                    if previous is not None
                    else float(following["start"])
                    if following is not None
                    else None
                )
                if boundary is None:
                    raise RuntimeError(f"scene {scene['id']} contains no timed words")
                start = end = boundary
            words.append({
                "id": f"w-{scene['id']}-{local_index:03d}",
                "scene": scene["id"],
                "text": char,
                "start": round(start, 3),
                "end": round(end, 3),
            })
        positive = [word for word in words if word["end"] > word["start"]]
        if not positive:
            raise RuntimeError(f"scene {scene['id']} contains no positive-duration words")
        words_by_scene[scene["id"]] = words
        all_words.extend(words)
    return all_words, words_by_scene


def global_retime_factor(timeline: dict) -> float:
    value = timeline.get("global_retime_factor")
    if value is None:
        factors = {
            round(float(scene["retime_factor"]), 8)
            for scene in timeline.get("scenes", [])
            if scene.get("retime_factor") is not None
        }
        if len(factors) != 1:
            raise ValueError(
                "continuous timeline must define global_retime_factor "
                "(or one identical retime_factor on every scene)"
            )
        value = factors.pop()
    factor = float(value)
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError(f"invalid global_retime_factor: {value!r}")
    return factor


def atomic_json_batch(payloads: dict[Path, dict]) -> None:
    """Fully stage valid JSON beside the targets, then atomically replace each."""
    parents = {path.parent.resolve() for path in payloads}
    if len(parents) != 1:
        raise ValueError("atomic JSON batch targets must share one directory")
    parent = parents.pop()
    staging = Path(tempfile.mkdtemp(prefix=".caption-alignment-", dir=parent))
    staged = []
    backups: dict[Path, Path | None] = {}
    replaced = []
    try:
        for index, (target, payload) in enumerate(payloads.items()):
            stage = staging / f"{index:02d}-{target.name}"
            with stage.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            json.loads(stage.read_text(encoding="utf-8"))
            staged.append((stage, target))
            if target.exists():
                backup = staging / f"{index:02d}-{target.name}.previous"
                shutil.copy2(target, backup)
                backups[target] = backup
            else:
                backups[target] = None
        for stage, target in staged:
            os.replace(stage, target)
            replaced.append(target)
    except Exception:
        # Batch replacement is not atomic as a set. Restore every target that
        # was already replaced so a write failure cannot leave mixed formal
        # timeline/caption generations behind.
        for target in reversed(replaced):
            backup = backups[target]
            if backup is None:
                target.unlink(missing_ok=True)
            else:
                os.replace(backup, target)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def serialized_json_sha256(payload: dict) -> str:
    """Hash the exact JSON bytes written by atomic_json_batch."""
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def group(words, scene):
    groups, current = [], []
    def flush():
        if not current:
            return
        text = "".join(str(item["text"]) for item in current)
        focus = next((term for term in scene["focus"] if term in text), "")
        groups.append({"id": f"caption-{scene['id']}-{len(groups) + 1:02d}", "scene": scene["id"], "start": current[0]["start"], "end": max(current[-1]["end"], current[0]["start"] + .08), "text": text, "focus": focus, "words": list(current)})
        current.clear()
    for word in words:
        current.append(word)
        # Keep each caption short by breaking at a natural comma, semicolon,
        # or sentence ending. Never split merely by character count or elapsed
        # time.
        current_text = "".join(str(item["text"]) for item in current)
        cta_comma = (
            ENDING_CTA
            and scene["text"].rstrip().endswith(ENDING_CTA)
            and "，" in ENDING_CTA
            and current_text.endswith(ENDING_CTA.split("，", 1)[0] + "，")
        )
        if word["text"] in CAPTION_BREAKS and not cta_comma:
            flush()
    flush()
    # A comma normally ends a readable caption, but an ordinal such as
    # “第五，” is only a discourse marker. Showing it alone creates a one-word
    # subtitle flash and also makes loudness comparisons meaningless. Attach
    # it to the following clause while keeping semicolons and other commas as
    # hard boundaries.
    merged = []
    index = 0
    while index < len(groups):
        item = groups[index]
        item_text = str(item.get("text", "")).strip()
        if (
            item_text.startswith("重复几次")
            and index + 2 < len(groups)
            and str(groups[index + 1].get("text", "")).strip().startswith("发现稳定方法")
            and str(groups[index + 2].get("text", "")).strip().startswith("再封装")
        ):
            chain = groups[index:index + 3]
            combined_words = [word for part in chain for word in part.get("words", [])]
            text = "".join(str(part.get("text", "")) for part in chain)
            focus = next((term for term in scene["focus"] if term in text), "")
            merged.append({
                "id": "",
                "scene": scene["id"],
                "start": chain[0]["start"],
                "end": chain[-1]["end"],
                "text": text,
                "focus": focus,
                "words": combined_words,
            })
            index += 3
            continue
        short_comma_lead = (
            item_text.endswith(("，", ","))
            and sum(1 for char in item_text if is_effective(char)) <= 3
        )
        next_text = str(groups[index + 1].get("text", "")).strip() if index + 1 < len(groups) else ""
        parallel_pair = item_text.startswith("一半") and next_text.startswith("一半")
        if (ENUMERATOR_ONLY_RE.fullmatch(item_text) or short_comma_lead or parallel_pair) and index + 1 < len(groups):
            following = groups[index + 1]
            combined_words = list(item.get("words", [])) + list(following.get("words", []))
            text = str(item.get("text", "")) + str(following.get("text", ""))
            focus = next((term for term in scene["focus"] if term in text), "")
            merged.append({
                "id": "",
                "scene": scene["id"],
                "start": item["start"],
                "end": following["end"],
                "text": text,
                "focus": focus,
                "words": combined_words,
            })
            index += 2
            continue
        merged.append(item)
        index += 1
    groups = merged
    for group_index, item in enumerate(groups, start=1):
        item["id"] = f"caption-{scene['id']}-{group_index:02d}"
    cursor = None
    for item in groups:
        start = float(item["start"])
        if cursor is not None and start < cursor:
            delta = cursor - start
            item["start"] = round(cursor, 3)
            for word in item.get("words", []):
                word["start"] = round(float(word["start"]) + delta, 3)
                word["end"] = round(float(word["end"]) + delta, 3)
            item["end"] = round(float(item["end"]) + delta, 3)
        cursor = float(item["end"])
    return groups


def align_continuous_episode(aligner, episode: dict, project: Path, timeline: dict) -> None:
    scenes = scenes_with_ending_cta(episode)
    timeline_by_id = {scene["id"]: scene for scene in timeline.get("scenes", [])}
    expected_ids = [scene["id"] for scene in scenes]
    if set(timeline_by_id) != set(expected_ids):
        raise ValueError(
            "continuous timeline scene ids do not match series: "
            f"timeline={list(timeline_by_id)} series={expected_ids}"
        )

    scene_ranges = []
    source_parts = []
    cursor = 0
    for scene in scenes:
        text = str(scene["text"])
        source_parts.append(text)
        scene_ranges.append((cursor, cursor + len(text)))
        cursor += len(text)
    source_text = "".join(source_parts)
    master = project / "audio/output/narration_master.wav"
    if not master.is_file():
        raise FileNotFoundError(f"continuous narration master not found: {master}")

    result = aligner.generate(audio=str(master), text=source_text, language="Chinese")
    timing_by_offset, exact_offsets, coverage = alignment_maps(result.items, source_text)
    if coverage["effective_char_coverage"] < MIN_EFFECTIVE_COVERAGE:
        raise RuntimeError(
            "continuous alignment coverage gate failed: "
            f"{coverage['matched_effective_chars']}/{coverage['expected_effective_chars']} "
            f"({coverage['effective_char_coverage']:.2%}) < {MIN_EFFECTIVE_COVERAGE:.0%}"
        )

    cta_start = source_text.rfind(ENDING_CTA) if ENDING_CTA else -1
    if ENDING_CTA and cta_start < 0:
        raise RuntimeError("continuous source text does not contain the configured ending CTA")
    cta_report = (
        coverage_for_range(
            source_text,
            exact_offsets,
            timing_by_offset,
            cta_start,
            cta_start + len(ENDING_CTA),
        )
        if ENDING_CTA
        else None
    )
    if cta_report is not None and not cta_report["has_nonzero_time"]:
        raise RuntimeError("continuous alignment CTA has no nonzero timing")

    all_words, words_by_scene = build_master_words(
        source_text, scenes, scene_ranges, timing_by_offset
    )
    factor = global_retime_factor(timeline)
    updated_scenes = []
    all_groups = []
    for scene, scene_range in zip(scenes, scene_ranges):
        scene_report = coverage_for_range(
            source_text, exact_offsets, timing_by_offset, *scene_range
        )
        if scene_report["matched_effective_chars"] == 0 or not scene_report["has_nonzero_time"]:
            raise RuntimeError(f"continuous alignment has no matched words for scene {scene['id']}")
        start = float(scene_report["first_time_s"])
        end = float(scene_report["last_time_s"])
        spoken_duration = end - start
        effective_chars = scene_report["expected_effective_chars"]
        effective_cpm = effective_chars * 60.0 / spoken_duration
        row = dict(timeline_by_id[scene["id"]])
        row.update({
            "start_s": round(start, 6),
            "end_s": round(end, 6),
            "duration_s": round(spoken_duration, 6),
            "spoken_duration_s": round(spoken_duration, 6),
            "effective_chars": effective_chars,
            "effective_cpm": round(effective_cpm, 3),
            "retime_factor": round(factor, 8),
            "alignment_status": "forced_aligned",
        })
        updated_scenes.append(row)
        all_groups.extend(group(words_by_scene[scene["id"]], scene))

    updated_timeline = dict(timeline)
    updated_timeline["global_retime_factor"] = round(factor, 8)
    updated_timeline["alignment_status"] = "forced_aligned"
    updated_timeline["candidate_selection_status"] = "acoustic_and_alignment_pass"
    updated_timeline["scenes"] = updated_scenes
    alignment_meta = {
        **coverage,
        "effective_char_coverage": round(coverage["effective_char_coverage"], 6),
        "minimum_effective_char_coverage": MIN_EFFECTIVE_COVERAGE,
        "cta": cta_report,
    }
    audio_path = "audio/output/narration_master.wav"
    caption_words = {
        "model": MODEL_ID,
        "audio": audio_path,
        "source_mode": CONTINUOUS_GENERATION_MODE,
        "alignment": alignment_meta,
        "words": all_words,
    }
    caption_groups = {
        "model": MODEL_ID,
        "audio": audio_path,
        "source_mode": CONTINUOUS_GENERATION_MODE,
        "alignment": alignment_meta,
        "groups": all_groups,
    }
    payloads = {
        project / "audio/timeline.json": updated_timeline,
        project / "audio/caption-words.json": caption_words,
        project / "audio/caption-groups.json": caption_groups,
    }
    # Keep the voice manifest bound to the exact alignment generation. This
    # prevents a later render from silently pairing a newly aligned caption
    # track with an older timeline or unreviewed alignment state.
    manifest_path = project / "audio/voice-manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise RuntimeError("audio/voice-manifest.json must contain an object")
        manifest["alignment_status"] = "forced_aligned"
        manifest["alignment"] = alignment_meta
        manifest["timeline_sha256"] = serialized_json_sha256(updated_timeline)
        manifest["caption_words_sha256"] = serialized_json_sha256(caption_words)
        manifest["caption_groups_sha256"] = serialized_json_sha256(caption_groups)
        payloads[manifest_path] = manifest
    atomic_json_batch(payloads)
    print(json.dumps({
        "event": "episode_aligned",
        "episode": episode["episode"],
        "source_mode": CONTINUOUS_GENERATION_MODE,
        "coverage": round(coverage["effective_char_coverage"], 6),
        "words": len(all_words),
        "groups": len(all_groups),
    }, ensure_ascii=False), flush=True)


def align_candidate(aligner, audio: Path, text_file: Path, report_path: Path | None) -> bool:
    if not audio.is_file():
        raise FileNotFoundError(f"candidate audio not found: {audio}")
    if not text_file.is_file():
        raise FileNotFoundError(f"candidate text not found: {text_file}")
    source_text = text_file.read_text(encoding="utf-8").strip()
    result = aligner.generate(audio=str(audio), text=source_text, language="Chinese")
    items = list(result.items)
    timing_by_offset, exact_offsets, report = alignment_maps(items, source_text)
    first = min((float(item["start"]) for item in timing_by_offset.values()), default=None)
    last = max((float(item["end"]) for item in timing_by_offset.values()), default=None)
    cta_start = source_text.rfind(ENDING_CTA) if ENDING_CTA else -1
    cta = (
        coverage_for_range(
            source_text,
            exact_offsets,
            timing_by_offset,
            cta_start,
            cta_start + len(ENDING_CTA),
        )
        if ENDING_CTA and cta_start >= 0
        else {
            "expected_effective_chars": sum(1 for char in ENDING_CTA if is_effective(char)),
            "matched_effective_chars": 0,
            "coverage": 0.0,
            "first_time_s": None,
            "last_time_s": None,
            "has_nonzero_time": False,
        }
    )
    sentence_start, sentence_end = last_sentence_range(source_text)
    final_sentence = coverage_for_range(
        source_text, exact_offsets, timing_by_offset, sentence_start, sentence_end
    )
    passed = (
        report["effective_char_coverage"] >= MIN_EFFECTIVE_COVERAGE
        and (not ENDING_CTA or cta["has_nonzero_time"])
        and final_sentence["has_nonzero_time"]
    )
    errors = []
    if report["effective_char_coverage"] < MIN_EFFECTIVE_COVERAGE:
        errors.append(
            f"effective character coverage {report['effective_char_coverage']:.2%} "
            f"is below {MIN_EFFECTIVE_COVERAGE:.0%}"
        )
    if ENDING_CTA and not cta["has_nonzero_time"]:
        errors.append("ending CTA has no nonzero aligned time")
    if not final_sentence["has_nonzero_time"]:
        errors.append("last sentence has no nonzero aligned time")
    output = {
        "mode": "candidate_coverage",
        "status": "pass" if passed else "fail",
        "errors": errors,
        "audio": str(audio),
        "text_file": str(text_file),
        **report,
        "effective_char_coverage": round(report["effective_char_coverage"], 6),
        "minimum_effective_char_coverage": MIN_EFFECTIVE_COVERAGE,
        "cta": cta,
        "last_sentence": final_sentence,
        "first_time_s": round(first, 6) if first is not None else None,
        "last_time_s": round(last, 6) if last is not None else None,
        "aligned_items": serialized_alignment_items(items),
        "passed": passed,
    }
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_batch({report_path: output})
    print(json.dumps(output, ensure_ascii=False), flush=True)
    return passed


def main() -> None:
    global ENDING_CTA, SERIES
    parser = argparse.ArgumentParser()
    parser.add_argument("--series", type=Path, required=True)
    parser.add_argument("--project", type=Path, help="Explicit episode project directory")
    parser.add_argument("--timings", type=Path, help="Pipeline timing manifest")
    parser.add_argument("--episode", type=int, help="只对齐指定集数")
    parser.add_argument(
        "--source-master",
        action="store_true",
        help="强制对整集 narration_master.wav 只做一次对齐",
    )
    parser.add_argument("--candidate-audio", type=Path, help="只读验收的候选 WAV")
    parser.add_argument("--candidate-text-file", type=Path, help="候选 WAV 对应的纯口播文本")
    parser.add_argument("--candidate-report", type=Path, help="可选的候选覆盖率 JSON 报告")
    args = parser.parse_args()
    series_path = args.series.expanduser().resolve()
    if not series_path.is_file():
        raise FileNotFoundError(f"series file not found: {series_path}")
    SERIES = json.loads(series_path.read_text(encoding="utf-8"))
    ENDING_CTA = str(SERIES.get("ending_cta", "")).strip()
    candidate_mode = args.candidate_audio is not None or args.candidate_text_file is not None
    if candidate_mode and (args.candidate_audio is None or args.candidate_text_file is None):
        parser.error("--candidate-audio and --candidate-text-file must be used together")
    if args.candidate_report is not None and not candidate_mode:
        parser.error("--candidate-report requires candidate mode")
    if candidate_mode and (args.episode is not None or args.source_master):
        parser.error("candidate mode cannot be combined with --episode or --source-master")
    if args.project is not None and args.episode is None and not candidate_mode:
        parser.error("--project requires --episode for full-episode alignment")
    aligner = load_model(MODEL_ID)
    print(json.dumps({"event": "aligner_loaded", "model": MODEL_ID}), flush=True)
    if candidate_mode:
        passed = align_candidate(
            aligner, args.candidate_audio, args.candidate_text_file, args.candidate_report
        )
        if not passed:
            raise SystemExit(1)
        return
    episodes = [item for item in SERIES["episodes"] if args.episode is None or item["episode"] == args.episode]
    if not episodes:
        raise ValueError(f"episode not found: {args.episode}")
    for episode in episodes:
        ep = str(episode["episode"]).zfill(2)
        project = resolve_project(args.project, SERIES, series_path, ep)
        timeline = json.loads((project / "audio/timeline.json").read_text(encoding="utf-8"))
        if args.source_master or timeline.get("generation_mode") == CONTINUOUS_GENERATION_MODE:
            started_at = datetime.now(timezone.utc)
            started = time.perf_counter()
            status = "pass"
            try:
                align_continuous_episode(aligner, episode, project, timeline)
            except Exception:
                status = "fail"
                raise
            finally:
                timing_path = (
                    args.timings.expanduser().resolve()
                    if args.timings is not None
                    else project / "pipeline-timings.json"
                )
                master_path = project / "audio/output/narration_master.wav"
                cache_key = hashlib.sha256(
                    json.dumps(
                        {
                            "master_sha256": file_sha256(master_path) if master_path.is_file() else None,
                            "series_sha256": file_sha256(series_path),
                            "aligner_script_sha256": file_sha256(Path(__file__).resolve()),
                            "model_id": MODEL_ID,
                        },
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                append_timing(
                    timing_path,
                    {
                        "stage": "forced_alignment",
                        "started_at": started_at.isoformat(),
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "elapsed_s": round(time.perf_counter() - started, 3),
                        "status": status,
                        "attempt": 1,
                        "cache_hit": False,
                        "cache_key": cache_key,
                        "metadata": {"episode": episode["episode"], "project": str(project)},
                    },
                )
            continue
        timing = {scene["id"]: scene for scene in timeline["scenes"]}
        all_words, all_groups = [], []
        episode_scenes = scenes_with_ending_cta(episode)
        for scene in episode_scenes:
            row = timing[scene["id"]]
            result = aligner.generate(audio=str(project / row["path"]), text=scene["text"], language="Chinese")
            words = map_chars(result.items, scene, float(row["start_s"]))
            all_words.extend(words)
            all_groups.extend(group(words, scene))
            print(json.dumps({"event": "scene_aligned", "episode": episode["episode"], "scene": scene["id"], "chars": len(words)}, ensure_ascii=False), flush=True)
        (project / "audio/caption-words.json").write_text(json.dumps({"model": MODEL_ID, "audio": timeline["audio"], "words": all_words}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (project / "audio/caption-groups.json").write_text(json.dumps({"model": MODEL_ID, "audio": timeline["audio"], "groups": all_groups}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"event": "episode_aligned", "episode": episode["episode"], "words": len(all_words), "groups": len(all_groups)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
