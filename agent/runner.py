"""One subprocess per job: exit releases CUDA allocations and library state."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse
import httpx
import tomlkit
from maymay.datasets import safe_members
from maymay.recipes import BY_ID,MUSUBI_COMMIT
from .commands import train_commands,generate_command

def emit(stage,**data):print('MAYMAY_EVENT '+json.dumps({'stage':stage,**data},ensure_ascii=False),flush=True)
def download(url,path,sha=None,headers=None):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    partial=path.with_suffix(path.suffix+'.part');size=0
    with httpx.Client(timeout=120,follow_redirects=True) as c:
        with c.stream('GET',url,headers=headers or {}) as r:
            r.raise_for_status();total=int(r.headers.get('content-length',0));last=0
            with partial.open('wb') as f:
                for b in r.iter_bytes(1024*1024):
                    f.write(b);size+=len(b)
                    if time.monotonic()-last>2:
                        emit('Скачивание',progress=size/total if total else 0,bytes=size,total_bytes=total);last=time.monotonic()
    if sha:
        from maymay.storage import sha_file
        if sha_file(partial)!=sha:raise ValueError('Downloaded file checksum mismatch')
    partial.replace(path);return path

def component(spec,cache):
    if isinstance(spec,str):spec={'path':spec}
    if spec.get('path'):
        path=Path(spec['path'])
        if not path.exists():raise FileNotFoundError('GPU path missing: '+str(path))
        return str(path)
    from huggingface_hub import hf_hub_download,snapshot_download,try_to_load_from_cache
    repo=spec['repo'];revision=spec.get('revision')
    if not revision or revision=='main':raise ValueError('Component revision must be pinned by server preflight')
    missing_bytes=0
    for file in spec.get('files',[]):
        cached=try_to_load_from_cache(repo,file['name'],cache_dir=str(cache),revision=revision)
        if not isinstance(cached,str):missing_bytes+=int(file.get('bytes',0))
    if missing_bytes and shutil.disk_usage(cache).free<missing_bytes+5*1024**3:
        raise RuntimeError(f'Недостаточно места на диске {cache}: свободно {shutil.disk_usage(cache).free/1024**3:.1f} GiB; нужно ещё {missing_bytes/1024**3:.1f} GiB + 5 GiB запаса. Подключите большой рабочий диск к /workspace (см. docs/STORAGE_RU.md). Видеопамять здесь не используется.')
    emit('Загрузка весов',component=repo)
    if spec.get('filename'):return hf_hub_download(repo_id=repo,filename=spec['filename'],revision=revision,cache_dir=str(cache))
    folder=spec.get('subfolder')
    path=snapshot_download(repo_id=repo,revision=revision,cache_dir=str(cache),allow_patterns=[folder+'/*'] if folder else None)
    if folder:
        files=sorted((Path(path)/folder).glob('*.safetensors'))
        if not files:raise ValueError('No encoder shards')
        return str(files[0])
    return path

def run(stage,cmd,root):
    emit(stage)
    # CLI arguments are not echoed: they may contain signed URLs or private prompts.
    subprocess.run([str(x) for x in cmd],cwd=root,check=True)

def unpack(url,target,sha=None):
    zpath=download(url,target.parent/(target.name+'.zip'),sha)
    target.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(zpath) as z:safe_members(z);z.extractall(target)
    return target

def main(jobpath):
    job=json.loads(Path(jobpath).read_text());rid=job['recipe'];p=job['params'];r=BY_ID[rid]
    work=Path(jobpath).parent
    from .lease import watch
    watch(work/'lease')
    out=work/'outputs';out.mkdir(exist_ok=True)
    if r['kind']=='prepare':
        from .prepare import prepare
        return prepare(job,work)
    import torch
    if not torch.cuda.is_available():raise RuntimeError('CUDA GPU unavailable')
    if p.get('precision')=='bf16' and not torch.cuda.is_bf16_supported():raise RuntimeError('This GPU does not support BF16; select a separately tested FP16 profile')
    root=Path(os.environ.get('MUSUBI_ROOT','/opt/musubi'))
    if r['family']!='wan22-5b':
        actual=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
        if actual!=MUSUBI_COMMIT:raise RuntimeError('Musubi commit differs from recipe contract')
    cache=Path(os.getenv('HF_HOME','/workspace/cache/huggingface'));cache.mkdir(parents=True,exist_ok=True)
    # Conservative disk preflight; no artificial VRAM guarantees.
    if shutil.disk_usage(cache).free<5*1024**3:raise RuntimeError('Less than 5 GB free on model cache disk')
    reference=download(p['reference_url'],work/'reference.png') if p.get('reference_url') else None
    if p.get('snapshot'):
        unpack(p['snapshot']['url'],work/'dataset',p['snapshot']['sha256'])
    if p.get('resume_url'):
        resume=unpack(p['resume_url'],work/'resume')
        states=list(resume.rglob('optimizer.bin'))+list(resume.rglob('optimizer.pt'))
        # Manifest stores exact state directory inside archive.
        manifest=resume/'resume.json'
        rel=json.loads(manifest.read_text())['state_directory'] if manifest.exists() else '.'
        state=(resume/rel).resolve()
        if not state.is_relative_to(resume.resolve()):raise ValueError('Unsafe resume path')
        p['resume_path']=str(state)
    required=['raw','vae','text_encoder'] if rid=='krea.train' else ['dit','vae','text_encoder'] if r['family'] in ('krea2','klein9') else ['pipeline'] if r['family']=='wan22-5b' else ['low','high','vae','text_encoder']
    if r['kind']=='train' and r['family'].startswith('wan22-') and p.get('expert')!='both':required=[p.get('expert','low'),'vae','text_encoder']
    c={k:component(p['components'][k],cache) for k in required}
    adapters=[]
    for a in p.get('adapters',[]):
        source=a.get('url') or a['source'];dest=work/('adapter_'+a['id']+'.safetensors')
        if source.startswith('https://') or source.startswith('http://'):
            host=urlparse(source).hostname;headers={}
            if host=='huggingface.co' and os.getenv('HF_TOKEN'):headers={'Authorization':'Bearer '+os.environ['HF_TOKEN']}
            if host=='civitai.com' and os.getenv('CIVITAI_TOKEN'):headers={'Authorization':'Bearer '+os.environ['CIVITAI_TOKEN']}
            path=download(source,dest,headers=headers)
        else:path=Path(source)
        from safetensors import safe_open
        with safe_open(path,framework='pt',device='cpu') as f:
            keys=list(f.keys())
            from .adapters import validate_keys
            validate_keys(keys,f.metadata(),r['family'])
        adapters.append({**a,'path':str(path)})
    if r['kind']=='train':
        doc=tomlkit.document();doc['general']={'resolution':[p['resolution'],p['resolution']],'caption_extension':'.txt','batch_size':p['batch_size'],'enable_bucket':True,'bucket_no_upscale':True}
        entries=[];train=work/'dataset/train'
        has_images=any(f.suffix.lower() in ('.png','.jpg','.jpeg','.webp') for f in train.iterdir())
        has_videos=any(f.suffix.lower() in ('.mp4','.mov','.webm','.mkv') for f in train.iterdir())
        for is_video in ([False] if has_images else [])+([True] if has_videos else []):
            d={'video_directory' if is_video else 'image_directory':str(train),'cache_directory':str(work/('cache_video' if is_video else 'cache_image'))}
            if is_video:d.update(target_frames=[p['frames']],frame_extraction='head',source_fps=16)
            entries.append(d)
        doc['datasets']=entries;(work/'dataset.toml').write_text(tomlkit.dumps(doc))
        prompt=p['validation_prompt'].replace('\n',' ').split(' --')[0]
        preview=f'{prompt} --w {p["resolution"]} --h {p["resolution"]} --d 42 --s 28 --l 3.5'
        if r['family']=='klein9':preview=f'{prompt} --w {p["resolution"]} --h {p["resolution"]} --d 42 --s 50 --l 4'
        if r['family']=='wan22-i2v':
            image=next((f for f in train.iterdir() if f.suffix.lower() in ('.png','.jpg','.webp','.jpeg')),None)
            if not image:
                video=next(f for f in train.iterdir() if f.suffix.lower() in ('.mp4','.mov','.webm','.mkv'))
                image=work/'preview_reference.png';subprocess.run(['ffmpeg','-y','-i',str(video),'-frames:v','1',str(image)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            preview+=' --i '+str(image)+' --f 17'
        (work/'sample_prompts.txt').write_text(preview+'\n')
        for stage,cmd in train_commands(rid,p,c,root,work):run(stage,cmd,root)
        # Save immutable provenance beside all weight exports.
        (out/'provenance.json').write_text(json.dumps({'recipe':rid,'commit':MUSUBI_COMMIT,'components':p['components'],'snapshot':{k:v for k,v in p['snapshot'].items() if k!='url'},'params':{k:v for k,v in p.items() if not k.endswith('_url') and k not in ('items','snapshot')}},ensure_ascii=False,indent=2))
    elif r['family']=='wan22-5b':
        from diffusers import AutoencoderKLWan,WanPipeline,WanImageToVideoPipeline
        from diffusers.utils import export_to_video,load_image
        vae=AutoencoderKLWan.from_pretrained(c['pipeline'],subfolder='vae',torch_dtype=torch.float32)
        cls=WanImageToVideoPipeline if reference else WanPipeline
        pipe=cls.from_pretrained(c['pipeline'],vae=vae,torch_dtype=torch.bfloat16)
        if p.get('blocks_to_swap'):pipe.enable_model_cpu_offload()
        else:pipe.to('cuda')
        from diffusers import UniPCMultistepScheduler
        pipe.scheduler=UniPCMultistepScheduler.from_config(pipe.scheduler.config,flow_shift=p['flow_shift'])
        for n in range(p['count']):
            emit('Генерация видео')
            kwargs={'image':load_image(str(reference))} if reference else {}
            def callback(_pipe,i,t,kwargs):emit('Генерация видео',progress=(i+1)/p['steps']);return kwargs
            frames=pipe(prompt=p['prompt'],negative_prompt=p.get('negative_prompt'),height=p['height'],width=p['width'],num_frames=p['frames'],num_inference_steps=p['steps'],guidance_scale=p['guidance'],generator=torch.Generator('cuda').manual_seed(p['seed']+n),callback_on_step_end=callback,**kwargs).frames[0]
            export_to_video(frames,str(out/f'video_{n}.mp4'),fps=p['fps'])
    else:
        for n in range(p['count']):
            pn={**p,'seed':p['seed']+n};run('Генерация',generate_command(rid,pn,c,root,out,reference,adapters),root)
    emit('Сохранение результатов',progress=1)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('job');args=parser.parse_args();main(args.job)
