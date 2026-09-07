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

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

import asyncio

from app import worker
from app import pool as keypool
from app import storage_maintenance
from app.config import settings
from app.db import init_db
from app.routes import admin, auth, profile, tasks
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
    # 把 data/settings.json 里的系统 Agnes Key（默认 Key + 追加 Key）同步进池。
    await keypool.sync_system_keys_from_settings()
    await worker.start_worker()
    # 磁盘治理：后台维护循环（过期清理 + 容量兜底 + 占用告警）。
    _maintenance_task = asyncio.create_task(
        storage_maintenance.run_maintenance_loop(),
        name="storage-maintenance",
    )
    logger.info("Application started.")
    yield
    # ---- shutdown ----
    _maintenance_task.cancel()
    try:
        await _maintenance_task
    except (asyncio.CancelledError, Exception):
        pass
    await worker.stop_worker()
    logger.info("Application stopped.")


app = FastAPI(
    title="批量产品白底图转场景图系统",
    description="企业内部：白底产品图 -> 场景图（Google AI Studio / Gemini）生成、用户鉴权、数据统计与文件生命周期管理",
    version="2.0.0",
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(tasks.router)
app.include_router(admin.router)


@app.middleware("http")
async def no_cache_html(request, call_next):
    """单文件 SPA：任何 text/html 响应都禁止缓存。

    背景：index.html 之前只有 ETag / Last-Modified，无 Cache-Control，
    其他设备的浏览器会长期复用旧缓存的 HTML（用户在其他设备一直看到
    旧版模型卡片的根因）。这里强制 no-store + Surrogate-Control + Vary，
    保证所有 CDN/反向代理/浏览器都遵守，**任何设备任何网络都不会缓存老 HTML**。

    头字段说明：
      Cache-Control: no-store         — 浏览器/中间缓存不准存任何副本
      Pragma: no-cache                — HTTP/1.0 兼容
      Expires: 0                      — 立即过期
      Surrogate-Control: no-store     — CDN/代理专用，比 Cache-Control 更强，主流代理遵守
      Vary: *                         — 强制所有变体独立缓存，代理不能用其他缓存条目替代
      CDN-Cache-Control: no-store     — Cloudflare 等专用兜底
    """
    response = await call_next(request)
    ct = response.headers.get("content-type", "")
    if ct.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["Surrogate-Control"] = "no-store"
        response.headers["Vary"] = "*"
        response.headers["CDN-Cache-Control"] = "no-store"
    return response


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
    Staff cannot read the API key via this endpoint. Gemini models are hidden
    unless the admin enables them (``enable_gemini``).
    """
    from app import pricing, presets, scenes

    catalog = pricing.catalog()
    enable_gemini = settings_store.get_enable_gemini()
    if not enable_gemini:
        catalog = [m for m in catalog if m.get("provider") != "gemini"]

    full = pricing.catalog()
    # 默认模型优先级：管理员指定的 Agnes 默认模型 → gemini_model（兼容旧行为）。
    agnes_default = settings_store.get_agnes_default_model()
    model = agnes_default if any(
        m.get("provider") == "agnes" and m.get("id") == agnes_default for m in full
    ) else settings_store.get_model()
    if not enable_gemini and any(
        m.get("provider") == "gemini" and m.get("id") == model for m in full
    ):
        # 管理员配置的默认模型是 Gemini 但开关已关闭 → 退回首个可用模型。
        model = next((m["id"] for m in catalog), "")

    return {
        # 控制台默认选中的模型（后台可改；默认 Agnes Image 2.5 Flash，免费）
        "default_model": model,
        "gemini_model": model,  # 兼容旧前端字段名，等价于 default_model
        "models": catalog,
        # Gemini 模型全局开关（默认关闭=员工不可见不可用）
        "enable_gemini": enable_gemini,
        # 统一场景库：分类 Tab + 全部可选场景 + 微调修饰器 + 输出比例
        # （前端不再硬编码任何提示词，改场景只需改 app/presets.py 并重建）
        **presets.payload(),
        # Agnes 档位（非敏感）：供控制台预估批量耗时（RPM 由后端令牌桶精确封顶）
        "agnes_user_tier": settings_store.get_agnes_user_tier(),
        "agnes_size_tier": settings_store.get_agnes_size_tier(),
        # 免费额度与 Key 池概况（非敏感）：未绑定 Key 的员工每天免费 N 张
        "free_daily_limit": settings_store.get_free_daily_limit(),
        "agnes_pool": await keypool.pool_summary(),
        # 节日/季节主题目录（供管理后台与调试查看），前端主入口已改用 scenes 列表
        "themes": scenes.theme_catalog(),
    }


@app.get("/api/scenes/preview", tags=["meta"])
async def scene_preview(theme: str = Query(..., description="ghost|spring|autumn")) -> dict:
    """Return one sample themed prompt for the given theme.

    Used by the console's "random variant" chip so the staff can see what a
    generated combination looks like before submitting. Non-sensitive: it only
    exposes prompt text, never any credential.
    """
    from app import scenes

    if not scenes.is_valid_theme(theme):
        raise HTTPException(status_code=400, detail="未知主题。")
    text, meta = scenes.render_theme_prompt(theme)
    return {"prompt": text, **meta}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=7021, workers=1)
