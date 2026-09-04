"""Runtime, admin-editable application settings (API key, base URL, model).

Why this exists
---------------
The Gemini credential used to live only in ``.env`` (read once at startup).
To let a non-technical admin paste the key from the web UI -- and have it take
effect *without restarting* the server -- we persist it to
``data/settings.json``. Values saved here OVERRIDE the ``.env`` / environment
defaults; if nothing is saved, the env defaults still apply.

Security note
-------------
The key is stored in plaintext in ``data/settings.json``. For an internal
tool behind auth this is acceptable, but be aware the file is sensitive.
(``data/`` is git-ignored and mounted as a Docker volume, so it persists.)
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from app.config import settings

_PATH = Path(settings.data_dir) / "settings.json"
# RLock (reentrant) so a function holding the lock may call another that also
# acquires it (e.g. update() -> get_public()) without deadlocking.
_lock = threading.RLock()

# Defaults come from the env / .env via config.py.
_defaults = {
    "gemini_api_key": settings.gemini_api_key,
    "gemini_base_url": settings.gemini_base_url,
    "gemini_model": settings.gemini_model,
    "enable_gemini": bool(settings.enable_gemini),
    "agnes_api_key": settings.agnes_api_key,
    "agnes_base_url": settings.agnes_base_url,
    "agnes_size_tier": settings.agnes_size_tier,
    "agnes_user_tier": settings.agnes_user_tier,
    "agnes_default_model": settings.agnes_default_model,
    # 额外的系统级 Agnes Key（数组），由管理员直接编辑 data/settings.json
    # 追加；应用启动与后台保存配置时会同步进 api_keys 池。
    "agnes_extra_keys": [],
    "free_daily_limit": int(settings.free_daily_limit),
}


def _load() -> dict:
    """Return merged settings (defaults + anything persisted on disk)."""
    if _PATH.exists():
        try:
            with open(_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return {**_defaults, **data}
        except Exception:
            # Corrupt file -> fall back to defaults rather than crash.
            pass
    return dict(_defaults)


def _save(data: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: tmp + replace.
    tmp = _PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    tmp.replace(_PATH)


def get_api_key() -> str:
    with _lock:
        return _load().get("gemini_api_key", "") or ""


def get_base_url() -> str:
    with _lock:
        return _load().get("gemini_base_url", settings.gemini_base_url)


def get_model() -> str:
    with _lock:
        return _load().get("gemini_model", settings.gemini_model)


def get_enable_gemini() -> bool:
    with _lock:
        v = _load().get("enable_gemini", bool(settings.enable_gemini))
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def get_agnes_extra_keys() -> list:
    with _lock:
        extra = _load().get("agnes_extra_keys", []) or []
    return [str(k).strip() for k in extra if str(k).strip()]


def get_free_daily_limit() -> int:
    with _lock:
        v = _load().get("free_daily_limit", settings.free_daily_limit)
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return settings.free_daily_limit


def get_agnes_key() -> str:
    with _lock:
        return _load().get("agnes_api_key", "") or ""


def get_agnes_base_url() -> str:
    with _lock:
        return _load().get("agnes_base_url", settings.agnes_base_url)


def get_agnes_size_tier() -> str:
    with _lock:
        return (_load().get("agnes_size_tier", settings.agnes_size_tier) or "1K").strip() or "1K"


def get_agnes_user_tier() -> str:
    with _lock:
        return (_load().get("agnes_user_tier", settings.agnes_user_tier) or "default").strip() or "default"


def get_agnes_default_model() -> str:
    """Agnes 模型 id（生成控制台默认选中）；空串 = 不指定。"""
    with _lock:
        return str(_load().get("agnes_default_model", settings.agnes_default_model) or "").strip()


def mask_key(key: str) -> str:
    """Mask all but the last 4 characters for safe display."""
    if not key:
        return ""
    if len(key) <= 4:
        return "****"
    return "****" + key[-4:]


def get_public() -> dict:
    """Settings safe to expose to the admin UI (never the raw key)."""
    with _lock:
        data = _load()
    key = data.get("gemini_api_key", "") or ""
    agnes_key = data.get("agnes_api_key", "") or ""
    return {
        "is_key_set": bool(key),
        "key_masked": mask_key(key),
        "gemini_base_url": data.get("gemini_base_url", settings.gemini_base_url),
        "gemini_model": data.get("gemini_model", settings.gemini_model),
        "enable_gemini": bool(get_enable_gemini()),
        "agnes_is_key_set": bool(agnes_key),
        "agnes_key_masked": mask_key(agnes_key),
        "agnes_base_url": data.get("agnes_base_url", settings.agnes_base_url),
        "agnes_size_tier": data.get("agnes_size_tier", settings.agnes_size_tier),
        "agnes_user_tier": data.get("agnes_user_tier", settings.agnes_user_tier),
        "agnes_default_model": data.get("agnes_default_model", settings.agnes_default_model) or "",
        "free_daily_limit": data.get("free_daily_limit", settings.free_daily_limit),
    }


def update(
    api_key: str | None = None,
    gemini_base_url: str | None = None,
    gemini_model: str | None = None,
    enable_gemini: bool | None = None,
    agnes_api_key: str | None = None,
    agnes_base_url: str | None = None,
    agnes_size_tier: str | None = None,
    agnes_user_tier: str | None = None,
    agnes_default_model: str | None = None,
    agnes_extra_keys: list | None = None,
) -> dict:
    """Persist admin-provided overrides. Empty strings are ignored so the
    caller can send a partial update (e.g. only change the model)."""
    with _lock:
        data = _load()
        if api_key is not None and api_key.strip():
            data["gemini_api_key"] = api_key.strip()
        if gemini_base_url is not None and gemini_base_url.strip():
            data["gemini_base_url"] = gemini_base_url.strip().rstrip("/")
        if gemini_model is not None and gemini_model.strip():
            data["gemini_model"] = gemini_model.strip()
        if enable_gemini is not None:
            data["enable_gemini"] = bool(enable_gemini)
        if agnes_api_key is not None and agnes_api_key.strip():
            data["agnes_api_key"] = agnes_api_key.strip()
        if agnes_base_url is not None and agnes_base_url.strip():
            data["agnes_base_url"] = agnes_base_url.strip().rstrip("/")
        if agnes_size_tier is not None and agnes_size_tier.strip():
            data["agnes_size_tier"] = agnes_size_tier.strip().upper()
        if agnes_user_tier is not None and agnes_user_tier.strip():
            data["agnes_user_tier"] = agnes_user_tier.strip().lower()
        if agnes_default_model is not None:
            # 允许传空串清除（恢复跟随 gemini_model 的行为）。
            data["agnes_default_model"] = agnes_default_model.strip()
        if agnes_extra_keys is not None:
            clean = [str(k).strip() for k in agnes_extra_keys if str(k).strip()]
            data["agnes_extra_keys"] = clean
        _save(data)
        return get_public()
