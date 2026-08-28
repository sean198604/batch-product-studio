"""Google AI Studio (Gemini) client wrapper.

* Reads the API key from settings.
* Encodes a local source image to base64 and builds an AI-Studio-compliant
  ``inlineData`` payload (camelCase -- the Gemini REST API rejects snake_case)
  together with the scene-generation prompt.
* Calls ``models/<model>:generateContent`` asynchronously via httpx.
* Extracts the returned base64 image bytes.
* Retries on HTTP 429 (rate limit) and 5xx / network errors with the
  configured exponential backoff (default 5s / 10s / 20s, 3 attempts).

NOTE: this client does NOT enforce the inter-request 3.5s pacing — that global
pacing lives in the worker so it applies across all tasks. httpx is created
with ``trust_env=True`` (the default), so it automatically picks up
``HTTP_PROXY`` / ``HTTPS_PROXY`` from the environment (used in China deploys).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
from typing import Tuple

import httpx

from app import settings_store
from app.config import settings

logger = logging.getLogger("gemini")

# Status codes / error classes that are safe to retry (transient).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def _guess_mime_from_bytes(path: str) -> str:
    """Detect the real image MIME type from the file's magic bytes.

    Relying on the filename extension (``mimetypes.guess_type``) is fragile: an
    upload with a missing / wrong extension (e.g. a ``.heic`` saved as ``.png``,
    or no extension at all) would send ``application/octet-stream`` and Google
    rejects it with a cryptic ``Unable to process input image`` 400. Reading
    the leading bytes makes the MIME match the actual file content.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return _guess_mime(path)
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    # Fall back to extension guess; if still unknown, callers will surface a
    # clear "unsupported image" error rather than a cryptic Google 400.
    return _guess_mime(path)


def _encode_image(path: str) -> str:
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("utf-8")


def _describe_error(resp: "httpx.Response") -> str:
    """Turn a non-2xx Gemini response into an actionable, human-readable cause.

    Google's error body (``{"error": {"message": ...}}``) is far more useful
    than a bare status code -- e.g. it tells us the model's free-tier quota is
    0 and billing must be enabled, which otherwise just looks like a generic
    429. We surface that so the user isn't left guessing.
    """
    try:
        err = resp.json().get("error", {}) or {}
        msg = err.get("message") or ""
    except Exception:
        msg = ""
    if not msg:
        msg = resp.text[:400]
    low = msg.lower()
    hint = ""
    if any(k in low for k in ("quota", "billing", "exceeded")) or "limit: 0" in msg:
        hint = (
            "【该模型免费额度为 0，需为 API Key 所属 Google 账号开通计费"
            "（绑定付费方案）后才能生图】"
        )
    elif "unable to process input image" in low:
        hint = (
            "【Google 无法处理该输入图：请确认是标准 JPG/PNG/WEBP 且未损坏；"
            "如扩展名异常请先转成 PNG，或换一张图重试】"
        )
    tail = f" ({resp.status_code})"
    return f"{msg[:600]}{hint}{tail}"


def _extract_image_bytes(data: dict) -> Tuple[bytes, str]:
    candidates = data.get("candidates") or []
    if not candidates:
        reason = data.get("promptFeedback", {}).get("blockReason")
        raise RuntimeError(
            f"Gemini 未返回任何候选结果 (blockReason={reason or 'unknown'})。"
        )
    parts = candidates[0].get("content", {}).get("parts", [])
    for part in parts:
        # Accept both camelCase (REST) and snake_case for robustness.
        inline = part.get("inlineData") or part.get("inline_data")
        mime = (inline or {}).get("mimeType") or (inline or {}).get("mime_type") or ""
        if inline and str(mime).startswith("image"):
            return base64.b64decode(inline["data"]), mime
    # No image part: surface a clue (safety filter / text-only reply) so the
    # failure isn't a cryptic "no image data".
    texts = [p.get("text") for p in parts if p.get("text")]
    clue = texts[0][:160] if texts else ""
    raise RuntimeError(
        "Gemini 响应中未包含图片数据。"
        + (f"（模型仅返回文字：{clue}）" if clue else "可能为安全过滤或模型未生图。")
    )


