import datetime
import httpx
from app.config import settings

BASE_URL = settings.kaspi_api_base_url
HEADERS = {
    "X-Auth-Token": settings.kaspi_api_token,
    "Accept": "application/vnd.api+json, application/json",
    "Content-Type": "application/vnd.api+json",
}
ACTIVE_STATES = ["KASPI_DELIVERY", "PICKUP"]


def _date_window() -> tuple[int, int]:
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now - datetime.timedelta(days=settings.kaspi_lookback_days)
    return int(start.timestamp() * 1000), int(now.timestamp() * 1000)


async def fetch_orders(state: str, client: httpx.AsyncClient) -> list[dict]:
    start_ms, end_ms = _date_window()
    orders = []
    page = 0
    while True:
        # httpx encodes brackets — build raw query string to preserve them
        qs = (
            f"page[number]={page}&page[size]=100"
            f"&filter[orders][state]={state}"
            f"&filter[orders][creationDate][$ge]={start_ms}"
            f"&filter[orders][creationDate][$le]={end_ms}"
        )
        r = await client.get(f"{BASE_URL}/orders?{qs}", headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
        batch = data.get("data", [])
        orders.extend(batch)
        meta = data.get("meta", {})
        total_pages = meta.get("pageCount", 1)
        if page >= total_pages - 1 or not batch:
            break
        page += 1
    return orders


async def fetch_all_active() -> list[dict]:
    async with httpx.AsyncClient() as client:
        results = []
        for state in ACTIVE_STATES:
            try:
                batch = await fetch_orders(state, client)
                results.extend(batch)
            except Exception as e:
                import logging, traceback
                logging.error(f"Kaspi fetch error state={state}: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        return results
