# 动态布局盒契约

Storyboard 完成后、渲染前，把实际 DOM 的关键元素写入 `.hyperframes/layout-boxes.json`。画布和 protected zones 必须来自同一 resolved Profile。

```json
{
  "schema_version": 1,
  "status": "approved",
  "canvas": {"width": 1280, "height": 720},
  "profile": {"id": "standard", "sha256": "..."},
  "motion_plan": {"path": ".hyperframes/semantic-motion.json", "sha256": "..."},
  "actual_dom_verified": true,
  "scenes": [{
    "id": "scene-01",
    "start_s": 0.0,
    "end_s": 8.0,
    "elements": [{
      "id": "hero-media",
      "role": "illustration",
      "shape": "rect",
      "x": 640,
      "y": 160,
      "width": 500,
      "height": 360,
      "swept_bbox": {"x": 620, "y": 150, "width": 530, "height": 380},
      "start_s": 0.4,
      "end_s": 8.0,
      "protected": true,
      "animated": true,
      "z_index": 20,
      "intentional_composite_id": null
    }]
  }]
}
```

规则：

- 坐标以 resolved Profile 的最终画布为准，`y` 从顶部开始。
- 动画元素必须写完整运动路径的 `swept_bbox`。
- 每个 semantic beat 必须对应实际 DOM 元素，并让活动时间覆盖 `cue_s`。
- title、content、caption 始终必备；插图和数字人角色只在 Profile/场景启用时必备。
- 数字人形状、坐标和尺寸以及字幕最小 x 都从 Profile 读取。
- protected elements 同时相交即失败，合法组合必须共享显式 `intentional_composite_id`。
- background 不参与遮挡；transition 的层级必须低于 caption。
- scene 顺序、时间、motion plan SHA 和 Profile SHA 必须保持新鲜。
