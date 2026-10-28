"""YouGPU integration for MayMay Studio 3.

GET requests may be retried. Paid POST requests are NEVER automatically retried.
A cached catalog is a display fallback, not confirmation of live availability.
"""

import logging
import math
import uuid
import random
import threading
import time
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    def __init__(self, message, *, status_code=None, operation_id=None, uncertain=False):
        super().__init__(message)
        self.status_code=status_code
        self.operation_id=operation_id or uuid.uuid4().hex
        self.uncertain=uncertain
    def detail(self):
        return {"message":str(self),"provider_status":self.status_code,
                "operation_id":self.operation_id,"uncertain":self.uncertain}



class YouGPU:
    BASE = "https://api.yougpu.ru"
    GET_ATTEMPTS = 3
    GET_TIMEOUT = 8.0
    WRITE_TIMEOUT = 40.0
    CATALOG_BUDGET = 35.0
    CATALOG_TTL = 90.0
    STALE_TTL = 6 * 3600.0
    _cache = None
    _cache_time = 0.0
    _lock = threading.Lock()

    def __init__(self, key=""):
        self.key = key

    def request(self, method, path, payload=None, *, attempts=None, deadline=None):
        operation_id = uuid.uuid4().hex
        method = method.upper()
        is_get = method == "GET"
        limit = (self.GET_ATTEMPTS if is_get else 1) if attempts is None else max(1, int(attempts))
        if not is_get:
            limit = 1  # Never automatically repeat a paid operation.
        headers = {"X-API-KEY": self.key} if self.key else {}
        url = self.BASE + path

        for index in range(limit):
            timeout = self.GET_TIMEOUT if is_get else self.WRITE_TIMEOUT
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 1.0:
                    raise ProviderError("YouGPU catalog request time budget exceeded")
                timeout = min(timeout, remaining)
            try:
                with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                    response = client.request(method, url, headers=headers, json=payload)
                response.raise_for_status()
                obj = response.json()
                if not isinstance(obj, dict) or obj.get("status") == "error" or int(obj.get("code", 200)) >= 400:
                    raise ProviderError(f"YouGPU отклонил {method} {path}", status_code=response.status_code, operation_id=operation_id, uncertain=not is_get and response.status_code>=500)
                if "data" not in obj:
                    raise ValueError("missing data")
                return obj["data"]
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                logger.warning("YouGPU %s %s attempt %s/%s: %s: %s",
                               method, path, index + 1, limit, type(exc).__name__, "network failure")
                if index == limit - 1 or not is_get:
                    if is_get:
                        raise ProviderError(f"YouGPU: сеть недоступна ({type(exc).__name__}). Обновите состояние позже.", operation_id=operation_id) from exc
                    raise ProviderError(
                        "Результат операции YouGPU неизвестен. Нажмите «Сверить статус»; повторная аренда запрещена.", operation_id=operation_id, uncertain=True
                    ) from exc
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                logger.warning("YouGPU %s %s HTTP %s", method, path, code)
                if is_get and code in (429, 500, 502, 503, 504) and index < limit - 1:
                    pass
                else:
                    raise ProviderError(f"YouGPU HTTP {code}: запрос {method} {path} отклонён. Проверьте ключ, права и состояние в панели YouGPU.", status_code=code, operation_id=operation_id, uncertain=not is_get and code>=500) from exc
            except (httpx.InvalidURL, ValueError, TypeError) as exc:
                raise ProviderError(f"YouGPU вернул некорректный JSON/схему для {method} {path}.", operation_id=operation_id, uncertain=not is_get) from exc
            if index < limit - 1:
                delay = min(0.5 * (2 ** index) + random.uniform(0, 0.2), 2.0)
                if deadline is not None:
                    delay = min(delay, max(0.0, deadline - time.monotonic() - 1.0))
                if delay:
                    time.sleep(delay)
        raise ProviderError(f"YouGPU {method} {path} failed")

    @staticmethod
    def _normalize(gpu, offer):
        if not isinstance(offer, dict) or offer.get("is_active") is False:
            return None
        try:
            count = int(offer.get("gpu_count") or 1)
            price = float(offer["price"])
            if count < 1 or price < 0 or not math.isfinite(price):
                return None
            loc = offer.get("location") or {}
            provider = offer.get("provider") or {}
            if not isinstance(loc, dict):
                loc = {}
            if not isinstance(provider, dict):
                provider = {}
            return {
                "id": offer["id"],
                "in_stock": offer["in_stock"] if type(offer.get("in_stock")) is bool else (
                    True if (gpu.get('stats') or {}).get('in_stock_count',0)>0 and
                    (gpu.get('stats') or {}).get('in_stock_count')==(gpu.get('stats') or {}).get('total_offers') else None),
                "stale": False,
                "gpu": gpu["name"],
                "gpu_count": count,
                "vram_each": gpu["vram_gb"],
                "ram": offer.get("ram_gb") or 0,
                "disk": (offer.get("root_disk_gb") or 0) + (offer.get("ephemeral_disk_gb") or 0),
                "hourly": price,
                "currency": offer.get("currency") or "RUB",
                "region": offer.get("region") or loc.get("name") or loc.get("slug") or "",
                "provider": provider.get("name") or "",
                "images": offer.get("available_images") or [],
                "checked_at": time.time(),
            }
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Skipping malformed offer %s: %s", offer.get("id"), exc)
            return None

    def catalog(self, refresh=False, require_live=False):
        now = time.time()
        with self._lock:
            if not refresh and self._cache is not None and now - self._cache_time < self.CATALOG_TTL:
                return [dict(item) for item in self._cache]

        deadline = time.monotonic() + self.CATALOG_BUDGET
        try:
            gpus = self.request("GET", "/catalog/gpus", deadline=deadline)
            if not isinstance(gpus, list):
                raise ProviderError("YouGPU GPU catalog is not a list")
            available = [g for g in gpus if isinstance(g, dict) and
                         g.get("id") and g.get("name")]
            # Query families with known offers first; unknown stats are still queried.
            available.sort(key=lambda g: (0 if (g.get('stats') or {}).get('in_stock_count',0) else 1,0 if (g.get('stats') or {}).get('total_offers',0) else 1))
            results = []
            failed = []
            def fetch_offers(gpu):
                offers=self.request("GET", "/catalog/offers/"+quote(str(gpu["id"]),safe=""),deadline=deadline)
                if not isinstance(offers,list):raise ProviderError("GPU offers payload is not a list")
                return [item for offer in offers if (item:=self._normalize(gpu,offer)) is not None]
            # Bounded concurrency prevents one slow GPU family from consuming the
            # entire catalog budget before other families can be queried.
            with ThreadPoolExecutor(max_workers=4) as pool:
                pending={pool.submit(fetch_offers,g):g for g in available}
                for future in as_completed(pending):
                    gpu=pending[future]
                    try:results.extend(future.result())
                    except (ProviderError,KeyError,TypeError,ValueError):
                        failed.append(gpu.get("name","unknown"))
                        logger.warning("YouGPU offers unavailable for %s",gpu.get("name"))

            # A partial catalog is usable for display; do not overwrite good cached
            # offers with an empty result caused by network failure.
            if results or (not available and not failed):
                if failed:
                    results=[{**x,"partial":True,"failed_families":failed} for x in results]
                with self._lock:
                    self.__class__._cache = [dict(item) for item in results]
                    self.__class__._cache_time = time.time()
                if failed:
                    logger.warning("YouGPU catalog partial: %s", ", ".join(failed))
                return results
            raise ProviderError("YouGPU offers unavailable: " + ", ".join(failed))
        except ProviderError:
            with self._lock:
                if not require_live and self._cache is not None and time.time() - self._cache_time < self.STALE_TTL:
                    logger.warning("Using cached YouGPU catalog due to provider failure")
                    return [{**item,"stale":True} for item in self._cache]
            raise

    def balance(self):
        value=self.request("GET", "/billing/balance")
        if not isinstance(value,dict):raise ProviderError("Некорректный ответ баланса YouGPU")
        try:
            result={k:float(value[k]) for k in ("available","hourly_rate","pending","total")}
            if not all(math.isfinite(v) for v in result.values()):raise ValueError()
        except (KeyError,ValueError,TypeError):raise ProviderError("Неполный ответ баланса YouGPU")
        return result

    def instances(self):
        result=self.request("GET", "/instances")
        if isinstance(result,dict):result=result.get("items")
        if not isinstance(result,list) or any(not isinstance(x,dict) or not x.get("id") or not x.get("name") for x in result):
            raise ProviderError("Неполный список инстансов YouGPU; сверка не выполнена")
        return result

    def instance(self, id):
        result=self.request("GET", "/instances/" + quote(str(id), safe=""))
        if not isinstance(result,dict) or not result.get("status"):raise ProviderError("Неполное состояние инстанса YouGPU")
        return result

    def create(self, payload):
        result=self.request("POST", "/instances", payload)
        if not isinstance(result,dict) or not result.get("id"):
            raise ProviderError("YouGPU не вернул ID: требуется сверка", uncertain=True)
        return result

    def terminate(self, id):
        return self.request("POST", "/instances/" + quote(str(id), safe="") + "/terminate")
