"""
Scan endpoints:
  GET  /scan/check/{code}       — check order status from MoySklad cache + Kaspi fallback
  POST /scan/create-demands     — create MoySklad demands for a list of confirmed orders
"""
import logging
import datetime
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.redis_client import get_redis
from app.models.kaspi_order import KaspiOrder
from app.models.scan_session import ScanSession
from app.models.scanned_order import ScannedOrder
from app.api.deps import get_current_user
from app.models.user import User
from app.services import moysklad_service
from app.config import settings

logger = logging.getLogger("wms.scan")
router = APIRouter(prefix="/scan", tags=["scan"])


# ─── Check ───────────────────────────────────────────────────────────────────

@router.get("/check/{order_code}")
async def check_order(
    order_code: str,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    user: User = Depends(get_current_user),
):
    """
    1. Check MoySklad Redis cache
    2. If not found → check Kaspi PostgreSQL (fallback)
    Returns order info + has_demand status.
    """
    # 1. MoySklad cache
    ms = await moysklad_service.get_cached(redis, order_code)
    if ms:
        # Enrich with Kaspi data if available
        kaspi = await db.scalar(
            select(KaspiOrder).where(KaspiOrder.kaspi_order_code == order_code)
        )
        return {
            "found": True,
            "source": "moysklad",
            "has_demand": ms["has_demand"],
            "demand_id": ms.get("demand_id"),
            "customer_name": kaspi.customer_name if kaspi else None,
            "total_price": float(kaspi.total_price or 0) if kaspi else 0.0,
            "assembled": kaspi.assembled if kaspi else False,
            "express": kaspi.express if kaspi else False,
            "is_cancelling": kaspi.is_cancelling if kaspi else False,
        }

    # 2. Kaspi fallback
    kaspi = await db.scalar(
        select(KaspiOrder).where(
            KaspiOrder.kaspi_order_code == order_code,
            KaspiOrder.warehouse_id == user.warehouse_id,
        )
    )
    if kaspi:
        return {
            "found": True,
            "source": "kaspi",
            "has_demand": False,
            "demand_id": None,
            "customer_name": kaspi.customer_name,
            "total_price": float(kaspi.total_price or 0),
            "assembled": kaspi.assembled,
            "express": kaspi.express,
            "is_cancelling": kaspi.is_cancelling,
        }

    return {"found": False, "source": "none", "has_demand": False}


# ─── Create demands ───────────────────────────────────────────────────────────

class CreateDemandsBody(BaseModel):
    codes: list[str]
    session_batch_id: str | None = None


@router.post("/create-demands")
async def create_demands(
    body: CreateDemandsBody,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    user: User = Depends(get_current_user),
):
    """
    For each order code:
      - Find in MoySklad cache (Redis)
      - If not found → NOT_IN_MS (MoySklad hasn't imported yet)
      - If already has demand → ALREADY_SHIPPED
      - Otherwise → create demand via MoySklad API
    """
    if not settings.moysklad_token:
        return {"results": [{"code": c, "status": "NO_MS_TOKEN"} for c in body.codes]}

    results = []

    # Find active session for history tracking
    session = None
    if body.session_batch_id:
        session = await db.scalar(
            select(ScanSession).where(
                ScanSession.batch_id == body.session_batch_id,
                ScanSession.status == "ACTIVE",
            )
        )

    for code in body.codes:
        ms = await moysklad_service.get_cached(redis, code)

        if not ms:
            results.append({"code": code, "status": "NOT_IN_MS"})
            continue

        if ms["has_demand"]:
            results.append({"code": code, "status": "ALREADY_SHIPPED", "demand_id": ms.get("demand_id")})
            continue

        try:
            result = await moysklad_service.create_demand(ms["meta"])

            # Update cache so subsequent checks know demand exists
            ms["has_demand"] = True
            ms["demand_id"] = result["demand_id"]
            await redis.set(f"wms:ms:{code}", __import__("json").dumps(ms), ex=900)

            # Record in session history
            if session:
                kaspi = await db.scalar(
                    select(KaspiOrder).where(KaspiOrder.kaspi_order_code == code)
                )
                if kaspi:
                    db.add(ScannedOrder(
                        session_id=session.id,
                        order_id=kaspi.id,
                        scanned_by=user.id,
                        scan_result="SUCCESS",
                    ))
                    from sqlalchemy import update
                    await db.execute(
                        update(ScanSession).where(ScanSession.id == session.id)
                        .values(order_count=ScanSession.order_count + 1)
                    )

            results.append({
                "code": code,
                "status": "CREATED",
                "demand_name": result["demand_name"],
                "demand_id": result["demand_id"],
            })

        except Exception as e:
            logger.warning(f"Create demand failed for {code}: {e}")
            results.append({"code": code, "status": "ERROR", "detail": str(e)[:100]})

    if session:
        await db.commit()

    return {"results": results}


# ─── Cache status ─────────────────────────────────────────────────────────────

@router.get("/cache-status")
async def cache_status(
    redis: aioredis.Redis = Depends(get_redis),
    _: User = Depends(get_current_user),
):
    loaded_at = await redis.get("wms:ms:loaded_at")
    return {"loaded_at": loaded_at, "token_configured": bool(settings.moysklad_token)}


@router.post("/cache-refresh")
async def trigger_cache_refresh(
    redis: aioredis.Redis = Depends(get_redis),
    _: User = Depends(get_current_user),
):
    count = await moysklad_service.refresh_cache(redis)
    return {"loaded": count}
