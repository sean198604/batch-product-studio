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
    ratio: Optional[str] = None  # 输出比例（agnes 等支持 ratio 参数的模型使用，如 4:3 / 16:9；空=原图比例）
    theme: Optional[str] = None  # 节日/季节主题：ghost|spring|autumn|None；非空时注入居中构图锁与主题氛围层
    theme_random: bool = Field(default=False)  # True=逐张随机组合背景/元素/光影（seed 可复现）；False=沿用框内所见即所得文本
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


class RegistrationCode(SQLModel, table=True):
    """管理员签发的注册邀请码（仅在用户数达阈值后启用）。

    * ``code``       - 12 位大写字母 + 数字（管理员 UI 展示）
    * ``max_uses``   - 允许通过此码成功注册的次数（默认 1）
    * ``used_count`` - 已通过此码成功注册的次数
    * ``expires_at`` - 过期时间；NULL = 永久有效
    * ``created_by`` - 创建该码的管理员用户名（仅做记录，无外键约束）
    * ``note``       - 备注（如「销售部 6 月用」）

    通过校验的条件：存在 + status='active' + used_count < max_uses +
    (expires_at IS NULL OR expires_at > now())。
    """

    __tablename__ = "registration_codes"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(unique=True, index=True)
    max_uses: int = Field(default=1)
    used_count: int = Field(default=0)
    status: str = Field(default="active")  # active | disabled
    expires_at: Optional[datetime] = None
    created_by: Optional[str] = None
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    last_used_at: Optional[datetime] = None


# --------------------------------------------------------------------------- #
# API keys pool (user-bound validated Agnes keys + system keys)
# --------------------------------------------------------------------------- #
class ApiKey(SQLModel, table=True):
    """One entry in the shared generation-key pool.

    ``source``:
      * ``user``   - staff self-registered a validated Agnes key (owner_user_id set)
      * ``system`` - admin-provided key (owner_user_id usually NULL); seeded from
                     data/settings.json or added from the admin key-pool card
    ``status``:
      * ``valid``   - usable for generation (user keys only enter as valid after
                      a live probe succeeded)
      * ``invalid`` - probe failed / generation returned 401-403; excluded from pool
    The raw key is stored like the legacy data/settings.json convention
    (plaintext, git-ignored runtime volume). API responses only ever expose the
    masked form.
    """

    __tablename__ = "api_keys"

    id: Optional[int] = Field(default=None, primary_key=True)
    provider: str = Field(default="agnes", index=True)
    owner_user_id: Optional[int] = Field(
        default=None, foreign_key="users.id", index=True
    )
    source: str = Field(default="user")  # user | system
    key_value: str = Field(unique=True, index=True)
    status: str = Field(default="valid")  # valid | invalid
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    validated_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None


# --------------------------------------------------------------------------- #
# API schemas
# --------------------------------------------------------------------------- #
class RegisterRequest(SQLModel):
    username: str
    password: str
    # 用户数达阈值后由后端强制校验；不满阈值时，前端即使传空后端也忽略。
    registration_code: Optional[str] = None


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


# ---- my profile / personal Agnes key binding + quota ----
class ProfileKeyInfo(SQLModel):
    """Masked view of the caller's own pool key (never the raw value)."""

    saved: bool = False
    masked: str = ""
    status: str = ""
    source: str = ""
    note: Optional[str] = None
    validated_at: Optional[datetime] = None


class MyProfile(SQLModel):
    id: int
    username: str
    role: str
    agnes_key: Optional[ProfileKeyInfo] = None
    quota_unlimited: bool = False  # 管理员或已绑定有效 Agnes Key → 不限量
    quota_used_today: int = 0      # 今日已成功生成的图片数
    quota_limit: int = 0           # 未绑定时每日上限（free_daily_limit）


class ProfileKeyUpdate(SQLModel):
    api_key: str


# ---- admin key-pool views ----
class PoolKeyOut(SQLModel):
    id: int
    provider: str
    owner_username: Optional[str] = None  # None = 系统 Key
    source: str
    masked: str
    status: str
    note: Optional[str] = None
    created_at: datetime
    validated_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None


class PoolKeyAddBody(SQLModel):
    api_key: str
    note: Optional[str] = None


class PoolTestResult(SQLModel):
    ok: bool
    status: int
    message: str
    auth_failed: bool = False   # True = 服务端明确拒绝（401/403）



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
    # Gemini 模型全局开关：默认 False=对员工隐藏/禁用 Gemini，True=开放。
    enable_gemini: bool = False
    # Agnes AI（图生图，独立 Key / 节点）
    agnes_is_key_set: bool = False
    agnes_key_masked: str = ""
    agnes_base_url: str = ""
    agnes_size_tier: str = "1K"      # 1K / 2K / 3K / 4K
    agnes_user_tier: str = "default" # default / enterprise / tokenplan
    # Agnes 默认模型 id（生成控制台默认选中）；空 = 不指定（跟随 gemini_model）
    agnes_default_model: str = ""


class AdminSettingsUpdate(SQLModel):
    """Partial update: only the provided (non-empty) fields are changed."""

    gemini_api_key: Optional[str] = None
    gemini_base_url: Optional[str] = None
    gemini_model: Optional[str] = None
    enable_gemini: Optional[bool] = None
    agnes_api_key: Optional[str] = None
    agnes_base_url: Optional[str] = None
    agnes_size_tier: Optional[str] = None
    agnes_user_tier: Optional[str] = None
    agnes_default_model: Optional[str] = None


class ApiTestResult(SQLModel):
    ok: bool
    status: int
    message: str
    model_available: bool = False
    model_count: int = 0


# --------------------------------------------------------------------------- #
# Registration codes (admin-issued invitation codes)
# --------------------------------------------------------------------------- #
class RegistrationCodeCreate(SQLModel):
    """管理员创建注册码的入参。"""

    max_uses: int = 1               # 允许通过的注册次数（>=1）
    expires_at: Optional[datetime] = None  # NULL = 永不过期
    note: Optional[str] = None


class RegistrationCodeOut(SQLModel):
    """注册码展示（含真实 code，管理员可见）。"""

    id: int
    code: str
    max_uses: int
    used_count: int
    remaining: int                   # max_uses - used_count
    status: str                      # active | disabled
    expires_at: Optional[datetime] = None
    created_by: Optional[str] = None
    note: Optional[str] = None
    created_at: datetime
    last_used_at: Optional[datetime] = None


class RegistrationMode(SQLModel):
    """前端注册页用：根据当前用户数判断是否需要邀请码。"""

    code_required: bool
    current_count: int
    threshold: int

