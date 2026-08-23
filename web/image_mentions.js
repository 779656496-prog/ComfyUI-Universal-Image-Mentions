import { app } from "../../scripts/app.js";

const EXTENSION_NAME = "Universal.ImageMentions.V4.BaseMention";
const ROUTER_CLASS = "UniversalAtImageRouter16";
const CACHE_MS = 1800;
const MAX_RESULTS = 96;

let imageCache = { at: 0, images: [], path: "", pending: null };
let popup = null;
let active = null;
let fileInput = null;
let styleInstalled = false;
let actionBusy = false;
let libraryPollTimer = null;
let lastBindState = null;

async function uimPrompt(title, message, defaultValue = "") {
  try { const d = app?.extensionManager?.dialog; if (d?.prompt) return await d.prompt({ title, message, defaultValue }); } catch {}
  return window.prompt(`${title}\n${message}`, defaultValue);
}
async function uimConfirm(title, message) {
  try { const d = app?.extensionManager?.dialog; if (d?.confirm) return await d.confirm({ title, message }); } catch {}
  return window.confirm(`${title}\n${message}`);
}

function installStyle() {
  if (styleInstalled) return;
  styleInstalled = true;
  const style = document.createElement("style");
  style.textContent = `
    .uim-mention-popup{position:fixed;z-index:1000000;min-width:320px;max-width:min(620px,calc(100vw - 20px));max-height:min(420px,calc(100vh - 20px));display:flex;flex-direction:column;pointer-events:auto;background:var(--comfy-menu-bg,#202124);color:var(--input-text,#eee);border:1px solid var(--border-color,#555);border-radius:10px;box-shadow:0 12px 34px rgba(0,0,0,.38);padding:6px;font:13px/1.35 system-ui,-apple-system,Segoe UI,sans-serif}
    .uim-mention-popup[hidden]{display:none!important}
    .uim-mention-head{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:4px 6px 6px;flex-wrap:wrap}
    .uim-mention-title{font-weight:700;white-space:nowrap}
    .uim-mention-actions{display:flex;gap:5px;align-items:center;flex-wrap:wrap}
    .uim-mention-action{border:1px solid var(--border-color,#666);background:var(--comfy-input-bg,#353535);color:inherit;border-radius:6px;padding:4px 8px;cursor:pointer;font:inherit;font-size:11px;min-height:28px}
    .uim-mention-action:hover{filter:brightness(1.1)}
    .uim-mention-action:disabled{opacity:.55;cursor:wait}
    .uim-mention-path{padding:0 7px 6px;color:var(--descrip-text,#aaa);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-bottom:1px solid color-mix(in srgb,var(--border-color,#555) 60%,transparent)}
    .uim-mention-path strong{color:var(--input-text,#ddd);font-weight:600}
    .uim-mention-list{display:flex;flex-direction:column;gap:2px;overflow:auto;overscroll-behavior:contain;padding-top:5px;min-height:70px;max-height:330px;scrollbar-gutter:stable;touch-action:pan-y}
    .uim-mention-section{position:sticky;top:0;z-index:1;padding:5px 8px 3px;background:var(--comfy-menu-bg,#202124);color:var(--descrip-text,#aaa);font-size:10px;font-weight:700;letter-spacing:.03em;border-bottom:1px solid color-mix(in srgb,var(--border-color,#555) 45%,transparent)}
    .uim-bind-state{display:flex;align-items:center;gap:6px;padding:5px 7px;border-bottom:1px solid color-mix(in srgb,var(--border-color,#555) 50%,transparent);font-size:10px;color:var(--descrip-text,#aaa)}
    .uim-bind-dot{width:7px;height:7px;border-radius:50%;background:#888;flex:none}.uim-bind-dot.ok{background:#43a047}.uim-bind-dot.blocked{background:#e53935}.uim-bind-dot.idle{background:#888}
    .uim-mention-item{display:grid;grid-template-columns:48px minmax(0,1fr);align-items:center;gap:9px;width:100%;min-height:54px;padding:4px 7px;border:0;border-radius:7px;background:transparent;color:inherit;text-align:left;cursor:pointer}
    .uim-mention-item:hover,.uim-mention-item[aria-selected="true"]{background:var(--comfy-input-bg,#353535);outline:1px solid var(--border-color,#666)}
    .uim-mention-thumb{width:46px;height:46px;object-fit:cover;border-radius:5px;background:#111;border:1px solid rgba(255,255,255,.12)}
    .uim-mention-slotbadge{width:46px;height:46px;border-radius:5px;background:var(--comfy-input-bg,#353535);border:1px solid var(--border-color,#666);display:grid;place-items:center;font-size:19px;font-weight:800;color:var(--input-text,#eee)}
    .uim-mention-name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}
    .uim-mention-sub{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--descrip-text,#aaa);font-size:11px;margin-top:2px}
    .uim-mention-empty{padding:14px 10px;color:var(--descrip-text,#aaa)}
    .uim-mention-help{padding:6px 7px 2px;color:var(--descrip-text,#888);font-size:10px;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
    .uim-mention-key{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;border:1px solid var(--border-color,#666);border-bottom-width:2px;border-radius:4px;padding:0 4px;font-size:10px}
    .uim-mention-popup.uim-drop{outline:2px dashed var(--border-color,#888);outline-offset:-5px}
    .uim-toast{position:fixed;right:14px;bottom:14px;z-index:1000001;max-width:min(420px,calc(100vw - 28px));background:var(--comfy-menu-bg,#222);color:var(--input-text,#eee);border:1px solid var(--border-color,#666);border-radius:8px;padding:9px 12px;box-shadow:0 8px 24px rgba(0,0,0,.3);font:12px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}
  `;
  document.head.appendChild(style);
}

function routeCandidates(path) {
  const out = [path];
  const current = window.location.pathname || "/";
  if (current !== "/") {
    const prefix = current.endsWith("/") ? current : current.slice(0, current.lastIndexOf("/") + 1);
    out.push(`${prefix.replace(/\/$/, "")}${path}`);
  }
  return [...new Set(out)];
}

