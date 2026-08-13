# 语义动效系统

这份文件定义 `agent-video-pipeline` 的批量动效策略。目标不是让所有元素持续移动，而是让每个语义节点都有清楚、可复现、可降级的视觉动作。机器可读选型表见 [motion-catalog.json](motion-catalog.json)。

## 目录

- 生产原则
- 三档 Motion Profile
- 语义到动画的映射
- 动画层级与节奏
- 转场语法
- 图片、文字、图表和镜头规则
- 布局与无遮挡
- HyperFrames 实现约束
- 性能预算与降级
- Motion Plan 契约
- 规划与验证顺序
- 批量生产的一致性和变化
- 快照与视觉验收

## 生产原则

1. 先确定观众此刻要理解什么，再选择动画。动画必须能说明“关系、顺序、差异、变化或结论”中的至少一项。
2. 每个场景只设一个 hero motion。其余动作只能帮助建立层级、指向焦点或解释关系。
3. 场景遵循 `build → breathe → resolve`：前 30% 建立内容，中间 40% 阅读和理解，后 30% 完成结论或交给转场。
4. 静止是设计资源。没有新语义时允许画面稳定，不用永久漂浮、呼吸缩放或粒子填满空档。
5. 所有时间来自 `narration_master.wav` 的 timeline、caption words 和 approved prosody。不得在 HTML 中重新手抄另一套秒数。
6. 所有动作必须 deterministic、seek-safe、可直接定位到任意帧。禁止 `Math.random()`、`Date.now()`、墙钟、无限循环和累积 delta。
7. 高级感来自一致的运动语言、精确的落点和克制的层级，不来自同时堆叠 3D、模糊、发光、粒子和镜头摇移。

## 四档 Motion Profile

### `basic-stable`（默认）

用于批量正式成片。只使用低成本标题/卡片淡入位移、线条生长、节点出现和 Profile 允许的基础转场；只有 Profile 启用 CTA 动画时才使用对应 primitive。默认不使用镜头移动、3D、景深、扫描光、常驻视差、粒子、shader 或复杂 kinetic type。未选中的高级 DOM、装饰层和转场节点不进入默认构建产物；高级源码只保留给显式高级档。存在人物或插图时，其锚点由 Profile 和 layout variant 决定，入场后保持稳定。

### `clean`

用于信息密集、设备性能有限或快速批量预览。每场一个主动作和一到两个辅助动作；只用 transform、opacity、SVG path、静态遮罩和简单转场。

### `premium-balanced`

只在用户明确要求更丰富动效、且目标渲染器已经完成 implementation audit 时使用。每场一个主动作和两到三个辅助动作；允许 2.5D 图片、一次主镜头、遮罩揭示、数字变化和 SVG 路径。不能把“计划中存在 primitive”当成“成片已实现”。

### `cinematic`

只在用户明确要求高动态样片或电影级片段时使用。允许一到两个 hero shader transition 或高成本镜头，但仍服从语义 cue、字幕、布局和降级门禁；Profile 启用数字人安全区时还必须保护该区域。不得把该档作为大量视频的默认值。

`motion-catalog.json` 提供标准 preset 基线，resolved Profile 的 `motion.hierarchy`、`motion.density`、`motion.transitions`、`motion.layout_policy` 和 `motion.preset_overrides` 形成最终预算。项目必须把有效预算、seed 和输入哈希写进 `.hyperframes/semantic-motion.json`，不能根据目录名或临时代码开关高级动效。

## 动态布局规则

- 布局由语义角色、可用插图和固定 seed 共同选择，结果写入 motion plan；不按 scene index 机械轮换，也不在浏览器运行时随机。
- 最少 layout 数、是否允许相邻重复和可用 variant 全部读取 resolved Profile。
- Catalog 提供人物板书、上角人物、全宽流程、偏置编辑式、证据分栏、顶部流程条和卡片场等候选；项目只使用 Profile 选择且满足资产条件的 variant。每个 layout 必须使用预计算 safe boxes。
- Storyboard 必须把 `layout_variant` 和 `presenter_anchor` 写入 DOM 与 alignment binding；实现和计划不一致时停止渲染。

## 语义到动画的映射

