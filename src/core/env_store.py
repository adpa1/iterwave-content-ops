"""config/.env 写入工具（R23：密钥只落 .env，chmod 600）。

访问口令、企微/飞书 Webhook URL 都走这里，避免每个接口各写一套。
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / "config" / ".env"
_WRITE_LOCK = threading.RLock()
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def env_file() -> Path:
    """当前生效的 .env 路径（单测可通过 ITERWAVE_ENV_FILE 指向临时文件）。"""
    override = os.getenv("ITERWAVE_ENV_FILE")
    if override:
        return Path(override)
    return DEFAULT_ENV_PATH


def write_env(key: str, value: str) -> Path:
    """校验并串行化写入 / 删除一个环境变量，避免注入和进程内并发覆盖。"""
    if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key):
        raise ValueError("环境变量名格式不合法")
    if not isinstance(value, str):
        raise TypeError("环境变量值必须是字符串")
    if "\n" in value or "\r" in value:
        raise ValueError("环境变量值不能包含换行符")
    with _WRITE_LOCK:
        return _write_env_unlocked(key, value)


def _write_env_unlocked(key: str, value: str) -> Path:
    """调用方持有进程内锁时执行原子文件替换。"""
    path = env_file()
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            if value:
                out.append(f"{key}={value}")
                replaced = True
            continue
        out.append(line)
    if value and not replaced:
        out.append(f"{key}={value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(out) + "\n"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            try:
                os.fchmod(tmp.fileno(), 0o600)
            except (AttributeError, OSError):
                os.chmod(tmp_path, 0o600)
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    if value:
        os.environ[key] = value
    else:
        os.environ.pop(key, None)
    return path


def read_env(key: str) -> Optional[str]:
    """读当前环境变量（.env 已由 load_dotenv 注入；这里兜底直接读文件）。"""
    value = os.getenv(key)
    if value:
        return value
    path = env_file()
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(f"{key}="):
            return line.split("=", 1)[1].strip() or None
    return None
