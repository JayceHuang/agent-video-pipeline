---
name: agent-video-pipeline
description: Turn a Chinese long-form article into a debuggable Mac-local tutorial video pipeline with chapter scripts, prosody-aware narration, profile-driven semantic motion planning, HyperFrames animation, subtitles, optional pre-rendered avatar/lip-sync compositing, and co-located publishing assets and QC reports. Use for requests to build, batch-produce, rerender, or debug the Agent/faceless explainer workflow on a Mac.
---

# Agent 视频流水线

可复现、可调试、Mac 本地单机的中文教程视频工作流。本文件只是流程入口：**执行每个阶段前，必须先读该阶段指向的 reference 文件**；所有数值阈值只存在于 `references/default-profile.yaml` 与 `references/voice-stability-profile.json`，本文件与散文规则中不出现数字，冲突时以这两个文件为准。

细则总纲：[references/workflow-contract.md](references/workflow-contract.md)（每阶段必读对应章节）。

## 不可变的输出契约

最终视频目录必须包含 `final.mp4`、`cover.png`、`cover.jpg`、`cover-description.md`、`publishing-copy.md`、`asset-manifest.json`、`qc-report.json`、`visual-assets.json`、`semantic-motion.json`、`motion-qc.json`、`layout-boxes.json`、`layout-qc.json`、`alignment-qc.json`、`pipeline-timings.json`。只交 MP4 视为未完成；封面或文案失败时任务标记 `incomplete`。

通过 QC 后必须按契约复制日期交付副本（含从交付版 `final.mp4` 抽取的 `audio/final-audio.wav|.mp3`）到外部媒体目录；目录命名、必备文件与音频校验规则见 workflow-contract《外部交付目录契约》。

## 阶段流程

每阶段一个门禁产物；门禁不是 `pass`、SHA 过期或未批准时，下游一律视为未完成。门禁统一用 `scripts/run_gates.py --project <dir> --stage <audio|motion|final|all>` 执行，不逐个手敲。

### 0. 快速生产预检（强制，先于一切高成本调用）

```bash
python scripts/plan_fast_production.py --project <dir> --target-cpm <cpm> [--duration-target-s <s>]
```

先锁定字数、目标语速与预计时长，逐项审计插图/音频/动效/布局/渲染缓存，只重建真实下游。禁止先按低语速生成、发现太慢再改目标重跑。插图与 TTS 相互独立可并行准备，TTS 本身串行。预检估算超出 profile 时间预算时，先输出慢项和原因再开工。必读：workflow-contract《快速生产与增量重建》。

### 1. 文章和剧本

按内容结构自动分集；每集独立主题、独立标题/摘要/结论，禁止一切跨集引用或预告；结尾收束后只保留固定 CTA。完成后：

```bash
python scripts/validate_episode_independence.py --series <series.json>
```

必读：workflow-contract《分集与合集》。

### 1.5 语气层（强制，先于 TTS）

```bash
python scripts/analyze_prosody.py --scenes <scenes.json> --output <audio/prosody.json>
python scripts/approve_if_clean.py --file <audio/prosody.json> --kind prosody
python scripts/validate_prosody.py --prosody <audio/prosody.json> --require-approved
```

`prosody.json` 是剧本与声音模型之间唯一语气来源；控制标签不进入朗读文本。必读：workflow-contract《语气层》、[references/prosody-schema.md](references/prosody-schema.md)。

### 2. 声音（VoxCPM2，仅此一个模型）

```bash
python scripts/prepare_voxcpm2_prompt.py
<voxcpm-python> scripts/generate_all_voxcpm2.py --mode episode-take \
  --series <series.json> --project <dir> --episode <n> \
  --target-cpm <cpm> --candidate-count 3   # 上限，非固定批量
<aligner-python> scripts/align_all_captions.py \
  --series <series.json> --project <dir> --episode <n> \
  --source-master --timings <dir>/pipeline-timings.json
python scripts/run_gates.py --project <dir> --stage audio
```

生成与对齐入口已通用化并随 skill 分发：`--series` 必填；不传 `--project` 时 series.json 必须声明 `project_dir_template`（相对 series.json 解析，如 `"../<slug>-ep{ep}"`）。模型与对齐 venv 默认路径读自 profile `tts_runtime`，CLI 可覆盖。`<voxcpm-python>` 是装有 voxcpm 的解释器；`<aligner-python>` 必须是 venv launcher 原始路径。

