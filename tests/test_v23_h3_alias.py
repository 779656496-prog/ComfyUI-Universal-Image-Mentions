import os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ['UIM_LIBRARY_DIR']='/mnt/data/uim_test_library'
import universal_image_mentions as uim

g={
 '10': {'class_type':'LoadImage','inputs':{'image':'人物正面.jpg'}},
 '11': {'class_type':'LoadImage','inputs':{'image':'衣服来源.png'}},
 '20': {'class_type':'MiniMaxH3ReferenceToVideo','inputs':{
   'prompt':'@人物正面 保持脸，把 @1 的衣服换成 @2 的衣服款式',
   'ref_images.ref_image_0':['10',0],
   'ref_images.ref_image_1':['11',0],
 }}
}
ad=uim._adapter_for_target('MiniMaxH3ReferenceToVideo',None)
refs,occ=uim._resolve_refs_for_target(g['20']['inputs']['prompt'],'20',ad,g,True)
assert [r['index'] for r in refs]==[1,2],refs
assert refs[0]['source_kind']=='CONNECTED_SLOT' and refs[1]['source_kind']=='CONNECTED_SLOT'
sem=uim._render_target_semantic_prompt(g['20']['inputs']['prompt'],refs,occ,ad)
assert '<Picture 1>' in sem and '<Picture 2>' in sem
print('PASS: H3 connected filename alias + numeric slots')
