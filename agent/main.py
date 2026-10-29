import argparse
import contextlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse
import httpx
from maymay.recipes import RECIPES
from maymay.storage import sha_file
from maymay.security import redact

class Studio:
    def __init__(self,url,secret=''):self.url=url.rstrip('/');self.secret=secret
    def post(self,path,body=None,**kwargs):
        with httpx.Client(timeout=45) as c:
            r=c.post(self.url+path,json=body if not kwargs else None,headers=({'Authorization':'Bearer '+self.secret} if self.secret else {}),**kwargs)
        r.raise_for_status();return r.json()

def hardware():
    gpu='CPU';count=0;vram=0
    try:
        raw=subprocess.check_output(['nvidia-smi','--query-gpu=name,memory.total','--format=csv,noheader,nounits'],text=True,timeout=15)
        lines=raw.strip().splitlines();gpu=lines[0].rsplit(',',1)[0].strip();count=len(lines);vram=int(lines[0].rsplit(',',1)[1])
    except Exception:pass
    return {'gpu':gpu,'gpu_count':count,'vram_mb_each':vram,'recipes':[r['id'] for r in RECIPES if count or r['id']=='dataset.video'],'agent_version':'3.0.0'}

def upload(api,job,path,kind,meta=None):
    size=path.stat().st_size;sha=sha_file(path);base=f'/worker/jobs/{job["id"]}/artifact'
    init=api.post(base+'/init',{'attempt':job['attempt'],'name':path.name,'size':size,'sha':sha,'kind':kind,'meta':meta or {}})
    if init.get('existing'):return
    parts=[]
    if init.get('multipart'):
        config=init['multipart']
        with path.open('rb') as f,httpx.Client(timeout=600) as c:
            for n,url in enumerate(config['urls'],1):
                chunk=f.read(config['part_size']);r=c.put(url,content=chunk);r.raise_for_status()
                parts.append({'ETag':r.headers['etag'],'PartNumber':n})
    elif init.get('put_url'):
        # Presigned URLs must be used unchanged; no provider credentials are added.
        with path.open('rb') as f,httpx.Client(timeout=600) as c:
            r=c.put(init['put_url'],content=f,headers={'Content-Length':str(size)});r.raise_for_status()
    else:
        with path.open('rb') as f:
            api.post(base+'/bytes',data={'ticket':init['ticket']},files={'file':(path.name,f)})
    api.post(base+'/complete',{'ticket':init['ticket'],'parts':parts})

def exports(work,settled=False):
    out=work/'outputs'
    if not out.exists():return
    now=time.time()
    for f in out.rglob('*'):
        if not f.is_file() or '.part' in f.name:continue
        if not settled and now-f.stat().st_mtime<15:continue
        if any('state' in part for part in f.relative_to(out).parts[:-1]):continue
        ext=f.suffix.lower();kind=None
        if ext in ('.png','.jpg','.webp'):kind='preview' if 'sample' in str(f) or 'preview' in str(f) else 'image'
        elif ext=='.mp4':kind='video'
        elif ext=='.safetensors':
            from safetensors import safe_open
            try:
                with safe_open(f,framework='pt',device='cpu') as sf:
                    if not any('lora' in k.lower() for k in sf.keys()):continue
            except Exception:continue
            kind='lora'
        elif ext=='.json':kind='metadata'
        if kind:
            meta={}
            if '/high/' in f.as_posix():meta['expert']='high'
            elif '/low/' in f.as_posix():meta['expert']='low'
            yield f,kind,meta
    # A complete trainer state is exported as a single archive, separately from LoRA.
    for folder in out.rglob('*state'):
        if not folder.is_dir():continue
        files=[f for f in folder.rglob('*') if f.is_file()]
        if not files or (not settled and now-max(f.stat().st_mtime for f in files)<30):continue
        if not any('optimizer' in f.name for f in files):continue
        target=work/(folder.parent.name+'_'+folder.name+'.zip')
        stamp=max(f.stat().st_mtime for f in files)
        if not target.exists() or target.stat().st_mtime<stamp:
            partial=target.with_suffix('.part')
            with zipfile.ZipFile(partial,'w',zipfile.ZIP_STORED) as z:
                for f in files:z.write(f,str(Path('state')/f.relative_to(folder)))
                z.writestr('resume.json',json.dumps({'state_directory':'state'}))
            partial.replace(target)
        yield target,'state',{'expert':folder.parent.name}

