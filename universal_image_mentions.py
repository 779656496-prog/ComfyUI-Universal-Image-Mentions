from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import unicodedata
import uuid
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import folder_paths  # ComfyUI core, old and new versions
except Exception:  # allows parser/unit tests outside ComfyUI
    folder_paths = None

try:
    import nodes as comfy_nodes  # ComfyUI core legacy node module
except Exception:
    comfy_nodes = None


PLUGIN_NAME = "ComfyUI-Universal-Image-Mentions"
PLUGIN_VERSION = "4.2.4"
CATEGORY = "Universal/@ Image Mentions"
MAX_OUTPUT_REFS = 16

# V3 runtime diagnostics: last preflight/bind report is exposed to the frontend.
_LAST_BIND_REPORT: Dict[str, Any] = {"plugin": PLUGIN_NAME, "version": PLUGIN_VERSION, "status": "idle", "reports": []}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}

# Supported:
#   @图1
#   @图1.png
#   @{人物 正面}
#   @"人物 正面"
#   @“人物 正面”
# Plain mentions intentionally stop at punctuation/whitespace.
_MENTION_RE = re.compile(
    r"@(?:"
    r"(?P<slot>(?:#|槽|slot)?\d{1,2})(?=$|[^0-9A-Za-z_])"
    r"|\{(?P<brace>[^}]+)\}"
    r"|[\"“](?P<quote>[^\"”]+)[\"”]"
    r"|(?P<plain>[^@\s,，。；;:：!?！？()（）\[\]【】<>《》]+)"
    r")"
)

def _mention_token(match: re.Match) -> str:
    return (match.groupdict().get("slot") or match.groupdict().get("brace") or match.groupdict().get("quote") or match.groupdict().get("plain") or "")

# Purely descriptive metadata. It never changes model weights or hardware behavior.
_ROLE_KEYWORDS = {
    "IDENTITY": ("身份", "人物", "脸", "人脸", "五官", "脸型", "长相", "发型", "identity", "face", "character"),
    "CLOTHING": ("衣服", "服装", "穿搭", "裙", "裤", "鞋", "帽", "配饰", "clothes", "clothing", "outfit", "dress", "shirt"),
    "POSE_MOTION": ("姿势", "动作", "姿态", "站姿", "坐姿", "手势", "运动", "打斗", "pose", "motion", "action", "gesture"),
    "PRODUCT_OBJECT": ("产品", "商品", "物品", "包装", "瓶", "盒", "杯", "碗", "product", "object", "package", "packaging"),
    "STYLE": ("风格", "画风", "质感", "色调", "光影", "style", "aesthetic", "lighting", "texture"),
    "SCENE": ("背景", "场景", "环境", "空间", "室内", "室外", "background", "scene", "environment", "location"),
    "EDIT": ("改成", "变成", "换成", "替换", "修改", "去掉", "删除", "增加", "保持不变", "change", "turn", "replace", "remove", "add", "edit"),
}


def _norm(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _strip_known_image_ext(name: str) -> str:
    p = Path(name)
    return p.stem if p.suffix.lower() in _IMAGE_EXTS else name


def _input_root() -> Path:
    if folder_paths is None or not hasattr(folder_paths, "get_input_directory"):
        raise RuntimeError(
            f"{PLUGIN_NAME}: ComfyUI folder_paths.get_input_directory() is unavailable. "
            "Run this node inside a normal ComfyUI installation."
        )
    return Path(folder_paths.get_input_directory()).expanduser().resolve()


def _library_root() -> Path:
    """Dedicated @image library, intentionally outside the ComfyUI directory.

    Default layout for portable installs:
      <portable-root>/ComfyUI/
      <portable-root>/ComfyUI_Mention_Images/

    Override with environment variable UIM_LIBRARY_DIR when desired.
    """
    override = os.environ.get("UIM_LIBRARY_DIR", "").strip()
    if override:
        root = Path(override).expanduser().resolve()
    else:
        comfy_root = _input_root().parent
        root = comfy_root.parent / "ComfyUI_Mention_Images"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_library_path(rel: str) -> Path:
    root = _library_root()
    path = (root / str(rel or "")).expanduser().resolve()
    try:
        path.relative_to(root)
    except Exception:
        raise ValueError("Refusing to access a file outside the @image library")
    return path


def _library_id_registry_path() -> Path:
    return _library_root() / ".uim" / "library_ids_v1.json"


def _read_library_id_registry() -> Dict[str, Any]:
    try:
        data = json.loads(_library_id_registry_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("by_rel", {})
            data.setdefault("by_id", {})
            return data
    except Exception:
        pass
    return {"by_rel": {}, "by_id": {}}


def _write_library_id_registry(data: Dict[str, Any]) -> None:
    path = _library_id_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _hash_file_local(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _build_index(search_subfolders: bool = True) -> List[Dict[str, Any]]:
    """Build the mention-library index with content-stable image IDs.

    V4.1 stores a SHA256-derived ``uim_id`` for each image. The ID survives file
    renames, while prior filename/stem aliases remain searchable through the
    registry. Hashing is skipped for unchanged files by using size+mtime cache.
    """
    root = _library_root()
    if not root.exists():
        return []
    registry = _read_library_id_registry()
    by_rel = registry.setdefault("by_rel", {})
    by_id = registry.setdefault("by_id", {})
    changed = False
    iterator: Iterable[Path] = root.rglob("*") if search_subfolders else root.glob("*")
    items: List[Dict[str, Any]] = []
    live_rels = set()
    for path in iterator:
        try:
            if not path.is_file() or path.suffix.lower() not in _IMAGE_EXTS:
                continue
            if ".uim" in path.relative_to(root).parts:
                continue
            absolute = path.resolve()
            try:
                rel = absolute.relative_to(root).as_posix()
            except Exception:
                rel = path.name
            live_rels.add(rel)
            st = absolute.stat()
            old = by_rel.get(rel) if isinstance(by_rel.get(rel), dict) else {}
            sha = str(old.get("sha256") or "")
            if not sha or int(old.get("size", -1)) != int(st.st_size) or int(old.get("mtime_ns", -1)) != int(st.st_mtime_ns):
                sha = _hash_file_local(absolute)
                changed = True
            uim_id = "uim_" + sha[:24]
            record = {"sha256": sha, "uim_id": uim_id, "size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}
            if old != record:
                by_rel[rel] = record
                changed = True
            ident = by_id.get(uim_id) if isinstance(by_id.get(uim_id), dict) else {"sha256": sha, "aliases": []}
            aliases = list(ident.get("aliases") or [])
            for alias in (rel, Path(rel).name, Path(rel).stem):
                a = str(alias).replace("\\", "/").strip()
                if a and a not in aliases:
                    aliases.append(a)
                    changed = True
            ident.update({"sha256": sha, "last_rel": rel, "aliases": aliases[-64:]})
            by_id[uim_id] = ident
            n_aliases = sorted({_norm(a) for a in aliases if a} | {_norm(_strip_known_image_ext(a)) for a in aliases if a})
            items.append({
                "rel": rel,
                "base": path.name,
                "stem": path.stem,
                "uim_id": uim_id,
                "sha256": sha,
                "aliases": aliases[-64:],
                "n_aliases": n_aliases,
                "n_rel": _norm(rel),
                "n_base": _norm(path.name),
                "n_stem": _norm(path.stem),
            })
        except OSError:
            continue
    # Keep stale by_rel entries as rename history; by_id aliases intentionally
    # preserve old filenames so saved prompts can still resolve after a rename.
    if changed:
        try:
            _write_library_id_registry(registry)
        except Exception:
            pass
    items.sort(key=lambda x: x["n_rel"])
    return items

def _resolve_token(token: str, index: List[Dict[str, str]]) -> Dict[str, str]:
    raw = str(token or "").strip()
    if not raw:
        raise ValueError("Empty @image mention")

    key = _norm(raw.replace("\\", "/"))
    key_stem = _norm(_strip_known_image_ext(raw))

    # 1. Exact relative path, exact filename, exact stem.
    exact_rel = [x for x in index if x["n_rel"] == key]
    if len(exact_rel) == 1:
        return exact_rel[0]

    exact_base = [x for x in index if x["n_base"] == key]
    if len(exact_base) == 1:
        return exact_base[0]

    exact_stem = [x for x in index if x["n_stem"] == key_stem]
    if len(exact_stem) == 1:
        return exact_stem[0]
    if len(exact_stem) > 1:
        raise ValueError(
            f"@{raw} matches multiple input images with the same stem: "
            + ", ".join(x["rel"] for x in exact_stem[:12])
            + ". Use @{subfolder/filename.ext} to disambiguate."
        )

    # 2. Stable-ID alias history (survives file renames).
    alias_hits = [x for x in index if key in (x.get("n_aliases") or []) or key_stem in (x.get("n_aliases") or [])]
    if len(alias_hits) == 1:
        return alias_hits[0]
    if len(alias_hits) > 1:
        raise ValueError(f"@{raw} matches multiple stable image aliases: " + ", ".join(x["rel"] for x in alias_hits[:12]))

    # 3. Unique prefix, useful for filenames with generated timestamps/suffixes.
    prefix = [x for x in index if key_stem and x["n_stem"].startswith(key_stem)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        raise ValueError(
            f"@{raw} is ambiguous (prefix match): " + ", ".join(x["rel"] for x in prefix[:12])
        )

    # 4. Unique substring fallback.
    contains = [x for x in index if key_stem and key_stem in x["n_stem"]]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        raise ValueError(
            f"@{raw} is ambiguous (substring match): " + ", ".join(x["rel"] for x in contains[:12])
        )

    raise ValueError(
        f"Cannot find @{raw} in the dedicated @image library: {_library_root()}. Add the image there, or use the popup Add Images button."
    )


def _context_window(text: str, start: int, end: int, radius: int = 120) -> str:
    hard = "。；;!?！？\n"
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    for i in range(start - 1, left - 1, -1):
        if text[i] in hard:
            left = i + 1
            break
    for i in range(end, right):
        if text[i] in hard:
            right = i
            break
    return text[left:right].replace("\n", " ").strip()


def _roles_for_context(context: str) -> List[str]:
    low = _norm(context)
    roles = [name for name, kws in _ROLE_KEYWORDS.items() if any(_norm(k) in low for k in kws)]
    if not roles:
        return ["GENERAL_REFERENCE"]
    if "EDIT" in roles:
        roles = ["EDIT"] + [x for x in roles if x != "EDIT"]
    return roles[:6]


def _safe_custom_format(template: str, index: int, alias: str, file: str) -> str:
    mapping = {
        "i": index,
        "index": index,
        "alias": alias,
        "file": file,
        "stem": Path(file).stem,
    }
    try:
        return str(template).format(**mapping)
    except Exception as exc:
        raise ValueError(
            "Invalid custom_template. Supported fields: {i}, {index}, {alias}, {file}, {stem}. "
            f"Original error: {exc}"
        )


def _replacement(mode: str, custom_template: str, index: int, alias: str, file: str, original: str) -> str:
    mode = str(mode or "KEEP_AT")
    if mode == "KEEP_AT":
        return original
    if mode == "REMOVE_AT":
        return ""
    if mode == "GENERIC_REF":
        return f"<Reference {index}>"
    if mode == "INDEXED_TEXT":
        return f"Reference {index}"
    if mode == "H3_PICTURE":
        return f"<Picture {index}>"
    if mode == "CUSTOM":
        return _safe_custom_format(custom_template, index, alias, file)
    return original


def _load_library_image(rel: str):
    """Load an IMAGE tensor directly from the dedicated external mention library."""
    try:
        import numpy as np
        import torch
        from PIL import Image, ImageOps
    except Exception as exc:
        raise RuntimeError(
            f"{PLUGIN_NAME}: image dependencies bundled with ComfyUI could not load: {exc}"
        )

    path = _safe_library_path(rel)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Mention image no longer exists: {path}")
    image = Image.open(path)
    image = ImageOps.exif_transpose(image).convert("RGB")
    arr = np.asarray(image).astype(np.float32) / 255.0
    return torch.from_numpy(arr)[None,]


# =============================================================================
# V4 Vision Semantic Reader
# =============================================================================
# The core plugin remains hardware agnostic. Pixel-level metadata always works
# with Pillow (already bundled with ComfyUI). Rich semantic recognition is
# optional and uses an OpenAI-compatible vision endpoint when configured.
# Supported examples include local Ollama/LM Studio/llama.cpp/vLLM gateways and
# remote compatible services. No model is downloaded by this plugin.

_VISION_SCHEMA_VERSION = 1
_VISION_PROMPT_VERSION = "uim-v31-vision-1"


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return _norm(raw) not in {"0", "false", "off", "no", "none", ""}


def _vision_config_path() -> Path:
    return _library_root() / ".uim" / "vision_config.json"


def _read_vision_config() -> Dict[str, Any]:
    path = _vision_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_vision_config(data: Dict[str, Any]) -> None:
    path = _vision_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    allowed = {
        "mode": str(data.get("mode", "AUTO") or "AUTO").upper(),
        "url": str(data.get("url", "") or "").strip(),
        "model": str(data.get("model", "") or "").strip(),
        "api_key_env": str(data.get("api_key_env", "") or "").strip(),
        "required": bool(data.get("required", False)),
        "timeout": float(data.get("timeout", 90) or 90),
    }
    if allowed["mode"] not in {"AUTO", "OFF", "BASIC", "VLM"}:
        allowed["mode"] = "AUTO"
    allowed["timeout"] = max(5.0, min(300.0, allowed["timeout"]))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(allowed, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _vision_mode() -> str:
    cfg = _read_vision_config()
    value = str(os.environ.get("UIM_VISION_MODE", cfg.get("mode", "AUTO")) or "AUTO").strip().upper()
    if value not in {"AUTO", "OFF", "BASIC", "VLM"}:
        value = "AUTO"
    return value


def _vision_endpoint() -> str:
    cfg = _read_vision_config()
    return str(os.environ.get("UIM_VISION_URL", cfg.get("url", "")) or "").strip()


def _vision_model() -> str:
    cfg = _read_vision_config()
    return str(os.environ.get("UIM_VISION_MODEL", cfg.get("model", "")) or "").strip()


def _vision_api_key() -> str:
    direct = str(os.environ.get("UIM_VISION_API_KEY", "") or "").strip()
    if direct:
        return direct
    cfg = _read_vision_config()
    env_name = str(cfg.get("api_key_env", "") or "").strip()
    return str(os.environ.get(env_name, "") or "").strip() if env_name else ""


def _vision_required() -> bool:
    if "UIM_VISION_REQUIRED" in os.environ:
        return _norm(os.environ.get("UIM_VISION_REQUIRED", "")) not in {"0", "false", "off", "no", "none", ""}
    return bool(_read_vision_config().get("required", False))


def _vision_timeout() -> float:
    cfg = _read_vision_config()
    try:
        return max(5.0, min(300.0, float(os.environ.get("UIM_VISION_TIMEOUT", cfg.get("timeout", 90)) or 90)))
    except Exception:
        return 90.0


def _vision_cache_dir() -> Path:
    root = _library_root() / ".uim"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _vision_cache_path() -> Path:
    return _vision_cache_dir() / "vision_cache_v1.json"


def _read_vision_cache() -> Dict[str, Any]:
    path = _vision_cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_vision_cache(data: Dict[str, Any]) -> None:
    path = _vision_cache_path()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _coarse_color_name(rgb: Tuple[int, int, int]) -> str:
    r, g, b = [int(max(0, min(255, x))) for x in rgb]
    mx, mn = max(r, g, b), min(r, g, b)
    if mx < 38:
        return "black"
    if mn > 220:
        return "white"
    if mx - mn < 18:
        return "gray"
    if r > g * 1.35 and r > b * 1.35:
        return "red"
    if g > r * 1.28 and g > b * 1.22:
        return "green"
    if b > r * 1.25 and b > g * 1.15:
        return "blue"
    if r > 165 and g > 120 and b < 100:
        return "orange/yellow"
    if r > 120 and b > 120 and g < min(r, b) * 0.85:
        return "purple/magenta"
    if r > 150 and g > 130 and b > 110:
        return "beige/light neutral"
    return "mixed"


def _basic_visual_facts(path: Path) -> Dict[str, Any]:
    from PIL import Image, ImageOps, ImageStat
    with Image.open(path) as im0:
        im = ImageOps.exif_transpose(im0).convert("RGB")
        width, height = im.size
        thumb = im.copy()
        thumb.thumbnail((128, 128))
        stat = ImageStat.Stat(thumb)
        mean = tuple(int(round(x)) for x in stat.mean[:3])
        brightness = round(sum(mean) / (3.0 * 255.0), 3)
        if width > height * 1.12:
            orientation = "landscape"
        elif height > width * 1.12:
            orientation = "portrait"
        else:
            orientation = "square-ish"
        return {
            "width": int(width),
            "height": int(height),
            "orientation": orientation,
            "mean_rgb": list(mean),
            "coarse_color": _coarse_color_name(mean),
            "brightness": brightness,
        }


def _safe_input_image_path(raw: str) -> Optional[Path]:
    name = str(raw or "").strip()
    if not name:
        return None
    # Prefer ComfyUI's own annotated path resolver when available. This handles
    # names serialized as "foo.png [input]" in some versions/forks.
    if folder_paths is not None and hasattr(folder_paths, "get_annotated_filepath"):
        try:
            p = Path(folder_paths.get_annotated_filepath(name)).expanduser().resolve()
            if p.exists() and p.is_file():
                return p
        except Exception:
            pass
    cleaned = re.sub(r"\s*\[(?:input|output|temp)\]\s*$", "", name, flags=re.I)
    try:
        root = _input_root()
        p = (root / cleaned).expanduser().resolve()
        p.relative_to(root)
        return p if p.exists() and p.is_file() else None
    except Exception:
        return None


def _vision_path_for_ref(rec: Dict[str, Any]) -> Optional[Path]:
    source_kind = str(rec.get("source_kind") or "")
    file = str((rec.get("file") if source_kind == "LIBRARY" else rec.get("source_file")) or "").strip()
    if not file:
        return None
    if source_kind == "LIBRARY":
        try:
            p = _safe_library_path(file)
            return p if p.exists() and p.is_file() else None
        except Exception:
            return None
    return _safe_input_image_path(file)


def _prepare_vision_data_url(path: Path) -> str:
    from PIL import Image, ImageOps
    with Image.open(path) as im0:
        im = ImageOps.exif_transpose(im0).convert("RGB")
        im.thumbnail((1280, 1280))
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=88, optimize=True)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return "data:image/jpeg;base64," + encoded


def _extract_json_object(text: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"summary": raw}
    except Exception:
        pass
    a, b = raw.find("{"), raw.rfind("}")
    if 0 <= a < b:
        try:
            data = json.loads(raw[a:b+1])
            return data if isinstance(data, dict) else {"summary": raw}
        except Exception:
            pass
    return {"summary": raw[:1800]}


def _vision_chat_url(endpoint: str) -> str:
    url = str(endpoint or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/chat/completions"


def _call_vision_vlm(path: Path, context: str = "") -> Dict[str, Any]:
    endpoint = _vision_chat_url(_vision_endpoint())
    model = _vision_model()
    if not endpoint or not model:
        raise RuntimeError("UIM_VISION_URL and UIM_VISION_MODEL are required for VLM mode")
    system = (
        "You are a precise visual reference analyzer for image/video generation. "
        "Describe only visible facts; do not guess identity, age, ethnicity, brand, or hidden attributes. "
        "Return JSON only. Focus on details useful for reference transfer."
    )
    user_text = (
        "Analyze this reference image for controlled generation. Return a JSON object with keys: "
        "summary (short), subject_type, identity_features (visible non-sensitive appearance only), "
        "clothing, pose_motion, scene, style_lighting, product_object, colors, visible_text, usable_roles, confidence_notes, "
        "and confidence: an object mapping each semantic field to a 0.0-1.0 confidence based only on visible evidence. "
        "Use empty strings/lists when not visible. Never assign high confidence to guessed material/brand/identity facts. "
        f"The user's local instruction/context is: {str(context or '')[:1200]}"
    )
    payload = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": _prepare_vision_data_url(path)}},
            ]},
        ],
    }
    headers = {"Content-Type": "application/json"}
    key = _vision_api_key()
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_vision_timeout()) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1200]
        raise RuntimeError(f"Vision HTTP {exc.code}: {detail}")
    data = json.loads(body)
    content = ""
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        content = data.get("output_text") or data.get("response") or ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        content = "\n".join(parts)
    result = _extract_json_object(str(content))
    conf = result.get("confidence") if isinstance(result.get("confidence"), dict) else {}
    clean_conf = {}
    for k,v in conf.items():
        try: clean_conf[str(k)] = max(0.0, min(1.0, float(v)))
        except Exception: pass
    result["confidence"] = clean_conf
    result["provider"] = "OPENAI_COMPAT_VLM"
    result["model"] = model
    return result


def _vision_cache_key(path: Path, provider_key: str) -> str:
    return hashlib.sha256(
        (str(_VISION_SCHEMA_VERSION) + "|" + _VISION_PROMPT_VERSION + "|" + provider_key + "|" + _sha256_file(path)).encode("utf-8")
    ).hexdigest()


