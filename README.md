# agent-video-pipeline

一个可配置、可恢复、可审计的本地讲解视频流水线。它把已批准的口播稿包编排成：稳定母带、逐词字幕、视觉资产、语义动效、布局绑定、基础视频、可选数字人、封面、发布文案和最终 QC。

> 这是工作流与质量契约，不是某个账号的私有模板，也不是把所有 TTS/插画/渲染服务硬编码在仓库里的“一键黑盒”。个人声音、模型路径、人物 IP、CTA、画布、交付目录和 provider 都从外部 Profile 注入。

## 快速了解

流水线的唯一数据链是：

~~~text
口播稿包
  -> series.json / scenes.json
  -> prosody
  -> narration_master.wav + timeline + captions
  -> visual-assets.json
  -> semantic-motion.json
  -> layout-boxes.json
  -> HyperFrames 基础视频
  -> 可选数字人合成
  -> final.mp4 + 封面/文案/音频/QC
~~~

几个不可绕过的规则：

- 工作区必须存在完整的 `.agent-video/` 集中配置根目录，否则流水线拒绝运行。
- 每个项目先冻结一份 .pipeline/resolved-profile.json，下游只读这份配置。
- audio/output/narration_master.wav 是字幕、动画、数字人和成片时长的唯一时间基准。
- 音频、retime 或文本改变后，必须重新 forced-align；不能把旧字幕按比例平移。
- 已有本地图片默认复用，不自动覆盖；只有明确要求才替换。
- 动效和布局必须 deterministic、seek-safe，并通过完整运动路径遮挡检查。
- 任一上游产物缺失、未批准、QC 失败或 SHA 过期时，停止真实下游工作。
- 最终交付音频必须从最终 MP4 抽取，不能直接交付某个早期 TTS WAV。

## 目录

~~~text
agent-video-pipeline/
├── SKILL.md                         # 给 Agent 读取的主流程
├── references/                      # profile、声音、动效、布局和工作流契约
├── scripts/                         # 导入、TTS adapter、规划、验证、交付脚本
├── requirements.txt
└── README.md                        # 本说明
~~~

## 1. 安装

要求：

- Python 3.10+；
- ffmpeg、ffprobe；
- 一个安装了本仓库依赖的 Python 虚拟环境；
- 需要渲染时，另外准备 HyperFrames/Node 运行时；
- 需要声音或插画时，准备对应的 provider 运行时。provider 不包含在本仓库中。

~~~bash
git clone https://github.com/JayceHuang/agent-video-pipeline.git
cd agent-video-pipeline

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

python scripts/validate_profile.py \
  --profile references/default-profile.yaml
~~~

Windows PowerShell 可将 source 命令换成 .venv/Scripts/Activate.ps1。

默认 Profile 是中性的：无固定声音、CTA、人物、插画 provider、数字人和外部交付路径。实际项目应通过外部 Profile 注入这些内容。

## 2. 准备外部配置

不要把模型、授权声音、个人 prompt、密钥或本机绝对路径写进本仓库。第一次使用时先运行跨平台初始化脚本：

仓库内置两份可公开发布的脱敏模板：

~~~text
references/templates/workspace.example.yaml
references/templates/runtime.local.example.yaml
~~~

初始化脚本以它们作为唯一模板来源，在工作区外部生成实际配置。模板只有中性值和占位符，不包含任何用户身份、本机路径、凭据或私有素材。

macOS / Linux：

~~~bash
python3 scripts/init_config_root.py \
  --workspace /absolute/path/to/workspace
~~~

Windows PowerShell 或 CMD：

