"""Versioned contracts. CPU tests do not constitute GPU qualification."""
import hashlib
import json
from copy import deepcopy
from pathlib import Path

MUSUBI_COMMIT='f8a1b03794a49239a3539015075f5123d6c07d66'

def hf(repo,filename='',revision='main'):
    return {'repo':repo,'filename':filename,'revision':revision}
PROFILES={
 'krea2': {'raw':hf('krea/Krea-2-Raw','raw.safetensors'), 'dit':hf('krea/Krea-2-Turbo','turbo.safetensors'),
    'vae':hf('Comfy-Org/Krea-2','vae/qwen_image_vae.safetensors'),
    'text_encoder':hf('Comfy-Org/Qwen3-VL','text_encoders/qwen3vl_4b_bf16.safetensors')},
 'klein9':{'dit':hf('black-forest-labs/FLUX.2-klein-base-9B','flux-2-klein-base-9b.safetensors'),
    'vae':hf('black-forest-labs/FLUX.2-dev','ae.safetensors'),
    'text_encoder':{**hf('black-forest-labs/FLUX.2-klein-base-9B'),'subfolder':'text_encoder','pattern':'*.safetensors'}},
 'wan22-i2v': {'low':hf('Comfy-Org/Wan_2.2_ComfyUI_Repackaged','split_files/diffusion_models/wan2.2_i2v_low_noise_14B_fp16.safetensors'),
    'high':hf('Comfy-Org/Wan_2.2_ComfyUI_Repackaged','split_files/diffusion_models/wan2.2_i2v_high_noise_14B_fp16.safetensors'),
    'vae':hf('Comfy-Org/Wan_2.1_ComfyUI_repackaged','split_files/vae/wan_2.1_vae.safetensors'),
    'text_encoder':hf('Comfy-Org/Wan_2.1_ComfyUI_repackaged','split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors')},
 'wan22-t2v':{},
 'wan22-5b':{'pipeline':hf('Wan-AI/Wan2.2-TI2V-5B-Diffusers')}
}
PROFILES['wan22-t2v']=deepcopy(PROFILES['wan22-i2v'])
for k in ('low','high'):
    PROFILES['wan22-t2v'][k]['filename']=PROFILES['wan22-t2v'][k]['filename'].replace('i2v','t2v')
# BF16 encoder is suitable for both training and generation; do not assume a pre-quantized format.
for family in ('wan22-i2v','wan22-t2v'):
    PROFILES[family]['text_encoder']=hf('Wan-AI/Wan2.1-T2V-14B','models_t5_umt5-xxl-enc-bf16.pth')

def recipe(id,name,family,kind,defaults,**extra):
    return {'id':id,'name':name,'family':family,'kind':kind,'defaults':defaults,'version':'3.0.0',
        'qualification':'requires_gpu_validation','backend_commit':MUSUBI_COMMIT,
        'source': 'https://github.com/kohya-ss/musubi-tuner/tree/'+MUSUBI_COMMIT+'/docs',**extra}
COMMON={'width':1024,'height':1024,'seed':42,'count':1,'precision':'bf16','fp8':False,'blocks_to_swap':0,'adapters':[]}
TRAIN={'resolution':1024,'steps':1200,'rank':32,'learning_rate':0.0001,'batch_size':1,'gradient_accumulation':1,
       'save_every':200,'preview_every':200,'precision':'bf16','fp8':False,'blocks_to_swap':0,'expert':'low','frames':17,'gpus':1}
RECIPES=[
 recipe('krea.generate','Krea 2 · Turbo','krea2','generate',{**COMMON,'steps':8,'guidance':1,'mu':1.15},negative=False,edit=False),
 recipe('krea.train','Krea 2 · LoRA RAW','krea2','train',TRAIN,negative=True),
 recipe('klein.generate','Klein 9B · Base','klein9','generate',{**COMMON,'steps':50,'guidance':4},negative=True,edit=True),
 recipe('klein.train','Klein 9B · LoRA Base','klein9','train',TRAIN,negative=False),
 recipe('wan.i2v.generate','Wan 2.2 · Фото → видео','wan22-i2v','generate',{**COMMON,'width':832,'height':480,'frames':81,'fps':16,'steps':40,'guidance':5,'flow_shift':5},negative=True,edit=True),
 recipe('wan.t2v.generate','Wan 2.2 · Текст → видео','wan22-t2v','generate',{**COMMON,'width':832,'height':480,'frames':81,'fps':16,'steps':40,'guidance':5,'flow_shift':12},negative=True,edit=False),
 recipe('wan.i2v.train','Wan 2.2 I2V · LoRA','wan22-i2v','train',{**TRAIN,'resolution':512,'learning_rate':0.00005},negative=True),
 recipe('wan.t2v.train','Wan 2.2 T2V · LoRA','wan22-t2v','train',{**TRAIN,'resolution':512,'learning_rate':0.00005},negative=True),
 recipe('wan.5b.generate','Wan 2.2 · TI2V 5B','wan22-5b','generate',{**COMMON,'width':1280,'height':704,'frames':121,'fps':24,'steps':50,'guidance':5,'flow_shift':5},negative=True,edit=True),
 recipe('dataset.caption','Описать фотографии','preparation','prepare',{'model':'Qwen/Qwen2.5-VL-3B-Instruct'},negative=False),
 recipe('dataset.video','Нарезать видео','preparation','prepare',{'clip_seconds':3,'overlap':0.5,'fps':16,'frames_only':False},negative=False)
]
BY_ID={r['id']:r for r in RECIPES}

