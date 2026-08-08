# Agent 视频工作流契约

这份契约把当前项目已经确认的规则固化下来，供 Mac 本地编排、渲染和交付使用。

## 输入

- 长文 Markdown、Obsidian 文档或已批准口播稿。
- 原始声音来源：`/Users/jaycehuang/Library/CloudStorage/SynologyDrive-Obsidian/66_自媒体/木哥原始音频/木哥音频.mp3`。
- VoxCPM2 的黄金 prompt WAV：从原始 MP3 310.95–319.15 秒冻结的 `/Users/jaycehuang/obsidian-proj/videos/voxcpm2-voice-reference/audio/muge-golden-prompt-v1.wav`（PCM 16-bit、48kHz、单声道）；同一文件同时作为 `prompt_wav_path` 和 `reference_wav_path`，并传入准确 `prompt_text`。
- 可选已经生成好的数字人/口型视频。
- 可选章节标题、平台定位和 CTA。
- 视觉插图 Skill：`ian-xiaomu-illustrations`（小木 A 版正文配图）。默认路径为 `/Users/jaycehuang/.codex/skills/ian-xiaomu-illustrations/SKILL.md`，以当前 Skill 注册表中的同名 Skill 为准。

## 分集与合集

- 默认按 1–3 分钟分集生成，并分别完成语音、字幕、画面和 QC；不要把整篇长文放进一次 TTS 或一次 HyperFrames 渲染。
- 每集必须对应一个独立主题和独立章节：有独立标题、摘要、完整论证和本集结论，不能依赖上一集铺垫，也不能把本集的关键结论留给下一集。
- 任何口播稿、字幕、画面文案或语气字段都不得出现跨集引用或预告，包括“上一集”“上集”“下一集”“下集”“下一期”“下期”“下回”“下一章”“敬请期待”“下集继续”“后续再讲”等表达。最后一个场景应先收束本集主题，再以固定 CTA `关注我，给你带来更多AI知识。` 结束，不追加下一集提示。
- TTS、字幕和画面生成前运行 `scripts/validate_episode_independence.py`；独立标题/摘要缺失、发现跨集预告或 CTA 不在最后一句时，必须停止并修稿。
- 需要完整长视频时，先合并已验收的分集成合集；合集默认只保留总片尾 CTA，避免每集 CTA 重复出现。
- 全文逐字长课也必须按章节独立生成、对齐和验收，再做最终拼接。

## 中间产物

```text
script.json              # 章节、口播稿、有效字数、focus 词
prosody.json             # 固定声学基线 + 句内 pause/stress/emotion/pitch
narration_master.wav     # 唯一音频时间轴
audio/timeline.json      # 场景 start/end/duration
audio/caption-groups.json# 字幕短语时间轴
audio/caption-words.json # 逐词/逐字 cue，优先作为动效锚点
.hyperframes/semantic-motion.json # 已审核的逐场语义动效计划
.hyperframes/motion-qc.json       # 动效同步、密度、布局计划和 seek-safe 门禁
.hyperframes/layout-boxes.json    # 实际元素和完整运动路径的包围盒
.hyperframes/layout-qc.json       # 圆区、字幕、插图和 protected element 遮挡门禁
visual-assets.json       # ian-xiaomu-illustrations 调用/复用/跳过决定
```

## 语气层

- TTS 前必须先生成 `prosody.json`，按句子/语义短语记录 `sentence_type`、`focus`、`pause_after_s`、`stress`、`emotion`、`emotion_strength`、`pitch`、`rate` 和 `style_instruction`。
- `analyze_prosody.py` 只生成可复核的保守初稿；调用方必须结合上下文审核，确认顶层 `status=approved` 后才能生成声音。
- 顶层 `acoustic_baseline` 锁定音区、发声力度、气息、距离、声线明暗和整体能量；句内允许受控的 question/contrast/conclusion/CTA 语调，`emotion_strength` 只允许 1–2、`rate` 只允许 0.98–1.02。
- 不得把 `[emotion=...]` 等控制标签拼进最终朗读文本，也不得把每句标签转换成独立声学 prompt；VoxCPM2 只接收一条整集风格指令，语义标签用于审核、停顿和 QC。
- 语气分析失败、字段缺失或未批准时，停止 TTS、字幕、动画和数字人流程，不用默认平调硬生成。

