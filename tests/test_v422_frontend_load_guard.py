from pathlib import Path
root = Path(__file__).resolve().parents[1]
base = (root/'web'/'image_mentions.js').read_text(encoding='utf-8')
v4 = (root/'web'/'v4_multimodal.js').read_text(encoding='utf-8')
assert 'lines.join("\\n")' in v4, 'audit log newline must be escaped in JS source'
assert 'raw.startsWith("{") && raw.includes("}")' in base, 'completed braced mention must close autocomplete'
assert 'escHtml(ref.raw)' in v4, 'chip text must be escaped'
assert 'sourceIds.has(String(el.__uimNode?.id' in v4, 'audit retry should prefer bound prompt editor'
print('PASS: v4.2.2 frontend load/stability guards')
