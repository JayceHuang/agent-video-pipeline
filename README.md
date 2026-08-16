# agent-video-pipeline

把已经确认的口播稿做成带配音、字幕、画面、动画和质量检查的视频。需要时还能继续合成数字人。

最省事的方式是把这个仓库作为 Codex Skill 使用。你只需要准备工作区、配置文件和口播稿，后面的检查与执行交给 Codex。

## 开始前准备

- Python 3.10 或更高版本
- ffmpeg 和 ffprobe
- 需要动画渲染时准备 Node.js 与 HyperFrames
- 需要本地配音、插图或数字人时准备对应工具

## 第一步　安装到 Codex

macOS 或 Linux

```bash
mkdir -p "$HOME/.codex/skills"
git clone https://github.com/JayceHuang/agent-video-pipeline.git \
  "$HOME/.codex/skills/agent-video-pipeline"
cd "$HOME/.codex/skills/agent-video-pipeline"

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.codex\skills"
git clone https://github.com/JayceHuang/agent-video-pipeline.git `
  "$env:USERPROFILE\.codex\skills\agent-video-pipeline"
Set-Location "$env:USERPROFILE\.codex\skills\agent-video-pipeline"

py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

完成后，仓库中会有一个安装好依赖的 `.venv`。

## 第二步　生成外部配置

选一个专门保存视频项目的工作区，然后执行初始化脚本。

macOS 或 Linux

```bash
mkdir -p /absolute/path/to/workspace
python scripts/init_config_root.py \
  --workspace /absolute/path/to/workspace
```

Windows

```powershell
New-Item -ItemType Directory -Force "D:\path\to\workspace"
python scripts\init_config_root.py `
  --workspace "D:\path\to\workspace"
```

脚本会在工作区生成下面的目录。

```text
workspace/
└── .agent-video/
    ├── profiles/
    │   └── workspace.yaml
    ├── runtime.local.yaml
    ├── projects/
    ├── assets/
    │   ├── voice/
    │   ├── avatar/
    │   ├── character/
    │   ├── logo/
    │   └── music/
    └── resolved/
```

初始化脚本可以重复运行。已经修改过的配置不会被覆盖。

## 第三步　修改配置

只需要记住下面几个位置。

| 想改什么 | 修改位置 |
| --- | --- |
| 语言、口吻、语速、画布、动效、CTA、平台文案 | `.agent-video/profiles/workspace.yaml` |
| 当前电脑的 Python、TTS 环境和模型路径 | `.agent-video/runtime.local.yaml` |
| 某一个视频的特殊要求 | `.agent-video/projects/<项目名>.yaml` |
| 参考声音 | `.agent-video/assets/voice/` |
| 数字人人像或视频 | `.agent-video/assets/avatar/` |
| 人物、Logo 和音乐 | `.agent-video/assets/character/`、`logo/`、`music/` |

`workspace.yaml` 默认是中性模板，不包含姓名、品牌、固定 CTA 或个人素材。

`runtime.local.yaml` 保存当前电脑的路径。这个文件不要提交到公共 GitHub 仓库。

Skill 内的 `references/default-profile.yaml` 是公共默认值，一般不要直接修改。

## 第四步　创建项目并冻结配置

先创建一个视频项目目录。

```text
workspace/
├── .agent-video/
└── my-video/
```

然后运行配置解析。

macOS 或 Linux

```bash
.venv/bin/python scripts/resolve_profile.py \
  --config-root /absolute/path/to/workspace/.agent-video \
  --profile-id workspace \
  --project /absolute/path/to/workspace/my-video
```

Windows

```powershell
.venv\Scripts\python.exe scripts\resolve_profile.py `
  --config-root "D:\path\to\workspace\.agent-video" `
  --profile-id workspace `
  --project "D:\path\to\workspace\my-video"
```

成功后，项目里会出现两个文件。

```text
my-video/.pipeline/
├── resolved-profile.json
└── resolved-profile.sha256
```

流水线运行前会检查 `.agent-video/`。目录没有生成、文件放错位置或配置已经失效时，程序会停止，不会继续配音、插图或渲染。

## 第五步　把内容交给 Codex

已经有口播稿时，可以直接告诉 Codex。

```text
使用 agent-video-pipeline 处理这份口播稿。

工作区是 /path/to/workspace
项目目录是 /path/to/workspace/my-video
口播稿是 /path/to/spoken-script.json

先检查外部配置和 resolved profile。检查失败就停止并告诉我应该修改哪个文件。
```

输入是长文章时，先让 Codex 调用 `adapt-longform-for-speech`，确认口播稿以后再进入视频流水线。

需要数字人时，基础视频通过检查后再调用 `compose-avatar-video`。

## 第六步　查看结果

不同 provider 产生的中间文件会有差异。常用结果都在项目目录里。

```text
my-video/
├── audio/                         # 配音、字幕和时间轴
├── visual-assets.json             # 视觉素材清单
├── .hyperframes/                  # 动效与布局计划
├── renders/                       # 基础视频和最终视频
├── delivery/                      # 可交付文件
└── .pipeline/resolved-profile.*   # 本次项目使用的冻结配置
```

配置发生变化后，重新运行 `resolve_profile.py`。不要手工修改 `resolved-profile.json`。

## 常见问题

### 提示找不到 `.agent-video`

回到第二步运行 `init_config_root.py`，并确认项目目录位于工作区内。也可以通过 `--config-root` 显式指定目录。

### 提示 Python 路径无效

打开 `.agent-video/runtime.local.yaml`，把 `pipeline_runtime.python` 改成当前电脑真实存在的 Python 解释器。

### 想换声音或人物

素材放入 `.agent-video/assets/` 对应目录，再到 `workspace.yaml` 里填写 provider 和素材路径。不要把个人素材复制进 Skill 仓库。

### 只想修改这一期视频

在 `.agent-video/projects/` 新建项目配置，只写这一期的例外。公共风格继续保留在 `workspace.yaml`。

## 详细文档

- [SKILL.md](SKILL.md)　完整执行规则
- [Profile 契约](references/profile-contract.md)　配置合并与隐私边界
- [工作流契约](references/workflow-contract.md)　阶段、缓存、交付和质量检查
- [声音稳定性](references/voice-stability.md)　配音与音频质量
- [动效系统](references/motion-system.md)　动效选择与限制
- [布局规则](references/layout-box-schema.md)　安全区与遮挡检查

## 安全说明

公共仓库只保存脱敏模板、脚本和规则。下面这些内容留在工作区，不要提交到 GitHub。

- `runtime.local.yaml`
- API Key 和其他凭据
- 参考声音、人物素材、模型和生成文件
- 含真实路径的 resolved profile

## 许可证

本项目采用 [MIT License](LICENSE) 开源。完整条款请查看 [LICENSE](LICENSE)。
