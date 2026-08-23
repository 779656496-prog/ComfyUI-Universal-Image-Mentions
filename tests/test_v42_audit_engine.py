import importlib.util, os, tempfile
from pathlib import Path
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('uim_v42', ROOT/'universal_image_mentions.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

with tempfile.TemporaryDirectory() as td:
    os.environ['UIM_LIBRARY_DIR']=td
    os.environ['UIM_VISION_MODE']='VLM'
    os.environ['UIM_VISION_URL']='http://127.0.0.1:9999/v1'
    os.environ['UIM_VISION_MODEL']='fake-vision'
    root=Path(td)
    Image.new('RGB',(32,32),'gray').save(root/'person.png')
    Image.new('RGB',(32,32),'black').save(root/'cloth.png')
    output=root/'generated.png'; Image.new('RGB',(32,32),'navy').save(output)
    m._write_v4_config({'audit_threshold':0.78,'audit_min_confidence':0.55,'audit_critical_floor':0.58,'audit_max_retries':2})

    # Keep the test offline: audit orchestration is tested while the VLM comparator is deterministic.
    old_cmp=m._cached_vision_compare
    old_an=m._analyze_image_semantics
    phase={'n':1}
    def fake_cmp(gen, ref, attr, mode='TRANSFER'):
        attr=str(attr).upper()
        if attr=='CLOTHING':
            neck=0.42 if phase['n']==1 else 0.92
            return {
                'score': 0.10,  # intentionally misleading raw aggregate; reliable dimensions must drive the decision
                'confidence':0.93,
                'dimensions':{
                    'garment_category':{'score':0.96,'confidence':0.95,'correction':''},
                    'color':{'score':0.93,'confidence':0.95,'correction':''},
                    'silhouette_fit':{'score':0.90,'confidence':0.90,'correction':''},
                    'neckline':{'score':neck,'confidence':0.94,'missing':'neckline shape differs','correction':'match the reference neckline exactly'},
                    'sleeves':{'score':0.90,'confidence':0.90,'correction':''},
                    'material_texture':{'score':0.88,'confidence':0.86,'correction':''},
                    # Deliberately bad but low-confidence: must be INCONCLUSIVE for this dimension, not a failure trigger.
                    'pattern_details':{'score':0.10,'confidence':0.20,'missing':'not visible','correction':'do not guess'},
                }
            }
        if attr=='IDENTITY':
            return {'score':0.95,'confidence':0.94,'dimensions':{
                'face_identity':{'score':0.96,'confidence':0.96},
                'hair':{'score':0.94,'confidence':0.90},
                'body_identity':{'score':0.95,'confidence':0.90},
                'skin_tone_visible':{'score':0.94,'confidence':0.88},
            }}
        return {'score':0.9,'confidence':0.9,'dimensions':{}}
    m._cached_vision_compare=fake_cmp
    m._analyze_image_semantics=lambda *a,**k:{'semantic_available':True,'summary':'generated','confidence':{'summary':0.95}}
    try:
        report1={
            'run':{'run_id':'run_a','root_run_id':'root_a','retry_index':0},
            'reports':[{
                # Important regression: V3/V4 relationship parser emits `relations`, not `transfers`.
                'relationship_graph':{'relations':[{'action':'TRANSFER','source':2,'target':1,'attribute':'CLOTHING'}],'preserves':[]},
                'references':[
                    {'index':1,'source_kind':'LIBRARY','file':'person.png'},
                    {'index':2,'source_kind':'LIBRARY','file':'cloth.png'},
                ]
            }]
        }
        r1=m._audit_generated_image(output,report1)
        assert r1['ok'] and r1['engine']=='V4.2_DIMENSIONAL', r1
        assert len(r1['relations'])==2, r1  # clothing transfer + automatic identity preservation
        assert 'CLOTHING.neckline' in r1['failed_dimensions'], r1
        assert 'CLOTHING.pattern_details' not in r1['failed_dimensions'], r1
        assert r1['retry_recommended'] is True, r1
        assert 'match the reference neckline exactly' in r1['retry_prompt_suffix'], r1['retry_prompt_suffix']
        assert 'pattern_details' not in r1['retry_prompt_suffix'], r1['retry_prompt_suffix']

        phase['n']=2
        report2={**report1,'run':{'run_id':'run_b','root_run_id':'root_a','parent_run_id':'run_a','retry_index':1}}
        r2=m._audit_generated_image(output,report2)
        assert r2['previous_overall_score']==r1['overall_score'], (r1,r2)
        assert r2['score_delta'] is not None and r2['score_delta']>0, r2
        assert r2['retry_recommended'] is False, r2
        logs=m._recent_audit_log(5)
        assert len(logs)>=2 and logs[-1]['run']['run_id']=='run_b', logs

        # Vision semantic confidence: a low-confidence field must not be compiled into the final prompt.
        vision={'semantic_available':True,'clothing':'black cropped jacket','identity_features':'oval face',
                'confidence':{'clothing':0.95,'identity_features':0.20}}
        refs=[{'index':1,'roles':['GENERAL_REFERENCE'],'vision':vision}]
        lines=m._vision_semantic_lines(refs,'<Picture {i}>',{'relations':[],'assignments':{},'preserves':[]})
        joined='\n'.join(lines)
        assert 'black cropped jacket' in joined and 'oval face' not in joined, joined
    finally:
        m._cached_vision_compare=old_cmp
        m._analyze_image_semantics=old_an

print('PASS: v4.2 dimensional audit engine')