async function requestFallback(path, options = {}) {
  let lastError = null;
  for (const url of routeCandidates(path)) {
    try {
      const res = await fetch(url, options);
      if (!res.ok) {
        let message = `HTTP ${res.status}`;
        try { const data = await res.json(); message = data.error || data.detail || message; } catch {}
        throw new Error(message);
      }
      return res;
    } catch (err) { lastError = err; }
  }
  throw lastError || new Error(`Request failed: ${path}`);
}

function toast(message) {
  installStyle();
  const old = document.querySelector(".uim-toast");
  old?.remove();
  const el = document.createElement("div");
  el.className = "uim-toast";
  el.textContent = String(message || "");
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

function ensureFileInput() {
  if (fileInput) return fileInput;
  fileInput = document.createElement("input");
  fileInput.type = "file";
  fileInput.multiple = true;
  fileInput.accept = "image/png,image/jpeg,image/webp,image/bmp,image/tiff,.png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff";
  fileInput.style.display = "none";
  document.body.appendChild(fileInput);
  fileInput.addEventListener("change", async () => {
    const files = [...(fileInput.files || [])];
    fileInput.value = "";
    if (files.length) await uploadFiles(files);
  });
  return fileInput;
}

function ensurePopup() {
  installStyle();
  if (popup) return popup;
  popup = document.createElement("div");
  popup.className = "uim-mention-popup";
  popup.hidden = true;
  popup.setAttribute("role", "listbox");
  popup.innerHTML = `
    <div class="uim-mention-head">
      <span class="uim-mention-title">@ 图片库</span>
      <span class="uim-mention-actions">
        <button type="button" class="uim-mention-action" data-uim="add">＋ 添加图片</button>
        <button type="button" class="uim-mention-action" data-uim="refresh">刷新</button>
        <button type="button" class="uim-mention-action" data-uim="open">打开目录</button>
        <button type="button" class="uim-mention-action" data-uim="vision">Vision设置</button>
      </span>
    </div>
    <div class="uim-mention-path" title=""><strong>图库：</strong><span>正在读取…</span></div>
    <div class="uim-bind-state"><span class="uim-bind-dot idle"></span><span data-uim="bind-state">V4 Bind Validator：等待执行</span></div>
    <div class="uim-vision-state"><span class="uim-bind-dot idle"></span><span data-uim="vision-state">V4 Vision Reader：正在检测</span></div>
    <div class="uim-mention-list"></div>
    <div class="uim-mention-help"><span>也可以把图片直接拖到这里添加</span><span><span class="uim-mention-key">↑↓</span> <span class="uim-mention-key">Enter</span> <span class="uim-mention-key">Esc</span></span></div>`;
  document.body.appendChild(popup);
  // Keep pointer/wheel interaction inside the mention panel. ComfyUI's canvas
  // also listens for pointer/wheel events; without this, a click or scroll can
  // be interpreted as canvas navigation instead of selecting an @ item.
  // IMPORTANT: do not stop pointerdown in capture phase here. A capture-phase
  // stop on the popup prevents the event from ever reaching the candidate
  // buttons, producing the exact "visible but cannot click" failure. Let the
  // target receive the event first, then stop bubbling before ComfyUI canvas.
  popup.addEventListener("pointerdown", (e) => e.stopPropagation(), false);
  popup.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true, capture: false });

  const add = popup.querySelector('[data-uim="add"]');
  const refresh = popup.querySelector('[data-uim="refresh"]');
  const open = popup.querySelector('[data-uim="open"]');
  const vision = popup.querySelector('[data-uim="vision"]');
  [add, refresh, open, vision].forEach((b) => b.addEventListener("mousedown", (e) => e.preventDefault()));
  add.addEventListener("click", () => ensureFileInput().click());
  refresh.addEventListener("click", async () => {
    if (active?.el) await refreshFor(active.el, true);
    else await fetchImages(true);
  });
  open.addEventListener("click", openLibraryFolder);
  vision.addEventListener("click", configureVision);

  popup.addEventListener("dragover", (e) => {
    if (!e.dataTransfer?.types?.includes("Files")) return;
    e.preventDefault();
    popup.classList.add("uim-drop");
  });
  popup.addEventListener("dragleave", () => popup.classList.remove("uim-drop"));
  popup.addEventListener("drop", async (e) => {
    popup.classList.remove("uim-drop");
    const files = [...(e.dataTransfer?.files || [])];
    if (!files.length) return;
    e.preventDefault();
    await uploadFiles(files);
  });
  return popup;
}

function setButtonsBusy(busy) {
  actionBusy = busy;
  if (!popup) return;
  popup.querySelectorAll(".uim-mention-action").forEach((b) => { b.disabled = !!busy; });
}

async function fetchImages(force = false) {
  const now = Date.now();
  if (!force && imageCache.images.length && now - imageCache.at < CACHE_MS) return imageCache.images;
  if (imageCache.pending) return imageCache.pending;
  imageCache.pending = (async () => {
    try {
      const res = await requestFallback("/uim/images", { cache: "no-store" });
      const data = await res.json();
      imageCache.images = Array.isArray(data.images) ? data.images : [];
      imageCache.path = String(data.library_path || "");
      imageCache.at = Date.now();
      updateLibraryPath();
      return imageCache.images;
    } catch (err) {
      console.warn(`[${EXTENSION_NAME}] image library index unavailable`, err);
      return [];
    }
  })();
  try { return await imageCache.pending; }
  finally { imageCache.pending = null; }
}

function updateLibraryPath() {
  if (!popup) return;
  const box = popup.querySelector(".uim-mention-path");
  const span = box?.querySelector("span");
  if (!box || !span) return;
  const value = imageCache.path || "专用 @图片库尚未创建/不可用";
  span.textContent = value;
  box.title = value;
}

