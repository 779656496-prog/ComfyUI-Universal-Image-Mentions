import importlib.util, os, tempfile
from pathlib import Path
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('uim',ROOT/'universal_image_mentions.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

with tempfile.TemporaryDirectory() as td:
    os.environ['UIM_LIBRARY_DIR']=td
    root=Path(td)
    Image.new('RGB',(20,20),'red').save(root/'衣服.png')
    (root/'.uim/masks').mkdir(parents=True)
    Image.new('L',(8,8),255).save(root/'.uim/masks/m.png')
    idx=m._build_index(True)
    assert [x['rel'] for x in idx]==['衣服.png'], idx

    ui={'properties':{'uim_v4':{'mentions':{
        'slot:1':{'role':'IDENTITY','strength':1.15},
        'name:衣服':{'role':'CLOTHING','strength':1.7,'mask_rel':'.uim/masks/m.png','order':1}
    }}}}
    refs=[
        {'index':1,'slot':'ref_images.ref_image_0','source_kind':'CONNECTED_SLOT','alias':'1','roles':[]},
        {'index':2,'slot':'ref_images.ref_image_1','source_kind':'LIBRARY','file':'衣服.png','alias':'衣服','roles':[]},
    ]
    m._merge_v4_ref_controls(refs,ui,ui)
    assert refs[0]['v4']['role']=='IDENTITY'
    assert refs[1]['v4']['role']=='CLOTHING' and abs(refs[1]['v4']['strength']-1.7)<1e-6
    assert refs[1]['v4']['mask_rel'].endswith('m.png')
    lines=m._v4_control_lines(refs,'<Picture {i}>')
    assert any('clothing/garment only' in x and '1.70' in x for x in lines), lines

    graph={'10':{'class_type':'MiniMaxH3ReferenceToVideo','inputs':{'ref_images.ref_image_0':['1',0]}}}
    adapter={'slots':['ref_images.ref_image_0','ref_images.ref_image_1','ref_images.ref_image_2']}
    occ=[(0,2,1),(5,8,2)]
    occ2=m._apply_v4_library_order_for_target(refs,occ,'10',adapter,graph)
    assert refs[1]['slot']=='ref_images.ref_image_1'
    counter=m._initial_alloc_counter(graph)
    used=m._inject_library_refs_by_assigned_slot(graph,'10',refs,counter)
    assert 'ref_images.ref_image_1' in used
    conn=graph['10']['inputs']['ref_images.ref_image_1']
    assert graph[str(conn[0])]['class_type']=='UniversalMentionApplyMask', graph

    m._write_adapter_manifests({'AlienVisionNode':{'slots':['image_a','image_b'],'tag_template':'Pic {i}','prompt_input':'prompt','native_vision':True}})
    a=m._user_adapter_for_class('AlienVisionNode')
    assert a and a['slots']==['image_a','image_b'] and a['prompt_input']=='prompt'

print('PASS: v4 multimodal controls')
