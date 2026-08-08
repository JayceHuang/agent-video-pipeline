#!/usr/bin/env python3
"""Build a deterministic, reviewable semantic motion plan for HyperFrames.

The planner selects one hero motion per scene and binds supporting motions to
approved narration/caption anchors. It deliberately writes status=draft; the
calling workflow must review the plan and approve it before storyboard/render.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any


IGNORED_RE = re.compile(r"[\s，。！？；：、,.!?;:‘’“”（）()—｜·\-]")
CTA_TEXT = "关注我，给你带来更多AI知识。"
TIER_RANK = {"low": 0, "medium": 1, "high": 2}

ROLE_PATTERNS: dict[str, re.Pattern[str]] = {
    "definition": re.compile(r"(?:是什么|指的是|就是|定义|意味着|可以理解为|本质是|称为)"),
    "process": re.compile(r"(?:步骤|流程|工作流|先.+再|首先|然后|接着|依次|第[一二三四五六七八九十0-9]+步)"),
    "comparison": re.compile(r"(?:对比|相比|区别|不同|不是.+而是|前者|后者|一边.+另一边|VS|vs\.?|优于|劣于)"),
    "metric": re.compile(r"(?:\d+(?:\.\d+)?(?:小时|分钟|秒|天|周|倍|个|条|%|％)?|[零一二两三四五六七八九十百千万]{2,}(?:小时|分钟|秒|天|周|倍|%|％)|(?:两|半)(?:小时|分钟|秒|天|周|倍)|从.+到.+|提升|降低|压到|增长|减少)"),
    "warning": re.compile(r"(?:不要|不能|避免|警惕|注意|小心|风险|坑|错误|失败|误区|问题在于|别把|别让)"),
    "demo": re.compile(r"(?:代码|命令|终端|界面|按钮|点击|打开|安装|输入|运行|API|函数|配置|文件|插件)"),
    "hierarchy": re.compile(r"(?:层级|结构|组成|分类|关系|架构|节点|系统|模块|核心|连接|网络)"),
    "example": re.compile(r"(?:例如|比如|举个例子|案例|场景|有人|有一种|假设)"),
    "conclusion": re.compile(r"(?:所以|因此|总之|总结|关键是|本质上|记住|真正的|真正是|最终|结论|才是|才能)"),
    "hook": re.compile(r"(?:为什么|你有没有|是不是|竟然|真正的问题|越.+越|很多人|大多数人|如果.+会怎样)"),
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_index(seed: int, key: str, count: int) -> int:
    if count <= 0:
        raise ValueError("stable_index requires at least one candidate")
    value = hashlib.sha256(f"{seed}:{key}".encode("utf-8")).hexdigest()
    return int(value[:12], 16) % count


def normalize(text: str) -> str:
    return IGNORED_RE.sub("", text).lower()


def effective_chars(text: str) -> int:
    return len(normalize(text))


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def load_scenes(data: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    metadata = data if isinstance(data, dict) else {}
    source = data if isinstance(data, list) else data.get("scenes", [])
    if not isinstance(source, list) or not source:
        raise ValueError("scenes JSON must contain a non-empty scenes list")
    scenes: list[dict[str, Any]] = []
    for index, item in enumerate(source, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"scene {index} must be an object")
        scene_id = str(item.get("id", f"{index:02d}")).strip()
        text = str(item.get("text", "")).strip()
        if not scene_id or not text:
            raise ValueError(f"scene {index} is missing id or text")
        scenes.append({**item, "id": scene_id, "text": text})
    return metadata, scenes


def classify_scene(scene: dict[str, Any], index: int, total: int) -> tuple[str, str, float]:
    explicit = str(scene.get("semantic_role") or scene.get("motion_role") or "").strip()
    if explicit:
        return explicit, "scene explicitly declares semantic_role/motion_role", 1.0

    title = str(scene.get("title", ""))
    kicker = str(scene.get("kicker", ""))
    visual = str(scene.get("visual", ""))
    text = str(scene.get("text", ""))
    headline = f"{title}。{kicker}"
    scores = {
        role: float(len(pattern.findall(text))) + float(len(pattern.findall(headline))) * 2.25
        for role, pattern in ROLE_PATTERNS.items()
    }
    visual_hints = {
        "definition": ("definition", "concept"),
        "process": ("process", "workflow", "pipeline", "loop", "steps", "investigation"),
        "comparison": ("comparison", "compare", "versus", "split"),
        "metric": ("metric", "goal", "stat", "number", "target"),
        "warning": ("warning", "risk", "error", "pitfall"),
        "demo": ("demo", "code", "terminal", "interface", "ui"),
        "hierarchy": ("hierarchy", "network", "system", "rules", "lenses", "copies"),
        "example": ("example", "case"),
        "conclusion": ("conclusion", "summary", "human-value", "outro"),
        "hook": ("hook", "gap", "question"),
    }
    visual_normalized = visual.lower().replace("_", "-")
    for role, hints in visual_hints.items():
        if any(hint in visual_normalized for hint in hints):
            scores[role] += 3.0
    if index == 0:
        scores["hook"] += 2.25 if re.search(r"[？?]|为什么|越.+越", headline) else 1.5
    if index == total - 1:
        scores["conclusion"] += 1.75
    if CTA_TEXT in text:
        if effective_chars(text.replace(CTA_TEXT, "")) <= 18:
            return "cta", "scene is primarily the fixed CTA", 0.98
        scores["conclusion"] += 0.75
    if re.search(r"[？?]", text):
        scores["hook"] += 0.5 if index == 0 else 0.15
    if not any(scores.values()):
        return "statement", "no stronger semantic pattern matched; use conservative statement recipe", 0.55

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    role, best = ordered[0]
    second = ordered[1][1] if len(ordered) > 1 else 0.0
    confidence = min(0.96, 0.62 + max(0.0, best - second) * 0.09 + min(best, 3.0) * 0.05)
    reason = f"matched {role} semantic cues (score={best:.2f}, runner-up={second:.2f})"
    return role, reason, round(confidence, 3)


def allowed_candidates(
    ids: list[str], primitives: dict[str, Any], max_tier: str
) -> list[str]:
    ceiling = TIER_RANK[max_tier]
    values = [
        item for item in ids
        if item in primitives and TIER_RANK.get(str(primitives[item].get("tier")), 99) <= ceiling
    ]
    return values or ["semantic-card-reveal"]


def primitive_spec(primitive_id: str, primitives: dict[str, Any], costs: dict[str, float]) -> dict[str, Any]:
    spec = primitives[primitive_id]
    cost_name = str(spec.get("cost", "support"))
    return {
        "id": primitive_id,
        "tier": spec.get("tier", "low"),
        "motion_cost": float(costs.get(cost_name, 0.5)),
        "fallback_chain": fallback_chain(primitive_id, primitives),
        "seek_safe": True,
        "loop": False,
    }


def fallback_chain(primitive_id: str, primitives: dict[str, Any]) -> list[str]:
    chain = [primitive_id]
    seen = {primitive_id}
    current = primitive_id
    while current in primitives:
        nxt = primitives[current].get("fallback")
        if not nxt or nxt in seen:
            break
        chain.append(str(nxt))
        seen.add(str(nxt))
        current = str(nxt)
    if chain[-1] != "static-step":
        chain.append("static-step")
    return chain


def timeline_map(data: Any) -> tuple[dict[str, dict[str, Any]], int, int]:
    if not isinstance(data, dict) or not isinstance(data.get("scenes"), list):
        raise ValueError("timeline JSON must contain scenes")
    sample_rate = int(data.get("sample_rate", 48000))
    fps = int(data.get("fps", 30))
    result: dict[str, dict[str, Any]] = {}
    for item in data["scenes"]:
        scene_id = str(item.get("id", ""))
        start = float(item.get("start_s", 0.0))
        end = float(item.get("end_s", start + float(item.get("duration_s", 0.0))))
        if not scene_id or end <= start:
            raise ValueError(f"invalid timeline scene: {scene_id or '<missing id>'}")
        result[scene_id] = {**item, "start_s": start, "end_s": end}
    return result, sample_rate, fps


def prosody_map(data: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("scenes"), list):
        raise ValueError("prosody JSON must contain scenes")
    return {str(item.get("id")): item for item in data["scenes"] if isinstance(item, dict)}


def words_map(data: Any | None) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(data, dict):
        return {}
    source = data.get("words", [])
    result: dict[str, list[dict[str, Any]]] = {}
    for item in source if isinstance(source, list) else []:
        if not isinstance(item, dict):
            continue
        scene_id = str(item.get("scene") or item.get("scene_id") or "")
        if scene_id:
            result.setdefault(scene_id, []).append(item)
    for values in result.values():
        values.sort(key=lambda item: float(item.get("start", item.get("start_s", 0.0))))
    return result


def proportional_segments(
    scene: dict[str, Any], prosody: dict[str, Any], start: float, end: float
) -> list[dict[str, Any]]:
    segments = prosody.get("segments", []) if isinstance(prosody, dict) else []
    if not isinstance(segments, list) or not segments:
        return [{"id": f"{scene['id']}-01", "text": scene["text"], "focus": scene.get("focus", []), "start_s": start, "end_s": end}]
    weights = [max(1, effective_chars(str(segment.get("text", "")))) for segment in segments]
    total = sum(weights)
    cursor = start
    output: list[dict[str, Any]] = []
    for index, (segment, weight) in enumerate(zip(segments, weights), start=1):
        explicit_start = segment.get("start_s")
        explicit_end = segment.get("end_s")
        seg_start = float(explicit_start) if explicit_start is not None else cursor
        seg_end = float(explicit_end) if explicit_end is not None else seg_start + (end - start) * weight / total
        if index == len(segments):
            seg_end = end if explicit_end is None else seg_end
        output.append({
            **segment,
            "id": str(segment.get("id", f"{scene['id']}-{index:02d}")),
            "start_s": max(start, min(seg_start, end)),
            "end_s": max(start, min(seg_end, end)),
        })
        cursor = seg_end
    return output


def word_anchor(term: str, words: list[dict[str, Any]], start_char: int = 0) -> tuple[float, str | None, int, int] | None:
    if not words or not normalize(term):
        return None
    chars: list[str] = []
    char_to_word: list[int] = []
    for word_index, word in enumerate(words):
        normalized = normalize(str(word.get("text", "")))
        for char in normalized:
            chars.append(char)
            char_to_word.append(word_index)
    haystack = "".join(chars)
    needle = normalize(term)
    position = haystack.find(needle, max(0, start_char))
    if position < 0:
        position = haystack.find(needle)
    if position < 0:
        return None
    word = words[char_to_word[position]]
    cue = float(word.get("start", word.get("start_s", 0.0)))
    occurrence = haystack[: position + 1].count(needle)
    return cue, str(word.get("id")) if word.get("id") else None, max(1, occurrence), position + len(needle)


def semantic_candidates(
    scene: dict[str, Any], segments: list[dict[str, Any]], words: list[dict[str, Any]],
    start: float, end: float
) -> list[dict[str, Any]]:
    explicit_visual_beats = scene.get("visual_beats", [])
    if isinstance(explicit_visual_beats, list) and explicit_visual_beats:
        if not words:
            raise ValueError(f"{scene['id']}: visual_beats require caption-word alignment")
        candidates: list[dict[str, Any]] = []
        search_cursor = 0
        for visual_index, visual in enumerate(explicit_visual_beats, start=1):
            if not isinstance(visual, dict):
                raise ValueError(f"{scene['id']}: visual_beats[{visual_index}] must be an object")
            anchor = str(visual.get("anchor") or "").strip()
            title = str(visual.get("title") or anchor).strip()
            detail = str(visual.get("detail") or "").strip()
            slot = int(visual.get("slot", ((visual_index - 1) % 4) + 1))
            if not anchor or not title:
                raise ValueError(f"{scene['id']}: visual_beats[{visual_index}] requires anchor and title")
            if slot not in {1, 2, 3, 4}:
                raise ValueError(f"{scene['id']}: visual_beats[{visual_index}] slot must be 1..4")
            match = word_anchor(anchor, words, search_cursor)
            if not match:
                raise ValueError(
                    f"{scene['id']}: visual beat anchor is absent from forced-aligned words: {anchor}"
                )
            cue, word_id, occurrence, search_cursor = match
            candidates.append({
                "anchor": anchor,
                "cue_s": max(start, min(cue, end - 0.02)),
                "cue_source": "caption-word",
                "sentence_id": str(visual.get("sentence_id") or "") or None,
                "word_id": word_id,
                "occurrence": occurrence,
                "force_visual": True,
                "force_cta": "关注我" in anchor,
                "visual": {
                    "title": title,
                    "detail": detail,
                    "slot": slot,
                    "role": str(visual.get("role") or "semantic-focus"),
                },
            })
        return candidates

    candidates: list[dict[str, Any]] = [{
        "anchor": str(scene.get("title") or scene.get("kicker") or "场景建立"),
        "cue_s": start + min(0.08, max(0.0, (end - start) * 0.02)),
        "cue_source": "scene-start",
        "sentence_id": None,
        "word_id": None,
        "occurrence": 1,
    }]
    search_cursor = 0
    seen: set[str] = set()
    for segment in segments:
        focus = segment.get("focus", [])
        terms = [str(term) for term in focus if str(term).strip()] if isinstance(focus, list) else []
        if not terms:
            text = str(segment.get("text", "")).strip()
            terms = [text[: min(18, len(text))]] if text else []
        for term in terms[:2]:
            key = normalize(term)
            if not key or key in seen:
                continue
            seen.add(key)
            match = word_anchor(term, words, search_cursor)
            if match:
                cue, word_id, occurrence, search_cursor = match
                source = "caption-word"
            else:
                cue = float(segment.get("start_s", start))
                word_id = None
                occurrence = 1
                source = "prosody-proportional"
            candidates.append({
                "anchor": term,
                "cue_s": max(start, min(cue, end - 0.02)),
                "cue_source": source,
                "sentence_id": str(segment.get("id")) if segment.get("id") else None,
                "word_id": word_id,
                "occurrence": occurrence,
            })
    if CTA_TEXT in scene["text"]:
        match = word_anchor("关注我", words, search_cursor)
        candidates.append({
            "anchor": CTA_TEXT,
            "cue_s": match[0] if match else max(start, end - min(2.4, (end - start) * 0.22)),
            "cue_source": "caption-word" if match else "prosody-proportional",
            "sentence_id": segments[-1].get("id") if segments else None,
            "word_id": match[1] if match else None,
            "occurrence": match[2] if match else 1,
            "force_cta": True,
        })
    candidates.sort(key=lambda item: (float(item["cue_s"]), str(item["anchor"])))
    return candidates


def select_evenly(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(candidates) <= limit:
        return candidates
    required = [candidate for candidate in candidates if candidate.get("force_cta")]
    pool = [candidate for candidate in candidates if not candidate.get("force_cta")]
    slots = max(1, limit - len(required))
    if slots >= len(pool):
        chosen = pool
    elif slots == 1:
        chosen = [pool[0]]
    else:
        indices = sorted({round(i * (len(pool) - 1) / (slots - 1)) for i in range(slots)})
        chosen = [pool[index] for index in indices]
    return sorted((chosen + required)[:limit], key=lambda item: float(item["cue_s"]))


def default_safe_boxes() -> list[dict[str, Any]]:
    return [
        {"id": "scene-title", "role": "title", "shape": "rect", "x": 96, "y": 64, "width": 1510, "height": 170, "protected": True},
        {"id": "content-zone", "role": "content", "shape": "rect", "x": 110, "y": 250, "width": 760, "height": 490, "protected": False},
        {"id": "illustration-zone", "role": "illustration", "shape": "rect", "x": 960, "y": 250, "width": 760, "height": 500, "protected": True},
        {"id": "caption-zone", "role": "caption", "shape": "rect", "x": 368, "y": 900, "width": 1312, "height": 152, "protected": True},
        {"id": "avatar-circle", "role": "avatar", "shape": "circle", "x": 42, "y": 752, "diameter": 300, "protected": True},
    ]


def transition_for(role: str, previous_role: str | None, index: int, grammar: list[str]) -> tuple[str, str]:
    if index == 0:
        return "cut", "first content scene follows the fixed title card"
    if "cinematic-zoom" in grammar:
        if index == 1:
            return "cinematic-zoom", "first major content reveal uses the cinematic hero transition"
        if index == 4 and "domain-warp" in grammar:
            return "domain-warp", "one later concept peak uses the second and final shader accent"
        if "blur-crossfade" in grammar:
            return "blur-crossfade", "non-hero cinematic seams use the stable soft transition"
    if role in {"conclusion", "cta"} and "blur-crossfade" in grammar:
        return "blur-crossfade", "wind-down or conclusion needs a softer handoff"
    if previous_role and role != previous_role and "zoom-through" in grammar and index in {1, 4}:
        return "zoom-through", "major semantic turn uses the accent transition"
    if "push-slide" in grammar:
        return "push-slide", "adjacent tutorial point continues in the primary direction"
    return grammar[0], "use the profile primary transition"


def asset_refs(scene: dict[str, Any], visual_assets: Any) -> list[str]:
    if not isinstance(visual_assets, dict):
        return []
    haystack = normalize(" ".join([
        str(scene.get("id", "")), str(scene.get("title", "")), str(scene.get("visual", "")), str(scene.get("kicker", ""))
    ]))
    refs: list[str] = []
    for shot in visual_assets.get("shot_list", []):
        if not isinstance(shot, dict):
            continue
        shot_id = str(shot.get("shot_id", ""))
        if str(shot.get("scene_id", "")) == str(scene.get("id", "")) and shot_id:
            refs.append(shot_id)
            continue
        tokens = [normalize(shot_id), normalize(str(shot.get("meaning", ""))), normalize(str(shot.get("structure", "")))]
        if any(token and (token in haystack or any(part and part in token for part in re.split(r"[-_]", normalize(str(scene.get("visual", "")))))) for token in tokens):
            refs.append(shot_id)
    return list(dict.fromkeys(refs))[:2]


def build_scene_plan(
    scene: dict[str, Any], index: int, total: int, timing: dict[str, Any], prosody: dict[str, Any],
    words: list[dict[str, Any]], visual_assets: Any, profile_id: str, profile: dict[str, Any],
    catalog: dict[str, Any], seed: int, previous_role: str | None, previous_hero: str | None,
    sample_rate: int, fps: int
) -> dict[str, Any]:
    start = float(timing["start_s"])
    end = float(timing["end_s"])
    duration = end - start
    role, reason, confidence = classify_scene(scene, index, total)
    recipes = catalog["semantic_recipes"]
    if role not in recipes:
        role = "statement"
        reason += "; unknown role fell back to statement"
        confidence = min(confidence, 0.5)
    recipe = recipes[role]
    primitives = catalog["primitives"]
    costs = catalog["motion_costs"]
    hero_choices = allowed_candidates(recipe["hero"], primitives, profile["max_primitive_tier"])
    hero_choice_index = stable_index(seed, f"{scene['id']}:hero", len(hero_choices))
    hero_id = hero_choices[hero_choice_index]
    if previous_hero == hero_id and len(hero_choices) > 1:
        hero_id = hero_choices[(hero_choice_index + 1) % len(hero_choices)]
        reason += "; rotated hero candidate to avoid an identical adjacent scene"
    support_choices = allowed_candidates(recipe["support"], primitives, profile["max_primitive_tier"])

    avg_interval = sum(profile["event_interval_s"]) / 2.0
    explicit_visual_beats = scene.get("visual_beats", [])
    if isinstance(explicit_visual_beats, list) and explicit_visual_beats:
        if len(explicit_visual_beats) > int(profile["max_events_per_scene"]):
            raise ValueError(
                f"{scene['id']}: {len(explicit_visual_beats)} visual beats exceed profile max "
                f"{profile['max_events_per_scene']}"
            )
        desired_events = len(explicit_visual_beats)
    else:
        desired_events = clamp(round(duration / avg_interval) + 1, 2, int(profile["max_events_per_scene"]))
    support_min, support_max = [int(value) for value in profile["support_motion_range"]]
    support_count = clamp(desired_events - 2, support_min, support_max)
    support_ids: list[str] = []
    for offset in range(min(support_count, len(support_choices))):
        candidate = support_choices[(stable_index(seed, f"{scene['id']}:support", len(support_choices)) + offset) % len(support_choices)]
        if candidate not in support_ids:
            support_ids.append(candidate)

    segments = proportional_segments(scene, prosody, start, end)
    semantic = semantic_candidates(scene, segments, words, start, end)
    candidates = semantic if explicit_visual_beats else select_evenly(semantic, desired_events)
    hero_assigned = False
    beats: list[dict[str, Any]] = []
    for beat_index, candidate in enumerate(candidates, start=1):
        is_cta = bool(candidate.get("force_cta"))
        if beat_index == 1 and len(candidates) > 1:
            primitive_id = "accent-rule-draw"
            priority = "micro"
        elif is_cta:
            primitive_id = "follow-card-arrow-single-ripple"
            priority = "primary"
        elif not hero_assigned:
            primitive_id = hero_id
            priority = "primary"
            hero_assigned = True
        else:
            primitive_id = support_ids[(beat_index - 2) % len(support_ids)] if support_ids else "fade-slide"
            priority = "support"
        primitive = primitive_spec(primitive_id, primitives, costs)
        cue = round(float(candidate["cue_s"]), 6)
        motion_duration = 0.62 if priority == "primary" else (0.42 if priority == "support" else 0.28)
        settle = round(min(end, cue + motion_duration), 6)
        visual = candidate.get("visual") if isinstance(candidate.get("visual"), dict) else {
            "title": str(candidate["anchor"]),
            "detail": "",
            "slot": ((beat_index - 1) % 4) + 1,
            "role": "semantic-focus",
        }
        beats.append({
            "id": f"{scene['id']}-b{beat_index:02d}",
            "semantic_anchor": candidate["anchor"],
            "anchor_hash": hashlib.sha256(normalize(str(candidate["anchor"])).encode("utf-8")).hexdigest(),
            "cue_source": candidate["cue_source"],
            "sentence_id": candidate.get("sentence_id"),
            "word_id": candidate.get("word_id"),
            "occurrence": candidate.get("occurrence", 1),
            "cue_s": cue,
            "audio_sample": int(round(cue * sample_rate)),
            "render_frame": int(round(cue * fps)),
            "settle_s": settle,
            "target_ref": f"beat:{scene['id']}:{beat_index:02d}",
            "visual": visual,
            "primitive": primitive_id,
            "priority": priority,
            "motion_cost": primitive["motion_cost"],
            "fallback_chain": primitive["fallback_chain"],
            "seek_safe": True,
            "loop": False,
        })

    holds: list[dict[str, Any]] = []
    for left, right in zip(beats, beats[1:]):
        gap = float(right["cue_s"]) - float(left["settle_s"])
        if gap > 4.0:
            holds.append({
                "start_s": left["settle_s"],
                "end_s": right["cue_s"],
                "semantic_owner": left["id"],
                "intentional": False,
                "reason_code": "review-required",
            })

    grammar = list(profile["transition_grammar"])
    transition_id, transition_reason = transition_for(role, previous_role, index, grammar)
    transition_spec = catalog["transitions"][transition_id]
    return {
        "id": scene["id"],
        "title": str(scene.get("title", "")),
        "start_s": round(start, 6),
        "end_s": round(end, 6),
        "duration_s": round(duration, 6),
        "start_audio_sample": int(round(start * sample_rate)),
        "end_audio_sample": int(round(end * sample_rate)),
        "start_render_frame": int(round(start * fps)),
        "end_render_frame": int(round(end * fps)),
        "semantic_role": role,
        "selection_reason": reason,
        "classification_confidence": confidence,
        "layout_variant": recipe["layout"],
        "blueprint": recipe["blueprint"],
        "safe_boxes": default_safe_boxes(),
        "asset_refs": asset_refs(scene, visual_assets),
        "hero_motion": primitive_spec(hero_id, primitives, costs),
        "supporting_motions": [primitive_spec(item, primitives, costs) for item in support_ids],
        "transition_in": {
            "id": transition_id,
            "reason": transition_reason,
            "shader": bool(transition_spec.get("shader")),
            "fallback_chain": [transition_id, transition_spec.get("fallback")] if transition_spec.get("fallback") else [transition_id],
            "duration_s": 0.42 if transition_id not in {"cut", "crossfade"} else (0.0 if transition_id == "cut" else 0.5),
        },
        "beats": beats,
        "intentional_holds": holds,
        "budget": {
            "planned_events": len(beats),
            "max_simultaneous_animated_elements": profile["max_simultaneous_animated_elements"],
            "max_major_camera_moves": profile["max_major_camera_moves_per_scene"],
            "max_particles": profile["max_particles_per_scene"],
            "swept_bbox_check_required": True,
        },
        "profile_id": profile_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenes", type=Path, required=True)
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--prosody", type=Path, required=True)
    parser.add_argument("--visual-assets", type=Path, required=True)
    parser.add_argument("--caption-words", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--profile", default="premium-balanced")
    parser.add_argument("--seed", type=int, default=20260807)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    catalog_path = (args.catalog or script_dir.parent / "references/motion-catalog.json").expanduser().resolve()
    paths = {
        "scenes": args.scenes.expanduser().resolve(),
        "timeline": args.timeline.expanduser().resolve(),
        "prosody": args.prosody.expanduser().resolve(),
        "visual_assets": args.visual_assets.expanduser().resolve(),
    }
    if args.caption_words:
        paths["caption_words"] = args.caption_words.expanduser().resolve()
    for name, path in {**paths, "catalog": catalog_path}.items():
        if not path.is_file():
            raise FileNotFoundError(f"missing {name}: {path}")

    catalog = load_json(catalog_path)
    if args.profile not in catalog.get("profiles", {}):
        raise ValueError(f"unknown motion profile: {args.profile}")
    profile = catalog["profiles"][args.profile]
    scene_metadata, scenes = load_scenes(load_json(paths["scenes"]))
    timings, sample_rate, fps_from_timeline = timeline_map(load_json(paths["timeline"]))
    fps = int(scene_metadata.get("fps", fps_from_timeline or 30))
    prosodies = prosody_map(load_json(paths["prosody"]))
    visual_assets = load_json(paths["visual_assets"])
    word_data = load_json(paths["caption_words"]) if "caption_words" in paths else None
    word_groups = words_map(word_data)

    scene_ids = [scene["id"] for scene in scenes]
    missing_timing = [scene_id for scene_id in scene_ids if scene_id not in timings]
    missing_prosody = [scene_id for scene_id in scene_ids if scene_id not in prosodies]
    if missing_timing or missing_prosody:
        raise ValueError(f"scene IDs missing from timeline/prosody: timing={missing_timing}, prosody={missing_prosody}")

    plans: list[dict[str, Any]] = []
    previous_role: str | None = None
    previous_hero: str | None = None
    for index, scene in enumerate(scenes):
        plan = build_scene_plan(
            scene, index, len(scenes), timings[scene["id"]], prosodies[scene["id"]],
            word_groups.get(scene["id"], []), visual_assets, args.profile, profile,
            catalog, args.seed, previous_role, previous_hero, sample_rate, fps,
        )
        plans.append(plan)
        previous_role = str(plan["semantic_role"])
        previous_hero = str(plan["hero_motion"]["id"])

    source_manifest = {
        name: {"path": str(path), "sha256": file_sha256(path)} for name, path in paths.items()
    }
    output = {
        "schema_version": 1,
        "status": "draft",
        "method": "deterministic-semantic-motion-v1",
        "compiler": {
            "name": Path(__file__).name,
            "version": 1,
            "sha256": file_sha256(Path(__file__).resolve()),
        },
        "profile": {
            "id": args.profile,
            "catalog_path": str(catalog_path),
            "catalog_sha256": file_sha256(catalog_path),
            "description": profile["description"],
            "budgets": profile,
        },
        "seed": args.seed,
        "sources": source_manifest,
        "clock": {"sample_rate": sample_rate, "fps": fps, "rounding": "nearest-frame"},
        "transition_grammar": profile["transition_grammar"],
        "global_safe_zones": default_safe_boxes(),
        "scenes": plans,
        "review": {
            "required": True,
            "approved_by": None,
            "notes": [
                "Review semantic roles, anchors, hero/support hierarchy, safe boxes and any review-required holds.",
                "Set status=approved only after visual-assets and audio boundary gates have passed.",
            ],
        },
    }
    target = args.output.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(target), "status": "draft", "profile": args.profile, "scenes": len(plans)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
