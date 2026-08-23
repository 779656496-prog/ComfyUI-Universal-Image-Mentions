# ComfyUI-Universal-Image-Mentions V4.2.4 — Audit Engine + Selection Hotfix

V4.2 **完整包含 V4.1 Reliability**，并把 Result Audit 从“单总分比较”升级为**属性/维度级审查引擎**。

## 核心链路

`V4.1 可靠绑定 → Generate → Relationship Graph → Attribute Audit → Confidence Gate → Failed-Dimension Correction → Run Delta → 可选 Retry`

## 1. 修复 Relationship Audit 实际读取问题

Relationship Graph 的正式字段是 `relations`。V4.2 Audit Engine 直接读取 `relations`，同时兼容旧 `transfers` 字段。这样 `@2.CLOTHING → @1.CLOTHING` 会真正进入审查链，而不是只生成关系图却没有被 Audit 使用。

## 2. 多维度审查

不同任务使用不同维度。例如 CLOTHING 会拆成：

- garment category
- color
- silhouette / fit
- neckline
- sleeves
- material / texture
- pattern / details

POSE、IDENTITY、SCENE、STYLE、PRODUCT_OBJECT 也有各自维度。最终报告不仅给 overall score，还返回每个维度的 `score / confidence / status / missing / correction`。

## 3. Confidence Gate

V4.2 新增：

- `audit_min_confidence`（默认 0.55）
- `audit_critical_floor`（默认 0.58）
- `vision_min_confidence`（默认 0.55）

视觉信息低于可信度门槛时记为 `INCONCLUSIVE`，不因为 VLM “看不清却猜错”而直接触发失败。Vision Semantic Reader 也会过滤低可信度字段，不把它们编译进增强 Prompt。

## 4. 自动检查身份保持

当执行：

`@2.CLOTHING → @1.CLOTHING`

除了检查衣服是否接近 @2，V4.2 还会自动检查生成结果是否保持 @1 的 `IDENTITY`。换衣失败和“衣服对了但脸换了”会被分开报告。

## 5. 只修失败维度

如果服装审查结果是：

- category PASS
- color PASS
- silhouette PASS
- **neckline FAIL**
- sleeves PASS
- material PASS

自动 correction 只强调 neckline 等真实失败项，而不是把整段 Prompt 全部重复加强。低可信度失败项不会进入 correction。

## 6. Retry 前后对比

每个审查记录关联 V4.1 Run ID。相同 `root_run_id` 的下一次 Retry 会显示：

- `previous_overall_score`
- `score_delta`

例如 `72% → 86% (+14pp)`，可以直接判断自动修正是否真的改善结果。

## 7. Audit Cache / Log

- 比较缓存：`.uim/audit_compare_cache_v1.json`
- 审查日志：`.uim/audit_log.jsonl`
- HTTP：`GET /uim/audit/log?limit=...`

Prompt Chip 工具栏新增 `≋`，可查看最近审查记录。`✓` 设置页现在可配置总体阈值、最低可信度、关键失败线、最大自动重试次数。

## 8. 状态含义

- `PASS`：可靠维度达到阈值。
- `FAIL`：有足够可信度且明确未达到目标。
- `INCONCLUSIVE`：可见性/置信度不足，不当作确定失败。
- `UNTRACEABLE`：找不到对应参考图真实路径。
- `ERROR`：Vision 比较调用失败。

## 9. V4.1 Reliability 全部保留

包括：Stable Image ID、历史别名、Adapter Capability Validation、`Strength N/S`、`Mask N/P`、Run/Retry Guard、深层执行图追踪、Strict Bind Validator、H3/Flux-Klein/LTX/KREA2 适配。

## 10. Vision 要求

**绑定本身不需要额外 VLM。** 但 V4.2 Result Audit 要真正比较最终结果与参考图，因此需要配置可用的 OpenAI-compatible Vision endpoint/model。没有 VLM 时不会伪造评分。

## 11. 安装

解压到：

`ComfyUI/custom_nodes/ComfyUI-Universal-Image-Mentions/`

覆盖后：完全关闭 ComfyUI → 重启 → 浏览器 `Ctrl + F5`。

如果你要使用完整功能，建议直接安装 V4.2；V4.1 主要提供给只想要可靠绑定、暂时不需要新版 Audit Engine 的环境。


## 4.2.1 交互热修

如果 @ 图片列表可以正常出现但点击候选无反应，或 Enter/Tab 无法确认，请使用 4.2.1。该版本修复了弹窗 capture 阶段事件截断，并把候选键盘确认提升到 window capture。

## V4.2.4：新版 ComfyUI 缩放节点兼容修复

如果运行时出现：

```text
ImageScaleToTotalPixels 缺少必需的输入：resolution_steps
```

V4.2.4 会在包含 UIM `@` 引用的 Queue 中自动修复旧执行图缺失的 `resolution_steps=1`，并且 UIM 动态追加的 Flux/Klein 参考图不再直接创建 ComfyUI 核心 `ImageScaleToTotalPixels`，而改用插件自己的稳定缩放节点，降低以后 ComfyUI 核心接口升级造成的再次失效。

