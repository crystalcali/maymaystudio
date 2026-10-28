import io
import zipfile
import time
from pathlib import Path
from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from maymay.api import create_app
from maymay.config import Settings
from maymay.db import Worker,Job,Pairing,Artifact,Node
from maymay.recipes import PROFILES

@pytest.fixture
def app(tmp_path):
    return create_app(Settings(compute_mode='legacy',database_url='sqlite:///'+str(tmp_path/'db.sqlite'),data_dir=tmp_path/'objects',public_url='http://testserver'))
@pytest.fixture
def client(app):
    with TestClient(app) as c:
        assert c.post('/api/login',json={'password':'maymay-local'}).status_code==200
        yield c

def image_bytes(color='red'):
    b=io.BytesIO();Image.new('RGB',(640,800),color).save(b,format='PNG');return b.getvalue()
def preset(client,family='krea2'):
    return client.put('/api/profiles/'+family,json={'family':family,'components':{k:{'path':'/gpu/'+k} for k in PROFILES[family]}})
def gen(client,**extra):
    preset(client)
    r=client.post('/api/jobs',json={'recipe':'krea.generate','params':{'prompt':'MayMay_model, in a library'},**extra})
    assert r.status_code==200,r.text
    return r.json()
def worker(client):
    pair=client.post('/api/workers/pair').json()['pairing_code']
    r=client.post('/worker/register',json={'pairing_code':pair,'capabilities':{'gpu':'Test GPU','recipes':['krea.generate','krea.train','dataset.caption']}})
    assert r.status_code==200,r.text
    return {'Authorization':'Bearer '+r.json()['token']}

def test_auth_and_csrf(app):
    with TestClient(app) as c:
        assert c.get('/api/characters').status_code==401
        assert c.post('/api/login',headers={'Origin':'https://evil.test'},json={'password':'maymay-local'}).status_code==403
        assert c.post('/api/login',json={'password':'wrong'}).status_code==401
        assert c.get('/healthz').json()['status']=='ok'

def test_gallery_excludes_weights(client,app):
    with app.state.Session.begin() as s:
        for kind in ['image','metadata','lora','state','video']:
            f=app.state.storage.path(kind);f.write_bytes(b'abc')
            s.add(Artifact(kind=kind,name=kind,key=kind,sha='x'*64,size=3,meta={}))
    assert {a['kind'] for a in client.get('/api/artifacts').json()}=={'image','video'}

def test_pairing_one_use(client):
    pair=client.post('/api/workers/pair').json()['pairing_code']
    assert client.post('/worker/register',json={'pairing_code':pair}).status_code==200
    assert client.post('/worker/register',json={'pairing_code':pair}).status_code==403

def test_claim_fencing_cancel_upload(client):
    j=gen(client);auth=worker(client)
    claim=client.post('/worker/claim',headers=auth).json()['job']
    assert claim['id']==j['id']
    assert client.post('/worker/claim',headers=auth).json()['job'] is None
    base='/worker/jobs/'+j['id']
    assert client.post(base+'/progress',headers=auth,json={'attempt':'stale','stage':'oops'}).status_code==409
    assert client.post(base+'/finish',headers=auth,json={'attempt':claim['attempt'],'status':'succeeded'}).status_code==400
    assert client.post('/api/jobs/'+j['id']+'/cancel').json()['status']=='cancelling'
    assert client.post(base+'/progress',headers=auth,json={'attempt':claim['attempt']}).json()['cancel']
    assert client.post(base+'/finish',headers=auth,json={'attempt':claim['attempt'],'status':'cancelled'}).status_code==200
    assert client.post(base+'/progress',headers=auth,json={'attempt':claim['attempt']}).status_code==409

def test_artifact_atomic_idempotent(client):
    import hashlib
    j=gen(client);auth=worker(client);claim=client.post('/worker/claim',headers=auth).json()['job'];body=image_bytes()
    base='/worker/jobs/'+j['id']+'/artifact'
    init=client.post(base+'/init',headers=auth,json={'attempt':claim['attempt'],'kind':'image','name':'photo.png','size':len(body),'sha':hashlib.sha256(body).hexdigest()}).json()
    assert client.get('/api/artifacts').json()==[]
    assert client.post(base+'/bytes',headers=auth,data={'ticket':init['ticket']},files={'file':('photo.png',body)}).status_code==200
    assert client.post(base+'/complete',headers=auth,json={'ticket':init['ticket']}).status_code==200
    assert client.post(base+'/complete',headers=auth,json={'ticket':init['ticket']}).status_code==200
    assert len(client.get('/api/artifacts').json())==1
    assert client.post('/worker/jobs/'+j['id']+'/finish',headers=auth,json={'attempt':claim['attempt'],'status':'succeeded'}).status_code==200

def test_zip_dedup_and_stable_snapshot(client):
    ds=client.post('/api/datasets',json={'name':'May','character_id':'may'}).json()
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        for n,color in enumerate(['red','blue','green']):
            z.writestr(f'train/{n}.png',image_bytes(color));z.writestr(f'train/{n}.txt','MayMay_model, woman, standing')
        z.writestr('train/duplicate.png',image_bytes('red'))
    r=client.post('/api/datasets/'+ds['id']+'/upload',files={'file':('dataset.zip',b.getvalue())})
    assert r.json()=={'added':3,'duplicates':1},r.text
    first=client.post('/api/datasets/'+ds['id']+'/snapshot',json={}).json()
    second=client.post('/api/datasets/'+ds['id']+'/snapshot',json={}).json()
    assert first['sha256']==second['sha256']
    assert first['train']==2 and first['validation']==1
    download=client.get(first['url']);assert download.status_code==200
    with zipfile.ZipFile(io.BytesIO(download.content)) as z:assert z.testzip() is None

