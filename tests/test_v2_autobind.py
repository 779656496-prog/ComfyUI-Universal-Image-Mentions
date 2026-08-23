from pathlib import Path
import importlib.util
import json
import os
import tempfile
from PIL import Image

MODULE = Path(__file__).resolve().parents[1] / "universal_image_mentions.py"
spec = importlib.util.spec_from_file_location("uim_v2", MODULE)
uim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uim)
os.environ["UIM_STRICT_BIND"] = "0"


def mkimg(root, name, rgb):
    Image.new("RGB", (12, 10), rgb).save(Path(root) / f"{name}.png")


def workflow_wrap(prompt, nodes, links):
    return {
        "prompt": prompt,
        "extra_data": {"extra_pnginfo": {"workflow": {"nodes": nodes, "links": links}}},
    }


with tempfile.TemporaryDirectory() as td:
    os.environ["UIM_LIBRARY_DIR"] = td
    for i in range(1, 10):
        mkimg(td, f"图{i}", (i * 20 % 255, i * 10 % 255, i * 5 % 255))
    mkimg(td, "人物 正面", (210, 160, 130))
    mkimg(td, "黑色西装", (12, 12, 12))

    # 1) H3: all 9 legal autogrow refs must bind exactly by mention order.
    prompt_text = " ".join([f"@图{i} 参考第{i}张。" for i in range(1, 10)])
    data = workflow_wrap(
        {
            "1": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": prompt_text}},
            "2": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {"prompt": ["1", 0], "width": 1344, "height": 768, "length": 124}},
        },
        [
            {"id": 1, "type": "PrimitiveStringMultiline", "inputs": [], "outputs": [{"name": "STRING", "type": "STRING", "links": [100]}]},
            {"id": 2, "type": "MiniMaxH3ReferenceToVideo", "inputs": [{"name": "prompt", "type": "STRING", "link": 100}]},
        ],
        [[100, 1, 0, 2, 0, "STRING"]],
    )
    out = uim._global_autobind_on_prompt(data)
    target = out["prompt"]["2"]["inputs"]
    for i in range(9):
        assert f"ref_images.ref_image_{i}" in target
    text_node = out["prompt"][target["prompt"][0]]
    semantic = text_node["inputs"]["text"]
    assert "<Picture 1>" in semantic and "<Picture 9>" in semantic
    report = out["extra_data"]["uim_autobind"]["reports"][0]
    assert report["adapter"] == "MINIMAX_H3_NATIVE"
    assert len(report["slots"]) == 9

    # 2) Repeated mention uses one slot; spaced filename syntax resolves.
    refs = uim._resolve_refs_metadata("@{人物 正面} 保持脸。 @{人物 正面} 继续保持身份。 @黑色西装 换衣服。")
    assert len(refs) == 2
    assert refs[0]["file"] == "人物 正面.png"
    assert refs[1]["file"] == "黑色西装.png"

    # 3) LTX Prompt Enhancer binds exactly one ref to image_prompt.
    data = workflow_wrap(
        {"7": {"class_type": "LTXVPromptEnhancer", "inputs": {"prompt": "@图1 让人物向前走", "max_resulting_tokens": 256}}},
        [{"id": 7, "type": "LTXVPromptEnhancer", "inputs": [{"name": "image_prompt", "type": "IMAGE", "link": None}, {"name": "prompt", "type": "STRING", "link": None}]}],
        [],
    )
    out = uim._global_autobind_on_prompt(data)
    assert "image_prompt" in out["prompt"]["7"]["inputs"]
    assert "connected reference image" in out["prompt"]["7"]["inputs"]["prompt"]

    # 4) Krea/direct reference structural binding.
    data = workflow_wrap(
        {"9": {"class_type": "KreaPromptAutoUnlockEncodeV6", "inputs": {"prompt": "@图1 保持人物。 @图2 参考服装。"}}},
        [{"id": 9, "type": "KreaPromptAutoUnlockEncodeV6", "inputs": [
            {"name": "image", "type": "IMAGE", "link": None},
            {"name": "image_b", "type": "IMAGE", "link": None},
            {"name": "prompt", "type": "STRING", "link": None},
        ]}],
        [],
    )
    out = uim._global_autobind_on_prompt(data)
    tin = out["prompt"]["9"]["inputs"]
    assert "image" in tin and "image_b" in tin
    assert "Picture 1" in tin["prompt"] and "Picture 2" in tin["prompt"]

    # 5) Text-only node remains untouched; no fake IMAGE input invented.
    raw = "@图1 这只是文本说明"
    data = workflow_wrap(
        {"20": {"class_type": "SomeTextOnlyNode", "inputs": {"prompt": raw}}},
        [{"id": 20, "type": "SomeTextOnlyNode", "inputs": [{"name": "prompt", "type": "STRING", "link": None}]}],
        [],
    )
    out = uim._global_autobind_on_prompt(data)
    assert out["prompt"]["20"]["inputs"]["prompt"] == raw
    assert out["extra_data"]["uim_autobind"]["reports"][0]["status"] == "no_compatible_image_target"

print("PASS: v2 global autobind tests")
