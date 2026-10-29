"""Real CPU end-to-end protocol: upload → lease → FFmpeg worker → durable items."""
import socket
import subprocess
import threading
import time
import shutil
import json
from pathlib import Path
import pytest
import httpx
import uvicorn
from maymay.api import create_app
from maymay.config import Settings
from agent.main import Studio,execute

@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='ffmpeg required')
def test_cpu_worker_full_roundtrip(tmp_path,monkeypatch):
    monkeypatch.setenv('NO_PROXY','127.0.0.1,localhost')
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    url=f'http://127.0.0.1:{port}'
    app=create_app(Settings(database_url='sqlite:///'+str(tmp_path/'db'),data_dir=tmp_path/'store',public_url=url))
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        with httpx.Client(base_url=url,timeout=90) as c:
            assert c.post('/api/login',json={'password':'maymay-local'}).status_code==200
            ds=c.post('/api/datasets',json={'name':'Real clip test','character_id':'may'}).json()
            video=tmp_path/'original.mp4'
            subprocess.run(['ffmpeg','-loglevel','error','-y','-f','lavfi','-i','testsrc2=size=320x256:rate=16','-t','2.5','-c:v','libx264',str(video)],check=True)
            with video.open('rb') as f:
                assert c.post('/api/datasets/'+ds['id']+'/upload',files={'file':('original.mp4',f)}).status_code==200
            job=c.post('/api/jobs',json={'recipe':'dataset.video','params':{'dataset_id':ds['id'],'clip_seconds':1,'overlap':.25,'scene_cut':False}})
            assert job.status_code==200,job.text
            pair=c.post('/api/workers/pair').json()['pairing_code']
            # Exercise the real agent's unauthenticated registration transport.
            reg=Studio(url).post('/worker/register',{'pairing_code':pair,'capabilities':{'gpu':'CPU','recipes':['dataset.video']}})
            api=Studio(url,reg['token']);claim=api.post('/worker/claim')['job']
            execute(api,claim,{},tmp_path/'worker')
            latest=c.get('/api/jobs').json()[0]
            assert latest['status']=='succeeded',latest
            items=c.get('/api/datasets/'+ds['id']+'/items').json()
            clips=[i for i in items if not i['accepted']]
            assert len(clips)>=2
            assert len({i['group_id'] for i in items})==1
            assert c.get('/api/artifacts').json()==[]
            for clip in clips:
                assert c.get(clip['url']).status_code==200
    finally:
        server.should_exit=True;thread.join(timeout=10)
