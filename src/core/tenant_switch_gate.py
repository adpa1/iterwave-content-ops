"""请求闸门：账号组运行时切换期间阻止新业务请求并排空在途请求。"""
from __future__ import annotations

import asyncio
import threading
import time


class TenantSwitchInProgress(RuntimeError):
    """账号组切换期间拒绝新的业务请求。"""


class TenantSwitchGate:
    """跨 FastAPI 中间件与切换协程共享的轻量闸门。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._switching = False
        self._active_requests = 0

    def enter_request(self) -> None:
        with self._lock:
            if self._switching:
                raise TenantSwitchInProgress("账号组正在切换，请稍后重试")
            self._active_requests += 1

    def leave_request(self) -> None:
        with self._lock:
            if self._active_requests > 0:
                self._active_requests -= 1

    async def begin_switch(self, timeout_seconds: float | None = None) -> None:
        if timeout_seconds is None:
            from src.core.config import TenantSwitchConfig
            timeout_seconds = TenantSwitchConfig().request_drain_timeout_seconds
        with self._lock:
            if self._switching:
                raise TenantSwitchInProgress("已有账号组切换正在进行")
            self._switching = True

        deadline = time.monotonic() + timeout_seconds
        while True:
            with self._lock:
                active = self._active_requests
            if active == 0:
                return
            if time.monotonic() >= deadline:
                self.end_switch()
                raise TimeoutError("等待在途请求结束超时，已取消切换")
            await asyncio.sleep(0.01)

    def end_switch(self) -> None:
        with self._lock:
            self._switching = False

    @property
    def active_requests(self) -> int:
        with self._lock:
            return self._active_requests

    @property
    def switching(self) -> bool:
        with self._lock:
            return self._switching