def validate_params(recipe_id,params):
    if recipe_id not in BY_ID:raise ValueError('Unknown recipe')
    r=BY_ID[recipe_id];p={**deepcopy(r['defaults']),**params}
    def integer(key,lo,hi):
        if key in p and (type(p[key]) is not int or not lo<=p[key]<=hi):raise ValueError(f'{key}: {lo}–{hi}')
    for k,a,b in [('steps',1,20000),('rank',4,128),('width',256,2048),('height',256,2048),('resolution',256,1536),('count',1,8),('frames',1,241),('save_every',20,5000),('preview_every',0,5000),('batch_size',1,8),('gradient_accumulation',1,64),('gpus',1,8),('blocks_to_swap',0,26)]:integer(k,a,b)
    if p.get('gpus',1)!=1:raise ValueError('Multi-GPU training is not qualified in this release. Use one GPU; independent jobs can use different workers.')
    if p.get('precision','bf16') not in ('bf16','fp16'):raise ValueError('precision: bf16 or fp16')
    if 'guidance' in p and not 1<=float(p['guidance'])<=15:raise ValueError('guidance: 1–15')
    if 'learning_rate' in p and not 1e-7<=float(p['learning_rate'])<=0.001:raise ValueError('learning_rate: 1e-7–1e-3')
    if p.get('frames',1)%4!=1:raise ValueError('Frame count must be 4n+1')
    if p.get('expert','low') not in ('low','high','both'):raise ValueError('expert: low/high/both')
    if recipe_id=='krea.generate':p['guidance']=1
    if r['family']=='klein9' and p.get('blocks_to_swap',0)>16:raise ValueError('Klein supports at most 16 swap blocks')
    if r['family']=='wan22-5b':
        if p.get('fp8'):raise ValueError('5B FP8 is not qualified; use BF16')
        if (p['width'],p['height']) not in ((1280,704),(704,1280)):raise ValueError('5B: choose 1280×704 or 704×1280')
        if p.get('adapters'):raise ValueError('5B LoRA import is not GPU-qualified; use A14B for LoRAs')
    for k in ('width','height','resolution'):
        if k in p and p[k]%32:raise ValueError(f'{k} must be divisible by 32')
    if not r.get('negative'):p.pop('negative_prompt',None)
    if str(p.get('prompt','')).startswith('--'):raise ValueError('Prompt cannot start with a CLI flag')
    if len(p.get('prompt',''))>6000:raise ValueError('Prompt too long')
    if BY_ID[recipe_id]['kind']=='generate' and p.get('precision')!='bf16':raise ValueError('Generation recipes require BF16-capable GPU in this build')
    if p.get('fp8',False) not in (True,False):raise ValueError('fp8 must be boolean')
    return p

def benchmark_signature(recipe_id,params,gpu):
    keys=('width','height','resolution','frames','steps','precision','fp8','blocks_to_swap','batch_size','gradient_accumulation','gpus','expert','guidance')
    models={k:{x:v for x,v in spec.items() if x in ('repo','revision','path')} if isinstance(spec,dict) else spec for k,spec in params.get('components',{}).items()}
    data={'models':models,'recipe':recipe_id,'gpu':gpu,'commit':MUSUBI_COMMIT,'settings':{k:params[k] for k in keys if k in params},'adapter_count':len(params.get('adapters',[]))}
    # Steps are a work unit for training; speed measurements should survive a changed run length.
    if BY_ID[recipe_id]['kind']=='train':data['settings'].pop('steps',None)
    return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
