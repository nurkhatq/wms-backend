import redis.asyncio as aioredis

LOCK_TTL = 86400  # 24 hours — workers scan all day, demand created at end of shift

_RELEASE_SCRIPT = """
local val = redis.call('GET', KEYS[1])
if val and string.find(val, ARGV[1]) == 1 then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


def _key(order_code: str) -> str:
    return f"wms:lock:order:{order_code}"


async def acquire(redis: aioredis.Redis, order_code: str,
                  tsd_code: str, employee_name: str, batch_id: str) -> tuple[bool, str | None]:
    value = f"{tsd_code}:{employee_name}:{batch_id}"
    acquired = await redis.set(_key(order_code), value, nx=True, ex=LOCK_TTL)
    if acquired:
        return True, value
    holder = await redis.get(_key(order_code))
    return False, holder


async def release(redis: aioredis.Redis, order_code: str, tsd_code: str) -> bool:
    result = await redis.eval(_RELEASE_SCRIPT, 1, _key(order_code), tsd_code)
    return bool(result)


async def get_holder(redis: aioredis.Redis, order_code: str) -> str | None:
    return await redis.get(_key(order_code))
