"""Agnes AI (图生图) client wrapper.

Docs: https://agnes-ai.com/zh-Hans/docs/agnes-image-21-flash (and -25-flash)

* OpenAI-style image API: ``POST {base_url}/images/generations``
* Auth: ``Authorization: Bearer <key>``
* Image-to-image: reference image(s) go in ``extra_body.image`` as a list of
  public URLs **or** Data URIs. We always send local files as Data URIs.
* Output: request ``extra_body.response_format="b64_json"`` and decode the
  returned base64 bytes locally (URL output is time-limited and would need an
  immediate download anyway).
* ``size`` (1K/2K/3K/4K) selects resolution tier; ``ratio`` selects aspect.
* RPM is enforced per resolution tier + account plan by a token bucket
  (see ``RPM_MATRIX``). Quota (agnes-ai tokenplan FAQ, 2026-06-28):
    default    -> 1K 30/20, 2K 20/10, 3K 2/1, 4K 1/1  (allowed/actual RPM)
    enterprise -> 1K 60/40, 2K 40/20, 3K 2/1, 4K 2/1
    tokenplan  -> 1K 120/100, 2K 120/80, 3K 2/1, 4K 2/1
  The global worker pacing (3.5s) is a safe floor; the bucket is authoritative
  and is re-read live so an admin changing the plan/size takes effect at once.
* Retries on 429/5xx with the configured backoff (mirrors gemini.py).
* httpx uses ``trust_env=True`` (default) -> honours HTTP(S)_PROXY (China).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import time
from typing import List, Tuple

import httpx

from app import settings_store
from app.config import settings

logger = logging.getLogger("agnes")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def looks_like_auth_error(message: str) -> bool:
    """Best-effort detection of a definitive key rejection in an error string."""
    low = (message or "").lower()
    return any(
        token in low
        for token in (
            "401", "403",
            "invalid api key", "unauthorized", "authentication",
            "key 无效", "认证失败", "无权限",
        )
    )

# ---------------------------------------------------------------------------
# Official per-resolution RPM quotas (agnes-ai tokenplan FAQ, 2026-06-28).
# Keyed by user tier then size tier. Each value is (allowed_RPM, actual_RPM):
#   * allowed_RPM -> burst capacity (the worker may fire this many in a burst)
#   * actual_RPM  -> steady-state ceiling enforced by the token bucket
# Resolution is independent of aspect ratio; higher res => far fewer RPM.
# 3K/4K are tight for every plan (1-2 actual RPM) and need a slow bucket.
# ---------------------------------------------------------------------------
RPM_MATRIX: dict[str, dict[str, tuple[int, int]]] = {
    "default":    {"1K": (30, 20), "2K": (20, 10), "3K": (2, 1), "4K": (1, 1)},
    "enterprise": {"1K": (60, 40), "2K": (40, 20), "3K": (2, 1), "4K": (2, 1)},
    "tokenplan":  {"1K": (120, 100), "2K": (120, 80), "3K": (2, 1), "4K": (2, 1)},
}
VALID_USER_TIERS = ("default", "enterprise", "tokenplan")
VALID_SIZE_TIERS = ("1K", "2K", "3K", "4K")

# Agnes /images/generations API 接受的 ratio 白名单（2026-08 官方文档）。
# 任何不在此集合的值 Agnes 会回 HTTP 400 "ratio has invalid value"。
# "" / None 表示「原图比例」，调用时不发送 ratio 字段（让上游自己按上传图比例出图）。
ALLOWED_AGNES_RATIOS = frozenset({
    "1:1", "3:4", "4:3", "16:9", "9:16",
    "2:3", "3:2", "21:9",
})


class _TokenBucket:
    """Async token bucket that enforces (allowed, actual) RPM.

    capacity = allowed_RPM (burst headroom); refill = actual_RPM / 60 per
    second (steady-state ceiling). One token is consumed per generation; when
    the bucket is empty we sleep just long enough for the next token to refill.
    """

    def __init__(self, allowed: int, actual: int) -> None:
        self.capacity = float(allowed)
        self.tokens = float(allowed)
        self.refill = float(actual) / 60.0
        self._ts = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + self.refill * (now - self._ts))
            self._ts = now
            if self.tokens < 1.0:
                wait = (1.0 - self.tokens) / self.refill
                await asyncio.sleep(wait)
                self.tokens = 0.0
                self._ts = time.monotonic()
            else:
                self.tokens -= 1.0

# A valid 64x64 white PNG used only by test_connection / key probing
# (zero extra file I/O). NOTE: Agnes upstream rejects 1x1 input images
# with HTTP 500 "internal error", so the probe image must have a sane size.
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAXklEQVR4nO3PMQ0A"
    "MAzAsPInvYLYYVWKESTzjhsd8KsBrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGt"
    "Aa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BbQHKU9LC7/CP1AAAAABJ"
    "RU5ErkJggg=="
)


def _guess_mime_from_bytes(path: str) -> str:
    """Detect real image MIME from magic bytes (extension is unreliable)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return "image/png"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return mimetypes.guess_type(path)[0] or "image/png"


