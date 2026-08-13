# 外部 Profile 契约

## 配置层

按以下顺序深度合并，后者覆盖前者：

1. Skill 内的中性 `default-profile.yaml`
2. Skill 外的个人风格 Profile
3. Skill 外的本机 runtime 配置
4. 可选的项目级覆盖配置

个人 Profile 保存语言、口吻、CTA、声音风格、视觉 provider、动效 preset、画布、数字人布局与合成参数、封面和平台文案偏好。本机 runtime 保存流水线控制解释器（`pipeline_runtime.python`）、声音 provider 的解释器（`tts_runtime.generator_python`）、模型路径、对齐环境、声音资产、provider 凭据和交付根目录。二者都不是通用 Skill 的组成部分。

首次冻结配置时直接从 `runtime.local.yaml` 读取 `pipeline_runtime.python`，后续控制脚本读取 resolved profile 中的同名字段。执行本地声音 provider 时，必须使用 resolved profile 中的 `tts_runtime.generator_python` 启动 `tts_runtime.generator`；不得退回调用环境里的裸 `python3`。虚拟环境解释器路径应保持原样，不要解析为其基础 Python 的符号链接目标，否则可能丢失该环境的 site-packages。

发布文字模板放在 `publishing.copy_templates`，标签放在 `publishing.tags`，封面描述与
Alt text 模板放在 `publishing.cover_description_template` / `publishing.alt_text_template`。
通用 finalizer 只负责安全插值与文件生成，不保存任何平台固定文风。

推荐把它们集中放在工作区的同一个配置根目录，而不是放回 Skill：

```text
.agent-video/
├── profiles/<profile-id>.yaml
├── runtime.local.yaml
└── resolved/
```

`runtime.local.yaml` 与 `resolved/` 应加入 `.gitignore`；可共享的个人风格 Profile
可以正常版本管理。

## 冻结产物

`resolve_profile.py` 写出：

- `<project>/.pipeline/resolved-profile.json`
- `<project>/.pipeline/resolved-profile.sha256`

resolved profile 记录所有来源文件与 SHA。下游报告只需记录 `profile_id` 和 `profile_sha256`，不得把密钥复制进 manifest。

## 通用默认行为

没有外部 Profile 时使用中性口吻、无固定 CTA、无固定声音身份、无强制插图 provider、无数字人安全区、无外部交付目录。所有 QC 完整性规则仍然生效。

## 禁止项

- 不在 Python、JavaScript、SKILL.md 或通用 reference 中写入个人姓名、固定 CTA、人物 IP、绝对用户路径或固定 provider。
- 不允许 validator 自行保存一套与 resolved profile 不同的画布、坐标或资产数量。
- 不允许项目在 resolved profile 变化后继续复用旧报告而不更新 SHA。