关键不变式：整集一次连续 take（含 CTA）；候选自适应顺序生成，先 1 个、失败才扩展；仅改目标 CPM 时复用 raw WAV 只重评分/对齐；一次全局 retime；后处理只修 gain 且 `stabilize_aligned_continuous.py` 最多运行一次；任何重生成/retime 后必须重新 forced-align。批量用 `scripts/run_bounded_jobs.py --kind tts`（并发 1）。必读：[references/voice-stability.md](references/voice-stability.md)、workflow-contract《声音与对口型》。

### 2.5 小木插图（每集强制，可与 TTS 并行）

调用 `ian-xiaomu-illustrations` Skill 生成/复用 shot list 与配图，写入 `visual-assets.json`，然后：

```bash
python scripts/validate_visual_assets.py --project <dir>
```

已有图片只复用不重生成；整集不可跳过。必读：workflow-contract《视频规格》插图各条。

### 3. 语义动效与布局

```bash
python scripts/plan_semantic_motion.py --scenes <scenes.json> --timeline <audio/timeline.json> \
  --prosody <audio/prosody.json> --caption-words <audio/caption-words.json> \
  --visual-assets <visual-assets.json> --profile basic-stable \
  --output <.hyperframes/semantic-motion.json>
python scripts/approve_if_clean.py --file <.hyperframes/semantic-motion.json> --kind motion
python scripts/init_layout_boxes.py --motion-plan <.hyperframes/semantic-motion.json> --output <.hyperframes/layout-boxes.json>
# Storyboard/DOM 完成后：在预览页加载 scripts/extract_layout_boxes.js 自动导出实际 swept bbox，替换模板
python scripts/approve_if_clean.py --file <.hyperframes/layout-boxes.json> --kind layout
python scripts/run_gates.py --project <dir> --stage motion
```

关键不变式：布局与动效由 approved plan 驱动，确定性、seek-safe；`basic-stable` 只用白名单 primitive 并在构建期裁掉未选中的高级 DOM；关键主体不遮挡；beat 必须在 DOM 与 paused timeline 中真实落地。HyperFrames 完整 `check --snapshots` 只在机器门禁全过后跑一次，失败做定向修复，不循环盲测。必读：[references/motion-system.md](references/motion-system.md)、[references/layout-box-schema.md](references/layout-box-schema.md)、workflow-contract《语义动效契约》。

### 4. 可选数字人合成

已有口型视频先按 workflow-contract《声音与对口型》校验时长/帧率/音频哈希，再做圆形 mask 叠加；无数字人时保留左下圆形净空（几何参数见 profile）。

### 5. 渲染与成片资产

```bash
python scripts/finalize_video_assets.py --video <final.mp4> --title "..." --summary "..."
python scripts/run_gates.py --project <dir> --stage final
```

已有封面默认复用（`--force-cover` 才替换）；三平台文案分别生成。批量渲染用 `scripts/run_bounded_jobs.py --kind render`（并发上限见 profile）。必读：workflow-contract《成片目录契约》《发布文案契约》《封面/图片规则》。

### 6. 日期交付与耗时收尾

复制交付副本、抽取交付音频（`scripts/extract_delivery_audio.py`），最后：

```bash
python scripts/finalize_pipeline_timings.py --timings <dir>/pipeline-timings.json
```

未关闭 timing trace 不得把耗时报告称为最终统计；汇报必须区分 wall-clock 与累计 compute。必读：workflow-contract《外部交付目录契约》。

耗时事件不要手写 JSON：高成本命令一律用 `scripts/record_timing.py --timings <dir>/pipeline-timings.json --stage <name> -- <命令>` 包装执行，缓存命中用 `--status cache_hit` 记录；历史事件同时是下次预检估算的校准数据。

## 调试

按 [references/debug-checklist.md](references/debug-checklist.md) 的顺序定位，先跑 `run_gates.py --stage all`，只修失败层，不整条重跑。

## 资源

- 细则总纲：[references/workflow-contract.md](references/workflow-contract.md)
- 数值配置唯一来源：[references/default-profile.yaml](references/default-profile.yaml)
- 声音阈值唯一来源：[references/voice-stability-profile.json](references/voice-stability-profile.json)
- 语气字段：[references/prosody-schema.md](references/prosody-schema.md)
- 声音契约：[references/voice-stability.md](references/voice-stability.md)
- 动效系统与目录：[references/motion-system.md](references/motion-system.md)、[references/motion-catalog.json](references/motion-catalog.json)
- 布局盒契约：[references/layout-box-schema.md](references/layout-box-schema.md)
- 耗时 schema：[references/pipeline-timings-schema.json](references/pipeline-timings-schema.json)
- 调试顺序：[references/debug-checklist.md](references/debug-checklist.md)
