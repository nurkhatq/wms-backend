"""
Windows Kaspi poller - runs locally every 5 min, pushes to VPS.
Schedule with: schtasks /create /tn "KaspiPoller" /tr "python d:\\otgruzka\\wms-backend\\kaspi_poller_windows.py" /sc minute /mo 5
"""
import requests
import datetime
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("kaspi_poller")

KASPI_TOKEN = "Kv/vZG305UvNBHVGbgHouHCsAaCnewqrwTkNUj27gvs="
KASPI_BASE = "https://kaspi.kz/shop/api/v2"

# VPS backend URL and sync token (must match SECRET_KEY in VPS .env)
VPS_URL = "http://194.238.41.18/api/v1/admin/sync"
VPS_SYNC_TOKEN_FILE = r"d:\otgruzka\wms-backend\vps_sync_token.txt"

KASPI_HEADERS = {
    "X-Auth-Token": KASPI_TOKEN,
    "Accept": "application/vnd.api+json, application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
}

ACTIVE_STATES = ["KASPI_DELIVERY", "PICKUP"]


def _date_window():
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now - datetime.timedelta(days=14)
    return int(start.timestamp() * 1000), int(now.timestamp() * 1000)


def _parse(raw: dict) -> dict:
    a = raw.get("attributes", {})
    kd = a.get("kaspiDelivery") or {}
    slot = a.get("deliverySlot") or {}
    origin = a.get("originAddress") or {}
    customer = a.get("customer") or {}
    ms = a.get("creationDate")
    creation = (
        datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).isoformat()
        if ms else None
    )
    return {
        "kaspi_order_code": str(a.get("code", "")),
        "kaspi_order_id": raw.get("id"),
        "kaspi_status": a.get("status", "UNKNOWN"),
        "kaspi_state": a.get("state"),
        "delivery_mode": a.get("deliveryMode"),
        "pickup_point_id": a.get("pickupPointId"),
        "origin_address_b64": origin.get("id"),
        "total_price": float(a.get("totalPrice") or 0),
        "customer_name": customer.get("name") or customer.get("firstName"),
        "customer_phone": customer.get("cellPhone"),
        "creation_date": creation,
        "delivery_slot_from": slot.get("from"),
        "delivery_slot_to": slot.get("to"),
        "waybill_number": kd.get("waybillNumber"),
        "express": bool(kd.get("express", False)),
        "assembled": bool(a.get("assembled", False)),
        "cancellation_reason": a.get("cancellationReason"),
        "products_json": [],
    }


def fetch_state(state: str) -> list:
    start_ms, end_ms = _date_window()
    orders = []
    page = 0
    while True:
        qs = (
            f"page[number]={page}&page[size]=100"
            f"&filter[orders][state]={state}"
            f"&filter[orders][creationDate][$ge]={start_ms}"
            f"&filter[orders][creationDate][$le]={end_ms}"
        )
        r = requests.get(f"{KASPI_BASE}/orders?{qs}", headers=KASPI_HEADERS, timeout=60)
        r.raise_for_status()
        data = r.json()
        batch = data.get("data", [])
        orders.extend(batch)
        total_pages = data.get("meta", {}).get("pageCount", 1)
        log.info(f"  state={state} page={page}/{total_pages} got={len(batch)}")
        if page >= total_pages - 1 or not batch:
            break
        page += 1
    return orders


def get_sync_token() -> str:
    try:
        with open(VPS_SYNC_TOKEN_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        log.error(f"Sync token file not found: {VPS_SYNC_TOKEN_FILE}")
        sys.exit(1)


CHUNK_SIZE = 500


def push_to_vps(orders: list, sync_token: str):
    synced_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    total_upserted = 0
    total_events = 0

    for i in range(0, max(len(orders), 1), CHUNK_SIZE):
        chunk = orders[i:i + CHUNK_SIZE]
        payload = {"orders": chunk, "synced_at": synced_at}
        r = requests.post(
            VPS_URL,
            json=payload,
            headers={"x-sync-token": sync_token},
            timeout=60,
        )
        r.raise_for_status()
        result = r.json()
        total_upserted += result.get("upserted", 0)
        total_events += result.get("events", 0)
        log.info(f"  chunk {i // CHUNK_SIZE + 1}: upserted={result.get('upserted')} events={result.get('events')}")

    return {"upserted": total_upserted, "events": total_events, "synced_at": synced_at}


def main():
    log.info("=== Kaspi Poller started ===")
    sync_token = get_sync_token()

    all_orders = []
    for state in ACTIVE_STATES:
        try:
            batch = fetch_state(state)
            parsed = [_parse(o) for o in batch if o.get("attributes", {}).get("code")]
            all_orders.extend(parsed)
            log.info(f"Fetched state={state}: {len(parsed)} orders")
        except Exception as e:
            log.error(f"Failed to fetch state={state}: {e}")

    if not all_orders:
        log.warning("No orders fetched — skipping VPS push")
        return

    log.info(f"Pushing {len(all_orders)} orders to VPS...")
    result = push_to_vps(all_orders, sync_token)
    log.info(f"VPS sync done: {result}")


if __name__ == "__main__":
    main()
