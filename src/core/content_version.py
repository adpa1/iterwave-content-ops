"""Canonical fingerprints for the exact content version saved to a platform draft."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def content_version_hash(
    title: Any,
    body: Any,
    tags: Any,
    image_paths: Any,
) -> str:
    """Hash the payload fields that the platform draft saver receives.

    Normalization is deliberately small and deterministic. Callers should pass
    the same effective payload values that are sent to the adapter.
    """
    normalized_tags = tags if isinstance(tags, list) else []
    normalized_images = image_paths if isinstance(image_paths, list) else []
    payload = {
        "title": str(title or ""),
        "body": str(body or ""),
        "tags": [str(tag) for tag in normalized_tags],
        "image_paths": [str(path) for path in normalized_images if path],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_json_list(value: Any) -> list:
    """Decode a JSON list conservatively for legacy database values."""
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else []
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
