import httpx
from agent.main import Studio


def test_registration_omits_auth_but_worker_calls_and_multipart_keep_it(monkeypatch):
    actual_client=httpx.Client
    seen=[]
    def handle(request):
        seen.append(request)
        return httpx.Response(200,json={'ok':True})
    transport=httpx.MockTransport(handle)
    monkeypatch.setattr('agent.main.httpx.Client',lambda **kw:actual_client(transport=transport,**kw))
    Studio('https://studio.example').post('/worker/register',{'pairing_code':'one-use'})
    assert 'authorization' not in seen[-1].headers
    api=Studio('https://studio.example','worker-token')
    api.post('/worker/heartbeat',{})
    assert seen[-1].headers['authorization']=='Bearer worker-token'
    api.post('/worker/jobs/job/artifact/bytes',data={'ticket':'t'},files={'file':('f.txt',b'bytes')})
    assert seen[-1].headers['authorization']=='Bearer worker-token'
    assert 'multipart/form-data' in seen[-1].headers['content-type']
