"""Cache-aside with Redis (Valkey running inside the EKS cluster) to cut read latency on PostgreSQL (RDS).

read:   value = redis.get(key); on a miss read SQL, then redis.set(key, value, ex=ttl)
write:  write SQL first, then delete the keys that the write made stale
If Redis is down the app keeps working (reads go straight to SQL) and the miss is counted.
"""

import json
import logging
import os

from prometheus_client import Counter

log = logging.getLogger("agripulse.cache")
HITS = Counter("agripulse_cache_hits_total", "Cache hits", ["key"])
MISSES = Counter("agripulse_cache_misses_total", "Cache misses (including Redis errors)", ["key"])
ERRORS = Counter("agripulse_cache_errors_total", "Redis errors")


def make_client():
    import redis

    host = os.environ.get("REDIS_HOST")
    if not host:
        return None
    # In-cluster Valkey: plain TCP on 6379 inside the namespace, reachable only from the web pods
    # (NetworkPolicy). REDIS_TLS=true for a managed Redis service that requires TLS.
    return redis.Redis(host=host, port=int(os.environ.get("REDIS_PORT", "6379")),
                       ssl=os.environ.get("REDIS_TLS", "false").lower() == "true",
                       password=os.environ.get("REDIS_PASSWORD"), socket_timeout=0.5, socket_connect_timeout=0.5,
                       decode_responses=True)


class Cache:
    def __init__(self, client):
        self.client = client

    def get_or_load(self, key: str, ttl: int, loader, label: str):
        if self.client is not None:
            try:
                cached = self.client.get(key)
                if cached is not None:
                    HITS.labels(label).inc()
                    return json.loads(cached)
            except Exception:
                ERRORS.inc()
                log.warning("redis read failed", exc_info=True)
        MISSES.labels(label).inc()
        value = loader()
        if self.client is not None:
            try:
                self.client.set(key, json.dumps(value, default=str), ex=ttl)
            except Exception:
                ERRORS.inc()
        return value

    def invalidate(self, *keys: str):
        if self.client is not None and keys:
            try:
                self.client.delete(*keys)
            except Exception:
                ERRORS.inc()
                log.warning("redis delete failed: stale data until TTL", exc_info=True)