async function refreshBindState() {
  try {
    const res = await requestFallback("/uim/last-bind", { cache: "no-store" });
    lastBindState = await res.json();
  } catch { return; }
  if (!popup) return;
  const label = popup.querySelector('[data-uim="bind-state"]');
  const dot = popup.querySelector('.uim-bind-dot');
  if (!label || !dot) return;
  const st = String(lastBindState?.status || "idle");
  dot.className = `uim-bind-dot ${st === "ok" ? "ok" : st === "blocked" ? "blocked" : "idle"}`;
  if (st === "ok") {
    const reports = Array.isArray(lastBindState?.reports) ? lastBindState.reports : [];
    const bound = reports.filter((x) => x?.status === "bound").length;
    label.textContent = `上次执行绑定检查：通过 · ${bound} 条绑定路径`;
  } else if (st === "blocked") {
    label.textContent = `上次执行绑定检查：已阻止 · ${String(lastBindState?.error || "存在未真实绑定的 @图片").slice(0, 140)}`;
  } else {
    label.textContent = "V4 Bind Validator：等待执行";
  }
}

async function configureVision() {
  try {
    const res = await requestFallback("/uim/vision/config", { cache: "no-store" });
    const data = await res.json();
    const cfg = data?.config || {};
    const modeRaw = await uimPrompt("Vision 设置", "Vision 模式：AUTO / VLM / BASIC / OFF", String(cfg.mode || "AUTO"));
    if (modeRaw == null) return;
    const mode = String(modeRaw || "AUTO").trim().toUpperCase();
    if (!["AUTO","VLM","BASIC","OFF"].includes(mode)) { toast("Vision 模式只能是 AUTO / VLM / BASIC / OFF"); return; }
    let url = String(cfg.url || "");
    let model = String(cfg.model || "");
    let apiKeyEnv = String(cfg.api_key_env || "");
    if (mode === "AUTO" || mode === "VLM") {
      const u = await uimPrompt("Vision 设置", "OpenAI-compatible Vision 地址（本地可不需要密钥）\n例如：http://127.0.0.1:11434/v1", url);
      if (u == null) return; url = String(u).trim();
      const m = await uimPrompt("Vision 设置", "Vision 模型名称（例如你的 Qwen-VL 模型名）", model);
      if (m == null) return; model = String(m).trim();
      const e = await uimPrompt("Vision 设置", "可选：API Key 所在环境变量名（不会保存真实密钥）", apiKeyEnv);
      if (e == null) return; apiKeyEnv = String(e).trim();
    }
    const required = mode === "VLM" ? await uimConfirm("Vision 设置", "如果 VLM 读图失败，是否阻止 Queue？\n确定=阻止；取消=自动退回模型原生读图/基础信息") : false;
    const save = await requestFallback("/uim/vision/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, url, model, api_key_env: apiKeyEnv, required, timeout: Number(cfg.timeout || 90) }),
    });
    const saved = await save.json();
    if (!saved?.ok) throw new Error(saved?.error || "保存失败");
    toast(`Vision 设置已保存：${mode}${model ? ` · ${model}` : ""}`);
    await refreshVisionState();
  } catch (err) {
    toast(`Vision 设置失败：${err?.message || err}`);
  }
}

async function refreshVisionState() {
  if (!popup) return;
  const label = popup.querySelector('[data-uim="vision-state"]');
  const row = label?.closest?.('.uim-vision-state');
  const dot = row?.querySelector?.('.uim-bind-dot');
  if (!label || !dot) return;
  try {
    const res = await requestFallback("/uim/vision/status", { cache: "no-store" });
    const data = await res.json();
    const mode = String(data?.mode || "AUTO");
    const rich = !!data?.rich_semantic_ready;
    dot.className = `uim-bind-dot ${rich ? "ok" : "idle"}`;
    if (rich) {
      label.textContent = `V4 Vision Reader：VLM已就绪 · ${String(data?.model || "vision model")} · 缓存 ${Number(data?.cache_entries || 0)}`;
    } else if (mode === "OFF") {
      label.textContent = "V4 Vision Reader：已关闭；仍保留真实图片绑定";
    } else {
      label.textContent = "V4 Vision Reader：原生模型读图 + 基础像素信息；未配置外部VLM";
    }
  } catch {
    dot.className = "uim-bind-dot idle";
    label.textContent = "V4 Vision Reader：状态接口不可用；不影响@真实绑定";
  }
}

function startLibraryPolling() {
  if (libraryPollTimer) return;
  libraryPollTimer = setInterval(async () => {
    if (!popup || popup.hidden) return;
    const before = imageCache.images.map((x) => `${x.rel}:${x.mtime_ns}:${x.size}`).join("|");
    await fetchImages(true);
    const after = imageCache.images.map((x) => `${x.rel}:${x.mtime_ns}:${x.size}`).join("|");
    if (before !== after && active?.el) refreshFor(active.el, true).catch(() => {});
  }, 2500);
}

async function uploadFiles(files) {
  const allowed = /\.(png|jpe?g|webp|bmp|tiff?)$/i;
  const picked = [...files].filter((f) => allowed.test(f.name || ""));
  if (!picked.length) { toast("没有可添加的图片文件"); return; }
  const resumeEl = active?.el || document.activeElement;
  setButtonsBusy(true);
  try {
    const form = new FormData();
    picked.forEach((f, i) => form.append(`file_${i}`, f, f.name));
    const res = await requestFallback("/uim/library/upload", { method: "POST", body: form });
    const data = await res.json();
    imageCache.images = Array.isArray(data.images) ? data.images : [];
    imageCache.path = String(data.library_path || imageCache.path || "");
    imageCache.at = Date.now();
    updateLibraryPath();
    const saved = Array.isArray(data.saved) ? data.saved.length : 0;
    const rejected = Array.isArray(data.rejected) ? data.rejected.length : 0;
    toast(`@图片库：已添加 ${saved} 张${rejected ? `，${rejected} 张未通过` : ""}`);
    if (active?.el) await refreshFor(active.el, true);
  } catch (err) {
    toast(`添加图片失败：${err?.message || err}`);
  } finally {
    setButtonsBusy(false);
    try { resumeEl?.focus?.(); } catch {}
  }
}

