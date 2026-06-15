from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.api.v1.router import router
from app.database import engine
from app.models import *  # noqa: ensure all models are imported for Alembic


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(title="WMS Backend", version="1.0.0", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok"}
