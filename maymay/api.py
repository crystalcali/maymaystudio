import asyncio
import contextlib
import hashlib
import hmac
import json
import mimetypes
import os
import re
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request, HTTPException, Depends, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select, update, delete, func
from .config import Settings
from .db import *
from .security import digest, token, set_secret, get_secret, redact
from .storage import Storage, sha_file
from .recipes import RECIPES, BY_ID, PROFILES, validate_params, benchmark_signature
from .datasets import import_zip, ingest_file, snapshot, IMAGE, VIDEO
from .provider import YouGPU, ProviderError
from .preflight import inspect_components
from .estimates import estimate

TERMINAL={'succeeded','cancelled','failed'}
ACTIVE={'running','cancelling','connection_lost'}

def create_app(settings=None):
    s=(settings or Settings()).validate();engine,Session=connect(s.database_url);storage=Storage(s)
    @asynccontextmanager
    async def lifespan(app):
        task=asyncio.create_task(supervise(app))
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):await task
        engine.dispose()
    app=FastAPI(title='MayMay Studio 3',version='3.0.0',lifespan=lifespan)
    app.state.settings=s;app.state.Session=Session;app.state.storage=storage
    app.state.catalog=None;app.state.catalog_at=0;app.state.login_failures={}
    app.add_middleware(SessionMiddleware,secret_key=s.secret,https_only=s.production,same_site='strict',max_age=86400,session_cookie='maymay')

    @app.middleware('http')
    async def security_headers(request,call_next):
        if request.method in ('POST','PUT','PATCH','DELETE') and not request.url.path.startswith('/worker'):
            origin=request.headers.get('origin')
            if origin and origin!=s.public_url:return JSONResponse({'detail':'Invalid request origin'},403)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['X-Frame-Options']='DENY'
        if request.url.path.startswith(('/api','/worker')):response.headers['Cache-Control']='no-store'
        return response
    @app.exception_handler(ProviderError)
    async def provider_error(_,e):
        logger = __import__('logging').getLogger(__name__)
        logger.warning('YouGPU operation=%s status=%s uncertain=%s',e.operation_id,e.status_code,e.uncertain)
        with Session.begin() as record:
            record.add(KV(key='provider-error:'+e.operation_id,value={**e.detail(),'created':time.time()}))
        return JSONResponse({'detail':e.detail()},502)
    @app.exception_handler(ValueError)
    async def value_error(_,e):return JSONResponse({'detail':str(e)},400)

    def db():
        with Session() as session:yield session
    def admin(request:Request):
        if not request.session.get('admin'):raise HTTPException(401,'Sign in required')
    def worker(request:Request,session=Depends(db)):
        raw=request.headers.get('authorization','').removeprefix('Bearer ')
        w=session.scalar(select(Worker).where(Worker.token_hash==digest(raw)))
        if not w:raise HTTPException(401,'Invalid worker token')
        return w
    def get_or_404(session,model,id):
        row=session.get(model,id)
        if not row:raise HTTPException(404,'Not found')
        return row
    def serialize_artifact(row):
        return {**asdict(row),'url':storage.url(row.key),'download_url':storage.url(row.key,True)}
    def job_view(j):
        d=asdict(j);d['params']={k:v for k,v in d['params'].items() if k!='hf_token'}
        return d
    def provider(session):return YouGPU(get_secret(session,'yougpu',s.secret) or os.getenv('YOUGPU_API_KEY',''))
    def pairing(session,node_id=None,seconds=900):
        raw=token();session.add(Pairing(token_hash=digest(raw),expires=time.time()+seconds,node_id=node_id));session.flush();return raw
    async def save_upload(upload,path):
        n=0
        with path.open('wb') as out:
            while chunk:=await upload.read(1024*1024):
                n+=len(chunk)
                if n>s.max_upload:raise HTTPException(413,'File exceeds MAX_UPLOAD_MB')
                out.write(chunk)
        return n

    with Session.begin() as session:
        if not session.get(Character,'may'):
            session.add(Character(id='may',name='May',trigger='MayMay_model',description='35-year-old virtual woman. Identity is defined by original face and body anchors.'))
        for family,components in PROFILES.items():
            if not session.get(Profile,family):session.add(Profile(id=family,name=family,family=family,components=components))

    @app.get('/healthz')
    def health():
        with Session() as q:q.execute(select(1))
        return {'status':'ok','version':'3.0.0'}
    @app.post('/api/login')
    async def login(request:Request):
        ip=request.client.host if request.client else 'unknown';fails=app.state.login_failures
        history=[v for v in fails.get(ip,[]) if v>time.time()-300]
        if len(history)>=8:raise HTTPException(429,'Too many attempts; wait 5 minutes')
        body=await request.json()
        if not hmac.compare_digest(str(body.get('password','')),s.password):
            fails[ip]=history+[time.time()];raise HTTPException(401,'Неверный пароль')
        request.session.clear();request.session['admin']=True;fails.pop(ip,None)
        return {'ok':True}
    @app.post('/api/logout',dependencies=[Depends(admin)])
    def logout(request:Request):request.session.clear();return {'ok':True}
    @app.get('/api/session')
    def session_status(request:Request):return {'authenticated':bool(request.session.get('admin')),'local':not s.production}
    @app.get('/api/recipes',dependencies=[Depends(admin)])
    def recipes():return RECIPES
    @app.get('/api/overview',dependencies=[Depends(admin)])
    def overview(session=Depends(db)):
        return {'characters':session.scalar(select(func.count()).select_from(Character)),
            'media':session.scalar(select(func.count()).select_from(Artifact).where(Artifact.kind.in_(['image','video','preview']))),
            'active_jobs':session.scalar(select(func.count()).select_from(Job).where(Job.status.not_in(TERMINAL))),
            'workers':session.scalar(select(func.count()).select_from(Worker).where(Worker.heartbeat>time.time()-45)),
            'storage':'s3' if s.bucket else 'local','production':s.production}
    def character_view(c,session):
        result=asdict(c)
        for kind in ('face','body'):
            a=session.get(Artifact,getattr(c,kind+'_id')) if getattr(c,kind+'_id') else None
            result[kind+'_url']=storage.url(a.key) if a else ('/may-anchor.png' if c.id=='may' and kind=='face' else None)
        return result
    @app.get('/api/characters',dependencies=[Depends(admin)])
    def characters(session=Depends(db)):return [character_view(x,session) for x in session.scalars(select(Character).order_by(Character.created))]
    @app.post('/api/characters',dependencies=[Depends(admin)])
    def create_character(body:dict,session=Depends(db)):
        name=str(body.get('name','')).strip();trigger=str(body.get('trigger','')).strip()
        if not name or len(name)>160 or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{2,79}',trigger):raise ValueError('Name and English trigger (3–80 characters) required')
        c=Character(name=name,trigger=trigger,description=str(body.get('description',''))[:6000]);session.add(c);session.commit();return asdict(c)
    @app.patch('/api/characters/{id}',dependencies=[Depends(admin)])
    def edit_character(id:str,body:dict,session=Depends(db)):
        c=get_or_404(session,Character,id)
        for key in ('name','description'):
            if key in body:setattr(c,key,str(body[key])[:6000 if key=='description' else 160])
        for key in ('face_id','body_id'):
            if key in body:
                a=get_or_404(session,Artifact,body[key]) if body[key] else None
                if a and Path(a.name).suffix.lower() not in IMAGE:raise ValueError('Anchor must be an image')
                setattr(c,key,a.id if a else None)
        session.commit();return character_view(c,session)
    @app.get('/api/profiles',dependencies=[Depends(admin)])
    def profiles(session=Depends(db)):return [asdict(x) for x in session.scalars(select(Profile))]
    @app.put('/api/profiles/{id}',dependencies=[Depends(admin)])
    def profile_save(id:str,body:dict,session=Depends(db)):
        p=session.get(Profile,id)
        family=body.get('family',p.family if p else '')
        if p and family!=p.family:raise ValueError('Нельзя менять архитектуру существующего профиля; создайте копию')
        if family not in PROFILES:raise ValueError('Unknown family')
        components=body.get('components',{})
        if not isinstance(components,dict):raise ValueError('Components must be an object')
        for key,spec in components.items():
            if isinstance(spec,str):
                if not spec.startswith('/'):raise ValueError('Путь компонента должен быть абсолютным: '+key)
            elif not isinstance(spec,dict) or not (spec.get('path') or spec.get('repo')):
                raise ValueError('Компонент '+key+': укажите repo или абсолютный path')
            elif spec.get('path') and not str(spec['path']).startswith('/'):raise ValueError('Путь компонента должен быть абсолютным: '+key)
        if not p:p=Profile(id=id,name=body.get('name',id)[:160],family=family);session.add(p)
        p.components=components;p.name=body.get('name',p.name)[:160]
        session.commit();return asdict(p)
    @app.delete('/api/profiles/{id}',dependencies=[Depends(admin)])
    def profile_delete(id:str,confirm:bool=False,session=Depends(db)):
        p=get_or_404(session,Profile,id)
        if id in PROFILES:raise ValueError('Системный профиль нельзя удалить; используйте копию')
        if not confirm:raise ValueError('Подтвердите необратимое удаление профиля')
        if any(j.params.get('profile_id')==id for j in session.scalars(select(Job))):
            raise ValueError('Профиль используется в истории заданий. Сначала удалите зависимые задания')
        session.delete(p);session.commit();return {'ok':True}
    @app.post('/api/profiles/{id}/copy',dependencies=[Depends(admin)])
    def profile_copy(id:str,body:dict,session=Depends(db)):
        from copy import deepcopy
        p=get_or_404(session,Profile,id)
        new=Profile(name=str(body.get('name',p.name+' · копия'))[:160],family=p.family,components=deepcopy(p.components))
        session.add(new);session.commit();return asdict(new)
    @app.post('/api/profiles/{id}/reset',dependencies=[Depends(admin)])
    def profile_reset(id:str,session=Depends(db)):
        p=get_or_404(session,Profile,id);p.components=PROFILES[p.family];session.commit();return asdict(p)
    @app.post('/api/profiles/{id}/check',dependencies=[Depends(admin)])
    def profile_check(id:str,session=Depends(db)):
        p=get_or_404(session,Profile,id);report=inspect_components(p.components,get_secret(session,'huggingface',s.secret) or os.getenv('HF_TOKEN',''))
        if report['ok']:
            p.components={k:v['spec'] for k,v in report['components'].items()};session.commit()
        return report
    @app.get('/api/settings',dependencies=[Depends(admin)])
    def settings_view(session=Depends(db)):
        return {'public_url':s.public_url,'agent_image':s.agent_image,'production':s.production,'compute_mode':s.compute_mode,
            'yougpu_connected':bool(session.get(KV,'yougpu') or os.getenv('YOUGPU_API_KEY')),
            'hf_connected':bool(session.get(KV,'huggingface') or os.getenv('HF_TOKEN')),
            'civitai_connected':bool(session.get(KV,'civitai') or os.getenv('CIVITAI_TOKEN'))}
    @app.put('/api/settings/secrets',dependencies=[Depends(admin)])
    def secrets_save(body:dict,session=Depends(db)):
        for key in ('yougpu','huggingface','civitai'):
            if key in body:set_secret(session,key,str(body[key]),s.secret)
        session.commit();app.state.catalog=None;return {'ok':True}
    @app.get('/api/adapters',dependencies=[Depends(admin)])
    def adapters(session=Depends(db)):return [asdict(x) for x in session.scalars(select(Adapter).order_by(Adapter.created.desc()))]
    @app.post('/api/adapters',dependencies=[Depends(admin)])
    def adapter_create(body:dict,session=Depends(db)):
        family=body.get('family');source=str(body.get('source',''))
        if family not in PROFILES:raise ValueError('Select adapter family')
        if source.startswith('https://'):
            host=urlparse(source).hostname
            if host not in ('huggingface.co','civitai.com'):raise ValueError('Only Hugging Face and Civitai sources are supported')
            if host=='huggingface.co' and '/blob/' in source:source=source.replace('/blob/','/resolve/',1)
        elif not source.startswith(('artifact:','/')):raise ValueError('Use HTTPS URL, artifact:id or absolute path on GPU worker')
        a=Adapter(name=str(body.get('name','LoRA'))[:200],family=family,source=source,trigger=str(body.get('trigger',''))[:200],expert=body.get('expert','both'),character_id=body.get('character_id'))
        session.add(a);session.commit();return asdict(a)
    @app.delete('/api/adapters/{id}',dependencies=[Depends(admin)])
    def adapter_delete(id:str,session=Depends(db)):
        a=get_or_404(session,Adapter,id);session.delete(a);session.commit();return {'ok':True}
    @app.get('/api/datasets',dependencies=[Depends(admin)])
    def datasets(session=Depends(db)):
        result=[]
        for d in session.scalars(select(Dataset).order_by(Dataset.created.desc())):
            result.append({**asdict(d),'count':session.scalar(select(func.count()).select_from(Item).where(Item.dataset_id==d.id))})
        return result
    @app.post('/api/datasets',dependencies=[Depends(admin)])
    def dataset_create(body:dict,session=Depends(db)):
        get_or_404(session,Character,body['character_id']);d=Dataset(name=str(body.get('name','Dataset'))[:160],character_id=body['character_id']);session.add(d);session.commit();return asdict(d)
    def dataset_idle(session,id):
        if any(j.params.get('dataset_id')==id and j.status not in TERMINAL for j in session.scalars(select(Job))):
            raise ValueError('Датасет используется активным заданием. Отмените его и дождитесь завершения')
    @app.get('/api/datasets/{id}/settings',dependencies=[Depends(admin)])
    def dataset_settings(id:str,session=Depends(db)):
        d=get_or_404(session,Dataset,id);row=session.get(KV,'dataset-settings:'+id)
        return {'material':'image','caption_rules':'Describe variable properties in English','resolution':1024,'selection':'manual','validation_ratio':.15,
                **(row.value if row else {}),'character_id':d.character_id,'trigger':session.get(Character,d.character_id).trigger}
    @app.put('/api/datasets/{id}/settings',dependencies=[Depends(admin)])
    def dataset_settings_save(id:str,body:dict,session=Depends(db)):
        get_or_404(session,Dataset,id);dataset_idle(session,id)
        value={k:body[k] for k in ('material','caption_rules','resolution','selection','validation_ratio') if k in body}
        if value.get('material','image') not in ('image','video','mixed'):raise ValueError('Тип материала: image/video/mixed')
        if value.get('selection','manual') not in ('manual','exclude_duplicates'):raise ValueError('Неизвестная стратегия отбора')
        if not 0<=float(value.get('validation_ratio',.15))<=.5:raise ValueError('Validation split: 0–0.5')
        if type(value.get('resolution',1024)) is not int or not 256<=value.get('resolution',1024)<=2048:raise ValueError('Разрешение: 256–2048')
        key='dataset-settings:'+id;row=session.get(KV,key)
        if row:row.value={**row.value,**value}
        else:session.add(KV(key=key,value=value))
        session.commit();return dataset_settings(id,session)
    @app.get('/api/datasets/{id}/versions',dependencies=[Depends(admin)])
    def dataset_versions(id:str,session=Depends(db)):
        get_or_404(session,Dataset,id)
        return [{**r.value,'url':storage.url(r.value['key'],True)} for r in session.scalars(select(KV).where(KV.key.startswith('snapshot:'+id+':')))]
    @app.delete('/api/datasets/{id}',dependencies=[Depends(admin)])
    def dataset_delete(id:str,confirm:bool=False,session=Depends(db)):
        d=get_or_404(session,Dataset,id);dataset_idle(session,id)
        if not confirm:raise ValueError('Подтвердите необратимое удаление датасета')
        session.execute(delete(Item).where(Item.dataset_id==id));session.delete(d)
        # Retain immutable snapshots and source objects. Shared artifact/job keys must
        # never be destroyed by deleting an editing collection. GC is separate.
        session.commit();return {'ok':True,'objects_retained':True}
    @app.delete('/api/items/{id}',dependencies=[Depends(admin)])
    def item_delete(id:str,confirm:bool=False,session=Depends(db)):
        i=get_or_404(session,Item,id);dataset_idle(session,i.dataset_id)
        if not confirm:raise ValueError('Подтвердите удаление файла из набора')
        session.get(Dataset,i.dataset_id).revision+=1;session.delete(i);session.commit()
        return {'ok':True,'objects_retained':True}
    @app.get('/api/datasets/{id}/items',dependencies=[Depends(admin)])
    def dataset_items(id:str,session=Depends(db)):
        get_or_404(session,Dataset,id)
        return [{**asdict(i),'url':storage.url(i.key)} for i in session.scalars(select(Item).where(Item.dataset_id==id).order_by(Item.created))]
    @app.post('/api/datasets/{id}/upload',dependencies=[Depends(admin)])
    async def dataset_upload(id:str,file:UploadFile=File(...),session=Depends(db)):
        d=get_or_404(session,Dataset,id)
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'upload';await save_upload(file,path)
            try:
                if Path(file.filename or '').suffix.lower()=='.zip':result=import_zip(session,storage,d,path)
                else:
                    i=ingest_file(session,storage,d,path,file.filename or 'image.png');result={'added':int(i is not None),'duplicates':int(i is None)}
                session.commit();return result
            except Exception:session.rollback();raise
    @app.patch('/api/items/{id}',dependencies=[Depends(admin)])
    def item_edit(id:str,body:dict,session=Depends(db)):
        i=get_or_404(session,Item,id)
        if 'caption' in body:
            i.caption=str(body['caption'])[:12000]
            i.quality={**i.quality,'caption_review_required':False,'caption_status':'edited','caption_error':''}
        if 'accepted' in body:i.accepted=bool(body['accepted'])
        if 'group_id' in body:i.group_id=str(body['group_id'])[:120]
        session.get(Dataset,i.dataset_id).revision+=1;session.commit();return asdict(i)
    @app.post('/api/datasets/{id}/from-artifact',dependencies=[Depends(admin)])
    def dataset_from_artifact(id:str,body:dict,session=Depends(db)):
        d=get_or_404(session,Dataset,id);a=get_or_404(session,Artifact,body['artifact_id'])
        if a.kind not in ('image','video','preview'):raise ValueError('Choose media')
        existing=session.scalar(select(Item).where(Item.dataset_id==id,Item.sha==a.sha))
        if existing:return asdict(existing)
        i=Item(dataset_id=id,name=a.name,key=a.key,sha=a.sha,kind='video' if a.kind=='video' else 'image',caption='',accepted=False,group_id=a.job_id or a.id,quality={'generated':True,'review_required':True})
        session.add(i);d.revision+=1;session.commit();return asdict(i)
    @app.post('/api/datasets/{id}/snapshot',dependencies=[Depends(admin)])
    def dataset_snapshot(id:str,body:dict,session=Depends(db)):
        settings=dataset_settings(id,session)
        ratio=float(body.get('validation_ratio',settings['validation_ratio']))
        if not 0<=ratio<=.5:raise ValueError('Validation ratio: 0–0.5')
        result=snapshot(session,storage,get_or_404(session,Dataset,id),ratio)
        session.commit()
        return {**result,'url':storage.url(result['key'],True)}
    @app.post('/api/uploads',dependencies=[Depends(admin)])
    async def upload_reference(file:UploadFile=File(...),session=Depends(db)):
        ext=Path(file.filename or '').suffix.lower()
        if ext not in IMAGE|VIDEO|{'.safetensors','.zip'}:raise ValueError('Unsupported file')
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/('upload'+ext);size=await save_upload(file,p)
            if ext in IMAGE:
                from PIL import Image
                with Image.open(p) as im:im.verify()
            a=Artifact(id=uid(),name=Path(file.filename).name,kind='reference' if ext in IMAGE|VIDEO else 'weights',key='',sha=sha_file(p),size=size,meta={})
            a.key=f'uploads/{a.id}{ext}';storage.put(p,a.key);session.add(a);session.commit();return serialize_artifact(a)
    @app.get('/api/artifacts',dependencies=[Depends(admin)])
    def artifacts(kind:str='media',session=Depends(db)):
        query=select(Artifact)
        if kind=='media':query=query.where(Artifact.kind.in_(['image','video','preview']))
        elif kind!='all':query=query.where(Artifact.kind==kind)
        return [serialize_artifact(a) for a in session.scalars(query.order_by(Artifact.created.desc()).limit(500))]
    @app.delete('/api/artifacts/{id}',dependencies=[Depends(admin)])
    def artifact_delete(id:str,session=Depends(db)):
        a=get_or_404(session,Artifact,id)
        # Removing a gallery card does not delete objects referenced by jobs/LoRAs.
        a.kind='archived';session.commit();return {'ok':True}
    @app.get('/objects/{signed}')
    def local_object(signed:str):
        if storage.client:raise HTTPException(404)
        try:data=storage.signer.loads(signed,max_age=3600)
        except Exception:raise HTTPException(403,'Link expired')
        p=storage.path(data['key'])
        if not p.is_file():raise HTTPException(404)
        return FileResponse(p,filename=p.name if data.get('download') else None)
    @app.get('/api/jobs',dependencies=[Depends(admin)])
    def jobs(session=Depends(db)):return [job_view(j) for j in session.scalars(select(Job).order_by(Job.created.desc()).limit(200))]
    @app.post('/api/jobs',dependencies=[Depends(admin)])
    def job_create(body:dict,session=Depends(db)):
        rid=body.get('recipe');params=validate_params(rid,body.get('params',{}));recipe=BY_ID[rid]
        char=get_or_404(session,Character,body.get('character_id','may'))
        params['trigger']=char.trigger
        if recipe['kind']!='prepare':
            profile=session.get(Profile,body.get('profile_id') or params.get('profile_id') or recipe['family'])
            if not profile:raise ValueError('Профиль модели не найден. Создайте или выберите профиль в разделе «Модели и LoRA»')
            if profile.family!=recipe['family']:raise ValueError('Архитектура профиля несовместима с выбранным пайплайном')
            params['components']=profile.components;params['profile_id']=profile.id
            # Every cloud model revision must be pinned before paid work.
            required=['raw','vae','text_encoder'] if rid=='krea.train' else ['dit','vae','text_encoder'] if recipe['family'] in ('krea2','klein9') else ['pipeline'] if recipe['family']=='wan22-5b' else ['low','high','vae','text_encoder']
            for key in required:
                spec=profile.components.get(key)
                if not spec:raise ValueError('Отсутствует компонент модели: '+key+'. Заполните профиль')
                if not isinstance(spec,(dict,str)):raise ValueError('Некорректный компонент '+key)
                if isinstance(spec,dict) and not spec.get('path') and not re.fullmatch('[a-fA-F0-9]{40}',str(spec.get('revision',''))):raise ValueError('Не закреплена ревизия модели. В «Модели и LoRA» нажмите «Проверить доступ»')
        if recipe['kind']=='train':
            ds=get_or_404(session,Dataset,params.get('dataset_id',''))
            if ds.character_id!=char.id:raise ValueError('Dataset belongs to another character')
            ds_settings=dataset_settings(ds.id,session)
            params['dataset_settings']=ds_settings
            params['snapshot']=snapshot(session,storage,ds,ds_settings['validation_ratio'])
            if recipe['family'] in ('krea2','klein9') and 'video' in params['snapshot']['kinds']:raise ValueError('Image trainer requires an image-only dataset')
            params['validation_prompt']=params.get('validation_prompt',f'{char.trigger}, woman, candid photo, standing in a bookshop, natural lighting')
            if params.get('resume_artifact'):
                a=get_or_404(session,Artifact,params['resume_artifact'])
                if a.kind!='state':raise ValueError('Resume requires full state, not exported LoRA')
                old=get_or_404(session,Job,a.job_id)
                if old.recipe!=rid or old.params.get('snapshot',{}).get('sha256')!=params['snapshot']['sha256']:raise ValueError('Resume requires the same recipe and dataset snapshot')
                if BY_ID[rid]['family'].startswith('wan') and a.meta.get('expert') not in (None,params.get('expert')):raise ValueError('Resume state belongs to a different Wan expert')
                for k in ('rank','batch_size','gradient_accumulation','precision','expert','components'):
                    if old.params.get(k)!=params.get(k):raise ValueError('Resume configuration differs: '+k)
                params['resume_key']=a.key
        if recipe['kind']=='prepare':
            if rid=='dataset.caption':
                report=inspect_components({'caption_model':{'repo':'Qwen/Qwen2.5-VL-3B-Instruct','revision':'main'}},get_secret(session,'huggingface',s.secret) or os.getenv('HF_TOKEN',''))
                if not report['ok']:raise ValueError('Caption model access check failed')
                params['caption_model']=report['components']['caption_model']['spec']
            ds=get_or_404(session,Dataset,params.get('dataset_id',''))
            if ds.character_id!=char.id:raise ValueError('Dataset belongs to another character')
            params['dataset_settings']=dataset_settings(ds.id,session)
            items=session.scalars(select(Item).where(Item.dataset_id==ds.id)).all()
            params['items']=[{'id':i.id,'key':i.key,'name':i.name,'kind':i.kind,'group_id':i.group_id,'caption':i.caption} for i in items if rid=='dataset.caption' or i.kind=='video']
            if not params['items']:raise ValueError('No suitable items in dataset')
        selected=[]
        for item in params.get('adapters',[]):
            a=get_or_404(session,Adapter,item['id'])
            if a.family!=recipe['family']:raise ValueError('Incompatible LoRA '+a.name)
            weight=float(item.get('weight',1))
            if not -2<=weight<=2:raise ValueError('LoRA weight: -2–2')
            d=asdict(a);d['weight']=weight
            if a.source.startswith('artifact:'):d['key']=get_or_404(session,Artifact,a.source.split(':',1)[1]).key
            selected.append(d)
        params['adapters']=selected
        if recipe['kind']=='generate':
            if not params.get('prompt','').strip():raise ValueError('Введите описание сцены в поле запроса')
            if rid=='wan.i2v.generate' and not params.get('reference_id'):raise ValueError('I2V requires a starting image')
            if params.get('reference_id'):
                if not recipe.get('edit'):raise ValueError('This model does not support image input')
                a=get_or_404(session,Artifact,params['reference_id'])
                if Path(a.name).suffix.lower() not in IMAGE:raise ValueError('Image reference required')
                params['reference_key']=a.key
        j=Job(recipe=rid,params=params,character_id=char.id,preferred_worker=body.get('worker_id') or None,node_id=body.get('node_id') or None)
        if j.node_id:
            node=get_or_404(session,Node,j.node_id)
            if node.status not in ('ACTIVE','INSTALLING','PROVISIONING','CREATING','STARTING','creating'):raise ValueError('Инстанс недоступен. Сверьте статус в разделе «Мощности»')
        if j.preferred_worker:
            w=get_or_404(session,Worker,j.preferred_worker)
            if w.heartbeat<time.time()-45 or w.draining:raise ValueError('Выбранный GPU-агент недоступен или не принимает задачи')
            if j.node_id and w.node_id!=j.node_id:raise ValueError('Агент принадлежит другому инстансу')
            if rid not in w.capabilities.get('recipes',[]):raise ValueError('GPU-агент не поддерживает этот пайплайн')
            if params.get('precision')=='bf16' and re.search(r'\b(T4|P100|V100|P40|GTX)\b',w.capabilities.get('gpu',''),re.I):raise ValueError('Выбранная GPU несовместима с BF16')
        # Unassigned jobs remain queued intentionally; the UI explains this mode.

        session.add(j);session.commit();return job_view(j)
    @app.post('/api/jobs/{id}/cancel',dependencies=[Depends(admin)])
    def cancel_job(id:str,session=Depends(db)):
        j=get_or_404(session,Job,id)
        if j.status=='queued':j.status='cancelled';j.finished=time.time()
        elif j.status not in TERMINAL:j.status='cancelling';j.stage='Остановка процессов и сохранение готовых файлов'
        session.commit();return job_view(j)
    @app.post('/api/jobs/{id}/retry',dependencies=[Depends(admin)])
    def retry_job(id:str,session=Depends(db)):
        old=get_or_404(session,Job,id)
        if old.status not in TERMINAL:raise ValueError('Reconcile active worker before retrying')
        j=Job(recipe=old.recipe,params=old.params,character_id=old.character_id,preferred_worker=old.preferred_worker,node_id=old.node_id)
        session.add(j);session.commit();return job_view(j)
    @app.post('/api/jobs/{id}/reconcile',dependencies=[Depends(admin)])
    def reconcile_job(id:str,body:dict,session=Depends(db)):
        j=get_or_404(session,Job,id)
        if j.status not in ('connection_lost','cancelling'):raise ValueError('Task does not require reconciliation')
        if body.get('processes_stopped') is not True:raise ValueError('Confirm GPU processes have stopped in provider console')
        w=session.get(Worker,j.worker_id) if j.worker_id else None
        if w and w.heartbeat>time.time()-120:raise ValueError('Agent is online; wait for confirmed cancellation')
        j.status='failed';j.finished=time.time();j.stage='Завершено оператором после проверки процессов';j.error='Connection lost; operator confirmed processes stopped. Local-only files may require recovery.'
        if w:w.draining=True
        session.commit();return job_view(j)
    @app.delete('/api/jobs/{id}',dependencies=[Depends(admin)])
    def delete_job(id:str,session=Depends(db)):
        j=get_or_404(session,Job,id)
        if j.status not in TERMINAL|{'queued'}:raise ValueError('Cancel running task first')
        session.execute(delete(Log).where(Log.job_id==id));session.delete(j);session.commit();return {'ok':True}
    @app.get('/api/jobs/{id}/logs',dependencies=[Depends(admin)])
    def job_logs(id:str,session=Depends(db)):
        get_or_404(session,Job,id)
        lines=session.scalars(select(Log).where(Log.job_id==id).order_by(Log.created.desc()).limit(300)).all()
        return [asdict(x) for x in reversed(lines)]
    @app.get('/api/workers',dependencies=[Depends(admin)])
    def workers(session=Depends(db)):
        return [{k:v for k,v in asdict(w).items() if k!='token_hash'}|{'online':w.heartbeat>time.time()-45} for w in session.scalars(select(Worker))]
    @app.post('/api/workers/pair',dependencies=[Depends(admin)])
    def pair_worker(session=Depends(db)):
        raw=pairing(session);session.commit();return {'pairing_code':raw,'expires_in':900,'studio_url':s.public_url,'agent_image':s.agent_image}
    @app.patch('/api/workers/{id}',dependencies=[Depends(admin)])
    def worker_edit(id:str,body:dict,session=Depends(db)):
        w=get_or_404(session,Worker,id);w.draining=bool(body.get('draining',w.draining));session.commit();return {'ok':True}
    @app.get('/api/resources/catalog',dependencies=[Depends(admin)])
    def catalog(refresh:bool=False,session=Depends(db)):
        if refresh or not app.state.catalog or time.time()-app.state.catalog_at>90:
            app.state.catalog=provider(session).catalog(refresh=refresh);app.state.catalog_at=time.time()
        return app.state.catalog
    @app.get('/api/resources/ssh-keys',dependencies=[Depends(admin)])
    def resource_ssh_keys(session=Depends(db)):return provider(session).ssh_keys()
    @app.get('/api/resources/balance',dependencies=[Depends(admin)])
    def balance(session=Depends(db)):return provider(session).balance()
    @app.post('/api/resources/estimate',dependencies=[Depends(admin)])
    def estimates(body:dict,session=Depends(db)):
        params=validate_params(body['recipe'],body.get('params',{}))
        family=BY_ID[body['recipe']]['family']
        if family in PROFILES:
            profile=get_or_404(session,Profile,params.get('profile_id') or family);params['components']=profile.components
        return [{'offer_id':o['id'],**estimate(session,body['recipe'],params,o)} for o in app.state.catalog or []]
    @app.get('/api/resources/nodes',dependencies=[Depends(admin)])
    def nodes(session=Depends(db)):
        return [{**asdict(n),'estimated_spend':round(n.hourly*max(0,(n.terminated or time.time())-n.created)/3600,2) if n.provider_id else None} for n in session.scalars(select(Node).where(Node.status!='archived').order_by(Node.created.desc()))]
    @app.post('/api/resources/rent',dependencies=[Depends(admin)])
    def rent(body:dict,session=Depends(db)):
        if s.compute_mode=='manual':raise HTTPException(410,'Аренда управляется вручную в панели YouGPU. Studio только подключает агента')
        if not s.agent_image:raise ValueError('В Timeweb задайте AGENT_IMAGE — публичный образ GPU-агента из GitHub Packages, затем повторите деплой')
        request_id=body.get('request_id','')
        if not re.fullmatch(r'[a-zA-Z0-9-]{10,64}',request_id):raise ValueError('Unique request_id required')
        existing=session.scalar(select(Node).where(Node.request_id==request_id))
        if existing:return asdict(existing)
        keys=provider(session).ssh_keys()
        selected_keys=body.get('ssh_keys')
        if selected_keys is None:selected_keys=[k['id'] for k in keys if k.get('is_default') is True]
        if not isinstance(selected_keys,list) or not selected_keys or any(not isinstance(k,str) or k not in {x['id'] for x in keys} for k in selected_keys):
            raise ValueError('Нужен зарегистрированный SSH-ключ YouGPU. Добавьте публичный ключ в панели YouGPU, затем выберите его в форме аренды. Запрос оплаты не отправлен')
        offers=provider(session).catalog(refresh=True,require_live=True);offer=next((o for o in offers if o['id']==body['offer_id']),None)
        if not offer or not offer.get('in_stock') or offer.get('stale'):raise ValueError('Предложение недоступно. Обновите каталог')
        limit=float(body.get('max_hourly',0))
        if limit<=0 or offer['hourly']>limit:raise ValueError('Hourly price exceeds your limit')
        if body.get('recipe'):
            task_params=validate_params(body['recipe'],body.get('params',{}));recipe=BY_ID[body['recipe']]
            if recipe['kind']!='prepare':
                profile=get_or_404(session,Profile,task_params.get('profile_id') or recipe['family'])
                required=['raw','vae','text_encoder'] if body['recipe']=='krea.train' else ['dit','vae','text_encoder'] if recipe['family'] in ('krea2','klein9') else ['pipeline'] if recipe['family']=='wan22-5b' else ['low','high','vae','text_encoder']
                if profile.family!=recipe['family'] or any(not profile.components.get(k) for k in required):raise ValueError('Заполните компоненты совместимого профиля перед арендой')
                if any(isinstance(c,dict) and not c.get('path') and not re.fullmatch('[a-fA-F0-9]{40}',str(c.get('revision',''))) for c in profile.components.values()):raise ValueError('Перед арендой откройте профиль модели и нажмите «Проверить доступ»')
            if task_params.get('precision','bf16')=='bf16' and re.search(r'\b(T4|P100|V100|P40|GTX)\b',offer['gpu'],re.I):raise ValueError('Selected GPU is incompatible with this BF16 recipe')
        images=[i for i in offer['images'] if i.get('supports_docker')]
        if not images:raise ValueError('Нет совместимого Docker-образа ОС')
        if body.get('image_id') and body['image_id'] not in [i['id'] for i in images]:raise ValueError('Выберите Docker-образ из текущего предложения')
        if body.get('budget') is not None and float(body['budget'])<=0:raise ValueError('Session budget must be positive')
        n=Node(id=uid(),request_id=request_id,name='maymay-'+request_id,hourly=offer['hourly'],offer=offer,auto_terminate=bool(body.get('auto_terminate',False)),idle_minutes=max(5,int(body.get('idle_minutes',15))),budget=body.get('budget'))
        session.add(n);session.flush();pair=pairing(session,n.id,86400);session.commit()
        payload={'instance_name':n.name,'offer_id':offer['id'],'image_id':body.get('image_id') or images[0]['id'],
            'ssh_keys':selected_keys,'docker_config':{'image':s.agent_image,'env':[{'key':'STUDIO_URL','value':s.public_url},{'key':'PAIRING_CODE','value':pair},{'key':'WORKER_NAME','value':n.name}], 'ports':[]}}
        if body.get('storage_drive_id'):payload['storage_drive_id']=body['storage_drive_id']
        try:
            result=provider(session).create(payload);n.provider_id=result['id'];n.status=result.get('status','PROVISIONING')
            session.commit()
        except ProviderError as exc:
            n.status='reconcile_required' if exc.uncertain else 'ERROR'
            n.connection={'diagnostic':exc.detail()};session.commit();raise
        except Exception:
            n.status='reconcile_required';session.commit();raise
        return asdict(n)
    @app.post('/api/resources/nodes/{id}/refresh',dependencies=[Depends(admin)])
    def node_refresh(id:str,session=Depends(db)):
        n=get_or_404(session,Node,id)
        if not n.provider_id:
            result=provider(session).instances();matches=[x for x in result if x.get('name')==n.name]
            if len(matches)!=1:raise ValueError('Не найден единственный инстанс с именем '+n.name+'. Проверьте панель YouGPU и повторите сверку позже. Новую аренду не создавайте')
            n.provider_id=matches[0]['id']
        result=provider(session).instance(n.provider_id);n.status=result['status']
        # Exclude generated provider credentials from storage and browser.
        n.connection={k:result.get(k) for k in ('ip_address','ssh_port','ssh_username')}
        if n.status=='TERMINATED':n.terminated=time.time()
        session.commit();return asdict(n)
    @app.post('/api/resources/nodes/{id}/terminate',dependencies=[Depends(admin)])
    def node_terminate(id:str,session=Depends(db)):
        if s.compute_mode=='manual':raise HTTPException(410,'Аренда управляется вручную в панели YouGPU. Studio только подключает агента')
        n=get_or_404(session,Node,id)
        active=session.scalar(select(Job).where(Job.node_id==id,Job.status.not_in(TERMINAL)))
        if active:raise ValueError('Cancel tasks and wait for confirmed artifact upload first')
        if not n.provider_id:
            # Read-only reconciliation before an explicitly requested termination.
            node_refresh(id,session)
        if n.status=='TERMINATED':return asdict(n)
        try:provider(session).terminate(n.provider_id)
        except ProviderError as exc:
            if exc.uncertain:n.status='reconcile_required';session.commit()
            raise
        n.status='TERMINATING';session.commit();return asdict(n)

    @app.post('/api/resources/nodes/{id}/archive',dependencies=[Depends(admin)])
    def node_archive(id:str,body:dict,session=Depends(db)):
        n=get_or_404(session,Node,id)
        if body.get('confirm') is not True:raise ValueError('Подтвердите архивирование записи. Это не команда завершения аренды')
        if session.scalar(select(Job).where(Job.node_id==id,Job.status.not_in(TERMINAL))):raise ValueError('Сначала завершите активные задания')
        if session.scalar(select(Worker).where(Worker.node_id==id,Worker.heartbeat>time.time()-120)):raise ValueError('Агент ещё подключён; сначала завершите аренду')
        if n.provider_id:
            result=provider(session).instance(n.provider_id)
            if result['status']!='TERMINATED':raise ValueError('Инстанс ещё существует. Сначала завершите аренду и сверьте статус')
        else:
            matches=[x for x in provider(session).instances() if x['name']==n.name]
            if matches:raise ValueError('Инстанс найден в YouGPU. Нажмите «Сверить статус», затем завершите аренду')
        n.status='archived';n.auto_terminate=False
        n.connection={**n.connection,'archive_note':'Запись скрыта после сверки; это не подтверждение остановки биллинга иных инстансов','archived_at':time.time()}
        session.commit();return {'ok':True,'billing_action':False}

    # Worker API: scoped credentials + lease/attempt fencing. No global provider token is sent.
    @app.post('/worker/register')
    def register(body:dict,session=Depends(db)):
        ph=digest(str(body.get('pairing_code','')))
        p=session.scalar(select(Pairing).where(Pairing.token_hash==ph))
        if not p or p.used or p.expires<time.time():raise HTTPException(403,'Pairing code invalid/expired')
        changed=session.execute(update(Pairing).where(Pairing.id==p.id,Pairing.used==False).values(used=True)).rowcount
        if not changed:raise HTTPException(409,'Pairing code already used')
        raw=token();w=Worker(name=str(body.get('name','GPU worker'))[:160],token_hash=digest(raw),capabilities=body.get('capabilities',{}),heartbeat=time.time(),node_id=p.node_id)
        session.add(w);session.commit();return {'worker_id':w.id,'token':raw}
    @app.post('/worker/heartbeat')
    def heartbeat(body:dict,w=Depends(worker),session=Depends(db)):
        row=session.get(Worker,w.id);row.heartbeat=time.time()
        if body.get('capabilities'):row.capabilities=body['capabilities']
        session.commit();return {'draining':row.draining}
    @app.post('/worker/claim')
    def claim(w=Depends(worker),session=Depends(db)):
        locked=session.scalar(select(Worker).where(Worker.id==w.id).with_for_update())
        if locked.draining:return {'job':None}
        busy=session.scalar(select(Job).where(Job.worker_id==w.id,Job.status.in_(ACTIVE)))
        if busy:return {'job':None}
        if w.node_id:
            node=session.scalar(select(Node).where(Node.id==w.node_id).with_for_update())
            if node and (node.status not in ('ACTIVE','INSTALLING','PROVISIONING','CREATING','STARTING','creating') or (s.compute_mode=='legacy' and node.budget and node.hourly*(time.time()-node.created)/3600>=node.budget)):
                locked.draining=True;session.commit();return {'job':None}
        available=w.capabilities.get('recipes',[])
        q=select(Job).where(Job.status=='queued',Job.recipe.in_(available),((Job.preferred_worker==None)|(Job.preferred_worker==w.id)),((Job.node_id==None)|(Job.node_id==w.node_id))).order_by(Job.created).with_for_update(skip_locked=True)
        j=session.scalar(q)
        if not j:return {'job':None}
        attempt=uid()
        changed=session.execute(update(Job).where(Job.id==j.id,Job.status=='queued').values(status='running',stage='Подготовка',worker_id=w.id,node_id=w.node_id,attempt=attempt,lease=time.time()+s.lease_seconds,started=time.time())).rowcount
        if not changed:session.rollback();return {'job':None}
        session.expire_all();j=session.get(Job,j.id);session.commit()
        payload=job_view(j);params={**j.params}
        for field in ('snapshot','items'):
            if field=='snapshot' and params.get(field):params[field]={**params[field],'url':storage.url(params[field]['key'])}
            if field=='items' and params.get(field):params[field]=[{**i,'url':storage.url(i['key'])} for i in params[field]]
        for field in ('reference','resume'):
            if params.get(field+'_key'):params[field+'_url']=storage.url(params[field+'_key'])
        params['adapters']=[{**a,**({'url':storage.url(a['key'])} if a.get('key') else {})} for a in params.get('adapters',[])]
        payload['params']=params
        return {'job':payload,'secrets':{'hf_token':get_secret(session,'huggingface',s.secret) or os.getenv('HF_TOKEN',''),'civitai_token':get_secret(session,'civitai',s.secret) or os.getenv('CIVITAI_TOKEN','')}}
    def owned(session,id,w,attempt):
        j=get_or_404(session,Job,id)
        if j.worker_id!=w.id or j.attempt!=attempt or j.status in TERMINAL:raise HTTPException(409,'Stale attempt')
        return j
    @app.post('/worker/jobs/{id}/progress')
    def progress(id:str,body:dict,w=Depends(worker),session=Depends(db)):
        j=owned(session,id,w,body.get('attempt'));j.lease=time.time()+s.lease_seconds
        session.get(Worker,w.id).heartbeat=time.time()
        if j.status=='connection_lost':j.status='running'
        j.stage=str(body.get('stage',j.stage))[:120];j.progress=max(0,min(1,float(body.get('progress',j.progress))))
        j.eta=body.get('eta');j.metrics=body.get('metrics',j.metrics)
        if body.get('log'):session.add(Log(job_id=j.id,text=redact(body['log'])[-12000:]))
        if j.node_id:
            node=session.get(Node,j.node_id)
            if s.compute_mode=='legacy' and node and node.budget and node.hourly*(time.time()-node.created)/3600>=node.budget:
                j.status='cancelling';j.stage='Лимит расходов: сохраняем готовые файлы';session.get(Worker,w.id).draining=True
        session.commit();return {'cancel':j.status=='cancelling','lease_seconds':s.lease_seconds}
    @app.post('/worker/jobs/{id}/artifact/init')
    def artifact_init(id:str,body:dict,w=Depends(worker),session=Depends(db)):
        j=owned(session,id,w,body.get('attempt'))
        if body.get('kind') not in ('image','video','preview','lora','state','metadata','dataset_image','dataset_video'):raise ValueError('Invalid artifact kind')
        name=Path(str(body['name'])).name
        if not name or len(name)>240 or int(body.get('size',0))<=0:raise ValueError('Invalid artifact')
        sha=str(body.get('sha',''))
        if not re.fullmatch('[a-f0-9]{64}',sha):raise ValueError('SHA256 required')
        key=f'results/{j.id}/{j.attempt}/{sha}/{name}'
        existing=session.scalar(select(Artifact).where(Artifact.key==key))
        if existing:return {'existing':True,'id':existing.id}
        upload_id=None
        if storage.client and int(body['size'])>64*1024**2:
            upload_id=storage.client.create_multipart_upload(Bucket=s.bucket,Key=key)['UploadId']
        raw=storage.signer.dumps({'upload_id':upload_id,'job':id,'attempt':j.attempt,'key':key,'name':name,'kind':body['kind'],'size':int(body['size']),'sha':sha,'meta':body.get('meta',{})})
        result={'ticket':raw,'key':key}
        if storage.client:
            if upload_id:
                part_size=64*1024**2;count=(int(body['size'])+part_size-1)//part_size
                if count>10000:raise ValueError('Artifact too large')
                result['multipart']={'part_size':part_size,'urls':[storage.client.generate_presigned_url('upload_part',Params={'Bucket':s.bucket,'Key':key,'UploadId':upload_id,'PartNumber':n},ExpiresIn=3600) for n in range(1,count+1)]}
            else:result['put_url']=storage.client.generate_presigned_url('put_object',Params={'Bucket':s.bucket,'Key':key},ExpiresIn=3600)
        return result
    @app.post('/worker/jobs/{id}/artifact/bytes')
    async def artifact_bytes(id:str,ticket:str=Form(...),file:UploadFile=File(...),w=Depends(worker),session=Depends(db)):
        try:t=storage.signer.loads(ticket,max_age=3600)
        except Exception:raise HTTPException(403)
        owned(session,id,w,t['attempt'])
        if t['job']!=id or storage.client:raise HTTPException(403)
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'upload';n=await save_upload(file,p)
            if n!=t['size'] or sha_file(p)!=t['sha']:raise ValueError('Artifact checksum mismatch')
            storage.put(p,t['key'])
        return {'ok':True}
    @app.post('/worker/jobs/{id}/artifact/complete')
    def artifact_complete(id:str,body:dict,w=Depends(worker),session=Depends(db)):
        try:t=storage.signer.loads(body['ticket'],max_age=3600)
        except Exception:raise HTTPException(403)
        j=owned(session,id,w,t['attempt'])
        if t['job']!=id:raise HTTPException(403)
        if t.get('upload_id'):
            try:complete=storage.head(t['key'])==t['size']
            except Exception:complete=False
            if not complete:
                parts=body.get('parts',[])
                if not parts:raise ValueError('Multipart ETags required')
                storage.client.complete_multipart_upload(Bucket=s.bucket,Key=t['key'],UploadId=t['upload_id'],MultipartUpload={'Parts':parts})
        if storage.head(t['key'])!=t['size']:raise ValueError('Artifact size mismatch')
        a=session.scalar(select(Artifact).where(Artifact.key==t['key']))
        if not a:
            a=Artifact(job_id=id,kind=t['kind'],key=t['key'],name=t['name'],sha=t['sha'],size=t['size'],meta={**t['meta'],'recipe':j.recipe,'params':j.params})
            session.add(a);session.flush()
            if a.kind=='lora':session.add(Adapter(name=a.name,family=BY_ID[j.recipe]['family'],source='artifact:'+a.id,trigger=j.params.get('trigger',''),expert=t['meta'].get('expert','both'),character_id=j.character_id))
            if a.kind in ('dataset_image','dataset_video'):
                ds=get_or_404(session,Dataset,j.params['dataset_id'])
                if not session.scalar(select(Item).where(Item.dataset_id==ds.id,Item.sha==a.sha)):
                    session.add(Item(dataset_id=ds.id,name=a.name,key=a.key,sha=a.sha,kind='image' if a.kind=='dataset_image' else 'video',caption=t['meta'].get('caption',''),accepted=False,group_id=t['meta'].get('group_id',a.id),quality=t['meta'].get('quality',{})));ds.revision+=1
        session.commit();return {'id':a.id}
    @app.post('/worker/jobs/{id}/finish')
    def finish(id:str,body:dict,w=Depends(worker),session=Depends(db)):
        j=owned(session,id,w,body.get('attempt'))
        status=body.get('status')
        if status not in TERMINAL:raise ValueError('Invalid final state')
        if j.status=='cancelling':status='cancelled'
        if status=='succeeded' and BY_ID[j.recipe]['kind']=='generate':
            if not session.scalar(select(Artifact).where(Artifact.job_id==id,Artifact.kind.in_(['image','video']))):raise ValueError('Generation cannot succeed without media')
        if status=='succeeded' and BY_ID[j.recipe]['kind']=='train':
            if not session.scalar(select(Artifact).where(Artifact.job_id==id,Artifact.kind=='lora')):raise ValueError('Training cannot succeed without LoRA')
        for result in body.get('captions',[]):
            item=session.get(Item,result['id'])
            if item and item.dataset_id==j.params.get('dataset_id'):
                # A failed caption attempt updates diagnostics, never destroys prior text.
                if isinstance(result.get('caption'),str) and result['caption'].strip():
                    item.caption=result['caption'][:12000]
                item.quality={**item.quality,**result.get('quality',{})};session.get(Dataset,item.dataset_id).revision+=1
        j.status=status;j.stage={'succeeded':'Готово','cancelled':'Отменено','failed':'Ошибка'}[status];j.finished=time.time();j.error=redact(body.get('error',''))[:6000]
        if status=='succeeded' and j.recipe=='dataset.caption':
            failed=sum(r.get('quality',{}).get('caption_status')=='failed' for r in body.get('captions',[]))
            if failed:j.stage=f'Нужна проверка описаний: {failed}'
        if status=='succeeded':j.progress=1
        m=body.get('benchmark')
        if m and status=='succeeded' and m.get('samples',0)>0:
            node=session.get(Node,w.node_id) if w.node_id else None
            gpu=node.offer.get('gpu',w.capabilities.get('gpu','unknown')) if node else w.capabilities.get('gpu','unknown')
            session.add(Benchmark(signature=benchmark_signature(j.recipe,j.params,gpu),recipe=j.recipe,gpu=gpu,seconds=float(m['seconds']),samples=int(m['samples']),setup_seconds=float(m.get('setup_seconds',0))))
        session.commit();return {'ok':True}

    frontend=Path(__file__).resolve().parents[1]/'frontend'/'dist'
    if frontend.is_dir():
        app.mount('/assets',StaticFiles(directory=frontend/'assets'),name='assets')
        @app.get('/{path:path}')
        def spa(path:str):
            if path.startswith(('api/','worker/')):raise HTTPException(404)
            candidate=(frontend/path).resolve()
            if candidate.is_relative_to(frontend) and candidate.is_file():return FileResponse(candidate)
            return FileResponse(frontend/'index.html')
    return app

