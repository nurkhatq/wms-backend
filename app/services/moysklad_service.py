import logging
import httpx
from app.config import settings

logger = logging.getLogger("wms.moysklad")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.moysklad_token}",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
    }


async def sync_demand(order_code: str) -> dict:
    """
    Find a customerOrder in MoySklad by code and create a demand if one doesn't exist.
    Returns:
      {"status": "CREATED",   "demand_id": "..."}
      {"status": "EXISTS",    "demand_id": "..."}
      {"status": "NOT_FOUND"}
      {"status": "SKIP"}      — MoySklad token not configured
      {"status": "ERROR",     "detail": "..."}
    """
    if not settings.moysklad_token:
        return {"status": "SKIP"}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # 1. Find customer order by kaspi order code
            r = await client.get(
                f"{settings.moysklad_api_url}/entity/customerorder",
                headers=_headers(),
                params={"filter": f"name={order_code}", "limit": 1},
            )
            r.raise_for_status()
            rows = r.json().get("rows", [])
            if not rows:
                return {"status": "NOT_FOUND"}

            order = rows[0]

            # 2. Already has a demand?
            demands = order.get("demands") or []
            if demands:
                demand_href = demands[0].get("meta", {}).get("href", "")
                demand_id = demand_href.split("/")[-1]
                logger.info(f"MoySklad: demand already exists for {order_code}: {demand_id}")
                return {"status": "EXISTS", "demand_id": demand_id}

            # 3. Get demand template
            r = await client.put(
                f"{settings.moysklad_api_url}/entity/demand/new",
                headers=_headers(),
                json={"customerOrder": {"meta": order["meta"]}},
            )
            r.raise_for_status()
            template = r.json()

            # 4. Create demand
            template["applicable"] = True
            r = await client.post(
                f"{settings.moysklad_api_url}/entity/demand",
                headers=_headers(),
                json=template,
            )
            r.raise_for_status()
            demand = r.json()
            demand_id = demand.get("id", "")
            logger.info(f"MoySklad: created demand {demand_id} for {order_code}")
            return {"status": "CREATED", "demand_id": demand_id}

    except httpx.HTTPStatusError as e:
        logger.warning(f"MoySklad HTTP error for {order_code}: {e.response.status_code} {e.response.text[:200]}")
        return {"status": "ERROR", "detail": str(e.response.status_code)}
    except Exception as e:
        logger.warning(f"MoySklad error for {order_code}: {e}")
        return {"status": "ERROR", "detail": str(e)[:100]}