## 视频规格

- 默认横屏 1920×1080、30fps CFR。
- 每集默认不超过 3 分钟并作为一个连续 acoustic take；默认整集以 295 为目标。若用户要求约 330，则使用名义目标 330、整集允许区间 320–340。局部句子允许自然快慢，不再强迫每个视觉 scene 达到同一个整数 CPM。
- 左下圆形头像安全区：x=42、bottom=28、size=300。只保留圆形本身，不能扩成整条左侧留白；左上和左中为可用画面区域。预览辅助圆弧不得进入最终视频、封面或发布图片。
- 字幕底部居中，优先以逗号、分号和句号分组，问号/感叹号同样换组；中文 `；` 与英文 `;` 都必须换组，避免一个字幕包含多个分号。不能因字符数或时长阈值在标点之外硬切。
- 每集开头固定使用居中的标题卡：总时长约 1 秒，内容标题在 0.2 秒内入场，完整保持 0.6 秒，再用 0.2 秒淡出；标题卡淡出后才启动 narration、caption 和正文场景。标题只显示本集内容标题，不显示“第几章”“第几集/总集数”、页码或系列计数；场景内的左上标题继续遵守同一规则，系列编号只保留在元数据。
- 标题卡第一帧可绑定一个由 `media-use` 解析并冻结的短音效：t=0 精确触发、建议不超过 0.6 秒、静态音量约 0.20–0.35，禁止尾音侵入 1 秒后的第一句口播，也禁止叠加多个首帧冲击音。
- 每个视频结尾固定追加 CTA：`关注我，给你带来更多AI知识。`，并同步进入口播音频、字幕和时间线；CTA 使用温和收束语气。
- 开头和结尾的“关注我”引导必须有 seek-safe 动画：卡片平滑入场、箭头展开、单次波纹扩散；开头动画在标题卡 1 秒内完成，结尾动画与 CTA 口播/字幕起点一致，且不得遮挡人物、字幕或左下安全区。
- 底部只保留正式字幕，不显示说明性 footer、小字脚注、制作备注、技术标签、模型/引擎名称或时间轴标记。调试信息必须放在项目元数据或 preview-only 图层，最终视频、封面和发布图片中一律隐藏。
- 右下角不显示 `S01`、`S02` 等章节/场景编码或类似水印；这类标识只能保存在项目元数据。
- 画面元素按照语义 cue 出现，不能前置铺满，也不能用无意义动画填充讲解空档。
- 视觉资产遮挡门禁：小木人物脸、手部、核心动作及插图中的主要结构必须有独立 illustration safe box；场景标题、核心标题卡、beat 卡、字幕、转场和其他图片不得与其相交。多图叠加必须在 shot list 中显式声明并通过快照 QA，否则视为失败；禁止靠 z-index 覆盖来“解决”布局。
- 每集在 storyboard 前必须调用 `ian-xiaomu-illustrations` 并生成 `visual-assets.json`；运行 `scripts/validate_visual_assets.py` 时，`invocation_required=true`、`skill_invoked=true`、shot list 数量和图片资产数量都必须通过。单个场景可标记 `diagram_preferred`，整集不可跳过。
- 小木 Skill 每集默认生成 4–8 张 16:9 语义锚点图；每张图只表达一个核心动作，小木承担动作，使用 A 版身份参考、自然成人比例和黑白环境/低饱和人物半彩规则。已有本地图片仍要调用 Skill 做 shot 对齐与 QA，只做复用。
- 小木图片放在项目 `assets/<article-slug>-illustrations/`，在 `visual-assets.json` 和成片 `asset-manifest.json` 记录 `provider`、`invocation_stage`、`skill_invoked`、`shot_id`、路径、哈希和 `reused_existing`。音频中的“cloned Xiaomu voice”不计作插图 Skill 调用。
- 转场由 `.hyperframes/semantic-motion.json` 的 router 决定：相邻论点主要使用 `push-slide`，重大概念转向使用少量 `zoom-through`，结论/收束使用 `blur-crossfade`；全片最多三种 family。`paper-flip-soft` 只在纸张、笔记或章节语义成立时使用，不再是全片固定默认。下一场标题/核心视觉预入场不超过 0.12 秒，正文 beat 仍按音频 cue；转场不得白闪或盖住正式字幕。

