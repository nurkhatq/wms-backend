"""
MoySklad integration:
  - refresh_cache(redis)       : load all recent customerorders into Redis
  - get_cached(redis, code)    : lookup one order from cache
  - create_demand(order_meta)  : create a demand (отгрузка) via API
"""
import json
import datetime
import logging
import httpx
from app.config import settings

logger = logging.getLogger("wms.moysklad")

CACHE_TTL = 90000      # 25 hours — refreshed once at start of day + after each demand
LOOKBACK_DAYS = 14


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.moysklad_token}",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
    }


def _key(code: str) -> str:
    return f"wms:ms:{code}"


async def refresh_cache(redis) -> int:
    """Fetch all customerorders from MoySklad and store in Redis. Returns order count."""
    if not settings.moysklad_token:
        return 0

    date_from = (
        datetime.datetime.now() - datetime.timedelta(days=LOOKBACK_DAYS)
    ).strftime("%Y-%m-%d")

    page_size = 1000
    offset = 0
    total_loaded = 0

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                r = await client.get(
                    f"{settings.moysklad_api_url}/entity/customerorder",
                    headers=_headers(),
                    params={
                        "filter": f"moment>={date_from} 00:00:00",
                        "limit": page_size,
                        "offset": offset,
                    },
                )
                r.raise_for_status()
            except Exception as e:
                logger.error(f"MoySklad refresh error at offset {offset}: {e}")
                break

            data = r.json()
            rows = data.get("rows", [])
            if not rows:
                break

            pipe = redis.pipeline()
            for order in rows:
                code = order.get("name", "").strip()
                if not code:
                    continue
                demands = order.get("demands") or []
                has_demand = bool(demands)
                demand_id = demands[0]["meta"]["href"].split("/")[-1] if has_demand else None
                value = json.dumps({
                    "meta": order["meta"],
                    "has_demand": has_demand,
                    "demand_id": demand_id,
                })
                pipe.set(_key(code), value, ex=CACHE_TTL)
                total_loaded += 1
            await pipe.execute()

            total = data.get("meta", {}).get("size", 0)
            offset += page_size
            if offset >= total:
                break

    if total_loaded:
        await redis.set("wms:ms:loaded_at", datetime.datetime.now().isoformat(), ex=CACHE_TTL)
        logger.info(f"MoySklad cache refreshed: {total_loaded} orders")
    return total_loaded


async def get_cached(redis, code: str) -> dict | None:
    """Return cached MoySklad order dict. Falls back to direct API call on cache miss."""
    raw = await redis.get(_key(code))
    if raw:
        return json.loads(raw)

    if not settings.moysklad_token:
        return None

    # Cache miss — look up this specific order directly in MoySklad
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{settings.moysklad_api_url}/entity/customerorder",
                headers=_headers(),
                params={"filter": f"name={code}", "limit": 1},
            )
            r.raise_for_status()
            rows = r.json().get("rows", [])
            if not rows:
                return None
            order = rows[0]
            demands = order.get("demands") or []
            has_demand = bool(demands)
            demand_id = demands[0]["meta"]["href"].split("/")[-1] if has_demand else None
            value = {
                "meta": order["meta"],
                "has_demand": has_demand,
                "demand_id": demand_id,
            }
            await redis.set(_key(code), json.dumps(value), ex=CACHE_TTL)
            return value
    except Exception as e:
        logger.warning(f"Direct MS lookup failed for {code}: {e}")
        return None


async def create_demand(order_meta: dict) -> dict:
    """
    Create a demand (отгрузка) in MoySklad using the order meta object.
    Returns {"status": "CREATED", "demand_name": "...", "demand_id": "..."}
    Raises httpx.HTTPStatusError on API failure.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        # Get demand template
        r = await client.put(
            f"{settings.moysklad_api_url}/entity/demand/new",
            headers=_headers(),
            json={"customerOrder": {"meta": order_meta}},
        )
        r.raise_for_status()
        template = r.json()
        template["applicable"] = True

        # Create demand
        r = await client.post(
            f"{settings.moysklad_api_url}/entity/demand",
            headers=_headers(),
            json=template,
        )
        r.raise_for_status()
        demand = r.json()
        return {
            "status": "CREATED",
            "demand_name": demand.get("name", ""),
            "demand_id": demand.get("id", ""),
        }
