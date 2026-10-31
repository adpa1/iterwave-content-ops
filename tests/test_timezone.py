from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from src.core.config import Config
from src.core.timezone import business_now, business_timestamp, business_zone


def test_business_now_uses_configured_timezone_without_changing_legacy_naive_storage(cfg):
    cfg.business_timezone = "Asia/Tokyo"
    value = business_now(cfg)
    assert value.tzinfo is None
    assert business_zone(cfg).key == "Asia/Tokyo"


def test_business_timestamp_uses_configured_timezone(cfg):
    cfg.business_timezone = "Asia/Tokyo"
    before = datetime.now(ZoneInfo("Asia/Tokyo")).timestamp()
    value = business_timestamp(cfg)
    after = datetime.now(ZoneInfo("Asia/Tokyo")).timestamp()
    assert before <= value <= after


def test_invalid_business_timezone_is_rejected(cfg):
    payload = cfg.model_dump()
    payload["business_timezone"] = "Mars/Olympus"
    with pytest.raises(ValidationError):
        Config.model_validate(payload)
