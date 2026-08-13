#!/usr/bin/env python3
"""Create a conservative, sentence-level prosody plan for Chinese narration.

The script is intentionally deterministic. It provides a reviewable first pass
from a scenes JSON/Markdown file; the calling workflow must review the semantic
labels and mark the resulting prosody JSON as approved before TTS generation.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from profile_config import get_in, load_resolved_profile


IGNORED = set(" \t\n，。！？；：、,.!?;:‘’“”（）()—｜")
TERMINAL_RE = re.compile(r"[^。！？!?；;\n]+(?:[。！？!?；;]|$)")

QUESTION_RE = re.compile(r"(?:为什么|怎么(?:样|做)?|如何|是不是|是否|能不能|有没有|吗$|呢$)")
WARNING_RE = re.compile(r"(?:不要|别(?:被|把|让|再)|避免|警惕|注意|小心|风险|坑|不算|不能|不可|不应该|误把|问题在于|不属于)")
CTA_RE = re.compile(r"(?:点赞|收藏|评论|转发|订阅|私信|一起学习|更多内容)")
DEFINITION_RE = re.compile(r"(?:是|就是|指的是|指|意味着|通常是|本质是|可以理解为)")
INSTRUCTION_RE = re.compile(r"^(?:先|再|然后|现在|请|点击|打开|创建|输入|安装|执行|检查|设置|写下|建立|把)")
CONTRAST_RE = re.compile(r"(?:但是|不过|而是|却|反而|相比|区别|前者|后者|一边|另一边)")
CONCLUSION_RE = re.compile(r"(?:所以|因此|总之|关键是|本质上|简单来说|记住|才算|才是|真正的价值|意味着)")
EN_CTA_RE = re.compile(r"\b(?:follow|subscribe|like|share|comment)\b", re.I)
EN_WARNING_RE = re.compile(r"\b(?:do not|don't|avoid|warning|risk|never|must not|cannot)\b", re.I)
EN_DEFINITION_RE = re.compile(r"\b(?:is defined as|refers to|means|can be understood as)\b", re.I)
EN_INSTRUCTION_RE = re.compile(r"^(?:first|next|then|now|open|create|enter|install|run|check|set)\b", re.I)
EN_CONTRAST_RE = re.compile(r"\b(?:but|however|instead|whereas|unlike|on the other hand)\b", re.I)
EN_CONCLUSION_RE = re.compile(r"\b(?:therefore|so|in summary|the key is|remember|in conclusion)\b", re.I)


def effective_chars(text: str) -> int:
    return sum(1 for char in text if char not in IGNORED)


def load_scenes(path: Path) -> list[dict[str, Any]]:
    """Load the common scenes JSON shape, or a Markdown file split by headings."""
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(raw)
        source = data if isinstance(data, list) else data.get("scenes", [])
        if not isinstance(source, list):
            raise ValueError("JSON must contain a top-level scenes list")
        scenes: list[dict[str, Any]] = []
        for index, item in enumerate(source, start=1):
            if not isinstance(item, dict) or not str(item.get("text", "")).strip():
                raise ValueError(f"Scene {index} is missing non-empty text")
            scenes.append({**item, "id": str(item.get("id", f"{index:02d}"))})
        return scenes

    blocks = re.split(r"(?m)^#{1,3}\s+", raw)
    scenes = []
    for index, block in enumerate(blocks[1:], start=1):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        scenes.append({"id": f"{index:02d}", "title": lines[0], "text": "".join(lines[1:])})
    if not scenes:
        raise ValueError("Markdown must contain headings followed by narration text")
    return scenes


def split_long_segment(segment: str, max_chars: int = 36) -> list[str]:
    segment = segment.strip()
    if len(segment) <= max_chars:
        return [segment]
    pieces = re.split(r"(?<=[，,、])", segment)
    output: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > max_chars:
            output.append(current.strip())
            current = piece
        else:
            current += piece
    if current.strip():
        output.append(current.strip())
    return output or [segment]


def sentence_segments(text: str) -> list[str]:
    segments: list[str] = []
    for match in TERMINAL_RE.finditer(text.replace("\n", "")):
        value = match.group(0).strip()
        if value:
            segments.extend(split_long_segment(value))
    return segments or [text.strip()]


def classify(text: str, cta_text: str = "") -> str:
    value = text.strip()
    if (cta_text and cta_text in value) or CTA_RE.search(value) or EN_CTA_RE.search(value):
        return "cta"
    if value.endswith(("？", "?")) or QUESTION_RE.search(value):
        return "question"
    if WARNING_RE.search(value) or EN_WARNING_RE.search(value):
        return "warning"
    if CONCLUSION_RE.search(value) or EN_CONCLUSION_RE.search(value):
        return "conclusion"
    if CONTRAST_RE.search(value) or EN_CONTRAST_RE.search(value):
        return "contrast"
    if INSTRUCTION_RE.search(value) or EN_INSTRUCTION_RE.search(value):
        return "instruction"
    if DEFINITION_RE.search(value) or EN_DEFINITION_RE.search(value):
        return "definition"
    if value.endswith(("！", "!")):
        return "excited"
    return "statement"


def focus_terms(
    text: str,
    scene_focus: list[str],
    sentence_type: str,
    cta_text: str = "",
) -> list[str]:
    found: list[str] = [term for term in scene_focus if term and term in text]
    patterns = {
        "question": ("为什么", "是不是", "如何", "能不能", "有没有"),
        "warning": ("不要", "别被", "别把", "避免", "风险", "不算", "不能", "坑"),
        "cta": ("收藏", "点赞", "订阅"),
        "conclusion": ("关键", "本质", "真正", "才算", "才是", "记住"),
        "definition": ("指的是", "意味着", "本质是"),
        "instruction": ("创建", "打开", "检查", "点击", "输入", "安装", "执行"),
    }
    for term in patterns.get(sentence_type, ()):
        if term in text and term not in found:
            found.append(term)
    if sentence_type == "cta" and cta_text and cta_text in text:
        configured_focus = cta_text[: min(8, len(cta_text))]
        if configured_focus not in found:
            found.insert(0, configured_focus)
    return found[:3]


def prosody_for(
    text: str,
    sentence_type: str,
    focus: list[str],
    language: str,
    base_style: str,
) -> dict[str, Any]:
    # Semantic prosody is recorded for review and QC, while the acoustic
    # baseline remains locked for the entire take. These labels must never be
    # expanded into independent scene-level voice-state prompts.
    emotion_map = {
        "question": "curious",
        "warning": "warning",
        "conclusion": "warm",
        "cta": "warm",
        "excited": "warm",
    }
    pitch_map = {
        "question": "slightly-up",
        "conclusion": "slightly-down",
        "cta": "slightly-down",
    }
    emotion = emotion_map.get(sentence_type, "calm")
    pitch = pitch_map.get(sentence_type, "stable")
    rate, energy = 1.0, 1 if sentence_type in {"statement", "definition"} else 2
    ending = text.rstrip()[-1:] if text.rstrip() else ""
    pause = {"。": 0.28, "！": 0.28, "!": 0.28, "？": 0.28, "?": 0.28, "；": 0.22, ";": 0.22}.get(ending, 0.16)
    stress = "light"
    is_chinese = language.lower().startswith("zh")
    if is_chinese:
        semantic_cue = {
            "question": "保留轻微探询感",
            "warning": "提醒感清楚但不提高发声力度",
            "conclusion": "温和收束",
            "cta": "自然收束但不提高音量",
            "contrast": "对比关系清楚",
        }.get(sentence_type, "自然陈述")
        instruction = (
            f"{base_style} {semantic_cue}；只改变句内语调和停顿，不改变整集声学基线。"
        ).strip()
        if focus:
            instruction += f" 重点词：{'、'.join(focus)}，只做轻微重读。"
    else:
        semantic_cue = {
            "question": "Keep a lightly inquisitive contour.",
            "warning": "Make the warning clear without increasing vocal effort.",
            "conclusion": "Close gently.",
            "cta": "Close naturally without increasing volume.",
            "contrast": "Make the contrast easy to follow.",
        }.get(sentence_type, "Use a natural statement contour.")
        instruction = (
            f"{base_style} {semantic_cue} Change only phrase-level timing and intonation; "
            "keep the episode acoustic baseline fixed."
        ).strip()
        if focus:
            instruction += f" Lightly stress: {', '.join(focus)}."
    return {
        "pause_after_s": round(min(0.5, pause), 3),
        "stress": stress,
        "emotion": emotion,
        "emotion_strength": energy,
        "pitch": pitch,
        "rate": rate,
        "style_instruction": instruction,
        "intentional_emphasis": False,
    }


def analyze_scene(
    scene: dict[str, Any],
    index: int,
    cta_text: str,
    language: str,
    base_style: str,
) -> dict[str, Any]:
    scene_id = str(scene.get("id", f"{index:02d}"))
    text = str(scene.get("text", "")).strip()
    scene_focus = [str(item) for item in scene.get("focus", []) if str(item).strip()]
    segments: list[dict[str, Any]] = []
    for segment_index, segment_text in enumerate(sentence_segments(text), start=1):
        sentence_type = classify(segment_text, cta_text)
        focus = focus_terms(segment_text, scene_focus, sentence_type, cta_text)
        segment = {
            "id": f"{scene_id}-{segment_index:02d}",
            "text": segment_text,
            "sentence_type": sentence_type,
            "focus": focus,
            **prosody_for(segment_text, sentence_type, focus, language, base_style),
            "start_s": None,
            "end_s": None,
        }
        segments.append(segment)
    return {
        "id": scene_id,
        "title": str(scene.get("title", "")),
        "source_text": text,
        "effective_chars": effective_chars(text),
        "style_instruction": base_style,
        "segments": segments,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenes", "--script", dest="scenes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path, help="resolved profile JSON")
    args = parser.parse_args()

    scenes = load_scenes(args.scenes.expanduser().resolve())
    profile, profile_path = load_resolved_profile(args.profile, None, required=args.profile is not None)
    cta_text = str(get_in(profile, "episode.final_cta", "") or "")
    language = str(get_in(profile, "content.language", "auto") or "auto")
    if language == "auto":
        sample = "".join(str(scene.get("text", "")) for scene in scenes)
        cjk = sum(1 for char in sample if "\u3400" <= char <= "\u9fff")
        language = "zh-CN" if cjk >= max(1, len(sample) // 10) else "en"
    base_style = str(
        get_in(profile, "voice.style_instruction", "Use a clear, natural, consistent narration style.")
    ).strip()
    acoustic_baseline = get_in(profile, "voice.acoustic_baseline", {})
    if not isinstance(acoustic_baseline, dict) or not acoustic_baseline:
        acoustic_baseline = {
            "register": "neutral",
            "vocal_effort": "conversational",
            "breath_pressure": "natural",
            "microphone_distance": "consistent",
            "timbre_brightness": "consistent",
            "global_energy": "consistent",
        }
    output = {
        "schema_version": 2,
        "status": "draft",
        "source": str(args.scenes.expanduser().resolve()),
        "method": "deterministic-semantic-micro-prosody-v2",
        "language": language,
        "defaults": {
            "emotion": "calm",
            "emotion_strength": 1,
            "pitch": "stable",
            "stress": "light",
            "pause_after_s": 0.25,
            "rate": 1.0,
        },
        "rules": {
            "max_emotion_strength": int(get_in(profile, "voice.max_emotion_strength", 2)),
            "rate_range": get_in(profile, "voice.rate_range", [0.98, 1.02]),
            "pitch_policy": get_in(profile, "voice.pitch_policy", "semantic-micro-only"),
            "fixed_tone": False,
            "acoustic_baseline_locked": True,
            "semantic_micro_prosody": True,
            "scene_state_switching": False,
            "control_tags_in_tts_text": False,
            "tts_gate": "approved-only",
        },
        "acoustic_baseline": acoustic_baseline,
        "profile": {
            "path": str(profile_path) if profile_path else None,
            "id": profile.get("profile_id") if profile else None,
            "sha256": get_in(profile, "_meta.profile_sha256") if profile else None,
        },
        "scenes": [
            analyze_scene(scene, index, cta_text, language, base_style)
            for index, scene in enumerate(scenes, start=1)
        ],
    }
    target = args.output.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
