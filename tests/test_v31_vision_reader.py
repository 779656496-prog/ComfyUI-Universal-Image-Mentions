import importlib.util, os, tempfile
from pathlib import Path

spec=importlib.util.spec_from_file_location('uim','universal_image_mentions.py')
uim=importlib.util.module_from_spec(spec); spec.loader.exec_module(uim)

from PIL import Image

with tempfile.TemporaryDirectory() as td:
    lib=Path(td)
    os.environ['UIM_LIBRARY_DIR']=str(lib)
    os.environ['UIM_VISION_MODE']='BASIC'
    img=Image.new('RGB',(320,640),(15,20,25))
    img.save(lib/'人物.png')
    out=uim._analyze_image_semantics(lib/'人物.png','保持人物身份',force=True)
    assert out['basic']['width']==320 and out['basic']['height']==640, out
    assert out['basic']['orientation']=='portrait', out
    assert out['semantic_available'] is False, out

    # Fake an OpenAI-compatible VLM response to verify role-focused compilation
    os.environ['UIM_VISION_MODE']='VLM'
    os.environ['UIM_VISION_URL']='http://127.0.0.1:9999/v1'
    os.environ['UIM_VISION_MODEL']='fake-vision'
    old=uim._call_vision_vlm
    uim._call_vision_vlm=lambda path, context='': {
        'provider':'OPENAI_COMPAT_VLM','model':'fake-vision','summary':'a woman in a black jacket',
        'identity_features':'black hair, oval face','clothing':'black cropped jacket, long sleeves',
        'pose_motion':'standing','scene':'studio','style_lighting':'soft light','product_object':'',
        'colors':['black'],'visible_text':'','usable_roles':['IDENTITY','CLOTHING'],'confidence_notes':''
    }
    try:
        rich=uim._analyze_image_semantics(lib/'人物.png','只参考衣服',force=True)
    finally:
        uim._call_vision_vlm=old
    assert rich['semantic_available'] is True, rich
    refs=[
      {'index':1,'alias':'1','source_kind':'CONNECTED_SLOT','file':None,'roles':['IDENTITY'],'vision':rich},
      {'index':2,'alias':'衣服','source_kind':'LIBRARY','file':'人物.png','roles':['CLOTHING'],'vision':rich},
    ]
    graph={'relations':[{'action':'TRANSFER','source':2,'target':1,'attribute':'CLOTHING'}],'assignments':{},'preserves':[]}
    lines=uim._vision_semantic_lines(refs,'<Picture {i}>',graph)
    joined='\n'.join(lines)
    assert '<Picture 2>' in joined and 'black cropped jacket' in joined, joined
    # Clothing source should not inject its identity description when relation scope is clothing-only.
    pic2=[x for x in lines if '<Picture 2>' in x][0]
    assert 'oval face' not in pic2, pic2

print('PASS: v3.1 vision reader + role-focused grounding')