async function openLibraryFolder() {
  setButtonsBusy(true);
  try {
    const res = await requestFallback("/uim/library/open", { method: "POST" });
    const data = await res.json();
    imageCache.path = String(data.library_path || imageCache.path || "");
    updateLibraryPath();
    toast(data.ok ? `已请求打开 @图片库：${imageCache.path}` : `图库路径：${imageCache.path}`);
  } catch (err) {
    toast(`无法自动打开目录；图库路径：${imageCache.path || "未知"}`);
  } finally { setButtonsBusy(false); }
}

function normalize(s) {
  try { return String(s ?? "").normalize("NFKC").toLocaleLowerCase(); }
  catch { return String(s ?? "").toLowerCase(); }
}

function getEditorValue(el) {
  if (!el) return "";
  if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) return String(el.value || "");
  if (el.isContentEditable) return String(el.innerText || el.textContent || "");
  return "";
}

function getEditorCaret(el) {
  if (!el) return 0;
  if (typeof el.selectionStart === "number") return el.selectionStart;
  if (el.isContentEditable) {
    try {
      const sel = window.getSelection();
      if (!sel || sel.rangeCount < 1 || !el.contains(sel.anchorNode)) return getEditorValue(el).length;
      const range = sel.getRangeAt(0).cloneRange();
      range.selectNodeContents(el);
      range.setEnd(sel.anchorNode, sel.anchorOffset);
      return range.toString().length;
    } catch {}
  }
  return getEditorValue(el).length;
}

function isEditorElement(el) {
  if (!el || !(el instanceof Element)) return false;
  if (el instanceof HTMLTextAreaElement) return true;
  if (el instanceof HTMLInputElement) {
    const type = String(el.type || "text").toLowerCase();
    return (type === "text" || type === "search") && (el.dataset.uimPromptEditor === "1" || !!el.__uimWidget);
  }
  return !!el.isContentEditable && (el.dataset.uimPromptEditor === "1" || !!el.__uimWidget);
}

function resolveEditor(target) {
  if (!(target instanceof Element)) return null;
  if (isEditorElement(target)) return target;
  const nested = target.closest?.('textarea,[contenteditable="true"],input[type="text"],input[type="search"]');
  return isEditorElement(nested) ? nested : null;
}

function mentionAtCaret(el) {
  const value = getEditorValue(el);
  const caret = getEditorCaret(el);
  const before = value.slice(0, caret);
  const at = before.lastIndexOf("@");
  if (at < 0) return null;

  // v2.1: a bare @ must ALWAYS be a valid trigger. Do not require whitespace
  // before it; prompts such as "让@人物A转身" should work too.
  const raw = before.slice(at + 1);
  if (/[\n\r,，。；;:：!?！？()（）\[\]【】<>《》]/.test(raw)) return null;
  // A completed braced/quoted mention is no longer an active autocomplete query.
  // Without this guard, selecting @{file name} dispatches an input event and the
  // popup can immediately reopen because the trailing space is still accepted.
  if (raw.startsWith("{") && raw.includes("}")) return null;
  if (raw.startsWith('"') && raw.slice(1).includes('"')) return null;
  if (raw.startsWith("“") && raw.slice(1).includes("”")) return null;
  if (/\s/.test(raw) && !(raw.startsWith("{") || raw.startsWith('"') || raw.startsWith("“"))) return null;
  let query = raw;
  if (query.startsWith("{")) query = query.slice(1);
  if (query.startsWith('"') || query.startsWith("“")) query = query.slice(1);
  return { at, caret, raw, query };
}


function collectNodeAndInner(node) {
  const out = [];
  const seen = new Set();
  const visit = (n) => {
    if (!n || seen.has(n)) return;
    seen.add(n);
    out.push(n);
    try {
      const inner = typeof n.getInnerNodes === "function" ? n.getInnerNodes(new Map()) : [];
      for (const x of inner || []) if (x !== n) visit(x);
    } catch {}
  };
  visit(node);
  return out;
}

function nodeHasImageInputs(node) {
  return Array.isArray(node?.inputs) && node.inputs.some((x) => String(x?.type || "").toUpperCase() === "IMAGE");
}

function resolveReferenceTargetNode(el) {
  const source = el?.__uimNode;
  if (!source) return null;

  // Prefer a real inner multimodal node. This is important for ComfyUI
  // subgraphs/proxy widgets: the visible node may have a prompt widget while
  // H3/Krea/Flux reference nodes live inside the subgraph.
  const local = collectNodeAndInner(source);
  const h3 = local.find((n) => String(n.comfyClass || n.type || "") === "MiniMaxH3ReferenceToVideo");
  if (h3) return h3;
  const multi = local.find((n) => nodeHasImageInputs(n));
  if (multi) return multi;

  // Walk outward a few hops. Primitive/String -> subgraph -> model is common.
  try {
    const queue = [source];
    const seen = new Set([source]);
    for (let depth = 0; depth < 4 && queue.length; depth++) {
      const next = [];
      for (const n of queue) {
        for (const out of n.outputs || []) {
          for (const linkId of out?.links || []) {
            const link = app.graph?.links?.[linkId];
            const target = link ? app.graph?.getNodeById?.(link.target_id) : null;
            if (!target || seen.has(target)) continue;
            seen.add(target);
            for (const candidate of collectNodeAndInner(target)) {
              const klass = String(candidate.comfyClass || candidate.type || "");
              if (klass === "MiniMaxH3ReferenceToVideo") return candidate;
              if (nodeHasImageInputs(candidate)) return candidate;
            }
            next.push(target);
          }
        }
      }
      queue.splice(0, queue.length, ...next);
    }
  } catch {}
  return source;
}