## 语义动效契约

- 默认 profile 为 `premium-balanced`；`clean` 用于低成本预览，`cinematic` 只在用户明确要求高动态效果时启用。profile、primitive、fallback 和转场候选以 `references/motion-catalog.json` 为唯一机器可读来源。
- audio boundary、voice stability 和 visual-assets 门禁通过后，运行 `scripts/plan_semantic_motion.py`。输入必须包含 scenes、timeline、approved prosody、caption words 和 visual-assets；输出固定为 `.hyperframes/semantic-motion.json`，初始 `status=draft`。
- 每场恰好一个 hero motion，并按 profile 配置一到四个 supporting motion。语义角色至少覆盖 hook、definition、process、comparison、metric、warning、demo、hierarchy、example、conclusion、CTA 和保守 statement fallback。
- 每个 beat 绑定 scene/sentence/word/focus anchor，同时记录 `cue_s`、`audio_sample` 和 `render_frame`；word cue 可用时禁止退回关键字模糊匹配。输入哈希变化时计划立即 stale，不能平移旧秒数继续使用。
- 每场记录 `selection_reason`、layout variant、safe boxes、hero/support、transition、beats、intentional holds、budget 和 fallback chain。超过 4 秒没有新 cue 的区间必须声明具体 reason code 和 semantic owner；禁止自动写泛化理由后直接批准。
- 审核语义、anchor、布局和密度后设置 `status=approved` 与 `review.approved_by`，再运行 `scripts/validate_semantic_motion.py --require-approved`。该脚本必须生成 `.hyperframes/motion-qc.json`；source hash、scene ID/order、时间、密度、转场 family、safe zone、fallback 或 seek-safe 任一失败都停止 storyboard 和 render。
- HyperFrames host 负责 clip、音轨、字幕、SFX 和转场；每场使用独立 scene composition 表达镜内语义。实现时调用 `hyperframes-animation` 的对应 rule/blueprint；图片 wrapper 负责入场，child image 负责 2.5D 慢推，避免同一元素堆叠 transform tween。
- Storyboard/DOM 完成后，先用 `scripts/init_layout_boxes.py` 从 motion plan 生成待审核模板，再按 `references/layout-box-schema.md` 替换成实际元素坐标：动画元素记录整个运动路径的 `swept_bbox`，人物脸/手/动作、illustration、title、caption 和 avatar 都是 protected element。清除 `needs_dom_review`、设置 `actual_dom_verified=true` 并批准后，运行 `scripts/validate_layout_boxes.py --require-approved` 生成 `.hyperframes/layout-qc.json`；motion plan SHA、scene 顺序、时段、几何或层级任一失败都禁止 render。
- 默认优先 transform、opacity、SVG stroke 与 clip-path。性能不足时按“移除 filter → 压平 3D → 减少装饰/粒子 → shader 改 CSS → fade-slide → static-step”降级；语义 cue、数字、字幕和保护区不得改变。
- 视觉 QA 至少覆盖标题完整态、每场 establish/hero/payoff/end、所有转场中点、metric/warning/comparison、CTA 三阶段和封面候选。技术 QC 通过不能替代这些快照检查。

## 声音与对口型

