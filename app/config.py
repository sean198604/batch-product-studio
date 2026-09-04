"""Application configuration loaded from environment / .env.

All tunables (Gemini model, rate-limit pacing, JWT, storage & DB paths) live
here so they can be overridden via environment variables — which is how the
Docker / docker-compose deployment injects them.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = parent of the `app` package directory. Used to resolve the
# (possibly relative) SQLite path so the database ALWAYS lands under a known
# location regardless of the process working directory (uvicorn launch dir,
# Docker WORKDIR, systemd, etc.). This is what keeps the DB inside the mounted
# volume in Docker instead of leaking into an ephemeral layer.
_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---------- Google AI Studio ----------
    gemini_api_key: str = ""
    # 系统默认生图模型：Agnes Image 2.5 Flash（图生图，国内节点，当前官方免费 $0）。
    # Agnes 免费且质量优于 2.1，故作为开箱默认；管理员可在后台改回任意 Gemini 模型。
    # RPM 由 app/agnes.py 的令牌桶按「账户档位 × 分辨率档位」精确封顶，不会超限。
    gemini_model: str = "agnes-image-2.5-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/models"

    # ---------- Agnes AI (图生图，国内 .cn 节点，当前免费) ----------
    # 国际节点 apihub.agnes-ai.com 在国内常不可达，默认用 .cn 镜像（模型名/
    # 参数/Key 完全一致）。size 决定 RPM 上限与出图分辨率（详见 agnes.py 的
    # RPM_MATRIX：default 1K=20、2K=10、3K/4K=1 稳态 RPM 等）。
    agnes_api_key: str = ""
    agnes_base_url: str = "https://apihub.agnes-ai.cn/v1"
    agnes_size_tier: str = "1K"        # 1K / 2K / 3K / 4K（分辨率档）
    agnes_user_tier: str = "default"   # default / enterprise / tokenplan（账户档）
    # Agnes 默认模型（生成控制台默认选中，优先级高于 gemini_model）。
    # 留空 = 不指定，控制台默认沿用 gemini_model。仅接受 Agnes 模型 id
    # （agnes-image-*.flash）；若值不在模型目录则自动退回 gemini_model。
    agnes_default_model: str = ""

    # ---------- 模型开放 / 免费额度 ----------
    # Gemini 模型默认隐藏：仅当管理员在后台开启 enable_gemini 后，员工才
    # 能看见并使用 Gemini 模型（/api/config 过滤 + 建任务时后端二次拦截）。
    enable_gemini: bool = False
    # 未绑定有效 Agnes API Key 的员工，每天最多免费生成图片的张数；
    # 绑定过有效 Key（或管理员）的用户不限量。
    free_daily_limit: int = 20

    # ---------- Rate limiting (anti 429) ----------
    request_interval_seconds: float = 3.5   # forced sleep after each success
    semaphore_concurrency: int = 1          # single in-flight channel
    max_retries: int = 3                    # 429/5xx retries
    retry_backoff_seconds: str = "5,10,20"  # comma-separated backoff schedule

    # ---------- Multi-task parallel pool ----------
    # 调度器同时最多并行处理的任务数（每个任务内仍串行处理自己的图片）。
    # 超过该数的任务按提交顺序进入后退队列（strict FIFO）。
    # 调整后必须重启服务；运行时改这个值不会立刻生效。
    max_concurrent_tasks: int = 4

    # ---------- Registration gate (invitation code) ----------
    # 注册用户累计 ≥ 该阈值后，新注册必须提供管理员签发的注册码。
    # 默认 20 用户门槛，留作扩容缓冲。设为 0 即立刻强制启用注册码。
    registration_open_user_threshold: int = 20

    # ---------- JWT ----------
    jwt_secret_key: str = "change-me-to-a-long-random-string"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440  # 24h

    # ---------- Storage & Database ----------
    storage_dir: str = "storage"
    uploads_dir: str = "storage/uploads"
    outputs_dir: str = "storage/outputs"
    data_dir: str = "data"
    sqlite_path: str = "data/app.db"

    # ---------- Storage maintenance (disk governance) ----------
    # 磁盘上限（MB）。storage 目录（uploads + outputs）累计超过该值时，
    # 后台维护循环会自动清理最老的任务，并拒绝新的上传请求，直到降回上限内。
    storage_max_mb: int = 10240
    # 任务保留天数。超过该天数的任务（连同原图与生成图）会被自动删除。
    storage_retention_days: int = 30
    # 占用告警阈值（占 storage_max_mb 的比例，0.0-1.0）。超过即记录 WARN 日志
    # 并写入 data/storage_alert.json，供宿主机 cron 监控脚本读取后发告警。
    storage_warn_ratio: float = 0.8
    # 维护循环检查间隔（小时）。到点即执行「过期清理 + 容量兜底」。
    storage_cleanup_interval_hours: int = 6

    @property
    def resolved_db_path(self) -> Path:
        """Absolute, canonical path to the SQLite file.

        A relative ``sqlite_path`` is resolved against the project root so the
        DB never accidentally lands outside the Docker-mounted ``/app/data``
        volume (which would make it ephemeral and wipe on container rebuild).
        """
        p = Path(self.sqlite_path)
        if not p.is_absolute():
            p = (_ROOT / p).resolve()
        return p

    @property
    def database_url(self) -> str:
        """Compose an async SQLite URL, creating the parent dir if needed."""
        p = self.resolved_db_path
        p.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{p}"

    @property
    def retry_backoff_list(self) -> List[float]:
        return [
            float(x) for x in self.retry_backoff_seconds.split(",") if x.strip()
        ]


settings = Settings()
