import time
from test_api import app,client,gen,worker
from maymay.api import supervise_once
from maymay.config import Settings
from maymay.db import Node,Worker,Job
from maymay.provider import YouGPU


def test_default_manual_and_paid_routes_disabled(client,app,monkeypatch):
    assert Settings().compute_mode=='manual'
    app.state.settings.compute_mode='manual'
    monkeypatch.setattr(YouGPU,'create',lambda *a:(_ for _ in ()).throw(AssertionError('paid create')))
    monkeypatch.setattr(YouGPU,'terminate',lambda *a:(_ for _ in ()).throw(AssertionError('terminate')))
    assert client.post('/api/resources/rent',json={}).status_code==410
    assert client.post('/api/resources/nodes/old/terminate').status_code==410


def test_manual_mode_preserves_lease_supervision_without_auto_terminate(client,app,monkeypatch):
    app.state.settings.compute_mode='manual'
    monkeypatch.setattr(YouGPU,'terminate',lambda *a:(_ for _ in ()).throw(AssertionError('must not terminate')))
    with app.state.Session.begin() as s:
        s.add(Node(id='old',request_id='legacy-policy-123',name='old',provider_id='paid',auto_terminate=True,status='ACTIVE',created=time.time()-3600))
        s.add(Job(id='expired',recipe='krea.generate',params={},status='running',lease=0))
    supervise_once(app)
    with app.state.Session() as s:
        assert s.get(Node,'old').status=='ACTIVE'
        assert s.get(Job,'expired').status=='connection_lost'


def test_manual_pair_claim_cancel_without_provider(client,app,monkeypatch):
    app.state.settings.compute_mode='manual'
    monkeypatch.setattr(YouGPU,'request',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('No provider calls for manual jobs')))
    pair=client.post('/api/workers/pair').json()
    assert pair['expires_in']==900 and pair['studio_url']=='http://testserver'
    auth=worker(client)
    j=gen(client)
    claimed=client.post('/worker/claim',headers=auth).json()['job']
    assert claimed['id']==j['id']
    assert client.post('/api/jobs/'+j['id']+'/cancel').json()['status']=='cancelling'
