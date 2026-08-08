---
name: agent-video-pipeline
description: Turn a Chinese long-form article into a debuggable Mac-local tutorial video pipeline with chapter scripts, prosody-aware narration, profile-driven semantic motion planning, HyperFrames animation, subtitles, optional pre-rendered avatar/lip-sync compositing, and co-located publishing assets and QC reports. Use for requests to build, batch-produce, rerender, or debug the Agent/faceless explainer workflow on a Mac.
---

# Agent 视频流水线

这是一个“可复现、可调试、Mac 本地单机”的中文教程视频工作流。每次视频完成都必须在视频同目录留下封面图片、图片描述、三平台发布文案、任务清单和质检报告。

## 不可变的输出契约

最终视频所在目录必须包含：

```text
final.mp4                 # 最终视频
cover.png                 # 从最终视频提取的可复现封面帧
cover.jpg                 # 适合上传平台的压缩封面
cover-description.md      # 图片视觉描述、alt text、封面帧信息
publishing-copy.md        # 抖音、小红书、视频号的标题/正文/标签
asset-manifest.json       # 输入、输出、哈希、时长、规格
qc-report.json            # 最终检查结果
visual-assets.json        # 小木插图 Skill 的调用/复用/跳过决定与资产清单
semantic-motion.json      # 已批准的语义动效计划副本
motion-qc.json            # 动效密度、同步、布局计划和 seek-safe 门禁
layout-boxes.json         # 实际 Storyboard/DOM 的动态包围盒
layout-qc.json            # 头像、字幕、插图与运动路径遮挡门禁
alignment-qc.json         # 声音、字幕、语义 beat、DOM 卡片与插图绑定门禁
```

不要只把 MP4 发给用户。若封面或文案生成失败，任务应标记为 `incomplete`，不能声称视频交付完成。

最终交付还必须复制到外部媒体目录：

```text
/Users/jaycehuang/Library/CloudStorage/SynologyDrive-Obsidian/66_自媒体/<MMDD>-<slug>/
```

使用 Asia/Shanghai 本地日期的 `MMDD` 作为目录前缀，例如 `0807-skill-beginner-ch03-ep01-330cpm`。至少复制 `final.mp4`、`cover.png` 和 `cover.jpg`；同时保留发布资产、manifest、`semantic-motion.json`、`motion-qc.json`、`layout-boxes.json`、`layout-qc.json`、`alignment-qc.json`，并按 `visual-assets.json` 的相对路径复制实际插图文件，便于离线查看与追溯。外部目录只做交付副本，不移动或删除项目 `renders/` 中的原始资产。

日期交付版还必须包含从该交付版 `final.mp4` 单独抽取的音频：

```text
audio/final-audio.wav   # 48kHz、立体声、PCM，保持与 final.mp4 完全相同的起止时间
audio/final-audio.mp3   # 便于试听/发布的压缩副本
```

音频必须从最终交付 MP4 抽取，不能拿另一版 `narration_master.wav` 代替；抽取后要校验时长、采样率、声道和 SHA-256，并把两个文件的路径、规格、时长和哈希写入交付目录的 `asset-manifest.json`（`delivery_audio` 字段）。

## 核心流程

### 1. 文章和剧本

