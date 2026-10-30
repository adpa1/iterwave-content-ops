from __future__ import annotations

import asyncio
from pathlib import Path

from src.adapters.base import Platform
from src.core import login_manager


def test_tenant_login_dir_is_scoped_and_restored(tmp_path):
    original = login_manager.LOGIN_DIR
    tenant_a = tmp_path / "tenant-a"
    tenant_b = tmp_path / "tenant-b"
    try:
        with login_manager.tenant_login_dir(tenant_a):
            assert login_manager._platform_state_path(Platform.XHS) == tenant_a / "login_states" / "xhs.json"
        assert login_manager.LOGIN_DIR == original
        assert login_manager._platform_state_path(Platform.XHS) == original / "xhs.json"
    finally:
        login_manager.LOGIN_DIR = original


def test_async_tasks_keep_distinct_tenant_login_dirs(tmp_path):
    async def run():
        async def inspect(directory: Path):
            with login_manager.tenant_login_dir(directory):
                await asyncio.sleep(0)
                return login_manager._platform_state_path(Platform.DOUYIN)

        a, b = await asyncio.gather(
            inspect(tmp_path / "tenant-a"),
            inspect(tmp_path / "tenant-b"),
        )
        assert a == tmp_path / "tenant-a" / "login_states" / "douyin.json"
        assert b == tmp_path / "tenant-b" / "login_states" / "douyin.json"

    asyncio.run(run())
