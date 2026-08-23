import os, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ['UIM_LIBRARY_DIR']='/mnt/data/uim_test_library'
import universal_image_mentions as uim

# Simulate an older execution graph/template where the core scaler does NOT
# contain the newly-added resolution_steps API input.
g={
 '1': {'class_type':'CLIPTextEncode','inputs':{'text':'@1把衣服换成@衣服 的衣服','clip':['20',0]}},
 '2': {'class_type':'LoadImage','inputs':{'image':'base_person.jpg'}},
 '3': {'class_type':'ImageScaleToTotalPixels','inputs':{'image':['2',0],'upscale_method':'lanczos','megapixels':1.0}},
 '4': {'class_type':'VAELoader','inputs':{'vae_name':'flux2-vae.safetensors'}},
 '5': {'class_type':'VAEEncode','inputs':{'pixels':['3',0],'vae':['4',0]}},
 '6': {'class_type':'ReferenceLatent','inputs':{'conditioning':['1',0],'latent':['5',0]}},
 '7': {'class_type':'CFGGuider','inputs':{'positive':['6',0],'negative':['6',0],'model':['30',0]}},
}
prof=uim._reference_latent_profile(g,'1')
assert prof is not None, prof
assert prof['scale_template']['resolution_steps']==1, prof
refs,occ=uim._resolve_refs_for_reference_chain(g['1']['inputs']['text'],prof,True)
counter=uim._initial_alloc_counter(g)
injected=uim._inject_reference_latent_library_refs(g,prof,refs,counter)
assert injected, injected
scalers=[(nid,node) for nid,node in g.items() if isinstance(node,dict) and node.get('class_type')=='UniversalMentionScaleToTotalPixels']
assert len(scalers)==1, scalers
inputs=scalers[0][1]['inputs']
assert inputs['resolution_steps']==1, inputs
assert 'image' in inputs and 'upscale_method' in inputs and 'megapixels' in inputs
# Critical guard: UIM must no longer inject a new core ImageScaleToTotalPixels node.
core_scalers=[nid for nid,node in g.items() if isinstance(node,dict) and node.get('class_type')=='ImageScaleToTotalPixels']
assert core_scalers==['3'], core_scalers
print('PASS: v4.2.3 core scaler schema compatibility')

legacy={
 '1': {'class_type':'CLIPTextEncode','inputs':{'text':'@1 测试','clip':['20',0]}},
 '2': {'class_type':'ImageScaleToTotalPixels','inputs':{'image':['99',0],'upscale_method':'lanczos','megapixels':1.0}},
}
patched=uim._patch_core_scale_resolution_steps(legacy)
assert patched==['2'], patched
assert legacy['2']['inputs']['resolution_steps']==1
print('PASS: legacy ImageScaleToTotalPixels prompt repair')
