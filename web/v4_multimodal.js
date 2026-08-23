import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const EXT = "Universal.ImageMentions.V4_2_2.AuditEngine";
const editors = new Set();
const docks = new WeakMap();
let libraryCache = { at: 0, items: [] };
let controls = null;
let maskModal = null;
let v4Config = null;
let executionImages = new Map();
const runStateByPrompt = new Map();
let retryInFlight = false;
let lastBindReport = null;

function routeCandidates(path) {
  const out = [path];
  const current = window.location.pathname || "/";
  if (current !== "/") {
    const prefix = current.endsWith("/") ? current : current.slice(0, current.lastIndexOf("/") + 1);
    out.push(`${prefix.replace(/\/$/, "")}${path}`);
  }
  return [...new Set(out)];
}
async function req(path, options = {}) {
  let err;
  for (const url of routeCandidates(path)) {
    try {
      const r = await fetch(url, options);
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try { const j = await r.json(); msg = j.error || j.reason || msg; } catch {}
        throw new Error(msg);
      }
      return r;
    } catch (e) { err = e; }
  }
  throw err || new Error(path);
}
async function ask(title, message, defaultValue = "") {
  // Fail-soft dialog wrapper. Native prompt/confirm are intentionally used as
  // the lowest common denominator across ComfyUI frontend generations/forks.
  const label = `${String(title || "UIM")}\n\n${String(message || "")}`;
  return window.prompt(label, String(defaultValue ?? ""));
}
async function askConfirm(title, message) {
  return window.confirm(`${String(title || "UIM")}\n\n${String(message || "")}`);
}
function toast(text) {
  const el = document.createElement("div");
  el.className = "uim-v4-toast";
  el.textContent = String(text || "");
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}
function norm(s) {
  try { return String(s ?? "").normalize("NFKC").toLocaleLowerCase(); }
  catch { return String(s ?? "").toLowerCase(); }
}
function stripExt(s) { return String(s || "").replace(/\.(png|jpe?g|webp|bmp|tiff?)$/i, ""); }

function escHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function canonicalForToken(raw) {
  const t = String(raw || "").replace(/^@/, "").replace(/^\{/, "").replace(/\}$/, "").replace(/^["“]|["”]$/g, "");
  const m = /^(?:#|槽|slot)?(\d{1,2})$/i.exec(t.trim());
  if (m) return `slot:${Number(m[1])}`;
  return `name:${norm(stripExt(t.replace(/\\/g, "/")))}`;
}
function parseMentions(text) {
  const re = /@(?:(?<slot>(?:#|槽|slot)?\d{1,2})(?=$|[^0-9A-Za-z_])|\{(?<brace>[^}]+)\}|["“](?<quote>[^"”]+)["”]|(?<plain>[^@\s,，。；;:：!?！？()（）\[\]【】<>《》]+))/giu;
  const out = [];
  for (const m of String(text || "").matchAll(re)) {
    const token = m.groups?.slot || m.groups?.brace || m.groups?.quote || m.groups?.plain || "";
    out.push({ raw: m[0], token, start: m.index, end: m.index + m[0].length, key: canonicalForToken(m[0]) });
  }
  return out;
}
function isEditor(el) {
  return el instanceof HTMLTextAreaElement || (el instanceof HTMLInputElement && ["text","search"].includes(String(el.type||"text"))) || !!el?.isContentEditable;
}
function valueOf(el) { return el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement ? String(el.value || "") : String(el.innerText || el.textContent || ""); }
function syncEditor(el, value) {
  if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) el.value = value;
  else el.textContent = value;
  const w = el.__uimWidget;
  if (w) { try { w.value = value; w.callback?.(value); } catch {} }
  try { el.dispatchEvent(new Event("input", { bubbles: true })); } catch {}
  try { el.dispatchEvent(new Event("change", { bubbles: true })); } catch {}
  try { app.graph?.setDirtyCanvas?.(true, true); } catch {}
}
function nodeMeta(node) {
  if (!node) return { version: 2, mentions: {}, audit: {} };
  node.properties ||= {};
  node.properties.uim_v4 ||= { version: 2, mentions: {}, audit: {} };
  node.properties.uim_v4.mentions ||= {};
  node.properties.uim_v4.audit ||= {};
  return node.properties.uim_v4;
}
function controlFor(el, key) {
  const meta = nodeMeta(el.__uimNode);
  meta.mentions[key] ||= { role: "AUTO", strength: 1.0, mask_rel: "", mask_mode: "FOCUS", order: 0 };
  return meta.mentions[key];
}
async function loadLibrary(force=false) {
  if (!force && libraryCache.items.length && Date.now()-libraryCache.at < 2000) return libraryCache.items;
  try {
    const r = await req("/uim/images", { cache: "no-store" });
    const j = await r.json();
    libraryCache = { at: Date.now(), items: Array.isArray(j.images) ? j.images : [] };
  } catch {}
  return libraryCache.items;
}
function libraryMatch(token, items) {
  const q = norm(stripExt(token));
  const exact = items.filter(x => [norm(x.stem), norm(x.name), norm(stripExt(x.rel))].includes(q));
  if (exact.length === 1) return exact[0];
  const prefix = items.filter(x => norm(x.stem).startsWith(q));
  return prefix.length === 1 ? prefix[0] : null;
}
function collectNodeAndInner(node) {
  const out=[], seen=new Set();
  const visit=n=>{ if(!n||seen.has(n))return; seen.add(n); out.push(n); try{ for(const x of n.getInnerNodes?.(new Map())||[]) if(x!==n) visit(x); }catch{} };
  visit(node); return out;
}
function resolveTarget(el) {
  const src=el.__uimNode; if(!src) return null;
  for(const n of collectNodeAndInner(src)) if((n.inputs||[]).some(x=>String(x?.type||"").toUpperCase()==="IMAGE")) return n;
  try {
    let frontier=[src], seen=new Set(frontier);
    for(let depth=0; depth<5; depth++) {
      const next=[];
      for(const n of frontier) for(const o of n.outputs||[]) for(const lid of o.links||[]) {
        const link=app.graph?.links?.[lid], t=link?app.graph?.getNodeById?.(link.target_id):null;
        if(!t||seen.has(t)) continue; seen.add(t);
        for(const c of collectNodeAndInner(t)) if((c.inputs||[]).some(x=>String(x?.type||"").toUpperCase()==="IMAGE")) return c;
        next.push(t);
      }
      frontier=next;
    }
  } catch {}
  return src;
}
function traceLoadImage(inp) {
  try {
    let link=inp?.link!=null?app.graph?.links?.[inp.link]:null;
    let n=link?app.graph?.getNodeById?.(link.origin_id):null;
    const seen=new Set();
    for(let d=0;n&&d<10&&!seen.has(n);d++) {
      seen.add(n); const k=String(n.comfyClass||n.type||"");
      if(k==="LoadImage") {
        const w=(n.widgets||[]).find(x=>x?.name==="image")||n.widgets?.[0];
        return String(w?.value||"").replace(/\\/g,"/");
      }
      const p=(n.inputs||[]).find(x=>/^(image|pixels|latent|samples)$/i.test(String(x?.name||""))&&x.link!=null)||(n.inputs||[]).find(x=>x.link!=null&&/IMAGE|LATENT/i.test(String(x?.type||"")));
      link=p?.link!=null?app.graph?.links?.[p.link]:null; n=link?app.graph?.getNodeById?.(link.origin_id):null;
    }
  } catch {}
  return "";
}
function connectedSlots(el) {
  const node=resolveTarget(el); if(!node) return [];
  const klass=String(node.comfyClass||node.type||""); const rows=[];
  let ordinal=0;
  for(const inp of node.inputs||[]) {
    const isH3=/^ref_images\.ref_image_(\d+)$/.exec(String(inp?.name||""));
    if(isH3) ordinal=Number(isH3[1])+1;
    else {
      if(String(inp?.type||"").toUpperCase()!=="IMAGE") continue;
      if(/mask|control|depth|canny|normal|seg|preview/i.test(String(inp?.name||""))) continue;
      ordinal++;
    }
    if(inp?.link==null) continue;
    const file=traceLoadImage(inp);
    rows.push({ kind:"slot", slotIndex:ordinal, file, inputName:String(inp.name||""), targetClass:klass });
  }
  return rows.sort((a,b)=>a.slotIndex-b.slotIndex);
}
function inputPreview(file) {
  if(!file) return "";
  const clean=String(file).replace(/\s*\[(?:input|output|temp)\]\s*$/i,"");
  const parts=clean.split("/"); const filename=parts.pop(); const subfolder=parts.join("/");
  const q=new URLSearchParams({filename,subfolder,type:"input"});
  return routeCandidates(`/view?${q.toString()}`)[0];
}
function libPreview(rel) { return routeCandidates(`/uim/library/thumb?rel=${encodeURIComponent(rel)}`)[0]; }
async function resolveMentionItems(el) {
  const mentions=parseMentions(valueOf(el)); if(!mentions.length) return [];
  const lib=await loadLibrary(); const slots=connectedSlots(el);
  return mentions.map((m,idx)=>{
    const n=/^(?:#|槽|slot)?(\d{1,2})$/i.exec(m.token);
    if(n) {
      const s=slots.find(x=>x.slotIndex===Number(n[1]));
      return {...m,index:idx,item:s||{kind:"slot",slotIndex:Number(n[1]),file:""}, preview:s?inputPreview(s.file):"", title:s?.file||`Picture ${Number(n[1])}`, lockedOrder:true};
    }
    const li=libraryMatch(m.token,lib);
    const stableKey=li?.uim_id?`id:${li.uim_id}`:m.key;
    return {...m,key:stableKey,index:idx,item:li?{kind:"library",...li}:{kind:"missing",stem:m.token},preview:li?libPreview(li.rel):"",title:li?.rel||m.token,lockedOrder:false};
  });
}
function installStyle() {
  if(document.getElementById("uim-v4-style")) return;
  const st=document.createElement("style"); st.id="uim-v4-style"; st.textContent=`
  .uim-v4-dock{position:fixed;z-index:999990;display:flex;align-items:center;gap:5px;overflow-x:auto;overflow-y:hidden;max-height:46px;padding:4px 5px;border-radius:7px;background:color-mix(in srgb,var(--comfy-input-bg,#262626) 88%,transparent);border:1px solid color-mix(in srgb,var(--border-color,#555) 70%,transparent);backdrop-filter:blur(3px);overscroll-behavior:contain;scrollbar-width:thin;pointer-events:auto}
  .uim-v4-chip{height:34px;min-width:74px;max-width:170px;display:flex;align-items:center;gap:5px;padding:2px 7px 2px 3px;border:1px solid var(--border-color,#666);border-radius:7px;background:var(--comfy-menu-bg,#202124);color:var(--input-text,#eee);font:11px/1.2 system-ui;cursor:pointer;flex:none;user-select:none}
  .uim-v4-chip[draggable=true]{cursor:grab}.uim-v4-chip.dragging{opacity:.45}.uim-v4-chip.missing{border-color:#d84343}.uim-v4-chip.hasmask{box-shadow:inset 0 0 0 1px #4caf50}
  .uim-v4-chip img,.uim-v4-chip .slot{width:28px;height:28px;border-radius:5px;object-fit:cover;background:#111;display:grid;place-items:center;font-weight:800;border:1px solid rgba(255,255,255,.12);flex:none}
  .uim-v4-chip .txt{overflow:hidden;white-space:nowrap;text-overflow:ellipsis}.uim-v4-chip .badge{font-size:9px;opacity:.72}
  .uim-v4-tools{display:flex;gap:3px;margin-left:auto;position:sticky;right:0;background:inherit;padding-left:3px;flex:none}.uim-v4-tool{width:28px;height:28px;border-radius:6px;border:1px solid var(--border-color,#666);background:var(--comfy-input-bg,#333);color:inherit;cursor:pointer}
  .uim-v4-control{position:fixed;z-index:1000010;width:320px;padding:10px;border:1px solid var(--border-color,#666);border-radius:10px;background:var(--comfy-menu-bg,#202124);color:var(--input-text,#eee);box-shadow:0 12px 34px rgba(0,0,0,.42);font:12px/1.35 system-ui}.uim-v4-control[hidden]{display:none}.uim-v4-control label{display:block;margin:7px 0 3px}.uim-v4-control select,.uim-v4-control input[type=range]{width:100%}.uim-v4-control .row{display:flex;gap:6px;align-items:center}.uim-v4-control button{border:1px solid var(--border-color,#666);background:var(--comfy-input-bg,#333);color:inherit;border-radius:6px;padding:5px 8px;cursor:pointer}.uim-v4-control .grow{flex:1}.uim-v4-control .small{font-size:10px;opacity:.75}
  .uim-v4-mask{position:fixed;z-index:1000020;inset:0;background:rgba(0,0,0,.72);display:grid;place-items:center}.uim-v4-mask[hidden]{display:none}.uim-v4-maskbox{max-width:92vw;max-height:92vh;background:#1e1e1e;border:1px solid #666;border-radius:10px;padding:10px;display:flex;flex-direction:column;gap:8px}.uim-v4-maskstage{position:relative;overflow:auto;max-width:86vw;max-height:76vh}.uim-v4-maskstage img{display:block;max-width:78vw;max-height:70vh}.uim-v4-maskstage canvas{position:absolute;left:0;top:0;touch-action:none;cursor:crosshair}.uim-v4-maskbar{display:flex;gap:6px;align-items:center;flex-wrap:wrap}.uim-v4-maskbar button{padding:5px 9px}.uim-v4-toast{position:fixed;right:14px;bottom:14px;z-index:1000030;max-width:480px;background:#222;color:#eee;border:1px solid #666;border-radius:8px;padding:9px 12px;box-shadow:0 8px 24px #0008;font:12px/1.4 system-ui}
  `; document.head.appendChild(st);
}
function ensureControls() {
  if(controls) return controls;
  controls=document.createElement("div"); controls.className="uim-v4-control"; controls.hidden=true; document.body.appendChild(controls);
  controls.addEventListener("pointerdown",e=>e.stopPropagation(),false); return controls;
}
function markGraphChanged() { try { app.graph?.change?.(); app.graph?.setDirtyCanvas?.(true,true); } catch {} }
function openControls(el, ref, chip) {
  const p=ensureControls(), ctl=controlFor(el, ref.key); const role=String(ctl.role||"AUTO"), strength=Number(ctl.strength??1);
  p.innerHTML=`<div class="row"><strong class="grow">${escHtml(ref.raw)} · 参考控制</strong><button data-x>×</button></div>
  <div class="small">${escHtml(ref.title||"")}</div>
  <label>只参考哪一类信息</label><select data-role><option>AUTO</option><option>IDENTITY</option><option>FACE</option><option>HAIR</option><option>CLOTHING</option><option>POSE_MOTION</option><option>PRODUCT_OBJECT</option><option>SCENE</option><option>STYLE</option><option>GENERAL_REFERENCE</option></select>
  <label>参考强度 / 优先级：<b data-sval>${strength.toFixed(2)}</b></label><input data-strength type="range" min="0" max="2" step="0.05" value="${strength}">
  <div class="row" style="margin-top:9px"><button data-mask class="grow">绘制区域 Mask</button><button data-clear>清除 Mask</button></div>
  <div class="small" style="margin-top:6px">${ctl.mask_rel?`Mask：${escHtml(ctl.mask_rel)}`:"当前未设置 Mask"}</div>`;
  p.querySelector("[data-role]").value=role;
  p.querySelector("[data-x]").onclick=()=>p.hidden=true;
  p.querySelector("[data-role]").onchange=e=>{ ctl.role=e.target.value; markGraphChanged(); renderDock(el); };
  const sr=p.querySelector("[data-strength]"); sr.oninput=e=>{ ctl.strength=Number(e.target.value); p.querySelector("[data-sval]").textContent=Number(ctl.strength).toFixed(2); markGraphChanged(); }; sr.onchange=()=>renderDock(el);
  p.querySelector("[data-clear]").onclick=()=>{ ctl.mask_rel=""; markGraphChanged(); p.hidden=true; renderDock(el); };
  p.querySelector("[data-mask]").onclick=()=>openMaskEditor(el,ref,ctl);
  const r=chip.getBoundingClientRect(); p.hidden=false; p.style.left=`${Math.max(8,Math.min(r.left,innerWidth-330))}px`; p.style.top=`${Math.min(innerHeight-250,r.bottom+5)}px`;
}
function ensureMaskModal() {
  if(maskModal) return maskModal;
  maskModal=document.createElement("div"); maskModal.className="uim-v4-mask"; maskModal.hidden=true; maskModal.innerHTML=`<div class="uim-v4-maskbox"><div class="uim-v4-maskbar"><strong>V4 Reference Mask</strong><span class="grow"></span><label>画笔 <input data-brush type="range" min="4" max="100" value="34"></label><button data-clear>清空</button><button data-invert>反选</button><button data-save>保存 Mask</button><button data-close>关闭</button></div><div class="uim-v4-maskstage"><img><canvas></canvas></div><div class="small">白色区域=允许参考；黑色区域=忽略。鼠标/触摸直接涂白。</div></div>`; document.body.appendChild(maskModal); return maskModal;
}
async function openMaskEditor(el,ref,ctl) {
  const modal=ensureMaskModal(), img=modal.querySelector("img"), canvas=modal.querySelector("canvas"), viewCtx=canvas.getContext("2d");
  const src=ref.preview; if(!src){toast("这张引用当前没有可读取的缩略图，无法绘制 Mask");return;}
  controls.hidden=true; modal.hidden=false; img.src=src;
  let imageLoaded=true;
  await new Promise((resolve)=>{ if(img.complete&&img.naturalWidth)resolve(); else {img.onload=resolve;img.onerror=()=>{imageLoaded=false;resolve();};} });
  if(!imageLoaded||!img.naturalWidth){modal.hidden=true;toast("参考图预览加载失败，无法绘制 Mask");return;}
  const maxW=Math.min(900,innerWidth*.78), maxH=innerHeight*.68; const scale=Math.min(1,maxW/img.naturalWidth,maxH/img.naturalHeight);
  img.style.width=`${Math.max(64,Math.round(img.naturalWidth*scale))}px`; img.style.height="auto"; await new Promise(r=>requestAnimationFrame(r));
  const rect=img.getBoundingClientRect(); canvas.width=Math.max(1,Math.round(rect.width)); canvas.height=Math.max(1,Math.round(rect.height)); canvas.style.width=`${rect.width}px`; canvas.style.height=`${rect.height}px`;

  // V4.2.4: separate the visible paint overlay from the real black/white mask data.
  // The visible canvas stays transparent so the reference image is never covered by an opaque black mask.
  const maskCanvas=document.createElement("canvas"); maskCanvas.width=canvas.width; maskCanvas.height=canvas.height;
  const maskCtx=maskCanvas.getContext("2d",{willReadFrequently:true});
  maskCtx.fillStyle="black"; maskCtx.fillRect(0,0,maskCanvas.width,maskCanvas.height);
  viewCtx.clearRect(0,0,canvas.width,canvas.height);
  viewCtx.lineCap=maskCtx.lineCap="round"; viewCtx.lineJoin=maskCtx.lineJoin="round";

  const redrawOverlay=()=>{
    const d=maskCtx.getImageData(0,0,maskCanvas.width,maskCanvas.height), out=viewCtx.createImageData(canvas.width,canvas.height);
    for(let i=0;i<d.data.length;i+=4){const a=d.data[i];out.data[i]=255;out.data[i+1]=255;out.data[i+2]=255;out.data[i+3]=Math.round(a*0.58);}
    viewCtx.clearRect(0,0,canvas.width,canvas.height);viewCtx.putImageData(out,0,0);
  };

  // Reopen an existing mask instead of silently resetting it.
  if(ctl.mask_rel){
    try{
      const oldMask=new Image(); oldMask.src=`/uim/mask/file?rel=${encodeURIComponent(ctl.mask_rel)}&t=${Date.now()}`;
      await new Promise((resolve,reject)=>{oldMask.onload=resolve;oldMask.onerror=reject;});
      maskCtx.drawImage(oldMask,0,0,maskCanvas.width,maskCanvas.height); redrawOverlay();
    }catch(e){console.warn("[UIM] Existing mask preview could not be restored",e);}
  }

  let drawing=false,last=null; const point=e=>{const rr=canvas.getBoundingClientRect(),t=e.touches?.[0]||e;return{x:(t.clientX-rr.left)*canvas.width/rr.width,y:(t.clientY-rr.top)*canvas.height/rr.height}};
  const draw=e=>{ if(!drawing)return; e.preventDefault(); const q=point(e), b=Number(modal.querySelector("[data-brush]").value||34); maskCtx.strokeStyle="white";maskCtx.lineWidth=b;maskCtx.beginPath();maskCtx.moveTo(last?.x??q.x,last?.y??q.y);maskCtx.lineTo(q.x,q.y);maskCtx.stroke(); viewCtx.strokeStyle="rgba(255,255,255,.58)";viewCtx.lineWidth=b;viewCtx.beginPath();viewCtx.moveTo(last?.x??q.x,last?.y??q.y);viewCtx.lineTo(q.x,q.y);viewCtx.stroke();last=q; };
  canvas.onpointerdown=e=>{drawing=true;last=point(e);canvas.setPointerCapture?.(e.pointerId);draw(e)}; canvas.onpointermove=draw; canvas.onpointerup=canvas.onpointercancel=()=>{drawing=false;last=null};
  modal.querySelector("[data-clear]").onclick=()=>{maskCtx.fillStyle="black";maskCtx.fillRect(0,0,maskCanvas.width,maskCanvas.height);viewCtx.clearRect(0,0,canvas.width,canvas.height)};
  modal.querySelector("[data-invert]").onclick=()=>{const d=maskCtx.getImageData(0,0,maskCanvas.width,maskCanvas.height);for(let i=0;i<d.data.length;i+=4){const v=255-d.data[i];d.data[i]=d.data[i+1]=d.data[i+2]=v;d.data[i+3]=255;}maskCtx.putImageData(d,0,0);redrawOverlay()};
  modal.querySelector("[data-close]").onclick=()=>modal.hidden=true;
  modal.querySelector("[data-save]").onclick=async()=>{try{const r=await req("/uim/mask/upload",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({data_url:maskCanvas.toDataURL("image/png")})});const j=await r.json();if(!r.ok||!j.mask_rel)throw new Error(j.error||`HTTP ${r.status}`);ctl.mask_rel=j.mask_rel;markGraphChanged();modal.hidden=true;renderDock(el);toast("Mask 已保存并绑定到该 @图片");}catch(e){toast(`Mask 保存失败：${e.message||e}`)}};
}
function updateDockPosition(el,dock) {
  const r=el.getBoundingClientRect(); if(r.width<80||r.height<30||r.bottom<0||r.top>innerHeight){dock.hidden=true;return;} dock.hidden=false;
  dock.style.left=`${r.left+4}px`; dock.style.top=`${r.top+4}px`; dock.style.width=`${Math.max(70,r.width-8)}px`;
}
async function refreshLastBind(){try{const r=await req("/uim/last-bind",{cache:"no-store"});lastBindReport=await r.json();}catch{}}
function backendControlFor(ref){
  const reps=Array.isArray(lastBindReport?.reports)?lastBindReport.reports:[];
  for(const rep of reps){for(const rr of Array.isArray(rep?.references)?rep.references:[]){
    const sameId=ref.item?.uim_id&&rr.uim_id&&String(ref.item.uim_id)===String(rr.uim_id);
    const sameSlot=ref.item?.kind==="slot"&&Number(rr.index)===Number(ref.item.slotIndex);
    const sameFile=ref.item?.rel&&rr.file&&String(rr.file)===String(ref.item.rel);
    if(sameId||sameSlot||sameFile)return rr.v4||{};
  }} return {};
}
function controlBadge(ref,ctl){
  const eff=backendControlFor(ref); const role=String(ctl.role||"AUTO"); const bits=[role];
  const st=Number(ctl.strength??1); if(Math.abs(st-1)>.001){const mode=String(eff.strength_effective||"");bits.push(`${st.toFixed(2)}${mode==="NATIVE"?"N":mode==="SEMANTIC"?"S":"?"}`);}
  if(ctl.mask_rel){const mode=String(eff.mask_effective||"");bits.push(`Mask:${mode==="NATIVE"?"N":mode==="PREPROCESS"?"P":"?"}`);}
  return bits.join(" · ");
}
function runId(){try{return crypto.randomUUID().replace(/-/g,"");}catch{return `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;}}
function runMarker({runId:rid,rootRunId,parentRunId,retryIndex}){return `[UIM-RUN id=${rid} root=${rootRunId} parent=${parentRunId||"-"} retry=${retryIndex}]`;}

async function renderDock(el) {
  if(!document.body.contains(el)) return;
  const refs=await resolveMentionItems(el); let dock=docks.get(el);
  if(!refs.length){ if(dock)dock.hidden=true; restorePadding(el); return; }
  if(!dock){dock=document.createElement("div");dock.className="uim-v4-dock";document.body.appendChild(dock);docks.set(el,dock);dock.addEventListener("wheel",e=>e.stopPropagation(),{passive:true,capture:true});}
  ensurePadding(el); dock.replaceChildren(); updateDockPosition(el,dock);
  const meta=nodeMeta(el.__uimNode); const ordered=[...refs].sort((a,b)=>{const ao=Number(meta.mentions[a.key]?.order||0),bo=Number(meta.mentions[b.key]?.order||0);return (ao||1e6)-(bo||1e6)||a.index-b.index;});
  for(const ref of ordered){const ctl=controlFor(el,ref.key),chip=document.createElement("button");chip.type="button";chip.className=`uim-v4-chip ${ref.item.kind==="missing"?"missing":""} ${ctl.mask_rel?"hasmask":""}`;chip.title=`${ref.title}\n点击设置用途/强度/Mask`;chip.dataset.key=ref.key;chip.draggable=!ref.lockedOrder;
    const vis=ref.preview?document.createElement("img"):document.createElement("span"); if(ref.preview){vis.src=ref.preview;vis.onerror=()=>{vis.style.visibility="hidden"}}else{vis.className="slot";vis.textContent=ref.item.kind==="slot"?String(ref.item.slotIndex):"?"}
    const t=document.createElement("span");t.className="txt";t.innerHTML=`<b>${escHtml(ref.raw)}</b><br><span class="badge">${escHtml(controlBadge(ref,ctl))}</span>`;chip.append(vis,t);chip.onclick=e=>{e.preventDefault();e.stopPropagation();openControls(el,ref,chip)};
    chip.ondragstart=e=>{chip.classList.add("dragging");e.dataTransfer.setData("text/uim-key",ref.key)};chip.ondragend=()=>chip.classList.remove("dragging");chip.ondragover=e=>{if(!ref.lockedOrder)e.preventDefault()};chip.ondrop=e=>{e.preventDefault();const from=e.dataTransfer.getData("text/uim-key");if(!from||from===ref.key)return;const libKeys=ordered.filter(x=>!x.lockedOrder).map(x=>x.key);const a=libKeys.indexOf(from),b=libKeys.indexOf(ref.key);if(a<0||b<0)return;libKeys.splice(b,0,libKeys.splice(a,1)[0]);libKeys.forEach((k,i)=>{controlFor(el,k).order=i+1});markGraphChanged();renderDock(el);toast("图库 @图片顺序已更新；已连接 @1/@2 槽位保持锁定")}; dock.appendChild(chip);}
  const tools=document.createElement("div");tools.className="uim-v4-tools";tools.innerHTML=`<button class="uim-v4-tool" title="自动适配第三方节点" data-adapt>↔</button><button class="uim-v4-tool" title="V4.2 结果审查 / 自动重试设置" data-audit>✓</button><button class="uim-v4-tool" title="V4.2 最近审查日志" data-auditlog>≋</button>`;tools.querySelector("[data-adapt]").onclick=e=>{e.preventDefault();configureAdapter(el)};tools.querySelector("[data-audit]").onclick=e=>{e.preventDefault();configureAudit()};tools.querySelector("[data-auditlog]").onclick=e=>{e.preventDefault();showAuditLog()};dock.appendChild(tools);
}
function ensurePadding(el){if(!el.dataset.uimV4Pad){const cs=getComputedStyle(el);el.dataset.uimV4Pad=cs.paddingTop||"";} const n=parseFloat(getComputedStyle(el).paddingTop)||0;if(n<50)el.style.paddingTop="50px";}
function restorePadding(el){if(el.dataset.uimV4Pad!=null){el.style.paddingTop=el.dataset.uimV4Pad;delete el.dataset.uimV4Pad;}}
function attach(el,node=null,widget=null){if(!isEditor(el))return;if(node)el.__uimNode=node;if(widget)el.__uimWidget=widget;if(el.dataset.uimV4Attached)return;el.dataset.uimV4Attached="1";editors.add(el);const run=()=>renderDock(el).catch(()=>{});["input","change","focus","click","keyup","compositionend"].forEach(ev=>el.addEventListener(ev,run));el.addEventListener("scroll",()=>{const d=docks.get(el);if(d)updateDockPosition(el,d)});run();}
function scan(){try{for(const n of app.graph?._nodes||[])for(const w of n.widgets||[]){const el=w?.element||w?.inputEl;if(el&&(el instanceof HTMLTextAreaElement||w?.options?.multiline||w?.type==="textarea"))attach(el,n,w)}}catch{} document.querySelectorAll("textarea[data-uim-prompt-editor='1'],textarea").forEach(el=>attach(el,el.__uimNode||null,el.__uimWidget||null));}
async function configureAdapter(el){
  const t=resolveTarget(el);if(!t){toast("未找到下游目标节点");return;} const klass=String(t.comfyClass||t.type||"");
  const inputs=t.inputs||[]; const imgs=inputs.filter(x=>String(x?.type||"").toUpperCase()==="IMAGE"&&!/mask|control|depth|canny|normal|seg|preview/i.test(String(x?.name||""))).map(x=>x.name);
  const masks=inputs.filter(x=>String(x?.type||"").toUpperCase()==="MASK").map(x=>x.name); const nums=inputs.filter(x=>["FLOAT","INT","NUMBER"].includes(String(x?.type||"").toUpperCase())&&/strength|weight|influence|fidelity|reference_scale|ref_scale|image_scale/i.test(String(x?.name||""))).map(x=>x.name);
  const strings=inputs.filter(x=>String(x?.type||"").toUpperCase()==="STRING").map(x=>x.name);
  const slots=await ask("V4.2 第三方模型适配",`节点：${klass}\n真正用于参考图的 IMAGE 槽位（逗号分隔）`,imgs.join(","));if(slots==null)return;
  const slotList=String(slots).split(",").map(x=>x.trim()).filter(Boolean); if(!slotList.length){toast("没有参考 IMAGE 槽位，未保存 Adapter");return;}
  const promptInput=await ask("V4.2 第三方模型适配","Prompt 输入名（当前节点内提示词可留空）",strings[0]||"");if(promptInput==null)return;
  const tag=await ask("V4.2 第三方模型适配","模型图片标签格式；{i}=序号","Reference Image {i}");if(tag==null)return;
  const strengthMap={}; if(slotList.length===1&&nums.length===1)strengthMap[slotList[0]]=nums[0]; else slotList.forEach((slot,i)=>{const n=String(i+1),m=nums.find(x=>new RegExp(`(?:^|[_\\.])${n}(?:$|[_\\.])`).test(x)||((slot.match(/\\d+/g)||[]).at(-1)&&(x.match(/\\d+/g)||[]).at(-1)===(slot.match(/\\d+/g)||[]).at(-1)));if(m)strengthMap[slot]=m;});
  const maskMap={}; if(slotList.length===1&&masks.length===1)maskMap[slotList[0]]=masks[0]; else slotList.forEach((slot,i)=>{const n=String(i+1),m=masks.find(x=>new RegExp(`(?:^|[_\\.])${n}(?:$|[_\\.])`).test(x)||((slot.match(/\\d+/g)||[]).at(-1)&&(x.match(/\\d+/g)||[]).at(-1)===(slot.match(/\\d+/g)||[]).at(-1)));if(m)maskMap[slot]=m;});
  try{const cfg={name:`USER_${klass}`,slots:slotList,max_refs:slotList.length,tag_template:String(tag),prompt_input:String(promptInput),native_vision:true,replace_existing:false,strength_map:strengthMap,mask_map:maskMap,capabilities:{supports_reference_image:true,supports_multi_reference:slotList.length>1,supports_native_strength:Object.keys(strengthMap).length>0,supports_native_mask:Object.keys(maskMap).length>0,confidence:"USER_VERIFIED",reference_semantics:["GENERAL_REFERENCE"]}};await req("/uim/adapters",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({adapters:{[klass]:cfg}})});toast(`已保存 ${klass} Adapter；原生权重 ${Object.keys(strengthMap).length} 个，原生 Mask ${Object.keys(maskMap).length} 个`);}catch(e){toast(`Adapter 保存失败：${e.message||e}`)}
}
async function getConfig(){if(v4Config)return v4Config;try{const r=await req("/uim/v4/config",{cache:"no-store"}),j=await r.json();v4Config=j.config||{};}catch{v4Config={}}return v4Config;}
async function configureAudit(){
  const cfg=await getConfig();
  const enabled=await askConfirm("V4.2 结果审查",`开启生成结果 Vision Audit？
当前：${cfg.audit_enabled?"开启":"关闭"}
确定=开启，取消=关闭`);
  let auto=!!cfg.audit_auto_retry, max=Number(cfg.audit_max_retries||1), threshold=Number(cfg.audit_threshold||0.78), minConfidence=Number(cfg.audit_min_confidence||0.55), criticalFloor=Number(cfg.audit_critical_floor||0.58), visionMinConfidence=Number(cfg.vision_min_confidence||0.55);
  if(enabled){
    auto=await askConfirm("V4.2 自动重跑","审查不通过时，只针对失败维度自动修正 Prompt 并重跑？");
    const th=await ask("V4.2 Audit Engine","总体/维度通过阈值 0~1",String(threshold)); if(th!=null)threshold=Math.max(0,Math.min(1,Number(th)||.78));
    const mc=await ask("V4.2 Audit Engine","最低可信度 0~1；低于此值记为 INCONCLUSIVE，不作为失败依据",String(minConfidence)); if(mc!=null)minConfidence=Math.max(0,Math.min(1,Number(mc)||.55));
    const cf=await ask("V4.2 Audit Engine","关键维度严重失败线 0~1",String(criticalFloor)); if(cf!=null)criticalFloor=Math.max(0,Math.min(1,Number(cf)||.58));
    const vm=await ask("V4.2 Vision Grounding","写入增强 Prompt 的视觉字段最低可信度 0~1",String(visionMinConfidence)); if(vm!=null)visionMinConfidence=Math.max(0,Math.min(1,Number(vm)||.55));
    if(auto){const m=await ask("V4.2 自动重跑","最多自动重跑次数 0~5",String(max));if(m!=null)max=Math.max(0,Math.min(5,Number(m)||1));}
  }
  try{
    const r=await req("/uim/v4/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({audit_enabled:enabled,audit_auto_retry:auto,audit_threshold:threshold,audit_min_confidence:minConfidence,audit_critical_floor:criticalFloor,vision_min_confidence:visionMinConfidence,audit_max_retries:max})});
    v4Config=(await r.json()).config;
    toast(`V4.2 审查：${enabled?`开启 · 阈值 ${threshold.toFixed(2)} · 可信度≥${minConfidence.toFixed(2)}${auto?` · 自动重跑≤${max}`:""}`:"关闭"}`);
  }catch(e){toast(`保存失败：${e.message||e}`)}
}
function auditSummary(j){
  const pct=Math.round(Number(j?.overall_score||0)*100), delta=j?.score_delta;
  const d=delta==null?"":` · ${Number(delta)>=0?"+":""}${Math.round(Number(delta)*100)}pp`;
  const failed=Array.isArray(j?.failed_dimensions)?j.failed_dimensions:[];
  const inc=Number(j?.inconclusive_count||0);
  return `${pct}%${d}${failed.length?` · 失败:${failed.slice(0,4).join(",")}${failed.length>4?"…":""}`:""}${inc?` · 不确定:${inc}`:""}`;
}
async function showAuditLog(){
  try{
    const r=await req("/uim/audit/log?limit=12",{cache:"no-store"}),j=await r.json(),items=Array.isArray(j.items)?j.items:[];
    if(!items.length){toast("V4.2 暂无审查记录");return;}
    const lines=items.slice().reverse().map((x,i)=>{const run=x.run||{},rid=String(run.run_id||"").slice(0,8)||"-",score=Math.round(Number(x.overall_score||0)*100),delta=x.score_delta==null?"":` ${Number(x.score_delta)>=0?"+":""}${Math.round(Number(x.score_delta)*100)}pp`,failed=(x.failed_dimensions||[]).slice(0,3).join(",")||"无";return `${i+1}. ${score}%${delta} · run ${rid} · 失败维度 ${failed}`;});
    await ask("V4.2 最近审查日志",lines.join("\n"),"");
  }catch(e){toast(`读取审查日志失败：${e.message||e}`)}
}
function outputUrl(info){const q=new URLSearchParams({filename:info.filename||"",subfolder:info.subfolder||"",type:info.type||"output"});return routeCandidates(`/view?${q.toString()}`)[0];}
function bestPromptEditor(){
  const live=[...editors].filter(el=>document.body.contains(el)&&parseMentions(valueOf(el)).length);
  const sourceIds=new Set((Array.isArray(lastBindReport?.reports)?lastBindReport.reports:[]).map(r=>String(r?.source??"")));
  return live.find(el=>sourceIds.has(String(el.__uimNode?.id??"")))||live[0]||null;
}
async function queueAutomaticRetry(el,j,cfg,promptId){
  if(retryInFlight)return; retryInFlight=true;
  const currentRun=j.run||runStateByPrompt.get(String(promptId))||{}; const parent=String(currentRun.run_id||currentRun.runId||""); const root=String(currentRun.root_run_id||currentRun.rootRunId||parent||runId()); const prev=Number(currentRun.retry_index??currentRun.retryIndex??0); const next=prev+1;
  if(next>Number(cfg.audit_max_retries||1)){retryInFlight=false;toast("V4.2 Retry Guard：已达到最大自动重跑次数");return;}
  const rid=runId(), marker=runMarker({runId:rid,rootRunId:root,parentRunId:parent||root,retryIndex:next}); const original=valueOf(el); const temp=`${original}\n\n${marker}\n[V4 automatic audit correction ${next}]\n${j.retry_prompt_suffix}`;
  syncEditor(el,temp);
  try{
    if(typeof app.graphToPrompt!=="function"||typeof api.queuePrompt!=="function")throw new Error("当前 ComfyUI 前端没有可用的 graphToPrompt/api.queuePrompt 接口");
    const payload=await app.graphToPrompt(); const res=await api.queuePrompt(0,payload); const pid=String(res?.prompt_id||"");
    if(pid)runStateByPrompt.set(pid,{run_id:rid,root_run_id:root,parent_run_id:parent||root,retry_index:next,kind:"AUTO_RETRY"});
    toast(`V4.2 自动重跑已入队 ${next}/${cfg.audit_max_retries}${pid?` · ${pid.slice(0,8)}`:""}`);
  } finally {syncEditor(el,original);retryInFlight=false;}
}
async function auditAfterSuccess(promptId){
  const cfg=await getConfig();if(!cfg.audit_enabled)return;const infos=executionImages.get(String(promptId))||[];if(!infos.length)return;const info=infos[infos.length-1];
  try{
    const img=await fetch(outputUrl(info)).then(r=>{if(!r.ok)throw new Error(`image HTTP ${r.status}`);return r.blob()});
    const form=new FormData();form.append("file",img,info.filename||"generated.png");form.append("prompt_id",String(promptId));const state=runStateByPrompt.get(String(promptId));if(state?.run_id)form.append("run_id",state.run_id);
    const r=await req("/uim/audit/analyze",{method:"POST",body:form}),j=await r.json();if(j.run?.run_id)runStateByPrompt.set(String(promptId),j.run);
    const summary=auditSummary(j); toast(`V4.2 Audit：${summary} ${j.retry_recommended?"· 建议重试":"· 通过/无需重试"}`);
    if(Array.isArray(j.relations)&&j.relations.length){try{console.groupCollapsed(`[UIM V4.2 Audit] ${summary}`);console.table(j.relations.map(x=>({mode:x.mode,attribute:x.attribute,score:x.score,confidence:x.confidence,status:x.status,failed:(x.failed_dimensions||[]).join(",")})));console.groupEnd();}catch{}}
    if(j.retry_recommended&&cfg.audit_auto_retry&&j.retry_prompt_suffix){await refreshLastBind();const el=bestPromptEditor();if(el)await queueAutomaticRetry(el,j,cfg,promptId);}
  }catch(e){toast(`V4.2 结果审查未执行：${e.message||e}`)}
  finally { executionImages.delete(String(promptId)); if(runStateByPrompt.size>128){for(const k of [...runStateByPrompt.keys()].slice(0,runStateByPrompt.size-96))runStateByPrompt.delete(k);} }
}
function installExecutionHooks(){try{api.addEventListener("execution_start",e=>{const pid=String(e.detail?.prompt_id||e.detail||"");executionImages.set(pid,[])});api.addEventListener("executed",e=>{const d=e.detail||{},pid=String(d.prompt_id||"");const imgs=Array.isArray(d.output?.images)?d.output.images:[];if(imgs.length){const arr=executionImages.get(pid)||[];arr.push(...imgs);executionImages.set(pid,arr)}});api.addEventListener("execution_success",e=>{const pid=String(e.detail?.prompt_id||"");setTimeout(()=>auditAfterSuccess(pid),150)});api.addEventListener("execution_error",e=>{retryInFlight=false;const pid=String(e.detail?.prompt_id||"");if(pid)executionImages.delete(pid)});api.addEventListener("execution_interrupted",e=>{retryInFlight=false;const pid=String(e.detail?.prompt_id||"");if(pid)executionImages.delete(pid)})}catch(e){console.warn(`[${EXT}] execution hooks unavailable`,e)}}

function updateAll(){for(const el of [...editors]){if(!document.body.contains(el)){editors.delete(el);docks.get(el)?.remove();continue;}const d=docks.get(el);if(d)updateDockPosition(el,d)}}
function setup(){installStyle();scan();let scanQueued=false;new MutationObserver(()=>{if(scanQueued)return;scanQueued=true;requestAnimationFrame(()=>{scanQueued=false;scan()})}).observe(document.body,{childList:true,subtree:true});window.addEventListener("resize",updateAll);window.addEventListener("scroll",updateAll,true);document.addEventListener("pointerdown",e=>{if(controls&&!controls.hidden&&!controls.contains(e.target)&&!e.target.closest?.(".uim-v4-chip"))controls.hidden=true},true);setInterval(()=>{for(const el of [...editors])if(!document.body.contains(el)){editors.delete(el);docks.get(el)?.remove()}Promise.all([loadLibrary(true),refreshLastBind()]).then(()=>{for(const el of editors)renderDock(el)})},3500);installExecutionHooks();getConfig();refreshLastBind();}
app.registerExtension({name:EXT,nodeCreated(node){setTimeout(()=>{for(const w of node.widgets||[]){const el=w?.element||w?.inputEl;if(el&&(el instanceof HTMLTextAreaElement||w?.options?.multiline||w?.type==="textarea"))attach(el,node,w)}},0)},loadedGraphNode(node){setTimeout(()=>{for(const w of node.widgets||[]){const el=w?.element||w?.inputEl;if(el&&(el instanceof HTMLTextAreaElement||w?.options?.multiline||w?.type==="textarea"))attach(el,node,w)}},50)},setup});
