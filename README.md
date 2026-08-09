# agent-video-pipeline

把中文长文转换成一套可复现、可调试、适合 Mac 本地生产的教程视频工作流。

这个仓库沉淀的是一套强个人风格的视频生产契约：长文拆集、口播脚本、VoxCPM2 声音克隆、Prosody、强制对齐、字幕、语义动效、HyperFrames、可选数字人、封面、发布文案和质量检查。

> 重要：这不是“一条命令输入文章就自动得到成片”的程序。当前仓库提供工作流规则、配置参考和 28 个随包脚本（27 个 Python、1 个浏览器端 JavaScript），其中已经包含 VoxCPM2 连续生成和强制对齐的通用入口；模型权重、运行时环境、文章拆集、HyperFrames 工程构建/渲染、数字人生成和外部发布仍需要相应环境或项目适配器。

## 目录

- [它解决什么问题](#它解决什么问题)
- [当前包含和不包含什么](#当前包含和不包含什么)
- [使用前总原则](#使用前总原则)
- [整体架构](#整体架构)
- [依赖的 Skills](#依赖的-skills)
- [安装](#安装)
- [下载后必须修改的个人元素](#下载后必须修改的个人元素)
- [第一次使用的正确顺序](#第一次使用的正确顺序)
- [完整运行流程](#完整运行流程)
- [28 个脚本的作用](#28-个脚本的作用)
- [项目文件契约](#项目文件契约)
- [最终交付契约](#最终交付契约)
- [常用命令](#常用命令)
- [隐私、版权与公开仓库注意事项](#隐私版权与公开仓库注意事项)
- [当前限制](#当前限制)

## 它解决什么问题

这套 Skill 主要解决以下问题：

1. 把长文按语义拆成若干独立、可单独观看的视频。
2. 让口播、字幕、动画和数字人共享同一条最终音频时间线。
3. 把声音稳定性、插图调用、语义动效、布局遮挡和音画绑定变成可执行门禁。
4. 保存每一步的输入、输出、审批状态和 SHA-256，方便定位问题与局部重做。
5. 每次交付完整视频包，而不是只留下一个 MP4。

默认生产形态：

- 中文知识/教程类内容。
- Mac 本地单机。
- 单集通常不超过 3 分钟。
- 1920×1080、30 fps、横屏。
- VoxCPM2 声音克隆。
- HyperFrames 语义动画。
- 每集强制使用 4–8 张小木 A 版语义插图。
- 左下角预留圆形数字人区域。

## 当前包含和不包含什么

### 已经包含

- `SKILL.md`：完整工作流入口和不可变规则。
- 10 份参考契约/配置文件。
- 28 个随包脚本：27 个 Python 与 1 个浏览器端布局导出脚本（含 VoxCPM2 生成与强制对齐入口）。
- 高成本阶段前的时长预测、缓存审计与增量重建计划。
- Prosody 初稿与批准门禁。
- VoxCPM2 黄金 prompt 冻结与验证。
- 连续声音稳定化、边界检查和声音状态检查。
- 小木插图资产门禁。
- 语义动效规划和 QC。
- 实际布局盒与 swept bbox 遮挡 QC。
- 音频、字幕、动效、DOM 与插图的绑定 QC。
- 封面、发布文案、manifest、交付音频和成片总检。

### 没有包含

- 自动把任意文章转换成完整 `series.json` / `scenes.json` 的统一程序。
- VoxCPM2/Qwen 强制对齐模型权重与安装环境（生成/对齐入口脚本已包含，需自备 `voxcpm`、`mlx_audio` 和对应 venv）。
- 一键建立和渲染 HyperFrames 工程的总 runner。
- 小木角色的私有参考图和作者声音文件。
- 数字人生成/合成服务。
- 通用的 NAS/云盘复制和平台自动发布程序。
- 对其他 TTS 的自动回退。

如果缺少这些外部能力，本 Skill 仍然可以作为生产 SOP、文件契约和质量验证工具使用，但不能宣称已经完成全自动出片。

## 使用前总原则

1. **每个阶段先读对应 reference。** 先读 `references/workflow-contract.md` 的相关章节，以及该阶段列出的 schema/profile；不要只看本 README 的摘要。
2. **数值只以 profile 为准。** 生产阈值和默认路径的单一真源是 `references/default-profile.yaml`；声音阈值的单一真源是 `references/voice-stability-profile.json`。README 中的数字只是当前作者档示例，改档时以这两个文件和项目 manifest 为准。
3. **统一用绝对 Skill 路径和 `python3`。** 从任意工作目录调用脚本时都写成 `python3 "$SKILL_DIR/scripts/..."`；不要假设当前目录就是 Skill 根目录，也不要用不存在的 `python` 别名。
4. **门禁统一入口。** 阶段验收优先使用 `scripts/run_gates.py`；单个 validator 命令只用于诊断或生成特定报告。任何门禁失败、报告 SHA 过期或审批状态不对，都不能进入下游。
5. **不要把外部环境写进公开仓库。** 声音原件、黄金 prompt、模型权重、人物参考图、渲染缓存、API key 和个人云盘路径都必须留在本机或私有配置中。

## 整体架构

```text
agent-video-pipeline（总规则 / 文件契约 / 门禁）
  ├─ Fast preflight / gate runner
  │    └─ plan_fast_production / run_gates / timing trace
  ├─ 内容适配器
  │    └─ series.json / SCRIPT.md / scenes.json
  ├─ ian-xiaomu-illustrations
  │    └─ visual-assets.json + 4–8 张插图
  ├─ Prosody
  │    └─ prosody.json / prosody-qc.json
  ├─ VoxCPM2 项目适配器
  │    └─ 候选连续 take / voice-manifest.json
  ├─ 声音稳定与强制对齐
  │    └─ narration_master.wav / timeline / captions
  ├─ 语义动效与布局
  │    └─ semantic-motion / layout-boxes / alignment QC
  ├─ HyperFrames / faceless-explainer
  │    └─ compositions / preview / final.mp4
  └─ Finalize / QC
       └─ 封面 / 文案 / manifest / 交付音频 / QC / pipeline-timings
```

### 四类单一真源

| 真源 | 作用 |
| --- | --- |
| `scenes.json` | 机器真正交给 TTS 的文本和场景定义 |
| `audio/output/narration_master.wav` | 字幕、动画、数字人和视频时长的唯一时间基准 |
| `visual-assets.json` | 插图 provider、shot、文件、复用状态和哈希 |
| `.hyperframes/semantic-motion.json` | 语义 cue、DOM selector、动画动作和转场路由 |

## 依赖的 Skills

| Skill | 作用 |
| --- | --- |
| `agent-video-pipeline` | 总入口、个人风格、文件契约、执行顺序、停止条件、QC 和交付 |
| `hyperframes` | HyperFrames 意图入口、brief、路由和审批状态 |
| `faceless-explainer` | 把文章/主题转换成不依赖真人素材的讲解视频 |
| `hyperframes-core` | HTML composition、`data-*`、tracks、paused timeline 和确定性渲染契约 |
| `hyperframes-creative` | 品牌、色彩、字体、构图、叙事和非动画视觉方向 |
| `hyperframes-animation` | 原子动效、scene blueprint、转场和 seek-safe 动画规则 |
| `hyperframes-cli` | `init`、`lint`、`check`、`snapshot`、`preview`、`render` 和诊断 |
| `media-use` | 首帧 SFX、BGM、图像、图标、Logo 等媒体的解析、冻结和复用 |
| `ian-xiaomu-illustrations` | 小木 A 版正文插图；当前版本每集强制调用 |

默认文章路线是 `hyperframes → faceless-explainer`。`general-video` 只在没有更具体工作流时作为通用 HyperFrames 入口，不是本流水线的默认依赖。

## 安装

### 1. 克隆到项目级 Skill 目录

```bash
mkdir -p /你的项目/.agents/skills
git clone https://github.com/JayceHuang/agent-video-pipeline.git \
  /你的项目/.agents/skills/agent-video-pipeline
```

或者安装到 Codex 用户级目录：

```bash
mkdir -p ~/.codex/skills
git clone https://github.com/JayceHuang/agent-video-pipeline.git \
  ~/.codex/skills/agent-video-pipeline
```

安装后重新加载 Agent，并确认它能识别 `agent-video-pipeline`。

### 2. 基础环境

建议：

- macOS。
- Python 3.10 或更高。
- FFmpeg / ffprobe。
- Node.js 22 或更高。
- HyperFrames CLI。

```bash
python3 --version
ffmpeg -version
ffprobe -version
node --version
npx hyperframes --help
```

安装 Python 依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

当前主要第三方 Python 依赖是：

- `numpy`
- `soundfile`

音视频脚本还依赖系统中的 `ffmpeg` 和 `ffprobe`。

VoxCPM2 生成和 Qwen 强制对齐不是 `requirements.txt` 的最小依赖：请在自己的专用环境中安装 `voxcpm`、`mlx_audio` 及模型权重，并把解释器路径写入 `references/default-profile.yaml` 的 `tts_runtime`，或在命令行显式传入 `--model` / `--aligner-python`。不要把这两个大环境塞进 Skill 的基础 venv。

### 3. 检查脚本入口

不要依赖当前工作目录，建议始终使用绝对 `SKILL_DIR`：

```bash
SKILL_DIR=/你的绝对路径/agent-video-pipeline

python3 "$SKILL_DIR/scripts/analyze_prosody.py" --help
python3 "$SKILL_DIR/scripts/prepare_voxcpm2_prompt.py" --help
python3 "$SKILL_DIR/scripts/plan_semantic_motion.py" --help
python3 "$SKILL_DIR/scripts/validate_video_output.py" --help
python3 "$SKILL_DIR/scripts/run_gates.py" --help
python3 "$SKILL_DIR/scripts/plan_fast_production.py" --help
```

## 下载后必须修改的个人元素

这个仓库不是中性模板。它保留了作者的声音、人物 IP、CTA、版式、动效和交付习惯。第三方不能只改一个 YAML 就开始生产。

### 先执行全局扫描

```bash
SKILL_DIR=/你的绝对路径/agent-video-pipeline

rg -n "/Users/jaycehuang|SynologyDrive|木哥|小木|关注我|ian-xiaomu|1920|1080|x.?42|300" \
  "$SKILL_DIR"
```

### 需要修改的总表

| 类别 | 当前作者默认 | 需要修改的位置 |
| --- | --- | --- |
| 声音原件 | 木哥原始 MP3 的绝对路径 | `SKILL.md`、`default-profile.yaml`、`workflow-contract.md`、`voice-stability-profile.json`、`prepare_voxcpm2_prompt.py` |
| TTS/对齐运行时 | 作者本机的 VoxCPM2 模型缓存和 `.venv-audio` | `default-profile.yaml` 的 `tts_runtime`，或每次命令的 `--model` / `--aligner-python` |
| 项目目录解析 | 私有项目目录结构 | 每个 `series.json` 的 `project_dir_template`，或显式传 `--project`；模板按 series 文件相对路径解析 |
| 黄金 prompt | 310.95–319.15 秒、8.2 秒、作者固定句子 | 同上，加上自己的 `voice-manifest.json` 和 VoxCPM2 适配器 |
| 插图人物 | `ian-xiaomu-illustrations`、小木 A 版、深蓝 T 恤等规则 | 插图 Skill、身份参考图、`visual-assets.json`、`validate_visual_assets.py`、视觉模板 |
| CTA | `关注我，给你带来更多AI知识。` | `plan_semantic_motion.py`、`validate_episode_independence.py`、Prosody 关键词、字幕、时间线、CTA 动画 |
| 画布 | 1920×1080、30 fps | motion/layout/video validators、finalizer、HyperFrames composition |
| 数字人安全区 | 左 42px、底 28px、直径 300px | `plan_semantic_motion.py`、`init_layout_boxes.py`、`validate_layout_boxes.py`、`validate_semantic_motion.py`、finalizer |
| 动效 | `basic-stable`，每场一个 hero、一个 supporting，只用已实现的基础动画；默认构建裁掉未选中的高级 DOM | `motion-system.md`、`motion-catalog.json`、default profile、HyperFrames rules/blueprints |
| 正文布局 | 语义驱动的确定性动态布局；每集至少 3 种、相邻不重复、人物默认不居中 | motion catalog、planner、motion/layout/alignment validators、HyperFrames composition |
| 转场 | push-slide / crossfade | motion catalog、planner、HyperFrames 工程 |
| 首帧媒体 | t=0 的克制 SFX，来自 `media-use` | default profile、媒体 manifest、composition |
| 品牌 | 作者配色、字体、封面和三平台口吻 | `frame.md` / design spec、creative preset、finalizer 输入或模板 |
| 交付路径 | 作者 SynologyDrive 绝对路径 | `SKILL.md`、`workflow-contract.md`、自己的复制/发布适配器 |
| 时区/命名 | Asia/Shanghai、`MMDD-<slug>` | workflow contract、交付脚本或项目编排器 |

### A. 更换声音

必须使用本人或已经获得明确授权的声音。

当前作者档使用：

- 原始 MP3：作者私有绝对路径。
- 黄金句区间：310.95–319.15 秒。
- 时长：8.2 秒。
- 模式：VoxCPM2 ultimate cloning。
- 同一 WAV 同时作为 `prompt_wav_path` 和 `reference_wav_path`。
- 必须传入与音频逐字一致的 `prompt_text`。

第三方应重新选择自己的 6–15 秒完整句：

- 单人讲话。
- 无背景音乐。
- 麦克风距离稳定。
- 情绪和声学状态自然。
- 句子完整结束，不截断半句话。

```bash
python3 "$SKILL_DIR/scripts/prepare_voxcpm2_prompt.py" \
  --source /绝对路径/authorized-source.mp3 \
  --output /绝对路径/my-golden-prompt.wav \
  --start 123.45 \
  --duration 8.20 \
  --prompt-text "与音频逐字一致的完整句子"

python3 "$SKILL_DIR/scripts/prepare_voxcpm2_prompt.py" \
  --output /绝对路径/my-golden-prompt.wav \
  --verify
```

同步修改：

1. `references/default-profile.yaml`
2. `references/voice-stability-profile.json`
3. `references/workflow-contract.md`
4. `SKILL.md`
5. `scripts/prepare_voxcpm2_prompt.py` 的默认值
6. 项目级 VoxCPM2 推理适配器
7. `audio/voice-manifest.json`

现在随包的 `generate_all_voxcpm2.py`、`generate_voxcpm2_continuous.py` 和 `align_all_captions.py` 会读取 profile 的 `tts_runtime` 默认值。每个项目的 `series.json` 还应声明自己的 `target_effective_chars_per_minute`、`ending_cta`、声音 manifest 字段；若不在命令行传 `--project`，必须增加相对 `series.json` 解析的 `project_dir_template`，例如：

```json
{
  "project_dir_template": "../<slug>-ep{ep}",
  "target_effective_chars_per_minute": 295,
  "ending_cta": "你的固定 CTA。"
}
```

不能把示例中的 `<slug>` 原样当作目录名；应替换成项目实际 slug，并确认每一集解析出的目录唯一、可写。

不要把作者的声音文件、prompt WAV、人物声音或模型缓存提交到公开仓库。

### B. 更换人物 IP 或插图 Skill

当前 validator 强制：

- `provider=ian-xiaomu-illustrations`
- `invocation_required=true`
- `skill_invoked=true`
- 每集 4–8 个 shot
- 每个 shot 有对应文件和哈希

如果保留小木视觉，只需要安装同名 Skill，并准备作者有权使用的角色参考资产。

如果换成自己的角色，必须同时修改：

1. 插图 Skill 名称和 provider。
2. 角色身份参考图、人物比例、服装、配色和动作规则。
3. prompt 模板和 QA 清单。
4. `visual-assets.json` 契约。
5. `scripts/validate_visual_assets.py`。
6. `references/default-profile.yaml`。
7. `references/workflow-contract.md`。
8. HyperFrames 的 safe box、布局和视觉模板。

只替换图片、但不修改 validator 和 manifest，会导致流水线必然失败。

### C. 更换 CTA

当前固定 CTA：

```text
关注我，给你带来更多AI知识。
```

它不只存在于文档里，还进入：

- 分集独立性校验。
- Prosody CTA 判断。
- VoxCPM2 主音频。
- 字幕和时间线。
- `plan_semantic_motion.py`。
- 结尾关注动画。
- 最终人工检查。

更换 CTA 时，需要同步搜索和修改所有位置：

```bash
rg -n "关注我|更多AI知识|CTA_TEXT|DEFAULT_CTA|final_cta" "$SKILL_DIR"
```

### D. 更换画幅、分辨率或数字人位置

当前代码和 validator 多处固定：

- 1920×1080。
- 30 fps。
- 左下头像圆：`x=42`、`bottom=28`、`diameter=300`。

竖屏、方形、无安全区或其他数字人位置属于结构性 fork，至少需要修改：

- `scripts/plan_semantic_motion.py`
- `scripts/init_layout_boxes.py`
- `scripts/validate_layout_boxes.py`
- `scripts/validate_semantic_motion.py`
- `scripts/validate_video_output.py`
- `scripts/finalize_video_assets.py`
- `references/layout-box-schema.md`
- `references/motion-system.md`
- `references/default-profile.yaml`
- HyperFrames composition 和字幕布局

### E. 更换品牌和动效风格

品牌视觉通常放在 HyperFrames 项目的 `frame.md` / design spec 中，包括：

- Logo。
- 品牌色。
- 字体。
- 卡片圆角与阴影。
- 背景纹理。
- 封面形式。
- 三个平台的文案口吻。

动效有四个 profile；默认只用基础稳定档：

| Profile | 用途 |
| --- | --- |
| `clean` | 信息密集、低成本、克制预览 |
| `basic-stable` | 当前默认，每场一个 hero + 1 个 supporting，只使用已实现的基础动画 |
| `premium-balanced` | 仅显式要求且 runtime implementation audit 通过后使用 |
| `cinematic` | 用户明确要求更强视觉冲击时使用 |

默认转场不再是全片纸张翻页：

- 相邻论点：`push-slide`
- 标题、结论和章节边界：`crossfade`
- `zoom-through`、`blur-crossfade`、`paper-flip-soft` 仅属于通过实现审计后的高级档

### F. 更换交付目录

作者默认路径是私有 SynologyDrive 目录。第三方必须改成自己的绝对路径。

当前仓库没有通用外部目录复制 runner。你需要在项目适配器中实现：

1. 创建日期交付目录。
2. 复制视频、封面、发布文案、manifest 和所有 QC。
3. 根据 `visual-assets.json` 复制实际插图文件。
4. 保留项目 `renders/` 中的原始文件，不移动、不删除。
5. 从交付版 `final.mp4` 提取交付 WAV/MP3。

## 第一次使用的正确顺序

第一次不要直接处理几十分钟长文。先选一集 60–90 秒、2–4 个场景的独立短稿。

0. 先读 `SKILL.md`、`references/workflow-contract.md` 和 profile；跑一次 `plan_fast_production.py`，确认目标 CPM、缓存和预计时长。
1. 确认声音、人物、品牌、CTA、画布、安全区和交付路径已经去个人化。
2. 生成 `series.json`、`SCRIPT.md` 和 `scenes.json`，并声明 `project_dir_template` 或每集显式项目目录。
3. 运行分集独立性校验。
4. 调用插图 Skill，完成 4–8 个 shot 和图片。
5. 生成 `prosody.json`；无风险草稿可用 `approve_if_clean.py`，有风险标记时人工审批。
6. 冻结并试听自己的黄金 prompt。
7. 使用整集连续 take 从 1 个候选开始，失败才扩展，必要时最多 5 个。
8. 只做一次整集全局 retime，范围以 profile 为准；不要按视觉 scene 反复拉伸。
9. 用 `run_gates.py --stage audio` 通过声音稳定性、边界、节奏和 Prosody 门禁。
10. 以最终 master 重新强制对齐字幕；任何重生成或 retime 后都必须重新对齐。
11. 生成、审核和批准 semantic motion；默认使用 `basic-stable`。
12. 完成实际 DOM 后用 `extract_layout_boxes.js` 导出 swept bbox，审核并批准 layout boxes。
13. 用 `run_gates.py --stage motion` 通过视觉、动效、布局和音画绑定 QC。
14. HyperFrames 执行 lint、check、snapshot、preview；机器门禁全过后完整 snapshot check 只跑一次。
15. 用户批准 preview 后才 render。
16. 生成封面、发布文案、manifest、`pipeline-timings.json` 和最终 QC，用 `run_gates.py --stage final` 验收。
17. 从交付版 MP4 提取 WAV/MP3，关闭 timing trace，并复制完整日期交付包。

推荐首次调用：

```text
使用 $agent-video-pipeline 处理“/绝对路径/article.md”。

只做第一集 60～90 秒。先读对应 reference，审计并替换作者的声音、人物、CTA、
品牌、画布、安全区和交付路径；先跑快速预检。使用我已授权的黄金 prompt；声音从
1 个连续 take 候选开始，失败才增加，只做一次全局 retime。用统一 gate runner
验收。在分集、插图、Prosody、候选声音、master、semantic-motion、layout-boxes、
HyperFrames preview 和最终交付处等待确认。缺少外部能力时明确报告，不要杜撰一键出片能力。
```

## 完整运行流程

### 阶段 0：快速生产预检

高成本阶段前先固定目标语速、预计时长和缓存策略：

```bash
python3 "$SKILL_DIR/scripts/plan_fast_production.py" \
  --project /项目绝对路径 \
  --target-cpm 295 \
  --output /项目绝对路径/.pipeline/fast-production-plan.json
```

这一步是只读审计（只写计划 JSON）：会区分全量 TTS、复用 raw candidate 后重评分/retime/重对齐，以及插图/音频/动效/布局/渲染的真实下游。预估超过 profile 时间预算时，先处理慢项再生成；不要先用低 CPM 生成后再整段重跑。

### 阶段 A：内容规划

- 按语义拆集，而不是固定字数。
- 每集有独立标题、摘要、论证和结论。
- 禁止“上一集、下一集、敬请期待”等跨集引用。
- 结尾先收束本集，再进入 CTA。

```bash
python3 "$SKILL_DIR/scripts/validate_episode_independence.py" \
  --series /项目绝对路径/series.json
```

`series.json` 至少要能让生成/对齐脚本找到每集项目目录：要么每次传 `--project`，要么声明相对 series 文件解析的 `project_dir_template`。

### 阶段 B：视觉资产与 Prosody

先完成 4–8 个插图 shot，再生成 Prosody：

```bash
python3 "$SKILL_DIR/scripts/validate_visual_assets.py" \
  --project /项目绝对路径

python3 "$SKILL_DIR/scripts/analyze_prosody.py" \
  --scenes /项目/scenes.json \
  --output /项目/audio/prosody.json

python3 "$SKILL_DIR/scripts/approve_if_clean.py" \
  --file /项目/audio/prosody.json --kind prosody

# 有低置信度/显式复核标记时，人工审核后再将顶层 status 改为 approved。

python3 "$SKILL_DIR/scripts/validate_prosody.py" \
  --prosody /项目/audio/prosody.json \
  --require-approved \
  --output /项目/audio/prosody-qc.json
```

Prosody 的声音契约是两层：

- 整集锁定音区、发声力度、气息、距离、音色明暗和全局能量。
- 句内允许问题、对比、重点词、结论和 CTA 有小幅自然语调。

不要把每个句子的标签转换成独立 VoxCPM2 声学 prompt，也不要把控制标签读出来。

### 阶段 C：VoxCPM2 连续声音

- 一集默认生成一个完整 acoustic take，包括 CTA。
- 按确定性 seed 自适应顺序生成：先生成 1 个并立即做声学/对齐验收，严格 early-stop 通过即停止；默认上限 3 个，必要时最多 5 个。
- 只有连续 take 反复失败时，才降级为 45–60 秒、完整句边界的长块。
- 默认目标和允许区间读取 `references/default-profile.yaml`；用户明确覆盖目标时必须把 override 写入 series/timeline/manifest。
- 全集只做一次 retime，范围以 profile 为准；不要按视觉 scene 使用相反方向的倍速。
- 禁止用相反方向的逐场景倍速制造节奏跳变。

随 Skill 分发的生成/对齐入口现在可直接编排，但仍需要外部运行时和模型：

```bash
VOXCPM_PY=/绝对路径/voxcpm-venv/bin/python
ALIGN_PY=/绝对路径/mlx-audio-venv/bin/python

# 只生成指定集；--candidate-count 是上限，不是固定批量。
"$VOXCPM_PY" "$SKILL_DIR/scripts/generate_all_voxcpm2.py" \
  --mode episode-take \
  --series /项目绝对路径/series.json \
  --project /项目绝对路径 \
  --episode 1 \
  --target-cpm 295 \
  --candidate-count 3

"$ALIGN_PY" "$SKILL_DIR/scripts/align_all_captions.py" \
  --series /项目绝对路径/series.json \
  --project /项目绝对路径 \
  --episode 1 \
  --source-master \
  --timings /项目绝对路径/pipeline-timings.json

python3 "$SKILL_DIR/scripts/run_gates.py" \
  --project /项目绝对路径 --stage audio
```

如果省略 `--project`，`series.json` 必须包含 `project_dir_template`。模型默认路径和对齐 venv 读取 `references/default-profile.yaml` 的 `tts_runtime`，但生产环境建议显式设置 `--model`、`--aligner-python` 或使用上述绝对解释器。VoxCPM2、Qwen 强制对齐模型和权重不随仓库分发。

### 阶段 D：声音稳定化与对齐

```bash
python3 "$SKILL_DIR/scripts/validate_voice_stability.py" \
  --project /项目绝对路径 \
  --stage master

python3 "$SKILL_DIR/scripts/validate_audio_boundaries.py" \
  --project /项目绝对路径
```

也可以直接运行 `python3 "$SKILL_DIR/scripts/run_gates.py" --project /项目绝对路径 --stage audio`；它会按正确顺序串行执行并把汇总写入 `.pipeline/gates-report.json`。默认失败即停，排查时才使用 `--keep-going`。

两份报告都为 `pass`，且 SHA-256 对应当前 master 后，才能强制对齐并生成：

- `audio/timeline.json`
- `audio/caption-words.json`
- `audio/caption-groups.json`

### 阶段 E：语义动效

```bash
python3 "$SKILL_DIR/scripts/plan_semantic_motion.py" \
  --scenes /项目/scenes.json \
  --timeline /项目/audio/timeline.json \
  --prosody /项目/audio/prosody.json \
  --visual-assets /项目/visual-assets.json \
  --caption-words /项目/audio/caption-words.json \
  --profile basic-stable \
  --output /项目/.hyperframes/semantic-motion.json

python3 "$SKILL_DIR/scripts/approve_if_clean.py" \
  --file /项目/.hyperframes/semantic-motion.json --kind motion

# 有低置信度/显式复核标记时，人工审核后设置 status=approved 和 review.approved_by。

python3 "$SKILL_DIR/scripts/validate_semantic_motion.py" \
  --plan /项目/.hyperframes/semantic-motion.json \
  --scenes /项目/scenes.json \
  --timeline /项目/audio/timeline.json \
  --require-approved \
  --report /项目/.hyperframes/motion-qc.json
```

每个场景恰好一个 hero motion。环境漂移、呼吸、慢 zoom、字幕或 parallax 不能作为语义 beat 唯一的视觉变化。

### 阶段 F：实际布局和音画绑定

```bash
python3 "$SKILL_DIR/scripts/init_layout_boxes.py" \
  --motion-plan /项目/.hyperframes/semantic-motion.json \
  --output /项目/.hyperframes/layout-boxes.json

# 在真实 HyperFrames 预览页加载 $SKILL_DIR/scripts/extract_layout_boxes.js，
# 沿 paused timeline 导出实际 bbox/swept_bbox；具体加载方式由 composition 决定。
# 将导出的数据替换模板中的 needs_dom_review 后再审批：

python3 "$SKILL_DIR/scripts/approve_if_clean.py" \
  --file /项目/.hyperframes/layout-boxes.json --kind layout

python3 "$SKILL_DIR/scripts/validate_layout_boxes.py" \
  --layout /项目/.hyperframes/layout-boxes.json \
  --motion-plan /项目/.hyperframes/semantic-motion.json \
  --require-approved \
  --report /项目/.hyperframes/layout-qc.json

python3 "$SKILL_DIR/scripts/validate_av_alignment.py" \
  --project /项目绝对路径

python3 "$SKILL_DIR/scripts/run_gates.py" \
  --project /项目绝对路径 --stage motion
```

布局门禁保护：

- 人物脸和手。
- 人物核心动作。
- 插图核心结构。
- 标题和核心卡片。
- 字幕。
- 左下头像圆。
- 动画的完整扫掠路径。

### 阶段 G：HyperFrames

```bash
npx hyperframes lint
npx hyperframes check --snapshots
npx hyperframes snapshot --at <各场景中点>
npx hyperframes preview
```

顺序要点：先让 `run_gates.py --stage motion` 通过，再执行带快照的完整 check 和场景 snapshot；技术 check 通过不等于可以自动渲染。用户批准 preview 后，再用下面的 timing wrapper 执行 render；失败时只修对应层，不要盲目整条重跑。

### 阶段 H：交付

```bash
# 用户批准 preview 后，所有高成本命令都用 timing wrapper 记录；不要手写事件 JSON。
python3 "$SKILL_DIR/scripts/record_timing.py" \
  --timings /项目/pipeline-timings.json \
  --stage hyperframes_render -- \
  npx hyperframes render --quality high --output /项目/renders/final.mp4

python3 "$SKILL_DIR/scripts/finalize_video_assets.py" \
  --video /项目/renders/final.mp4 \
  --title "本集标题" \
  --summary "本集摘要"

python3 "$SKILL_DIR/scripts/extract_delivery_audio.py" \
  --video /交付目录/final.mp4 \
  --output-dir /交付目录

python3 "$SKILL_DIR/scripts/finalize_pipeline_timings.py" \
  --timings /项目/pipeline-timings.json

# 关闭 trace 后再运行一次 finalizer，让最终目录和 asset-manifest.json
# 拿到已关闭的 pipeline-timings.json；已有封面默认复用，不会被覆盖。
python3 "$SKILL_DIR/scripts/finalize_video_assets.py" \
  --video /项目/renders/final.mp4 \
  --title "本集标题" \
  --summary "本集摘要"

python3 "$SKILL_DIR/scripts/run_gates.py" \
  --project /项目绝对路径 --stage final
```

交付音频必须从最终 `final.mp4` 提取，不能直接复制早期 `narration_master.wav`，因为最终视频可能包含首帧 SFX、CTA SFX 或最终混音。

## 28 个脚本的作用

| 分组 | 脚本 | 作用 |
| --- | --- | --- |
| 预检 | `plan_fast_production.py` | 在高成本阶段前预测时长、验证缓存并生成增量重建决策 |
| 内容 | `validate_episode_independence.py` | 检查分集自洽、跨集预告和结尾 CTA |
| Prosody | `analyze_prosody.py` | 从 scenes 生成保守、可审核的 Prosody 草稿 |
| Prosody | `validate_prosody.py` | 检查 schema、声学基线、语义微语气和批准状态 |
| 声音 | `generate_all_voxcpm2.py` | VoxCPM2 生产驱动器：episode-take 连续生成入口（需 voxcpm 环境） |
| 声音 | `generate_voxcpm2_continuous.py` | 连续 take 生成引擎：自适应候选、早停、评分、raw 缓存 |
| 对齐 | `align_all_captions.py` | 强制对齐与字幕组/逐字 cue 生成（需 mlx_audio venv） |
| 声音 | `prepare_voxcpm2_prompt.py` | 从授权原件提取/验证完整黄金 prompt，记录哈希 |
| 声音 | `stabilize_aligned_continuous.py` | 对已经对齐的连续 take 做受限 gain 稳定化 |
| 声音 | `stabilize_audio_boundaries.py` | 降级分块路径的淡入淡出、间隙、重组和边界稳定化 |
| 声音 | `validate_voice_stability.py` | 检查短窗响度、F0/音区、谱亮度、句级能量和 retime 连续性 |
| 声音 | `validate_audio_boundaries.py` | 检查响度、间隙、起止和场景边界 |
| 声音 | `validate_scene_pacing.py` | 根据 timeline 统计有效中文 CPM；兼容旧项目和报告 |
| 视觉 | `validate_visual_assets.py` | 强制 provider、小木 Skill 调用、4–8 个 shot/资产和哈希 |
| 动效 | `plan_semantic_motion.py` | 从语义、时间、Prosody、插图和 catalog 生成 deterministic motion 草稿 |
| 动效 | `validate_semantic_motion.py` | 检查 hero/supporting、cue、转场、审批和哈希 |
| 布局 | `init_layout_boxes.py` | 从 approved motion plan 初始化布局盒草稿 |
| 布局 | `validate_layout_boxes.py` | 检查画布、bbox、swept bbox、安全区、保护元素和遮挡 |
| 对齐 | `validate_av_alignment.py` | 验证音频、字幕、semantic beat、DOM selector 和插图哈希绑定 |
| 交付 | `finalize_video_assets.py` | 生成封面、描述、三平台文案、manifest，并复制 QC sidecars |
| 交付 | `extract_delivery_audio.py` | 从最终 MP4 提取 48 kHz 双声道 WAV 和 MP3 |
| 交付 | `validate_video_output.py` | 最终规格和强制资产总检，验证审批与哈希新鲜度 |
| 编排 | `run_gates.py` | 按阶段一次性串行执行全部门禁，失败即停并输出汇总 |
| 编排 | `run_bounded_jobs.py` | TTS 固定单并发、渲染最多双并发的批处理执行器 |
| 编排 | `record_timing.py` | 包装命令自动追加 timing 事件，取代手写 pipeline-timings.json |
| 编排 | `approve_if_clean.py` | 无风险标记时自动批准草稿；有标记时列出需人工复核的位置 |
| 布局 | `extract_layout_boxes.js` | 浏览器端沿 paused timeline 采样，自动导出实际 swept bbox |
| 交付 | `finalize_pipeline_timings.py` | 关闭 timing trace，汇总 wall-clock、compute、缓存命中与失败数 |

### 编排脚本怎么用

`run_gates.py` 是阶段门禁的统一入口：

```bash
python3 "$SKILL_DIR/scripts/run_gates.py" --project /项目 --stage audio
python3 "$SKILL_DIR/scripts/run_gates.py" --project /项目 --stage motion
python3 "$SKILL_DIR/scripts/run_gates.py" --project /项目 --stage final --render-dir /项目/renders
```

它会把报告写入 `/项目/.pipeline/gates-report.json`，默认遇到第一个失败就停；只有定位问题时才加 `--keep-going`。缺少前置文件会明确记录为 `skipped`，不能把 `skipped` 当作通过。

`run_bounded_jobs.py` 读取一个 JSON jobs manifest，并把 TTS 并发限制为 1、渲染并发限制为 2，同时追加 timing 事件：

```json
{
  "jobs": [
    {
      "id": "episode-01",
      "command": ["npx", "hyperframes", "render", "--output", "renders/final.mp4"],
      "cwd": "/绝对路径/project-ep01",
      "attempt": 1,
      "cache_key": "render-key"
    }
  ]
}
```

```bash
python3 "$SKILL_DIR/scripts/run_bounded_jobs.py" \
  --jobs /项目/jobs.json --kind render \
  --timings /项目/pipeline-timings.json
```

单条高成本命令则用 `record_timing.py --timings ... --stage ... -- <command>` 包装；缓存复用用 `--status cache_hit` 记录。不要直接编辑 `pipeline-timings.json` 的事件数组。

## 项目文件契约

```text
project/
├── BRIEF.md
├── SCRIPT.md
├── script.json                 # 可选的机器可读章节/口播索引
├── series.json
├── scenes.json
├── visual-assets.json
├── index.html
├── assets/
│   └── <article-slug>-illustrations/
├── audio/
│   ├── prosody.json
│   ├── prosody-qc.json
│   ├── voice-manifest.json
│   ├── output/
│   │   └── narration_master.wav
│   ├── timeline.json
│   ├── caption-words.json
│   ├── caption-groups.json
│   ├── voice-stability-qc.json
│   └── boundary-qc.json
├── .hyperframes/
│   ├── semantic-motion.json
│   ├── motion-qc.json
│   ├── layout-boxes.json
│   ├── layout-qc.json
│   └── alignment-qc.json
├── compositions/
├── snapshots/
├── .pipeline/
│   ├── fast-production-plan.json
│   └── gates-report.json
├── pipeline-timings.json
└── renders/
```

`BRIEF.md`、`SCRIPT.md` 和 `references/default-profile.yaml` 不是一个会自动注入所有字段的统一运行时配置。生成/对齐脚本会读取 `series.json`、profile 的 `tts_runtime` 和命令行参数；HyperFrames、数字人和外部交付仍由项目适配器显式读取并传参。

## 最终交付契约

最终输出目录至少包含：

```text
final.mp4
cover.png
cover.jpg
cover-description.md
publishing-copy.md
asset-manifest.json
visual-assets.json
semantic-motion.json
motion-qc.json
layout-boxes.json
layout-qc.json
alignment-qc.json
qc-report.json
pipeline-timings.json
```

日期交付包还包含：

```text
audio/final-audio.wav
audio/final-audio.mp3
```

以及 `visual-assets.json` 引用的实际插图文件。

`pipeline-timings.json` 在最终关闭前必须有完整的 `wall_clock_*` 字段，并能区分实际 wall-clock、累计 compute、缓存命中和失败事件。用 `record_timing.py` 追加事件、用 `finalize_pipeline_timings.py` 收尾；不要手工改事件数组。

`validate_video_output.py` 会检查强制资产、批准状态和报告哈希新鲜度，但仍不能代替完整人工观看。声音是否自然、封面是否抓住语义、动画是否好看、人物是否在所有瞬间无遮挡，都需要人工 Review。

## 常用命令

```bash
# 查看所有脚本帮助
for f in "$SKILL_DIR"/scripts/*.py; do
  python3 "$f" --help
done

# 一次执行当前项目的机器门禁；没有输入的门禁会被明确标记为 skipped。
python3 "$SKILL_DIR/scripts/run_gates.py" \
  --project /项目绝对路径 --stage all

# 关闭已记录的耗时 trace，并输出 wall-clock / compute 摘要。
python3 "$SKILL_DIR/scripts/finalize_pipeline_timings.py" \
  --timings /项目绝对路径/pipeline-timings.json

# 搜索仍未去除的作者个人信息
rg -n "/Users/jaycehuang|SynologyDrive|木哥|小木|关注我" "$SKILL_DIR"

# 检查最终目录
python3 "$SKILL_DIR/scripts/validate_video_output.py" \
  --dir /项目/renders
```

## 隐私、版权与公开仓库注意事项

不要提交：

- 作者或任何第三方的声音原件。
- 黄金 prompt WAV。
- 人脸、数字人视频和私有人物参考图。
- VoxCPM2 或其他模型权重。
- 成片、缓存、临时音频、渲染帧。
- API Key、Token、Cookie、`.env`。
- NAS/云盘中的私有文件。
- 未获得授权的文章、图片、字体或 Logo。

仓库中的作者绝对路径只是迁移提示，不代表对应私有文件会或应该公开。

当前仓库没有附带开源许可证。下载和查看不等于自动获得作者声音、人物 IP、素材或第三方模型的使用授权；如需公开再分发或商业使用，请先确认相应权利。

## 当前限制

1. 没有“文章 → 全部工程 → 成片”的一键总 runner；`run_gates.py` 只统一机器门禁，`run_bounded_jobs.py` 只负责受限批处理。
2. 没有自动加载所有 YAML/BRIEF 字段的配置编译器；生成/对齐脚本只读取约定字段，HyperFrames 和外部交付仍需适配器显式传参。
3. 多处个人值仍在脚本和 validator 中硬编码（CTA、画布、头像安全区、小木 provider、声音默认路径），换人必须按上面的个人元素清单同步修改。
4. VoxCPM2/Qwen 强制对齐的模型权重、venv 和推理服务不随仓库提供；入口脚本已随包，但实际运行依赖本地环境。
5. HyperFrames composition、Storyboard/DOM、数字人合成和外部媒体目录复制仍是项目级实现。
6. 小木插图 provider 当前被强制，换人物需要同步修改 provider、manifest、Skill 调用和 validator，不能只换图片。
7. 1920×1080 和左下头像圆在多处被强制，换画幅需要结构性修改。
8. 自动 QC 不能代替声音、封面和视觉美感的人工 Review。

## 参考文件

- [`SKILL.md`](SKILL.md)：完整入口规则。
- [`references/workflow-contract.md`](references/workflow-contract.md)：工作流和输出契约。
- [`references/default-profile.yaml`](references/default-profile.yaml)：作者默认参数参考。
- [`references/prosody-schema.md`](references/prosody-schema.md)：Prosody 字段。
- [`references/voice-stability.md`](references/voice-stability.md)：声音稳定方法。
- [`references/voice-stability-profile.json`](references/voice-stability-profile.json)：声音 QC 阈值。
- [`references/motion-system.md`](references/motion-system.md)：动效系统。
- [`references/motion-catalog.json`](references/motion-catalog.json)：语义角色与动效 catalog。
- [`references/layout-box-schema.md`](references/layout-box-schema.md)：实际布局盒与 swept bbox 契约。
- [`references/pipeline-timings-schema.json`](references/pipeline-timings-schema.json)：耗时 trace schema。
- [`references/debug-checklist.md`](references/debug-checklist.md)：机器门禁和人工快照的调试顺序。
