# 调试定位顺序

遇到问题时按以下顺序定位，不要跳步，也不要在未定位根因前重跑高成本阶段。

1. `audio/prosody.json`：确认每个句子都有语义类型、停顿、重音、情绪、音高和速度提示，并且已标记 `approved`。
2. `scripts/validate_prosody.py --require-approved`：确认语气字段在保守范围内，没有连续强重音或大幅情绪跳变。
3. `asset-manifest.json`：确认主音频和可选数字人视频的哈希、时长、路径。
4. `qc-report.json`：确认分辨率、帧率、音轨和强制输出文件。
5. `audio/timeline.json` 与 `caption-groups.json`：确认字幕/动画是否使用同一主音频。
6. `scripts/validate_audio_boundaries.py` 与 `audio/boundary-qc.json`：确认 gap、边界、开头和真峰值通过。
7. `scripts/validate_voice_stability.py` 与 `audio/voice-stability-qc.json`：确认字幕短语/1秒包络、F0、声线明暗、边界和 retime 全部通过，且报告中的 master、timeline、caption 和 profile SHA 均为当前文件；渲染后由 `validate_video_output.py` 对最终 MP4 重跑局部画像。
8. `scripts/validate_scene_pacing.py`：连续 take 检查整集目标区间和唯一全局 retime；分块降级模式再检查所有块的 retime 相同或相邻差不超过 profile 上限，不能强制每个视觉 scene 达到同一个整数 CPM。
9. `.hyperframes/semantic-motion.json`：确认 profile、语义角色、word/sentence anchor、hero/support 层级、转场语法、safe boxes 和 fallback；输入哈希变化时重建，不能平移旧秒数。
10. `scripts/validate_semantic_motion.py --require-approved` 与 `.hyperframes/motion-qc.json`：确认同步、密度、长 hold、seek-safe 和布局计划通过。
11. `.hyperframes/layout-boxes.json` 与 `scripts/validate_layout_boxes.py --require-approved`：确认实际元素的 swept bbox、时间和层级没有侵入头像、字幕、人物或其他图片。
12. `.hyperframes/alignment-qc.json` 与 `scripts/validate_av_alignment.py`：确认声音全文、字幕组、逐字 cue、全部动效 beat、DOM 卡片文字和场景插图是一条完整绑定链，没有遗漏或错配。
13. HyperFrames check 与场景快照：抽查标题卡、每场 establish/hero/payoff/end、所有转场、metric/warning/comparison、CTA 和封面候选；确认圆区净空、无白闪、无元素复活、无未完成阴影。
14. 场景快照：确认底部除正式字幕外没有说明性小字、制作备注、技术标签、引擎名或时间轴标记。
15. 首帧与 CTA 快照：确认 t=0 音效已登记，开头/结尾关注动画可见且不遮挡正文、人物、字幕或安全区。
16. `cover-description.md`：确认封面帧不是空白页，描述与画面一致。
17. `publishing-copy.md`：确认三个平台都有独立的标题、正文和标签。

提示：第 1–12 步可以直接用 `scripts/run_gates.py --project <dir> --stage all` 一次跑完机器可验的部分；只有失败项需要按上表回到对应文件手工定位。