def execute(api,job,secrets,root):
    work=root/'jobs'/job['id']/job['attempt'];work.mkdir(parents=True,exist_ok=True)
    jobpath=work/'job.json';jobpath.write_text(json.dumps(job));jobpath.chmod(0o600);lease_file=work/'lease';lease_file.touch()
    env=os.environ.copy();env.update({'HF_TOKEN':secrets.get('hf_token',''),'CIVITAI_TOKEN':secrets.get('civitai_token',''),'PYTHONUNBUFFERED':'1','HF_HUB_DISABLE_XET':os.getenv('HF_HUB_DISABLE_XET','1')})
    process=subprocess.Popen([sys.executable,'-m','agent.runner',str(jobpath)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1,start_new_session=True,env=env)
    state={'stage':'Подготовка','progress':0,'metrics':{},'eta':None};cancel=threading.Event();done=threading.Event();lease_lost=threading.Event();lines=queue.Queue();upload_errors=[]
    def heartbeat():
        last=time.monotonic()
        while not done.is_set():
            try:
                answer=api.post(f'/worker/jobs/{job["id"]}/progress',{'attempt':job['attempt'],**state})
                last=time.monotonic();lease_file.touch()
                if answer.get('cancel'):cancel.set()
            except Exception:
                if time.monotonic()-last>95:lease_lost.set();cancel.set()
            if cancel.is_set() and process.poll() is None:
                with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGINT)
            done.wait(5)
    def read():
        for line in process.stdout:lines.put(line)
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start();threading.Thread(target=read,daemon=True).start()
    sent=set();started=time.monotonic();last_upload=0;cancel_at=None;last_log=0;log_buffer=[];step_seconds=[];compute_start=None;compute_end=None
    def flush_exports(settled=False):
        for path,kind,meta in exports(work,settled) or []:
            signature=(str(path),path.stat().st_size,path.stat().st_mtime_ns)
            if signature in sent:continue
            upload(api,job,path,kind,meta);sent.add(signature)
    try:
        while process.poll() is None or not lines.empty():
            try:
                line=lines.get(timeout=.5).strip()
                if line.startswith('MAYMAY_EVENT '):
                    event=json.loads(line.removeprefix('MAYMAY_EVENT '));state['stage']=event.get('stage',state['stage']);state['progress']=event.get('progress',0);state['eta']=None
                    if compute_start is None and state['stage'].startswith(('Обучение','Генерация')):compute_start=time.monotonic()
                else:
                    clean=redact(line);log_buffer.append(clean)
                    match=re.search(r'(\d+)/(\d+)\s*\[',line)
                    if match:
                        current,total=map(int,match.groups());state['progress']=min(1,current/max(1,total))
                        secs=re.search(r'([\d.]+)s/it',line)
                        if secs:
                            speed=float(secs[1]);state['eta']=(total-current)*speed;
                            if state['stage'].startswith('Обучение') and total==job['params'].get('steps'):step_seconds.append(speed)
                            state['metrics']={'step':current,'total':total,'seconds_per_step':speed}
                        loss=re.search(r'avr_loss=([\d.eE+-]+)',line)
                        if loss:state['metrics']={**state['metrics'],'loss':float(loss[1])}
            except queue.Empty:pass
            if log_buffer and time.monotonic()-last_log>3:
                try:
                    answer=api.post(f'/worker/jobs/{job["id"]}/progress',{'attempt':job['attempt'],**state,'log':'\n'.join(log_buffer)[-12000:]})
                    lease_file.touch();log_buffer.clear();last_log=time.monotonic()
                    if answer.get('cancel'):cancel.set()
                except Exception:pass
            if cancel.is_set():
                if cancel_at is None:cancel_at=time.monotonic()
                if process.poll() is None:
                    with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGINT if time.monotonic()-cancel_at<30 else signal.SIGKILL)
            if time.monotonic()-last_upload>30 and not lease_lost.is_set():
                try:flush_exports()
                except Exception:pass # retain complete files and retry; no terminal success yet
                last_upload=time.monotonic()
        process.wait();compute_end=time.monotonic();state['stage']='Сохранение результатов';state['progress']=0
        if lease_lost.is_set():raise RuntimeError('Lease lost. Local files retained; reconnect and reconcile before retrying.')
        # Final uploads retry without discarding the completed computation.
        for attempt in range(5):
            try:flush_exports(settled=True);break
            except Exception:
                if attempt==4:raise
                time.sleep(min(20,2**attempt))
        captions=[];results=work/'prepared.json'
        if results.exists():
            prepared=json.loads(results.read_text());captions=prepared.get('captions',[])
            for item in prepared.get('media',[]):
                path=Path(item['path']).resolve()
                if not path.is_relative_to(work.resolve()):raise ValueError('Unsafe prepared output')
                upload(api,job,path,item['kind'],item.get('meta',{}))
        status='cancelled' if cancel.is_set() else 'succeeded' if process.returncode==0 else 'failed'
        result={'attempt':job['attempt'],'status':status,'error':'' if status!='failed' else '\n'.join(log_buffer)[-5000:] or 'GPU process failed; see task log','captions':captions}
        if status=='succeeded' and BY_KIND(job)=='train' and step_seconds:
            from statistics import median
            result['benchmark']={'seconds':median(step_seconds[-30:])*min(30,len(step_seconds)),'samples':min(30,len(step_seconds))}
        if status=='succeeded' and BY_KIND(job)=='generate' and compute_start is not None:
            result['benchmark']={'seconds':compute_end-compute_start,'samples':job['params'].get('count',1),'setup_seconds':compute_start-started}
        api.post(f'/worker/jobs/{job["id"]}/finish',result)
    except Exception as e:
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGKILL)
            process.wait()
        print(redact(str(e)),flush=True)
        if not lease_lost.is_set():
            with contextlib.suppress(Exception):api.post(f'/worker/jobs/{job["id"]}/finish',{'attempt':job['attempt'],'status':'failed','error':redact(str(e))})
    finally:
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGKILL)
                process.wait()
        done.set();thread.join(timeout=10)
        jobpath.unlink(missing_ok=True)

