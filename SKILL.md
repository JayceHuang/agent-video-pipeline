---
name: agent-video-pipeline
description: Orchestrate a configurable, debuggable local pipeline from approved narration packages to timed audio, captions, visual assets, semantic motion, rendered video, optional avatar compositing, publishing assets, and QC. Use for building, batch-producing, resuming, rerendering, or debugging explainer-video projects. Keep author identity, voice assets, CTA, illustration provider, canvas, avatar layout, brand, platform copy, delivery paths, and machine runtime in external profiles; use the separate adapt-longform-for-speech and compose-avatar-video Skills for those independent capabilities.
---

# 通用 Agent 视频流水线

编排可复现、可调试、可增量重建的本地讲解视频。Skill 只保存通用流程、契约、Schema、provider adapter 和 QC；任何个人风格或本机路径必须通过外部配置注入。

## 先冻结配置

每个项目在高成本操作前必须生成唯一的 resolved profile：

```bash
"<pipeline_runtime.python>" scripts/resolve_profile.py \
  --profile <personal-profile.yaml> \
  --runtime <runtime.local.yaml> \
  --project-config <optional-project.yaml> \
  --project <project-dir>
```

首次运行从外部 `runtime.local.yaml` 读取 `pipeline_runtime.python`；后续所有通用 Python 控制脚本都使用 resolved profile 中的该解释器。未配置本机 runtime 时，使用已经安装本 Skill `requirements.txt` 的 Python。不要静默退回缺少依赖的系统 `python3`。不需要个性化时省略个人 Profile，只使用 `references/default-profile.yaml`。下游脚本优先读取 `<project>/.pipeline/resolved-profile.json`，并把 Profile SHA 写入缓存键和报告。配置规则见 [references/profile-contract.md](references/profile-contract.md)。

## 不可变质量规则

- 上游产物缺失、未批准、QC 失败或 SHA 过期时停止下游。
- 音频改变后重新强制对齐字幕和语义 cue。
- 数字人使用同一份获批母带，不重新朗读。
- 已有本地资产默认复用，除非用户明确要求替换。
- 最终交付必须包含视频、封面、描述、发布文案、manifest、阶段 QC 与耗时记录。
- Profile 可以改变风格与规格，不能关闭哈希新鲜度、音画同步、边界安全和交付完整性检查。

## 阶段

### 1. 内容包

长文改写调用独立的 `adapt-longform-for-speech` Skill。已批准稿件可直接标准化。流水线 adapter 再把通用 `spoken-script.json` 转换为项目的 `series.json` 与 `scenes.json`。

```bash
"<pipeline_runtime.python>" scripts/import_spoken_script.py \
  --script <spoken-script.json> \
  --script-qc <script-qc.json> \
  --profile <resolved-profile.json> \
  --series-output <series.json> \
  --projects-root <episode-projects-dir>
```

```bash
"<pipeline_runtime.python>" scripts/validate_episode_independence.py \
  --series <series.json> \
  --profile <resolved-profile.json>
```

### 2. Prosody、声音与字幕

先生成并批准 `audio/prosody.json`，再调用 Profile 选择的声音 provider。随包的 VoxCPM2 脚本是可选 adapter，不是个人默认值；模型、对齐环境、声音原件和 prompt 由 resolved profile 或 CLI 提供。

本地声音 provider 必须使用 resolved profile 的 `tts_runtime.generator_python` 启动，不能沿用环境中的裸 `python3`。随包 adapter 的调用方式如下；解释器、模型与对齐器路径都来自同一份 frozen profile：

```bash
"<pipeline_runtime.python>" scripts/analyze_prosody.py \
  --scenes <scenes.json> --profile <resolved-profile.json> \
  --output <audio/prosody.json>
"<pipeline_runtime.python>" scripts/approve_if_clean.py --file <audio/prosody.json> --kind prosody

"<tts_runtime.generator_python>" scripts/generate_all_voxcpm2.py \
  --mode episode-take --series <series.json> --project <project-dir> \
  --profile <resolved-profile.json> --episode <episode-number>

"<pipeline_runtime.python>" scripts/run_gates.py --project <project-dir> --stage audio \
  --profile <resolved-profile.json>
```

`audio/output/narration_master.wav` 是字幕、动画、数字人和成片时长的唯一音频时间基准。

### 3. 视觉资产

是否调用插图 Skill、provider、资产数量和跳过策略全部读取 Profile。所有项目先初始化明确的 `visual-assets.json` 决策；启用 provider 时再由 provider 填充并通过验证。

```bash
"<pipeline_runtime.python>" scripts/init_visual_assets.py \
  --project <project-dir> --profile <resolved-profile.json>

"<pipeline_runtime.python>" scripts/validate_visual_assets.py \
  --project <project-dir> --profile <resolved-profile.json>
```

Profile 启用 provider 时，先让 provider 把初始化 manifest 更新为 `complete` / `reused`；
允许跳过的可选 provider 更新为 `skipped`。初始化器默认不覆盖已有决策。

### 4. 语义动效与布局

```bash
"<pipeline_runtime.python>" scripts/plan_semantic_motion.py \
  --scenes <scenes.json> --timeline <audio/timeline.json> \
  --prosody <audio/prosody.json> --caption-words <audio/caption-words.json> \
  --visual-assets <visual-assets.json> --resolved-profile <resolved-profile.json> \
  --output <.hyperframes/semantic-motion.json>

"<pipeline_runtime.python>" scripts/init_layout_boxes.py \
  --motion-plan <.hyperframes/semantic-motion.json> \
  --profile <resolved-profile.json> \
  --output <.hyperframes/layout-boxes.json>

"<pipeline_runtime.python>" scripts/run_gates.py --project <project-dir> --stage motion \
  --profile <resolved-profile.json>
```

布局与动效必须确定性、seek-safe，并以 Profile 的画布、protected zones、motion preset 和 provider 决策为准。

### 5. 渲染、可选数字人与交付

先生成基础视频。需要数字人时调用独立的 `compose-avatar-video` Skill，获得 composited video 后再进入最终包装；不需要数字人时直接使用基础视频。

```bash
"<pipeline_runtime.python>" scripts/finalize_video_assets.py \
  --video <selected-video.mp4> \
  --profile <resolved-profile.json> \
  --title "..." --summary "..."

"<pipeline_runtime.python>" scripts/run_gates.py --project <project-dir> --stage final \
  --profile <resolved-profile.json>
```

外部交付目录未配置时只保留项目内交付包，不假设任何个人云盘或 NAS 路径。

## 调试

先运行 `scripts/run_gates.py --stage all`，只修失败层。缓存、耗时、门禁顺序与产物契约见 [references/workflow-contract.md](references/workflow-contract.md) 和 [references/debug-checklist.md](references/debug-checklist.md)。

## 资源

- Profile：[references/profile-contract.md](references/profile-contract.md)、[references/profile-schema.json](references/profile-schema.json)
- 通用默认值：[references/default-profile.yaml](references/default-profile.yaml)
- 工作流：[references/workflow-contract.md](references/workflow-contract.md)
- 声音：[references/voice-stability.md](references/voice-stability.md)、[references/voice-stability-profile.json](references/voice-stability-profile.json)
- 动效与布局：[references/motion-system.md](references/motion-system.md)、[references/motion-catalog.json](references/motion-catalog.json)、[references/layout-box-schema.md](references/layout-box-schema.md)
- Prosody：[references/prosody-schema.md](references/prosody-schema.md)
