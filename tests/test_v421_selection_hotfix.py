from pathlib import Path

root = Path(__file__).resolve().parents[1]
mention = (root / "web" / "image_mentions.js").read_text(encoding="utf-8")
v4 = (root / "web" / "v4_multimodal.js").read_text(encoding="utf-8")

# The popup ancestor must NOT kill pointerdown during capture; otherwise the
# target candidate button never receives pointerdown.
assert 'popup.addEventListener("pointerdown", (e) => e.stopPropagation(), false);' in mention
assert 'popup.addEventListener("pointerdown", (e) => e.stopPropagation(), true);' not in mention

# Keyboard confirmation must run before ComfyUI document/canvas handlers and
# must be able to use the active prompt even if DOM focus moves transiently.
assert 'window.addEventListener("keydown", handleGlobalKeydown, true);' in mention
assert 'const el = popupOpen' in mention
assert 'consumePopupKey(e);' in mention

# V4 control menu had the same ancestor capture trap.
assert 'controls.addEventListener("pointerdown",e=>e.stopPropagation(),false);' in v4
assert 'controls.addEventListener("pointerdown",e=>e.stopPropagation(),true);' not in v4

print("PASS: v4.2.1 pointer + keyboard selection hotfix")