- 先读取全文，按内容结构自动判断章节，不按固定字数硬切。
- 每集默认控制在 3 分钟以内；教程内容不足时不要为了凑时长扩写。
- 每集必须是一个独立主题、一个独立章节：必须有自己的完整标题、摘要、正文论证和本集结论，观众不需要观看前后集才能理解本集。
- 禁止在任何口播、字幕、画面文案或 prosody/style 字段中加入跨集引用或预告，例如“上一集”“上集”“下一集”“下集”“下一期”“下期”“下回”“下一章”“敬请期待”等。结尾先收束当前主题，再只保留固定 CTA，不得用“下集继续”“后续再讲”等话术吊起下一集。
- 默认采用“分集生成、逐集验收”的方式，不要把整篇长文作为一次 VoxCPM2 TTS 或一次 HyperFrames 渲染任务。若用户需要完整长视频，先完成各集，再把已通过校验的成片拼接为合集；合集可只保留总片尾 CTA，避免重复口播。
- 只有用户明确接受几十分钟的长课并要求全文逐字口播时，才走长课模式；长课仍按章节独立生成和校验，禁止单次全篇 TTS/渲染。
- 口播稿要保留主要概念、例子、边界和结论；记录原文字数、口播有效字数和压缩比例。
- 在 TTS、字幕和画面生成前运行 `scripts/validate_episode_independence.py`；缺少独立标题/摘要、跨集预告词或固定 CTA 未落在最后一句时，立即停止流水线并修稿。
- 默认目标为 **295 个有效中文口播字/分钟**；用户明确要求约 330 时，把 330 当作名义目标，整集接受 320–340 的自然浮动。声音默认整集一次连续生成，只允许一次 0.97–1.03 的保守全局 retime（0.95–1.05 为硬边界）；视觉 scene 只是同一条声带上的时间区间，禁止再按场景使用相反方向的倍速。计算、字段和验收规则见 [references/voice-stability.md](references/voice-stability.md)。

### 1.5 语气分析（强制，先于 TTS）

- 剧本完成后，必须先把每个场景拆成句子/语义短语，生成 `prosody.json`；没有通过语气层门禁，不得调用 VoxCPM2。
- 使用 `scripts/analyze_prosody.py` 生成可复核的保守初稿，再按上下文审核 `sentence_type`、`focus`、`pause_after_s`、`stress`、`emotion`、`pitch` 和 `rate`。不能只根据标点，也不能把所有句子都标成强调或警告。
- 审核通过后将顶层 `status` 改为 `approved`，并运行 `scripts/validate_prosody.py --require-approved`。发现分类、停顿或情绪不合理时，先修正 `prosody.json`，不得让 TTS 自己猜。
- `prosody.json` 是剧本和声音模型之间的唯一语气来源；原始口播文本和控制字段分开保存，控制标签不能被读出来。字段说明见 [references/prosody-schema.md](references/prosody-schema.md)。

### 2. 语气和音频

必须为每个句子或语义短语生成 prosody 标记：

```text
[pause=0.25] 自然停顿
[stress=light] 轻读/轻重音
[stress=strong] 概念或结论重音
[emotion=calm|curious|warning|excited|warm]
[pitch=stable|slightly-up|slightly-down]
```

本流水线只使用 **VoxCPM2**，不自动切换 IndexTTS2、GPT-SoVITS 等模型。声音分为两层：整集固定同一音区、发声力度、气息、距离、声线明暗和速度基线；句内允许问题、对比、重点词和结论产生小幅自然语调。VoxCPM2 只接收一条整集风格指令，禁止按场景切换 calm/warning/warm 等声学状态；语义标签保留在 `prosody.json`，不得拼进朗读文本。

音频规则：

