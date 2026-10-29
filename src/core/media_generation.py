"""OpenAI-compatible image generation/editing adapter.

Provider URL and API key are supplied by server environment, never by the request body.
Only base64 image responses are accepted; provider-returned arbitrary URLs are not fetched.
"""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path

import httpx
from PIL import Image, UnidentifiedImageError


_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
    "GIF": ("image/gif", ".gif"),
}


class ImageGenerationError(RuntimeError):
    pass


def validate_image_bytes(raw: bytes) -> tuple[str, str, int, int]:
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image_format = (image.format or "").upper()
            width, height = image.size
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise ImageGenerationError("生成结果不是有效的图片文件或超过安全像素限制") from exc
    if image_format not in _FORMATS:
        raise ImageGenerationError("生成结果格式不支持，仅接受 JPEG、PNG、WebP、GIF")
    if width < 1 or height < 1 or width > 20000 or height > 20000 or width * height > 40_000_000:
        raise ImageGenerationError("生成结果尺寸超出允许范围（总像素不超过 4000 万）")
    mime_type, suffix = _FORMATS[image_format]
    return mime_type, suffix, width, height


def generate_image_bytes(
    prompt: str,
    *,
    model: str,
    size: str,
    source_path: Path | None = None,
    source_mime_type: str | None = None,
    source_filename: str | None = None,
) -> tuple[bytes, str, str, int, int]:
    """调用兼容 images/generations 或 images/edits 的 API，返回校验后的图片与元数据。"""
    base_url = os.getenv("ITERWAVE_IMAGE_API_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key_env = os.getenv("ITERWAVE_IMAGE_API_KEY_ENV", "OPENAI_API_KEY").strip()
    api_key = os.getenv(key_env) if key_env else None
    if not api_key:
        raise ImageGenerationError(
            f"未配置图片生成 API 密钥。请在服务端环境中设置 {key_env or 'ITERWAVE_IMAGE_API_KEY_ENV'}"
        )
    if not base_url.startswith(("https://", "http://")):
        raise ImageGenerationError("图片 API 地址必须是 HTTP(S) URL")
    endpoint = base_url + ("/images/edits" if source_path else "/images/generations")
    headers = {"Authorization": f"Bearer {api_key}"}
    data = {"model": model, "prompt": prompt, "size": size, "n": "1"}
    try:
        with httpx.Client(timeout=httpx.Timeout(150.0, connect=15.0), follow_redirects=False) as client:
            if source_path:
                source_bytes = source_path.read_bytes()
                files = {
                    "image": (
                        source_filename or source_path.name,
                        source_bytes,
                        source_mime_type or "application/octet-stream",
                    )
                }
                response = client.post(endpoint, headers=headers, data=data, files=files)
            else:
                response = client.post(endpoint, headers=headers, json={**data, "n": 1})
        if response.status_code >= 400:
            message = ""
            try:
                payload = response.json()
                message = str(payload.get("error", {}).get("message") or payload.get("message") or "")
            except (ValueError, AttributeError):
                message = response.text[:300]
            raise ImageGenerationError(f"图片服务返回 HTTP {response.status_code}: {message[:300]}")
        payload = response.json()
    except ImageGenerationError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise ImageGenerationError(f"图片服务请求失败：{exc}") from exc
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise ImageGenerationError("图片服务响应缺少 data[0]")
    encoded = items[0].get("b64_json")
    if not isinstance(encoded, str) or not encoded:
        raise ImageGenerationError(
            "图片服务没有返回 b64_json；请使用支持 base64 图片响应的兼容接口"
        )
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ImageGenerationError("图片服务返回的 base64 无效") from exc
    if not raw or len(raw) > 20 * 1024 * 1024:
        raise ImageGenerationError("图片服务返回内容为空或超过 20MB")
    mime_type, suffix, width, height = validate_image_bytes(raw)
    return raw, mime_type, suffix, width, height