- 只使用 VoxCPM2。IndexTTS2、GPT-SoVITS 不属于本流水线的候选模型。
- 声音溯源必须指向原始 MP3，但 MP3 只保存原件。先运行 `prepare_voxcpm2_prompt.py` 冻结黄金 prompt；manifest 必须记录原件、prompt WAV、prompt text、提取区间和 SHA-256。
- VoxCPM2 必须使用 ultimate cloning：`prompt_wav_path`、`reference_wav_path` 都指向黄金 prompt，并传入 exact `prompt_text`。模型 revision、cfg、steps 和 seed 都写入 `voice-manifest.json`。
- 默认把整集正文与 CTA 一次生成；visual scene 在 forced alignment 后从连续 take 划分。若整集失败，只能按完整句子降级为 45–60 秒块，并共享黄金 prompt 与声学基线。
- 默认生成 3 个 deterministic seed，最多 5 个。候选先通过 ASR 完整性与 `validate_voice_stability.py`，再按声学连续性、所需增益、局部包络、CPM 的顺序选择；不再只按 CPM 选择。
- 默认目标 295；用户明确要求约 330 时，整集名义目标 330、允许 320–340。连续 take 只做一次全局 retime，0.97–1.03 优先、0.95–1.05 硬上限；超过就重生或改稿。降级分块时所有块的 retime 应相同，相邻差不得超过 0.05。
- `audio/timeline.json` 仍记录每个视觉 scene 的字数、时长和局部 CPM，作为诊断而不是强制拉伸依据；另记录 `generation_mode=continuous_episode_take`、全局 retime、候选 seed 和 profile SHA。
- 后处理仅做受限慢速 gain（每句最多 ±3 dB）、一次轻压缩和一次 measured two-pass loudnorm（I≈-16、TP≤-2）。若线性二遍因真峰值余量不足回退 dynamic，必须改用测量目标静态增益加静态 true-peak limiter，并在 manifest 记录回退原因；禁止 scene/master 双重 dynamic loudnorm，禁止固定低频 notch stack 改变男声厚度。连续 take 强制对齐后若仍有局部字幕/边界跳变，只能运行 `scripts/stabilize_aligned_continuous.py` 做 0.5 秒窗口的受限 gain 平滑、边界渐变和静态真峰值保护；边界测量必须与 voice QC 统一采用 40ms/20ms 有声帧；它不再跑第二次 loudnorm，完成后必须更新 manifest 并重跑 QC。
- master 先完成整集 forced alignment，再运行 `validate_audio_boundaries.py`，随后运行 `validate_voice_stability.py`。后一门禁检查 1 秒包络、字幕短语、边界、F0、spectral centroid 和 retime；阈值唯一来源是 `voice-stability-profile.json`。连续 take 的 scene gap 是同一 master 上的分析切点，不按 120–200ms 拼接静音判错，但禁止时间范围重叠，仍必须通过边界 RMS/LUFS。任一失败时停止渲染：局部 gain 项可按上述 stabilizer 重算，F0/音色/气息/retime 失败必须换 seed。
- 所有报告都必须绑定当前 master、timeline、caption、prosody、prompt/reference 和 profile SHA；不匹配即 stale。更新音频后必须重新 forced-align，禁止只平移旧字幕。
- 最终混音也必须单独验收。若渲染器把 mono narration 复制为 dual-mono stereo，使用 `1/√2≈0.7071` 的渲染增益抵消约 +3.01 LU 的通道求和；最终 MP4 必须落在 -17.5 到 -14.5 LUFS 且真峰值不高于 -1.8dBTP。master 通过但 final 失败时仍禁止交付。
- 复生成视频时，如果输出目录已有 `cover.png` 或 `cover.jpg`，默认复用并保留现有图片及 `cover-description.md`；缺失格式可由现有图片转换。只有显式 `--force-cover` 才重新提取封面。
- 可选数字人/口型视频生成时使用的 WAV 必须来自 master；不能用另一版 MP3 或重新朗读的文件。
- 数字人视频只负责人物画面。最终剪辑默认使用 master 音频，避免重采样、编码延迟和双音轨。
- 成片前检查 `duration`、音频哈希、视频帧率和首帧/尾帧 PTS。