- 生成一份唯一的 `narration_master.wav`，作为字幕、动画、可选数字人视频和最终 MP4 的唯一时间基准。
- 优先使用 WAV（48kHz、单声道）；不要先转 MP3 再送去对口型。
- 默认语速规则是 295 个有效中文口播字/分钟、每场景 290–300；用户明确要求试跑其他速度时，必须显式传入 `--target-cpm`，并在 timeline/voice-manifest 标记 `speed_override` 与本次允许区间，不能悄悄改掉默认配置。
- 用户说“330 左右”时，以 330 为名义目标、整集允许 320–340，不追求每个 scene 精确等于 330。一次连续 take 只允许一次全局 retime：优先 0.97–1.03，硬边界 0.95–1.05；超出就换候选或改稿，不能用多个 scene 的相反倍速制造节奏跳变。
- 每集默认一次生成完整 acoustic take（含 CTA），先生成 3 个确定性 seed，必要时最多 5 个。只有模型无法可靠完成整集时，才按完整句子降级为 45–60 秒块；块之间共享同一个黄金 prompt、模型参数和声学基线，并做上下文连续性评分，不能按视觉 scene 机械重置声音。
- 候选必须先 forced-align，再按“硬门禁通过 → 声学状态最接近 → 所需增益最小 → 局部包络最稳 → CPM 最接近”排序。raw 的 F0、声线明暗或气息力度跳变时必须换 seed；旧 raw 只有通过 `voice-stability-qc` 才能复用，不能因为文件存在就直接进入平滑处理。
- 后处理只校正 gain，不改变语气：句级慢速 gain rider 最多 ±3 dB（约 250ms attack / 600ms release），之后只做一次轻压缩（ratio≤1.4、GR≤4dB）和一次带 measured 参数的两遍 EBU R128 loudnorm。禁止 scene 与 master 各跑一遍动态 loudnorm，也禁止未经检测固定削减 146–293Hz 的男声基频/低共振区。连续 take 完成 forced alignment 后，如果字幕短语或场景边界仍出现局部电平跳变，只能运行 `scripts/stabilize_aligned_continuous.py` 做一次受限的 0.5 秒窗口 gain 平滑、边界渐变和静态目标增益/真峰值 limiter；它不重做 loudnorm、不改音高/语速，并且必须更新 manifest 后重跑全部声音 QC。
- 如果 measured loudnorm 的线性二遍因真峰值余量不足而回退 dynamic，禁止接受这个动态回退；改用测量得到的目标静态增益，再接一次静态 true-peak limiter，并在 `voice-manifest.json` 的 `normalization.method` 和 `fallback_reason` 中记录。边界修正必须使用与 `validate_voice_stability.py` 相同的 40ms 帧/20ms hop 有声功率均值，避免“修正报告通过、最终 QC 仍失败”的测量口径漂移。
- **场景边界稳定门禁是强制步骤。** 普通分块切场不得沿用 0.55 秒等长空白；默认边界静音为 0.18 秒，只允许在 0.12–0.20 秒内自然浮动。连续 take 的 scene 只是同一 master 上的分析切点，不是音频拼接；此模式不把 80ms 之类的自然标点停顿判作 splice error，但仍禁止 scene 范围重叠，并必须通过边界 RMS/LUFS 门禁。确实需要长停顿时，必须在当前场景显式记录 `intentional_audio_pause=true` 或 `intentional_audio_pause_s`，不能靠未登记的长 gap 制造节奏。
- 连续 take 的视觉 scene 不再做物理拼接；只有 45–60 秒降级分块时才使用 12ms 边缘淡化和已登记的自然停顿。任何重新生成或 retime 都必须重新 forced-align，禁止只平移旧字幕时间。
- master 生成后，连续 take 先完成一次整集 forced alignment，再运行 `scripts/validate_audio_boundaries.py` 和 `scripts/validate_voice_stability.py`，分别写入 `audio/boundary-qc.json` 与 `audio/voice-stability-qc.json`。后者检查 1 秒局部响度、字幕短语、边界、F0、声线明暗与相邻 retime；任一失败都停止字幕、动画和渲染，阈值来自唯一配置 `references/voice-stability-profile.json`，不得临时放宽。若只剩局部 gain 跳变，按上一条规则做 aligned stabilization 后重新强制验收。
- master 通过不代表成片音轨通过。渲染器把单声道旁白复制为 dual-mono stereo 时会让综合响度约增加 3.01 LU；此时 narration 的渲染增益应使用 `1/√2≈0.7071`，或在最终混音阶段做等效校准。渲染后必须再次测量最终 MP4：综合响度需在 -17.5 到 -14.5 LUFS，真峰值不得高于 -1.8dBTP；`scripts/validate_video_output.py` 未通过时不得交付。
- 这套门禁保证的是“异常音频不会进入成片”，不是承诺 TTS 模型永远不产生异常。`boundary-qc.json` 或 `voice-stability-qc.json` 不是 `pass`、SHA 与当前输入不一致或报告过期时，后续流水线一律视为未完成。
- 当前默认的原始声音来源是 `/Users/jaycehuang/Library/CloudStorage/SynologyDrive-Obsidian/66_自媒体/木哥原始音频/木哥音频.mp3`。它是声音溯源的唯一原件；旧的 `voice-reference-v3.wav` 不再作为默认来源。
- VoxCPM2 不直接读取 MP3。运行 `scripts/prepare_voxcpm2_prompt.py`，从原件 310.95–319.15 秒冻结完整句子的 `/Users/jaycehuang/obsidian-proj/videos/voxcpm2-voice-reference/audio/muge-golden-prompt-v1.wav`。调用 VoxCPM2 时必须把它同时作为 `prompt_wav_path` 与 `reference_wav_path`，并传入 manifest 中完全对应的 `prompt_text`（ultimate cloning）；禁止继续使用截断在半句话上的“开头 12 秒 reference-only”模式。
- 每份 `audio/voice-manifest.json` 必须记录原始 MP3、黄金 prompt WAV、prompt text、提取区间、模型 revision、cfg、steps、每个 seed 和所有 SHA-256。任一来源或 profile 变化时先使缓存/QC 失效，再开始 TTS。
- 如果已有外部数字人/口型视频，它必须使用这份主音频生成；最终合成默认丢弃视频自带音频，重新使用主音频，避免编码延迟和双音轨。

