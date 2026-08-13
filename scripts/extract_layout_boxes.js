/**
 * extract_layout_boxes.js — 在 Storyboard 预览页里自动导出 layout-boxes.json。
 *
 * 用途：替代“人工从 DOM 抄 swept bbox → 截图检查 → 手改坐标”的循环。
 * 在浏览器控制台加载本文件后调用 extractLayoutBoxes(config)，它会沿 paused
 * GSAP timeline 按步长采样，读取每个元素的 getBoundingClientRect()，把
 * 视口坐标换算到 config.canvas 声明的画布，输出符合 references/layout-box-schema.md
 * 的 JSON（base box = 元素入场时刻几何，swept_bbox = 全运动路径并集）。
 *
 * config = {
 *   timeline: gsapTimeline,            // 必须是 paused、seek-safe 的主 timeline
 *   stageSelector: "#stage",           // 画布根元素
 *   canvas: { width: <profile width>, height: <profile height> },
 *   profile: { id: "<profile>", sha256: "<sha>" },
 *   sampleStep: 0.1,                   // 采样步长（秒）
 *   motionPlan: { path: ".hyperframes/semantic-motion.json", sha256: "<sha>" },
 *   scenes: [{
 *     id: "01-hook", start_s: 1.0, end_s: 9.2,
 *     elements: [{ id: "hero-media", role: "illustration", selector: ".s01 .hero",
 *                  protected: true, animated: true, semantic_beat_id: null,
 *                  intentional_composite_id: null }]
 *   }]
 * };
 *
 * 返回完整文档并同时触发 JSON 下载；status 保持 draft、actual_dom_verified=true，
 * 之后跑 scripts/approve_if_clean.py --kind layout 与 validate_layout_boxes.py。
 */
function extractLayoutBoxes(config) {
  const tl = config.timeline;
  if (!tl || typeof tl.time !== "function") throw new Error("config.timeline must be a GSAP timeline");
  const stage = document.querySelector(config.stageSelector || "#stage");
  if (!stage) throw new Error("stage element not found: " + config.stageSelector);
  if (!config.canvas || !(config.canvas.width > 0) || !(config.canvas.height > 0)) {
    throw new Error("config.canvas with positive width/height is required");
  }
  const canvas = config.canvas;
  const step = config.sampleStep || 0.1;
  const wasPaused = tl.paused();
  tl.pause();
  const originalTime = tl.time();

  const toCanvas = (rect) => {
    const stageRect = stage.getBoundingClientRect();
    const sx = canvas.width / stageRect.width;
    const sy = canvas.height / stageRect.height;
    return {
      x: (rect.left - stageRect.left) * sx,
      y: (rect.top - stageRect.top) * sy,
      width: rect.width * sx,
      height: rect.height * sy,
    };
  };

  const visible = (el) => {
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (parseFloat(style.opacity) < 0.01) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0.5 && r.height > 0.5;
  };

  const scenes = (config.scenes || []).map((scene) => {
    const elements = scene.elements.map((spec) => {
      let union = null;
      let firstSeen = null;
      let lastSeen = null;
      let baseBox = null;
      let zIndex = 0;
      for (let t = scene.start_s; t <= scene.end_s + 1e-9; t += step) {
        tl.time(Math.min(t, tl.duration()));
        // 强制同步布局
        void document.body.offsetHeight;
        const el = document.querySelector(spec.selector);
        if (!el || !visible(el)) continue;
        const box = toCanvas(el.getBoundingClientRect());
        const z = parseInt(getComputedStyle(el).zIndex, 10);
        if (!Number.isNaN(z)) zIndex = Math.max(zIndex, z);
        if (firstSeen === null) {
          firstSeen = t;
          baseBox = box;
        }
        lastSeen = t;
        union = union
          ? {
              x: Math.min(union.x, box.x),
              y: Math.min(union.y, box.y),
              x2: Math.max(union.x2, box.x + box.width),
              y2: Math.max(union.y2, box.y + box.height),
            }
          : { x: box.x, y: box.y, x2: box.x + box.width, y2: box.y + box.height };
      }
      const round = (v) => Math.round(v * 10) / 10;
      if (!union) {
        return { id: spec.id, role: spec.role, error: "element never visible in scene window", selector: spec.selector };
      }
      return {
        id: spec.id,
        role: spec.role,
        shape: spec.shape || "rect",
        x: round(baseBox.x),
        y: round(baseBox.y),
        width: round(baseBox.width),
        height: round(baseBox.height),
        swept_bbox: {
          x: round(union.x),
          y: round(union.y),
          width: round(union.x2 - union.x),
          height: round(union.y2 - union.y),
        },
        start_s: round(firstSeen),
        end_s: round(lastSeen + step),
        protected: spec.protected === true,
        animated: spec.animated !== false,
        z_index: zIndex,
        semantic_beat_id: spec.semantic_beat_id || undefined,
        intentional_composite_id: spec.intentional_composite_id || null,
        selector: spec.selector,
      };
    });
    return { id: scene.id, start_s: scene.start_s, end_s: scene.end_s, elements };
  });

  tl.time(originalTime);
  if (!wasPaused) tl.play();

  const doc = {
    schema_version: 1,
    status: "draft",
    actual_dom_verified: true,
    extraction: { tool: "extract_layout_boxes.js", sample_step_s: step, extracted_at: new Date().toISOString() },
    canvas,
    profile: config.profile || null,
    motion_plan: config.motionPlan || { path: ".hyperframes/semantic-motion.json", sha256: "" },
    scenes,
  };

  const errors = scenes.flatMap((s) => s.elements.filter((e) => e.error).map((e) => `${s.id}/${e.id}: ${e.error}`));
  if (errors.length) console.warn("extract_layout_boxes: unresolved elements:\n" + errors.join("\n"));

  const blob = new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "layout-boxes.json";
  a.click();
  URL.revokeObjectURL(a.href);
  return doc;
}

if (typeof window !== "undefined") window.extractLayoutBoxes = extractLayoutBoxes;