| 语义角色 | 推荐 hero motion | 辅助动作 | 默认布局 | 降级方向 |
| --- | --- | --- | --- | --- |
| Hook | 短 camera punch + 关键词锁定，或 scanline title build | 下划线绘制、关键词 cascade、一次 glow bloom | 非对称大标题 | kinetic focus → fade-slide |
| Definition | 术语先固定，定义随后展开 | 连接线、关键词标记、右侧插图 mask reveal | 左术语右媒体 | 单定义卡逐显 |
| Process | 路径一次绘制，步骤按口播激活 | node activation、当前步骤高亮、一次 camera track | 中央路径两侧步骤 | 逐步卡片高亮 |
| Comparison | 两侧卡片镜像进入并标差异 | 中线绘制、差异 marker、标签 lock | 受保护 split frame | 两卡顺序淡入 |
| Metric | 旧值 → 方向 → 新值，或受控 count/scale | bar fill、证明条件、一次 impact | 大数字 + 证据区 | 静态数字 + 箭头展开 |
| Warning | 边框收紧或单次注意脉冲 | 风险词标记、一次 strike/difference marker | 警示轨 + 证据 | 静态警示卡 |
| Demo/Code | 设备窗口建立后由 cursor/typing 驱动状态变化 | 命令输入、局部放大、callout | 设备窗口 + 说明区 | 截图 mask reveal |
| Hierarchy | 核心节点先建立，层级或关系线逐层长出 | node activation、标签 lock | hub-and-levels | 静态树 + 逐层高亮 |
| Example | 证据卡按句子依次组装 | 插图慢推、重点圈画 | staggered evidence | 普通 card cascade |
| Conclusion | 已出现的节点收束到一句结论 | claim land、soft focus pull | convergence to claim | 结论卡落位 |
| CTA | 卡片入场 → 箭头展开 → 单次波纹 | 一次关键词亮起 | CTA 与 Profile 数字人安全区分离 | 卡片 + 箭头 |

选型时同时看 `semantic_role + data_shape + 可用安全区 + profile 能力`。低置信度时使用 `statement` 的低风险方案，不随机挑最炫的效果。warning 禁止抖动、频闪和循环；metric 禁止显示原稿没有的中间含义；CTA 只能触发一次。

## 动画层级与节奏

`basic-stable` 每场必须声明：

- `hero_motion`：恰好一个，表达该场的主要关系。
- `supporting_motions`：只允许一个，不能与 hero 抢同一焦点。
- `semantic_beats`：每个 beat 绑定 sentence/word/focus anchor，不得只写抽象秒数。
- `intentional_holds`：超过 4 秒没有新 cue 时，登记具体理由和语义 owner。

默认节奏：

- 微动作入场约 0.18–0.45 秒。
- 主卡片、术语和图片入场约 0.35–0.65 秒。
- SVG 路径、流程生长、图片慢推约 0.8–2.4 秒。
- `basic-stable` 不允许 major camera move；其他显式高级档同场最多一个。
- 两个 primary cue 间隔小于 profile 的 `min_primary_gap_s` 时，先合并成组合动作；仍过密则删除 support，最后才降为 fade-only。
- 不得用“每 N 秒必须动一次”替代语义判断。`event_interval_s` 只是规划提示，最终以句子、重点词和理解时间为准。

## 转场语法

默认视频最多使用两种转场 family：`push-slide` 与 `crossfade`。不要每场换一种，也不要把纸张翻页固定到全部场景。

- 60–70% 的相邻论点：`push-slide`，表达继续向前。
- 标题进入正文、结论和边界澄清：普通 `crossfade`。
- `zoom-through`、`blur-crossfade` 和 `paper-flip-soft` 只属于显式高级档，默认不用。
- `paper-flip-soft` 只在纸张、笔记、章节或编辑语义成立时作为可选 accent。
- `cinematic` 档可把一到两个重点转场升级为 `cinematic-zoom` 或 `domain-warp`；失败必须降到对应 CSS fallback。

转场由 host 层统一处理，子 composition 只负责镜内动画。出场和入场在同一时间点并发，转场本身就是出场；除最终场景外，不先把旧场景淡空再切下一场。正式字幕始终高于转场层。

## 图片、文字、图表和镜头规则

### 图片

- 每张图片放进独立 media safe box，禁止图片互相叠压。
- 入场 transform 放在 wrapper；Ken Burns/2.5D 慢推放在 child image，避免同一元素堆两个 transform tween。
- 默认慢推范围 `scale 1.00→1.03/1.06`；不得为了“有动画”无限放大。
- 可选 perspective tilt、mask reveal、scroll reveal 或局部浮层视差，但每张图只选一种主要处理。

### 文字

- 只对 focus 词做 kinetic emphasis；正文不能逐字乱飞。
- 定义先锁术语再出现解释；步骤按口播逐条出现；结论最后落位。
- 数字可 count/morph，但真实终值、单位、正负号和小数必须与稿件一致。
- 字幕动画和画面文字动画分轨处理；画面重点词不能替代正式字幕。

### 图表与关系

- 流程、因果和层级优先使用 SVG path draw + node activation。
- 对比优先使用受保护的左右/上下区域，不用 z-index 覆盖人物或插图。
- 卡片数量多时用 stagger，整段 stagger 总时长不超过 0.5 秒。

### 镜头

- 镜头只在概念转向、聚焦关键指标或流程追踪时使用。
- 每场最多一个大镜头动作；小幅 2.5D 视差不算第二个 hero camera move。
- camera transform 放在统一 world/stage wrapper，不能让多个内容元素分别模拟相互冲突的镜头。

## 布局与无遮挡

画布尺寸、数字人区域、字幕区域、场景标题区和内容区必须来自 resolved profile 的
`layout.canvas`、`layout.safe_boxes` 与 `layout.avatar_safe_zone`。规划器不得复制坐标默认值；
缺少必要区域时应停止并要求补全 Profile。

