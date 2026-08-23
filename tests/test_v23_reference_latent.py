import os, sys, json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ['UIM_LIBRARY_DIR']='/mnt/data/uim_test_library'
import universal_image_mentions as uim

# Flattened execution-style graph equivalent to a Flux2/Klein subgraph:
# CLIPTextEncode -> positive RefLatent -> guider
#               -> zero-out -> negative RefLatent -> guider
# One already-connected image latent is shared by both ref branches.
g={
 '1': {'class_type':'CLIPTextEncode','inputs':{'text':'@1 作为基础人物，把 @1 的衣服换成 @衣服 的衣服款式。','clip':['20',0]}},
 '2': {'class_type':'ConditioningZeroOut','inputs':{'conditioning':['1',0]}},
 '3': {'class_type':'LoadImage','inputs':{'image':'base_person.jpg'}},
 '4': {'class_type':'ImageScaleToTotalPixels','inputs':{'image':['3',0],'upscale_method':'lanczos','megapixels':1}},
 '5': {'class_type':'VAELoader','inputs':{'vae_name':'flux2-vae.safetensors'}},
 '6': {'class_type':'VAEEncode','inputs':{'pixels':['4',0],'vae':['5',0]}},
 '7': {'class_type':'ReferenceLatent','inputs':{'conditioning':['1',0],'latent':['6',0]}},
 '8': {'class_type':'ReferenceLatent','inputs':{'conditioning':['2',0],'latent':['6',0]}},
 '9': {'class_type':'CFGGuider','inputs':{'positive':['7',0],'negative':['8',0],'model':['30',0]}},
}
prof=uim._reference_latent_profile(g,'1')
assert prof and prof['existing_count']==1, prof
assert set(prof['terminal_nodes'])=={'7','8'}, prof
refs,occ=uim._resolve_refs_for_reference_chain(g['1']['inputs']['text'],prof,True)
assert [(r['index'],r['source_kind'],r.get('file')) for r in refs]==[(1,'CONNECTED_SLOT',None),(2,'LIBRARY','衣服.png')], refs
sem=uim._render_target_semantic_prompt(g['1']['inputs']['text'],refs,occ,{'tag_template':'Reference Image {i}','append_bindings':True})
assert 'Reference Image 1' in sem and 'Reference Image 2' in sem
counter=uim._initial_alloc_counter(g)
injected=uim._inject_reference_latent_library_refs(g,prof,refs,counter)
assert len(injected)==1, injected
scale_nodes=[n for n in g.values() if isinstance(n,dict) and n.get('class_type')=='UniversalMentionScaleToTotalPixels']
assert len(scale_nodes)==1, scale_nodes
assert scale_nodes[0]['inputs'].get('resolution_steps')==1, scale_nodes[0]
assert len(injected[0]['reference_latent_nodes'])==2, injected
new_refs=set(injected[0]['reference_latent_nodes'])
assert g['9']['inputs']['positive'][0] in new_refs
assert g['9']['inputs']['negative'][0] in new_refs
# filename alias of connected image should address @1
refs2,occ2=uim._resolve_refs_for_reference_chain('@base_person 把衣服换成 @衣服 的款式',prof,True)
assert refs2[0]['index']==1 and refs2[0]['source_kind']=='CONNECTED_SLOT', refs2

payload={'prompt':{
 '1': {'class_type':'CLIPTextEncode','inputs':{'text':'@1 作为基础人物，把 @1 的衣服换成 @衣服 的衣服款式。','clip':['20',0]}},
 '2': {'class_type':'ConditioningZeroOut','inputs':{'conditioning':['1',0]}},
 '3': {'class_type':'LoadImage','inputs':{'image':'base_person.jpg'}},
 '4': {'class_type':'ImageScaleToTotalPixels','inputs':{'image':['3',0],'upscale_method':'lanczos','megapixels':1}},
 '5': {'class_type':'VAELoader','inputs':{'vae_name':'flux2-vae.safetensors'}},
 '6': {'class_type':'VAEEncode','inputs':{'pixels':['4',0],'vae':['5',0]}},
 '7': {'class_type':'ReferenceLatent','inputs':{'conditioning':['1',0],'latent':['6',0]}},
 '8': {'class_type':'ReferenceLatent','inputs':{'conditioning':['2',0],'latent':['6',0]}},
 '9': {'class_type':'CFGGuider','inputs':{'positive':['7',0],'negative':['8',0],'model':['30',0]}},
}, 'extra_data':{}}
out=uim._global_autobind_on_prompt(payload)
report=out['extra_data']['uim_autobind']['reports'][0]
assert report['adapter']=='REFERENCE_LATENT_CHAIN' and report['status']=='bound', report
assert 'Reference Image 2' in out['prompt']['1']['inputs']['text']
print('v2.3 ReferenceLatent test PASS')
print(json.dumps(report,ensure_ascii=False,indent=2)[:2500])
