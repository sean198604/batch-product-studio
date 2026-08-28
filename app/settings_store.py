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
    return {
        "is_key_set": bool(key),
        "key_masked": mask_key(key),
        "gemini_base_url": data.get("gemini_base_url", settings.gemini_base_url),
        "gemini_model": data.get("gemini_model", settings.gemini_model),
    }


def update(
    api_key: str | None = None,
    gemini_base_url: str | None = None,
    gemini_model: str | None = None,
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
        _save(data)
        return get_public()
