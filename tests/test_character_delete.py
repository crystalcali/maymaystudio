import pytest
from fastapi.testclient import TestClient
from test_api import app,client,image_bytes
from maymay.api import create_app
from maymay.db import Adapter,Artifact,Job,KV


def character(client):
    return client.post('/api/characters',json={'name':'Vivi','trigger':'Vivi_model'}).json()['id']


def test_delete_character_confirmation_and_unrelated_survival(client):
    c=character(client)
    plan=client.get('/api/characters/'+c+'/deletion').json()
    assert plan['can_delete'] and plan['dependencies']=={'datasets':0,'adapters':0,'active_jobs':0,'history_jobs':0}
    assert client.delete('/api/characters/'+c).status_code==400
    assert client.delete('/api/characters/'+c+'?confirm=true').json()=={'ok':True,'objects_retained':True}
    assert [c['id'] for c in client.get('/api/characters').json()]==['may']
    assert client.get('/api/characters/'+c+'/deletion').status_code==404
    assert client.delete('/api/characters/'+c+'?confirm=true').status_code==404
    assert client.post('/api/datasets',json={'name':'stale','character_id':c}).status_code==404
    assert client.post('/api/adapters',json={'name':'stale','family':'krea2','source':'/weights/lora.safetensors','character_id':c}).status_code==404


@pytest.mark.parametrize('dependency',['dataset','adapter','queued','running','cancelling','connection_lost','succeeded','failed','cancelled'])
def test_character_delete_blocks_all_references(client,app,dependency):
    c=character(client)
    if dependency=='dataset':
        client.post('/api/datasets',json={'name':'Vivi photos','character_id':c})
    else:
        with app.state.Session.begin() as s:
            if dependency=='adapter':s.add(Adapter(name='Vivi LoRA',family='krea2',source='/weights/vivi.safetensors',character_id=c))
            else:s.add(Job(recipe='krea.generate',character_id=c,params={},status=dependency))
    plan=client.get('/api/characters/'+c+'/deletion').json()
    assert plan['can_delete'] is False and plan['blockers']
    r=client.delete('/api/characters/'+c+'?confirm=true')
    assert r.status_code==409
    assert r.json()['detail']=='; '.join(plan['blockers'])
    assert c in [x['id'] for x in client.get('/api/characters').json()]


def test_delete_rechecks_new_dependency_after_preview(client):
    c=character(client)
    assert client.get('/api/characters/'+c+'/deletion').json()['can_delete']
    client.post('/api/datasets',json={'name':'New dependency','character_id':c})
    assert client.delete('/api/characters/'+c+'?confirm=true').status_code==409


def test_delete_keeps_shared_reference_bytes_and_snapshots(client,app):
    c=character(client)
    key='shared-face';path=app.state.storage.path(key);path.write_bytes(image_bytes())
    with app.state.Session.begin() as s:
        s.add(Artifact(id='face',kind='image',name='face.png',key=key,sha='a'*64,size=path.stat().st_size,meta={}))
        s.add(KV(key='snapshot:old:version',value={'key':'frozen.zip','character_id':c}))
    client.patch('/api/characters/'+c,json={'face_id':'face'})
    client.patch('/api/characters/may',json={'face_id':'face'})
    assert client.delete('/api/characters/'+c+'?confirm=true').status_code==200
    assert path.exists()
    with app.state.Session() as s:
        assert s.get(Artifact,'face') is not None
        assert s.get(KV,'snapshot:old:version').value['key']=='frozen.zip'
    assert client.get('/api/characters').json()[0]['face_id']=='face'


def test_delete_seeded_may_does_not_recreate_on_restart(client,app):
    assert client.delete('/api/characters/may?confirm=true').status_code==200
    assert client.get('/api/characters').json()==[]
    restarted=create_app(app.state.settings)
    with TestClient(restarted) as c:
        c.post('/api/login',json={'password':'maymay-local'})
        assert c.get('/api/characters').json()==[]
        assert c.post('/api/characters',json={'name':'New character','trigger':'New_model'}).status_code==200


def test_character_delete_requires_owner_and_same_origin(app):
    with TestClient(app) as c:
        assert c.delete('/api/characters/may?confirm=true').status_code==401
        c.post('/api/login',json={'password':'maymay-local'})
        assert c.delete('/api/characters/may?confirm=true',headers={'Origin':'https://evil.example'}).status_code==403
