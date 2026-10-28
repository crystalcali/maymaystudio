"""YouGPU contract adapter. Never automatically retry a paid create request."""
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
import httpx

logger = logging.getLogger(__name__)

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
        gpus = self.request('GET', '/catalog/gpus')
        available = [
            gpu for gpu in gpus
            if int((gpu.get('stats') or {}).get('total_offers') or 0) > 0
        ]
        if not available:
            return []

        def fetch(gpu):
            result = []
            offers = self.request('GET', '/catalog/offers/' + quote(gpu['id'], safe=''))
            for offer in offers:
                if not offer.get('in_stock'):
                    continue
                try:
                    count = int(offer.get('gpu_count') or 1)
                    result.append({
                        'id': offer['id'], 'gpu': gpu['name'], 'gpu_count': count,
                        'vram_each': gpu['vram_gb'], 'ram': offer.get('ram_gb') or 0,
                        'disk': (offer.get('root_disk_gb') or 0) + (offer.get('ephemeral_disk_gb') or 0),
                        'hourly': float(offer['price']),
                        'currency': offer.get('currency') or 'RUB',
                        'region': offer.get('region') or '',
                        'provider': (offer.get('provider') or {}).get('name', ''),
                        'images': offer.get('available_images') or [],
                        'checked_at': time.time(),
                    })
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning('Skipping malformed YouGPU offer %s: %s', offer.get('id'), exc)
            return result

        result = []
        failures = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [(gpu, pool.submit(fetch, gpu)) for gpu in available]
            for gpu, future in futures:
                try:
                    result.extend(future.result())
                except (ProviderError, httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                    failures.append(gpu['name'])
                    logger.warning('YouGPU offers unavailable for %s: %s', gpu['name'], exc)

        if failures and not result:
            raise ProviderError('YouGPU offers unavailable for all queried GPUs: ' + ', '.join(failures))
        if failures:
            logger.warning('YouGPU catalog is partial; failed GPU models: %s', ', '.join(failures))
        return result
    def instances(self):return self.request('GET','/instances')
    def instance(self,id):return self.request('GET','/instances/'+quote(id,safe=''))
    def create(self,payload):return self.request('POST','/instances',payload)
    def terminate(self,id):return self.request('POST','/instances/'+quote(id,safe='')+'/terminate')