### 3. 画面和动画

- 默认横屏 1920×1080、30fps、CFR。
- 左下角只保留真实的圆形数字人安全区：左 42px、底 28px、直径 300px。安全区是圆形，不是整条左侧栏；左上和左中必须正常参与排版，不能因为头像区让整列画面留白。正文、装饰和字幕不得与圆形相交；调试圆弧只允许在预览中出现，`final.mp4`、封面和发布图片必须隐藏圆弧，但仍保留该位置净空。
- 字幕下方居中，字幕分组以逗号、分号和句号为首要边界，问号/感叹号同样换组；中文 `；` 和英文 `;` 都必须换组，避免一个字幕里堆叠多个分号。不得为了字符数或时间上限在标点之外硬切。重点词才使用黄色。
- 每个视频固定先播放居中的设计感标题卡：优先读取 `audio/timeline.json.title`（无则读取 `scenes.json.episode_title` 或 `scenes.json.title`），只显示本集内容标题，不显示“第几章”“第几集/总集数”、页码或其他系列编号。标题卡总时长固定约 1 秒：0.2 秒入场、完整停留 0.6 秒、0.2 秒淡出；旁白、正式字幕和正文内容必须在标题卡淡出后才开始，不能让第一句口播压在标题卡下面。标题卡消失后，正文场景再按原有语义节奏展开；场景顶部标题仍只保留内容标题，不显示章节标签或“01 / 04”这类集数计数。标题层不占用左下圆形安全区，也不能使用底部技术小字样式。
- 标题卡第一帧默认可使用一个短促、克制的非音乐音效，必须从 `media-use` 解析并冻结到项目；时间点严格为 t=0，建议时长不超过 0.6 秒、`data-volume` 约 0.20–0.35，不能拖到 1 秒后的第一句口播，也不能叠加多个冲击音效。音效资产和来源必须进入媒体/成片 manifest。
- 每个视频的最后一句固定追加 CTA：`关注我，给你带来更多AI知识。`；CTA 必须进入主音频、字幕和时间线，语气温和收束，不能只做画面贴字。
- 出现“关注我”引导时必须绑定确定性动画：卡片使用平滑 `power3.out` 入场，箭头随后展开，最后扩散一次轻量波纹；禁止弹跳和循环。开头引导在 1 秒标题卡内完成，结尾引导必须与固定 CTA 口播/字幕的起点一致，并在字幕结束后平滑退场。动画不得覆盖小木主体、正式字幕或左下圆形安全区。
- 成片底部只允许出现正式字幕；不得添加说明性小字、制作备注、技术标签、引擎名称或时间轴标记，例如“语义随口播逐步展开”“VOXCPM2”“MASTER TIMELINE”。这类调试信息如有必要，只能存在于项目元数据或预览调试层，不能进入 `final.mp4`、封面和发布图片。
- 画面右下角不得出现 `S01`、`S02` 等章节/场景编码或类似水印；章节信息只保留在项目元数据，不进入成片、封面或发布图片。
- 默认使用 `premium-balanced` 动效档：每场恰好一个 hero motion、两到三个 supporting motion，按概念、步骤、对比、数字、警告、示例和结论逐步展开。`clean` 用于低成本预览；`cinematic` 只在用户明确要求高动态样片时启用。详细预算和选型见 [references/motion-system.md](references/motion-system.md) 与 [references/motion-catalog.json](references/motion-catalog.json)。
- 画面随着语义节奏逐步展开：概念出现、例子展开、关系连通、指标变化、结论落地；不要一开始把所有卡片同时摆满，也不要规定每句话或每 N 秒必须动。没有新语义时允许稳定阅读。
- 画面排版必须遵守“关键主体不遮挡”规则：小木人物的脸、手部、动作和插图中的核心结构先划定 illustration safe box；场景标题、核心标题卡、beat 卡、字幕、转场层和其他图片不得覆盖该区域。多个图片资产也不得互相叠放，除非 shot list 明确声明 intentional composite，并在快照 QA 中确认主体仍完整可见。禁止用更高 z-index 把人物或关键结构压在标题下面。
- 转场使用统一语法，不再全片固定纸张翻页：相邻论点默认 `push-slide`，重大概念转向最多一到两次 `zoom-through`，结论/收束使用 `blur-crossfade`；全片最多三种 family。`paper-flip-soft` 仅在纸张、笔记或章节语义成立时使用。下一场景的标题/核心视觉预入场不超过 0.12 秒，正文 beat 仍按音频 cue 对齐，字幕始终高于转场。
- 图片必须放在独立 media safe box：wrapper 负责入场，child image 负责 1.00→1.03/1.06 的 2.5D 慢推或 parallax，避免同一元素叠两个 transform tween。流程优先 SVG path + node activation；对比优先 protected split；数字优先旧值→方向→新值；重点词才使用 kinetic type。
- 动画必须 deterministic、seek-safe：使用 paused timeline 和 `fromTo()`，环境动作也挂到该 timeline；禁止运行时随机、墙钟、无限循环和裸 `gsap.to()`。除最终场景外，转场负责出场，不先把旧场景淡空；边界元素和字幕必须 hard kill，防止直接 seek 后复活。
- 每个场景都要有语义 beat 和音频 cue。长于 4 秒的讲解空档必须登记具体的 intentional hold 和 semantic owner，而不是用无意义漂移动画掩盖。
- `scenes.json` 中应为每个需要画面响应的语义点声明显式 `visual_beats`（`anchor/title/detail/slot`）。`anchor` 必须能在 forced-aligned `caption-words.json` 中精确找到；Storyboard 必须实现每一个已计划 beat，禁止按卡片数量截断、跳过或用 `focus/items` 数组取模配对。

