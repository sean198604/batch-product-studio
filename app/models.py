"""SQLModel table definitions and Pydantic API schemas.

Tables (persisted in SQLite):
  * User            - accounts, roles, cumulative usage counters
  * GenerationTask  - one batch job (one prompt, N images)
  * TaskItem        - a single source image + its generated output

The API schemas (suffix ``*Out`` / ``*Read``) are used for (de)serialization
and keep the wire format decoupled from the DB column set.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Database tables
# --------------------------------------------------------------------------- #
class User(SQLModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    role: str = Field(default="staff")  # "admin" | "staff"
    total_api_calls: int = Field(default=0)
    total_images_generated: int = Field(default=0)
    total_cost_usd: float = Field(default=0.0)  # 累计消耗（USD）
    created_at: datetime = Field(default_factory=_utcnow)


class GenerationTask(SQLModel, table=True):
    __tablename__ = "generation_tasks"

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()), primary_key=True
    )
    user_id: int = Field(foreign_key="users.id", index=True)
    prompt: str                  # 用户填写的统一场景提示词（WYSIWYG，前端框内所见即所得）
    full_prompt: Optional[str] = None  # 后端四层自动拼装后的完整提示词（含保真锁/防畸变/画质层），用于记录与一键复制
    model: Optional[str] = None  # per-task model override (None -> follow admin default)
    env: Optional[str] = None    # 室内/室外环境分类（indoor|outdoor|None），生成时追加对应环境提示词
    is_white_bg: bool = Field(default=False)  # 纯白底模式：后端据此切换 Layer2/Layer4，避免多余杂色背景
    mode: Optional[str] = None   # 生成模式：None/"single" 单图批量；"multi_angle_fusion" 多角度合成
    total_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    cost_usd: float = Field(default=0.0)  # 本任务累计消耗（USD）
    status: str = Field(default="pending")  # pending|processing|completed|failed
    created_at: datetime = Field(default_factory=_utcnow)


class TaskItem(SQLModel, table=True):
    __tablename__ = "task_items"

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()), primary_key=True
    )
    task_id: str = Field(foreign_key="generation_tasks.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    original_filename: str
    original_path: str            # relative to storage_dir（多角度合成时取第一张，作为封面/兼容）
    original_paths: Optional[str] = None  # JSON 数组：多角度合成模式下的全部参考图相对路径
    output_path: Optional[str] = None  # relative to storage_dir, set on success
    cost_usd: float = Field(default=0.0)  # 单张生成消耗（USD）
    status: str = Field(default="pending")
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)


# --------------------------------------------------------------------------- #
# API schemas
# --------------------------------------------------------------------------- #
class RegisterRequest(SQLModel):
    username: str
    password: str


class TokenResponse(SQLModel):
    access_token: str
    token_type: str = "bearer"


class UserRead(SQLModel):
    id: int
    username: str
    role: str
    total_api_calls: int
    total_images_generated: int
    total_cost_usd: float = 0.0
    created_at: datetime


class ItemOut(SQLModel):
    id: str
    original_filename: str
    original_url: str
    original_urls: List[str] = []  # 多角度合成时含多张参考图；普通模式为 [original_url]
    output_url: Optional[str] = None
    status: str
    cost_usd: float = 0.0
    error_message: Optional[str] = None


class TaskDetail(SQLModel):
    id: str
    prompt: str
    full_prompt: Optional[str] = None
    status: str
    total_count: int
    completed_count: int
    failed_count: int
    progress_percent: int
    created_at: datetime
    items: List[ItemOut]
    tasks_ahead: int = 0
    message: Optional[str] = None
    model: Optional[str] = None
    env: Optional[str] = None
    is_white_bg: bool = False
    mode: Optional[str] = None
    cost_usd: float = 0.0


class TaskSummary(SQLModel):
    id: str
    prompt: str
    full_prompt: Optional[str] = None
    status: str
    total_count: int
    completed_count: int
    failed_count: int
    progress_percent: int
    created_at: datetime
    tasks_ahead: int = 0
    message: Optional[str] = None
    model: Optional[str] = None
    env: Optional[str] = None
    is_white_bg: bool = False
    mode: Optional[str] = None
    cost_usd: float = 0.0
    username: Optional[str] = None  # populated for admin "all tasks" view


class HistoryTask(SQLModel):
    """历史页每条任务：自带完整 items（含原图/生成图 URL），刷新后直接渲染图片。"""
    id: str
    prompt: str
    full_prompt: Optional[str] = None
    status: str
    total_count: int
    completed_count: int
    failed_count: int
    progress_percent: int
    created_at: datetime
    model: Optional[str] = None
    is_white_bg: bool = False
    mode: Optional[str] = None
    cost_usd: float = 0.0
    items: List[ItemOut] = []


class PagedTasks(SQLModel):
    total: int
    page: int
    page_size: int
    items: List[HistoryTask]


class AdminImageRecord(SQLModel):
    """管理后台「全公司图片记录」：逐张图片视角，含提交人。"""
    id: str
    task_id: str
    owner_username: str
    prompt: str
    model: Optional[str] = None
    mode: Optional[str] = None
    status: str
    cost_usd: float = 0.0
    created_at: datetime
    original_urls: List[str] = []   # 多角度合成含多张；普通为 [original_url]
    output_url: Optional[str] = None
    error_message: Optional[str] = None


class PagedImageRecords(SQLModel):
    total: int
    page: int
    page_size: int
    items: List[AdminImageRecord]


class AdminStats(SQLModel):
    total_users: int
    total_api_calls: int
    total_images_generated: int
    total_cost_usd: float = 0.0
    cost_by_model: dict = {}


# --------------------------------------------------------------------------- #
# API settings (admin-editable key / base URL / model)
# --------------------------------------------------------------------------- #
class AdminSettings(SQLModel):
    """Safe-to-expose view of the runtime settings (never the raw key)."""

    is_key_set: bool
    key_masked: str
    gemini_base_url: str
    gemini_model: str


class AdminSettingsUpdate(SQLModel):
    """Partial update: only the provided (non-empty) fields are changed."""

    gemini_api_key: Optional[str] = None
    gemini_base_url: Optional[str] = None
    gemini_model: Optional[str] = None


class ApiTestResult(SQLModel):
    ok: bool
    status: int
    message: str
    model_available: bool = False
    model_count: int = 0

