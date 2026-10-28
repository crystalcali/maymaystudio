import io
import zipfile
import json
import pytest
from test_api import app,client,image_bytes,preset,gen
from maymay.db import Node, Job
from maymay.provider import YouGPU,ProviderError


def dataset(client):return client.post('/api/datasets',json={'name':'test','character_id':'may'}).json()['id']

@pytest.mark.parametrize('mode',['separate','jsonl'])
def test_caption_formats_delete_snapshot(client,mode):
    d=dataset(client);b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        z.writestr('images/a.png',image_bytes())
        if mode=='separate':z.writestr('captions/a.txt','MayMay_model, portrait')
        else:z.writestr('metadata.jsonl',json.dumps({'file_name':'images/a.png','text':'MayMay_model, portrait'}))
    assert client.post(f'/api/datasets/{d}/upload',files={'file':('d.zip',b.getvalue())}).status_code==200
    i=client.get(f'/api/datasets/{d}/items').json()[0];assert i['caption']=='MayMay_model, portrait'
    snap=client.post(f'/api/datasets/{d}/snapshot',json={}).json()
    assert len(client.get(f'/api/datasets/{d}/versions').json())==1
    assert client.delete('/api/items/'+i['id']).status_code==400
    assert client.delete('/api/items/'+i['id']+'?confirm=true').status_code==200
    assert client.get(snap['url']).status_code==200
    assert client.delete(f'/api/datasets/{d}?confirm=true').status_code==200
    assert client.get(snap['url']).status_code==200


def test_delete_active_dataset_blocked(client,app):
    d=dataset(client)
    with app.state.Session.begin() as s:s.add(Job(recipe='dataset.caption',character_id='may',params={'dataset_id':d}))
    assert client.delete(f'/api/datasets/{d}?confirm=true').status_code==400


def test_settings_and_profiles(client):
    d=dataset(client)
    assert client.put(f'/api/datasets/{d}/settings',json={'validation_ratio':.2,'material':'mixed'}).json()['validation_ratio']==.2
    assert client.put(f'/api/datasets/{d}/settings',json={'validation_ratio':.8}).status_code==400
    assert client.delete('/api/profiles/krea2?confirm=true').status_code==400
    p=client.post('/api/profiles/krea2/copy',json={'name':'Mine'}).json()
    assert client.delete('/api/profiles/'+p['id']+'?confirm=true').status_code==200
    p=client.post('/api/profiles/krea2/copy',json={}).json()
    preset(client)
    client.put('/api/profiles/'+p['id'],json={'components':{k:{'path':'/gpu/'+k} for k in p['components']}})
    r=client.post('/api/jobs',json={'recipe':'krea.generate','params':{'prompt':'test','profile_id':p['id']}})
    assert r.status_code==200,r.text
    assert r.json()['params']['profile_id']==p['id']
    assert client.delete('/api/profiles/'+p['id']+'?confirm=true').status_code==400


def test_generation_validation(client):
    r=client.post('/api/jobs',json={'recipe':'krea.generate','params':{'prompt':'hi'}})
    assert r.status_code==400 and 'ревизия' in r.json()['detail']
    preset(client)
    assert client.post('/api/jobs',json={'recipe':'krea.generate','params':{'prompt':'','width':999}}).status_code==400
    assert client.post('/api/jobs',json={'recipe':'krea.generate','profile_id':'missing','params':{'prompt':'hi'}}).status_code==400
    assert client.post('/api/jobs',json={'recipe':'krea.generate','params':{'prompt':'hi'},'worker_id':'missing'}).status_code==404


def test_reconcile_no_repeated_create(client,app,monkeypatch):
    with app.state.Session.begin() as s:s.add(Node(id='local',name='maymay-full-id',request_id='full-id-123456',status='reconcile_required'))
    monkeypatch.setattr(YouGPU,'create',lambda *a:pytest.fail('Never create during reconcile'))
    monkeypatch.setattr(YouGPU,'instances',lambda self:[{'id':'paid','name':'maymay-full-id'}])
    monkeypatch.setattr(YouGPU,'instance',lambda self,id:{'id':id,'status':'ACTIVE','connection_info':{'secrets':{'password':'secret'}}})
    r=client.post('/api/resources/nodes/local/refresh')
    assert r.status_code==200 and r.json()['provider_id']=='paid'
    assert 'secret' not in r.text


def test_structured_provider_error(client,monkeypatch):
    def fail(*a,**kw):raise ProviderError('Сеть недоступна',status_code=503,operation_id='op')
    monkeypatch.setattr(YouGPU,'balance',fail)
    r=client.get('/api/resources/balance');assert r.status_code==502
    assert r.json()['detail']['provider_status']==503


