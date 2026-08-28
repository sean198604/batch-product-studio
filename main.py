"""FastAPI application entry point.

Wires together:
  * DB bootstrap + background queue worker (via lifespan),
  * auth / tasks / admin routers,
  * static mounts: /storage (uploads + generated images) and /static (frontend),
  * a root route serving the single-page frontend (index.html).
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from app import worker
from app.config import settings
from app.db import init_db
from app.routes import admin, auth, tasks
from app import settings_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")

# Ensure mount directories exist before StaticFiles is created at import time.
os.makedirs(settings.storage_dir, exist_ok=True)
os.makedirs(settings.uploads_dir, exist_ok=True)
os.makedirs(settings.outputs_dir, exist_ok=True)
os.makedirs(settings.data_dir, exist_ok=True)
os.makedirs("static", exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- startup ----
    os.makedirs(settings.uploads_dir, exist_ok=True)
    os.makedirs(settings.outputs_dir, exist_ok=True)
    os.makedirs(settings.data_dir, exist_ok=True)
    await init_db()
    await worker.start_worker()
    logger.info("Application started.")
    yield
    # ---- shutdown ----
    await worker.stop_worker()
    logger.info("Application stopped.")


app = FastAPI(
    title="批量产品白底图转场景图系统",
    description="企业内部：白底产品图 -> 场景图（Google AI Studio / Gemini）生成、用户鉴权、数据统计与文件生命周期管理",
    version="2.0.0",
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(tasks.router)
app.include_router(admin.router)

# Generated images + originals are served under /storage.
app.mount("/storage", StaticFiles(directory=settings.storage_dir), name="storage")
# Frontend assets under /static (index.html also served at "/").
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(os.path.join("static", "index.html"))


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/config", tags=["meta"])
async def public_config() -> dict:
    """Non-sensitive runtime config for the frontend (default model + catalog).

    Lets the console pre-select the admin-configured default model and build
    the model dropdown with live unit prices, without exposing any secret.
    Staff cannot read the API key via this endpoint.
    """
    from app import pricing

    return {
        "gemini_model": settings_store.get_model(),
        "models": pricing.catalog(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=7021, workers=1)