function sourceLabelForInput(node, inp) {
  try {
    const link = inp?.link != null ? app.graph?.links?.[inp.link] : null;
    let src = link ? app.graph?.getNodeById?.(link.origin_id) : null;
    const seen = new Set();
    for (let depth = 0; src && depth < 8 && !seen.has(src); depth++) {
      seen.add(src);
      const klass = String(src.comfyClass || src.type || "");
      if (klass === "LoadImage") {
        const w = (src.widgets || []).find((x) => x?.name === "image") || src.widgets?.[0];
        const raw = String(w?.value || "").replace(/\\/g, "/");
        if (raw) return raw.split("/").pop();
      }
      // Follow the most likely image/latent input upstream.
      const pref = (src.inputs || []).find((x) => /^(image|pixels|latent|samples)$/i.test(String(x?.name || "")) && x?.link != null)
        || (src.inputs || []).find((x) => x?.link != null && /IMAGE|LATENT/i.test(String(x?.type || "")));
      const up = pref?.link != null ? app.graph?.links?.[pref.link] : null;
      src = up ? app.graph?.getNodeById?.(up.origin_id) : null;
    }
  } catch {}
  return "";
}

function connectedReferenceCandidates(el) {
  const node = resolveReferenceTargetNode(el);
  if (!node || !Array.isArray(node.inputs)) return [];
  const klass = String(node.comfyClass || node.type || "");
  const rows = [];

  if (klass === "MiniMaxH3ReferenceToVideo") {
    for (const inp of node.inputs) {
      const m = /^ref_images\.ref_image_(\d+)$/.exec(String(inp?.name || ""));
      if (!m || inp?.link == null) continue;
      const slotIndex = Number(m[1]) + 1;
      const sourceLabel = sourceLabelForInput(node, inp);
      rows.push({
        kind: "slot",
        slotIndex,
        stem: String(slotIndex),
        aliases: sourceLabel ? [sourceLabel, sourceLabel.replace(/\.[^.]+$/, "")] : [],
        name: sourceLabel ? `@${slotIndex} · ${sourceLabel}` : `@${slotIndex} · 已连接 Picture ${slotIndex}`,
        rel: String(inp.name || ""),
        sub: `H3 已连接参考图 · <Picture ${slotIndex}>`,
      });
    }
    return rows.sort((a, b) => a.slotIndex - b.slotIndex);
  }

  // Generic fallback for other multimodal nodes: expose connected IMAGE
  // sockets in their visual order. This affects autocomplete only; backend
  // Auto-Bind still validates the exact target adapter before execution.
  let ordinal = 0;
  for (const inp of node.inputs) {
    if (String(inp?.type || "").toUpperCase() !== "IMAGE") continue;
    const low = String(inp?.name || "").toLowerCase();
    if (/mask|control|depth|canny|normal|seg|preview/.test(low)) continue;
    ordinal += 1;
    if (inp?.link == null) continue;
    const sourceLabel = sourceLabelForInput(node, inp);
    rows.push({
      kind: "slot",
      slotIndex: ordinal,
      stem: String(ordinal),
      aliases: sourceLabel ? [sourceLabel, sourceLabel.replace(/\.[^.]+$/, "")] : [],
      name: sourceLabel ? `@${ordinal} · ${sourceLabel}` : `@${ordinal} · 已连接参考图 ${ordinal}`,
      rel: String(inp.name || ""),
      sub: `当前节点已连接 IMAGE 槽位 · ${String(inp.name || "image")}`,
    });
  }
  return rows;
}

function scoreImage(item, query) {
  const q = normalize(query).trim();
  if (!q) return item?.kind === "slot" ? 1400 - Number(item.slotIndex || 0) : 100;
  if (item?.kind === "slot") {
    const n = String(item.slotIndex || "");
    if (q === n || q === `#${n}` || q === `槽${n}` || q === `slot${n}`) return 2000;
    if (n.startsWith(q)) return 1500;
    for (const alias of item.aliases || []) {
      const a = normalize(alias);
      if (a === q) return 1900;
      if (a.startsWith(q)) return 1350;
      if (a.includes(q)) return 1100;
    }
    return -1;
  }
  const stem = normalize(item.stem);
  const name = normalize(item.name);
  const rel = normalize(item.rel);
  if (stem === q || name === q) return 1000;
  if (stem.startsWith(q)) return 800 - Math.min(100, stem.length - q.length);
  if (name.startsWith(q)) return 750;
  const si = stem.indexOf(q);
  if (si >= 0) return 600 - si;
  const ri = rel.indexOf(q);
  if (ri >= 0) return 400 - ri;
  let qi = 0;
  for (let i = 0; i < rel.length && qi < q.length; i++) if (rel[i] === q[qi]) qi++;
  return qi === q.length ? 200 : -1;
}

function previewUrl(item) {
  const rel = encodeURIComponent(String(item.rel || ""));
  return routeCandidates(`/uim/library/thumb?rel=${rel}`)[0];
}

