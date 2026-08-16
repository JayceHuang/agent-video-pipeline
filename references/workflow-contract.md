# 通用 Agent 视频工作流契约

## 配置与输入

每个项目必须先用 `scripts/resolve_profile.py` 把中性默认值、必需的外部工作区 Profile、本机 runtime 和项目覆盖冻结为 `.pipeline/resolved-profile.json`。流水线只读取冻结文件；个人身份、声音资产、CTA、视觉 provider、画布、数字人位置和交付路径不得写回 Skill。初始化生成的 `profiles/workspace.yaml` 必须保持标准化和中性，个性化内容只有在用户明确选择时才添加。

输入可以是：

- `adapt-longform-for-speech` 生成的通用稿件包；
- 已批准的口播稿；
- 已有 `series.json` / `scenes.json`；
- 可选视觉资产决策；
- 可选已生成的数字人轨道。

## 阶段边界

```text
spoken-script.json
  -> import_spoken_script.py -> series.json / scenes.json
  -> audio/prosody.json
  -> narration_master.wav + timeline + captions
  -> visual-assets.json
  -> semantic-motion.json
  -> layout-boxes.json + composition bindings
  -> base video
  -> optional avatar-composited video
  -> final publishing package
```

每阶段产物必须记录输入 SHA、resolved Profile SHA 和批准状态。任一输入改变后，只让真实下游失效。

## 内容与分集

- 通用稿件包先通过自身 QC，再把未过期的 `script-qc.json` 一并交给 `import_spoken_script.py`；导入产物只记录稿件、QC 和 Profile 的路径、ID 与 SHA，不复制声音或本机 runtime 字段。
- 是否分集、每集时长、跨集引用政策与 CTA 全部读取 Profile 或项目输入。
- 声明为独立分集时，每集必须有标题、摘要、完整论证和结论。
- CTA 为空时不得擅自追加；CTA 非空时只能追加配置中的原文。
- 内容稿与 TTS 控制标签分离。

## Prosody 与声音

- `prosody.json` 是稿件与声音 provider 之间的通用语气层。
- 整集共享一套 acoustic baseline；句内只允许 Profile 声明的微语气变化。
- provider adapter 必须从 resolved Profile 或 CLI 获取模型、运行时、授权声音与 prompt，不得包含个人默认路径。
- `audio/output/narration_master.wav` 是字幕、动效、数字人和最终时长的唯一音频基准。
- 声音或 retime 改变后必须重新 forced-align，不得平移旧字幕。
- 候选、全局 retime、增益稳定和最终混音阈值读取 Profile/声音 QC Profile。

## 视觉资产

- `layout.illustration_skill` 决定 provider、是否强制、资产数量与逐场要求。
- 所有项目先用 `init_visual_assets.py` 写出绑定 Profile SHA 的决策 manifest；未启用视觉 provider 时必须为 `status=disabled` 且不含旧 shots/assets。
- 已有本地资产默认复用；替换必须由用户明确要求。
- 每个资产记录 provider、项目内路径、当前 SHA、shot/scene 绑定与复用状态。

## 语义动效与布局

- 动效由 approved `semantic-motion.json` 驱动，必须 deterministic、seek-safe。
- cue 优先绑定 forced-aligned word；已有逐词时间时禁止建立第二套比例时间轴。
- 画布、FPS、标题区、字幕区、插图区、内容区和可选数字人安全区读取 resolved Profile。
- 动画元素验证完整 `swept_bbox`，不能只检查静止帧。
- protected elements 在相同时段不得相交，除非显式共享合法的 composite id。
- motion plan、layout、DOM bindings 和 QC 都必须绑定同一个 Profile SHA。

## 数字人

- 数字人是可选分支，调用独立的 `compose-avatar-video` Skill。
- 数字人轨道必须对应获批母带；合成默认使用 master audio，丢弃 provider 返回音轨。
- 位置、尺寸、遮罩、绿幕、画幅和音频策略通过 Profile/CLI 注入。
- 无数字人时是否保留安全区由 Profile 决定，不生成调试占位图层。

## 增量重建与耗时

- 高成本阶段前运行 `plan_fast_production.py`。
- 缓存键至少包含当前文本、Prosody、母带/字幕、视觉资产、动效、布局、provider revision、resolved Profile SHA 和逻辑版本。
- 高成本命令使用 `record_timing.py` 包装；缓存命中显式记录。
- `pipeline-timings.json` 必须区分 wall-clock 与累计 compute。
- 失败后只修对应层，不盲目整条重跑。

## 最终目录

最终包至少包含：

```text
final.mp4
cover.png
cover.jpg
cover-description.md
publishing-copy.md
asset-manifest.json
qc-report.json
visual-assets.json
semantic-motion.json
motion-qc.json
layout-boxes.json
layout-qc.json
alignment-qc.json
pipeline-timings.json
```

配置了外部交付根目录时复制交付副本；未配置时只保留项目内资产。任何交付音频都从最终 MP4 抽取，避免漏掉最终混音、SFX 或编码延迟。

## 通用质量底线

Profile 不得关闭：

- SHA 新鲜度检查；
- 音画同步与时长检查；
- 字幕覆盖与边界检查；
- protected region 遮挡检查；
- 已有资产防覆盖；
- 最终交付完整性；
- QC 失败时停止交付。
