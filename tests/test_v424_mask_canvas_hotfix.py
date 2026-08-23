from pathlib import Path

root=Path(__file__).resolve().parents[1]
js=(root/'web'/'v4_multimodal.js').read_text(encoding='utf-8')
py=(root/'universal_image_mentions.py').read_text(encoding='utf-8')
assert 'const maskCanvas=document.createElement("canvas")' in js
assert 'viewCtx.clearRect(0,0,canvas.width,canvas.height)' in js
assert 'maskCanvas.toDataURL("image/png")' in js
assert '/uim/mask/file?rel=' in js
assert '@routes.get("/uim/mask/file")' in py
assert 'PLUGIN_VERSION = "4.2.4"' in py
print('PASS v4.2.4 mask canvas hotfix')
