# 语气分析契约

`prosody.json` 是剧本和 VoxCPM2 之间的强制中间层。它把“固定声学基线”和“自然语义语气”分开：前者整集锁定，后者只描述句内的停顿、轻重和小幅语调。控制标签不能拼进朗读文本，也不能被展开成多个相互独立的场景语气 prompt。

## 生成和验收

```bash
python scripts/analyze_prosody.py \
  --scenes /path/to/scenes.json \
  --output /path/to/audio/prosody.json

python scripts/validate_prosody.py \
  --prosody /path/to/audio/prosody.json
```

语义审核通过后，把顶层 `status` 从 `draft` 改为 `approved`，再执行最终门禁：

```bash
python scripts/validate_prosody.py \
  --prosody /path/to/audio/prosody.json \
  --require-approved
```

## 字段

每个句子/语义短语必须包含：

```json
{
  "text": "原始口播文本，不包含控制标签",
  "sentence_type": "statement|question|warning|definition|instruction|contrast|conclusion|cta|excited",
  "focus": ["需要轻微重读的词"],
  "pause_after_s": 0.28,
  "stress": "light|strong",
  "emotion": "calm|curious|warning|excited|warm",
  "emotion_strength": 1,
  "pitch": "stable|slightly-up|slightly-down",
  "rate": 1.0,
  "style_instruction": "受固定声学基线约束的句内语义提示",
  "intentional_emphasis": false
}
```

顶层必须包含 `acoustic_baseline`，锁定 `register`、`vocal_effort`、`breath_pressure`、`microphone_distance`、`timbre_brightness` 和 `global_energy`。这些字段贯穿整集，不随句型或场景切换。

句子可以保留受控的自然语气：问题可为 `curious + slightly-up`，结论和 CTA 可为 `warm + slightly-down`，提醒可为 `warning`；`emotion_strength` 只能是 1–2，`rate` 只能在 0.98–1.02。它们表达语义意图，不授权提高音量、改变气息力度、切换音区、耳语或喊读。`strong` 必须显式记录 `intentional_emphasis=true`，并在声音稳定 QC 中作为人工审核例外，不能成为自动放宽整段阈值的理由。

VoxCPM2 默认把整集正文作为一个连续 acoustic take，只使用一条整集风格指令；句内变化主要由文本、标点和已审核的停顿驱动。若模型需要分块，块之间仍共享同一个黄金 prompt、声学基线和候选连续性评分。

脚本生成的是可复现的保守初稿。模型必须根据上下文检查分类：不能只看标点，也不能把每个句子都标成警告或强调。发现分类不合理时，先改 `prosody.json`，再标记为 `approved`。