class GeminiClient:
    """Async wrapper around the AI Studio image-generation endpoint."""

    def __init__(self) -> None:
        # Endpoint details are resolved lazily on every call (see _endpoint) so
        # that an admin-edited key / URL / model takes effect immediately,
        # without restarting the worker.
        if not settings_store.get_api_key():
            logger.warning("GEMINI_API_KEY is not set - generation calls will fail.")
        self.timeout = 120.0

    def _endpoint_for(self, model: str | None) -> str:
        # Resolved at request time from the runtime settings store.
        # A per-call ``model`` overrides the admin-configured default, so a
        # single task can pick e.g. imagen-3.0-generate-002 without changing
        # the global setting.
        api_key = settings_store.get_api_key()
        model = (model or settings_store.get_model()).strip()
        base_url = settings_store.get_base_url().rstrip("/")
        return f"{base_url}/{model}:generateContent?key={api_key}"

    @property
    def _endpoint(self) -> str:
        return self._endpoint_for(None)

    def _build_payload(self, prompt: str, image_path: str) -> dict:
        # NOTE: the Gemini *REST* API uses camelCase keys (inlineData / mimeType),
        # not snake_case. Sending inline_data makes Google silently ignore the
        # input image and the call returns text-only -> "no image data".
        return {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inlineData": {
                                "mimeType": _guess_mime_from_bytes(image_path),
                                "data": _encode_image(image_path),
                            }
                        },
                    ]
                }
            ]
        }

    def _build_payload_multi(self, prompt: str, image_paths: list) -> dict:
        """Build a payload with ONE text part + MULTIPLE inline images.

        Used by the multi-angle fusion mode: all reference angles are placed in
        the same ``contents[0].parts`` array so the model sees them together and
        can reason about the product's 3D form before generating one scene.
        """
        parts = [{"text": prompt}]
        for p in image_paths:
            parts.append(
                {
                    "inlineData": {
                        "mimeType": _guess_mime_from_bytes(p),
                        "data": _encode_image(p),
                    }
                }
            )
        return {"contents": [{"parts": parts}]}

    async def _generate(self, payload: dict, model: str | None) -> Tuple[bytes, str]:
        """Post *payload* to the generateContent endpoint with retry/backoff.

        Shared by single-image and multi-image calls. Returns
        (image_bytes, mime_type). Raises the last underlying error once retries
        are exhausted so the caller can mark the item as failed.
        """
        endpoint = self._endpoint_for(model)
        backoffs = settings.retry_backoff_list
        last_error: Exception | None = None

        for attempt in range(settings.max_retries + 1):
            try:
                # trust_env=True (default) -> honours HTTP(S)_PROXY env vars.
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(endpoint, json=payload)
                    if 200 <= resp.status_code < 300:
                        return _extract_image_bytes(resp.json())
                    # Non-2xx. Build a descriptive error that includes Google's
                    # real message (e.g. "Unable to process input image") so the
                    # failure is actionable instead of a bare "400 Bad Request".
                    err_msg = f"Gemini 返回 {resp.status_code}：{_describe_error(resp)}"
                    if resp.status_code in _RETRYABLE_STATUS:
                        last_error = RuntimeError(err_msg)
                        if attempt < settings.max_retries:
                            wait = backoffs[attempt] if attempt < len(backoffs) else backoffs[-1]
                            logger.warning("Rate/transient %s, backing off %.0fs",
                                           resp.status_code, wait)
                            await asyncio.sleep(wait)
                            continue
                        raise last_error
                    # Non-retryable (400 invalid arg, 403, etc.) -> fail fast
                    # with the real reason. Retrying would not help.
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

        raise last_error or RuntimeError("Unknown error during image generation.")

    async def generate_image(
        self, prompt: str, image_path: str, model: str | None = None
    ) -> Tuple[bytes, str]:
        """Generate a scene image for *image_path* using *prompt*.

        *model* optionally overrides the admin-configured default model for
        this single call (e.g. a per-task selection from the frontend).

        Returns (image_bytes, mime_type). Raises the last underlying error once
        retries are exhausted so the worker can mark the item as failed.
        """
        return await self._generate(self._build_payload(prompt, image_path), model)

    async def generate_image_multi(
        self, prompt: str, image_paths: list, model: str | None = None
    ) -> Tuple[bytes, str]:
        """Multi-angle fusion: send several reference images in one request.

        *image_paths* is an ordered list of local file paths (2-4 angles of the
        SAME product). The model receives them as multiple ``inlineData`` parts
        within a single ``contents[0]`` and fuses them into one scene image.
        """
        if not image_paths:
            raise RuntimeError("多角度合成需要至少 1 张参考图。")
        return await self._generate(self._build_payload_multi(prompt, image_paths), model)


def mime_to_ext(mime: str) -> str:
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get(mime.lower(), "png")


async def test_connection() -> dict:
    """Validate the current API key / endpoint WITHOUT burning image quota.

    Hits the ``models`` list endpoint (``GET {base_url}?key=...``), which only
    needs a valid key -- cheap and quota-free. Returns a plain dict the admin
    route serialises into :class:`~app.models.ApiTestResult`.
    """
    api_key = settings_store.get_api_key()
    base_url = settings_store.get_base_url().rstrip("/")
    model = settings_store.get_model()

    if not api_key:
        return {
            "ok": False,
            "status": 0,
            "message": "未配置 API Key，请先在上方粘贴并保存。",
            "model_available": False,
            "model_count": 0,
        }

    url = f"{base_url}?key={api_key}"
    try:
        # trust_env=True (default) -> honours HTTP(S)_PROXY if configured.
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
    except httpx.RequestError as exc:
        return {
            "ok": False,
            "status": 0,
            "message": f"网络错误：{exc}（请检查网络 / 代理配置）",
            "model_available": False,
            "model_count": 0,
        }

    try:
        payload = resp.json()
    except ValueError:
        payload = {}

    if resp.status_code != 200:
        detail = payload.get("error", {}).get("message") or resp.text[:200]
        return {
            "ok": False,
            "status": resp.status_code,
            "message": f"Key 校验失败：{detail}",
            "model_available": False,
            "model_count": 0,
        }

    models = payload.get("models", []) or []
    names = [m.get("name", "") for m in models]
    # Model names look like ".../models/gemini-2.5-flash-image".
    model_available = any(n.endswith("/" + model) or n == model for n in names)
    return {
        "ok": True,
        "status": resp.status_code,
        "message": (
            "连接成功，Key 可访问 Google API。"
            "（注意：仅代表 Key 有效、模型在目录中；实际生图还需该模型有可用"
            "额度/已开通计费，未计费账号生图会返回 429 配额错误）"
        ),
        "model_available": model_available,
        "model_count": len(models),
    }

