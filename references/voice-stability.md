# VoxCPM2 自然且稳定的声音契约

目标不是把声音压成一条直线，而是让同一个人以同一种发声状态，带着受控的自然语气讲完整集。生产承诺是“异常 take 不会进入成片”，不是承诺生成模型永不产生异常。

## 两层必须分开

### 固定声学基线

整集锁定以下六项，不随场景、句型或 CTA 切换：

- 音区与基准 F0；
- 发声力度与气息压力；
- 麦克风距离；
- 声线明暗与共鸣位置；
- 整体能量；
- 整体语速基线。

### 自然语义语气

允许问题略有探询、关键词轻微强调、对比关系清楚、结论与 CTA 温和收束。变化只发生在句内的停顿、轻重和小幅语调，不能靠提高音量、换音区、喊读、耳语或整段情绪状态切换来实现。

## 生成顺序（低自由度）

1. 用 `prepare_voxcpm2_prompt.py` 从唯一原始 MP3 冻结一条 6–15 秒、以完整句号结束、声学状态稳定的黄金 prompt。VoxCPM2 同时传入同一个 `prompt_wav_path`、`reference_wav_path` 和准确 `prompt_text`，使用 ultimate cloning；禁止再用被截断的开头 12 秒 reference-only 模式。
2. 一集不超过 3 分钟时，默认把整集口播（含 CTA）作为一个连续 acoustic take 生成。视觉 scene 只在 forced alignment 后从这条连续音频上划分，不能触发新的声学 take。
3. 若模型无法可靠生成整集，才按完整句子切为 45–60 秒块；每块共享黄金 prompt、模型 revision、cfg、steps 和声学基线，并带 2.5 秒上下文做候选连续性比较。禁止按视觉场景机械重置声音。
4. 候选按确定性 seed 顺序逐个生成。第 1 个生成后立即做 raw voice-stability 和 ASR/forced alignment；只有 acoustic/alignment 失败，或未进入 `voice-stability-profile.json` 的严格 early-stop 区间时才生成下一个。默认最多 3 个，必要时显式放宽到 5 个；`--candidate-count` 是上限，`--fixed-candidate-batch` 只用于诊断基准测试。
5. 单候选早停必须同时满足 acoustic pass、alignment pass、全局 retime 0.97–1.03，以及 profile 中更严格的局部包络、F0 和 spectral-centroid 阈值。未早停时先淘汰硬门禁失败项，再使用可复现的机器评分组合 prompt F0/centroid 距离、局部 F0/centroid 步进、1 秒包络和 CPM 偏差，选择最低分。不能只按语速选择。
6. 约 330 字/分钟是名义目标，整集 320–340 为自然区间。连续 take 只允许一次全局 retime：0.97–1.03 优先，0.95–1.05 为硬边界；超出就重生候选或改稿，不按 scene 使用相反方向的倍速。
7. 后处理只负责 gain，不修语气：句级慢速 gain rider 最多 ±3 dB（约 250 ms attack / 600 ms release），随后只做一次轻压缩（ratio≤1.4、GR≤4 dB）和一次带 measured 参数的两遍 EBU R128 loudnorm。禁止 scene 和 master 各做一次动态 loudnorm，禁止未经检测固定削减 146–293 Hz 的男声基频/低共振区。
8. master 必须运行 `validate_audio_boundaries.py` 和 `validate_voice_stability.py`；二者都为 `pass` 才能对齐字幕与渲染。最终 MP4 再运行相同局部画像，不能只看整集 LUFS/LRA。
9. 所有 QC 报告绑定 profile、prompt、reference、prosody、timeline、caption、raw/master/final 的 SHA-256；文件变化后旧报告立即视为 stale。产物先在 staging 完整通过，再原子替换项目文件，禁止半套新时间线配旧 master。

## 硬门禁

阈值唯一来源是 `voice-stability-profile.json`，脚本不得复制另一套默认值。当前生产线包括：

- speech-active 1 秒 RMS 的 P90-P10 ≤ 3.5 dB；
- 相邻字幕短语有效 RMS 差 ≤ 2.0 dB；
- 普通 scene 边界前后 0.4 秒有效 RMS 差 ≤ 2.0 dB；
- 相邻 retime factor 差 ≤ 0.05；连续 take 应全部相同；
- 2.5 秒声学基线滑窗的 F0 硬门禁为最大步进 ≤ 5 半音、P95 ≤ 3 半音；单候选 early-stop 使用更严格的最大步进 ≤ 4、P95 ≤ 2.5。
- 同一滑窗的 spectral-centroid 硬门禁为最大步进 ≤ 5 半音、P95 ≤ 3.5 半音；单候选 early-stop 使用更严格的最大步进 ≤ 4、P95 ≤ 3。

明确登记的轻微强调可以人工复核，但不能自动放宽整段阈值。英文拼读、句末低能量和停顿应通过 speech gate 排除，不能用全局重压缩掩盖。

## 失败处理

- raw 失败：换 seed；连续失败时只重生失败 take/块。
- 只有响度失败、声学基线通过：重算受限慢速 gain，不重生语气。
- F0、声线明暗、气息力度或 retime 失败：后期不救，必须重生。
- master 通过但 final 失败：检查 dual-mono 增益、混音、AAC 或音效侵入，再渲染；不得沿用 master 的 QC 代替成片 QC。
- 任何 profile/hash 不匹配：标记 stale 并重新验收，禁止交付。

## 标准命令

```bash
python scripts/prepare_voxcpm2_prompt.py

<voxcpm-python> scripts/generate_all_voxcpm2.py \
  --mode episode-take --series /path/to/series.json \
  --project /path/to/episode-project \
  --episode 2 --target-cpm 330 --candidate-count 3

<aligner-python> scripts/align_all_captions.py \
  --series /path/to/series.json --project /path/to/episode-project \
  --episode 2 --source-master \
  --timings /path/to/episode-project/pipeline-timings.json

python scripts/validate_voice_stability.py \
  --project /path/to/project

python scripts/validate_video_output.py \
  --dir /path/to/project/renders
```
