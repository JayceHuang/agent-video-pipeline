# 动态布局盒契约

Storyboard 完成后、HyperFrames render 前，把实际实现中的关键元素写入 `.hyperframes/layout-boxes.json`。这份文件验证实际布局，不是 motion plan 中的建议区域。

```json
{
  "schema_version": 1,
  "status": "approved",
  "canvas": {"width": 1920, "height": 1080},
  "motion_plan": {"path": ".hyperframes/semantic-motion.json", "sha256": "..."},
  "scenes": [{
    "id": "01-hook",
    "start_s": 1.0,
    "end_s": 9.2,
    "elements": [{
      "id": "hero-media",
      "role": "illustration",
      "shape": "rect",
      "x": 960,
      "y": 250,
      "width": 760,
      "height": 500,
      "swept_bbox": {"x": 942, "y": 238, "width": 790, "height": 524},
      "start_s": 1.3,
      "end_s": 9.2,
      "protected": true,
      "animated": true,
      "z_index": 20,
      "intentional_composite_id": null
    }]
  }]
}
```

规则：

- 坐标以最终 1920×1080 画布为准，`y` 从顶部开始。
- 动画元素必须写 `swept_bbox`，它是整个运动路径的包围盒；只写起点和终点不合格。
- 每个语义 beat 必须有一个实际 DOM 元素记录：`role=beat`、`semantic_beat_id=<motion beat id>`、`animated=true`，且 `start_s/end_s` 覆盖该 beat 的 `cue_s`。计划了但没有进入 DOM 的 beat 必须失败，不能只留在 motion plan/QC 中。
- 每场必须包含 title、content、caption、avatar；使用插图时还要包含 illustration，以及脸、手和动作的子保护盒。
- avatar 固定为 `x=42, y=752, diameter=300`；caption 默认 `x>=368`。
- 两个 protected element 在相同时间内相交即失败，除非两者具有相同且非空的 `intentional_composite_id`。
- background 不参与遮挡；transition 可以覆盖画面，但 `z_index` 必须低于 caption。
- 人物 face、hand、action 与父 illustration 可使用同一个 composite id；标题、卡片或其他图片不能复用该 id 绕过门禁。
- scene ID/order、时间范围和 motion plan SHA 必须匹配当前计划。更新布局或 motion plan 后必须重跑 `scripts/validate_layout_boxes.py`。
