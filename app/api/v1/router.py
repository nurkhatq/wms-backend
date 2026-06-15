from fastapi import APIRouter
from app.api.v1 import auth, orders, scan, sessions

router = APIRouter(prefix="/api/v1")
router.include_router(auth.router)
router.include_router(orders.router)
router.include_router(scan.router)
router.include_router(sessions.router)
