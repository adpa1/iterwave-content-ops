"""config/.env 写入工具（R23：密钥只落 .env，chmod 600）。

访问口令、企微/飞书 Webhook URL 都走这里，避免每个接口各写一套。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / "config" / ".env"


def env_file() -> Path:
    """当前生效的 .env 路径（单测可通过 ITERWAVE_ENV_FILE 指向临时文件）。"""
    override = os.getenv("ITERWAVE_ENV_FILE")
    if override:
        return Path(override)
    return DEFAULT_ENV_PATH


def write_env(key: str, value: str) -> Path:
    """原子写入 / 删除一个环境变量，保留其它行并尽量避免半写入密钥文件。"""
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
