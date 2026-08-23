"""Standalone parser smoke tests.
Run from the plugin parent folder with: python tests/test_parser_standalone.py
No ComfyUI runtime required for regex/replacement checks.
"""
from pathlib import Path
import importlib.util

MODULE = Path(__file__).resolve().parents[1] / "universal_image_mentions.py"
spec = importlib.util.spec_from_file_location("uim", MODULE)
uim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uim)


def mentions(text):
    return [m.group("brace") or m.group("quote") or m.group("plain") for m in uim._MENTION_RE.finditer(text)]

assert mentions("@图1 衣服变黑") == ["图1"]
assert mentions("@图1.png 衣服变黑") == ["图1.png"]
assert mentions('@"人物 正面" 只锁脸') == ["人物 正面"]
assert mentions("@{人物 正面} 只锁脸") == ["人物 正面"]
assert uim._replacement("H3_PICTURE", "", 2, "x", "x.png", "@x") == "<Picture 2>"
assert uim._replacement("GENERIC_REF", "", 3, "x", "x.png", "@x") == "<Reference 3>"
assert uim._replacement("CUSTOM", "[IMG_{i}:{stem}]", 4, "x", "abc.png", "@x") == "[IMG_4:abc]"
print("PASS: standalone parser smoke tests")