def _encode_data_uri(path: str) -> str:
    """Read a local image and return a ``data:<mime>;base64,...`` URI."""
    mime = _guess_mime_from_bytes(path)
    with open(path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def _mime_from_bytes(data: bytes) -> str:
    """Best-effort MIME detection of the returned image bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/png"


def _describe_error(resp: "httpx.Response") -> str:
    try:
        body = resp.json()
    except Exception:
        body = {}
    msg = ""
    # OpenAI-style error envelope: {"error": {"message": ...}}
    if isinstance(body, dict):
        err = body.get("error") or {}
        if isinstance(err, dict):
            msg = err.get("message") or ""
        # Agnes sometimes returns {"detail": ...} (FastAPI) on validation errs.
        if not msg and isinstance(body.get("detail"), str):
            msg = body["detail"]
    if not msg:
        msg = resp.text[:400]
    low = msg.lower()
    hint = ""
    if any(k in low for k in ("quota", "billing", "exceeded", "rate", "limit")):
        hint = "（疑似额度/限速问题：请检查该分辨率档的 RPM 上限，或放慢请求频率）"
    return f"{msg[:600]}{hint} ({resp.status_code})"


class AgnesClient:
    """Async wrapper around the Agnes ``/images/generations`` endpoint.

    Each Agnes *account* (i.e. each API key) carries its own official RPM
    quota, so the token bucket is keyed per API key: multiple pool keys never
    contend for the same bucket. The plan/size tiers are still global
    (admin-configured) and read live on every request.
    """

    def __init__(self) -> None:
        self.timeout = 120.0
        # Per-key token buckets: { "<key-hash>:<tier>:<size>": _TokenBucket }.
        self._buckets: "dict[str, _TokenBucket]" = {}
        if not settings_store.get_agnes_key():
            logger.warning("No system Agnes key configured - generation may fail.")

    def _endpoint(self) -> str:
        return f"{settings_store.get_agnes_base_url().rstrip('/')}/images/generations"

    async def _throttle(self, size_tier: str, api_key: str) -> None:
        """Block until this KEY is under the official per-resolution RPM.

        Reads live settings so an admin change (plan or size) takes effect on
        the very next request. One bucket per (account, tier, size).
        """
        user_tier = settings_store.get_agnes_user_tier()
        if user_tier not in VALID_USER_TIERS:
            user_tier = "default"
        size = (size_tier or "1K").strip() or "1K"
        if size not in VALID_SIZE_TIERS:
            size = "1K"
        # Never keep real keys in memory as dict keys -> hash it.
        bucket_id = f"{hash(api_key)}:{user_tier}:{size}"
        bucket = self._buckets.get(bucket_id)
        if bucket is None:
            allowed, actual = RPM_MATRIX[user_tier][size]
            bucket = _TokenBucket(allowed, actual)
            self._buckets[bucket_id] = bucket
            logger.info(
                "Agnes RPM limiter active for key#%d: tier=%s size=%s (burst=%d, steady=%d RPM)",
                len(self._buckets), user_tier, size, allowed, actual,
            )
        await bucket.acquire()

    def _build_payload(
        self,
        prompt: str,
        image_paths: List[str],
        model: str,
        size: str,
        ratio: str,
    ) -> dict:
        """Build the OpenAI-style generations payload with img2img images."""
        images = [_encode_data_uri(p) for p in image_paths]
        extra_body: dict = {
            "image": images,
            "response_format": "b64_json",
        }
        payload: dict = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "extra_body": extra_body,
        }
        # 发送给 Agnes 的 ratio 永远是白名单值 —— 绝不能让上游落到缺省 1:1。
        # 官方文档明确 ratio 缺省 Default is 1:1（不是「跟随上传图」）；若空值
        # 直接不发字段，任何非方形输入图都会被硬拉到 1:1 画布重绘，产品随之被
        # 拉伸/压缩（忽胖忽瘦）。因此「原图比例」在此解析为首张输入图的真实
        # 宽高比对应的白名单档位，显式发送。
        payload["ratio"] = self._resolve_safe_ratio(ratio, image_paths)
        return payload

    @staticmethod
    def _resolve_safe_ratio(ratio: str, image_paths: List[str]) -> str:
        """把请求 ratio 规范化为 Agnes 一定接受的值（兜底 1:1，绝不发送非法值）。"""
        from app.presets import nearest_ratio

        r = (ratio or "").strip()
        if r in ALLOWED_AGNES_RATIOS:
            return r
        try:
            from PIL import Image

            for p in image_paths or []:
                if p and os.path.exists(p):
                    with Image.open(p) as im:
                        w, h = im.size
                    mapped = nearest_ratio("", w, h)
                    if mapped:
                        return mapped
        except Exception:  # noqa: BLE001 —— 解析失败只降级，绝不能阻断生图
            pass
        return "1:1"

    def _headers(self, api_key: str | None = None) -> dict:
        key = (api_key or "").strip() or settings_store.get_agnes_key()
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    async def _generate(
        self, payload: dict, model: str, api_key: str | None = None
    ) -> Tuple[bytes, str]:
        endpoint = self._endpoint()
        backoffs = settings.retry_backoff_list
        last_error: Exception | None = None

        for attempt in range(settings.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(
                        endpoint, json=payload, headers=self._headers(api_key)
                    )
                    if 200 <= resp.status_code < 300:
                        return self._extract_image_bytes(resp.json())
                    err_msg = f"Agnes 返回 {resp.status_code}：{_describe_error(resp)}"
                    if resp.status_code in _RETRYABLE_STATUS:
                        last_error = RuntimeError(err_msg)
                        if attempt < settings.max_retries:
                            wait = backoffs[attempt] if attempt < len(backoffs) else backoffs[-1]
                            logger.warning("Rate/transient %s, backing off %.0fs", resp.status_code, wait)
                            await asyncio.sleep(wait)
                            continue
                        raise last_error
                    raise RuntimeError(err_msg)
            except httpx.HTTPStatusError:
                raise
            except httpx.RequestError as exc:
                last_error = exc
                if attempt < settings.max_retries:
                    wait = backoffs[attempt] if attempt < len(backoffs) else backoffs[-1]
                    logger.warning("Network error, backing off %.0fs: %s", wait, exc)
                    await asyncio.sleep(wait)
                    continue
                raise RuntimeError(f"Network error after retries: {exc}") from exc

        raise last_error or RuntimeError("Unknown error during Agnes image generation.")

    @staticmethod
    def _extract_image_bytes(data: dict) -> Tuple[bytes, str]:
        """Pull image bytes out of an Agnes generations response.

        Accepts either ``data[].b64_json`` (preferred) or ``data[].url``
        (download it). Raises a clear error if no image came back.
        """
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"Agnes 返回错误：{data['error']}")
        items = (data or {}).get("data") or []
        if not items:
            raise RuntimeError(
                "Agnes 未返回图片数据（响应不含 data 数组，可能为内容过滤或参数错误）。"
            )
        first = items[0] or {}
        b64 = first.get("b64_json")
        if b64:
            raw = base64.b64decode(b64)
            return raw, _mime_from_bytes(raw)
        url = first.get("url")
        if url:
            # Synchronous-ish download via httpx inside the running loop; the
            # worker awaits this coroutine so the event loop is free.
            return _download_url(url)
        raise RuntimeError("Agnes 响应中既无 b64_json 也无 url 字段。")

    async def generate(
        self,
        prompt: str,
        image_paths: List[str],
        model: str | None = None,
        size: str | None = None,
        ratio: str | None = None,
        api_key: str | None = None,
    ) -> Tuple[bytes, str]:
        """Generate a scene image from one or more reference images.

        *prompt*  - the (already assembled) scene prompt.
        *image_paths* - local file paths (single or multiple angles); they are
          encoded to Data URIs and placed in ``extra_body.image``.
        *model*  - e.g. ``agnes-image-2.1-flash`` / ``agnes-image-2.5-flash``.
        *size*   - resolution tier (1K/2K/3K/4K); drives RPM.
        *ratio*  - aspect ratio string (e.g. ``4:3``); empty = omit.
        *api_key* - the Agnes account key to charge this generation to. When
          omitted the system default key (settings) is used.
        """
        model = (model or "").strip()
        if not model:
            raise RuntimeError("未指定 Agnes 模型。")
        if not image_paths:
            raise RuntimeError("图生图需要至少 1 张参考图。")
        key = (api_key or "").strip() or settings_store.get_agnes_key()
        if not key:
            raise RuntimeError("未配置可用的 Agnes API Key（池为空且无系统默认 Key）。")
        size = (size or settings.agnes_size_tier).strip() or "1K"
        await self._throttle(size, key)
        return await self._generate(
            self._build_payload(prompt, image_paths, model, size, ratio or ""),
            model,
            api_key=key,
        )


async def _download_url(url: str) -> Tuple[bytes, str]:
    """Fetch a returned image URL (used when ``response_format`` was url)."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url)
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"下载 Agnes 返回的图片 URL 失败：HTTP {resp.status_code}")
        raw = resp.content
        return raw, _mime_from_bytes(raw)


