"""YouGPU integration for MayMay Studio 3.

GET requests may be retried. Paid POST requests are NEVER automatically retried.
A cached catalog is a display fallback, not confirmation of live availability.
"""

import logging
import random
import threading
import time
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    pass


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
                if not isinstance(obj, dict) or obj.get("status") == "error" or obj.get("code", 200) >= 400:
                    raise ProviderError(f"YouGPU rejected {method} {path}")
                return obj.get("data", obj)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                logger.warning("YouGPU %s %s attempt %s/%s: %s: %s",
                               method, path, index + 1, limit, type(exc).__name__, exc)
                if index == limit - 1 or not is_get:
                    if is_get:
                        raise ProviderError(f"YouGPU GET {path} failed: {type(exc).__name__}") from exc
                    raise ProviderError(
                        "YouGPU write outcome uncertain; check instance list before retrying"
                    ) from exc
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                logger.warning("YouGPU %s %s HTTP %s", method, path, code)
                if is_get and code in (429, 500, 502, 503, 504) and index < limit - 1:
                    pass
                else:
                    raise ProviderError(f"YouGPU HTTP {code} for {method} {path}") from exc
            except (httpx.InvalidURL, ValueError) as exc:
                raise ProviderError(f"Invalid YouGPU response for {method} {path}: {exc}") from exc
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
            if count < 1 or price < 0:
                return None
            loc = offer.get("location") or {}
            provider = offer.get("provider") or {}
            if not isinstance(loc, dict):
                loc = {}
            if not isinstance(provider, dict):
                provider = {}
            return {
                "id": offer["id"],
                "gpu": gpu["name"],
                "gpu_count": count,
                "vram_each": gpu["vram_gb"],
                "ram": offer.get("ram_gb") or 0,
                "disk": (offer.get("root_disk_gb") or 0) + (offer.get("ephemeral_disk_gb") or 0),
                "hourly": price,
                "currency": offer.get("currency") or "RUB",
                "region": loc.get("name") or loc.get("slug") or "",
                "provider": provider.get("name") or "",
                "images": offer.get("available_images") or [],
                "checked_at": time.time(),
            }
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Skipping malformed offer %s: %s", offer.get("id"), exc)
            return None

    def catalog(self):
        now = time.time()
        with self._lock:
            if self._cache is not None and now - self._cache_time < self.CATALOG_TTL:
                return [dict(item) for item in self._cache]

        deadline = time.monotonic() + self.CATALOG_BUDGET
        try:
            gpus = self.request("GET", "/catalog/gpus", deadline=deadline)
            if not isinstance(gpus, list):
                raise ProviderError("YouGPU GPU catalog is not a list")
            available = [g for g in gpus if isinstance(g, dict) and
                         int((g.get("stats") or {}).get("total_offers") or 0) > 0]
            results = []
            failed = []
            for gpu in available:
                if time.monotonic() >= deadline - 1:
                    failed.append("time budget")
                    break
                try:
                    offers = self.request("GET", "/catalog/offers/" + quote(str(gpu["id"]), safe=""),
                                          deadline=deadline)
                    if not isinstance(offers, list):
                        raise ProviderError("GPU offers payload is not a list")
                    for offer in offers:
                        item = self._normalize(gpu, offer)
                        if item is not None:
                            results.append(item)
                except (ProviderError, KeyError, TypeError, ValueError) as exc:
                    failed.append(gpu.get("name", "unknown"))
                    logger.warning("YouGPU offers unavailable for %s: %s", gpu.get("name"), exc)

            # A partial catalog is usable for display; do not overwrite good cached
            # offers with an empty result caused by network failure.
            if results or (not available and not failed):
                with self._lock:
                    self.__class__._cache = [dict(item) for item in results]
                    self.__class__._cache_time = time.time()
                if failed:
                    logger.warning("YouGPU catalog partial: %s", ", ".join(failed))
                return results
            raise ProviderError("YouGPU offers unavailable: " + ", ".join(failed))
        except ProviderError:
            with self._lock:
                if self._cache is not None and time.time() - self._cache_time < self.STALE_TTL:
                    logger.warning("Using cached YouGPU catalog due to provider failure")
                    return [dict(item) for item in self._cache]
            raise

    def instances(self):
        return self.request("GET", "/instances")

    def instance(self, id):
        return self.request("GET", "/instances/" + quote(str(id), safe=""))

    def create(self, payload):
        return self.request("POST", "/instances", payload)

    def terminate(self, id):
        return self.request("POST", "/instances/" + quote(str(id), safe="") + "/terminate")
