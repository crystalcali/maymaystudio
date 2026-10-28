"""Offline transport contracts: never send requests to paid YouGPU API."""
import httpx
import pytest
from maymay.provider import YouGPU, ProviderError

@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(YouGPU,'_cache',None)
    monkeypatch.setattr(YouGPU,'_cache_time',0)
    monkeypatch.setattr('maymay.provider.time.sleep',lambda _:None)

def transport(monkeypatch, handler):
    original=httpx.Client
    monkeypatch.setattr('maymay.provider.httpx.Client',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))

@pytest.mark.parametrize('code',[400,401,403,404,429,500,502,503,504])
def test_http_preserves_status_and_retry_policy(monkeypatch,code):
    calls=[]
    def handler(req):
        calls.append(req);assert req.headers['X-API-KEY']=='private'
        return httpx.Response(code,json={'message':'token=private'})
    transport(monkeypatch,handler)
    with pytest.raises(ProviderError) as err:YouGPU('private').request('GET','/catalog/gpus')
    assert len(calls)==(3 if code in [429,500,502,503,504] else 1)
    assert err.value.status_code==code and err.value.operation_id
    assert 'private' not in str(err.value)

@pytest.mark.parametrize('failure',['timeout','json','missing','502'])
def test_paid_write_never_retried(monkeypatch,failure):
    calls=[]
    def handler(req):
        calls.append(req)
        if failure=='timeout':raise httpx.ReadTimeout('secret',request=req)
        if failure=='json':return httpx.Response(200,text='broken')
        if failure=='missing':return httpx.Response(200,json={'status':'success'})
        return httpx.Response(502)
    transport(monkeypatch,handler)
    with pytest.raises(ProviderError) as err:YouGPU().create({'instance_name':'unique'})
    assert len(calls)==1 and err.value.uncertain

@pytest.mark.parametrize('payload',[[],{'data':[],'code':'invalid'},{'data':[],'code':None}])
def test_invalid_schema_is_diagnostic(monkeypatch,payload):
    transport(monkeypatch,lambda req:httpx.Response(200,json=payload))
    with pytest.raises(ProviderError):YouGPU().request('GET','/catalog/gpus')

def test_real_request_deadline_contract_and_missing_stats(monkeypatch):
    paths=[]
    def handler(req):
        paths.append(req.url.path)
        value=[{'id':'g','name':'A100','vram_gb':80}] if req.url.path.endswith('gpus') else [{'id':'o','price':'10','in_stock':True,'region':'R','available_images':[]}]
        return httpx.Response(200,json={'status':'success','code':200,'data':value})
    transport(monkeypatch,handler)
    p=YouGPU();assert p.catalog()[0]['region']=='R'
    assert len(paths)==2
    p.catalog();assert len(paths)==2
    p.catalog(refresh=True);assert len(paths)==4


def test_stale_cannot_be_used_for_rental(monkeypatch):
    import time
    monkeypatch.setattr(YouGPU,'_cache',[{'id':'o'}]);monkeypatch.setattr(YouGPU,'_cache_time',time.time()-100)
    transport(monkeypatch,lambda req:httpx.Response(503))
    assert YouGPU().catalog()[0]['stale']
    with pytest.raises(ProviderError):YouGPU().catalog(refresh=True,require_live=True)


def test_balance_contract(monkeypatch):
    transport(monkeypatch,lambda req:httpx.Response(200,json={'data':dict(available='120.50',hourly_rate='30',pending='30',total='150.50')}))
    assert YouGPU().balance()['available']==120.5


def test_get_timeout_recovers(monkeypatch):
    calls=[]
    def handler(req):
        calls.append(req)
        if len(calls)<3:raise httpx.ConnectTimeout('unreachable',request=req)
        return httpx.Response(200,json={'data':[]})
    transport(monkeypatch,handler)
    assert YouGPU().request('GET','/catalog/gpus')==[] and len(calls)==3


def test_partial_catalog_visible(monkeypatch):
    def handler(req):
        if req.url.path.endswith('gpus'):data=[{'id':'good','name':'A100','vram_gb':80},{'id':'bad','name':'H100','vram_gb':80}]
        elif req.url.path.endswith('/bad'):return httpx.Response(200,json={'data':{'wrong':'shape'}})
        else:data=[{'id':'o','price':'10','in_stock':False}]
        return httpx.Response(200,json={'data':data})
    transport(monkeypatch,handler)
    o=YouGPU().catalog()[0]
    assert o['partial'] and not o['in_stock']