## 圆形安全区验收

- 安全圆边界为 `x=42..342`、`y=752..1052`（1920×1080）。正文、字幕、卡片和关键装饰不得与圆相交；圆外的左侧区域可以使用。
- 标题、章节标签和上半区信息应利用左上空间，不能统一从 `x≈400` 开始造成整列空白。
- 无数字人版本保留几何净空，但不绘制虚线、底色、弧线或“占位区”文字。需要调试时使用 preview-only 图层，并在渲染前关闭。

## 底部文字验收

- 底部区域只允许显示与当前口播同步的正式字幕；字幕消失时该区域应恢复为纯画面。
- 禁止出现“语义随口播逐步展开”“VOXCPM2 MASTER TIMELINE”等说明性文案，也禁止出现模型名、渲染器名、时间轴名、版本号和制作备注。
- 在场景快照与封面帧中检查底部区域；发现上述文字即判定画面验收失败。

## 成片目录契约

每个视频的最终目录至少有：

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
```

## 外部交付目录契约

- 每次成片通过 QC 后，必须把视频和封面复制到 `/Users/jaycehuang/Library/CloudStorage/SynologyDrive-Obsidian/66_自媒体/<MMDD>-<slug>/`。
- `<MMDD>` 使用 Asia/Shanghai 本地日期，例如 `0807-skill-beginner-ch03-ep01-330cpm`；至少包含 `final.mp4`、`cover.png` 和 `cover.jpg`，并保留描述、发布文案、manifest、visual-assets、semantic-motion、layout-boxes 与相关 QC 报告。
- 外部目录是带日期标签的交付副本；项目目录中的 `renders/` 资产必须保留，不能用移动或删除代替复制。
- 日期交付目录必须新增 `audio/final-audio.wav` 与 `audio/final-audio.mp3`。两者都从该目录的 `final.mp4` 抽取，WAV 为 48kHz PCM 立体声并保持完整视频时长，MP3 仅作为试听/发布副本；不得用另一版 narration master 代替。抽取后的路径、时长、采样率、声道、编码和 SHA-256 写入 `asset-manifest.json` 的 `delivery_audio` 字段。

## 发布文案契约

线程中已经确认：每次图片/封面生成都要同时交付抖音、小红书和视频号文案。

### 抖音

- 标题：关键词前置、直接清楚。
- 正文：简短、先给价值点。
- 标签：4–8 个搜索友好标签。

### 小红书

- 标题：有点击动力但不夸张。
- 正文：短段落，可适量使用 Emoji，适合收藏。
- 标签：5–10 个主题和搜索标签。

### 视频号

- 标题：可信、偏知识分享。
- 正文：自然完整，适合微信社交传播。
- 标签：3–6 个精准标签。

三平台文案必须分别优化，不能完全复制；默认只保存，不自动发布。

## 封面/图片规则

如果用户要求用图像模型制作新封面，而不是直接提取视频帧：

- 先锁定平台比例：小红书通常 3:4；视频号竖屏 9:16；视频号横屏 16:9。
- 标题必须逐字正确，主标题优先，避免生成额外伪文字、乱码、重复标题或水印。
- 有人物参考图时保留可识别身份，但人物通常作为老师/讲解者缩小到画面一侧，让主题标题和信息板成为主体。
- 生成后的图片、提示词（如有）、视觉描述和三平台发布文案必须保存在同一个输出目录。
- 图像生成模型不可用时，使用最终视频中的语义内容帧生成 `cover.png`，不能因此跳过描述和发布文案。
- 调用图像生成工具前，先检查项目和交付目录中是否已有同用途的本地图片（封面、章节图、背景图或插图）。已有图片默认复用，不重复生成、不覆盖；只有用户明确要求重做图片时才重新生成。
