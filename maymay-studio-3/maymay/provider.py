"""YouGPU contract adapter. Never automatically retry a paid create request."""
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
import httpx

class ProviderError(Exception):pass
class YouGPU:
    BASE='https://api.yougpu.ru'
    def __init__(self,key=''):self.key=key
    def request(self,method,path,payload=None):
        try:
            with httpx.Client(timeout=40) as c:
                r=c.request(method,self.BASE+path,headers={'X-API-KEY':self.key} if self.key else {},json=payload)
            if r.status_code>=400:raise ProviderError(f'YouGPU HTTP {r.status_code}: check access, balance and request in provider console')
            obj=r.json()
            if obj.get('status')=='error':raise ProviderError('YouGPU rejected request')
            return obj.get('data',obj)
        except (httpx.TimeoutException,httpx.NetworkError) as e:raise ProviderError('YouGPU connection uncertain; reconcile instance list before retrying creation') from e
    def catalog(self):
        gpus=self.request('GET','/catalog/gpus')
        def fetch(g):
            result=[]
            for o in self.request('GET','/catalog/offers/'+quote(g['id'],safe='')):
                if not o.get('in_stock'):continue
                count=int(o.get('gpu_count',1))
                result.append({'id':o['id'],'gpu':g['name'],'gpu_count':count,'vram_each':g['vram_gb'],
                   'ram':o.get('ram_gb',0),'disk':o.get('root_disk_gb',0)+o.get('ephemeral_disk_gb',0),
                   'hourly':float(o['price']),'currency':o.get('currency','RUB'),'region':o.get('region',''),
                   'provider':o.get('provider',{}).get('name',''),'images':o.get('available_images',[]),'checked_at':time.time()})
            return result
        with ThreadPoolExecutor(max_workers=6) as pool:
            return [o for offers in pool.map(fetch,gpus) for o in offers]
    def instances(self):return self.request('GET','/instances')
    def instance(self,id):return self.request('GET','/instances/'+quote(id,safe=''))
    def create(self,payload):return self.request('POST','/instances',payload)
    def terminate(self,id):return self.request('POST','/instances/'+quote(id,safe='')+'/terminate')
