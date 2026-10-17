"""Small fixed-window rate limiter. Redis in production (shared across web workers); in-memory otherwise and
whenever Redis is unreachable, so a Redis outage weakens the limit instead of taking sign-in down."""
import logging
import time

from flask import current_app

log = logging.getLogger("rlk.ratelimit")


class MemoryStore:
    def __init__(self):
        self.hits: dict[str, tuple[float, int]] = {}

    def hit(self, key: str, window: int) -> int:
        now = time.monotonic()
        start, n = self.hits.get(key, (now, 0))
        if now - start >= window:
            start, n = now, 0
        self.hits[key] = (start, n + 1)
        return n + 1


class RedisStore:
    def __init__(self, url: str, fallback: MemoryStore):
        from redis import Redis
        self.r, self.fallback = Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5), fallback

    def hit(self, key: str, window: int) -> int:
        try:
            pipe = self.r.pipeline()
            pipe.incr(key)
            pipe.expire(key, window, nx=True)
            return int(pipe.execute()[0])
        except Exception as e:  # redis down: degrade to per-process counting
            log.warning("rate limit store unavailable (%s); using memory", e)
            return self.fallback.hit(key, window)


def _store():
    ext = current_app.extensions
    if "ratelimit" not in ext:
        mem = MemoryStore()
        url = current_app.config.get("REDIS_URL")
        ext["ratelimit"] = mem if current_app.config.get("TESTING") or not url else RedisStore(url, mem)
    return ext["ratelimit"]


def allow(name: str, ident: str, limit: int, window_s: int) -> bool:
    """True if this caller is within `limit` requests per `window_s` seconds for the named action."""
    if not current_app.config.get("RATELIMIT_ENABLED", True):
        return True
    return _store().hit(f"rl:{name}:{ident}", window_s) <= limit