### 3.1 语义动效规划门禁（强制）

- `narration_master.wav`、`audio/timeline.json`、approved `prosody.json`、`caption-words.json` 和 `visual-assets.json` 就绪后，先运行 `scripts/plan_semantic_motion.py`，生成 `.hyperframes/semantic-motion.json`。planner 只写 `status=draft`，不得直接进入 storyboard。
- 审核每场 `semantic_role`、anchor、hero/support 层级、transition、safe boxes、长 hold 和 fallback；低置信度选保守的 `statement` 方案。审核通过后设置 `status=approved` 和 `review.approved_by`。
- Storyboard 前强制运行 `scripts/validate_semantic_motion.py --require-approved`，生成 `.hyperframes/motion-qc.json`。输入哈希过期、cue 越界、动作过密、未知 primitive、头像/字幕/插图安全区相交、无理由长 hold 或非 seek-safe，任一项失败都停止动画与渲染。
- HyperFrames host 只负责 clip、转场、音频、SFX 和字幕；每场使用独立 scene composition 实现镜内语义动画。实现时读取 `hyperframes-animation` 对应 rule/blueprint 和 transition catalog，不从记忆手写近似效果。
- Storyboard/DOM 完成后，先用 `scripts/init_layout_boxes.py` 生成模板，再把实际元素及完整运动路径的 swept bbox 写入 `.hyperframes/layout-boxes.json`，按 [references/layout-box-schema.md](references/layout-box-schema.md) 运行 `scripts/validate_layout_boxes.py --require-approved`。模板中的 `needs_dom_review` 未清除、只声明 safe box 但实际元素越界、字幕/头像/人物相交或动画元素缺少 swept bbox，都必须停止 render。
- Storyboard/DOM 完成后还必须运行 `scripts/validate_av_alignment.py --project <project-dir>`。它逐项核对主音频时长、字幕全文覆盖、word cue、全部 semantic beat 的 DOM selector/可见文案、motion sidecar 与场景插图哈希；`alignment-qc.json` 不是 `pass` 时禁止 render。
- 默认优先 transform、opacity、SVG stroke 和 clip-path。遇到渲染压力时依次移除 filter、压平 3D、减少装饰、改 CSS 转场，再降到 fade-slide/static-step；降级不得改变语义 cue、数字、字幕和布局安全区。

