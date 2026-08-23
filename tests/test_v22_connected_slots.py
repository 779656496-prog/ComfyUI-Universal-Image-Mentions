from pathlib import Path
import importlib.util
import os
import tempfile
from PIL import Image

MODULE = Path(__file__).resolve().parents[1] / "universal_image_mentions.py"
spec = importlib.util.spec_from_file_location("uim_v22", MODULE)
uim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uim)
os.environ["UIM_STRICT_BIND"] = "0"


def h3(prompt, connected=3):
    inputs = {"prompt": prompt}
    for i in range(connected):
        inputs[f"ref_images.ref_image_{i}"] = [str(100 + i), 0]
    graph = {"1": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": inputs}}
    for i in range(connected):
        graph[str(100 + i)] = {"class_type": "DummyImage", "inputs": {}}
    return {"prompt": graph, "extra_data": {}}


with tempfile.TemporaryDirectory() as td:
    os.environ["UIM_LIBRARY_DIR"] = td
    Image.new("RGB", (8, 8), (10, 20, 30)).save(Path(td) / "背景A.png")

    # Existing H3 refs: @1/@2/@3 must address, not overwrite, those wires.
    data = h3("@1 让她把衣服变成 @2 的衣服款式。@3 只参考动作。", 3)
    before = {k: list(v) for k, v in data["prompt"]["1"]["inputs"].items() if k.startswith("ref_images.")}
    out = uim._global_autobind_on_prompt(data)
    target = out["prompt"]["1"]["inputs"]
    for k, v in before.items():
        assert target[k] == v
    semantic = target["prompt"]
    assert "<Picture 1>" in semantic and "<Picture 2>" in semantic and "<Picture 3>" in semantic
    assert "edit target/base subject" in semantic
    assert "source reference for clothing" in semantic

    # Named library image fills first FREE slot (Picture 4 here), never Picture 1-3.
    data = h3("@1 保持人物。@背景A 换成这个背景。", 3)
    out = uim._global_autobind_on_prompt(data)
    target = out["prompt"]["1"]["inputs"]
    assert target["ref_images.ref_image_0"] == ["100", 0]
    assert target["ref_images.ref_image_1"] == ["101", 0]
    assert target["ref_images.ref_image_2"] == ["102", 0]
    assert "ref_images.ref_image_3" in target
    loader_id = target["ref_images.ref_image_3"][0]
    assert out["prompt"][loader_id]["inputs"]["library_rel"] == "背景A.png"
    assert "<Picture 4>" in target["prompt"]
    # Independent sentences must not invent a 1->4 transfer relationship.
    assert "Cross-reference relationships" not in target["prompt"]

    # @2 must error when slot 2 is not connected; it must not silently mean library file 2.
    data = h3("@2 保持身份。", 1)
    out = uim._global_autobind_on_prompt(data)
    report = out["extra_data"]["uim_autobind"]["reports"][0]
    assert report["status"] == "resolve_error"
    assert "not connected" in report["error"]

    # Preview supports numeric connected-slot relationships without needing files named 1.png/2.png.
    preview = uim.UniversalMentionSemanticPreview()
    semantic, manifest = preview.preview("@1 把衣服换成 @2 的衣服款式。", "MINIMAX_H3")
    assert "<Picture 1>" in semantic and "<Picture 2>" in semantic
    assert "edit target/base subject" in semantic

print("PASS: v2.2 connected-slot + relationship tests")