def test_malicious_zip_rejected(client):
    ds=client.post('/api/datasets',json={'name':'Unsafe','character_id':'may'}).json()
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:z.writestr('../evil.png',image_bytes())
    assert client.post('/api/datasets/'+ds['id']+'/upload',files={'file':('x.zip',b.getvalue())}).status_code==400
    assert client.get('/api/datasets/'+ds['id']+'/items').json()==[]

def test_same_group_no_leak(client):
    ds=client.post('/api/datasets',json={'name':'Series','character_id':'may'}).json()
    for color in ['red','blue']:
        client.post('/api/datasets/'+ds['id']+'/upload',files={'file':(color+'.png',image_bytes(color))})
    items=client.get('/api/datasets/'+ds['id']+'/items').json()
    for i in items:client.patch('/api/items/'+i['id'],json={'caption':'MayMay_model, portrait','group_id':'same-video'})
    snap=client.post('/api/datasets/'+ds['id']+'/snapshot',json={}).json()
    assert snap['train']==2 and snap['validation']==0

def test_unpinned_and_incompatible_blocked(client):
    assert client.post('/api/jobs',json={'recipe':'krea.generate','params':{'prompt':'test'}}).status_code==400
    preset(client)
    a=client.post('/api/adapters',json={'name':'wrong','family':'klein9','source':'/gpu/a.safetensors'}).json()
    r=client.post('/api/jobs',json={'recipe':'krea.generate','params':{'prompt':'test','adapters':[{'id':a['id']}]}})
    assert r.status_code==400 and 'Incompatible' in r.text

def test_queued_cancel_and_delete(client):
    j=gen(client)
    assert client.post('/api/jobs/'+j['id']+'/cancel').json()['status']=='cancelled'
    assert client.delete('/api/jobs/'+j['id']).status_code==200

def test_secret_encrypted_not_echoed(client,app):
    client.put('/api/settings/secrets',json={'yougpu':'secret-value'})
    assert 'secret-value' not in client.get('/api/settings').text
    from maymay.db import KV
    with app.state.Session() as s:assert 'secret-value' not in str(s.get(KV,'yougpu').value)

def test_prod_requires_persistent_services(tmp_path):
    with pytest.raises(RuntimeError):Settings(production=True,data_dir=tmp_path).validate()

def test_persists_after_app_restart(tmp_path):
    settings=Settings(database_url='sqlite:///'+str(tmp_path/'restart.sqlite'),data_dir=tmp_path/'objects',public_url='http://testserver')
    a=create_app(settings)
    with TestClient(a) as c:
        c.post('/api/login',json={'password':'maymay-local'})
        j=gen(c)
    b=create_app(settings)
    with TestClient(b) as c:
        c.post('/api/login',json={'password':'maymay-local'})
        assert c.get('/api/jobs').json()[0]['id']==j['id']
        assert c.get('/api/jobs').json()[0]['status']=='queued'

def test_budget_drains_worker_before_claim(client,app):
    auth=worker(client);j=gen(client)
    with app.state.Session.begin() as s:
        w=s.query(Worker).one();n=Node(request_id='budget-request',name='test',hourly=100,budget=1,created=time.time()-3600)
        s.add(n);s.flush();w.node_id=n.id
    assert client.post('/worker/claim',headers=auth).json()['job'] is None

def test_tampered_artifact_rejected(client):
    import hashlib
    j=gen(client);auth=worker(client);claim=client.post('/worker/claim',headers=auth).json()['job'];body=image_bytes()
    base='/worker/jobs/'+j['id']+'/artifact'
    init=client.post(base+'/init',headers=auth,json={'attempt':claim['attempt'],'kind':'image','name':'photo.png','size':len(body),'sha':hashlib.sha256(body).hexdigest()}).json()
    assert client.post(base+'/bytes',headers=auth,data={'ticket':init['ticket']},files={'file':('photo.png',b'corrupt')}).status_code==400
    assert client.get('/api/artifacts').json()==[]

def test_character_anchors_are_independent(client):
    photo=client.post('/api/uploads',files={'file':('face.png',image_bytes())}).json()
    person=client.post('/api/characters',json={'name':'Other','trigger':'Other_model'}).json()
    assert client.patch('/api/characters/'+person['id'],json={'face_id':photo['id'],'description':'anchor'}).status_code==200
    people=client.get('/api/characters').json()
    other=next(c for c in people if c['id']==person['id']);may=next(c for c in people if c['id']=='may')
    assert other['face_id']==photo['id'] and other['face_url']!=may['face_url']

def test_idle_termination_requires_no_unfinished_jobs(client,app,monkeypatch):
    from maymay.api import supervise_once
    calls=[]
    monkeypatch.setenv('YOUGPU_API_KEY','test-only')
    monkeypatch.setattr('maymay.api.YouGPU.terminate',lambda self,id:calls.append(id) or {})
    auth=worker(client)
    with app.state.Session.begin() as s:
        w=s.query(Worker).one();n=Node(request_id='idle-termination-request',provider_id='provider-id',name='idle',hourly=10,auto_terminate=True,created=time.time()-3600,status='ACTIVE')
        s.add(n);s.flush();w.node_id=n.id;node_id=n.id
    j=gen(client,node_id=node_id)
    supervise_once(app);assert calls==[]
    client.post('/api/jobs/'+j['id']+'/cancel')
    # The recently completed task resets the idle timer; budget can intentionally shorten it.
    with app.state.Session.begin() as s:s.get(Node,node_id).budget=.01
    supervise_once(app);assert calls==['provider-id']
    assert client.post('/worker/claim',headers=auth).json()['job'] is None
