# ComfyUI-Universal-Image-Mentions v4.2.4

> Model-agnostic **@image mention** routing for ComfyUI — write `@1`, `@2`, `@黑色西装` in your prompt and the plugin automatically binds reference images to the correct slots.
>
> No model, GPU, VRAM, LLM, or third-party custom-node dependency required for core binding.

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-4.2.4-green.svg)](#)

---

## Table of Contents

- [What It Does](#what-it-does)
- [Key Features](#key-features)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Usage Guide](#usage-guide)
  - [1. Adding Images to the Library](#1-adding-images-to-the-library)
  - [2. Writing @Mentions in Prompts](#2-writing-mentions-in-prompts)
  - [3. Auto-Bind Mode (Recommended)](#3-auto-bind-mode-recommended)
  - [4. Manual Router Mode](#4-manual-router-mode)
  - [5. Attribute Roles](#5-attribute-roles)
  - [6. Per-Reference Controls](#6-per-reference-controls)
  - [7. Reference Mask](#7-reference-mask)
  - [8. Result Audit (Optional, V4.2)](#8-result-audit-optional-v42)
  - [9. Vision Semantic Reader (Optional)](#9-vision-semantic-reader-optional)
- [Node List](#node-list)
- [Configuration](#configuration)
- [Example Prompts](#example-prompts)
- [Testing](#testing)
- [Changelog](#changelog)
- [License](#license)

---

## What It Does

Instead of manually wiring image inputs to model nodes, you simply type `@1`, `@2`, `@人物正面` etc. in any CLIPTextEncode / prompt widget. The plugin:

1. Parses all `@mentions` in the prompt text
2. Resolves each mention to an image in your library (by slot number, alias, or filename)
3. Automatically injects those images into the connected model node's reference slots
4. (Optional) Uses a vision model to audit whether the generated result actually matches your references

**Works with**: MiniMax H3 (T2VA/I2VA/FL2VA/Ref2VA/L2VA), Flux-Klein, LTX-Video, KREA2, and any third-party node with an adapter manifest.

## Key Features

| Feature | Description |
|---------|-------------|
| **@ Mention Autocomplete** | Type `@` in any prompt → popup shows your image library with thumbnails |
| **Auto-Bind** | Automatically injects reference images into connected model nodes — no manual wiring |
| **16-Slot Router** | Up to 16 reference images per prompt via the Universal @Image Router node |
| **Attribute Roles** | `@2.CLOTHING`, `@3.POSE` — route specific attributes (identity, clothing, pose, style, scene, product) |
| **Per-Reference Controls** | Per-image strength (N/S), mask (N/P), drag-to-reorder |
| **Reference Mask** | Paint a mask on the reference image to focus the model's attention |
| **Stable Image IDs** | Renaming an image doesn't break existing prompts — aliases persist |
| **Adapter Wizard** | Auto-detects connected model nodes and validates their capabilities |
| **Result Audit (V4.2)** | Multi-dimensional attribute audit with confidence gating and automatic retry |
| **Vision Reader (V3.1)** | Optional VLM reads reference images and compiles semantic descriptions into the prompt |
| **Zero Dependencies** | No pip install needed — uses only Python stdlib + ComfyUI core |

---

## Installation

### Method 1: Manual (Recommended)

1. Copy the `ComfyUI-Universal-Image-Mentions` folder into:
   ```
   ComfyUI/custom_nodes/ComfyUI-Universal-Image-Mentions/
   ```
2. **Completely close ComfyUI** (not just refresh — stop the process)
3. Restart ComfyUI
4. In your browser, press `Ctrl + F5` (hard refresh) to reload the frontend
5. Look in the ComfyUI console — you should see the plugin load message

### Method 2: Install Scripts

- **Windows**: Double-click `install_windows.bat`
- **Linux/Mac**: Run `sh install_linux_mac.sh`

> **No `pip install` required.** `requirements.txt` is intentionally empty — the plugin uses only Python standard library plus what ComfyUI already provides.

### Verify Installation

1. Add a `CLIPTextEncode` (or any prompt) node to your workflow
2. Type `@` in the text field — an autocomplete popup should appear
3. If you see the popup, the plugin is working

---

## Quick Start

```
Step 1: Put images in the library folder (see below)
Step 2: Add your model node (e.g. MiniMax H3, Flux-Klein) to the workflow
Step 3: Connect a CLIPTextEncode to the model's prompt input
Step 4: Type "@1 是基础人物，把衣服换成@2的衣服" in the prompt
Step 5: Queue — the plugin auto-binds @1 and @2 to the model's reference slots
```

---

## Usage Guide

### 1. Adding Images to the Library

The plugin uses a dedicated image library folder, intentionally separate from ComfyUI's `input/` directory:

**Default location (portable install):**
```
<portable-root>/ComfyUI_Mention_Images/
```

That is, if your ComfyUI is at `D:/AI/ComfyUI/`, the library is at `D:/AI/ComfyUI_Mention_Images/`.

**Custom location:** Set the environment variable `UIM_LIBRARY_DIR` to any path:
```bash
# Windows
set UIM_LIBRARY_DIR=D:\my_images

# Linux/Mac
export UIM_LIBRARY_DIR=/home/user/my_images
```

**Supported formats:** `.png`, `.jpg`, `.jpeg`, `.webp`, `.bmp`, `.tif`, `.tiff`

**Organizing images:**
- Put images directly in the library root, or in subfolders
- The `@` autocomplete popup shows all images recursively
- You can also drag-and-drop images directly into the `@` popup

### 2. Writing @Mentions in Prompts

Type `@` in any prompt text field. The autocomplete popup appears:

| Syntax | Example | Meaning |
|--------|---------|---------|
| `@<number>` | `@1`, `@2` | Reference image by slot number |
| `@<name>` | `@黑色西装` | Reference image by filename (without extension) |
| `@{name}` | `@{人物 正面}` | Reference by name with spaces |
| `@"name"` | `@"人物 正面"` | Reference by quoted name |
| `@<num>.<ROLE>` | `@2.CLOTHING` | Reference image with specific attribute role |

**In the popup:**
- Type to filter by name
- Click or `Enter`/`Tab` to select
- Arrow keys to navigate
- Drag images to reorder slots
- `+` button to upload new images directly

### 3. Auto-Bind Mode (Recommended)

This is the default and recommended mode — **no extra nodes needed**.

1. Add your model node (e.g. MiniMax H3 I2VA) to the canvas
2. Connect a `CLIPTextEncode` → model's `prompt` input
3. Type your prompt with `@mentions`:
   ```
   @1 是唯一基础人物，保持脸、五官、发型和身体比例。
   让@1把当前衣服换成@2的衣服款式、剪裁、材质和颜色，
   但不要继承@2的脸、发型、体型和身份。
   ```
4. **Queue** — the plugin intercepts the prompt graph before execution and:
   - Resolves each `@mention` to a real image file
   - Injects the images into the model node's reference image slots
   - Validates adapter capabilities (does the model support multi-reference? masks? strength?)

You'll see a bind status indicator (green/red dot) near the prompt widget showing whether binding succeeded.

### 4. Manual Router Mode

If you prefer explicit control, use the **Universal @Image Router | 通用@图片｜16图** node:

1. Add `UniversalAtImageRouter16` to the canvas
2. Wire image outputs to the model node's reference inputs
3. Connect the Router's output to the model's prompt (or use it alongside a CLIPTextEncode)
4. The Router provides up to 16 image slots

**When to use Manual mode:**
- Your model node isn't auto-detected by the adapter system
- You want explicit visual control over which image goes where
- You're using a very custom workflow

### 5. Attribute Roles

Assign semantic roles to references for the Relationship Graph and Audit Engine:

| Role | Keyword Examples | Description |
|------|-----------------|-------------|
| `IDENTITY` | 身份, 人物, 脸, identity, face | Face, body, identity |
| `CLOTHING` | 衣服, 服装, clothing, outfit | Garments, accessories |
| `POSE_MOTION` | 姿势, 动作, pose, motion | Body pose, gestures |
| `PRODUCT_OBJECT` | 产品, 商品, product, object | Products, packaging |
| `STYLE` | 风格, 画风, style, aesthetic | Art style, lighting |
| `SCENE` | 背景, 场景, background, scene | Environment, backdrop |
| `EDIT` | 改成, 换成, change, replace | Transfer/edit directive |

**Usage in prompt:**
```
@2.CLOTHING → @1.CLOTHING
```
This tells the Audit Engine to check whether @1's clothing in the result matches @2's clothing in the reference.

The plugin also auto-detects roles from natural language:
```
@1 是基础人物，衣服参考@2，动作用@3
```
The plugin infers: @2 = CLOTHING, @3 = POSE_MOTION automatically.

### 6. Per-Reference Controls

When using V4 Multimodal Canvas (image chips appear below the prompt):

| Control | Description |
|---------|-------------|
| **Strength N/S** | Native strength (model's built-in) vs Semantic strength (prompt-level emphasis) |
| **Mask N/P** | Native mask (model API) vs Preprocess mask (applied before injection) |
| **Drag reorder** | Click and drag image chips to change slot order |
| **Remove** | Click `×` on a chip to remove it from the prompt |

### 7. Reference Mask

Paint a mask on any reference image to focus the model's attention:

1. Click the mask icon on an image chip
2. The mask editor opens — paint over the area you want to focus on
3. The mask is saved and applied during generation
4. Mask data is stored separately from the visible overlay (fixed in V4.2.4)

### 8. Result Audit (Optional, V4.2)

The Audit Engine compares your generated result against the reference images across multiple dimensions:

**How it works:**
1. After generation, the audit engine sends the result + reference images to a vision model
2. It checks per-dimension scores (e.g., for CLOTHING: garment category, color, silhouette, neckline, sleeves, material, pattern)
3. Each dimension gets: `score`, `confidence`, `status` (PASS/FAIL/INCONCLUSIVE/UNTRACEABLE/ERROR)
4. Low-confidence dimensions are marked INCONCLUSIVE, not FAIL
5. If any reliable dimension fails, auto-correction adds targeted fix instructions to the prompt
6. Retry shows score delta (e.g., `72% → 86% (+14pp)`)

**Audit states:**
| State | Meaning |
|-------|---------|
| `PASS` | Dimension meets threshold with sufficient confidence |
| `FAIL` | Dimension clearly below threshold with sufficient confidence |
| `INCONCLUSIVE` | Confidence too low to judge — doesn't trigger retry |
| `UNTRACEABLE` | Reference image file not found |
| `ERROR` | Vision model call failed |

**Audit settings (via `✓` button in chip toolbar):**
- `audit_threshold` — minimum overall score to PASS (default: configurable)
- `audit_min_confidence` — minimum confidence to count a dimension (default: 0.55)
- `audit_critical_floor` — hard floor for critical dimensions (default: 0.58)
- `max_auto_retries` — maximum automatic retry attempts

**Audit log:** Stored in `.uim/audit_log.jsonl`, viewable via the `≋` button in the chip toolbar.

> **Note:** Result Audit requires a configured OpenAI-compatible vision model. Core binding and auto-bind work without any VLM.

### 9. Vision Semantic Reader (Optional)

The Vision Reader uses a VLM to "read" your reference images and compile their semantic descriptions into the prompt text:

1. Copy `vision_config.example.json` to `vision_config.json`
2. Configure your vision endpoint:
   ```json
   {
     "mode": "AUTO",
     "url": "http://127.0.0.1:11434/v1",
     "model": "your-vision-model-name",
     "required": false,
     "timeout": 90
   }
   ```
3. The reader analyzes each reference image and extracts attributes (clothing color, pose, style, etc.)
4. Low-confidence fields are filtered out
5. The compiled description is injected into the prompt as additional context

> Works with any OpenAI-compatible vision API (Ollama, LM Studio, OpenAI, etc.)

---

## Node List

| Node Class | Display Name | Purpose |
|-----------|-------------|---------|
| `UniversalAtImageRouter16` | Universal @Image Router｜通用@图片｜16图 | Main router node, 16 image slots |
| `UniversalMentionImageByIndex` | Universal @Image｜按编号取图 | Get image by slot number |
| `UniversalMentionImageByAlias` | Universal @Image｜按名称取图 | Get image by alias name |
| `UniversalMentionPromptAdapter` | Universal @Image｜提示词标签适配器 | Prompt tag adapter |
| `UniversalMentionLibraryInfo` | Universal @Image Library｜图库信息 | Library info / management |
| `UniversalMentionSemanticPreview` | Universal @Image｜语义绑定预览 | Preview semantic binding |
| `UniversalMentionVisionAnalyze` | Universal @Image｜V4 Vision 读图分析 | Vision analysis node |
| `UniversalMentionVisionStatus` | Universal @Image｜V4 Vision 状态 | Vision service status |
| `UniversalMentionV3Status` | Universal @Image｜V4 最后绑定状态 | Last bind status report |
| `UniversalMentionScaleToTotalPixels` | Universal @Image｜内部稳定缩放器 | Internal stable scaler (V4.2.4) |
| `UniversalMentionApplyMask` | Universal @Image｜V4 参考区域 Mask | Apply reference mask |
| `UniversalMentionLoadOne` | Universal @Image｜内部加载器 | Internal image loader |
| `UniversalMentionTextLiteral` | Universal @Image｜内部文本适配 | Internal text adapter |

> Most of these are internal nodes used by the auto-bind system. You typically only need to add `UniversalAtImageRouter16` if using manual mode.

---

## Configuration

### Adapter Manifest (for third-party model nodes)

If you're using a model node that isn't auto-detected, create an adapter manifest:

1. Copy `adapter_manifest.example.json` to `adapter_manifest.json`
2. Define your node's slot names, strength/mask mappings, and capabilities:

```json
{
  "MyThirdPartyReferenceNode": {
    "name": "MY_NODE_ADAPTER",
    "slots": ["image_1", "image_2", "image_3"],
    "max_refs": 3,
    "prompt_input": "prompt",
    "native_vision": true,
    "strength_map": {
      "image_1": "image_1_strength",
      "image_2": "image_2_strength"
    },
    "mask_map": {
      "image_1": "image_1_mask"
    },
    "capabilities": {
      "supports_reference_image": true,
      "supports_multi_reference": true,
      "supports_native_strength": true,
      "supports_native_mask": true,
      "confidence": "USER_VERIFIED",
      "reference_semantics": ["IDENTITY", "CLOTHING", "STYLE", "GENERAL_REFERENCE"]
    }
  }
}
```

### Vision Config (for Audit / Semantic Reader)

Copy `vision_config.example.json` to `vision_config.json` and configure:

| Field | Description |
|-------|-------------|
| `mode` | `"AUTO"` (auto-detect) or specific mode |
| `url` | OpenAI-compatible API endpoint |
| `model` | Vision model name (e.g. `llava-v1.6-vicuna-7b`) |
| `api_key_env` | Environment variable name for API key (empty = no key) |
| `required` | `false` = plugin works without VLM (recommended) |
| `timeout` | Request timeout in seconds |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `UIM_LIBRARY_DIR` | `<portable-root>/ComfyUI_Mention_Images/` | Custom image library path |

---

## Example Prompts

See `examples/EXAMPLE_PROMPTS.txt` for more. Quick examples:

**Three-reference character:**
```
@1 是唯一基础人物，保持脸、五官、发型、肤色和身体比例。
让@1把当前衣服换成@2的衣服款式、剪裁、材质和颜色，
但不要继承@2的脸、发型、体型和身份。
@3只参考动作和姿势，不继承@3的身份和服装。
```

**Reverse expression:**
```
把@2的衣服给@1穿，保持@1的脸、发型和身份。
```

**Role assignment:**
```
人物用@1，衣服参考@2，动作用@3，背景保持@1。
```

**With library name:**
```
@1 是基础人物，把@1的衣服换成@黑色西装的衣服款式，保持@1的脸和身份。
```

---

## Testing

The plugin includes a test suite. Run from inside the plugin directory:

```bash
# Run all tests
python -m pytest tests/

# Or run individual test files
python tests/test_parser_standalone.py
python tests/test_v42_audit_engine.py
```

Tests cover: parser, auto-bind, connected slots, H3 alias, reference latent, relationship validator, vision reader, multimodal controls, reliability, audit engine, selection hotfix, frontend load guard, scaler schema compatibility, and mask canvas hotfix.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for full version history.

### v4.2.4 — Mask Canvas Hotfix + Core Scaler Compatibility
- Fixed Reference Mask editor showing solid black rectangle
- Split visible translucent overlay from actual mask data canvas
- Fixed `ImageScaleToTotalPixels: resolution_steps` validation error on newer ComfyUI builds
- Dynamic references now use plugin-owned `UniversalMentionScaleToTotalPixels` instead of core scaler

### v4.2.2 — Frontend Load & Stability Hotfix
- Fixed fatal JS parse error in audit-log UI
- Fixed completed mentions reopening autocomplete
- Debounced DOM MutationObserver scans
- Escaped mention/control HTML

### v4.2.1 — Selection Interaction Hotfix
- Fixed @ image candidate list click not registering
- Fixed Enter/Tab consumed by ComfyUI canvas in Subgraph/third-party widgets
- Window capture for keyboard confirmation

### v4.2.0 — Audit Engine
- Multi-dimensional attribute audit (clothing, pose, identity, scene, style, product)
- Per-dimension score/confidence with PASS/FAIL/INCONCLUSIVE/UNTRACEABLE/ERROR states
- Confidence gate prevents low-confidence guesses from triggering retry
- Automatic identity-preservation audit for non-identity transfers
- Retry score delta comparison
- Audit cache + bounded JSONL log

### v4.1.0 — Reliability
- Stable image IDs, rename aliases
- Adapter capability validation
- Strength N/S, Mask N/P modes
- Run/Retry Guard, per-run reports

### v4.0.0 — Multimodal Canvas
- Image chips, per-reference controls
- Mask painter, drag order
- Adapter Wizard, Vision Reader, Result Audit

---

## License

MIT License — see [LICENSE](LICENSE) for details.

## Acknowledgments

This plugin is designed to be model-agnostic and works with ComfyUI 0.32+ (and compatible forks). It requires no external dependencies beyond what ComfyUI already provides.
