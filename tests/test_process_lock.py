from __future__ import annotations

import pytest

from src.core.process_lock import ProcessAlreadyRunning, ProcessSingletonLock


def test_process_lock_rejects_second_instance_and_releases(tmp_path):
    path = tmp_path / "shared" / "api.lock"
    first = ProcessSingletonLock(path)
    second = ProcessSingletonLock(path)

    first.acquire()
    try:
        with pytest.raises(ProcessAlreadyRunning):
            second.acquire()
        assert path.exists()
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        first.release()

    second.acquire()
    second.release()


def test_process_lock_release_is_idempotent(tmp_path):
    lock = ProcessSingletonLock(tmp_path / "api.lock")
    lock.acquire()
    lock.release()
    lock.release()
    lock.acquire()
    lock.release()