function tokenFor(item) {
  if (item?.kind === "slot") return `@${Number(item.slotIndex)}`;
  const stem = String(item.stem || item.name || "");
  if (/^[^\s{}"“”]+$/.test(stem)) return `@${stem}`;
  return `@{${stem}}`;
}

function caretRectForTextControl(el) {
  try {
    const value = getEditorValue(el);
    const caret = getEditorCaret(el);
    const cs = getComputedStyle(el);
    const mirror = document.createElement("div");
    const props = [
      "boxSizing","width","height","overflowX","overflowY","borderTopWidth","borderRightWidth","borderBottomWidth","borderLeftWidth",
      "paddingTop","paddingRight","paddingBottom","paddingLeft","fontStyle","fontVariant","fontWeight","fontStretch","fontSize","fontSizeAdjust",
      "lineHeight","fontFamily","textAlign","textTransform","textIndent","textDecoration","letterSpacing","wordSpacing","tabSize","MozTabSize"
    ];
    mirror.style.position = "fixed";
    mirror.style.visibility = "hidden";
    mirror.style.whiteSpace = el instanceof HTMLInputElement ? "pre" : "pre-wrap";
    mirror.style.wordWrap = "break-word";
    mirror.style.left = `${el.getBoundingClientRect().left}px`;
    mirror.style.top = `${el.getBoundingClientRect().top}px`;
    for (const prop of props) try { mirror.style[prop] = cs[prop]; } catch {}
    mirror.textContent = value.slice(0, caret);
    const marker = document.createElement("span");
    marker.textContent = value.slice(caret, caret + 1) || "\u200b";
    mirror.appendChild(marker);
    document.body.appendChild(mirror);
    const mr = marker.getBoundingClientRect();
    const er = el.getBoundingClientRect();
    const left = mr.left - (el.scrollLeft || 0);
    const top = mr.top - (el.scrollTop || 0);
    mirror.remove();
    if (Number.isFinite(left) && Number.isFinite(top)) {
      return { left, top, bottom: top + (parseFloat(cs.lineHeight) || 18), right: left + 2, editor: er };
    }
  } catch {}
  const r = el.getBoundingClientRect();
  return { left: r.left + 12, top: r.top + 12, bottom: r.top + 32, right: r.left + 14, editor: r };
}

function positionPopup(el) {
  const p = ensurePopup();
  const anchor = caretRectForTextControl(el);
  const editor = anchor.editor || el.getBoundingClientRect();
  const width = Math.min(Math.max(360, Math.min(editor.width || 520, 560)), Math.max(320, window.innerWidth - 16));
  p.style.width = `${width}px`;

  // Measure after width assignment. Prefer below the caret; flip above when
  // needed. This avoids covering the entire prompt editor on tall nodes.
  p.hidden = false;
  p.style.visibility = "hidden";
  const height = Math.min(p.scrollHeight || 360, 420);
  const gap = 6;
  const below = window.innerHeight - anchor.bottom - gap;
  const above = anchor.top - gap;
  let top = below >= Math.min(height, 220)
    ? anchor.bottom + gap
    : Math.max(8, anchor.top - Math.min(height, Math.max(180, above)) - gap);
  let left = anchor.left;
  left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
  top = Math.max(8, Math.min(top, window.innerHeight - Math.min(height, window.innerHeight - 16) - 8));
  p.style.left = `${left}px`;
  p.style.top = `${top}px`;
  p.style.visibility = "";
}

function hidePopup() {
  if (popup) popup.hidden = true;
  active = null;
}

function renderResults(el, mention, images) {
  const p = ensurePopup();
  positionPopup(el);
  updateLibraryPath();
  refreshBindState().catch(() => {});
  refreshVisionState().catch(() => {});
  startLibraryPolling();
  const list = p.querySelector(".uim-mention-list");
  const ranked = images
    .map((item) => ({ item, score: scoreImage(item, mention.query) }))
    .filter((x) => x.score >= 0)
    .sort((a, b) => b.score - a.score || String(a.item.rel).localeCompare(String(b.item.rel)))
    .slice(0, MAX_RESULTS)
    .map((x) => x.item);

  active = { el, mention, items: ranked, selected: 0 };
  list.replaceChildren();
  if (!ranked.length) {
    const empty = document.createElement("div");
    empty.className = "uim-mention-empty";
    empty.textContent = images.length ? `没有匹配“${mention.query}”的图片` : "@图片库还是空的，点上面的“＋ 添加图片”即可加入。";
    list.appendChild(empty);
    return;
  }

  const slots = ranked.filter((x) => x?.kind === "slot");
  const library = ranked.filter((x) => x?.kind !== "slot");
  const ordered = [...slots, ...library];
  active.items = ordered;
  active.selected = 0;
  let visualIndex = 0;

  function addHeader(text) {
    const h = document.createElement("div");
    h.className = "uim-mention-section";
    h.textContent = text;
    list.appendChild(h);
  }
  function addItem(item) {
    const index = visualIndex++;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "uim-mention-item";
    btn.setAttribute("role", "option");
    btn.setAttribute("aria-selected", index === 0 ? "true" : "false");
    btn.dataset.index = String(index);

    let visual;
    if (item?.kind === "slot") {
      visual = document.createElement("div");
      visual.className = "uim-mention-slotbadge";
      visual.textContent = String(item.slotIndex || "?");
      visual.setAttribute("aria-hidden", "true");
    } else {
      visual = document.createElement("img");
      visual.className = "uim-mention-thumb";
      visual.loading = "lazy";
      visual.alt = "";
      visual.src = previewUrl(item);
      visual.addEventListener("error", () => { visual.style.visibility = "hidden"; }, { once: true });
    }
    const meta = document.createElement("div"); meta.style.minWidth = "0";
    const name = document.createElement("div"); name.className = "uim-mention-name";
    name.textContent = item?.kind === "slot" ? (item.name || `@${item.slotIndex}`) : (item.stem || item.name || item.rel);
    const sub = document.createElement("div"); sub.className = "uim-mention-sub"; sub.textContent = item.sub || item.rel || item.name;
    meta.append(name, sub); btn.append(visual, meta);

    const choose = (e) => {
      e?.preventDefault?.(); e?.stopPropagation?.(); e?.stopImmediatePropagation?.();
      active.selected = index;
      insertSelected(item);
    };
    // Multiple input modalities. `pointerdown` is primary; mousedown/touchend
    // are compatibility fallbacks for older embedded browsers and forks.
    btn.addEventListener("pointerdown", choose, { capture: true });
    btn.addEventListener("mousedown", (e) => { if (!window.PointerEvent) choose(e); }, { capture: true });
    btn.addEventListener("touchend", (e) => { if (!window.PointerEvent) choose(e); }, { capture: true, passive: false });
    btn.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); });
    btn.addEventListener("mousemove", () => {
      if (!active) return; active.selected = index;
      list.querySelectorAll(".uim-mention-item").forEach((b, i) => b.setAttribute("aria-selected", i === index ? "true" : "false"));
    });
    list.appendChild(btn);
  }

  if (slots.length) { addHeader(`当前工作流已连接参考图 · ${slots.length} 张（@1 / @2 / …）`); slots.forEach(addItem); }
  if (library.length) { addHeader(`专用 @图片库 · ${library.length} 个匹配`); library.forEach(addItem); }
}

