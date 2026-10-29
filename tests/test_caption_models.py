from test_api import app,client,image_bytes,worker


def test_caption_model_selection_and_revision_pin(client,monkeypatch):
    d=client.post('/api/datasets',json={'name':'test','character_id':'may'}).json()['id']
    repo='huihui-ai/Qwen2.5-VL-7B-Instruct-abliterated'
    assert client.put(f'/api/datasets/{d}/settings',json={'captioner':'untrusted/model'}).status_code==400
    assert client.put(f'/api/datasets/{d}/settings',json={'captioner':repo}).json()['captioner']==repo
    client.post(f'/api/datasets/{d}/upload',files={'file':('image.png',image_bytes())})
    calls=[]
    def inspect(spec,token):
        calls.append(spec)
        return {'ok':True,'components':{'caption_model':{'spec':{'repo':repo,'revision':'a'*40}}}}
    monkeypatch.setattr('maymay.api.inspect_components',inspect)
    j=client.post('/api/jobs',json={'recipe':'dataset.caption','character_id':'may','params':{'dataset_id':d,'caption_model':{'repo':'untrusted/model'}}})
    assert j.status_code==200,j.text
    assert calls[0]['caption_model']['repo']==repo
    assert j.json()['params']['caption_model']=={'repo':repo,'revision':'a'*40}
