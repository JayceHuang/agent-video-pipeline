# 外部 Profile 契约

## 配置层

按以下顺序深度合并，后者覆盖前者：

1. Skill 内的中性 `default-profile.yaml`
2. Skill 外的工作区 Profile
3. Skill 外的本机 runtime 配置
4. 可选的项目级覆盖配置

外部工作区 Profile 可以保存用户明确选择的语言、口吻、CTA、声音风格、视觉 provider、动效 preset、画布、数字人布局与合成参数、封面和平台文案偏好；初始化模板本身必须保持中性。本机 runtime 保存流水线控制解释器（`pipeline_runtime.python`）、声音 provider 的解释器（`tts_runtime.generator_python`）、模型路径、对齐环境、声音资产、provider 凭据和交付根目录。二者都不是通用 Skill 的组成部分。

首次冻结配置时直接从 `runtime.local.yaml` 读取 `pipeline_runtime.python`，后续控制脚本读取 resolved profile 中的同名字段。执行本地声音 provider 时，必须使用 resolved profile 中的 `tts_runtime.generator_python` 启动 `tts_runtime.generator`；不得退回调用环境里的裸 `python3`。虚拟环境解释器路径应保持原样，不要解析为其基础 Python 的符号链接目标，否则可能丢失该环境的 site-packages。

发布文字模板放在 `publishing.copy_templates`，标签放在 `publishing.tags`，封面描述与
Alt text 模板放在 `publishing.cover_description_template` / `publishing.alt_text_template`。
通用 finalizer 只负责安全插值与文件生成，不保存任何平台固定文风。

必须把它们集中放在工作区唯一的 `.agent-video` 配置根目录，而不是放回 Skill 或复制到各项目目录：

```text
.agent-video/
├── profiles/workspace.yaml         # 默认中性模板；可显式创建其他 ID
├── runtime.local.yaml
├── projects/                      # 可选项目级覆盖文件
├── assets/                        # 声音、人像、人物 IP、Logo、音乐等复用资产
└── resolved/
```

以上目录和 `runtime.local.yaml` 缺一即视为配置契约失败，流水线不得运行。外部工作区 Profile 只能从 `profiles/` 读取，项目覆盖只能从 `projects/` 读取，runtime 只能使用根目录下唯一的 `runtime.local.yaml`。项目目录只保留本项目输入、输出、可再生 manifest 与 `.pipeline/resolved-profile.*`，不得维护工作区配置副本。

首次准备工作区必须先运行 `scripts/init_config_root.py --workspace <workspace>`。该脚本只使用 Python 标准库，在 macOS、Linux 和 Windows 上运行同一份实现；它默认创建中性的 `profiles/workspace.yaml`、runtime、资产分类目录、README 和 `.gitignore`，但绝不覆盖已有配置。标准模板不得包含作者姓名、人物 IP、CTA、品牌或其他个人信息；用户只有在明确需要时才追加个性化覆盖与授权资产。

脱敏模板的唯一来源是 Skill 内的 `references/templates/workspace.example.yaml` 与 `references/templates/runtime.local.example.yaml`。初始化器只能复制并替换显式占位符，不得在 Python 中维护另一份配置结构。模板可以提交到 GitHub；生成后的 `.agent-video/runtime.local.yaml`、用户资产和含真实路径的 resolved profile 不得提交到 Skill 仓库。

配置根目录按以下顺序确定：

1. 命令行 `--config-root`
2. 环境变量 `AGENT_VIDEO_CONFIG_ROOT`
3. 从 `--project` 开始向父目录查找最近的 `.agent-video/`

显式指定的根目录本身也必须命名为 `.agent-video`。路径参数不能通过 `..` 或符号链接逃逸规定目录。`runtime.local.yaml` 与 `resolved/` 应加入 `.gitignore`；不含密钥和敏感资产路径的工作区 Profile 可以按需要进行版本管理。

## 冻结产物

`resolve_profile.py` 写出：

- `<project>/.pipeline/resolved-profile.json`
- `<project>/.pipeline/resolved-profile.sha256`

resolved profile 记录所有来源文件与 SHA。下游报告只需记录 `profile_id` 和 `profile_sha256`，不得把密钥复制进 manifest。它还必须记录 `_meta.config_contract_version`、`_meta.config_root` 以及每个来源的角色。所有下游加载器必须复验集中配置根目录、来源边界和 SHA；旧式或手工伪造的 resolved profile 不得继续运行。

## 通用默认行为

Skill 内的中性默认 Profile 只作为合并基线，不能单独启动流水线。即使不需要固定 CTA、声音身份、插图 provider 或数字人，仍必须在统一配置根目录提供一个明确的中性工作区 Profile 与 runtime。

## 禁止项

- 不在 Python、JavaScript、SKILL.md 或通用 reference 中写入个人姓名、固定 CTA、人物 IP、绝对用户路径或固定 provider。
- 不允许 validator 自行保存一套与 resolved profile 不同的画布、坐标或资产数量。
- 不允许项目在 resolved profile 变化后继续复用旧报告而不更新 SHA。
- 不允许从 `.agent-video/profiles/`、`.agent-video/projects/` 之外加载工作区或项目配置。
- 不允许缺少集中配置根目录时回退到项目内散装配置或 Skill 中性默认值。