### 小木插图 Skill（视觉插图阶段，强制）

- 这是每集都必须执行的固定阶段，不再使用“按需调用”或整集跳过。视觉脚本完成后、HyperFrames storyboard/画面实现前，必须显式调用 `ian-xiaomu-illustrations`（小木 A 版正文配图）Skill；默认 Skill 文件为 `/Users/jaycehuang/.codex/skills/ian-xiaomu-illustrations/SKILL.md`，如果安装位置不同，按当前 Skill 注册表解析同名 Skill。
- 每次调用都必须先为本集设计 4–8 个 shot list，再生成或复用对应的 4–8 张 16:9 配图；每张图只表达一个核心动作或结构，小木必须承担动作，人物保持自然成人比例、深蓝 T 恤、灰色长裤和白鞋，环境以黑白手绘为主并保留留白。纯结构/对比场景可以在 shot list 中标注 `diagram_preferred`，但不能因此跳过本集 Skill 调用。
- 生成前必须读取该 Skill 要求的角色、风格、色彩、提示词和 QA 参考；生成后按其 QA 清单逐张验收。已有同用途本地图片时仍需调用 Skill 做 shot 对齐与 QA，只复用图片，不重新生成、不覆盖。
- 图片保存到项目 `assets/<article-slug>-illustrations/`，并在 `visual-assets.json` 记录 `invocation_required=true`、`skill_invoked=true`、调用阶段、4–8 个 shot、路径、哈希和复用/新生成状态；成片 `asset-manifest.json` 镜像这份决定。HyperFrames 画面引用这些资产后再进入渲染。
- 小木插图必须与标题和其他视觉资产分区排版：生成 storyboard 时为插图、场景标题、核心卡、字幕和转场分别指定不重叠的 safe box；插图居中时，核心标题卡必须移到插图下方或旁侧，不得压住人物。渲染前必须运行遮挡预检并抽查开场、首个 beat、每个场景和转场快照；发现未声明的 bbox 相交、人物脸/手/动作被遮挡或图片互相覆盖时，停止渲染并先重排尺寸、位置或层级。
- 单个场景可以记录 `diagram_preferred` 和 `skip_reason`，但整集不得记录 `skill_invoked=false`。`visual-assets.json` 未通过强制校验时，停止 Storyboard、TTS、动画和渲染。`VoxCPM2 cloned Xiaomu voice` 只表示声音克隆来源，不等于调用了小木插图 Skill。

### 4. 可选数字人合成（Mac 本地）

如果用户已有数字人/口型成片，直接把本地 MP4 放进当前 Mac 项目并进入最终合成；没有数字人视频时保留左下角圆形占位区。无需跨机器传输、任务队列或额外运行时配置。

### 5. 数字人视频回到 Mac 后的合成

收到可选数字人视频后，先检查：

1. 视频时长与主音频相同（允许不超过 1 帧误差）。
2. 视频为 30fps CFR，不能是可变帧率。
3. 数字人生成时使用的输入音频哈希等于 `narration_master.wav`。
4. 人物头部完整、圆形裁切后居中，脸不会被字幕或边缘切掉。

通过后再进行圆形 mask、缩放和左下角叠加。最终视频只保留一条主音频轨道。

### 6. 成片后强制生成发布资产

视频渲染完成后立即运行：

