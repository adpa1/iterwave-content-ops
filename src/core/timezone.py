"""Business timezone helpers shared by API, scheduler and operation windows."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Shanghai"


def business_zone(cfg=None) -> ZoneInfo:
    name = getattr(cfg, "business_timezone", None) or DEFAULT_TIMEZONE
    return ZoneInfo(str(name))


def business_now(cfg=None) -> datetime:
    """Return naive wall-clock datetime in configured business timezone.

    Persisted timestamps remain local-naive for backward compatibility; timezone
    conversion is centralized here rather than delegated to the host OS setting.
    """
    return datetime.now(business_zone(cfg)).replace(tzinfo=None)


def business_timestamp(cfg=None) -> float:
    """Unix timestamp for the configured business clock (DST-safe)."""
    return datetime.now(business_zone(cfg)).timestamp()


def business_today(cfg=None):
    return business_now(cfg).date()
