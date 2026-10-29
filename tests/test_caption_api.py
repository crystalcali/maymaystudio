from test_api import app,client,image_bytes,worker
from maymay.db import Job


def test_failed_caption_preserves_text_and_manual_edit_clears_warning(client,app):
    ds=client.post('/api/datasets',json={'name':'captions','character_id':'may'}).json()['id']
    client.post(f'/api/datasets/{ds}/upload',files={'file':('photo.png',image_bytes())})
    item=client.get(f'/api/datasets/{ds}/items').json()[0]
    original='MayMay_model, a woman standing outdoors in daylight.'
    client.patch('/api/items/'+item['id'],json={'caption':original})
    with app.state.Session.begin() as s:
        s.add(Job(id='caption-test',recipe='dataset.caption',character_id='may',params={'dataset_id':ds}))
    auth=worker(client)
    claim=client.post('/worker/claim',headers=auth).json()['job']
    result=client.post('/worker/jobs/caption-test/finish',headers=auth,json={
        'attempt':claim['attempt'],'status':'succeeded','captions':[{
            'id':item['id'],'quality':{'caption_status':'failed','caption_error':'Invalid JSON','caption_review_required':True}}]})
    assert result.status_code==200,result.text
    assert client.get('/api/jobs').json()[0]['stage']=='Нужна проверка описаний: 1'
    current=client.get(f'/api/datasets/{ds}/items').json()[0]
    assert current['caption']==original
    assert current['quality']['caption_error']=='Invalid JSON'
    edit=client.patch('/api/items/'+item['id'],json={'caption':original+' Trees in the background.'}).json()
    assert not edit['quality']['caption_error']
    assert edit['quality']['caption_review_required'] is False


def test_caption_callback_cannot_modify_another_dataset(client,app):
    a=client.post('/api/datasets',json={'name':'A','character_id':'may'}).json()['id']
    b=client.post('/api/datasets',json={'name':'B','character_id':'may'}).json()['id']
    client.post(f'/api/datasets/{b}/upload',files={'file':('photo.png',image_bytes())})
    item=client.get(f'/api/datasets/{b}/items').json()[0]
    with app.state.Session.begin() as s:
        s.add(Job(id='caption-test',recipe='dataset.caption',character_id='may',params={'dataset_id':a}))
    auth=worker(client);claim=client.post('/worker/claim',headers=auth).json()['job']
    r=client.post('/worker/jobs/caption-test/finish',headers=auth,json={
        'attempt':claim['attempt'],'status':'succeeded','captions':[{'id':item['id'],'caption':'wrong dataset'}]})
    assert r.status_code==200
    assert client.get(f'/api/datasets/{b}/items').json()[0]['caption']==item['caption']
