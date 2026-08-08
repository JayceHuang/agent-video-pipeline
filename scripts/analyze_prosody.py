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


IGNORED = set(" \t\n，。！？；：、,.!?;:‘’“”（）()—｜")
TERMINAL_RE = re.compile(r"[^。！？!?；;\n]+(?:[。！？!?；;]|$)")

QUESTION_RE = re.compile(r"(?:为什么|怎么(?:样|做)?|如何|是不是|是否|能不能|有没有|吗$|呢$)")
WARNING_RE = re.compile(r"(?:不要|别(?:被|把|让|再)|避免|警惕|注意|小心|风险|坑|不算|不能|不可|不应该|误把|问题在于|不属于)")
CTA_RE = re.compile(r"(?:关注我|点赞|收藏|评论|转发|订阅|私信|加我|一起学习|更多教程)")
DEFINITION_RE = re.compile(r"(?:是|就是|指的是|指|意味着|通常是|本质是|可以理解为)")
INSTRUCTION_RE = re.compile(r"^(?:先|再|然后|现在|请|点击|打开|创建|输入|安装|执行|检查|设置|写下|建立|把)")
CONTRAST_RE = re.compile(r"(?:但是|不过|而是|却|反而|相比|区别|前者|后者|一边|另一边)")
CONCLUSION_RE = re.compile(r"(?:所以|因此|总之|关键是|本质上|简单来说|记住|才算|才是|真正的价值|意味着)")


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


def classify(text: str) -> str:
    value = text.strip()
    if CTA_RE.search(value):
        return "cta"
    if value.endswith(("？", "?")) or QUESTION_RE.search(value):
        return "question"
    if WARNING_RE.search(value):
        return "warning"
    if CONCLUSION_RE.search(value):
        return "conclusion"
    if CONTRAST_RE.search(value):
        return "contrast"
    if INSTRUCTION_RE.search(value):
        return "instruction"
    if DEFINITION_RE.search(value):
        return "definition"
    if value.endswith(("！", "!")):
        return "excited"
    return "statement"


def focus_terms(text: str, scene_focus: list[str], sentence_type: str) -> list[str]:
    found: list[str] = [term for term in scene_focus if term and term in text]
    patterns = {
        "question": ("为什么", "是不是", "如何", "能不能", "有没有"),
        "warning": ("不要", "别被", "别把", "避免", "风险", "不算", "不能", "坑"),
        "cta": ("关注我", "更多教程", "收藏", "点赞"),
        "conclusion": ("关键", "本质", "真正", "才算", "才是", "记住"),
        "definition": ("本地优先", "Markdown", "链接", "工作流", "Agent"),
        "instruction": ("创建", "打开", "写下", "检查", "点击", "输入", "安装", "执行"),
    }
    for term in patterns.get(sentence_type, ()):
        if term in text and term not in found:
            found.append(term)
    return found[:3]


def prosody_for(text: str, sentence_type: str, focus: list[str]) -> dict[str, Any]:
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
    semantic_cue = {
        "question": "保留轻微探询感",
        "warning": "提醒感清楚但不提高发声力度",
        "conclusion": "温和收束",
        "cta": "亲切收束但不提高音量",
        "contrast": "对比关系清楚",
    }.get(sentence_type, "自然陈述")
    instruction = (
        f"{semantic_cue}；只改变句内语调和停顿，不改变中音区、气息力度、距离、"
        "声线明暗或整体速度。"
    )
    if focus:
        instruction += f" 重点词：{'、'.join(focus)}，只做轻微重读。"
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


def analyze_scene(scene: dict[str, Any], index: int) -> dict[str, Any]:
    scene_id = str(scene.get("id", f"{index:02d}"))
    text = str(scene.get("text", "")).strip()
    scene_focus = [str(item) for item in scene.get("focus", []) if str(item).strip()]
    segments: list[dict[str, Any]] = []
    for segment_index, segment_text in enumerate(sentence_segments(text), start=1):
        sentence_type = classify(segment_text)
        focus = focus_terms(segment_text, scene_focus, sentence_type)
        segment = {
            "id": f"{scene_id}-{segment_index:02d}",
            "text": segment_text,
            "sentence_type": sentence_type,
            "focus": focus,
            **prosody_for(segment_text, sentence_type, focus),
            "start_s": None,
            "end_s": None,
        }
        segments.append(segment)
    scene_instruction = (
        "自然、有交流感的知识讲解；语气跟随标点和句意轻微变化，同时锁定同一中音区、"
        "同一气息力度、同一麦克风距离和同一声线明暗。禁止整段情绪换挡、喊读或耳语。"
    )
    return {
        "id": scene_id,
        "title": str(scene.get("title", "")),
        "source_text": text,
        "effective_chars": effective_chars(text),
        "style_instruction": scene_instruction,
        "segments": segments,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenes", "--script", dest="scenes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    scenes = load_scenes(args.scenes.expanduser().resolve())
    output = {
        "schema_version": 2,
        "status": "draft",
        "source": str(args.scenes.expanduser().resolve()),
        "method": "deterministic-semantic-micro-prosody-v2",
        "defaults": {
            "emotion": "calm",
            "emotion_strength": 1,
            "pitch": "stable",
            "stress": "light",
            "pause_after_s": 0.25,
            "rate": 1.0,
        },
        "rules": {
            "max_emotion_strength": 2,
            "rate_range": [0.98, 1.02],
            "pitch_policy": "semantic-micro-only",
            "fixed_tone": False,
            "acoustic_baseline_locked": True,
            "semantic_micro_prosody": True,
            "scene_state_switching": False,
            "control_tags_in_tts_text": False,
            "tts_gate": "approved-only",
        },
        "acoustic_baseline": {
            "register": "stable-mid",
            "vocal_effort": "stable-conversational",
            "breath_pressure": "stable",
            "microphone_distance": "fixed",
            "timbre_brightness": "fixed",
            "global_energy": "fixed",
        },
        "scenes": [analyze_scene(scene, index) for index, scene in enumerate(scenes, start=1)],
    }
    target = args.output.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
