import importlib.util, os, tempfile
from pathlib import Path
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('uim', ROOT/'universal_image_mentions.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

with tempfile.TemporaryDirectory() as td:
    os.environ['UIM_LIBRARY_DIR']=td
    root=Path(td)
    Image.new('RGB',(12,12),'red').save(root/'旧名字.png')
    idx1=m._build_index(True)
    assert len(idx1)==1 and idx1[0]['uim_id'].startswith('uim_')
    stable=idx1[0]['uim_id']
    (root/'旧名字.png').rename(root/'新名字.png')
    idx2=m._build_index(True)
    assert idx2[0]['uim_id']==stable
    resolved=m._resolve_token('旧名字',idx2)
    assert resolved['rel']=='新名字.png', resolved

    (root/'.uim/masks').mkdir(parents=True,exist_ok=True)
    Image.new('L',(8,8),255).save(root/'.uim/masks/m.png')
    ui={'inputs':[{'name':'image_a','type':'IMAGE'},{'name':'image_a_strength','type':'FLOAT'},{'name':'image_a_mask','type':'MASK'}]}
    adapter=m._validate_adapter_for_ui({
        'name':'USER_TEST','slots':['image_a'],'strength_map':{'image_a':'image_a_strength'},
        'mask_map':{'image_a':'image_a_mask'},'user_manifest':True,
        'capabilities':{'supports_reference_image':True}
    },ui)
    assert not adapter['validation_errors'], adapter
    refs=[{'index':1,'slot':'image_a','alias':'1','source_kind':'CONNECTED_SLOT','roles':[],
           'v4':{'role':'AUTO','strength':1.4,'mask_rel':'.uim/masks/m.png','mask_mode':'FOCUS','order':0}}]
    m._prepare_control_modes(refs,adapter)
    assert refs[0]['v4']['strength_effective']=='NATIVE'
    assert refs[0]['v4']['mask_effective']=='NATIVE'
    graph={'10':{'class_type':'Any','inputs':{'image_a':['1',0]}},'1':{'class_type':'LoadImage','inputs':{'image':'x.png'}}}
    counter=m._initial_alloc_counter(graph)
    masks=m._apply_native_masks(graph,'10',refs,adapter,counter)
    assert masks and graph['10']['inputs']['image_a']==['1',0]
    mask_conn=graph['10']['inputs']['image_a_mask']
    assert graph[str(mask_conn[0])]['class_type']=='UniversalMentionApplyMask' and mask_conn[1]==1

    bad=m._validate_adapter_for_ui({'name':'GENERIC_BAD','slots':['not_real']},{'inputs':[{'name':'image','type':'IMAGE'}]})
    assert bad['validation_errors']

    m._write_v4_config({'audit_max_retries':1})
    g={'1':{'class_type':'X','inputs':{'text':'hello [UIM-RUN id=abcdefgh root=abcdefgh parent=- retry=1] @1'}}}
    meta=m._extract_run_meta_and_strip(g)
    assert meta['run_id']=='abcdefgh' and meta['retry_index']==1
    assert '[UIM-RUN' not in g['1']['inputs']['text']
    g2={'1':{'class_type':'X','inputs':{'text':'[UIM-RUN id=abcdefgh root=abcdefgh parent=abcdefgh retry=2] @1'}}}
    try:
        m._extract_run_meta_and_strip(g2)
        raise AssertionError('retry guard should block')
    except ValueError as e:
        assert 'retry guard' in str(e).lower()

print('PASS: v4.1 reliability')