def _analyze_image_semantics(path: Path, context: str = "", force: bool = False) -> Dict[str, Any]:
    mode = _vision_mode()
    basic = _basic_visual_facts(path)
    endpoint_ready = bool(_vision_endpoint() and _vision_model())
    want_vlm = mode == "VLM" or (mode == "AUTO" and endpoint_ready)
    if mode == "OFF":
        return {"mode": "OFF", "basic": basic, "semantic_available": False}
    provider_key = f"{_vision_endpoint()}|{_vision_model()}" if want_vlm else "BASIC"
    key = _vision_cache_key(path, provider_key)
    cache = _read_vision_cache()
    if not force and key in cache and isinstance(cache[key], dict):
        result = dict(cache[key])
        result["cache_hit"] = True
        return result

    result: Dict[str, Any] = {
        "mode": "VLM" if want_vlm else "BASIC",
        "basic": basic,
        "semantic_available": False,
        "provider": "BASIC_PIXEL_FACTS",
        "summary": f"{basic['orientation']} image, {basic['width']}x{basic['height']}, coarse color={basic['coarse_color']}",
    }
    if want_vlm:
        try:
            rich = _call_vision_vlm(path, context)
            result.update(rich)
            result["semantic_available"] = True
            result["mode"] = "VLM"
        except Exception as exc:
            result["vision_error"] = f"{type(exc).__name__}: {exc}"
            if mode == "VLM" and _vision_required():
                raise
    result["file_sha256"] = _sha256_file(path)
    result["cache_hit"] = False
    cache[key] = result
    # Keep cache bounded; newest insertions are at the end in modern Python.
    if len(cache) > 600:
        for old in list(cache.keys())[: len(cache) - 600]:
            cache.pop(old, None)
    _write_vision_cache(cache)
    return result


def _vision_context_for_ref(rec: Dict[str, Any]) -> str:
    return " / ".join(dict.fromkeys([str(x) for x in (rec.get("instructions") or rec.get("contexts") or []) if x]))[:1400]


