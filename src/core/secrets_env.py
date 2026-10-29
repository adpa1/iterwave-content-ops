"""把密钥写进 config/.env（只写不读回页面），并立即更新当前进程环境变量。

用于页面上填写的 Webhook 地址、远程选题库鉴权等：值不进 config.yaml、不进覆盖文件、
不进日志，接口只返回「是否已设置」。
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
import threading
from pathlib import Path

from .config import DEFAULT_ENV_PATH

_LOCK = threading.Lock()
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _quote(v: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_\-./:@?=&%+]*", v):
        return v
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_env_secret(name: str, value: str, env_path: Path | None = None) -> None:
    if not _NAME_RE.match(name or ""):
        raise ValueError("环境变量名不合法")
    if "\n" in value or "\r" in value:
        raise ValueError("值不能包含换行")
    p = Path(env_path) if env_path else DEFAULT_ENV_PATH
    with _LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
        out, found = [], False
        for line in lines:
            if re.match(rf"^\s*(export\s+)?{re.escape(name)}\s*=", line):
                found = True
                if value:
                    out.append(f"{name}={_quote(value)}")
                continue
            out.append(line)
        if value and not found:
            out.append(f"{name}={_quote(value)}")
        content = "\n".join(out) + ("\n" if out else "")

        # 在写入任何密钥前先创建 0600 临时文件，再原子替换正式文件。
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="",
                dir=p.parent, prefix=f".{p.name}.", suffix=f".{secrets.token_hex(4)}.tmp",
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
            os.replace(tmp_path, p)
            try:
                p.chmod(0o600)
            except OSError:
                pass
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)