function updateSelection(delta) {
  if (!active?.items?.length || !popup || popup.hidden) return;
  active.selected = (active.selected + delta + active.items.length) % active.items.length;
  const buttons = popup.querySelectorAll(".uim-mention-item");
  buttons.forEach((b, i) => b.setAttribute("aria-selected", i === active.selected ? "true" : "false"));
  buttons[active.selected]?.scrollIntoView?.({ block: "nearest" });
}

async function refreshFor(el, force = false) {
  if (!isEditorElement(el)) return;
  const mention = mentionAtCaret(el);
  if (!mention) {
    if (active?.el === el) hidePopup();
    return;
  }
  const images = await fetchImages(force);
  if (!editorHasFocus(el) && !actionBusy) return;
  const current = mentionAtCaret(el);
  if (!current || current.at !== mention.at || current.caret !== mention.caret) return;
  const slots = connectedReferenceCandidates(el);
  renderResults(el, current, [...slots, ...images]);
}

function editorHasFocus(el) {
  if (!el) return false;
  const ae = document.activeElement;
  if (ae === el) return true;
  try { if (el.contains?.(ae)) return true; } catch {}
  try { if (el.matches?.(":focus-within")) return true; } catch {}
  return false;
}

const scheduledEditors = new WeakMap();
function scheduleRefresh(el, force = false) {
  if (!isEditorElement(el)) return;
  const seq = (scheduledEditors.get(el) || 0) + 1;
  scheduledEditors.set(el, seq);
  const run = () => {
    if (scheduledEditors.get(el) !== seq) return;
    refreshFor(el, force).catch((err) => console.warn(`[${EXTENSION_NAME}] refresh failed`, err));
  };
  // input usually fires immediately. These two fallbacks cover widgets/forks
  // that commit their value after keydown/beforeinput or via a Vue store tick.
  setTimeout(run, 0);
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(run);
}

function setContentEditableText(el, value, caret) {
  el.textContent = value;
  try {
    const sel = window.getSelection();
    const range = document.createRange();
    const node = el.firstChild || el;
    const max = node.nodeType === Node.TEXT_NODE ? (node.nodeValue || "").length : 0;
    range.setStart(node, Math.max(0, Math.min(caret, max)));
    range.collapse(true);
    sel.removeAllRanges();
    sel.addRange(range);
  } catch {}
}

function syncWidget(el) {
  const widget = el.__uimWidget;
  const value = getEditorValue(el);
  if (widget) {
    try { widget.value = value; } catch {}
    try { widget.callback?.(value); } catch {}
  }
  try { el.dispatchEvent(new Event("input", { bubbles: true })); } catch {}
  try { el.dispatchEvent(new Event("change", { bubbles: true })); } catch {}
  try { app.graph?.setDirtyCanvas?.(true, true); } catch {}
}

function insertSelected(item) {
  if (!active?.el || !active?.mention) return;
  const { el, mention } = active;
  const value = getEditorValue(el);
  const token = tokenFor(item);
  const after = value.slice(mention.caret);
  // Always delimit an inserted mention. This keeps Chinese possessive phrases
  // such as "@1 的衣服" parseable even when the user continues typing immediately.
  const replacement = token + (/^\s/.test(after) ? "" : " ");
  const nextValue = value.slice(0, mention.at) + replacement + after;
  const pos = mention.at + replacement.length;

  if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
    el.value = nextValue;
    try { el.setSelectionRange(pos, pos); } catch {}
  } else if (el.isContentEditable) {
    setContentEditableText(el, nextValue, pos);
  }

  syncWidget(el);
  hidePopup();
  try { el.focus(); } catch {}
}

function attachEditor(el, widget = null, node = null) {
  if (!el || !(el instanceof Element)) return;
  if (!(el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement || el.isContentEditable)) return;

  // Mark first so delegated listeners can recognize the editor even if the
  // node-specific listener is installed a little later.
  el.dataset.uimPromptEditor = "1";
  if (widget) {
    el.__uimWidget = widget;
    widget.__uimMentionAttached = true;
  }
  if (node) el.__uimNode = node;
  if (el.dataset.uimMentionAttached === "1") return;
  el.dataset.uimMentionAttached = "1";

  // Per-editor listeners are a fast path. Document capture listeners below
  // are the compatibility fallback for dynamically-created widgets/subgraphs.
  el.addEventListener("input", () => refreshFor(el));
  el.addEventListener("beforeinput", (e) => {
    if (String(e.data || "").includes("@")) scheduleRefresh(el);
  });
  el.addEventListener("compositionend", () => scheduleRefresh(el));
  el.addEventListener("paste", () => scheduleRefresh(el));
  el.addEventListener("click", () => {
    if (mentionAtCaret(el)) refreshFor(el);
    else if (active?.el === el) hidePopup();
  });
  el.addEventListener("focus", () => {
    if (mentionAtCaret(el)) scheduleRefresh(el);
  });
  el.addEventListener("blur", () => setTimeout(() => {
    if (actionBusy) return;
    if (active?.el === el && !popup?.matches(":hover")) hidePopup();
  }, 150));
}

function attachNode(node) {
  if (!node?.widgets) return;
  for (const widget of node.widgets) {
    const el = widget?.element || widget?.inputEl;
    if (!el) continue;
    const isRouterPrompt = node.comfyClass === ROUTER_CLASS && widget.name === "prompt";
    const isMultiline = el instanceof HTMLTextAreaElement || !!el.isContentEditable || widget?.options?.multiline === true || widget?.type === "textarea";
    if (isRouterPrompt || isMultiline) attachEditor(el, widget, node);
  }
}

function scheduleAttachNode(node) {
  // Some Vue/DOM widgets assign widget.element after nodeCreated returns.
  // Retry a few cheap times so the first typed character is never the event
  // that finally causes the element to become discoverable.
  [0, 40, 160, 600].forEach((ms) => setTimeout(() => {
    try { attachNode(node); } catch (err) { console.warn(`[${EXTENSION_NAME}] delayed node attach failed`, err); }
  }, ms));
}


function associateKnownWidgets() {
  try {
    for (const node of app.graph?._nodes || []) {
      for (const widget of node?.widgets || []) {
        const el = widget?.element || widget?.inputEl;
        if (el) attachEditor(el, widget, node);
      }
    }
  } catch {}
}

