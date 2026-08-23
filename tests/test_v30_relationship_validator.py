from pathlib import Path
import importlib.util, os, tempfile
from PIL import Image

MODULE = Path(__file__).resolve().parents[1] / "universal_image_mentions.py"
spec = importlib.util.spec_from_file_location("uim_v30", MODULE)
uim = importlib.util.module_from_spec(spec); spec.loader.exec_module(uim)

with tempfile.TemporaryDirectory() as td:
    os.environ["UIM_LIBRARY_DIR"] = td
    Image.new("RGB", (8,8), (20,20,20)).save(Path(td)/"衣服.png")

    # Direction: @1 target, @2 source.
    fake={"1":{"class_type":"MiniMaxH3ReferenceToVideo","inputs":{"prompt":"@1 让她把衣服变成 @2 的衣服款式。","ref_images.ref_image_0":["10",0],"ref_images.ref_image_1":["11",0]}},"10":{"class_type":"DummyImage","inputs":{}},"11":{"class_type":"DummyImage","inputs":{}}}
    os.environ["UIM_STRICT_BIND"]="1"
    out=uim._global_autobind_on_prompt({"prompt":fake,"extra_data":{}})
    sem=out["prompt"]["1"]["inputs"]["prompt"]
    assert "edit target/base subject" in sem and "source reference for clothing" in sem
    graph=out["extra_data"]["uim_autobind"]["reports"][0]["relationship_graph"]
    assert any(r["source"]==2 and r["target"]==1 and r["attribute"]=="CLOTHING" for r in graph["relations"])

    # Reverse phrasing: 把@2的衣服给@1穿 -> source 2 -> target 1.
    prompt="把@2的衣服给@1穿，保持@1的脸和身份。"
    refs=[{"index":1},{"index":2}]
    occ=[]
    for m in uim._MENTION_RE.finditer(prompt):
        tok=uim._mention_token(m)
        occ.append((m.start(),m.end(),int(tok)))
    g=uim._relationship_graph(prompt, refs, occ)
    assert any(r["source"]==2 and r["target"]==1 and r["attribute"]=="CLOTHING" for r in g["relations"])


    # Numeric slot mentions must parse even when Chinese text is attached directly.
    compact="@1让她把衣服换成@2的衣服款式"
    assert [uim._mention_token(m) for m in uim._MENTION_RE.finditer(compact)] == ["1","2"]

    # Strict validator blocks a real @library image in a text-only execution path.
    bad={"prompt":{"20":{"class_type":"SomeTextOnlyNode","inputs":{"prompt":"@衣服 换衣服"}}},"extra_data":{"extra_pnginfo":{"workflow":{"nodes":[{"id":20,"type":"SomeTextOnlyNode","inputs":[{"name":"prompt","type":"STRING","link":None}]}],"links":[]}}}}
    blocked=False
    try:
        uim._global_autobind_on_prompt(bad)
    except ValueError as e:
        blocked="Bind Validator blocked Queue" in str(e)
    assert blocked

print("PASS: v3 relationship graph + strict bind validator")