def test_uncertain_rental_idempotency(client,app,monkeypatch):
    app.state.settings.agent_image='ghcr.io/example/agent:commit'
    offers=[{'id':'o','hourly':12,'in_stock':True,'gpu':'A100','images':[{'id':'os','supports_docker':True}]}]
    monkeypatch.setattr(YouGPU,'catalog',lambda self,**kw:offers)
    calls=[]
    def create(self,payload):
        calls.append(payload)
        raise ProviderError('uncertain',uncertain=True)
    monkeypatch.setattr(YouGPU,'create',create)
    body={'offer_id':'o','max_hourly':20,'request_id':'full-unique-operation-123'}
    assert client.post('/api/resources/rent',json=body).status_code==502
    n=client.post('/api/resources/rent',json=body).json()
    assert n['status']=='reconcile_required' and len(calls)==1
    assert calls[0]['instance_name']=='maymay-'+body['request_id']
    monkeypatch.setattr(YouGPU,'instances',lambda self:[{'id':'paid','name':calls[0]['instance_name']}])
    monkeypatch.setattr(YouGPU,'instance',lambda self,id:{'status':'ACTIVE'})
    assert client.post('/api/resources/nodes/'+n['id']+'/refresh').json()['provider_id']=='paid'
    assert len(calls)==1


def test_shared_artifact_object_retained(client,app):
    from maymay.db import Artifact
    with app.state.Session.begin() as s:
        p=app.state.storage.path('shared.png');p.write_bytes(image_bytes())
        s.add(Artifact(id='shared',kind='image',name='shared.png',key='shared.png',sha='x'*64,size=p.stat().st_size,meta={}))
    d=dataset(client)
    client.post(f'/api/datasets/{d}/from-artifact',json={'artifact_id':'shared'})
    assert client.delete(f'/api/datasets/{d}?confirm=true').status_code==200
    assert app.state.storage.path('shared.png').exists()
    assert client.get('/api/artifacts').json()[0]['id']=='shared'


def test_terminate_uncertain_requires_reconcile(client,app,monkeypatch):
    with app.state.Session.begin() as s:s.add(Node(id='paid',provider_id='remote',name='paid',request_id='unique-paid-request',status='ACTIVE'))
    def terminate(*args):raise ProviderError('uncertain',uncertain=True)
    monkeypatch.setattr(YouGPU,'terminate',terminate)
    assert client.post('/api/resources/nodes/paid/terminate').status_code==502
    assert client.get('/api/resources/nodes').json()[0]['status']=='reconcile_required'


def test_cancel_does_not_terminate_rental(client,app,monkeypatch):
    with app.state.Session.begin() as s:s.add(Node(id='paid',provider_id='remote',name='paid',request_id='unique-paid-request',status='ACTIVE'))
    monkeypatch.setattr(YouGPU,'terminate',lambda *args:pytest.fail('Cancellation must not terminate'))
    j=gen(client,node_id='paid')
    assert client.post('/api/jobs/'+j['id']+'/cancel').status_code==200
    assert client.get('/api/resources/nodes').json()[0]['status']=='ACTIVE'


def test_archive_missing_rental_is_read_only(client,app,monkeypatch):
    with app.state.Session.begin() as s:s.add(Node(id='old',name='old',request_id='old-request-123',status='reconcile_required',hourly=186))
    monkeypatch.setattr(YouGPU,'instances',lambda self:[])
    monkeypatch.setattr(YouGPU,'terminate',lambda *a:pytest.fail('Archive must not terminate'))
    assert client.get('/api/resources/nodes').json()[0]['estimated_spend'] is None
    assert client.post('/api/resources/nodes/old/archive',json={}).status_code==400
    assert client.post('/api/resources/nodes/old/archive',json={'confirm':True}).json()['billing_action'] is False
    assert client.get('/api/resources/nodes').json()==[]


def test_archive_live_rental_refused(client,app,monkeypatch):
    with app.state.Session.begin() as s:s.add(Node(id='old',name='old',request_id='old-request-123',status='reconcile_required'))
    monkeypatch.setattr(YouGPU,'instances',lambda self:[{'id':'real','name':'old'}])
    assert client.post('/api/resources/nodes/old/archive',json={'confirm':True}).status_code==400


def test_terminate_resolves_missing_id(client,app,monkeypatch):
    with app.state.Session.begin() as s:s.add(Node(id='old',name='old',request_id='old-request-123',status='reconcile_required'))
    monkeypatch.setattr(YouGPU,'instances',lambda self:[{'id':'real','name':'old'}])
    monkeypatch.setattr(YouGPU,'instance',lambda self,id:{'status':'ACTIVE'})
    calls=[]
    monkeypatch.setattr(YouGPU,'terminate',lambda self,id:calls.append(id))
    assert client.post('/api/resources/nodes/old/terminate').status_code==200
    assert calls==['real']