规划阶段先写 `layout.safe_boxes`；实现后必须检查完整运动路径的 swept bbox，而不只是起点和终点。人物脸、手、动作、场景标题、字幕和数字人安全区都是 protected region。多图组合必须声明 `intentional_composite_id`，否则任何相交都判失败。

## HyperFrames 实现约束

1. 使用一条 paused、seekable 主时间线；子 composition 也必须用稳定 timeline key。
2. `.clip` 场景优先使用 `fromTo()`，不要依赖 `from()` 的 `immediateRender`。
3. 环境动画也挂到 seekable timeline，禁止裸 `gsap.to()`。
4. scene boundary 和需要消失的 caption/overlay 使用确定性 hard kill，防止 seek 后复活。
5. 非最终场景不提前做独立 exit；转场负责旧场景离开和新场景进入。
6. Canvas/粒子使用固定 seed 或确定性 hash；Lottie 使用 `autoplay:false`、`loop:false` 并注册给 HyperFrames seek adapter。
7. 默认使用 transform、opacity、SVG stroke 和 clip-path。高成本 blur、filter、WebGL 与 `onUpdate` 仅在 profile 允许时使用。
8. 调用 HyperFrames 时读取 `hyperframes-animation` 的对应 rule/blueprint 和 transition catalog，不从记忆手写近似版本。

## 性能预算与降级

`basic-stable` 的批量优先级：

1. 保留语义 cue、文字、数字、路径和安全布局。
2. 优先移除全屏 blur、常驻 filter 和多层 shadow。
3. 禁止引入 3D、粒子、blur、filter、shader 或持续镜头动作。
4. 同一语义点只保留一个基础 primitive。
5. 计算或布局不稳定时直接降为 `fade-slide` 或 `static-step`。

降级只能降低视觉复杂度，不能改变 cue 时间、重点词、数字含义、布局保护区或字幕。每次降级写进 `degradations` 和 `motion-qc.json`。

## Motion Plan 契约

`.hyperframes/semantic-motion.json` 是实现前必须批准的声明式计划，至少包含：

```text
schema_version, status, profile, seed, sources, clock,
transition_grammar, global_safe_zones, scenes, review
```

每个 scene 至少包含：

```text
id, start_s, end_s, semantic_role, selection_reason,
layout_variant, safe_boxes, hero_motion, supporting_motions,
transition_in, beats, intentional_holds, budget
```

每个 beat 至少包含：

```text
id, semantic_anchor, cue_source, sentence_id, word_id,
cue_s, audio_sample, render_frame, primitive, target_ref,
priority, motion_cost, settle_s, fallback_chain, seek_safe
```

planner 只生成 `status=draft`。调用方结合全文、approved prosody、caption words、插图 shot list 和实际版面审核后，才能改为 `approved`。未知 primitive、模糊 anchor、过密动作或没有理由的长 hold 不能批准。

## 规划与验证顺序

1. 完成并通过 audio boundary gate。
2. 若 Profile 启用视觉资产，完成所选 provider 的 shot list 与 visual-assets gate。
3. 用 `plan_semantic_motion.py` 生成 `.hyperframes/semantic-motion.json`。
4. 人工/代理审核语义角色、anchor、hero、布局和 intentional hold，设置 `status=approved`。
5. 用 `validate_semantic_motion.py --require-approved` 生成 `.hyperframes/motion-qc.json`；失败就停止 storyboard 和 render。
6. 按 motion plan 写 modular HyperFrames：host 负责转场，scene composition 负责镜内语义动作。
7. 运行 HyperFrames check、布局检查和关键快照。
8. 渲染 MP4 后再次核对成片字幕、数字人安全区、静帧、转场和 A/V 同步。

## 批量生产的一致性和变化

- 同一 resolved profile 固定字体、调色板、线条、圆角、字幕、CTA 和 motion preset。
- 同一集只使用一套 transition grammar。
- 相邻场景不要连续使用完全相同的 hero motion；同类语义从 catalog 的候选中按稳定 seed 轮换。
- 同一系列可以记录 `series-motion-history.json`，限制连续模板和 hero 重复；该历史影响候选排序，不改变语义正确性。
- 变化主要来自信息结构和 hero motion；不要通过随机颜色、随机方向或随机特效制造“每集不同”。
- 缓存以 script、timeline、prosody、caption words、visual-assets、catalog 和 profile 的哈希为键。任一输入变化时 motion plan 标记 stale，不能平移旧秒数继续用。

## 快照与视觉验收

每集至少检查：

- 标题卡完整态和淡出后第一帧。
- 每场 establish、hero 开始、hero payoff、场尾。
- 每个 metric、warning、comparison 和重大 concept turn。
- 所有转场的中点与结束帧。
- Profile 启用 CTA 动画时，检查 CTA 卡片、箭头完成和波纹结束。
- 封面候选帧。

必须确认：没有空白/白闪、没有元素突然复活、图片和标题不遮挡、字幕不与数字人安全区相交、重点动画没有早于对应口播、画面没有因持续漂移而妨碍阅读。技术 QC 通过不能替代这些视觉检查。