function scanDomEditors() {
  associateKnownWidgets();
  // Every textarea is a potential prompt/editor in ComfyUI. Inputs and
  // contenteditable elements are only picked up globally once a widget marks
  // them, avoiding the global search box and other one-line UI controls.
  document.querySelectorAll("textarea").forEach((el) => attachEditor(el, el.__uimWidget || null, el.__uimNode || null));
  document.querySelectorAll('[data-uim-prompt-editor="1"]').forEach((el) => attachEditor(el, el.__uimWidget || null, el.__uimNode || null));
}

const handledKeyEvents = new WeakSet();
function consumePopupKey(e) {
  try { handledKeyEvents.add(e); } catch {}
  e.preventDefault?.();
  e.stopPropagation?.();
  e.stopImmediatePropagation?.();
}

function handleGlobalKeydown(e) {
  if (handledKeyEvents.has(e)) return;

  // When the mention popup is open, active.el is authoritative. ComfyUI
  // subgraphs/forks may transiently move DOM focus or consume Enter before the
  // textarea sees it, so do not require e.target/document.activeElement to be
  // the editor for popup navigation/selection.
  const popupOpen = !!(popup && !popup.hidden && active?.el);
  const el = popupOpen
    ? active.el
    : (resolveEditor(e.target) || (isEditorElement(document.activeElement) ? document.activeElement : null));
  if (!el) return;

  if (popupOpen) {
    if (e.key === "ArrowDown") { consumePopupKey(e); updateSelection(1); return; }
    if (e.key === "ArrowUp") { consumePopupKey(e); updateSelection(-1); return; }
    if (e.key === "PageDown") { consumePopupKey(e); updateSelection(8); return; }
    if (e.key === "PageUp") { consumePopupKey(e); updateSelection(-8); return; }
    if (e.key === "Home") { consumePopupKey(e); if (active?.items?.length) { active.selected = 0; updateSelection(0); } return; }
    if (e.key === "End") { consumePopupKey(e); if (active?.items?.length) { active.selected = active.items.length - 1; updateSelection(0); } return; }
    if (e.key === "Enter" || e.key === "Tab") {
      if (active?.items?.length) {
        const item = active.items[active.selected] || active.items[0];
        consumePopupKey(e);
        insertSelected(item);
        return;
      }
    }
    if (e.key === "Escape") { consumePopupKey(e); hidePopup(); return; }
  }

  // Critical v2.1 fix: keydown happens before the browser inserts '@'.
  // Schedule checks after the default insertion/store update.
  if (e.key === "@" || (e.key === "2" && e.shiftKey)) scheduleRefresh(el);
}

function handleGlobalBeforeInput(e) {
  const el = resolveEditor(e.target);
  if (!el) return;
  if (String(e.data || "").includes("@")) scheduleRefresh(el);
}

function handleGlobalInput(e) {
  const el = resolveEditor(e.target);
  if (!el) return;
  refreshFor(el).catch((err) => console.warn(`[${EXTENSION_NAME}] input refresh failed`, err));
}

function handleGlobalKeyup(e) {
  const el = resolveEditor(e.target);
  if (!el) return;
  if (e.key === "@" || e.key === "Process" || mentionAtCaret(el)) scheduleRefresh(el);
}

function handleGlobalFocusOrClick(e) {
  const el = resolveEditor(e.target);
  if (!el) return;
  if (mentionAtCaret(el)) scheduleRefresh(el);
}

function installGlobalEditorListeners() {
  // Capture phase means we still see the event even if a third-party node
  // stops propagation in its own handler.
  // Window capture runs before ComfyUI's document/canvas handlers. This makes
  // Enter/Tab/arrow selection reliable even in subgraphs and third-party widgets.
  window.addEventListener("keydown", handleGlobalKeydown, true);
  // Document is retained as a compatibility fallback; handledKeyEvents avoids
  // processing the same event twice.
  document.addEventListener("keydown", handleGlobalKeydown, true);
  document.addEventListener("beforeinput", handleGlobalBeforeInput, true);
  document.addEventListener("input", handleGlobalInput, true);
  document.addEventListener("keyup", handleGlobalKeyup, true);
  document.addEventListener("compositionend", handleGlobalFocusOrClick, true);
  document.addEventListener("focusin", handleGlobalFocusOrClick, true);
  document.addEventListener("click", handleGlobalFocusOrClick, true);
  document.addEventListener("paste", (e) => {
    const el = resolveEditor(e.target);
    if (el) scheduleRefresh(el);
  }, true);
}

app.registerExtension({
  name: EXTENSION_NAME,
  async nodeCreated(node) {
    try { attachNode(node); scheduleAttachNode(node); } catch (err) { console.warn(`[${EXTENSION_NAME}] node attach failed`, err); }
  },
  async loadedGraphNode(node) {
    try { attachNode(node); scheduleAttachNode(node); } catch (err) { console.warn(`[${EXTENSION_NAME}] loaded node attach failed`, err); }
  },
  async setup() {
    try {
      ensurePopup();
      ensureFileInput();
      fetchImages(true);
      refreshBindState();
      scanDomEditors();
      installGlobalEditorListeners();
      let scanQueued = false;
      const observer = new MutationObserver(() => {
        if (scanQueued) return;
        scanQueued = true;
        requestAnimationFrame(() => { scanQueued = false; scanDomEditors(); });
      });
      observer.observe(document.body, { childList: true, subtree: true });
      document.addEventListener("pointerdown", (e) => {
        if (popup && !popup.hidden && !popup.contains(e.target) && e.target !== active?.el) hidePopup();
      }, true);
      window.addEventListener("resize", () => { if (active?.el && popup && !popup.hidden) positionPopup(active.el); });
      window.addEventListener("scroll", () => { if (active?.el && popup && !popup.hidden) positionPopup(active.el); }, true);
    } catch (err) {
      console.warn(`[${EXTENSION_NAME}] setup failed; backend @ parsing remains available`, err);
    }
  },
});