def _enrich_refs_with_vision(refs: List[Dict[str, Any]], adapter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Attach pixel facts and optional rich semantics to reference records.

    Connected images are resolved back to the actual LoadImage filename whenever
    graph tracing found it. If no file path is available (e.g. procedural image),
    the target model can still inspect the pixels through its native reference path.
    """
    for rec in refs:
        path = _vision_path_for_ref(rec)
        if path is not None:
            try:
                rec["vision"] = _analyze_image_semantics(path, _vision_context_for_ref(rec))
            except Exception as exc:
                rec["vision"] = {"semantic_available": False, "vision_error": f"{type(exc).__name__}: {exc}"}
        else:
            rec["vision"] = {
                "semantic_available": False,
                "mode": "NATIVE_TARGET_ONLY",
                "note": "No disk-backed image path was traceable; the target reference mechanism still receives the actual image tensor.",
            }
        if adapter and adapter.get("native_vision"):
            rec["vision"]["native_target_vision"] = True
    return refs


def _vision_field_confidence(v: Dict[str, Any], field: str) -> float:
    conf = v.get("confidence") if isinstance(v.get("confidence"), dict) else {}
    if field not in conf:
        return 1.0  # backward-compatible for older cached VLM results
    try:
        return max(0.0, min(1.0, float(conf.get(field))))
    except Exception:
        return 0.0


def _vision_semantic_lines(refs: List[Dict[str, Any]], tag_template: str, graph: Optional[Dict[str, Any]] = None) -> List[str]:
    lines: List[str] = []
    graph = graph or {}
    focused: Dict[int, set] = {}
    target_ids = set()
    for rel in graph.get("relations") or []:
        try:
            source, target = int(rel.get("source")), int(rel.get("target"))
        except Exception:
            continue
        focused.setdefault(source, set()).add(str(rel.get("attribute") or "GENERAL_REFERENCE"))
        target_ids.add(target)
    for role, idx in (graph.get("assignments") or {}).items():
        try:
            focused.setdefault(int(idx), set()).add(str(role))
        except Exception:
            pass
    # Edit targets should be grounded primarily by their identity/base visual
    # features, while source references are narrowed to the requested transfer
    # attribute. This reduces cross-reference identity leakage.
    for idx in target_ids:
        focused.setdefault(int(idx), set()).add("IDENTITY")

    field_map = {
        "IDENTITY": (("identity_features", "visible identity/appearance"), ("colors", "colors")),
        "CLOTHING": (("clothing", "clothing"), ("colors", "colors"), ("style_lighting", "material/style cues")),
        "POSE_MOTION": (("pose_motion", "pose/motion"),),
        "SCENE": (("scene", "scene"), ("style_lighting", "lighting"), ("colors", "colors")),
        "STYLE": (("style_lighting", "style/lighting"), ("colors", "colors")),
        "PRODUCT_OBJECT": (("product_object", "product/object"), ("colors", "colors"), ("visible_text", "visible text")),
        "GENERAL_REFERENCE": (
            ("summary", "summary"), ("identity_features", "visible identity/appearance"),
            ("clothing", "clothing"), ("pose_motion", "pose/motion"),
            ("scene", "scene"), ("style_lighting", "style/lighting"),
            ("product_object", "product/object"), ("colors", "colors"),
            ("visible_text", "visible text"),
        ),
    }

    for rec in refs:
        v = rec.get("vision") or {}
        idx = int(rec.get("index", 0))
        tag = tag_template.format(i=idx, index=idx, alias=rec.get("alias", ""), file=rec.get("file") or "")
        attrs = focused.get(idx) or set(rec.get("roles") or ["GENERAL_REFERENCE"])
        # EDIT is an operation, not a visual field.
        attrs.discard("EDIT")
        if not attrs:
            attrs = {"GENERAL_REFERENCE"}
        if v.get("semantic_available"):
            requested_fields = []
            for attr in attrs:
                requested_fields.extend(field_map.get(attr, field_map["GENERAL_REFERENCE"]))
            seen_keys = set()
            fields = []
            for key, label in requested_fields:
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                value = v.get(key)
                confidence = _vision_field_confidence(v, key)
                if confidence < float(_read_v4_config().get("vision_min_confidence", 0.55)):
                    continue
                if value not in (None, "", [], {}):
                    if isinstance(value, (list, dict)):
                        value = json.dumps(value, ensure_ascii=False)
                    fields.append(f"{label}={str(value)[:420]}")
            if fields:
                scope = ", ".join(sorted(attrs))
                lines.append(f"{tag} visual reading for role(s) {scope}: " + "; ".join(fields) + ".")
        elif v.get("native_target_vision"):
            scope = ", ".join(sorted(attrs))
            lines.append(
                f"{tag}: inspect the actual connected image pixels before applying the instruction. "
                f"Ground only the visually supported attributes for role(s) {scope}; do not invent unseen details or transfer unrelated identity."
            )
        else:
            basic = v.get("basic") or {}
            if basic:
                lines.append(
                    f"{tag} basic pixel facts: {basic.get('width')}x{basic.get('height')}, "
                    f"{basic.get('orientation')}, coarse color={basic.get('coarse_color')}."
                )
    return lines


def _parse(
    prompt: str,
    replacement_mode: str,
    custom_template: str,
    strict_missing: bool,
    search_subfolders: bool,
    max_refs: int,
) -> Tuple[str, str, List[Dict[str, Any]], List[Optional[Any]]]:
    text = str(prompt or "")
    max_refs = max(1, min(int(max_refs), MAX_OUTPUT_REFS))
    matches = list(_MENTION_RE.finditer(text))
    if not matches:
        return text, text, [], [None] * MAX_OUTPUT_REFS

    index = _build_index(bool(search_subfolders))
    refs: List[Dict[str, Any]] = []
    by_file: Dict[str, Dict[str, Any]] = {}
    replacements: List[Tuple[int, int, str]] = []
    clean_replacements: List[Tuple[int, int, str]] = []
    missing: List[str] = []

    for match in matches:
        token = _mention_token(match)
        try:
            found = _resolve_token(token, index)
        except Exception as exc:
            if strict_missing:
                raise
            missing.append(str(exc))
            continue

        key = found["rel"]
        if key not in by_file:
            if len(refs) >= max_refs:
                raise ValueError(
                    f"Prompt contains more than max_refs={max_refs} unique @images. "
                    f"This node exposes at most {MAX_OUTPUT_REFS} image outputs."
                )
            rec: Dict[str, Any] = {
                "index": len(refs) + 1,
                "alias": token,
                "file": found["rel"],
                "uim_id": found.get("uim_id"),
                "sha256": found.get("sha256"),
                "roles": [],
                "contexts": [],
            }
            by_file[key] = rec
            refs.append(rec)

        rec = by_file[key]
        context = _context_window(text, match.start(), match.end())
        rec["contexts"].append(context)
        for role in _roles_for_context(context):
            if role not in rec["roles"]:
                rec["roles"].append(role)

        repl = _replacement(
            replacement_mode,
            custom_template,
            int(rec["index"]),
            str(token),
            str(found["rel"]),
            match.group(0),
        )
        replacements.append((match.start(), match.end(), repl))
        clean_replacements.append((match.start(), match.end(), ""))

    resolved = text
    for start, end, repl in reversed(replacements):
        resolved = resolved[:start] + repl + resolved[end:]

    clean = text
    for start, end, repl in reversed(clean_replacements):
        clean = clean[:start] + repl + clean[end:]
    clean = re.sub(r"[ \t]+", " ", clean).strip()

    images: List[Optional[Any]] = [None] * MAX_OUTPUT_REFS
    for rec in refs:
        contexts = list(dict.fromkeys(rec.pop("contexts", [])))
        rec["context"] = " | ".join(contexts)[:600]
        images[int(rec["index"]) - 1] = _load_library_image(str(rec["file"]))

    if missing:
        # Preserve diagnostics in manifest without mutating the prompt.
        refs.append({"_warnings": missing})

    return resolved, clean, refs, images


def _manifest(prompt: str, resolved: str, clean: str, refs: List[Dict[str, Any]], mode: str) -> Dict[str, Any]:
    real_refs = [r for r in refs if "index" in r]
    warnings: List[str] = []
    for r in refs:
        warnings.extend(r.get("_warnings", []))
    return {
        "plugin": PLUGIN_NAME,
        "version": PLUGIN_VERSION,
        "replacement_mode": mode,
        "library_path": str(_library_root()),
        "reference_count": len(real_refs),
        "references": real_refs,
        "warnings": warnings,
        "original_prompt": prompt,
        "resolved_prompt": resolved,
        "clean_prompt": clean,
    }


def _bank_from(refs: List[Dict[str, Any]], images: List[Optional[Any]]) -> Dict[str, Any]:
    items = []
    for rec in refs:
        if "index" not in rec:
            continue
        idx = int(rec["index"])
        items.append({"meta": dict(rec), "image": images[idx - 1]})
    return {
        "plugin": PLUGIN_NAME,
        "version": PLUGIN_VERSION,
        "library_path": str(_library_root()),
        "items": items,
    }



# =============================================================================
# V4 Multimodal Mention Controls
# =============================================================================
# V4 keeps the canonical prompt as plain text for maximum ComfyUI compatibility,
# while the frontend stores rich chip metadata in workflow-node properties.
# Backend execution reads those properties and turns them into deterministic
# role/strength/mask guidance. When a target exposes native weight/mask sockets
# the adapter can use them; otherwise mask is applied to the reference pixels and
# strength is expressed as explicit semantic guidance rather than silently faked.

_V4_META_VERSION = 2
_V4_ROLES = {
    "AUTO", "IDENTITY", "FACE", "HAIR", "CLOTHING", "POSE_MOTION",
    "PRODUCT_OBJECT", "SCENE", "STYLE", "GENERAL_REFERENCE",
}


def _v4_config_path() -> Path:
    return _vision_cache_dir() / "v4_config.json"


def _read_v4_config() -> Dict[str, Any]:
    defaults = {
        "rich_chips": True,
        "auto_adapter": True,
        "audit_enabled": False,
        "audit_auto_retry": False,
        "audit_threshold": 0.78,
        "audit_min_confidence": 0.55,
        "audit_critical_floor": 0.58,
        "vision_min_confidence": 0.55,
        "audit_max_retries": 1,
        "audit_output_node": "",
    }
    try:
        data = json.loads(_v4_config_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            defaults.update(data)
    except Exception:
        pass
    defaults["audit_threshold"] = max(0.0, min(1.0, float(defaults.get("audit_threshold", 0.78) or 0.78)))
    defaults["audit_min_confidence"] = max(0.0, min(1.0, float(defaults.get("audit_min_confidence", 0.55) or 0.55)))
    defaults["audit_critical_floor"] = max(0.0, min(1.0, float(defaults.get("audit_critical_floor", 0.58) or 0.58)))
    defaults["vision_min_confidence"] = max(0.0, min(1.0, float(defaults.get("vision_min_confidence", 0.55) or 0.55)))
    defaults["audit_max_retries"] = max(0, min(5, int(defaults.get("audit_max_retries", 1) or 1)))
    return defaults


def _write_v4_config(data: Dict[str, Any]) -> Dict[str, Any]:
    current = _read_v4_config()
    for key in list(current):
        if key in data:
            current[key] = data[key]
    current["rich_chips"] = bool(current.get("rich_chips", True))
    current["auto_adapter"] = bool(current.get("auto_adapter", True))
    current["audit_enabled"] = bool(current.get("audit_enabled", False))
    current["audit_auto_retry"] = bool(current.get("audit_auto_retry", False))
    current["audit_threshold"] = max(0.0, min(1.0, float(current.get("audit_threshold", 0.78) or 0.78)))
    current["audit_min_confidence"] = max(0.0, min(1.0, float(current.get("audit_min_confidence", 0.55) or 0.55)))
    current["audit_critical_floor"] = max(0.0, min(1.0, float(current.get("audit_critical_floor", 0.58) or 0.58)))
    current["vision_min_confidence"] = max(0.0, min(1.0, float(current.get("vision_min_confidence", 0.55) or 0.55)))
    current["audit_max_retries"] = max(0, min(5, int(current.get("audit_max_retries", 1) or 1)))
    current["audit_output_node"] = str(current.get("audit_output_node", "") or "")
    path = _v4_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return current


def _canonical_mention_key(token: str) -> str:
    raw = str(token or "").strip()
    n = _slot_alias_number(raw) if "_slot_alias_number" in globals() else None
    if n is not None:
        return f"slot:{int(n)}"
    return "name:" + _norm(_strip_known_image_ext(raw.replace("\\", "/")))


def _ui_v4_meta(ui_node: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(ui_node, dict):
        return {}
    props = ui_node.get("properties")
    if not isinstance(props, dict):
        return {}
    meta = props.get("uim_v4")
    return meta if isinstance(meta, dict) else {}


def _ref_control_keys(rec: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    if rec.get("uim_id"):
        out.append("id:" + str(rec.get("uim_id")))
    if str(rec.get("source_kind")) == "CONNECTED_SLOT":
        try:
            out.append(f"slot:{int(rec.get('index', 0))}")
        except Exception:
            pass
    for raw in (rec.get("file"), rec.get("source_file"), rec.get("alias")):
        if raw:
            out.append("name:" + _norm(_strip_known_image_ext(str(raw).replace("\\", "/"))))
            try:
                out.append("name:" + _norm(Path(str(raw)).stem))
            except Exception:
                pass
    return list(dict.fromkeys(x for x in out if x and not x.endswith(":")))


def _merge_v4_ref_controls(refs: List[Dict[str, Any]], source_ui: Optional[Dict[str, Any]], target_ui: Optional[Dict[str, Any]] = None) -> None:
    merged_mentions: Dict[str, Any] = {}
    for node in (target_ui, source_ui):
        meta = _ui_v4_meta(node)
        vals = meta.get("mentions") if isinstance(meta, dict) else None
        if isinstance(vals, dict):
            merged_mentions.update(vals)

    for rec in refs:
        control: Dict[str, Any] = {}
        for key in _ref_control_keys(rec):
            value = merged_mentions.get(key)
            if isinstance(value, dict):
                control.update(value)
        role = str(control.get("role", "AUTO") or "AUTO").upper()
        if role not in _V4_ROLES:
            role = "AUTO"
        try:
            strength = max(0.0, min(2.0, float(control.get("strength", 1.0) or 1.0)))
        except Exception:
            strength = 1.0
        mask_rel = str(control.get("mask_rel", "") or "").strip()
        mask_mode = str(control.get("mask_mode", "FOCUS") or "FOCUS").upper()
        if mask_mode not in {"FOCUS", "INVERT"}:
            mask_mode = "FOCUS"
        try:
            order = int(control.get("order", 0) or 0)
        except Exception:
            order = 0
        rec["v4"] = {
            "role": role,
            "strength": strength,
            "mask_rel": mask_rel,
            "mask_mode": mask_mode,
            "order": order,
            "strength_effective": "DEFAULT",
            "mask_effective": "NONE",
        }
        if role != "AUTO":
            mapped = "IDENTITY" if role in {"FACE", "HAIR"} else role
            if mapped not in rec.setdefault("roles", []):
                rec["roles"].insert(0, mapped)



def _apply_v4_library_order_for_target(
    refs: List[Dict[str, Any]],
    occurrences: List[Tuple[int, int, int]],
    target_id: str,
    adapter: Dict[str, Any],
    prompt_graph: Dict[str, Any],
) -> List[Tuple[int, int, int]]:
    """Apply drag order to library refs without disturbing connected slots.

    Connected @1/@2 are physical user wires and remain stable. Library chips can
    be dragged; their `order` metadata determines which free Picture slot they
    occupy. This is deterministic and safe even when only some references are
    mentioned.
    """
    target = prompt_graph.get(str(target_id), {})
    inputs = target.get("inputs", {}) if isinstance(target, dict) and isinstance(target.get("inputs"), dict) else {}
    slots = list(adapter.get("slots") or [])
    if not slots:
        return occurrences
    connected_positions = {i + 1 for i, slot in enumerate(slots) if _nonempty_input(inputs.get(str(slot)))}
    libs = [r for r in refs if r.get("source_kind") == "LIBRARY"]
    if len(libs) < 2 or not any(int((r.get("v4") or {}).get("order", 0) or 0) > 0 for r in libs):
        return occurrences
    libs.sort(key=lambda r: (int((r.get("v4") or {}).get("order", 0) or 10**6), int(r.get("index", 0))))
    free = [i for i in range(1, len(slots) + 1) if i not in connected_positions]
    if len(free) < len(libs):
        return occurrences
    remap: Dict[int, int] = {}
    for rec, new_idx in zip(libs, free):
        old_idx = int(rec.get("index", 0))
        remap[old_idx] = int(new_idx)
        rec["index"] = int(new_idx)
        rec["slot"] = str(slots[new_idx - 1])
    refs.sort(key=lambda r: int(r.get("index", 0)))
    return [(a, b, remap.get(int(idx), int(idx))) for a, b, idx in occurrences]


def _apply_v4_library_order_for_chain(
    refs: List[Dict[str, Any]],
    occurrences: List[Tuple[int, int, int]],
    profile: Dict[str, Any],
) -> List[Tuple[int, int, int]]:
    libs = [r for r in refs if r.get("source_kind") == "LIBRARY"]
    if len(libs) < 2 or not any(int((r.get("v4") or {}).get("order", 0) or 0) > 0 for r in libs):
        return occurrences
    start = int(profile.get("existing_count", 0)) + 1
    libs.sort(key=lambda r: (int((r.get("v4") or {}).get("order", 0) or 10**6), int(r.get("index", 0))))
    remap: Dict[int, int] = {}
    for offset, rec in enumerate(libs):
        new_idx = start + offset
        old_idx = int(rec.get("index", 0))
        remap[old_idx] = new_idx
        rec["index"] = new_idx
        rec["slot"] = f"ReferenceLatent {new_idx}"
    refs.sort(key=lambda r: int(r.get("index", 0)))
    return [(a, b, remap.get(int(idx), int(idx))) for a, b, idx in occurrences]


def _v4_control_lines(refs: List[Dict[str, Any]], tag_template: str) -> List[str]:
    lines: List[str] = []
    role_text = {
        "IDENTITY": "identity/person only",
        "FACE": "face/facial features only",
        "HAIR": "hair only",
        "CLOTHING": "clothing/garment only",
        "POSE_MOTION": "pose/motion only",
        "PRODUCT_OBJECT": "product/object only",
        "SCENE": "scene/background only",
        "STYLE": "style/visual treatment only",
        "GENERAL_REFERENCE": "general visual reference",
    }
    for rec in refs:
        ctl = rec.get("v4") if isinstance(rec.get("v4"), dict) else {}
        if not ctl:
            continue
        idx = int(rec.get("index", 0))
        tag = tag_template.format(i=idx, index=idx, alias=rec.get("alias", ""), file=rec.get("file") or "")
        role = str(ctl.get("role", "AUTO"))
        strength = float(ctl.get("strength", 1.0) or 1.0)
        parts = []
        if role != "AUTO":
            parts.append(f"locked role={role_text.get(role, role)}")
        if abs(strength - 1.0) > 0.001:
            if strength < 0.35:
                desc = "very weak"
            elif strength < 0.75:
                desc = "moderate"
            elif strength <= 1.15:
                desc = "normal"
            elif strength <= 1.55:
                desc = "strong"
            else:
                desc = "very strong"
            parts.append(f"reference priority={strength:.2f} ({desc})")
        if ctl.get("mask_rel"):
            mmode=str(ctl.get("mask_effective") or "PREPROCESS")
            parts.append(f"mask mode={mmode}; use only the user-selected masked visual region; ignore outside-region details")
        if parts:
            lines.append(f"{tag}: " + "; ".join(parts) + ".")
    return lines


def _mask_path(rel: str) -> Path:
    raw = str(rel or "").replace("\\", "/").strip()
    if not raw.startswith(".uim/masks/"):
        raise ValueError("V4 mask must be stored below .uim/masks/")
    return _safe_library_path(raw)


def _load_mask_tensor(mask_rel: str, width: int, height: int):
    import numpy as np
    import torch
    from PIL import Image
    path = _mask_path(mask_rel)
    if not path.exists():
        raise FileNotFoundError(f"Reference mask not found: {path}")
    with Image.open(path) as im:
        im = im.convert("L").resize((int(width), int(height)), getattr(Image, "Resampling", Image).BILINEAR)
        arr = np.asarray(im).astype(np.float32) / 255.0
    return torch.from_numpy(arr)[None,]


def _inject_mask_node(prompt_graph: Dict[str, Any], image_conn: Any, rec: Dict[str, Any], counter: List[int]) -> Any:
    ctl = rec.get("v4") if isinstance(rec.get("v4"), dict) else {}
    mask_rel = str(ctl.get("mask_rel", "") or "")
    if not mask_rel:
        return image_conn
    if str(ctl.get("mask_effective") or "") == "NATIVE":
        return image_conn
    ctl["mask_effective"] = "PREPROCESS"
    # Validate path now so Queue fails before expensive generation.
    _mask_path(mask_rel)
    node_id = _alloc_prompt_node_id(prompt_graph, counter)
    prompt_graph[node_id] = {
        "class_type": "UniversalMentionApplyMask",
        "inputs": {
            "image": image_conn,
            "mask_rel": mask_rel,
            "mode": str(ctl.get("mask_mode", "FOCUS") or "FOCUS"),
            "outside_level": 0.5,
        },
    }
    return [node_id, 0]


def _apply_connected_masks_direct(prompt_graph: Dict[str, Any], target_id: str, refs: List[Dict[str, Any]], counter: List[int]) -> List[Dict[str, Any]]:
    target = prompt_graph.get(str(target_id))
    if not isinstance(target, dict):
        return []
    inputs = target.get("inputs") if isinstance(target.get("inputs"), dict) else {}
    applied = []
    for rec in refs:
        if rec.get("source_kind") != "CONNECTED_SLOT":
            continue
        ctl = rec.get("v4") if isinstance(rec.get("v4"), dict) else {}
        if not ctl.get("mask_rel"):
            continue
        slot = str(rec.get("slot") or "")
        old = inputs.get(slot)
        if _conn(old) is None:
            continue
        new_conn = _inject_mask_node(prompt_graph, old, rec, counter)
        inputs[slot] = new_conn
        applied.append({"index": rec.get("index"), "slot": slot, "mask_rel": ctl.get("mask_rel")})
    return applied


def _apply_existing_reference_latent_masks(prompt_graph: Dict[str, Any], profile: Dict[str, Any], refs: List[Dict[str, Any]], counter: List[int]) -> List[Dict[str, Any]]:
    existing = {int(x.get("index", 0)): x for x in (profile.get("existing_refs") or []) if isinstance(x, dict)}
    applied = []
    for rec in refs:
        if rec.get("source_kind") != "CONNECTED_SLOT":
            continue
        ctl = rec.get("v4") if isinstance(rec.get("v4"), dict) else {}
        if not ctl.get("mask_rel"):
            continue
        info = existing.get(int(rec.get("index", 0)))
        latent = _conn((info or {}).get("latent"))
        if latent is None:
            continue
        enc = prompt_graph.get(latent[0])
        if not isinstance(enc, dict) or str(enc.get("class_type") or "") != "VAEEncode":
            continue
        einputs = enc.get("inputs") if isinstance(enc.get("inputs"), dict) else {}
        pixels = einputs.get("pixels")
        if _conn(pixels) is None:
            continue
        einputs["pixels"] = _inject_mask_node(prompt_graph, pixels, rec, counter)
        applied.append({"index": rec.get("index"), "vae_encode": latent[0], "mask_rel": ctl.get("mask_rel")})
    return applied


def _write_mask_data_url(data_url: str) -> str:
    raw = str(data_url or "")
    m = re.match(r"^data:image/png;base64,(.+)$", raw, flags=re.I | re.S)
    if not m:
        raise ValueError("mask_data_url must be a PNG data URL")
    blob = base64.b64decode(m.group(1), validate=False)
    if len(blob) > 32 * 1024 * 1024:
        raise ValueError("Mask PNG is too large")
    from PIL import Image
    with Image.open(BytesIO(blob)) as im:
        im = im.convert("L")
        digest = hashlib.sha256(im.tobytes()).hexdigest()[:24]
        rel = f".uim/masks/{digest}.png"
        path = _safe_library_path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        im.save(path, format="PNG", optimize=True)
    return rel


def _adapter_manifest_path() -> Path:
    return _vision_cache_dir() / "adapters_v1.json"


def _read_adapter_manifests() -> Dict[str, Any]:
    try:
        data = json.loads(_adapter_manifest_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_adapter_manifests(data: Dict[str, Any]) -> None:
    clean: Dict[str, Any] = {}
    for klass, cfg in (data or {}).items():
        if not isinstance(cfg, dict):
            continue
        slots = [str(x) for x in (cfg.get("slots") or []) if str(x).strip()][:32]
        if not slots:
            continue
        caps = cfg.get("capabilities") if isinstance(cfg.get("capabilities"), dict) else {}
        clean[str(klass)] = {
            "name": str(cfg.get("name") or f"USER_{klass}"),
            "slots": slots,
            "max_refs": max(1, min(32, int(cfg.get("max_refs", len(slots)) or len(slots)))),
            "tag_template": str(cfg.get("tag_template") or "Reference Image {i}"),
            "prompt_input": str(cfg.get("prompt_input") or ""),
            "native_vision": bool(cfg.get("native_vision", True)),
            "replace_existing": bool(cfg.get("replace_existing", False)),
            "strength_map": {str(k): str(v) for k,v in (cfg.get("strength_map") or {}).items()} if isinstance(cfg.get("strength_map"), dict) else {},
            "mask_map": {str(k): str(v) for k,v in (cfg.get("mask_map") or {}).items()} if isinstance(cfg.get("mask_map"), dict) else {},
            "capabilities": {
                "supports_reference_image": bool(caps.get("supports_reference_image", True)),
                "supports_multi_reference": bool(caps.get("supports_multi_reference", len(slots) > 1)),
                "supports_native_strength": bool(caps.get("supports_native_strength", bool(cfg.get("strength_map")))),
                "supports_native_mask": bool(caps.get("supports_native_mask", bool(cfg.get("mask_map")))),
                "confidence": str(caps.get("confidence", "USER_VERIFIED") or "USER_VERIFIED"),
                "reference_semantics": [str(x) for x in (caps.get("reference_semantics") or [])][:16],
            },
        }
    path = _adapter_manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def _user_adapter_for_class(class_type: str) -> Optional[Dict[str, Any]]:
    cfg = _read_adapter_manifests().get(str(class_type))
    if not isinstance(cfg, dict):
        return None
    out = dict(cfg)
    out["name"] = str(out.get("name") or f"USER_{class_type}")
    out["append_bindings"] = True
    out["user_manifest"] = True
    return out


def _simple_token_set(value: Any) -> set:
    raw = _norm(value)
    return {x for x in re.findall(r"[a-z0-9_\-]+|[\u4e00-\u9fff]{1,4}", raw) if len(x) >= 2}


def _audit_similarity(a: Any, b: Any) -> float:
    aa, bb = _simple_token_set(a), _simple_token_set(b)
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / max(1, len(aa | bb))


_AUDIT_DIMENSIONS = {
    "CLOTHING": ["garment_category", "color", "silhouette_fit", "neckline", "sleeves", "material_texture", "pattern_details"],
    "POSE_MOTION": ["body_pose", "limb_positions", "gesture", "motion_direction"],
    "IDENTITY": ["face_identity", "hair", "body_identity", "skin_tone_visible"],
    "SCENE": ["scene_type", "layout", "background_objects", "lighting", "palette"],
    "STYLE": ["render_style", "lighting", "texture", "palette", "composition"],
    "PRODUCT_OBJECT": ["object_category", "shape_structure", "color", "material", "logos_text", "small_details"],
    "GENERAL_REFERENCE": ["overall_structure", "color", "details"],
}
_AUDIT_CRITICAL = {
    "CLOTHING": {"garment_category", "silhouette_fit"},
    "POSE_MOTION": {"body_pose"},
    "IDENTITY": {"face_identity"},
    "SCENE": {"scene_type", "layout"},
    "STYLE": {"render_style"},
    "PRODUCT_OBJECT": {"object_category", "shape_structure"},
    "GENERAL_REFERENCE": {"overall_structure"},
}
_AUDIT_COMPARE_PROMPT_VERSION = "uim-v42-audit-2"


def _audit_compare_cache_path() -> Path:
    return _vision_cache_dir() / "audit_compare_cache_v1.json"


def _read_audit_compare_cache() -> Dict[str, Any]:
    try:
        data=json.loads(_audit_compare_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data,dict) else {}
    except Exception:
        return {}


def _write_audit_compare_cache(data: Dict[str, Any]) -> None:
    path=_audit_compare_cache_path(); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(".tmp"); tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8"); tmp.replace(path)


def _audit_log_path() -> Path:
    return _vision_cache_dir() / "audit_log.jsonl"


def _append_audit_log(record: Dict[str, Any]) -> None:
    path=_audit_log_path(); path.parent.mkdir(parents=True,exist_ok=True)
    lines=[]
    try: lines=path.read_text(encoding="utf-8").splitlines()[-599:]
    except Exception: pass
    lines.append(json.dumps(record,ensure_ascii=False,separators=(",",":")))
    path.write_text("\n".join(lines)+"\n",encoding="utf-8")


def _recent_audit_log(limit: int = 50) -> List[Dict[str, Any]]:
    try: raw=_audit_log_path().read_text(encoding="utf-8").splitlines()[-max(1,min(500,int(limit))):]
    except Exception: return []
    out=[]
    for line in raw:
        try:
            item=json.loads(line)
            if isinstance(item,dict): out.append(item)
        except Exception: pass
    return out


def _previous_audit_for_root(root_run_id: str, exclude_run_id: str = "") -> Optional[Dict[str, Any]]:
    if not root_run_id: return None
    for item in reversed(_recent_audit_log(300)):
        run=item.get("run") if isinstance(item.get("run"),dict) else {}
        if str(run.get("root_run_id") or "") == str(root_run_id) and str(run.get("run_id") or "") != str(exclude_run_id or ""):
            return item
    return None


def _normalize_audit_comparison(result: Dict[str, Any], attribute: str) -> Dict[str, Any]:
    attr=str(attribute or "GENERAL_REFERENCE").upper()
    expected=_AUDIT_DIMENSIONS.get(attr,_AUDIT_DIMENSIONS["GENERAL_REFERENCE"])
    try: overall=max(0.0,min(1.0,float(result.get("score",0.0) or 0.0)))
    except Exception: overall=0.0
    try: overall_conf=max(0.0,min(1.0,float(result.get("confidence",1.0) or 1.0)))
    except Exception: overall_conf=0.0
    raw_dims=result.get("dimensions") if isinstance(result.get("dimensions"),dict) else {}
    dims={}
    for name in expected:
        raw=raw_dims.get(name)
        if isinstance(raw,dict):
            try: score=max(0.0,min(1.0,float(raw.get("score",overall) or 0.0)))
            except Exception: score=overall
            try: conf=max(0.0,min(1.0,float(raw.get("confidence",overall_conf) or 0.0)))
            except Exception: conf=overall_conf
            dims[name]={"score":score,"confidence":conf,"matched":raw.get("matched") or raw.get("matched_details") or "","missing":raw.get("missing") or raw.get("missing_or_wrong_details") or "","correction":raw.get("correction") or raw.get("correction_instruction") or ""}
        elif isinstance(raw,(int,float)):
            dims[name]={"score":max(0.0,min(1.0,float(raw))),"confidence":overall_conf,"matched":"","missing":"","correction":""}
    if dims:
        usable=[d for d in dims.values() if d["confidence"]>0]
        if usable:
            denom=sum(d["confidence"] for d in usable)
            overall=sum(d["score"]*d["confidence"] for d in usable)/max(1e-9,denom)
            overall_conf=sum(d["confidence"] for d in usable)/len(usable)
    result=dict(result); result["score"]=round(overall,4); result["confidence"]=round(overall_conf,4); result["dimensions"]=dims
    return result


def _call_vision_compare(generated_path: Path, reference_path: Path, attribute: str, mode: str = "TRANSFER") -> Dict[str, Any]:
    endpoint = _vision_chat_url(_vision_endpoint()); model = _vision_model()
    if not endpoint or not model: raise RuntimeError("Vision endpoint/model is not configured")
    attr=str(attribute or "GENERAL_REFERENCE").upper(); dims=_AUDIT_DIMENSIONS.get(attr,_AUDIT_DIMENSIONS["GENERAL_REFERENCE"])
    system=("You are a strict visual audit engine for controlled image generation. Image 1 is the generated result; Image 2 is the reference. "
            "Compare only the requested attribute. Do not reward unrelated similarities. Report uncertainty instead of guessing. Return JSON only.")
    user=(f"Audit mode={mode}; requested attribute={attr}. Score these exact dimensions: {', '.join(dims)}. "
          "Return JSON: score 0..1, confidence 0..1, dimensions object where every dimension has score, confidence, matched, missing, correction; "
          "also correction_instruction. Low visibility/occlusion must lower confidence, not invent details.")
    payload={"model":model,"temperature":0,"max_tokens":1500,"messages":[{"role":"system","content":system},{"role":"user","content":[{"type":"text","text":user},{"type":"image_url","image_url":{"url":_prepare_vision_data_url(generated_path)}},{"type":"image_url","image_url":{"url":_prepare_vision_data_url(reference_path)}}]}]}
    headers={"Content-Type":"application/json"}; key=_vision_api_key()
    if key: headers["Authorization"]=f"Bearer {key}"
    request=urllib.request.Request(endpoint,data=json.dumps(payload).encode("utf-8"),headers=headers,method="POST")
    with urllib.request.urlopen(request,timeout=_vision_timeout()) as resp: body=json.loads(resp.read().decode("utf-8",errors="replace"))
    content=body.get("choices",[{}])[0].get("message",{}).get("content","")
    if isinstance(content,list): content="".join(str(x.get("text","")) if isinstance(x,dict) else str(x) for x in content)
    return _normalize_audit_comparison(_extract_json_object(str(content)),attr)


def _cached_vision_compare(generated_path: Path, reference_path: Path, attribute: str, mode: str = "TRANSFER") -> Dict[str, Any]:
    key=hashlib.sha256((f"{_AUDIT_COMPARE_PROMPT_VERSION}|{_vision_model()}|{mode}|{attribute}|{_sha256_file(generated_path)}|{_sha256_file(reference_path)}").encode()).hexdigest()
    cache=_read_audit_compare_cache()
    if key in cache and isinstance(cache[key],dict):
        out=dict(cache[key]); out["cache_hit"]=True; return out
    out=_call_vision_compare(generated_path,reference_path,attribute,mode); out["cache_hit"]=False; cache[key]=out
    if len(cache)>400:
        for k in list(cache)[:len(cache)-400]: cache.pop(k,None)
    try: _write_audit_compare_cache(cache)
    except Exception: pass
    return out


def _audit_relation(path: Path, ref: Dict[str, Any], attribute: str, mode: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    ref_path=_vision_path_for_ref(ref); attr=str(attribute or "GENERAL_REFERENCE").upper()
    if ref_path is None or not ref_path.exists():
        return {"attribute":attr,"mode":mode,"score":0.0,"confidence":0.0,"status":"UNTRACEABLE","dimensions":{},"failed_dimensions":[],"comparison":{"error":"Reference image path is not traceable."}}
    try: comp=_cached_vision_compare(path,ref_path,attr,mode)
    except Exception as exc: return {"attribute":attr,"mode":mode,"score":0.0,"confidence":0.0,"status":"ERROR","dimensions":{},"failed_dimensions":[],"comparison":{"error":f"{type(exc).__name__}: {exc}"}}
    min_conf=float(cfg.get("audit_min_confidence",0.55)); threshold=float(cfg.get("audit_threshold",0.78)); floor=float(cfg.get("audit_critical_floor",0.58))
    failed=[]; reliable=[]; reliable_values=[]; critical=_AUDIT_CRITICAL.get(attr,set())
    for name,d in (comp.get("dimensions") or {}).items():
        conf=float(d.get("confidence",0.0) or 0.0); score=float(d.get("score",0.0) or 0.0)
        if conf>=min_conf:
            reliable.append(name); reliable_values.append((score,conf))
            if score<threshold or (name in critical and score<floor): failed.append(name)
    # Decision score deliberately excludes low-confidence dimensions. The raw VLM
    # aggregate is kept under `comparison`, but uncertain visual guesses cannot
    # drag a reliable relation into FAIL or trigger an automatic retry.
    if reliable_values:
        denom=sum(c for _,c in reliable_values)
        score=sum(v*c for v,c in reliable_values)/max(1e-9,denom)
        confidence=sum(c for _,c in reliable_values)/len(reliable_values)
    else:
        score=float(comp.get("score",0.0) or 0.0); confidence=float(comp.get("confidence",0.0) or 0.0)
    status="INCONCLUSIVE" if not reliable else ("FAIL" if failed or score<threshold else "PASS")
    return {"attribute":attr,"mode":mode,"score":round(score,3),"confidence":round(confidence,3),"status":status,"dimensions":comp.get("dimensions") or {},"failed_dimensions":failed,"comparison":comp}


def _audit_generated_image(path: Path, bind_report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg=_read_v4_config()
    if _vision_mode() in {"OFF","BASIC"} or not (_vision_endpoint() and _vision_model()): return {"ok":False,"reason":"V4.2 Audit Engine requires configured VLM Vision mode."}
    generated=_analyze_image_semantics(path,"Audit generated result; report visible facts and confidence.",True)
    active_bind=bind_report if isinstance(bind_report,dict) else _LAST_BIND_REPORT; reports=list((active_bind or {}).get("reports") or []); run=dict((active_bind or {}).get("run") or {})
    relation_reports=[]; retry_hints=[]; reliable_scores=[]; all_failed=[]; audited_preserve=set()
    for rep in reports:
        graph=rep.get("relationship_graph") if isinstance(rep,dict) else None; refs=rep.get("references") if isinstance(rep,dict) else None
        if not isinstance(graph,dict) or not isinstance(refs,list): continue
        by_idx={int(r.get("index",0)):r for r in refs if isinstance(r,dict)}
        # V3 graph uses `relations`; accept old `transfers` for compatibility.
        for edge in (graph.get("relations") or graph.get("transfers") or []):
            if not isinstance(edge,dict) or str(edge.get("action","TRANSFER"))!="TRANSFER": continue
            src=by_idx.get(int(edge.get("source",0))); target=by_idx.get(int(edge.get("target",0))); attr=str(edge.get("attribute") or "GENERAL_REFERENCE")
            if not src: continue
            rr=_audit_relation(path,src,attr,"TRANSFER",cfg); rr.update({"source":edge.get("source"),"target":edge.get("target")}); relation_reports.append(rr)
            if rr["status"]!="INCONCLUSIVE" and rr["confidence"]>=float(cfg.get("audit_min_confidence",0.55)): reliable_scores.append(rr["score"])
            for d in rr.get("failed_dimensions") or []: all_failed.append(f"{attr}.{d}")
            if rr["status"]=="FAIL":
                dims=rr.get("dimensions") or {}; details=[]
                for d in rr.get("failed_dimensions") or []:
                    item=dims.get(d) or {}; corr=str(item.get("correction") or item.get("missing") or "").strip(); details.append(f"{d}: {corr}" if corr else d)
                retry_hints.append(f"Correct ONLY {attr} from source reference {edge.get('source')} to target {edge.get('target')}. Failed dimensions: " + "; ".join(details[:7]) + ". Preserve all non-requested target attributes.")
            # Default preservation audit: when transferring a non-identity attribute, verify target identity remains stable.
            tid=int(edge.get("target",0) or 0)
            identity_key=(tid,"IDENTITY")
            if target and attr!="IDENTITY" and identity_key not in audited_preserve:
                pr=_audit_relation(path,target,"IDENTITY","PRESERVE",cfg); pr.update({"source":tid,"target":tid,"preserve_for":attr}); relation_reports.append(pr); audited_preserve.add(identity_key)
                if pr["status"]!="INCONCLUSIVE" and pr["confidence"]>=float(cfg.get("audit_min_confidence",0.55)): reliable_scores.append(pr["score"])
                for d in pr.get("failed_dimensions") or []: all_failed.append(f"PRESERVE_IDENTITY.{d}")
                if pr["status"]=="FAIL": retry_hints.append(f"Restore target reference {tid} identity. Correct only failed identity dimensions: {', '.join(pr.get('failed_dimensions') or [])}. Do not undo the requested transfer.")
        # Explicit preserve statements not already covered.
        for prq in graph.get("preserves") or []:
            tid=int(prq.get("target",0) or 0); attr=str(prq.get("attribute") or "IDENTITY")
            if not tid or tid not in by_idx: continue
            key=(tid,attr)
            if key in audited_preserve: continue
            pr=_audit_relation(path,by_idx[tid],attr,"PRESERVE",cfg); pr.update({"source":tid,"target":tid}); relation_reports.append(pr); audited_preserve.add(key)
            if pr["status"]!="INCONCLUSIVE" and pr["confidence"]>=float(cfg.get("audit_min_confidence",0.55)): reliable_scores.append(pr["score"])
            for d in pr.get("failed_dimensions") or []: all_failed.append(f"PRESERVE_{attr}.{d}")
    if reliable_scores:
        mean=sum(reliable_scores)/len(reliable_scores); worst=min(reliable_scores); overall=0.75*mean+0.25*worst
    else: overall=0.0
    failed_reports=[r for r in relation_reports if r.get("status")=="FAIL"]; inconclusive=[r for r in relation_reports if r.get("status")=="INCONCLUSIVE"]
    retry=bool(reliable_scores) and (overall<float(cfg.get("audit_threshold",0.78)) or bool(failed_reports))
    previous=_previous_audit_for_root(str(run.get("root_run_id") or run.get("run_id") or ""),str(run.get("run_id") or "")); previous_score=float(previous.get("overall_score",0.0)) if previous else None
    result={"ok":True,"engine":"V4.2_DIMENSIONAL","overall_score":round(overall,3),"threshold":float(cfg.get("audit_threshold",0.78)),"min_confidence":float(cfg.get("audit_min_confidence",0.55)),"relations":relation_reports,"failed_dimensions":list(dict.fromkeys(all_failed)),"inconclusive_count":len(inconclusive),"retry_recommended":retry,"retry_prompt_suffix":"\n".join(dict.fromkeys(retry_hints))[:3600],"generated_vision":generated,"run":run,"previous_overall_score":round(previous_score,3) if previous_score is not None else None,"score_delta":round(overall-previous_score,3) if previous_score is not None else None}
    log_record={"timestamp":time.time(),"run":run,"overall_score":result["overall_score"],"previous_overall_score":result["previous_overall_score"],"score_delta":result["score_delta"],"failed_dimensions":result["failed_dimensions"],"retry_recommended":retry,"relations":[{"attribute":r.get("attribute"),"mode":r.get("mode"),"score":r.get("score"),"confidence":r.get("confidence"),"status":r.get("status"),"failed_dimensions":r.get("failed_dimensions")} for r in relation_reports],"generated_sha256":_sha256_file(path)}
    try: _append_audit_log(log_record)
    except Exception: pass
    return result

# -----------------------------------------------------------------------------
# Optional lightweight HTTP endpoint for frontend @mention autocomplete.
# This is deliberately isolated from the node runtime. If PromptServer/aiohttp
# are unavailable in an old or unusual fork, node execution is unaffected.
# -----------------------------------------------------------------------------
try:
    from aiohttp import web as _aiohttp_web
    from server import PromptServer as _PromptServer
except Exception:
    _aiohttp_web = None
    _PromptServer = None


def _frontend_image_index() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in _build_index(True):
        rel = str(item["rel"])
        path = _safe_library_path(rel)
        try:
            stat = path.stat()
            size = int(stat.st_size)
            mtime_ns = int(stat.st_mtime_ns)
        except OSError:
            size = 0
            mtime_ns = 0
        pp = Path(rel)
        subfolder = pp.parent.as_posix()
        if subfolder == ".":
            subfolder = ""
        rows.append({
            "rel": rel,
            "name": pp.name,
            "stem": pp.stem,
            "subfolder": subfolder,
            "size": size,
            "mtime_ns": mtime_ns,
            "uim_id": str(item.get("uim_id") or ""),
            "sha256": str(item.get("sha256") or ""),
            "aliases": list(item.get("aliases") or []),
        })
    return rows


def _dedupe_upload_name(filename: str) -> Path:
    root = _library_root()
    base = Path(str(filename or "image")).name
    stem = Path(base).stem or "image"
    ext = Path(base).suffix.lower()
    if ext not in _IMAGE_EXTS:
        raise ValueError(f"Unsupported image extension: {ext or '(none)'}")
    candidate = root / f"{stem}{ext}"
    n = 2
    while candidate.exists():
        candidate = root / f"{stem}_{n}{ext}"
        n += 1
    return candidate


def _open_library_folder() -> Tuple[bool, str]:
    root = _library_root()
    try:
        import subprocess
        import sys
        if os.name == "nt":
            os.startfile(str(root))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(root)])
        else:
            subprocess.Popen(["xdg-open", str(root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, str(root)
    except Exception as exc:
        return False, f"{root} | {type(exc).__name__}: {exc}"


def _register_frontend_routes() -> None:
    if _PromptServer is None or _aiohttp_web is None:
        return
    try:
        routes = _PromptServer.instance.routes

        async def _index_response():
            rows = _frontend_image_index()
            return _aiohttp_web.json_response({
                "plugin": PLUGIN_NAME,
                "version": PLUGIN_VERSION,
                "library_path": str(_library_root()),
                "count": len(rows),
                "images": rows,
            })

        @routes.get("/uim/images")
        async def _uim_images(request):
            try:
                return await _index_response()
            except Exception as exc:
                return _aiohttp_web.json_response({
                    "plugin": PLUGIN_NAME, "version": PLUGIN_VERSION,
                    "library_path": "", "count": 0, "images": [],
                    "error": f"{type(exc).__name__}: {exc}",
                }, status=500)

        @routes.get("/uim/library/info")
        async def _uim_library_info(request):
            try:
                root = _library_root()
                return _aiohttp_web.json_response({
                    "plugin": PLUGIN_NAME, "version": PLUGIN_VERSION,
                    "library_path": str(root),
                    "count": len(_frontend_image_index()),
                })
            except Exception as exc:
                return _aiohttp_web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=500)

        @routes.get("/uim/library/thumb")
        async def _uim_library_thumb(request):
            try:
                from io import BytesIO
                from PIL import Image, ImageOps
                rel = str(request.query.get("rel", ""))
                path = _safe_library_path(rel)
                if not path.exists() or path.suffix.lower() not in _IMAGE_EXTS:
                    raise FileNotFoundError(rel)
                image = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
                image.thumbnail((256, 256))
                buf = BytesIO()
                image.save(buf, format="WEBP", quality=86, method=4)
                return _aiohttp_web.Response(body=buf.getvalue(), content_type="image/webp", headers={"Cache-Control": "no-cache"})
            except Exception as exc:
                return _aiohttp_web.Response(text=f"{type(exc).__name__}: {exc}", status=404)

        @routes.post("/uim/library/upload")
        async def _uim_library_upload(request):
            saved = []
            rejected = []
            try:
                reader = await request.multipart()
                while True:
                    field = await reader.next()
                    if field is None:
                        break
                    if not getattr(field, "filename", None):
                        continue
                    target = None
                    try:
                        target = _dedupe_upload_name(field.filename)
                        total = 0
                        with target.open("wb") as fh:
                            while True:
                                chunk = await field.read_chunk(size=1024 * 1024)
                                if not chunk:
                                    break
                                total += len(chunk)
                                if total > 250 * 1024 * 1024:
                                    raise ValueError("Image exceeds 250 MB upload limit")
                                fh.write(chunk)
                        # Validate that Pillow can actually decode it.
                        from PIL import Image
                        with Image.open(target) as check:
                            check.verify()
                        saved.append(target.name)
                    except Exception as exc:
                        try:
                            if target is not None and target.exists():
                                target.unlink()
                        except Exception:
                            pass
                        rejected.append({"name": getattr(field, "filename", ""), "error": str(exc)})
                return _aiohttp_web.json_response({
                    "ok": bool(saved),
                    "library_path": str(_library_root()),
                    "saved": saved, "rejected": rejected,
                    "images": _frontend_image_index(),
                })
            except Exception as exc:
                return _aiohttp_web.json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

        @routes.post("/uim/library/open")
        async def _uim_library_open(request):
            ok, detail = _open_library_folder()
            return _aiohttp_web.json_response({"ok": ok, "library_path": str(_library_root()), "detail": detail}, status=200 if ok else 409)

        @routes.get("/uim/last-bind")
        async def _uim_last_bind(request):
            try:
                return _aiohttp_web.json_response(dict(_LAST_BIND_REPORT))
            except Exception as exc:
                return _aiohttp_web.json_response({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, status=500)

        @routes.get("/uim/run/{run_id}")
        async def _uim_run_report(request):
            rid=str(request.match_info.get("run_id", ""))
            rep=_run_report(rid)
            if rep is None:
                return _aiohttp_web.json_response({"ok":False,"error":"run not found"},status=404)
            return _aiohttp_web.json_response({"ok":True,"report":rep})

        @routes.get("/uim/vision/status")
        async def _uim_vision_status(request):
            try:
                cache = _read_vision_cache()
                cfg = _read_vision_config()
                return _aiohttp_web.json_response({
                    "plugin": PLUGIN_NAME, "version": PLUGIN_VERSION,
                    "mode": _vision_mode(),
                    "endpoint_configured": bool(_vision_endpoint()),
                    "model": _vision_model(),
                    "rich_semantic_ready": bool(_vision_endpoint() and _vision_model()),
                    "required": _vision_required(),
                    "cache_entries": len(cache),
                    "cache_path": str(_vision_cache_path()),
                    "config_path": str(_vision_config_path()),
                    "config": {k:v for k,v in cfg.items() if k != "api_key"},
                })
            except Exception as exc:
                return _aiohttp_web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=500)

        @routes.get("/uim/vision/config")
        async def _uim_vision_config_get(request):
            try:
                cfg = _read_vision_config()
                return _aiohttp_web.json_response({
                    "ok": True, "config_path": str(_vision_config_path()),
                    "config": cfg,
                    "effective": {
                        "mode": _vision_mode(), "url": _vision_endpoint(), "model": _vision_model(),
                        "required": _vision_required(), "timeout": _vision_timeout(),
                    },
                })
            except Exception as exc:
                return _aiohttp_web.json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)

        @routes.post("/uim/vision/config")
        async def _uim_vision_config_post(request):
            try:
                data = await request.json()
                _write_vision_config(data or {})
                return _aiohttp_web.json_response({
                    "ok": True, "config_path": str(_vision_config_path()),
                    "config": _read_vision_config(),
                })
            except Exception as exc:
                return _aiohttp_web.json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

        @routes.post("/uim/vision/analyze")
        async def _uim_vision_analyze(request):
            try:
                data = await request.json()
                rel = str((data or {}).get("rel") or "")
                context = str((data or {}).get("context") or "")
                force = bool((data or {}).get("force", False))
                path = _safe_library_path(rel)
                if not path.exists() or not path.is_file():
                    raise FileNotFoundError(rel)
                result = _analyze_image_semantics(path, context, force=force)
                return _aiohttp_web.json_response({"ok": True, "rel": rel, "vision": result})
            except Exception as exc:
                return _aiohttp_web.json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)


        @routes.get("/uim/v4/config")
        async def _uim_v4_config_get(request):
            try:
                return _aiohttp_web.json_response({"ok": True, "config": _read_v4_config()})
            except Exception as exc:
                return _aiohttp_web.json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)

        @routes.post("/uim/v4/config")
        async def _uim_v4_config_post(request):
            try:
                data = await request.json()
                return _aiohttp_web.json_response({"ok": True, "config": _write_v4_config(data or {})})
            except Exception as exc:
                return _aiohttp_web.json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

        @routes.get("/uim/mask/file")
        async def _uim_mask_file(request):
            try:
                rel = str(request.query.get("rel", ""))
                if not rel.replace("\\", "/").startswith(".uim/masks/"):
                    raise ValueError("invalid mask path")
                path = _safe_library_path(rel)
                if not path.exists() or path.suffix.lower() != ".png":
                    raise FileNotFoundError(rel)
                return _aiohttp_web.FileResponse(path, headers={"Cache-Control": "no-cache"})
            except Exception as exc:
                return _aiohttp_web.Response(text=f"{type(exc).__name__}: {exc}", status=404)

        @routes.post("/uim/mask/upload")
        async def _uim_mask_upload(request):
            try:
                data = await request.json()
                rel = _write_mask_data_url(str((data or {}).get("data_url") or ""))
                return _aiohttp_web.json_response({"ok": True, "mask_rel": rel, "thumb": f"/uim/library/thumb?rel={rel}"})
            except Exception as exc:
                return _aiohttp_web.json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

        @routes.get("/uim/adapters")
        async def _uim_adapters_get(request):
            return _aiohttp_web.json_response({"ok": True, "path": str(_adapter_manifest_path()), "adapters": _read_adapter_manifests()})

        @routes.post("/uim/adapters")
        async def _uim_adapters_post(request):
            try:
                data = await request.json()
                adapters = (data or {}).get("adapters") if isinstance(data, dict) else None
                if not isinstance(adapters, dict):
                    raise ValueError("adapters must be an object keyed by class_type")
                merged = _read_adapter_manifests()
                merged.update(adapters)
                _write_adapter_manifests(merged)
                return _aiohttp_web.json_response({"ok": True, "adapters": _read_adapter_manifests()})
            except Exception as exc:
                return _aiohttp_web.json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

        @routes.get("/uim/audit/log")
        async def _uim_audit_log(request):
            try:
                limit=max(1,min(500,int(request.query.get("limit","50"))))
                return _aiohttp_web.json_response({"ok":True,"path":str(_audit_log_path()),"items":_recent_audit_log(limit)})
            except Exception as exc:
                return _aiohttp_web.json_response({"ok":False,"error":f"{type(exc).__name__}: {exc}"},status=400)

        @routes.post("/uim/audit/analyze")
        async def _uim_audit_analyze(request):
            target = None
            try:
                reader = await request.multipart()
                blob = None
                filename = "generated.png"
                fields: Dict[str, str] = {}
                while True:
                    field = await reader.next()
                    if field is None:
                        break
                    if getattr(field, "filename", None):
                        filename = str(field.filename or filename)
                        chunks=[]; total=0
                        while True:
                            chunk=await field.read_chunk(size=1024*1024)
                            if not chunk: break
                            total += len(chunk)
                            if total > 80*1024*1024: raise ValueError("Audit image exceeds 80 MB")
                            chunks.append(chunk)
                        blob=b"".join(chunks)
                    else:
                        try:
                            fields[str(field.name or "")] = (await field.text()).strip()
                        except Exception:
                            pass
                if not blob:
                    raise ValueError("No generated image uploaded")
                from PIL import Image, ImageOps
                audit_dir=_vision_cache_dir()/"audit"
                audit_dir.mkdir(parents=True,exist_ok=True)
                digest=hashlib.sha256(blob).hexdigest()[:24]
                target=audit_dir/f"{digest}.png"
                with Image.open(BytesIO(blob)) as im:
                    im=ImageOps.exif_transpose(im).convert("RGB")
                    im.save(target,format="PNG",optimize=True)
                bind = _run_report(fields.get("run_id", "")) if fields.get("run_id") else None
                result=_audit_generated_image(target, bind_report=bind)
                result["prompt_id"] = fields.get("prompt_id", "")
                return _aiohttp_web.json_response(result, status=200 if result.get("ok") else 409)
            except Exception as exc:
                return _aiohttp_web.json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)
    except Exception:
        # Duplicate route during hot reload or an incompatible server implementation.
        # Autocomplete will gracefully disable itself; backend parsing remains intact.
        return


_register_frontend_routes()


class UniversalAtImageRouter16:
    """Parse @filename mentions and expose up to 16 IMAGE outputs.

    The node is deliberately model-agnostic. It does not know or care whether the
    downstream workflow is H3, LTX, KREA, Flux, SDXL, WAN, or a future model.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": (
                    "STRING",
                    {
                        "default": "@图1，把他的衣服变成黑色，其他保持不变。",
                        "multiline": True,
                    },
                ),
                "replacement_mode": (
                    ["KEEP_AT", "GENERIC_REF", "INDEXED_TEXT", "REMOVE_AT", "H3_PICTURE", "CUSTOM"],
                    {"default": "KEEP_AT"},
                ),
                "custom_template": (
                    "STRING",
                    {
                        "default": "<Reference {i}>",
                        "multiline": False,
                    },
                ),
                "strict_missing": ("BOOLEAN", {"default": True}),
                "search_subfolders": ("BOOLEAN", {"default": True}),
                "max_refs": ("INT", {"default": 16, "min": 1, "max": MAX_OUTPUT_REFS, "step": 1}),
            }
        }

    RETURN_TYPES = (
        "STRING",
        "STRING",
        "UIM_IMAGE_BANK",
        *("IMAGE",) * MAX_OUTPUT_REFS,
        "INT",
        "STRING",
    )
    RETURN_NAMES = (
        "resolved_prompt",
        "clean_prompt",
        "image_bank",
        *(f"image_{i}" for i in range(1, MAX_OUTPUT_REFS + 1)),
        "ref_count",
        "manifest_json",
    )
    FUNCTION = "route"
    CATEGORY = CATEGORY

    def route(self, prompt, replacement_mode, custom_template, strict_missing, search_subfolders, max_refs):
        resolved, clean, refs, images = _parse(
            str(prompt or ""),
            str(replacement_mode),
            str(custom_template or ""),
            bool(strict_missing),
            bool(search_subfolders),
            int(max_refs),
        )
        real_refs = [r for r in refs if "index" in r]
        manifest = _manifest(str(prompt or ""), resolved, clean, refs, str(replacement_mode))
        bank = _bank_from(refs, images)
        return (
            resolved,
            clean,
            bank,
            *images,
            len(real_refs),
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )

    @classmethod
    def IS_CHANGED(cls, prompt, replacement_mode, custom_template, strict_missing, search_subfolders, max_refs):
        """Invalidate ComfyUI cache when any referenced input image is overwritten."""
        try:
            text = str(prompt or "")
            matches = list(_MENTION_RE.finditer(text))
            index = _build_index(bool(search_subfolders))
            rows = []
            seen = set()
            for match in matches:
                token = _mention_token(match)
                try:
                    found = _resolve_token(token, index)
                except Exception:
                    if strict_missing:
                        rows.append(("missing", token))
                    continue
                rel = found["rel"]
                if rel in seen:
                    continue
                seen.add(rel)
                path = _safe_library_path(rel)
                try:
                    stat = path.stat()
                    rows.append((rel, stat.st_mtime_ns, stat.st_size))
                except OSError:
                    rows.append((rel, "unreadable"))
            payload = repr((text, replacement_mode, custom_template, bool(strict_missing), bool(search_subfolders), int(max_refs), rows))
            return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()
        except Exception as exc:
            return f"error:{type(exc).__name__}:{exc}"


