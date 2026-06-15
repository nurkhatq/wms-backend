import redis.asyncio as aioredis
from app.config import settings

_pool: aioredis.Redis | None = None


def get_redis_pool() -> aioredis.Redis:
    global _pool
    if _pool is None:
        _pool = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _pool


async def get_redis():
    yield get_redis_pool()
