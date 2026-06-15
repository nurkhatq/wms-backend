"""
Scan endpoints:
  POST /scan/lock              — check MS cache + Kaspi Redis/Postgres fallback + acquire Redis lock atomically
  DELETE /scan/lock/{code}     — release lock (on × button or Стереть)
  POST /scan/create-demands    — create MoySklad demands for locked orders, release locks
  POST /scan/cache-refresh     — manually trigger MoySklad cache reload
  GET  /scan/cache-status      — show when cache was last loaded
"""
import json
import logging
import uuid
import datetime
import redis.asyncio as aioredis
from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.database import get_db, AsyncSessionLocal
from app.redis_client import get_redis, get_redis_pool
from app.models.kaspi_order import KaspiOrder
from app.models.scan_session import ScanSession
from app.models.scanned_order import ScannedOrder
from app.models.tsd_device import TsdDevice
from app.models.user import User
from app.api.deps import get_current_user
from app.services import lock_service, moysklad_service
from app.config import settings

logger = logging.getLogger("wms.scan")
router = APIRouter(prefix="/scan", tags=["scan"])


# ─── Lock (scan) ─────────────────────────────────────────────────────────────

class LockRequest(BaseModel):
    order_code: str
    session_batch_id: str


@router.post("/lock")
async def scan_lock(
    body: LockRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    user: User = Depends(get_current_user),
):
    """
    Unified scan endpoint:
    1. Check MoySklad Redis cache → already shipped?
    2. Check Kaspi Redis cache (wms:order:status:{code}) → is_cancelling? order info?
       Fallback to Kaspi PostgreSQL if Redis miss (for older orders)
    3. Acquire Redis lock atomically (SET NX)
    4. Always save ScannedOrder (SUCCESS, ALREADY_LOCKED, ALREADY_SHIPPED, CANCELLING, NOT_FOUND)
    """
    code = body.order_code

    # 1. MoySklad cache (Redis)
    ms = await moysklad_service.get_cached(redis, code)

    # 2a. Kaspi Redis cache
    kaspi_redis = await redis.hgetall(f"wms:order:status:{code}")
    # hgetall returns bytes keys/values — decode them
    if kaspi_redis:
        kaspi_redis = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in kaspi_redis.items()
        }

    # 2b. Postgres fallback if Redis miss
    kaspi_db = None
    if not kaspi_redis:
        kaspi_db = await db.scalar(
            select(KaspiOrder).where(
                KaspiOrder.kaspi_order_code == code,
                KaspiOrder.warehouse_id == user.warehouse_id,
            )
        )

    # Determine order info for response
    def _order_info_from_redis(r: dict) -> dict:
        return {
            "customer_name": r.get("customer_name") or None,
            "total_price": float(r.get("total_price") or 0),
            "assembled": r.get("assembled") == "1",
            "express": r.get("express") == "1",
            "source": "kaspi_redis",
        }

    def _order_info_from_db(k: KaspiOrder) -> dict:
        return {
            "customer_name": k.customer_name,
            "total_price": float(k.total_price or 0),
            "assembled": k.assembled,
            "express": k.express,
            "source": "kaspi_db",
        }

    # Look up session first — needed for all paths that save a ScannedOrder
    session = await db.scalar(
        select(ScanSession).where(
            ScanSession.batch_id == body.session_batch_id,
            ScanSession.status == "ACTIVE",
        )
    )

    # Resolve TSD code
    tsd_code = user.username
    if user.tsd_device_id:
        device = await db.scalar(select(TsdDevice).where(TsdDevice.id == user.tsd_device_id))
        if device:
            tsd_code = device.device_code
            await db.execute(
                update(TsdDevice).where(TsdDevice.id == device.id)
                .values(last_seen_at=datetime.datetime.now(datetime.timezone.utc))
            )

    # Helper: save ScannedOrder for any terminal result
    async def _save_scan(scan_result: str, order_id: int | None = None, lock_holder_val: str | None = None):
        if session:
            db.add(ScannedOrder(
                session_id=session.id,
                order_id=order_id,
                order_code=code,
                scanned_by=user.id,
                tsd_device_id=user.tsd_device_id,
                scan_result=scan_result,
                lock_holder=lock_holder_val,
            ))
            if scan_result == "SUCCESS":
                await db.execute(
                    update(ScanSession).where(ScanSession.id == session.id)
                    .values(order_count=ScanSession.order_count + 1)
                )
        await db.commit()

    # Already shipped?
    if ms and ms["has_demand"]:
        await _save_scan("ALREADY_SHIPPED", order_id=kaspi_db.id if kaspi_db else None)
        return {
            "result": "ALREADY_SHIPPED",
            "order_code": code,
            "lock_acquired": False,
        }

    # Cancelling?
    is_cancelling = False
    if kaspi_redis:
        is_cancelling = kaspi_redis.get("is_cancelling") == "1"
    elif kaspi_db:
        is_cancelling = kaspi_db.is_cancelling

    if is_cancelling:
        await _save_scan("CANCELLING", order_id=kaspi_db.id if kaspi_db else None)
        return {
            "result": "CANCELLING",
            "order_code": code,
            "lock_acquired": False,
            "message": "Заказ в процессе отмены.",
        }

    # Not found in Kaspi at all (and no MoySklad record either)?
    if not ms and not kaspi_redis and not kaspi_db:
        await _save_scan("NOT_FOUND")
        return {"result": "NOT_FOUND", "order_code": code, "lock_acquired": False}

    # 3. Attempt Redis lock
    acquired, holder = await lock_service.acquire(
        redis, code, tsd_code, user.full_name, body.session_batch_id
    )

    kaspi_order_id = kaspi_db.id if kaspi_db else None

    if not acquired:
        await _save_scan("ALREADY_LOCKED", order_id=kaspi_order_id, lock_holder_val=holder)
        return {
            "result": "ALREADY_LOCKED",
            "order_code": code,
            "lock_acquired": False,
            "lock_holder": holder,
        }

    await _save_scan("SUCCESS", order_id=kaspi_order_id)

    # Build order info
    if kaspi_redis:
        order_info = _order_info_from_redis(kaspi_redis)
    elif kaspi_db:
        order_info = _order_info_from_db(kaspi_db)
    else:
        # ms-only order (Kaspi not found but MoySklad knows about it)
        order_info = {
            "customer_name": None,
            "total_price": 0.0,
            "assembled": False,
            "express": False,
            "source": "moysklad",
        }

    return {
        "result": "SUCCESS",
        "order_code": code,
        "lock_acquired": True,
        "lock_holder": f"{tsd_code}:{user.full_name}",
        "order": order_info,
    }


