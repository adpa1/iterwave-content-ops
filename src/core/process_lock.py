"""单机服务实例互斥锁，防止多 Uvicorn worker/重复服务回收彼此的任务。"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path


class ProcessAlreadyRunning(RuntimeError):
    """已有 IterWave API 实例持有运行锁。"""


class ProcessSingletonLock:
    """基于 flock 的进程级排他锁；锁由内核在进程退出时自动释放。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise ProcessAlreadyRunning(
                f"IterWave API 已有实例运行（锁文件：{self.path}）；当前部署只允许一个 Uvicorn worker"
            ) from exc
        except Exception:
            os.close(fd)
            raise
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\\n".encode("ascii"))
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "ProcessSingletonLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