async def probe_agnes_key(
    api_key: str | None = None, base_url: str | None = None
) -> dict:
    """Validate one Agnes key with a tiny, free 1x1 generation.

    Returns::

        {ok, status, message, model_available, model_count, auth_failed}

    ``ok``          - True only when the key produced an image.
    ``auth_failed`` - True only when the server *definitively* rejected the key
                      (401/403). A 429 (rate limit) or network error does NOT
                      prove the key is bad -> auth_failed stays False so callers
                      can tell "invalid key" apart from "try again later".
    """
    api_key = (api_key or "").strip() or settings_store.get_agnes_key()
    base = (base_url or "").strip() or settings_store.get_agnes_base_url()
    base_url_clean = base.rstrip("/")
    if not api_key:
        return {
            "ok": False, "status": 0,
            "message": "未提供 Agnes API Key。",
            "model_available": False, "model_count": 0,
            "auth_failed": False,
        }
    probe_models = [
        "agnes-image-2.1-flash",
        "agnes-image-2.5-flash",
        "agnes-image-2.0-flash",
    ]
    data_uri = f"data:image/png;base64,{_TINY_PNG_B64}"
    last_err = ""
    auth_failed = False
    for model in probe_models:
        payload = {
            "model": model,
            "prompt": "test",
            "size": "1K",
            "extra_body": {"image": [data_uri], "response_format": "b64_json"},
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{base_url_clean}/images/generations",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                )
        except httpx.RequestError as exc:
            last_err = f"网络错误：{exc}（请检查网络 / 代理 / .cn 节点可达性）"
            continue
        if 200 <= resp.status_code < 300:
            try:
                AgnesClient._extract_image_bytes(resp.json())
            except Exception:
                return {
                    "ok": True, "status": resp.status_code,
                    "message": f"连接成功，Key 可用（{model} 可达）。",
                    "model_available": True, "model_count": 1,
                    "auth_failed": False,
                }
            return {
                "ok": True, "status": resp.status_code,
                "message": f"连接成功，Key 可用，{model} 可生成。",
                "model_available": True, "model_count": 1,
                "auth_failed": False,
            }
        last_err = _describe_error(resp)
        if resp.status_code in (401, 403):
            auth_failed = True
            break  # key rejected, no point probing the other model
    return {
        "ok": False,
        "status": 0,
        "message": f"Key 校验失败：{last_err}",
        "model_available": False,
        "model_count": 0,
        "auth_failed": auth_failed,
    }


async def test_connection_agnes(
    api_key: str | None = None, base_url: str | None = None
) -> dict:
    """Back-compat wrapper: validate with the given key (or the configured one)."""
    r = await probe_agnes_key(api_key=api_key, base_url=base_url)
    return {k: r[k] for k in ("ok", "status", "message", "model_available", "model_count")}
