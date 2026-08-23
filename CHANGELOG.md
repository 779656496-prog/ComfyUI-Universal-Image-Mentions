# v4.2.4 Mask Canvas Hotfix

- Fixed the Reference Mask editor showing a solid black rectangle over the source image.
- Split the visible translucent paint overlay from the actual black/white mask data canvas.
- Existing masks are restored when reopening the editor through a dedicated safe mask-file endpoint.
- Mask save now validates the server response.
- Reference image preview load failures now report an explicit error instead of opening a blank/black editor.

# Changelog

## 4.2.4 - Core scaler schema compatibility hotfix

- Fixed Flux/Klein dynamic reference injection failing validation with `ImageScaleToTotalPixels: Required input is missing: resolution_steps` on newer ComfyUI builds.
- Dynamic library references now use plugin-owned `UniversalMentionScaleToTotalPixels` instead of constructing the mutable ComfyUI core scaler API node.
- Preserves `upscale_method`, `megapixels`, and `resolution_steps` from the existing reference chain; defaults `resolution_steps` to `1` for old workflows.
- Added a torch interpolation fallback if `comfy.utils.common_upscale` is unavailable in a future build.
- Added regression tests for both old and new scaler schemas.

## 4.2.2 - Frontend Load & Stability Hotfix
- Fixed fatal JavaScript parse error in V4.2 audit-log UI (`lines.join("\n")`).
- Fixed completed braced/quoted mentions reopening autocomplete after selection.
- Debounced DOM MutationObserver scans to reduce canvas/UI churn.
- Escaped mention/control HTML rendered by the V4 chip layer.
- Audit auto-retry now prefers the prompt editor associated with the last bind report.
- Cleans execution-image state and caps retained retry state.
- Includes V4.2.1 mouse/keyboard selection hotfix.

# Changelog

## 4.2.1 — Selection Interaction Hotfix

- 修复 @ 图片候选列表“能显示但鼠标点不下去”：弹窗不再在 capture 阶段截断 pointerdown。
- 修复 Subgraph / 第三方 Prompt Widget 中 Enter/Tab/方向键可能被 ComfyUI 先消费：增加 window capture，并在弹窗打开时以 active Prompt 为准。
- 修复 V4 Chip 控制菜单同类 capture-phase pointerdown 截断问题。
- 保留画布事件隔离：候选项/控制项先收到事件，再阻止事件向 ComfyUI Canvas 冒泡。

## 4.2.0 — Audit Engine

- Fixed Result Audit to consume Relationship Graph `relations`; legacy `transfers` remains supported.
- Added attribute-specific multi-dimensional visual audits for clothing, pose/motion, identity, scene, style, product/object and general reference.
- Added per-dimension score/confidence and `PASS / FAIL / INCONCLUSIVE / UNTRACEABLE / ERROR` states.
- Audit decisions exclude dimensions below the confidence gate, so uncertain low-score guesses cannot by themselves trigger retry.
- Added `audit_min_confidence`, `audit_critical_floor` and `vision_min_confidence` gates.
- Vision semantic compilation now drops fields below the configured confidence threshold.
- Added automatic identity-preservation audit when transferring non-identity attributes.
- Auto-correction now targets only reliably failed dimensions.
- Added previous-run score and score delta for retry comparisons.
- Added comparison cache and bounded `.uim/audit_log.jsonl` history with `/uim/audit/log` endpoint.
- Added V4.2 frontend Audit settings, richer summary/toast, relation table diagnostics and recent-audit log button.
- Includes all V4.1 Reliability changes.

## 4.1.0 — Reliability

- Added stable image IDs, rename aliases, Adapter capability validation, truthful Native/Semantic strength and Native/Preprocess Mask modes, Run/Retry Guard, per-run reports and deeper topology tracing.

## 4.0.0 — Multimodal Canvas

- Added image chips, per-reference controls, Mask painter, drag order, Adapter Wizard, Vision Reader and Result Audit.