class UniversalMentionImageByIndex:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image_bank": ("UIM_IMAGE_BANK",),
                "index": ("INT", {"default": 1, "min": 1, "max": MAX_OUTPUT_REFS, "step": 1}),
            }
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "reference_json")
    FUNCTION = "pick"
    CATEGORY = CATEGORY

    def pick(self, image_bank, index):
        items = list((image_bank or {}).get("items", []))
        wanted = int(index)
        for item in items:
            meta = item.get("meta", {})
            if int(meta.get("index", -1)) == wanted:
                return item.get("image"), json.dumps(meta, ensure_ascii=False, indent=2)
        raise ValueError(f"No @image with index {wanted} exists in this image_bank")


class UniversalMentionImageByAlias:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image_bank": ("UIM_IMAGE_BANK",),
                "alias": ("STRING", {"default": "图1", "multiline": False}),
            }
        }

    RETURN_TYPES = ("IMAGE", "INT", "STRING")
    RETURN_NAMES = ("image", "index", "reference_json")
    FUNCTION = "pick"
    CATEGORY = CATEGORY

    def pick(self, image_bank, alias):
        wanted = _norm(str(alias or "").lstrip("@"))
        items = list((image_bank or {}).get("items", []))
        exact = []
        for item in items:
            meta = item.get("meta", {})
            names = {
                _norm(meta.get("alias", "")),
                _norm(meta.get("file", "")),
                _norm(Path(str(meta.get("file", ""))).stem),
            }
            if wanted in names:
                exact.append(item)
        if len(exact) == 1:
            item = exact[0]
            meta = item.get("meta", {})
            return item.get("image"), int(meta.get("index", 0)), json.dumps(meta, ensure_ascii=False, indent=2)
        if len(exact) > 1:
            raise ValueError(f"Alias '{alias}' is ambiguous inside this image_bank")
        raise ValueError(f"Alias '{alias}' is not present in this image_bank")