~~~powershell
py scripts\init_config_root.py `
  --workspace "D:\path\to\workspace"
~~~

脚本默认生成中性的 `profiles/workspace.yaml`，其中 `profile_id: workspace`，不会预填作者姓名、品牌、人物 IP、CTA 或其他个人信息；同时把当前执行它的 Python 写入 `pipeline_runtime.python`。只有需要多个外部 Profile 时才传 `--profile-id <custom-id>`。需要独立声音环境时增加 `--tts-python <解释器路径>`，使用本地模型时增加 `--model-path <模型目录>`。脚本只创建缺失文件，重复运行不会覆盖已经编辑的 Profile 或 runtime。

初始化后会在工作区建立：

~~~text
.agent-video/
├── profiles/workspace.yaml        # 标准化、中性的工作区模板
├── runtime.local.yaml              # 本机解释器、模型、对齐器和凭据
├── projects/                       # 可选的项目覆盖
├── assets/
│   ├── voice/
│   ├── avatar/
│   ├── character/
│   ├── logo/
│   └── music/
└── resolved/                       # 解析缓存
~~~

这些目录及 `runtime.local.yaml` 缺一不可。工作区 Profile、项目覆盖和 runtime 不能散放到视频项目目录；解析器会检查路径边界，失败时不生成 resolved profile。runtime.local.yaml 和 resolved 文件应加入你自己的 .gitignore。最少需要让流水线知道流水线解释器；声音 provider 启用时还需对应解释器：

~~~yaml
pipeline_runtime:
  python: /absolute/path/to/pipeline-venv/bin/python

tts_runtime:
  generator_python: /absolute/path/to/voice-venv/bin/python
  # generator、model_path、aligner_python 等按 provider 需要填写
~~~

外部工作区 Profile 可以按用户明确选择覆盖语言、口吻、目标 CPM、声音策略、画布、字幕、动效 preset、插画 provider、数字人安全区、封面和发布文案。初始化模板本身保持中性。合并顺序是：

~~~text
references/default-profile.yaml
  < 外部工作区 Profile
  < 本机 runtime.local.yaml
  < 可选项目级配置
~~~

## 3. 冻结项目配置（必做）

为每个视频项目选择一个明确的项目目录。下面的命令中，<PIPELINE_PY> 是安装了本仓库依赖的 Python，<PROJECT> 是项目目录，尖括号内容请替换成真实路径。

~~~bash
export SKILL_DIR=/absolute/path/to/agent-video-pipeline
export PIPELINE_PY=/absolute/path/to/pipeline-venv/bin/python
export PROJECT=/absolute/path/to/videos/my-video

"$PIPELINE_PY" "$SKILL_DIR/scripts/resolve_profile.py" \
  --config-root /absolute/path/to/workspace/.agent-video \
  --profile-id workspace \
  --project-config /absolute/path/to/workspace/.agent-video/projects/my-video.yaml \
  --project "$PROJECT"
~~~

也可以省略 `--config-root`，解析器会从项目目录向上查找最近的 `.agent-video/`；或者通过 `AGENT_VIDEO_CONFIG_ROOT` 指定。省略 `--profile-id` 时，`profiles/` 内必须恰好只有一个 YAML Profile，否则解析器拒绝猜测。不能只用 Skill 内的中性默认值启动流水线。

解析后必须存在：

~~~text
$PROJECT/.pipeline/resolved-profile.json
$PROJECT/.pipeline/resolved-profile.sha256
~~~

随后所有通用 Python 控制脚本都使用 resolved profile；不要在中途修改 Profile 后继续复用旧报告。Profile 改变会让真实受影响的下游失效，这是刻意设计的安全机制。

## 4. 从口播稿开始

长文章先使用独立的 adapt-longform-for-speech Skill，得到已批准的：

~~~text
spoken-script.md       # 人可读稿件，可选
spoken-script.json     # 导入所需的结构化稿件
script-qc.json         # 必须是 approved/pass
~~~

已有 series.json / scenes.json 的项目可以跳过导入。将通用稿件导入本流水线：

~~~bash
PROFILE="$PROJECT/.pipeline/resolved-profile.json"

"$PIPELINE_PY" "$SKILL_DIR/scripts/import_spoken_script.py" \
  --script /absolute/path/to/spoken-script.json \
  --script-qc /absolute/path/to/script-qc.json \
  --profile "$PROFILE" \
  --series-output "$PROJECT/series.json" \
  --projects-root "$(dirname "$PROJECT")"

"$PIPELINE_PY" "$SKILL_DIR/scripts/validate_episode_independence.py" \
  --series "$PROJECT/series.json" \
  --profile "$PROFILE"
~~~

导入器按 episode 的 `slug` 在 `--projects-root/<slug>/scenes.json` 写出场景文件；如果 slug 和项目目录名不同，请把后续命令中的 `PROJECT` 改成对应的生成目录。导入器只记录稿件、QC 和 Profile 的路径、ID 与 SHA，不把声音或本机运行时复制进通用产物。

## 5. 运行前规划与复用

在 TTS、ASR 或渲染等高成本阶段前先运行增量规划：

~~~bash
"$PIPELINE_PY" "$SKILL_DIR/scripts/plan_fast_production.py" \
  --project "$PROJECT" \
  --target-cpm 300 \
  --profile "$PROFILE"
~~~

规划结果会区分可复用和必须重建的部分。常见规则：

- 只改封面/发布文字：不要重做音频、字幕或视频；
- 只改目标 CPM，文本和 prosody 不变：优先复用原始候选并重新评分/retime；
- 音频、文本或 forced-align 改变：重新生成下游 caption、motion、render；
- 已有视觉资产：默认复用；
- Profile SHA、provider revision 或语义动效版本改变：按报告标出的真实下游重建。

## 6. 声音、字幕与音画对齐

先分析 prosody 并人工/自动批准，再调用 Profile 选择的 TTS provider：

~~~bash
"$PIPELINE_PY" "$SKILL_DIR/scripts/analyze_prosody.py" \
  --scenes "$PROJECT/scenes.json" \
  --profile "$PROFILE" \
  --output "$PROJECT/audio/prosody.json"

"$PIPELINE_PY" "$SKILL_DIR/scripts/approve_if_clean.py" \
  --file "$PROJECT/audio/prosody.json" --kind prosody
~~~

如果使用仓库随附的 VoxCPM2 adapter，必须用 resolved profile 中的声音运行时，而不是环境里的裸 python3：

~~~bash
TTS_PY=/absolute/path/to/voice-venv/bin/python

"$TTS_PY" "$SKILL_DIR/scripts/generate_all_voxcpm2.py" \
  --mode episode-take \
  --series "$PROJECT/series.json" \
  --project "$PROJECT" \
  --profile "$PROFILE" \
  --episode 1
~~~

该 adapter 会围绕整集 master、候选选择、全局 retime、单次归一化和稳定性 QC 工作；实际模型、对齐器、prompt 和参考音频必须由 Profile/CLI 提供。也可以自行实现 provider adapter，只要产物契约相同。

~~~bash
"$PIPELINE_PY" "$SKILL_DIR/scripts/run_gates.py" \
  --project "$PROJECT" --stage audio --profile "$PROFILE"
~~~

音频阶段至少应产生：

~~~text
audio/output/narration_master.wav
audio/timeline.json
audio/captions.json
audio/caption-words.json
~~~

必要时可以单独执行整集对齐：

~~~bash
"$PIPELINE_PY" "$SKILL_DIR/scripts/align_all_captions.py" \
  --series "$PROJECT/series.json" \
  --project "$PROJECT" \
  --episode 1 \
  --source-master
~~~

## 7. 视觉资产、语义动效与布局

先写出绑定 Profile SHA 的视觉决策 manifest。启用插画 Skill/provider 时，由 provider 填充；不启用时，manifest 必须明确标为 disabled/skipped，而不是留下旧资产。

~~~bash
"$PIPELINE_PY" "$SKILL_DIR/scripts/init_visual_assets.py" \
  --project "$PROJECT" --profile "$PROFILE"

# provider 生成或复用图片后
"$PIPELINE_PY" "$SKILL_DIR/scripts/validate_visual_assets.py" \
  --project "$PROJECT" --profile "$PROFILE"
~~~

用同一份时间轴、逐词字幕、prosody、视觉 manifest 和 resolved profile 规划动效：

~~~bash
"$PIPELINE_PY" "$SKILL_DIR/scripts/plan_semantic_motion.py" \
  --scenes "$PROJECT/scenes.json" \
  --timeline "$PROJECT/audio/timeline.json" \
  --prosody "$PROJECT/audio/prosody.json" \
  --caption-words "$PROJECT/audio/caption-words.json" \
  --visual-assets "$PROJECT/visual-assets.json" \
  --resolved-profile "$PROFILE" \
  --output "$PROJECT/.hyperframes/semantic-motion.json"

"$PIPELINE_PY" "$SKILL_DIR/scripts/init_layout_boxes.py" \
  --motion-plan "$PROJECT/.hyperframes/semantic-motion.json" \
  --profile "$PROFILE" \
  --output "$PROJECT/.hyperframes/layout-boxes.json"

"$PIPELINE_PY" "$SKILL_DIR/scripts/run_gates.py" \
  --project "$PROJECT" --stage motion --profile "$PROFILE"
~~~

basic-stable 是默认安全 preset；更复杂的 preset 必须提供运行时实现审计。所有 preset 都要通过 cue 同步、motion density、transition、seek-safe、protected zone 和完整 swept-bbox 遮挡检查。

## 8. 渲染、封装与日期交付

本仓库负责 orchestration 和 QC，不把某个 HyperFrames composition 的目录结构硬编码进 Skill。请在你的 composition 项目中执行其 README 定义的 build/check/render 命令，得到基础视频，例如：

~~~text
<PROJECT>/renders/base.mp4
~~~

渲染前建议再次执行 motion gate：

~~~bash
"$PIPELINE_PY" "$SKILL_DIR/scripts/run_gates.py" \
  --project "$PROJECT" --stage motion --profile "$PROFILE"
~~~

需要数字人时，调用独立的 compose-avatar-video Skill，把同一份获批母带作为最终音频基准。无论是否合成数字人，选定的最终视频都要放在 `$PROJECT/renders/final.mp4`；final gate 默认检查这个位置：

~~~text
<PROJECT>/renders/final.mp4
~~~

然后生成封面、发布文案和 manifest：

~~~bash
"$PIPELINE_PY" "$SKILL_DIR/scripts/finalize_video_assets.py" \
  --video "$PROJECT/renders/final.mp4" \
  --profile "$PROFILE" \
  --title "视频标题" \
  --summary "视频摘要" \
  --visual-description "封面和画面描述"

"$PIPELINE_PY" "$SKILL_DIR/scripts/run_gates.py" \
  --project "$PROJECT" --stage final --profile "$PROFILE"
~~~

日期交付版必须从最终 MP4 单独抽取音频，并把 WAV/MP3 放到对应交付目录：

~~~bash
"$PIPELINE_PY" "$SKILL_DIR/scripts/extract_delivery_audio.py" \
  --video "$PROJECT/renders/final.mp4" \
  --output-dir "$PROJECT/delivery" \
  --manifest "$PROJECT/renders/asset-manifest.json"
~~~

这一步避免把早期 TTS 音频误当成最终混音，也会把抽取结果写入交付 manifest。日期目录、封面副本和平台发布目录由外部 publishing/delivery Profile 决定，Skill 不假设任何云盘或 NAS 路径。

## 9. 最终交付包

通过 final gate 后，finalizer 的输出目录（上面的示例为 `$PROJECT/renders`）至少应能找到：

~~~text
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
delivery/audio/*.wav
delivery/audio/*.mp3
~~~

若使用外部交付根目录，复制交付副本；没有配置时保留项目内包即可。

## 10. 失败、恢复与排查

只修失败层，不盲目从头重跑：

~~~bash
# 先看完整门禁报告
"$PIPELINE_PY" "$SKILL_DIR/scripts/run_gates.py" \
  --project "$PROJECT" --stage all --profile "$PROFILE"

# 需要让后续层也跑完以收集报告时才使用 --keep-going
"$PIPELINE_PY" "$SKILL_DIR/scripts/run_gates.py" \
  --project "$PROJECT" --stage all --profile "$PROFILE" --keep-going
~~~

常见原因：

- profile SHA stale：重新解析 Profile，确认项目没有继续使用旧报告；
- audio boundary/voice stability：检查整集 master 的增益归一化、retime 和场景边界，不要用逐场独立响度硬拼；
- caption coverage/alignment：确认字幕完全覆盖 narration text，并对新 master 重新 forced-align；
- layout overlap：检查完整运动路径的 swept bbox，而不是只看首帧/末帧；
- visual-assets 未完成：让 provider 更新 manifest，或明确标记可选 provider 为 skipped；
- 没有渲染器：本仓库不安装 HyperFrames/Node；先完成 composition 项目的 build/check/render，再回到 final gate；
- 缺少依赖：使用同一个虚拟环境运行 requirements.txt 中的 Python 脚本，不要切换到另一个系统 Python。

## 11. 安全与版本控制

公共仓库里不要提交：

- runtime.local.yaml、resolved profile、API key、模型和参考音频；
- TTS/ASR 生成的 WAV、MP3、MP4、图片和缓存；
- 含个人绝对路径的私有项目配置。

共享风格可以放在独立 Profile 仓库或私有工作区；公共仓库只保留中性默认值、脚本、Schema、验证器和本说明。

## 更多契约

- [SKILL.md](SKILL.md)：给 Agent 的完整执行规则；
- [references/profile-contract.md](references/profile-contract.md)：配置合并、冻结和隐私边界；
- [references/workflow-contract.md](references/workflow-contract.md)：阶段、缓存、交付与 QC 契约；
- [references/default-profile.yaml](references/default-profile.yaml)：中性默认配置；
- [references/voice-stability.md](references/voice-stability.md)：连续母带、响度和声音稳定性；
- [references/motion-system.md](references/motion-system.md)：语义动效、deterministic 和 seek-safe 规则；
- [references/layout-box-schema.md](references/layout-box-schema.md)：布局框与遮挡验证。

如果你要接入新的 TTS、ASR、插画或渲染 provider，优先新增外部 adapter/profile，并保持上述产物、SHA、批准状态和 gate 契约不变。