# ─── Release lock ─────────────────────────────────────────────────────────────

@router.delete("/lock/{order_code}")
async def release_lock(
    order_code: str,
    redis: aioredis.Redis = Depends(get_redis),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tsd_code = user.username
    if user.tsd_device_id:
        device = await db.scalar(select(TsdDevice).where(TsdDevice.id == user.tsd_device_id))
        if device:
            tsd_code = device.device_code
    released = await lock_service.release(redis, order_code, tsd_code)
    return {"released": released}


# ─── Create demands ───────────────────────────────────────────────────────────

_JOB_TTL = 7200  # 2 hours — enough for any shift


class CreateDemandsBody(BaseModel):
    codes: list[str]
    session_batch_id: str | None = None


async def _update_scan_demand(db: AsyncSession, session_id: int | None, code: str,
                               status: str, name: str | None = None):
    if not session_id:
        return
    so = await db.scalar(
        select(ScannedOrder).where(
            ScannedOrder.order_code == code,
            ScannedOrder.session_id == session_id,
        )
    )
    if so:
        so.demand_status = status
        if name is not None:
            so.demand_name = name
        await db.flush()


async def _run_demands_bg(job_id: str, codes: list[str], session_batch_id: str | None,
                           tsd_code: str):
    redis = get_redis_pool()
    results = []
    any_created = False

    try:
        async with AsyncSessionLocal() as db:
            session_id = None
            if session_batch_id:
                s = await db.scalar(
                    select(ScanSession).where(ScanSession.batch_id == session_batch_id)
                )
                if s:
                    session_id = s.id

            for i, code in enumerate(codes):
                try:
                    ms = await moysklad_service.get_cached(redis, code)

                    if not ms:
                        await lock_service.release(redis, code, tsd_code)
                        await _update_scan_demand(db, session_id, code, "NOT_IN_MS")
                        await db.commit()
                        results.append({"code": code, "status": "NOT_IN_MS"})

                    elif ms["has_demand"]:
                        await lock_service.release(redis, code, tsd_code)
                        results.append({"code": code, "status": "ALREADY_SHIPPED"})

                    else:
                        result = await moysklad_service.create_demand(ms["meta"])
                        ms["has_demand"] = True
                        ms["demand_id"] = result["demand_id"]
                        await redis.set(f"wms:ms:{code}", json.dumps(ms), ex=moysklad_service.CACHE_TTL)
                        await lock_service.release(redis, code, tsd_code)
                        await _update_scan_demand(db, session_id, code, "CREATED", result["demand_name"])
                        await db.commit()
                        any_created = True
                        results.append({"code": code, "status": "CREATED", "demand_name": result["demand_name"]})

                except Exception as e:
                    logger.warning(f"Demand failed for {code}: {e}")
                    try:
                        await _update_scan_demand(db, session_id, code, "ERROR")
                        await db.commit()
                    except Exception:
                        pass
                    results.append({"code": code, "status": "ERROR", "detail": str(e)[:100]})

                # Update progress after each order
                await redis.set(f"wms:job:{job_id}", json.dumps({
                    "status": "PROCESSING", "done": i + 1,
                    "total": len(codes), "results": results,
                }), ex=_JOB_TTL)

        await redis.set(f"wms:job:{job_id}", json.dumps({
            "status": "DONE", "done": len(codes),
            "total": len(codes), "results": results,
        }), ex=_JOB_TTL)

    except Exception as e:
        logger.error(f"Background demand job {job_id} crashed: {e}")
        await redis.set(f"wms:job:{job_id}", json.dumps({
            "status": "ERROR", "done": len(results),
            "total": len(codes), "results": results,
        }), ex=_JOB_TTL)

    if any_created:
        import asyncio
        asyncio.ensure_future(moysklad_service.refresh_cache(redis))


@router.post("/create-demands")
async def create_demands(
    body: CreateDemandsBody,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    user: User = Depends(get_current_user),
):
    if not settings.moysklad_token:
        return {"job_id": None, "status": "ERROR", "total": 0, "done": 0,
                "results": [{"code": c, "status": "NO_MS_TOKEN"} for c in body.codes]}

    tsd_code = user.username
    if user.tsd_device_id:
        device = await db.scalar(select(TsdDevice).where(TsdDevice.id == user.tsd_device_id))
        if device:
            tsd_code = device.device_code

    job_id = uuid.uuid4().hex[:12]
    total = len(body.codes)

    await redis.set(f"wms:job:{job_id}", json.dumps({
        "status": "PROCESSING", "done": 0, "total": total, "results": [],
    }), ex=_JOB_TTL)

    background_tasks.add_task(
        _run_demands_bg, job_id, body.codes, body.session_batch_id, tsd_code
    )

    return {"job_id": job_id, "status": "PROCESSING", "total": total, "done": 0, "results": None}


@router.get("/demand-job/{job_id}")
async def get_demand_job(
    job_id: str,
    redis: aioredis.Redis = Depends(get_redis),
    _: User = Depends(get_current_user),
):
    raw = await redis.get(f"wms:job:{job_id}")
    if not raw:
        return {"job_id": job_id, "status": "NOT_FOUND", "done": 0, "total": 0, "results": None}
    data = json.loads(raw)
    return {"job_id": job_id, **data}


# ─── Cache management ─────────────────────────────────────────────────────────

@router.post("/cache-refresh")
async def trigger_cache_refresh(
    redis: aioredis.Redis = Depends(get_redis),
    _: User = Depends(get_current_user),
):
    count = await moysklad_service.refresh_cache(redis)
    return {"loaded": count}


@router.get("/cache-status")
async def cache_status(
    redis: aioredis.Redis = Depends(get_redis),
    _: User = Depends(get_current_user),
):
    loaded_at = await redis.get("wms:ms:loaded_at")
    return {
        "loaded_at": loaded_at,
        "token_configured": bool(settings.moysklad_token),
    }