def BY_KIND(job):
    from maymay.recipes import BY_ID
    return BY_ID[job['recipe']]['kind']

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');args=parser.parse_args()
    url=os.environ.get('STUDIO_URL','').rstrip('/')
    parsed=urlparse(url)
    if not (parsed.scheme=='https' or (parsed.scheme=='http' and parsed.hostname in ('localhost','127.0.0.1','host.docker.internal'))):raise RuntimeError('STUDIO_URL must use HTTPS (except local development)')
    root=Path(os.getenv('WORKER_DIR','/workspace/maymay'));root.mkdir(parents=True,exist_ok=True);root.chmod(0o700)
    credentials=root/'worker.json'
    if credentials.exists():
        saved=json.loads(credentials.read_text())
        if saved.get('studio_url')!=url:raise RuntimeError('Worker credentials belong to another studio')
    else:
        api=Studio(url);pair=os.environ.get('PAIRING_CODE','')
        if not pair:raise RuntimeError('PAIRING_CODE required for first registration')
        # Retry startup only on failures known to precede a response; never
        # repeat registration after a read timeout (one-use pairing may be consumed).
        for startup_attempt in range(5):
            try:
                saved=api.post('/worker/register',{'pairing_code':pair,'name':os.getenv('WORKER_NAME','YouGPU worker'),'capabilities':hardware()})
                break
            except (httpx.ConnectError,httpx.ConnectTimeout):
                if startup_attempt==4:raise
                print('Регистрация: Studio недоступна; повтор подключения',flush=True)
                time.sleep(min(2**startup_attempt,8))
        saved['studio_url']=url;credentials.write_text(json.dumps(saved));credentials.chmod(0o600)
    api=Studio(url,saved['token']);delay=2
    while True:
        try:
            api.post('/worker/heartbeat',{'capabilities':hardware()});claim=api.post('/worker/claim')
            if claim.get('job'):execute(api,claim['job'],claim.get('secrets',{}),root)
            elif args.once:return
            else:time.sleep(5)
            delay=2
        except KeyboardInterrupt:return
        except Exception as e:
            print('Studio connection: '+redact(str(e)),flush=True);time.sleep(delay);delay=min(30,delay*2)

if __name__=='__main__':main()