```text
scripts/finalize_video_assets.py
scripts/validate_video_output.py
```

`finalize_video_assets.py` 默认从最终 MP4 选取一个语义内容已经出现的帧，生成 `cover.png` 和 `cover.jpg`，并写入 `cover-description.md`。若调用图像生成模型制作新封面，也必须把生成后的图片和描述放在同一目录，不得只保存到模型缓存。

如果重新生成视频时输出目录已经存在 `cover.png` 或 `cover.jpg`，默认复用本地图片，不重新提取或生成，不覆盖用户挑选的封面；缺失的另一种格式可以从现有图片转换得到。只有显式传入 `--force-cover` 才允许替换。已有 `cover-description.md` 也随图片一起保留。

调用任何图像生成工具前，都要先检查项目和交付目录中是否已有同用途的本地图片（封面、章节图、背景图或插图）。已有图片默认直接复用，不重复生成、不覆盖；只有用户明确要求重做图片时才重新生成。

`publishing-copy.md` 必须分别生成：

- 抖音：单独标题、正文、4–8 个标签。
- 小红书：单独标题、分段正文、5–10 个标签。
- 视频号：单独标题、自然正文、3–6 个标签。

三平台文案不能完全复制；默认只生成素材，不自动发布。

成片校验通过后，将交付副本按日期标签复制到 `/Users/jaycehuang/Library/CloudStorage/SynologyDrive-Obsidian/66_自媒体/` 下；不要只把文件留在项目工作目录。Storyboard 前运行 `scripts/validate_visual_assets.py --project <project-dir>`；每集必须 `invocation_required=true`、`skill_invoked=true`，并有 4–8 个 shot 与对应图片资产，否则停止流水线。把 `visual-assets.json`、`semantic-motion.json`、`motion-qc.json`、`layout-boxes.json`、`layout-qc.json` 与 `alignment-qc.json` 一并复制到外部交付目录。

## Mac 本地运行命令

语音合成前先生成并验收语气层：

```bash
python scripts/analyze_prosody.py \
  --scenes /path/to/scenes.json \
  --output /path/to/audio/prosody.json

python scripts/validate_prosody.py \
  --prosody /path/to/audio/prosody.json \
  --require-approved

python scripts/prepare_voxcpm2_prompt.py

<voxcpm-python> videos/skill-beginner-chapter-03-series/generate_all_voxcpm2.py \
  --mode episode-take \
  --episode 2 \
  --target-cpm 330 \
  --candidate-count 3

<align-python> videos/skill-beginner-chapter-03-series/align_all_captions.py \
  --episode 2 \
  --source-master

python scripts/validate_voice_stability.py \
  --project /path/to/project
```

音频和插图门禁通过后，先生成并批准动效计划：

```bash
python scripts/plan_semantic_motion.py \
  --scenes /path/to/scenes.json \
  --timeline /path/to/audio/timeline.json \
  --prosody /path/to/audio/prosody.json \
  --caption-words /path/to/audio/caption-words.json \
  --visual-assets /path/to/visual-assets.json \
  --profile premium-balanced \
  --output /path/to/.hyperframes/semantic-motion.json

# 审核后设置 status=approved 与 review.approved_by，再运行：
python scripts/validate_semantic_motion.py \
  --plan /path/to/.hyperframes/semantic-motion.json \
  --require-approved \
  --report /path/to/.hyperframes/motion-qc.json

python scripts/init_layout_boxes.py \
  --motion-plan /path/to/.hyperframes/semantic-motion.json \
  --output /path/to/.hyperframes/layout-boxes.json

# 按实际 DOM 更新模板、清除 needs_dom_review 并批准后再运行：
python scripts/validate_layout_boxes.py \
  --layout /path/to/.hyperframes/layout-boxes.json \
  --motion-plan /path/to/.hyperframes/semantic-motion.json \
  --require-approved \
  --report /path/to/.hyperframes/layout-qc.json

python scripts/validate_av_alignment.py \
  --project /path/to/project
```

视频完成后，在最终 MP4 所在目录生成同目录资产：