async def supervise(app):
    """Durable-state reconciliation only; never restarts uncertain GPU jobs automatically."""
    while True:
        await asyncio.sleep(15)
        try:await asyncio.to_thread(supervise_once,app)
        except Exception:
            import logging;logging.getLogger('maymay').exception('Supervisor reconciliation failed')

def supervise_once(app):
    s=app.state.settings
    with app.state.Session() as db:
        now=time.time()
        for j in db.scalars(select(Job).where(Job.status.in_(['running','cancelling']),Job.lease<now)):
            j.status='connection_lost';j.stage='Связь потеряна; автоматический повтор отключён'
        db.commit()
        if s.compute_mode=='manual':return
        for candidate in db.scalars(select(Node).where(Node.auto_terminate==True,Node.terminated==None)).all():
            n=db.scalar(select(Node).where(Node.id==candidate.id).with_for_update().execution_options(populate_existing=True))
            if not n.provider_id or n.status in ('TERMINATING','reconcile_required','creating'):continue
            ready=db.scalar(select(Worker).where(Worker.node_id==n.id,Worker.heartbeat>now-45))
            if not ready:continue
            active=db.scalar(select(Job).where(Job.node_id==n.id,Job.status.not_in(TERMINAL)))
            if active:continue
            latest=db.scalar(select(func.max(Job.finished)).where(Job.node_id==n.id)) or n.created
            budget_reached=n.budget and n.hourly*(now-n.created)/3600>=n.budget
            if now-latest<n.idle_minutes*60 and not budget_reached:continue
            key=get_secret(db,'yougpu',s.secret) or os.getenv('YOUGPU_API_KEY','')
            if key:
                n.status='TERMINATING';db.commit()
                try:YouGPU(key).terminate(n.provider_id)
                except ProviderError:
                    n.status='reconcile_required';db.commit()

app=None # uvicorn uses maymay.api:create_app --factory