class UniversalMentionPromptAdapter:
    """Re-render an already parsed manifest into a different text tag syntax.

    Useful when the same router output is reused for different model families.
    It changes text only; it never changes images or model state.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "original_prompt": ("STRING", {"default": "", "multiline": True}),
                "replacement_mode": (["KEEP_AT", "GENERIC_REF", "INDEXED_TEXT", "REMOVE_AT", "H3_PICTURE", "CUSTOM"], {"default": "GENERIC_REF"}),
                "custom_template": ("STRING", {"default": "<Reference {i}>", "multiline": False}),
                "strict_missing": ("BOOLEAN", {"default": True}),
                "search_subfolders": ("BOOLEAN", {"default": True}),
                "max_refs": ("INT", {"default": 16, "min": 1, "max": MAX_OUTPUT_REFS, "step": 1}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("resolved_prompt", "manifest_json")
    FUNCTION = "adapt"
    CATEGORY = CATEGORY

    def adapt(self, original_prompt, replacement_mode, custom_template, strict_missing, search_subfolders, max_refs):
        resolved, clean, refs, _images = _parse(
            str(original_prompt or ""), str(replacement_mode), str(custom_template or ""),
            bool(strict_missing), bool(search_subfolders), int(max_refs)
        )
        manifest = _manifest(str(original_prompt or ""), resolved, clean, refs, str(replacement_mode))
        return resolved, json.dumps(manifest, ensure_ascii=False, indent=2)


class UniversalMentionLibraryInfo:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    RETURN_TYPES = ("STRING", "INT", "STRING")
    RETURN_NAMES = ("library_path", "image_count", "library_json")
    FUNCTION = "info"
    CATEGORY = CATEGORY

    def info(self):
        root = _library_root()
        rows = _frontend_image_index()
        payload = {
            "plugin": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
            "library_path": str(root),
            "image_count": len(rows),
            "images": [r["rel"] for r in rows],
        }
        return str(root), len(rows), json.dumps(payload, ensure_ascii=False, indent=2)



# =============================================================================
# v2.0 Global Mention Auto-Bind Engine
# =============================================================================
# This layer runs immediately before ComfyUI validates/queues the API prompt.
# It is intentionally hardware/model-weight agnostic: it only rewrites the graph
# and injects tiny loader/text nodes. Vision understanding is delegated to the
# target multimodal model/encoder (H3, Krea2 ref encoder, LTX Prompt Enhancer, ...).

_PROMPTISH_NAMES = {"prompt", "text", "positive", "positive_prompt", "text_prompt", "description"}
_IMAGEISH_WORDS = ("image", "img", "picture", "ref", "reference", "guide", "character", "style", "subject")
_SKIP_IMAGE_WORDS = ("mask", "control", "depth", "canny", "normal", "seg", "preview")


def _resolve_refs_metadata(prompt: str, max_refs: int = MAX_OUTPUT_REFS, strict_missing: bool = True) -> List[Dict[str, Any]]:
    """Resolve mentions without decoding images; safe to call from on_prompt handler."""
    text = str(prompt or "")
    matches = list(_MENTION_RE.finditer(text))
    if not matches:
        return []
    index = _build_index(True)
    refs: List[Dict[str, Any]] = []
    by_file: Dict[str, Dict[str, Any]] = {}
    for mi, match in enumerate(matches):
        token = _mention_token(match)
        try:
            found = _resolve_token(token, index)
        except Exception:
            if strict_missing:
                raise
            continue
        key = found["rel"]
        if key not in by_file:
            if len(refs) >= max_refs:
                raise ValueError(f"Too many unique @images: {len(refs)+1}; max={max_refs}")
            rec = {
                "index": len(refs) + 1,
                "alias": token,
                "file": found["rel"],
                "roles": [],
                "instructions": [],
                "contexts": [],
                "occurrences": [],
            }
            refs.append(rec)
            by_file[key] = rec
        rec = by_file[key]
        context = _context_window(text, match.start(), match.end(), radius=180)
        rec["contexts"].append(context)
        for role in _roles_for_context(context):
            if role not in rec["roles"]:
                rec["roles"].append(role)

        # Bind the text that follows this mention to this image until the next
        # @mention or a hard sentence boundary. This is the important semantic
        # link for prompts such as: @图1 把衣服换成黑色。
        next_start = matches[mi + 1].start() if mi + 1 < len(matches) else len(text)
        tail = text[match.end():next_start]
        hard_positions = [tail.find(c) for c in "。；;!?！？\n" if tail.find(c) >= 0]
        if hard_positions:
            tail = tail[: min(hard_positions)]
        instruction = tail.strip(" \t\r\n,，:：-—")[:360]
        if instruction and instruction not in rec["instructions"]:
            rec["instructions"].append(instruction)
        rec["occurrences"].append((match.start(), match.end(), match.group(0)))
    return refs


def _render_semantic_prompt(prompt: str, refs: List[Dict[str, Any]], adapter: Dict[str, Any]) -> str:
    """Replace @tokens with a target-native reference label and append concise bindings."""
    text = str(prompt or "")
    tag_template = str(adapter.get("tag_template", "Reference Image {i}"))
    by_file = {str(r["file"]): r for r in refs}
    index = _build_index(True)
    replacements: List[Tuple[int, int, str]] = []
    for match in _MENTION_RE.finditer(text):
        token = _mention_token(match)
        try:
            found = _resolve_token(token, index)
            rec = by_file.get(str(found["rel"]))
            if rec is None:
                continue
            i = int(rec["index"])
            tag = tag_template.format(i=i, index=i, alias=rec.get("alias", token), file=rec.get("file", ""))
            replacements.append((match.start(), match.end(), tag))
        except Exception:
            continue
    for a, b, repl in reversed(replacements):
        text = text[:a] + repl + text[b:]

    if not adapter.get("append_bindings", True) or not refs:
        return text

    lines = []
    for rec in refs:
        i = int(rec["index"])
        tag = tag_template.format(i=i, index=i, alias=rec.get("alias", ""), file=rec.get("file", ""))
        roles = ", ".join(rec.get("roles") or ["GENERAL_REFERENCE"])
        instructions = " / ".join(rec.get("instructions") or [])
        if instructions:
            lines.append(f"{tag}: apply this image specifically to the linked instruction: {instructions}. Role: {roles}.")
        else:
            lines.append(f"{tag}: use the connected reference image as a visual reference. Role: {roles}.")
    return text.rstrip() + "\n\n[Reference bindings]\n" + "\n".join(lines)


def _workflow_payload(json_data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json_data.get("extra_data", {}).get("extra_pnginfo", {}).get("workflow", {}) or {}
    except Exception:
        return {}


def _ui_nodes_by_id(workflow: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out = {}
    for n in workflow.get("nodes", []) or []:
        if isinstance(n, dict) and "id" in n:
            out[str(n["id"])] = n
    return out


def _workflow_links(workflow: Dict[str, Any]) -> List[List[Any]]:
    rows = []
    for link in workflow.get("links", []) or []:
        if isinstance(link, (list, tuple)) and len(link) >= 6:
            rows.append(list(link))
    return rows


def _node_class(prompt_graph: Dict[str, Any], node_id: str, ui_node: Optional[Dict[str, Any]]) -> str:
    p = prompt_graph.get(str(node_id), {}) if isinstance(prompt_graph, dict) else {}
    return str(p.get("class_type") or (ui_node or {}).get("type") or "")


def _ui_image_slots(ui_node: Optional[Dict[str, Any]]) -> List[str]:
    if not ui_node:
        return []
    slots = []
    for inp in ui_node.get("inputs", []) or []:
        if not isinstance(inp, dict) or str(inp.get("type", "")).upper() != "IMAGE":
            continue
        name = str(inp.get("name", ""))
        low = name.casefold()
        if any(w in low for w in _SKIP_IMAGE_WORDS):
            continue
        if any(w in low for w in _IMAGEISH_WORDS) or low in {"image", "first_frame", "last_frame"}:
            slots.append(name)
    return slots



def _ui_strength_map(ui_node: Optional[Dict[str, Any]], image_slots: List[str]) -> Dict[str, str]:
    if not isinstance(ui_node, dict) or not image_slots:
        return {}
    candidates=[]
    for inp in ui_node.get("inputs", []) or []:
        if not isinstance(inp, dict):
            continue
        typ=str(inp.get("type", "")).upper()
        name=str(inp.get("name", ""))
        if typ not in {"FLOAT", "INT", "NUMBER"}:
            continue
        if any(w in name.casefold() for w in ("strength","weight","influence","fidelity","reference_scale","ref_scale","image_scale")):
            candidates.append(name)
    if len(image_slots)==1 and len(candidates)==1:
        return {image_slots[0]:candidates[0]}
    out={}
    for i,slot in enumerate(image_slots, start=1):
        sm=re.findall(r"\d+",str(slot))
        for cand in candidates:
            cm=re.findall(r"\d+",str(cand))
            if sm and cm and sm[-1]==cm[-1]:
                out[slot]=cand; break
            if re.search(rf"(?:^|[_\.]){i}(?:$|[_\.])",cand):
                out[slot]=cand; break
    return out


def _ui_mask_map(ui_node: Optional[Dict[str, Any]], image_slots: List[str]) -> Dict[str, str]:
    if not isinstance(ui_node, dict) or not image_slots:
        return {}
    masks = [str(inp.get("name", "")) for inp in (ui_node.get("inputs", []) or [])
             if isinstance(inp, dict) and str(inp.get("type", "")).upper() == "MASK"]
    if len(image_slots) == 1 and len(masks) == 1:
        return {image_slots[0]: masks[0]}
    out: Dict[str, str] = {}
    for i, slot in enumerate(image_slots, start=1):
        nums = re.findall(r"\d+", slot)
        for mask in masks:
            mnums = re.findall(r"\d+", mask)
            if nums and mnums and nums[-1] == mnums[-1]:
                out[slot] = mask; break
            if re.search(rf"(?:^|[_\.]){i}(?:$|[_\.])", mask):
                out[slot] = mask; break
    return out


def _adapter_capabilities(adapter: Dict[str, Any]) -> Dict[str, Any]:
    caps = dict(adapter.get("capabilities") or {}) if isinstance(adapter.get("capabilities"), dict) else {}
    slots = list(adapter.get("slots") or [])
    caps.setdefault("supports_reference_image", bool(slots))
    caps.setdefault("supports_multi_reference", len(slots) > 1)
    caps.setdefault("supports_native_strength", bool(adapter.get("strength_map")))
    caps.setdefault("supports_native_mask", bool(adapter.get("mask_map")))
    caps.setdefault("confidence", "BUILTIN" if not adapter.get("user_manifest") else "USER_VERIFIED")
    caps.setdefault("reference_semantics", [])
    return caps


def _validate_adapter_for_ui(adapter: Dict[str, Any], ui_node: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(adapter)
    out["capabilities"] = _adapter_capabilities(out)
    errors: List[str] = []
    slots = [str(x) for x in (out.get("slots") or [])]
    if not slots or len(slots) != len(set(slots)):
        errors.append("Adapter reference slots are empty or duplicated.")
    if not out["capabilities"].get("supports_reference_image"):
        errors.append("Adapter capability explicitly says reference images are unsupported.")
    if isinstance(ui_node, dict):
        types = {str(i.get("name")): str(i.get("type", "")).upper() for i in (ui_node.get("inputs", []) or []) if isinstance(i, dict)}
        # Subgraph/autogrow sockets are sometimes absent from serialized UI metadata;
        # built-in adapters remain authoritative. User/generic adapters must verify.
        if out.get("user_manifest") or str(out.get("name", "")).startswith("GENERIC"):
            for slot in slots:
                if slot not in types or types.get(slot) != "IMAGE":
                    errors.append(f"Mapped reference slot '{slot}' is not a declared IMAGE input on this node.")
        for slot, name in (out.get("strength_map") or {}).items():
            if str(name) not in types or types.get(str(name)) not in {"FLOAT", "INT", "NUMBER"}:
                errors.append(f"Native strength mapping {slot}->{name} is not a numeric input.")
        for slot, name in (out.get("mask_map") or {}).items():
            if str(name) not in types or types.get(str(name)) != "MASK":
                errors.append(f"Native mask mapping {slot}->{name} is not a MASK input.")
    out["validation_errors"] = list(dict.fromkeys(errors))
    return out


def _prepare_control_modes(refs: List[Dict[str, Any]], adapter: Dict[str, Any]) -> None:
    sm = adapter.get("strength_map") if isinstance(adapter.get("strength_map"), dict) else {}
    mm = adapter.get("mask_map") if isinstance(adapter.get("mask_map"), dict) else {}
    for rec in refs:
        ctl = rec.get("v4") if isinstance(rec.get("v4"), dict) else {}
        if not ctl:
            continue
        slot = str(rec.get("slot") or "")
        strength = float(ctl.get("strength", 1.0) or 1.0)
        ctl["strength_effective"] = "NATIVE" if slot in sm else ("SEMANTIC" if abs(strength - 1.0) > 0.001 else "DEFAULT")
        ctl["mask_effective"] = "NATIVE" if ctl.get("mask_rel") and slot in mm else ("PREPROCESS" if ctl.get("mask_rel") else "NONE")


def _apply_native_strengths(prompt_graph: Dict[str, Any], target_id: str, refs: List[Dict[str, Any]], adapter: Dict[str, Any]) -> List[Dict[str, Any]]:
    target=prompt_graph.get(str(target_id))
    if not isinstance(target,dict): return []
    inputs=target.get("inputs") if isinstance(target.get("inputs"),dict) else {}
    mapping=adapter.get("strength_map") if isinstance(adapter.get("strength_map"),dict) else {}
    applied=[]
    for rec in refs:
        ctl=rec.get("v4") if isinstance(rec.get("v4"),dict) else {}
        strength=float(ctl.get("strength",1.0) or 1.0)
        name=mapping.get(str(rec.get("slot") or ""))
        if name:
            ctl["strength_effective"] = "NATIVE"
        elif abs(strength-1.0)>0.001:
            ctl["strength_effective"] = "SEMANTIC"
        if not name or abs(strength-1.0)<0.001: continue
        if _conn(inputs.get(name)) is not None:
            raise ValueError(f"Native strength input '{name}' is already wired; refusing to overwrite it for @{rec.get('alias')}.")
        inputs[str(name)]=strength
        applied.append({"index":rec.get("index"),"input":name,"value":strength,"mode":"NATIVE"})
    return applied


def _apply_native_masks(prompt_graph: Dict[str, Any], target_id: str, refs: List[Dict[str, Any]], adapter: Dict[str, Any], counter: List[int]) -> List[Dict[str, Any]]:
    target=prompt_graph.get(str(target_id))
    if not isinstance(target,dict): return []
    inputs=target.get("inputs") if isinstance(target.get("inputs"),dict) else {}
    mapping=adapter.get("mask_map") if isinstance(adapter.get("mask_map"),dict) else {}
    applied=[]
    for rec in refs:
        ctl=rec.get("v4") if isinstance(rec.get("v4"),dict) else {}
        if not ctl.get("mask_rel") or ctl.get("mask_effective") != "NATIVE":
            continue
        slot=str(rec.get("slot") or "")
        mask_input=str(mapping.get(slot) or "")
        image_conn=inputs.get(slot)
        if not mask_input or _conn(image_conn) is None:
            raise ValueError(f"Native mask mapping for @{rec.get('alias')} has no usable image/mask socket.")
        if _nonempty_input(inputs.get(mask_input)):
            raise ValueError(f"Native mask input '{mask_input}' is already occupied; refusing to overwrite it.")
        node_id=_alloc_prompt_node_id(prompt_graph,counter)
        prompt_graph[node_id]={"class_type":"UniversalMentionApplyMask","inputs":{"image":list(_conn(image_conn)),"mask_rel":str(ctl.get("mask_rel")),"mode":str(ctl.get("mask_mode","FOCUS")),"outside_level":0.5}}
        inputs[mask_input]=[node_id,1]
        applied.append({"index":rec.get("index"),"input":mask_input,"mask_rel":ctl.get("mask_rel"),"mode":"NATIVE"})
    return applied



def _adapter_for_target(class_type: str, ui_node: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    c = str(class_type or "")
    low = c.casefold()
    adapter: Optional[Dict[str, Any]] = None
    if c == "MiniMaxH3ReferenceToVideo":
        adapter = {
            "name": "MINIMAX_H3_NATIVE", "max_refs": 9,
            "slots": [f"ref_images.ref_image_{i}" for i in range(9)],
            "tag_template": "<Picture {i}>", "append_bindings": True,
            "replace_existing": True, "native_vision": True,
            "capabilities": {"supports_reference_image":True,"supports_multi_reference":True,"supports_native_strength":False,"supports_native_mask":False,"confidence":"BUILTIN_VERIFIED","reference_semantics":["IDENTITY","CLOTHING","POSE_MOTION","STYLE","SCENE"]},
        }
    elif c == "LTXVPromptEnhancer":
        adapter = {
            "name": "LTX_PROMPT_ENHANCER", "max_refs": 1, "slots": ["image_prompt"],
            "tag_template": "the connected reference image", "append_bindings": True,
            "replace_existing": True, "native_vision": True,
            "capabilities": {"supports_reference_image":True,"supports_multi_reference":False,"supports_native_strength":False,"supports_native_mask":False,"confidence":"BUILTIN_VERIFIED","reference_semantics":["GENERAL_REFERENCE"]},
        }
    else:
        user_adapter = _user_adapter_for_class(c)
        if user_adapter is not None:
            adapter = user_adapter
        else:
            slots = _ui_image_slots(ui_node)
            if "krea" in low and slots:
                adapter = {
                    "name":"KREA2_DIRECT_REFERENCE","max_refs":min(12,len(slots)),"slots":slots[:12],
                    "tag_template":"Picture {i}","append_bindings":True,"replace_existing":True,"native_vision":True,
                    "strength_map":_ui_strength_map(ui_node,slots[:12]),"mask_map":_ui_mask_map(ui_node,slots[:12]),
                    "capabilities":{"supports_reference_image":True,"supports_multi_reference":len(slots)>1,"confidence":"STRUCTURAL_HIGH","reference_semantics":["IDENTITY","STYLE","CLOTHING","GENERAL_REFERENCE"]},
                }
            elif slots:
                adapter = {
                    "name":"GENERIC_DIRECT_IMAGE","max_refs":len(slots),"slots":slots,
                    "tag_template":"Reference Image {i}","append_bindings":True,"replace_existing":False,
                    "strength_map":_ui_strength_map(ui_node,slots),"mask_map":_ui_mask_map(ui_node,slots),
                    "capabilities":{"supports_reference_image":True,"supports_multi_reference":len(slots)>1,"confidence":"STRUCTURAL_ONLY","reference_semantics":["GENERAL_REFERENCE"]},
                }
    return _validate_adapter_for_ui(adapter, ui_node) if adapter is not None else None

def _find_prompt_input_on_target(ui_target: Optional[Dict[str, Any]], source_id: str, workflow: Dict[str, Any]) -> Optional[str]:
    if not ui_target:
        return None
    target_id = str(ui_target.get("id"))
    links_by_id = {str(row[0]): row for row in _workflow_links(workflow)}
    for inp in ui_target.get("inputs", []) or []:
        if not isinstance(inp, dict):
            continue
        name = str(inp.get("name", ""))
        if str(inp.get("type", "")).upper() != "STRING" and name.casefold() not in _PROMPTISH_NAMES:
            continue
        link_id = inp.get("link")
        if link_id is None:
            continue
        row = links_by_id.get(str(link_id))
        if row and str(row[1]) == str(source_id) and str(row[3]) == target_id:
            return name
    return None


def _direct_target_ids(source_id: str, workflow: Dict[str, Any]) -> List[str]:
    out = []
    for row in _workflow_links(workflow):
        if str(row[1]) == str(source_id):
            tid = str(row[3])
            if tid not in out:
                out.append(tid)
    return out


def _alloc_prompt_node_id(prompt_graph: Dict[str, Any], counter: List[int]) -> str:
    while True:
        counter[0] += 1
        nid = str(counter[0])
        if nid not in prompt_graph:
            return nid


def _initial_alloc_counter(prompt_graph: Dict[str, Any]) -> List[int]:
    vals = []
    for k in prompt_graph.keys():
        try:
            vals.append(int(str(k)))
        except Exception:
            pass
    return [max(vals or [1000]) + 100000]


def _inject_reference_nodes(
    prompt_graph: Dict[str, Any], target_id: str, refs: List[Dict[str, Any]], adapter: Dict[str, Any], counter: List[int]
) -> List[str]:
    target = prompt_graph.get(str(target_id))
    if not isinstance(target, dict):
        return []
    inputs = target.setdefault("inputs", {})
    slots = list(adapter.get("slots") or [])
    used = []
    for rec, slot in zip(refs[: int(adapter.get("max_refs", len(slots)))], slots):
        if not adapter.get("replace_existing", False) and slot in inputs and inputs.get(slot) not in (None, ""):
            # Generic mode avoids silently destroying a user's intentional wire.
            continue
        loader_id = _alloc_prompt_node_id(prompt_graph, counter)
        prompt_graph[loader_id] = {
            "class_type": "UniversalMentionLoadOne",
            "inputs": {"library_rel": str(rec["file"])},
        }
        inputs[str(slot)] = [loader_id, 0]
        used.append(str(slot))
    return used


def _inject_text_for_target(
    prompt_graph: Dict[str, Any], target_id: str, target_prompt_input: str, text: str, counter: List[int]
) -> str:
    literal_id = _alloc_prompt_node_id(prompt_graph, counter)
    prompt_graph[literal_id] = {
        "class_type": "UniversalMentionTextLiteral",
        "inputs": {"text": str(text)},
    }
    target = prompt_graph.get(str(target_id), {})
    target.setdefault("inputs", {})[str(target_prompt_input)] = [literal_id, 0]
    return literal_id



_SLOT_ALIAS_RE = re.compile(r"^(?:#|槽|slot)?(?P<n>\d{1,2})$", re.IGNORECASE)


def _slot_alias_number(token: str) -> Optional[int]:
    """Return 1-based connected reference slot for @1/@2/@槽3/@slot4.

    Pure numeric mentions are intentionally reserved for *already connected*
    reference slots in Global Auto-Bind mode. Library images should use a
    filename alias such as @人物正面 or @{服装/黑西装}.
    """
    m = _SLOT_ALIAS_RE.fullmatch(str(token or "").strip())
    if not m:
        return None
    try:
        n = int(m.group("n"))
    except Exception:
        return None
    return n if n >= 1 else None


def _nonempty_input(value: Any) -> bool:
    return value not in (None, "", [], {})


def _conn(value: Any) -> Optional[Tuple[str, int]]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (str(value[0]), int(value[1]))
        except Exception:
            return None
    return None


def _prompt_consumers(prompt_graph: Dict[str, Any], source_id: str, output_slot: Optional[int] = None) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    sid = str(source_id)
    for nid, node in prompt_graph.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs", {})
        if not isinstance(inputs, dict):
            continue
        for name, value in inputs.items():
            c = _conn(value)
            if c is None or c[0] != sid:
                continue
            if output_slot is not None and c[1] != int(output_slot):
                continue
            out.append((str(nid), str(name)))
    return out


def _prompt_downstream(prompt_graph: Dict[str, Any], source_id: str, max_depth: int = 32) -> Dict[str, int]:
    depths: Dict[str, int] = {}
    queue: List[Tuple[str, int]] = [(str(source_id), 0)]
    seen = {str(source_id)}
    while queue:
        current, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        for nid, _ in _prompt_consumers(prompt_graph, current):
            if nid in seen:
                continue
            seen.add(nid)
            depths[nid] = depth + 1
            queue.append((nid, depth + 1))
    return depths


def _trace_upstream_load_image(prompt_graph: Dict[str, Any], value: Any, max_depth: int = 32) -> Optional[Dict[str, Any]]:
    c = _conn(value)
    if c is None:
        return None
    queue: List[Tuple[str, int]] = [(c[0], 0)]
    seen = set()
    while queue:
        nid, depth = queue.pop(0)
        if nid in seen or depth > max_depth:
            continue
        seen.add(nid)
        node = prompt_graph.get(str(nid))
        if not isinstance(node, dict):
            continue
        klass = str(node.get("class_type") or "")
        inputs = node.get("inputs", {}) if isinstance(node.get("inputs"), dict) else {}
        if klass == "LoadImage":
            raw = inputs.get("image")
            if isinstance(raw, str) and raw.strip():
                return {"node_id": str(nid), "file": raw.strip(), "class_type": klass}
        # Prefer the visual-data path first, then inspect any other connection.
        preferred = []
        rest = []
        for name, iv in inputs.items():
            ic = _conn(iv)
            if ic is None:
                continue
            if str(name).casefold() in {"image", "pixels", "latent", "samples"}:
                preferred.append(ic[0])
            else:
                rest.append(ic[0])
        for up in preferred + rest:
            queue.append((str(up), depth + 1))
    return None


def _aliases_from_file(name: Optional[str]) -> List[str]:
    if not name:
        return []
    raw = str(name).replace("\\", "/").strip()
    p = Path(raw)
    vals = [raw, p.name, p.stem]
    out: List[str] = []
    for v in vals:
        n = _norm(v)
        if n and n not in out:
            out.append(n)
    return out


def _connected_alias_map_for_slots(prompt_graph: Dict[str, Any], inputs: Dict[str, Any], slots: List[str]) -> Dict[int, List[str]]:
    out: Dict[int, List[str]] = {}
    for i, slot in enumerate(slots, start=1):
        value = inputs.get(str(slot))
        if not _nonempty_input(value):
            continue
        info = _trace_upstream_load_image(prompt_graph, value)
        aliases = _aliases_from_file((info or {}).get("file"))
        if aliases:
            out[i] = aliases
    return out


def _connected_file_map_for_slots(prompt_graph: Dict[str, Any], inputs: Dict[str, Any], slots: List[str]) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for i, slot in enumerate(slots, start=1):
        value = inputs.get(str(slot))
        if not _nonempty_input(value):
            continue
        info = _trace_upstream_load_image(prompt_graph, value)
        raw = str((info or {}).get("file") or "").strip()
        if raw:
            out[i] = raw
    return out


def _match_connected_alias(token: str, alias_map: Dict[int, List[str]]) -> Optional[int]:
    key = _norm(str(token or "").replace("\\", "/"))
    stem_key = _norm(_strip_known_image_ext(str(token or "")))
    matches: List[int] = []
    for idx, aliases in alias_map.items():
        for alias in aliases:
            astem = _norm(_strip_known_image_ext(alias))
            if key == alias or (stem_key and stem_key == astem):
                matches.append(int(idx))
                break
    matches = sorted(set(matches))
    return matches[0] if len(matches) == 1 else None


def _reference_latent_profile(prompt_graph: Dict[str, Any], source_id: str) -> Optional[Dict[str, Any]]:
    """Discover a flattened ReferenceLatent chain after a text-conditioning node.

    ComfyUI's graphToPrompt flattens subgraphs into executable inner nodes. This
    lets the plugin support Flux.2/Klein subgraph workflows even when the visible
    subgraph exposes only one IMAGE socket.
    """
    downstream = _prompt_downstream(prompt_graph, str(source_id), max_depth=20)
    refs: List[Tuple[int, str, Dict[str, Any]]] = []
    for nid, depth in downstream.items():
        node = prompt_graph.get(str(nid))
        if isinstance(node, dict) and str(node.get("class_type") or "") == "ReferenceLatent":
            refs.append((int(depth), str(nid), node))
    if not refs:
        return None
    refs.sort(key=lambda x: (x[0], x[1]))

    logical: List[Dict[str, Any]] = []
    by_latent: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for depth, nid, node in refs:
        inputs = node.get("inputs", {}) if isinstance(node.get("inputs"), dict) else {}
        latent = _conn(inputs.get("latent"))
        if latent is None:
            continue
        rec = by_latent.get(latent)
        if rec is None:
            info = _trace_upstream_load_image(prompt_graph, list(latent))
            rec = {
                "index": len(logical) + 1,
                "latent": [latent[0], latent[1]],
                "nodes": [],
                "depth": depth,
                "file": (info or {}).get("file"),
                "aliases": _aliases_from_file((info or {}).get("file")),
            }
            by_latent[latent] = rec
            logical.append(rec)
        rec["nodes"].append(str(nid))

    if not logical:
        return None

    ref_ids = {nid for _, nid, _ in refs}
    terminals: List[str] = []
    for _, nid, _ in refs:
        # A terminal chain node has no downstream ReferenceLatent before leaving
        # the reference-conditioning chain.
        ds = _prompt_downstream(prompt_graph, nid, max_depth=8)
        if not any(x in ref_ids for x in ds):
            terminals.append(str(nid))
    terminals = list(dict.fromkeys(terminals))
    if not terminals:
        terminals = [refs[-1][1]]

    # Reuse the VAE connection from an existing VAEEncode that feeds one of the
    # discovered reference latents.
    vae_conn = None
    scale_template: Optional[Dict[str, Any]] = None
    for rec in logical:
        latent = _conn(rec.get("latent"))
        if latent is None:
            continue
        enc = prompt_graph.get(latent[0])
        if not isinstance(enc, dict) or str(enc.get("class_type") or "") != "VAEEncode":
            continue
        einputs = enc.get("inputs", {}) if isinstance(enc.get("inputs"), dict) else {}
        vc = _conn(einputs.get("vae"))
        if vc is not None:
            vae_conn = [vc[0], vc[1]]
        pc = _conn(einputs.get("pixels"))
        if pc is not None:
            pnode = prompt_graph.get(pc[0])
            if isinstance(pnode, dict) and str(pnode.get("class_type") or "") == "ImageScaleToTotalPixels":
                pin = pnode.get("inputs", {}) if isinstance(pnode.get("inputs"), dict) else {}
                scale_template = {
                    "upscale_method": pin.get("upscale_method", "lanczos"),
                    "megapixels": pin.get("megapixels", 1.0),
                    # ComfyUI 0.5+ added this as a required API-prompt input.
                    # Preserve it when present and use the core default otherwise.
                    "resolution_steps": pin.get("resolution_steps", 1),
                }
        if vae_conn is not None:
            break
    if vae_conn is None:
        return None

    aliases: Dict[int, List[str]] = {}
    for rec in logical:
        if rec.get("aliases"):
            aliases[int(rec["index"])] = list(rec["aliases"])

    return {
        "name": "REFERENCE_LATENT_CHAIN",
        "source_id": str(source_id),
        "existing_refs": logical,
        "existing_count": len(logical),
        "terminal_nodes": terminals,
        "vae_conn": vae_conn,
        "scale_template": scale_template,
        "aliases": aliases,
        "tag_template": "Reference Image {i}",
        "append_bindings": True,
        "native_vision": True,
        "max_refs": MAX_OUTPUT_REFS,
    }


def _resolve_refs_for_reference_chain(
    prompt: str,
    profile: Dict[str, Any],
    strict_missing: bool = True,
) -> Tuple[List[Dict[str, Any]], List[Tuple[int, int, int]]]:
    text = str(prompt or "")
    matches = list(_MENTION_RE.finditer(text))
    if not matches:
        return [], []
    existing_count = int(profile.get("existing_count", 0))
    alias_map = dict(profile.get("aliases") or {})
    existing_files = {int(x.get("index", 0)): str(x.get("file") or "") for x in (profile.get("existing_refs") or []) if isinstance(x, dict)}
    library_index: Optional[List[Dict[str, str]]] = None
    library_slot: Dict[str, int] = {}
    refs_by_key: Dict[str, Dict[str, Any]] = {}
    refs: List[Dict[str, Any]] = []
    occurrences: List[Tuple[int, int, int]] = []

    def ensure_rec(key: str, idx: int, alias: str, source_kind: str, file: Optional[str] = None) -> Dict[str, Any]:
        rec = refs_by_key.get(key)
        if rec is None:
            rec = {
                "index": int(idx),
                "slot": f"ReferenceLatent {idx}",
                "alias": str(alias),
                "source_kind": source_kind,
                "file": file,
                "roles": [],
                "instructions": [],
                "contexts": [],
            }
            refs_by_key[key] = rec
            refs.append(rec)
        return rec

    for match in matches:
        token = _mention_token(match)
        idx = _slot_alias_number(token)
        if idx is None:
            idx = _match_connected_alias(token, alias_map)
        if idx is not None and 1 <= idx <= existing_count:
            rec = ensure_rec(f"slot:{idx}", idx, token, "CONNECTED_SLOT", None)
            if existing_files.get(int(idx)):
                rec["source_file"] = existing_files.get(int(idx))
        else:
            if _slot_alias_number(token) is not None and strict_missing:
                raise ValueError(
                    f"@{token} refers to connected reference {token}, but this ReferenceLatent chain currently has {existing_count} connected reference image(s)."
                )
            if library_index is None:
                library_index = _build_index(True)
            try:
                found = _resolve_token(token, library_index)
            except Exception:
                if strict_missing:
                    raise
                continue
            rel = str(found["rel"])
            if rel not in library_slot:
                library_slot[rel] = existing_count + len(library_slot) + 1
            idx = library_slot[rel]
            if idx > int(profile.get("max_refs", MAX_OUTPUT_REFS)):
                raise ValueError(f"Too many reference images for this ReferenceLatent chain: {idx}")
            rec = ensure_rec(f"file:{rel}", idx, token, "LIBRARY", rel)
            rec["uim_id"] = found.get("uim_id")
            rec["sha256"] = found.get("sha256")

        context = _context_window(text, match.start(), match.end(), radius=220)
        if context and context not in rec["contexts"]:
            rec["contexts"].append(context)
        for role in _roles_for_context(context):
            if role not in rec["roles"]:
                rec["roles"].append(role)
        if context and context not in rec["instructions"]:
            rec["instructions"].append(context[:520])
        occurrences.append((match.start(), match.end(), int(rec["index"])))

    refs.sort(key=lambda r: int(r["index"]))
    return refs, occurrences


def _rewire_output_consumers(prompt_graph: Dict[str, Any], old_id: str, new_id: str, output_slot: int = 0) -> int:
    changed = 0
    for nid, node in prompt_graph.items():
        if str(nid) == str(new_id) or not isinstance(node, dict):
            continue
        inputs = node.get("inputs", {})
        if not isinstance(inputs, dict):
            continue
        for name, value in list(inputs.items()):
            c = _conn(value)
            if c is not None and c[0] == str(old_id) and c[1] == int(output_slot):
                inputs[name] = [str(new_id), int(output_slot)]
                changed += 1
    return changed


def _inject_reference_latent_library_refs(
    prompt_graph: Dict[str, Any],
    profile: Dict[str, Any],
    refs: List[Dict[str, Any]],
    counter: List[int],
) -> List[Dict[str, Any]]:
    terminals = [str(x) for x in profile.get("terminal_nodes") or []]
    vae_conn = profile.get("vae_conn")
    if not terminals or _conn(vae_conn) is None:
        return []
    injected: List[Dict[str, Any]] = []

    for rec in refs:
        if rec.get("source_kind") != "LIBRARY":
            continue
        loader_id = _alloc_prompt_node_id(prompt_graph, counter)
        prompt_graph[loader_id] = {
            "class_type": "UniversalMentionLoadOne",
            "inputs": {"library_rel": str(rec.get("file") or "")},
        }
        pixels_conn: List[Any] = _inject_mask_node(prompt_graph, [loader_id, 0], rec, counter)

        scale_template = profile.get("scale_template")
        if isinstance(scale_template, dict):
            scale_id = _alloc_prompt_node_id(prompt_graph, counter)
            prompt_graph[scale_id] = {
                # Use our compatibility wrapper instead of constructing a core
                # ImageScaleToTotalPixels API node. Core ComfyUI has changed the
                # required input schema of that node before (e.g. resolution_steps),
                # which can invalidate dynamically-injected prompts on update.
                "class_type": "UniversalMentionScaleToTotalPixels",
                "inputs": {
                    "image": pixels_conn,
                    "upscale_method": scale_template.get("upscale_method", "lanczos"),
                    "megapixels": scale_template.get("megapixels", 1.0),
                    "resolution_steps": scale_template.get("resolution_steps", 1),
                },
            }
            pixels_conn = [scale_id, 0]

        encode_id = _alloc_prompt_node_id(prompt_graph, counter)
        prompt_graph[encode_id] = {
            "class_type": "VAEEncode",
            "inputs": {"pixels": pixels_conn, "vae": list(vae_conn)},
        }

        new_terminals: List[str] = []
        branch_nodes: List[str] = []
        for terminal in terminals:
            ref_id = _alloc_prompt_node_id(prompt_graph, counter)
            # Rewire existing consumers BEFORE adding the new node, otherwise
            # its own conditioning input would be rewritten into a self-loop.
            _rewire_output_consumers(prompt_graph, terminal, ref_id, 0)
            prompt_graph[ref_id] = {
                "class_type": "ReferenceLatent",
                "inputs": {"conditioning": [terminal, 0], "latent": [encode_id, 0]},
            }
            new_terminals.append(ref_id)
            branch_nodes.append(ref_id)
        terminals = new_terminals
        injected.append({
            "index": int(rec.get("index", 0)),
            "file": rec.get("file"),
            "loader": loader_id,
            "vae_encode": encode_id,
            "reference_latent_nodes": branch_nodes,
        })
    return injected


def _resolve_refs_for_target(
    prompt: str,
    target_id: str,
    adapter: Dict[str, Any],
    prompt_graph: Dict[str, Any],
    strict_missing: bool = True,
) -> Tuple[List[Dict[str, Any]], List[Tuple[int, int, int]]]:
    """Resolve a prompt against the target's *actual* reference slots.

    Two namespaces coexist:
      @1 / @2 / ...  -> existing connected reference slot 1 / 2 / ...
      @人物A / @衣服B -> image from the dedicated Mention Image Library,
                         assigned to the first free reference slot.

    This is what makes an already-wired 3-image H3 workflow addressable without
    replacing the user's existing wires.
    """
    text = str(prompt or "")
    matches = list(_MENTION_RE.finditer(text))
    if not matches:
        return [], []

    target = prompt_graph.get(str(target_id), {})
    inputs = target.get("inputs", {}) if isinstance(target, dict) else {}
    if not isinstance(inputs, dict):
        inputs = {}
    slots = list(adapter.get("slots") or [])
    max_refs = min(int(adapter.get("max_refs", len(slots) or MAX_OUTPUT_REFS)), len(slots) or MAX_OUTPUT_REFS)
    slots = slots[:max_refs]

    connected = {i + 1 for i, slot in enumerate(slots) if _nonempty_input(inputs.get(str(slot)))}
    connected_aliases = _connected_alias_map_for_slots(prompt_graph, inputs, slots)
    connected_files = _connected_file_map_for_slots(prompt_graph, inputs, slots)
    allocated_library_slots: Dict[str, int] = {}
    refs_by_key: Dict[str, Dict[str, Any]] = {}
    refs: List[Dict[str, Any]] = []
    occurrences: List[Tuple[int, int, int]] = []
    library_index: Optional[List[Dict[str, str]]] = None

    def ensure_rec(key: str, **kwargs: Any) -> Dict[str, Any]:
        rec = refs_by_key.get(key)
        if rec is None:
            rec = {
                "index": int(kwargs["index"]),
                "slot": str(kwargs["slot"]),
                "alias": str(kwargs.get("alias", "")),
                "source_kind": str(kwargs.get("source_kind", "CONNECTED_SLOT")),
                "file": kwargs.get("file"),
                "roles": [],
                "instructions": [],
                "contexts": [],
            }
            refs_by_key[key] = rec
            refs.append(rec)
        return rec

    for mi, match in enumerate(matches):
        token = _mention_token(match)
        slot_alias = _slot_alias_number(token)
        if slot_alias is None:
            slot_alias = _match_connected_alias(token, connected_aliases)

        if slot_alias is not None:
            if slot_alias > len(slots):
                if strict_missing:
                    raise ValueError(
                        f"@{token} asks for reference slot {slot_alias}, but target {target_id} "
                        f"only exposes {len(slots)} image reference slots."
                    )
                continue
            if slot_alias not in connected:
                if strict_missing:
                    raise ValueError(
                        f"@{token} refers to reference slot {slot_alias}, but that slot is not connected. "
                        "Connect an image there first, or use @图片文件名 to load from the Mention Image Library."
                    )
                continue
            slot_name = str(slots[slot_alias - 1])
            rec = ensure_rec(
                f"slot:{slot_alias}", index=slot_alias, slot=slot_name,
                alias=token, source_kind="CONNECTED_SLOT", file=None,
            )
            if connected_files.get(int(slot_alias)):
                rec["source_file"] = connected_files.get(int(slot_alias))
        else:
            if library_index is None:
                library_index = _build_index(True)
            try:
                found = _resolve_token(token, library_index)
            except Exception:
                if strict_missing:
                    raise
                continue
            rel = str(found["rel"])
            if rel not in allocated_library_slots:
                occupied = set(connected) | set(allocated_library_slots.values())
                free = [i for i in range(1, len(slots) + 1) if i not in occupied]
                if not free:
                    raise ValueError(
                        f"No free reference image slot remains on target {target_id}. "
                        f"It has {len(connected)} connected slot(s) and {len(allocated_library_slots)} @library image(s)."
                    )
                allocated_library_slots[rel] = free[0]
            idx = allocated_library_slots[rel]
            rec = ensure_rec(
                f"file:{rel}", index=idx, slot=str(slots[idx - 1]),
                alias=token, source_kind="LIBRARY", file=rel,
            )
            rec["uim_id"] = found.get("uim_id")
            rec["sha256"] = found.get("sha256")

        context = _context_window(text, match.start(), match.end(), radius=220)
        if context and context not in rec["contexts"]:
            rec["contexts"].append(context)
        for role in _roles_for_context(context):
            if role not in rec["roles"]:
                rec["roles"].append(role)

        # Keep the whole local sentence as the linked instruction. This is
        # relationship-aware: "@1 ... @2 ..." remains one semantic unit instead
        # of being incorrectly split at the second mention.
        if context and context not in rec["instructions"]:
            rec["instructions"].append(context[:520])
        occurrences.append((match.start(), match.end(), int(rec["index"])))

    refs.sort(key=lambda r: int(r["index"]))
    return refs, occurrences


def _render_target_semantic_prompt(
    prompt: str,
    refs: List[Dict[str, Any]],
    occurrences: List[Tuple[int, int, int]],
    adapter: Dict[str, Any],
) -> str:
    """Render target-native tags plus explicit cross-image relationship hints."""
    text = str(prompt or "")
    tag_template = str(adapter.get("tag_template", "Reference Image {i}"))
    ref_by_index = {int(r["index"]): r for r in refs}

    for start, end, idx in reversed(occurrences):
        rec = ref_by_index.get(int(idx), {})
        tag = tag_template.format(i=idx, index=idx, alias=rec.get("alias", ""), file=rec.get("file") or "")
        text = text[:start] + tag + text[end:]

    if not refs or not adapter.get("append_bindings", True):
        return text

    lines: List[str] = []
    for rec in refs:
        idx = int(rec["index"])
        tag = tag_template.format(i=idx, index=idx, alias=rec.get("alias", ""), file=rec.get("file") or "")
        roles = ", ".join(rec.get("roles") or ["GENERAL_REFERENCE"])
        src = (
            f"already-connected reference slot {idx} ({rec.get('slot')})"
            if rec.get("source_kind") == "CONNECTED_SLOT"
            else f"Mention Image Library file {rec.get('file')}"
        )
        linked = " / ".join(dict.fromkeys(rec.get("instructions") or []))[:900]
        if linked:
            lines.append(f"{tag}: source={src}; linked instruction/context: {linked}; roles={roles}.")
        else:
            lines.append(f"{tag}: source={src}; roles={roles}.")

    # Cross-reference relationship guidance. This does not invent visual facts;
    # it tells the vision model how to use references that appear in the same
    # instruction. H3/Qwen-VL still reads the actual pixels itself.
    relation_lines: List[str] = []
    # Only infer a relationship when two or more references occur in the SAME
    # sentence/clause. Independent sentences such as "@1 keep person. @2 use
    # background." must remain independent.
    clauses = [x.strip() for x in re.split(r"[。；;!?！？\n]+", text) if x.strip()]
    for clause in clauses:
        normalized = _norm(clause)
        tags_in_order = []
        for idx in sorted(ref_by_index):
            tag = tag_template.format(i=idx, index=idx, alias="", file="")
            pos = normalized.find(_norm(tag))
            if pos >= 0:
                tags_in_order.append((pos, idx, tag))
        tags_in_order.sort()
        if len(tags_in_order) < 2:
            continue

        edit_words = ("换", "替换", "改成", "变成", "change", "replace", "turn into")
        clothing_words = ("衣服", "服装", "穿搭", "衣着", "款式", "clothing", "outfit", "dress", "shirt")
        pose_words = ("动作", "姿势", "站姿", "手势", "pose", "motion", "action")
        has_edit = any(_norm(w) in normalized for w in edit_words)
        has_clothing = any(_norm(w) in normalized for w in clothing_words)
        has_pose = any(_norm(w) in normalized for w in pose_words)
        taga = tags_in_order[0][2]
        tagb = tags_in_order[1][2]
        if has_edit and has_clothing:
            relation_lines.append(
                f"Relationship for this instruction: treat {taga} as the edit target/base subject and {tagb} as the source reference for clothing design/style/details. "
                f"Preserve the identity, face, hair, body proportions and all non-requested attributes of {taga} unless the user explicitly changes them."
            )
        elif has_pose:
            relation_lines.append(
                f"Relationship for this instruction: keep {taga} and {tagb} distinct. Follow the sentence direction exactly; transfer pose/motion only from the requested source, "
                f"without transferring source identity unless explicitly requested."
            )
        else:
            relation_lines.append(
                f"Relationship for this instruction: {taga} and {tagb} are directional references, not images to blend indiscriminately. Preserve the target and transfer only explicitly requested attributes from the source."
            )

    out = text.rstrip() + "\n\n[Reference bindings]\n" + "\n".join(lines)
    # V3/V3.1: compute the directional graph before vision grounding so rich
    # image semantics can be narrowed to the requested source attribute.
    graph = _relationship_graph(prompt, refs, occurrences)
    vision_lines = _vision_semantic_lines(refs, tag_template, graph)
    if vision_lines:
        out += "\n[Visual semantic grounding]\n" + "\n".join(vision_lines)
    # Deterministic relationship graph supports both forward and reversed
    # Chinese phrasing, and preserves a machine-readable direction internally.
    graph_lines = _relationship_guidance(graph, tag_template)
    if graph_lines:
        out += "\n[Reference relationship graph]\n" + "\n".join(graph_lines)
    elif relation_lines:
        out += "\n[Cross-reference relationships]\n" + "\n".join(relation_lines)
    control_lines = _v4_control_lines(refs, tag_template)
    if control_lines:
        out += "\n[V4 per-reference controls]\n" + "\n".join(control_lines)
    return out


def _inject_library_refs_by_assigned_slot(
    prompt_graph: Dict[str, Any],
    target_id: str,
    refs: List[Dict[str, Any]],
    counter: List[int],
) -> List[str]:
    """Inject only @library images. Never replace an already connected slot."""
    target = prompt_graph.get(str(target_id))
    if not isinstance(target, dict):
        return []
    inputs = target.setdefault("inputs", {})
    used: List[str] = []
    for rec in refs:
        if rec.get("source_kind") != "LIBRARY":
            continue
        slot = str(rec.get("slot") or "")
        if not slot:
            continue
        if _nonempty_input(inputs.get(slot)):
            # Fail-safe: target graph changed after resolution; preserve the
            # user's existing wire rather than silently overwriting it.
            continue
        loader_id = _alloc_prompt_node_id(prompt_graph, counter)
        prompt_graph[loader_id] = {
            "class_type": "UniversalMentionLoadOne",
            "inputs": {"library_rel": str(rec.get("file") or "")},
        }
        image_conn = _inject_mask_node(prompt_graph, [loader_id, 0], rec, counter)
        inputs[slot] = image_conn
        used.append(slot)
    return used


# =============================================================================
# V3 Relationship Graph + Bind Validator
# =============================================================================

_ATTR_WORDS = {
    "CLOTHING": ("衣服", "服装", "穿搭", "衣着", "上衣", "下装", "裙子", "裤子", "鞋", "帽", "配饰", "款式", "剪裁", "材质", "颜色", "clothing", "clothes", "outfit", "garment", "dress", "shirt"),
    "POSE_MOTION": ("动作", "姿势", "姿态", "站姿", "坐姿", "手势", "motion", "pose", "action", "gesture"),
    "IDENTITY": ("人物", "身份", "脸", "五官", "脸型", "长相", "发型", "肤色", "体型", "身体比例", "identity", "face", "hair", "body"),
    "SCENE": ("背景", "场景", "环境", "室内", "室外", "scene", "background", "environment"),
    "STYLE": ("风格", "画风", "质感", "光影", "色调", "style", "aesthetic", "lighting", "texture"),
    "PRODUCT_OBJECT": ("产品", "商品", "物品", "包装", "产品外观", "product", "object", "package"),
}
_PRESERVE_WORDS = ("保持", "保留", "不变", "不要改变", "不要继承", "禁止继承", "preserve", "keep", "unchanged", "do not transfer", "do not inherit")
_TRANSFER_WORDS = ("换成", "替换", "改成", "变成", "参考", "按照", "使用", "用", "穿", "给", "transfer", "replace", "change", "use", "follow", "reference")


def _attr_for_clause(text: str) -> str:
    low = _norm(text)
    scores = []
    for attr, words in _ATTR_WORDS.items():
        score = sum(1 for w in words if _norm(w) in low)
        if score:
            scores.append((score, attr))
    scores.sort(reverse=True)
    return scores[0][1] if scores else "GENERAL_REFERENCE"


def _canonical_clause_with_refs(clause: str, clause_occurrences: List[Tuple[int, int, int]], clause_start: int) -> str:
    out = str(clause)
    local = []
    for a,b,idx in clause_occurrences:
        la, lb = a-clause_start, b-clause_start
        if 0 <= la <= lb <= len(clause):
            local.append((la, lb, idx))
    for a,b,idx in reversed(local):
        out = out[:a] + f"<UIM_REF_{idx}>" + out[b:]
    return out


def _relationship_graph(prompt: str, refs: List[Dict[str, Any]], occurrences: List[Tuple[int,int,int]]) -> Dict[str, Any]:
    """Build a deterministic cross-reference relationship graph from user text.

    The parser intentionally does NOT describe pixels. It only extracts direction,
    requested attribute and preserve/transfer intent from the user's own language.
    """
    text = str(prompt or "")
    relations: List[Dict[str, Any]] = []
    assignments: Dict[str, int] = {}
    preserves: List[Dict[str, Any]] = []
    ref_ids = {int(r.get("index",0)) for r in refs}

    # split while retaining offsets
    starts = [0]
    for m in re.finditer(r"[。；;!?！？\n,，]+", text):
        starts.append(m.end())
    spans=[]
    for i, st in enumerate(starts):
        en = starts[i+1] if i+1 < len(starts) else len(text)
        raw = text[st:en].strip()
        if raw:
            # compensate stripped leading whitespace
            lead = len(text[st:en]) - len(text[st:en].lstrip())
            spans.append((st+lead, st+lead+len(raw), raw))

    for st,en,clause in spans:
        occ=[x for x in occurrences if x[0] >= st and x[1] <= en]
        ids=[]
        for _,_,idx in occ:
            if idx not in ids: ids.append(idx)
        if not ids:
            continue
        canon=_canonical_clause_with_refs(clause, occ, st)
        low=_norm(canon)
        attr=_attr_for_clause(canon)

        # Explicit role assignments: 人物用@1 / 衣服参考@2 / 动作用@3 / 背景保持@1
        role_patterns = [
            ("IDENTITY", r"(?:人物|主体|身份|脸|人脸|character|identity|face)\s*(?:用|使用|参考|保持|=|:|：)\s*<UIM_REF_(\d+)>") ,
            ("CLOTHING", r"(?:衣服|服装|穿搭|衣着|clothing|outfit|garment)\s*(?:用|使用|参考|来自|=|:|：)\s*<UIM_REF_(\d+)>") ,
            ("POSE_MOTION", r"(?:动作|姿势|姿态|pose|motion|action)\s*(?:用|使用|参考|按照|来自|=|:|：)\s*<UIM_REF_(\d+)>") ,
            ("SCENE", r"(?:背景|场景|环境|background|scene)\s*(?:用|使用|参考|保持|来自|=|:|：)\s*<UIM_REF_(\d+)>") ,
            ("STYLE", r"(?:风格|画风|质感|style|aesthetic)\s*(?:用|使用|参考|来自|=|:|：)\s*<UIM_REF_(\d+)>") ,
            ("PRODUCT_OBJECT", r"(?:产品|商品|物品|product|object)\s*(?:用|使用|参考|保持|来自|=|:|：)\s*<UIM_REF_(\d+)>") ,
        ]
        for role,pat in role_patterns:
            for m in re.finditer(pat, canon, flags=re.I):
                idx=int(m.group(1))
                if idx in ref_ids: assignments[role]=idx

        # Pattern A: 让@1...换成@2的衣服 / @1的衣服改成@2 / @1穿@2的衣服
        m=re.search(r"<UIM_REF_(\d+)>.*?(?:换成|替换(?:成|为)?|改成|变成|穿|change|replace|turn into).*?<UIM_REF_(\d+)>", canon, flags=re.I)
        if m:
            target, source=int(m.group(1)), int(m.group(2))
            relations.append({"action":"TRANSFER", "source":source, "target":target, "attribute":attr, "clause":clause})
        else:
            # Pattern B: 把@2的衣服给@1穿 / 将@2...替换到@1
            m=re.search(r"(?:把|将)\s*<UIM_REF_(\d+)>.*?(?:给|到|至|替换到|放到|穿到).*?<UIM_REF_(\d+)>", canon, flags=re.I)
            if m:
                source,target=int(m.group(1)),int(m.group(2))
                relations.append({"action":"TRANSFER", "source":source, "target":target, "attribute":attr, "clause":clause})
            elif len(ids) >= 2 and any(_norm(w) in low for w in _TRANSFER_WORDS):
                # Conservative fallback: first mention is target, second is source.
                relations.append({"action":"TRANSFER", "source":ids[1], "target":ids[0], "attribute":attr, "clause":clause, "inferred":True})

        if any(_norm(w) in low for w in _PRESERVE_WORDS):
            # preserve applies to the first mentioned/assigned base unless wording names another.
            target=ids[0]
            preserves.append({"target":target, "attribute":attr, "clause":clause})

    # If roles define a base identity and source roles, create missing directional edges.
    base = assignments.get("IDENTITY")
    if base:
        for attr in ("CLOTHING","POSE_MOTION","SCENE","STYLE","PRODUCT_OBJECT"):
            src=assignments.get(attr)
            if src and src != base and not any(r.get("source")==src and r.get("target")==base and r.get("attribute")==attr for r in relations):
                relations.append({"action":"TRANSFER", "source":src, "target":base, "attribute":attr, "clause":f"role assignment: {attr}"})

    # Deduplicate deterministically.
    dedup=[]; seen=set()
    for r in relations:
        key=(r.get("action"),r.get("source"),r.get("target"),r.get("attribute"),r.get("clause"))
        if key not in seen:
            seen.add(key); dedup.append(r)
    return {"assignments": assignments, "relations": dedup, "preserves": preserves}


def _relationship_guidance(graph: Dict[str, Any], tag_template: str) -> List[str]:
    def tag(i:int)->str:
        return tag_template.format(i=i,index=i,alias="",file="")
    lines=[]
    attr_text={
        "CLOTHING":"clothing only (style, cut, material, color and garment details)",
        "POSE_MOTION":"pose/motion only",
        "IDENTITY":"identity/face/hair/body identity attributes only",
        "SCENE":"background/scene only",
        "STYLE":"visual style/lighting/texture only",
        "PRODUCT_OBJECT":"product/object appearance only",
        "GENERAL_REFERENCE":"only the explicitly requested attribute",
    }
    for r in graph.get("relations") or []:
        s,t=int(r["source"]),int(r["target"]); attr=str(r.get("attribute") or "GENERAL_REFERENCE")
        if attr == "CLOTHING":
            lines.append(
                f"TRANSFER {attr}: treat {tag(t)} as the edit target/base subject and {tag(s)} as the source reference for clothing. "
                f"Apply {attr_text.get(attr, attr_text['GENERAL_REFERENCE'])} from {tag(s)} to {tag(t)}. "
                f"Do not transfer unrelated identity or attributes from {tag(s)}."
            )
        else:
            lines.append(
                f"TRANSFER {attr}: use {tag(s)} as the source and apply {attr_text.get(attr, attr_text['GENERAL_REFERENCE'])} to {tag(t)}. "
                f"Do not transfer unrelated identity or attributes from {tag(s)}."
            )
    for p in graph.get("preserves") or []:
        t=int(p["target"]); attr=str(p.get("attribute") or "GENERAL_REFERENCE")
        lines.append(f"PRESERVE on {tag(t)}: keep all non-requested attributes unchanged; preservation request context={attr}.")
    return list(dict.fromkeys(lines))


_RUN_MARKER_RE = re.compile(r"\[UIM-RUN\s+id=([A-Za-z0-9_-]{8,80})\s+root=([A-Za-z0-9_-]{8,80})\s+parent=([A-Za-z0-9_-]{1,80}|-)\s+retry=(\d{1,2})\]", re.I)
_RUN_REPORTS: Dict[str, Dict[str, Any]] = {}
_RUN_REPORT_ORDER: List[str] = []


def _extract_run_meta_and_strip(prompt_graph: Dict[str, Any]) -> Dict[str, Any]:
    found = None
    for node in prompt_graph.values():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        for name, value in list(node["inputs"].items()):
            if not isinstance(value, str):
                continue
            matches = list(_RUN_MARKER_RE.finditer(value))
            if matches and found is None:
                m = matches[0]
                found = {"run_id":m.group(1),"root_run_id":m.group(2),"parent_run_id":"" if m.group(3)=="-" else m.group(3),"retry_index":int(m.group(4)),"kind":"AUTO_RETRY"}
            if matches:
                node["inputs"][name] = _RUN_MARKER_RE.sub("", value).strip()
    if found is None:
        rid = uuid.uuid4().hex
        found = {"run_id":rid,"root_run_id":rid,"parent_run_id":"","retry_index":0,"kind":"USER"}
    cfg = _read_v4_config()
    max_retry = int(cfg.get("audit_max_retries", 1) or 1)
    if int(found.get("retry_index", 0)) > max_retry:
        raise ValueError(f"UIM retry guard blocked retry_index={found.get('retry_index')} > configured max={max_retry}.")
    found["created_at"] = time.time()
    return found


def _store_run_report(run_meta: Dict[str, Any], report: Dict[str, Any]) -> None:
    rid = str(run_meta.get("run_id") or "")
    if not rid:
        return
    _RUN_REPORTS[rid] = dict(report)
    if rid in _RUN_REPORT_ORDER:
        _RUN_REPORT_ORDER.remove(rid)
    _RUN_REPORT_ORDER.append(rid)
    while len(_RUN_REPORT_ORDER) > 96:
        old = _RUN_REPORT_ORDER.pop(0)
        _RUN_REPORTS.pop(old, None)


def _run_report(run_id: str) -> Optional[Dict[str, Any]]:
    value = _RUN_REPORTS.get(str(run_id or ""))
    return dict(value) if isinstance(value, dict) else None


def _strict_bind_enabled() -> bool:
    return _norm(os.environ.get("UIM_STRICT_BIND", "1")) not in {"0","false","off","no"}


def _validate_report_item(report: Dict[str, Any]) -> Optional[str]:
    status=str(report.get("status") or "")
    if status in {"resolve_error","no_compatible_image_target","no_supported_image_slots","bind_mismatch"}:
        return str(report.get("error") or report.get("note") or status)
    if status == "bound":
        refs=report.get("references") or []
        if not refs:
            return "Mention text was detected but no reference records were produced."
        for r in refs:
            if not r.get("slot") and report.get("adapter") != "REFERENCE_LATENT_CHAIN":
                return f"@{r.get('alias')} has no physical target slot."
    return None


def _update_last_bind_report(reports: List[Dict[str, Any]], status: str, error: Optional[str]=None, run_meta: Optional[Dict[str, Any]]=None) -> None:
    global _LAST_BIND_REPORT
    _LAST_BIND_REPORT = {
        "plugin": PLUGIN_NAME, "version": PLUGIN_VERSION, "status": status, "error": error,
        "library_path": str(_library_root()), "timestamp": time.time(), "reports": reports,
        "run": dict(run_meta or {}),
    }
    if run_meta:
        _store_run_report(run_meta, _LAST_BIND_REPORT)

def _autobind_enabled() -> bool:
    return _norm(os.environ.get("UIM_AUTOBIND", "1")) not in {"0", "false", "off", "no"}


def _patch_core_scale_resolution_steps(prompt_graph: Dict[str, Any]) -> List[str]:
    """Repair old ImageScaleToTotalPixels API prompts on newer ComfyUI.

    ComfyUI added `resolution_steps` as a required API-prompt input while giving
    it a UI default. Old/subgraph-expanded prompts may therefore validate in the
    editor yet fail server-side. UIM only applies this compatibility repair when
    the current queue contains an @mention handled by UIM.
    """
    patched: List[str] = []
    for nid, node in prompt_graph.items():
        if not isinstance(node, dict) or str(node.get("class_type") or "") != "ImageScaleToTotalPixels":
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        if "resolution_steps" not in inputs:
            inputs["resolution_steps"] = 1
            patched.append(str(nid))
    return patched


def _global_autobind_on_prompt(json_data: Dict[str, Any]) -> Dict[str, Any]:
    """V3 submit-time compiler: resolve -> relationship graph -> adapter bind -> validate.

    Strict mode (default) blocks Queue when a resolvable @image mention cannot be
    physically bound to the target model/reference-conditioning path.
    """
    if not _autobind_enabled() or not isinstance(json_data, dict):
        return json_data
    # Idempotency for unusual forks that invoke the same prompt handler twice on
    # the same in-memory payload. Normal ComfyUI queues always receive a fresh graph.
    extra0=json_data.setdefault("extra_data", {})
    if extra0.get("_uim_v3_processed"):
        return json_data

    prompt_graph = json_data.get("prompt")
    if not isinstance(prompt_graph, dict):
        return json_data

    # Only apply compatibility repairs when this queue actually contains a UIM
    # mention. This avoids mutating unrelated workflows merely because the
    # extension is installed.
    has_uim_mention = False
    for _node in prompt_graph.values():
        if not isinstance(_node, dict):
            continue
        _inputs = _node.get("inputs")
        if not isinstance(_inputs, dict):
            continue
        if any(isinstance(_v, str) and "@" in _v and _MENTION_RE.search(_v) for _v in _inputs.values()):
            has_uim_mention = True
            break
    compat_patches = _patch_core_scale_resolution_steps(prompt_graph) if has_uim_mention else []

    run_meta = _extract_run_meta_and_strip(prompt_graph)
    workflow = _workflow_payload(json_data)
    ui_nodes = _ui_nodes_by_id(workflow)
    counter = _initial_alloc_counter(prompt_graph)
    reports: List[Dict[str, Any]] = []
    errors: List[str] = []

    # Snapshot because runtime adapter nodes are injected during iteration.
    for source_id, pnode in list(prompt_graph.items()):
        if not isinstance(pnode, dict):
            continue
        inputs = pnode.get("inputs", {})
        if not isinstance(inputs, dict):
            continue
        string_fields = [(k,v) for k,v in list(inputs.items()) if isinstance(v,str) and "@" in v and _MENTION_RE.search(v)]
        if not string_fields:
            continue

        source_ui = ui_nodes.get(str(source_id))
        source_class = _node_class(prompt_graph, str(source_id), source_ui)

        for field_name, raw_text in string_fields:
            handled=False
            source_low=source_class.casefold()

            # Adapter 1: flattened Flux/Klein/other ReferenceLatent pipeline.
            if "textencode" in source_low or source_class == "CLIPTextEncode":
                chain_profile=_reference_latent_profile(prompt_graph, str(source_id))
                if chain_profile is not None:
                    try:
                        refs, occ=_resolve_refs_for_reference_chain(raw_text, chain_profile, strict_missing=True)
                        if refs:
                            adapter={"name":"REFERENCE_LATENT_CHAIN", "tag_template": chain_profile.get("tag_template","Reference Image {i}"), "append_bindings":True, "native_vision":True, "capabilities":{"supports_reference_image":True,"supports_multi_reference":True,"supports_native_strength":False,"supports_native_mask":False,"confidence":"TOPOLOGY_VERIFIED","reference_semantics":["GENERAL_REFERENCE"]}}
                            _merge_v4_ref_controls(refs, source_ui, source_ui)
                            _prepare_control_modes(refs, adapter)
                            occ=_apply_v4_library_order_for_chain(refs, occ, chain_profile)
                            _enrich_refs_with_vision(refs, adapter)
                            semantic=_render_target_semantic_prompt(raw_text, refs, occ, adapter)
                            masked_existing=_apply_existing_reference_latent_masks(prompt_graph, chain_profile, refs, counter)
                            injected=_inject_reference_latent_library_refs(prompt_graph, chain_profile, refs, counter)
                            # Validate every library ref resulted in one actual injected branch.
                            lib_refs=[r for r in refs if r.get("source_kind")=="LIBRARY"]
                            if len(injected) != len(lib_refs):
                                raise ValueError(f"ReferenceLatent bind mismatch: requested {len(lib_refs)} library reference(s), injected {len(injected)}.")
                            pnode.setdefault("inputs", {})[str(field_name)] = semantic
                            graph=_relationship_graph(raw_text, refs, occ)
                            rep={
                                "source":str(source_id),"target":str(source_id),"field":str(field_name),
                                "adapter":"REFERENCE_LATENT_CHAIN","status":"bound",
                                "existing_reference_count":chain_profile.get("existing_count",0),
                                "injected_reference_latents":injected,
                                "masked_existing_references":masked_existing,
                                "adapter_capabilities":_adapter_capabilities(adapter),
                                "references":[{"index":int(r.get("index",0)),"slot":r.get("slot"),"source_kind":r.get("source_kind"),"file":r.get("file"),"source_file":r.get("source_file"),"uim_id":r.get("uim_id"),"sha256":r.get("sha256"),"alias":r.get("alias"),"roles":r.get("roles"),"vision":r.get("vision"),"v4":r.get("v4")} for r in refs],
                                "relationship_graph":graph,"semantic_prompt":semantic,
                            }
                            reports.append(rep); handled=True
                    except Exception as exc:
                        rep={"source":str(source_id),"field":str(field_name),"adapter":"REFERENCE_LATENT_CHAIN","status":"resolve_error","error":str(exc)}
                        reports.append(rep); errors.append(f"Node {source_id}.{field_name}: {exc}"); handled=True

            if handled:
                continue

            candidates: List[Tuple[str, Optional[str], Dict[str, Any]]] = []
            direct_adapter=_adapter_for_target(source_class, source_ui)
            if direct_adapter is not None:
                candidates.append((str(source_id), str(field_name), direct_adapter))
            for target_id in _direct_target_ids(str(source_id), workflow):
                target_ui=ui_nodes.get(str(target_id))
                target_class=_node_class(prompt_graph,str(target_id),target_ui)
                adapter=_adapter_for_target(target_class,target_ui)
                if adapter is None: continue
                prompt_input=_find_prompt_input_on_target(target_ui,str(source_id),workflow)
                if not prompt_input and adapter.get("prompt_input"):
                    prompt_input=str(adapter.get("prompt_input"))
                if prompt_input: candidates.append((str(target_id),prompt_input,adapter))
            uniq=[]; seen=set()
            for item in candidates:
                key=(item[0],item[1])
                if key not in seen: seen.add(key); uniq.append(item)
            candidates=uniq

            if not candidates:
                rep={"source":str(source_id),"field":str(field_name),"status":"no_compatible_image_target","note":"@image mention is present, but no physical image-reference target was found in this execution path."}
                reports.append(rep); errors.append(f"Node {source_id}.{field_name}: {rep['note']}")
                continue

            for target_id,prompt_input,adapter in candidates:
                try:
                    if adapter.get("validation_errors"):
                        raise ValueError("Adapter capability validation failed: " + " | ".join(adapter.get("validation_errors") or []))
                    refs,occ=_resolve_refs_for_target(raw_text,str(target_id),adapter,prompt_graph,strict_missing=True)
                    if not refs: continue
                    target_ui_now=ui_nodes.get(str(target_id))
                    _merge_v4_ref_controls(refs, source_ui, target_ui_now)
                    _prepare_control_modes(refs, adapter)
                    occ=_apply_v4_library_order_for_target(refs, occ, str(target_id), adapter, prompt_graph)
                    _enrich_refs_with_vision(refs, adapter)
                    semantic=_render_target_semantic_prompt(raw_text,refs,occ,adapter)
                    native_strengths=_apply_native_strengths(prompt_graph,str(target_id),refs,adapter)
                    masked_connected=_apply_connected_masks_direct(prompt_graph,str(target_id),refs,counter)
                    injected_slots=_inject_library_refs_by_assigned_slot(prompt_graph,str(target_id),refs,counter)
                    native_masks=_apply_native_masks(prompt_graph,str(target_id),refs,adapter,counter)
                    lib_slots=[str(r.get("slot")) for r in refs if r.get("source_kind")=="LIBRARY"]
                    if set(injected_slots) != set(lib_slots):
                        raise ValueError(f"Physical bind mismatch: expected library slots {lib_slots}, injected {injected_slots}.")
                    bound_slots=[str(r.get("slot")) for r in refs if r.get("slot")]
                    if not bound_slots:
                        raise ValueError("No supported physical IMAGE reference slots are available.")
                    if str(target_id)==str(source_id):
                        prompt_graph[str(target_id)].setdefault("inputs",{})[str(prompt_input)] = semantic
                        text_node_id=None
                    else:
                        text_node_id=_inject_text_for_target(prompt_graph,str(target_id),str(prompt_input),semantic,counter)
                    graph=_relationship_graph(raw_text,refs,occ)
                    reports.append({
                        "source":str(source_id),"target":str(target_id),"prompt_input":str(prompt_input),
                        "adapter":adapter.get("name"),"status":"bound","slots":bound_slots,
                        "injected_library_slots":injected_slots,
                        "masked_connected_references":masked_connected,
                        "native_strength_inputs":native_strengths,
                        "native_mask_inputs":native_masks,
                        "adapter_capabilities":_adapter_capabilities(adapter),
                        "references":[{"index":int(r.get("index",0)),"slot":r.get("slot"),"source_kind":r.get("source_kind"),"file":r.get("file"),"source_file":r.get("source_file"),"uim_id":r.get("uim_id"),"sha256":r.get("sha256"),"alias":r.get("alias"),"roles":r.get("roles"),"vision":r.get("vision"),"v4":r.get("v4")} for r in refs],
                        "relationship_graph":graph,"semantic_prompt":semantic,"text_node":text_node_id,
                    })
                except Exception as exc:
                    reports.append({"source":str(source_id),"target":str(target_id),"field":str(field_name),"adapter":adapter.get("name"),"status":"resolve_error","error":str(exc)})
                    errors.append(f"Node {source_id}.{field_name} -> {adapter.get('name')}: {exc}")

    # Validate report as a second line of defense.
    for rep in reports:
        msg=_validate_report_item(rep)
        if msg and msg not in errors: errors.append(msg)

    extra=json_data.setdefault("extra_data",{})
    extra["_uim_v3_processed"] = True
    extra["uim_run"] = dict(run_meta)
    extra["uim_autobind"]={"plugin":PLUGIN_NAME,"version":PLUGIN_VERSION,"library_path":str(_library_root()),"strict":_strict_bind_enabled(),"run":dict(run_meta),"compat_patches":{"ImageScaleToTotalPixels.resolution_steps":compat_patches},"reports":reports,"errors":errors}

    if errors:
        detail=" | ".join(errors[:8])
        _update_last_bind_report(reports,"blocked",detail,run_meta)
        if _strict_bind_enabled():
            raise ValueError(f"{PLUGIN_NAME} V4.2 Audit/Reliability Bind Validator blocked Queue: {detail}")
    _update_last_bind_report(reports,"ok",None,run_meta)
    return json_data



class UniversalMentionScaleToTotalPixels:
    """Runtime-stable total-pixel scaler used by injected ReferenceLatent refs.

    This intentionally mirrors the useful behavior of ComfyUI's
    ImageScaleToTotalPixels while keeping a plugin-owned input contract.
    It prevents core-node schema additions from invalidating API prompts that
    UIM creates at queue time.
    """
    _METHODS = ["nearest-exact", "bilinear", "area", "bicubic", "lanczos"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "upscale_method": (cls._METHODS, {"default": "lanczos"}),
                "megapixels": ("FLOAT", {"default": 1.0, "min": 0.01, "max": 64.0, "step": 0.01}),
                "resolution_steps": ("INT", {"default": 1, "min": 1, "max": 256, "step": 1}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "scale"
    CATEGORY = CATEGORY

    def scale(self, image, upscale_method, megapixels, resolution_steps):
        import math
        import torch.nn.functional as F

        if not hasattr(image, "shape") or len(image.shape) != 4:
            raise ValueError("UniversalMentionScaleToTotalPixels expects IMAGE tensor [B,H,W,C]")
        samples = image.movedim(-1, 1)
        h = int(samples.shape[2])
        w = int(samples.shape[3])
        if h <= 0 or w <= 0:
            raise ValueError("Cannot scale an empty image")

        steps = max(1, int(resolution_steps or 1))
        total = max(0.01, float(megapixels or 1.0)) * 1024.0 * 1024.0
        scale_by = math.sqrt(total / float(w * h))
        out_w = max(steps, round(w * scale_by / steps) * steps)
        out_h = max(steps, round(h * scale_by / steps) * steps)

        method = str(upscale_method or "lanczos")
        try:
            import comfy.utils as comfy_utils
            out = comfy_utils.common_upscale(samples, int(out_w), int(out_h), method, "disabled")
        except Exception:
            # Fallback keeps the injected graph executable even if a future
            # ComfyUI build moves/changes common_upscale. Lanczos is mapped to
            # bicubic because torch.interpolate has no Lanczos mode.
            mode_map = {
                "nearest-exact": "nearest-exact",
                "bilinear": "bilinear",
                "area": "area",
                "bicubic": "bicubic",
                "lanczos": "bicubic",
            }
            mode = mode_map.get(method, "bicubic")
            kwargs = {}
            if mode in {"bilinear", "bicubic"}:
                kwargs["align_corners"] = False
            out = F.interpolate(samples, size=(int(out_h), int(out_w)), mode=mode, **kwargs)

        return (out.movedim(1, -1),)


class UniversalMentionApplyMask:
    """V4 runtime image-mask focus node.

    When the downstream model has no native per-reference MASK socket, V4 keeps
    the feature useful by suppressing pixels outside the selected region before
    the image enters H3/ReferenceLatent/other image conditioning. This is an
    actual pixel operation, not merely a prompt hint.
    """
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "mask_rel": ("STRING", {"default": "", "multiline": False}),
                "mode": (["FOCUS", "INVERT"], {"default": "FOCUS"}),
                "outside_level": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01}),
            }
        }
    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("image", "mask")
    FUNCTION = "apply"
    CATEGORY = CATEGORY

    def apply(self, image, mask_rel, mode, outside_level):
        import torch
        if not hasattr(image, "shape") or len(image.shape) != 4:
            raise ValueError("UniversalMentionApplyMask expects IMAGE tensor [B,H,W,C]")
        b, h, w, _ = image.shape
        mask = _load_mask_tensor(str(mask_rel), int(w), int(h)).to(device=image.device, dtype=image.dtype)
        if str(mode or "FOCUS").upper() == "INVERT":
            mask = 1.0 - mask
        if int(mask.shape[0]) == 1 and int(b) > 1:
            mask = mask.repeat(int(b), 1, 1)
        m = mask.unsqueeze(-1).clamp(0.0, 1.0)
        outside = torch.full_like(image, float(max(0.0, min(1.0, outside_level))))
        out = image * m + outside * (1.0 - m)
        return out, mask


class UniversalMentionLoadOne:
    """Internal/runtime-safe image loader used by the global auto-bind engine."""
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"library_rel": ("STRING", {"default": "", "multiline": False})}}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "load"
    CATEGORY = CATEGORY

    def load(self, library_rel):
        return (_load_library_image(str(library_rel or "")),)

    @classmethod
    def IS_CHANGED(cls, library_rel):
        try:
            path = _safe_library_path(str(library_rel or ""))
            st = path.stat()
            return hashlib.sha256(repr((str(path), st.st_mtime_ns, st.st_size)).encode()).hexdigest()
        except Exception as exc:
            return f"missing:{type(exc).__name__}:{exc}"


class UniversalMentionTextLiteral:
    """Internal text literal so one @ prompt can be adapted independently per target model."""
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"text": ("STRING", {"default": "", "multiline": True})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "emit"
    CATEGORY = CATEGORY

    def emit(self, text):
        return (str(text or ""),)


class UniversalMentionSemanticPreview:
    """User-visible debugger for library mentions and connected @1/@2 slots."""
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"default": "@1 把衣服换成 @2 的衣服款式。", "multiline": True}),
                "target_profile": (["AUTO_GENERIC", "MINIMAX_H3", "KREA2_PICTURE", "LTX_IMAGE_PROMPT"], {"default": "MINIMAX_H3"}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("semantic_prompt", "binding_json")
    FUNCTION = "preview"
    CATEGORY = CATEGORY

    def preview(self, prompt, target_profile):
        profile = str(target_profile)
        if profile == "MINIMAX_H3":
            adapter = {
                "name": "MINIMAX_H3_NATIVE", "max_refs": 9,
                "slots": [f"ref_images.ref_image_{i}" for i in range(9)],
                "tag_template": "<Picture {i}>", "append_bindings": True, "native_vision": True,
            }
        elif profile == "KREA2_PICTURE":
            adapter = {
                "name": "KREA2_PREVIEW", "max_refs": 12,
                "slots": [f"image_{i}" for i in range(1, 13)],
                "tag_template": "Picture {i}", "append_bindings": True, "native_vision": True,
            }
        elif profile == "LTX_IMAGE_PROMPT":
            adapter = {
                "name": "LTX_PREVIEW", "max_refs": 1,
                "slots": ["image_prompt"],
                "tag_template": "the connected reference image", "append_bindings": True, "native_vision": True,
            }
        else:
            adapter = {
                "name": "GENERIC_PREVIEW", "max_refs": MAX_OUTPUT_REFS,
                "slots": [f"image_{i}" for i in range(1, MAX_OUTPUT_REFS + 1)],
                "tag_template": "Reference Image {i}", "append_bindings": True,
            }

        # Pretend only numerically-mentioned slots are connected. Named
        # @library images are then free to occupy the remaining slots.
        fake_inputs: Dict[str, Any] = {}
        for m in _MENTION_RE.finditer(str(prompt or "")):
            token = _mention_token(m)
            n = _slot_alias_number(token)
            if n is not None and 1 <= n <= len(adapter["slots"]):
                fake_inputs[str(adapter["slots"][n - 1])] = [f"preview_image_{n}", 0]
        fake_graph = {"preview": {"class_type": "Preview", "inputs": fake_inputs}}
        refs, occurrences = _resolve_refs_for_target(
            str(prompt or ""), "preview", adapter, fake_graph, strict_missing=True
        )
        _enrich_refs_with_vision(refs, adapter)
        semantic = _render_target_semantic_prompt(str(prompt or ""), refs, occurrences, adapter)
        return semantic, json.dumps({"references": refs, "relationship_graph": _relationship_graph(str(prompt or ""), refs, occurrences), "semantic_prompt": semantic}, ensure_ascii=False, indent=2)



class UniversalMentionVisionAnalyze:
    """Analyze one image from the dedicated @library and cache its visual semantics."""
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image_name": ("STRING", {"default": "衣服", "multiline": False}),
                "instruction_context": ("STRING", {"default": "只参考这张图的衣服款式和材质", "multiline": True}),
                "force_refresh": ("BOOLEAN", {"default": False}),
            }
        }
    RETURN_TYPES=("STRING", "STRING")
    RETURN_NAMES=("vision_summary", "vision_json")
    FUNCTION="analyze"
    CATEGORY=CATEGORY
    def analyze(self, image_name, instruction_context, force_refresh):
        index = _build_index(True)
        found = _resolve_token(str(image_name or "").lstrip("@"), index)
        path = _safe_library_path(str(found["rel"]))
        result = _analyze_image_semantics(path, str(instruction_context or ""), bool(force_refresh))
        summary = str(result.get("summary") or "")
        if not summary:
            summary = json.dumps(result.get("basic") or {}, ensure_ascii=False)
        return summary, json.dumps({"file": found["rel"], "vision": result}, ensure_ascii=False, indent=2)


class UniversalMentionVisionStatus:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES=("STRING",)
    RETURN_NAMES=("vision_status_json",)
    FUNCTION="status"
    CATEGORY=CATEGORY
    def status(self):
        payload = {
            "plugin": PLUGIN_NAME, "version": PLUGIN_VERSION,
            "mode": _vision_mode(), "endpoint": _vision_endpoint(),
            "model": _vision_model(),
            "rich_semantic_ready": bool(_vision_endpoint() and _vision_model()),
            "required": _vision_required(),
            "cache_path": str(_vision_cache_path()),
            "cache_entries": len(_read_vision_cache()),
        }
        return (json.dumps(payload, ensure_ascii=False, indent=2),)


class UniversalMentionV3Status:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES=("STRING",)
    RETURN_NAMES=("status_json",)
    FUNCTION="status"
    CATEGORY=CATEGORY
    def status(self):
        return (json.dumps(_LAST_BIND_REPORT, ensure_ascii=False, indent=2),)

def _register_global_autobind() -> None:
    if _PromptServer is None:
        return
    try:
        server = _PromptServer.instance
        marker = "_uim_global_autobind_registered"
        if getattr(server, marker, False):
            return
        server.add_on_prompt_handler(_global_autobind_on_prompt)
        setattr(server, marker, True)
    except Exception:
        # Fail-soft: manual Router mode remains fully functional.
        return

NODE_CLASS_MAPPINGS = {
    "UniversalMentionScaleToTotalPixels": UniversalMentionScaleToTotalPixels,
    "UniversalMentionApplyMask": UniversalMentionApplyMask,
    "UniversalMentionLoadOne": UniversalMentionLoadOne,
    "UniversalMentionTextLiteral": UniversalMentionTextLiteral,
    "UniversalMentionSemanticPreview": UniversalMentionSemanticPreview,
    "UniversalMentionVisionAnalyze": UniversalMentionVisionAnalyze,
    "UniversalMentionVisionStatus": UniversalMentionVisionStatus,
    "UniversalMentionV3Status": UniversalMentionV3Status,
    "UniversalAtImageRouter16": UniversalAtImageRouter16,
    "UniversalMentionImageByIndex": UniversalMentionImageByIndex,
    "UniversalMentionImageByAlias": UniversalMentionImageByAlias,
    "UniversalMentionPromptAdapter": UniversalMentionPromptAdapter,
    "UniversalMentionLibraryInfo": UniversalMentionLibraryInfo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "UniversalMentionScaleToTotalPixels": "Universal @Image｜内部稳定缩放器",
    "UniversalMentionApplyMask": "Universal @Image｜V4 参考区域 Mask",
    "UniversalMentionLoadOne": "Universal @Image｜内部加载器",
    "UniversalMentionTextLiteral": "Universal @Image｜内部文本适配",
    "UniversalMentionSemanticPreview": "Universal @Image｜语义绑定预览",
    "UniversalMentionVisionAnalyze": "Universal @Image｜V4 Vision 读图分析",
    "UniversalMentionVisionStatus": "Universal @Image｜V4 Vision 状态",
    "UniversalMentionV3Status": "Universal @Image｜V4 最后绑定状态",
    "UniversalAtImageRouter16": "Universal @Image Router｜通用@图片｜16图",
    "UniversalMentionImageByIndex": "Universal @Image｜按编号取图",
    "UniversalMentionImageByAlias": "Universal @Image｜按名称取图",
    "UniversalMentionPromptAdapter": "Universal @Image｜提示词标签适配器",
    "UniversalMentionLibraryInfo": "Universal @Image Library｜图库信息",
}


_register_global_autobind()