```bash
python scripts/finalize_video_assets.py \
  --video /path/to/final.mp4 \
  --title "Agent 都有哪些：从自主等级到适用场景" \
  --summary "解释 Agent 的自主等级、界面形态、工作流与自主型区别，以及判断标准。"
```

最后质检：

```bash
python scripts/validate_video_output.py \
  --dir /path/to/output
```

分集脚本完成后先验收独立性：

```bash
python scripts/validate_episode_independence.py \
  --series /path/to/series.json
```

## 调试顺序

遇到问题时按以下顺序定位：

1. `prosody.json`：确认每个句子都有语义类型、停顿、重音、情绪、音高和速度提示，并且已标记 `approved`。
2. `scripts/validate_prosody.py --require-approved`：确认语气字段在保守范围内，没有连续强重音或大幅情绪跳变。
3. `asset-manifest.json`：确认主音频和可选数字人视频的哈希、时长、路径。
4. `qc-report.json`：确认分辨率、帧率、音轨和强制输出文件。
5. `audio/timeline.json` 与 `caption-groups.json`：确认字幕/动画是否使用同一主音频。
6. `scripts/validate_audio_boundaries.py` 与 `audio/boundary-qc.json`：确认 gap、边界、开头和真峰值通过。
7. `scripts/validate_voice_stability.py` 与 `audio/voice-stability-qc.json`：确认字幕短语/1秒包络、F0、声线明暗、边界和 retime 全部通过，且报告中的 master、timeline、caption 和 profile SHA 均为当前文件；渲染后由 `validate_video_output.py` 对最终 MP4 重跑局部画像。
8. `scripts/validate_scene_pacing.py`：连续 take 检查整集目标区间和唯一全局 retime；分块降级模式再检查所有块的 retime 相同或相邻差≤0.05，不能强制每个视觉 scene 达到同一个整数 CPM。
9. `.hyperframes/semantic-motion.json`：确认 profile、语义角色、word/sentence anchor、hero/support 层级、转场语法、safe boxes 和 fallback；输入哈希变化时重建，不能平移旧秒数。
10. `scripts/validate_semantic_motion.py --require-approved` 与 `.hyperframes/motion-qc.json`：确认同步、密度、长 hold、seek-safe 和布局计划通过。
11. `.hyperframes/layout-boxes.json` 与 `scripts/validate_layout_boxes.py --require-approved`：确认实际元素的 swept bbox、时间和层级没有侵入头像、字幕、人物或其他图片。
12. `.hyperframes/alignment-qc.json` 与 `scripts/validate_av_alignment.py`：确认声音全文、字幕组、逐字 cue、全部动效 beat、DOM 卡片文字和场景插图是一条完整绑定链，没有遗漏或错配。
13. HyperFrames check 与场景快照：抽查标题卡、每场 establish/hero/payoff/end、所有转场、metric/warning/comparison、CTA 和封面候选；确认圆区净空、无白闪、无元素复活、无未完成阴影。
14. 场景快照：确认底部除正式字幕外没有说明性小字、制作备注、技术标签、引擎名或时间轴标记。
15. 首帧与 CTA 快照：确认 t=0 音效已登记，开头/结尾关注动画可见且不遮挡正文、人物、字幕或安全区。
16. `cover-description.md`：确认封面帧不是空白页，描述与画面一致。
17. `publishing-copy.md`：确认三个平台都有独立的标题、正文和标签。

## 资源

- 工作流规则：[references/workflow-contract.md](references/workflow-contract.md)
- 默认配置：[references/default-profile.yaml](references/default-profile.yaml)
- 语气字段和审核规则：[references/prosody-schema.md](references/prosody-schema.md)
- 自然且稳定的声音契约：[references/voice-stability.md](references/voice-stability.md)
- 声音稳定阈值唯一来源：[references/voice-stability-profile.json](references/voice-stability-profile.json)
- 高级但克制的语义动效系统：[references/motion-system.md](references/motion-system.md)
- Motion profile、语义配方、primitive 与转场目录：[references/motion-catalog.json](references/motion-catalog.json)
- 实际动态布局盒与 swept bbox 契约：[references/layout-box-schema.md](references/layout-box-schema.md)
