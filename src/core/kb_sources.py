"""选题库三种来源 — 上传 / 远程地址 / 服务器路径（P0 闭环）。

设计：
- 来源登记在 ``<data_dir>/kb/sources.json``（含每源字段映射、同步状态）
- 上传文件存 ``<data_dir>/kb/uploads/``，最大 20MB，支持 CSV / Excel / JSON / JSONL / SQLite
- 远程来源支持 CSV / JSON / RSS，可带鉴权请求头；读取缓存到 ``<data_dir>/kb/cache/``
- 服务器路径相对数据目录或绝对路径均可
- 首次运行自动在数据目录创建空库（带表头的 CSV），没有任何来源也能手动加选题
- 所有对外返回的路径都经 ``redact_path`` 脱敏（R23）
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import ipaddress
import json
import logging
import re
import socket
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx

from .redact import redact_path
from .scoring.topic_eval import TopicItem

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# XLSX 本质是 ZIP。压缩文件小于 20MB 不代表展开后的 XML 安全；限制总展开体积与条目数，防止 ZIP bomb。
MAX_XLSX_EXPANDED_BYTES = 40 * 1024 * 1024
MAX_XLSX_ENTRIES = 1000
DEFAULT_DIMENSION = "综合"
SUPPORTED_FORMATS = ("auto", "csv", "json", "jsonl", "xlsx", "sqlite", "rss")
KINDS = ("file", "server", "remote")

# 通用字段 → 常见中英文列名（自动识别用）
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "source": ("source", "来源", "平台", "platform", "channel", "渠道"),
    "title": ("title", "标题", "主题", "题目", "话题", "name", "名称", "content", "内容"),
    "summary": ("summary", "摘要", "简介", "描述", "description", "desc", "abstract", "body"),
    "link": ("link", "链接", "url", "地址", "原文链接", "href"),
    "date": ("date", "日期", "时间", "发布时间", "pub_date", "pubdate", "published_at", "publish_time", "created_at"),
    "dimension": ("dimension", "维度", "方向", "分类", "category", "标签", "tag", "topic", "栏目"),
}
SUPPORTED_FIELDS = tuple(FIELD_ALIASES.keys())

_EXT_FORMAT = {
    ".csv": "csv",
    ".json": "json",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".db": "sqlite",
    ".sqlite": "sqlite",
    ".sqlite3": "sqlite",
    ".xml": "rss",
}


# ----------------------------------------------------------------------
# 基础路径
# ----------------------------------------------------------------------

def data_dir(cfg) -> Path:
    p = Path(cfg.paths.data_dir)
    if not p.is_absolute():
        p = cfg.resolve_path(str(p))
    return p


def kb_dir(cfg) -> Path:
    p = data_dir(cfg) / "kb"
    p.mkdir(parents=True, exist_ok=True)
    return p


def registry_file(cfg) -> Path:
    return kb_dir(cfg) / "sources.json"


def upload_dir(cfg) -> Path:
    p = kb_dir(cfg) / "uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir(cfg) -> Path:
    p = kb_dir(cfg) / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _resolve_server_path(cfg, raw: str) -> Path:
    """服务器路径：绝对路径原样；相对路径相对数据目录（也兼容以 data/ 开头的老写法）。"""
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p
    if raw.startswith("data/") or raw.startswith("data\\"):
        return cfg.resolve_path(raw)
    return data_dir(cfg) / raw


# ----------------------------------------------------------------------
# 来源登记
# ----------------------------------------------------------------------

def _default_source(cfg) -> dict:
    raw_path = cfg.topic_kb.path or "kb/topics.csv"
    path = _resolve_server_path(cfg, raw_path)
    return {
        "id": "default",
        "kind": "server",
        "name": "默认选题库",
        "enabled": True,
        "path": str(path),
        "format": cfg.topic_kb.type if cfg.topic_kb.type in SUPPORTED_FORMATS else "auto",
        "field_mapping": dict(cfg.topic_kb.field_mapping or {}),
        "table_name": cfg.topic_kb.table_name,
        "headers": {},
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "last_sync_at": None,
        "last_error": None,
        "rows": None,
    }


def _ensure_empty_file(path: Path, fmt: str) -> None:
    """默认空库：给一个带表头的空 CSV，自检可读且不报错。"""
    if path.exists() or fmt not in ("auto", "csv"):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerow(["source", "title", "summary", "link", "date", "dimension"])


def load_registry(cfg) -> list[dict]:
    """读取来源登记；不存在时初始化默认来源 + 空库。"""
    path = registry_file(cfg)
    if not path.exists():
        source = _default_source(cfg)
        _ensure_empty_file(Path(source["path"]), source["format"])
        save_registry(cfg, [source])
        return [source]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("选题库来源登记读取失败：%s", exc)
        return []
    sources = data.get("sources") if isinstance(data, dict) else data
    if not isinstance(sources, list):
        return []
    return [s for s in sources if isinstance(s, dict) and s.get("id")]


def save_registry(cfg, sources: list[dict]) -> None:
    path = registry_file(cfg)
    path.write_text(
        json.dumps({"version": 1, "sources": sources}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _new_id() -> str:
    return "kb-" + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + hashlib.sha1(
        str(datetime.now().timestamp()).encode()
    ).hexdigest()[:6]


def _source_path(cfg, source: dict) -> Path:
    if source.get("kind") == "remote":
        cached = cache_dir(cfg) / f"{source['id']}.{_guess_ext(source)}"
        return cached
    return _resolve_server_path(cfg, source.get("path") or "")


def _guess_ext(source: dict) -> str:
    fmt = source.get("format") or "auto"
    if fmt != "auto":
        return {"csv": "csv", "json": "json", "jsonl": "jsonl", "xlsx": "xlsx", "sqlite": "db", "rss": "xml"}[fmt]
    suffix = Path(str(source.get("path") or source.get("url") or "")).suffix.lower()
    return suffix.lstrip(".") or "csv"


def _validate_remote_url(url: str):
    """仅允许访问解析到公网地址的 HTTP(S) 来源，防止选题源变成 SSRF 入口。"""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("远程地址格式不合法") from exc

    if scheme not in ("http", "https") or not host:
        raise ValueError("远程地址必须是完整的 http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("远程地址不能在 URL 中包含用户名或密码")

    host = host.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("远程来源不能指向本机或内网地址")

    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            records = socket.getaddrinfo(host, port or (443 if scheme == "https" else 80),
                                         type=socket.SOCK_STREAM)
            addresses = {ipaddress.ip_address(record[4][0].split("%", 1)[0]) for record in records}
        except (OSError, ValueError) as exc:
            raise ValueError("远程来源域名无法解析") from exc

    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("远程来源不能解析到本机、内网或保留 IP 地址")
    return parsed


def _url_origin(parsed) -> tuple[str, str, int]:
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, host, port


# ----------------------------------------------------------------------
# 格式解析
# ----------------------------------------------------------------------

def detect_format(name: str, explicit: str = "auto", content_type: str = "") -> str:
    if explicit and explicit != "auto":
        return explicit
    if "rss" in content_type or "xml" in content_type or "atom" in content_type:
        return "rss"
    if "json" in content_type:
        return "json"
    if "csv" in content_type:
        return "csv"
    suffix = Path(name.split("?")[0]).suffix.lower()
    return _EXT_FORMAT.get(suffix, "csv")


def detect_mapping(columns: list[str], stored: Optional[dict] = None) -> tuple[dict[str, str], list[str]]:
    """自动识别中英文列名；stored 里显式指定的映射优先。返回 (映射, 未匹配字段)。"""
    columns_l = {c.strip().lower(): c for c in columns if c}
    mapping: dict[str, str] = {}
    for field, aliases in FIELD_ALIASES.items():
        raw = (stored or {}).get(field)
        if raw and raw in columns:
            mapping[field] = raw
            continue
        if raw and raw.lower() in columns_l:
            mapping[field] = columns_l[raw.lower()]
            continue
        for alias in aliases:
            if alias.lower() in columns_l:
                mapping[field] = columns_l[alias.lower()]
                break
    missing = [f for f in SUPPORTED_FIELDS if f not in mapping]
    return mapping, missing


def _read_text(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _rows_to_dicts(rows: list[list[str]]) -> tuple[list[str], list[dict]]:
    if not rows:
        return [], []
    header = [str(c).strip() for c in rows[0]]
    out = []
    for row in rows[1:]:
        out.append({header[i]: (row[i] if i < len(row) else "") for i in range(len(header))})
    return header, out


def parse_csv(text: str) -> tuple[list[str], list[dict]]:
    reader = list(csv.reader(io.StringIO(text)))
    return _rows_to_dicts(reader)


def parse_json(text: str) -> tuple[list[str], list[dict]]:
    payload = json.loads(text)
    rows = payload
    if isinstance(payload, dict):
        for key in ("items", "data", "rows", "topics", "results"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        else:
            rows = [payload]
    if not isinstance(rows, list):
        raise ValueError("JSON 顶层必须是数组，或包含 items/data/rows/topics 数组")
    rows = [r for r in rows if isinstance(r, dict)]
    columns: list[str] = []
    for r in rows:
        for k in r.keys():
            if k not in columns:
                columns.append(k)
    return columns, rows


def parse_jsonl(text: str) -> tuple[list[str], list[dict]]:
    rows: list[dict] = []
    columns: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if not isinstance(obj, dict):
            continue
        rows.append(obj)
        for k in obj.keys():
            if k not in columns:
                columns.append(k)
    return columns, rows


_XLSX_NS = {
    "main": "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}",
}


def _xlsx_col_index(cell_ref: str) -> int:
    letters = re.match(r"[A-Z]+", cell_ref or "")
    if not letters:
        return 0
    idx = 0
    for ch in letters.group():
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def parse_xlsx(path: Path) -> tuple[list[str], list[dict]]:
    """stdlib 解析 .xlsx（首个工作表），避免额外依赖。"""
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_XLSX_ENTRIES:
            raise ValueError("Excel 文件包含过多压缩条目")
        expanded_size = sum(info.file_size for info in infos)
        if expanded_size > MAX_XLSX_EXPANDED_BYTES:
            raise ValueError("Excel 展开后超过 40MB 上限")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{_XLSX_NS['main']}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_XLSX_NS['main']}t")))
        sheets = sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet"))
        if not sheets:
            raise ValueError("Excel 文件没有工作表")
        root = ET.fromstring(zf.read(sheets[0]))
        raw_rows: list[list[str]] = []
        for row in root.iter(f"{_XLSX_NS['main']}row"):
            cells: dict[int, str] = {}
            for c in row.findall(f"{_XLSX_NS['main']}c"):
                idx = _xlsx_col_index(c.get("r") or "")
                ctype = c.get("t")
                if ctype == "s":
                    v = c.find(f"{_XLSX_NS['main']}v")
                    text = shared[int(v.text)] if v is not None and v.text and int(v.text) < len(shared) else ""
                elif ctype == "inlineStr":
                    text = "".join(t.text or "" for t in c.iter(f"{_XLSX_NS['main']}t"))
                else:
                    v = c.find(f"{_XLSX_NS['main']}v")
                    text = v.text if v is not None and v.text else ""
                cells[idx] = text.strip()
            if cells:
                width = max(cells.keys()) + 1
                raw_rows.append([cells.get(i, "") for i in range(width)])
    return _rows_to_dicts(raw_rows)


def parse_rss(text: str) -> tuple[list[str], list[dict]]:
    """RSS 2.0 / Atom → 统一字段（title/link/summary/date/source）。"""
    root = ET.fromstring(text)
    rows: list[dict] = []
    feed_title = ""
    for tag in ("channel", "{http://www.w3.org/2005/Atom}feed"):
        node = root.find(tag)
        if node is not None:
            t = node.find("title") if node.tag == "channel" else node.find("{http://www.w3.org/2005/Atom}title")
            feed_title = (t.text or "").strip() if t is not None else ""
            break
    items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    for item in items:
        def _text(tag: str) -> str:
            node = item.find(tag)
            return (node.text or "").strip() if node is not None and node.text else ""
        title = _text("title") or _text("{http://www.w3.org/2005/Atom}title")
        link = _text("link")
        if not link:
            link_node = item.find("{http://www.w3.org/2005/Atom}link")
            link = link_node.get("href") if link_node is not None else ""
        summary = _text("description") or _text("{http://www.w3.org/2005/Atom}summary") \
            or _text("{http://www.w3.org/2005/Atom}content")
        date = _text("pubDate") or _text("{http://www.w3.org/2005/Atom}updated") \
            or _text("{http://www.w3.org/2005/Atom}published")
        if title:
            rows.append({
                "title": title, "link": link, "summary": summary,
                "date": date, "source": feed_title,
            })
    return ["title", "link", "summary", "date", "source"], rows


def parse_sqlite(path: Path, table_name: Optional[str]) -> tuple[list[str], list[dict]]:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        if not tables:
            raise ValueError("SQLite 文件没有表")
        table = table_name if table_name in tables else None
        if table is None:
            counts = []
            for t in tables:
                try:
                    counts.append((conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0], t))
                except sqlite3.Error:
                    continue
            table = max(counts)[1] if counts else tables[0]
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info([{table}])").fetchall()]
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM [{table}]").fetchall()]
        return columns, rows
    finally:
        conn.close()


def read_table(path: Path, fmt: str, table_name: Optional[str] = None) -> tuple[list[str], list[dict]]:
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{redact_path(path)}")
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise ValueError("选题库文件超过 20MB 上限")
    if fmt == "xlsx":
        return parse_xlsx(path)
    if fmt == "sqlite":
        return parse_sqlite(path, table_name)
    text = _read_text(path)
    if fmt == "json":
        return parse_json(text)
    if fmt == "jsonl":
        return parse_jsonl(text)
    if fmt == "rss":
        return parse_rss(text)
    return parse_csv(text)


# ----------------------------------------------------------------------
# 远程来源
# ----------------------------------------------------------------------

def fetch_remote(source: dict, timeout: int = 20) -> tuple[list[str], list[dict]]:
    url = (source.get("url") or "").strip()
    headers = {str(k): str(v) for k, v in (source.get("headers") or {}).items() if k and v}
    headers.setdefault("User-Agent", "iterwave/0.1 (+local)")

    # 手动处理重定向：每一跳都重新检查目标，且禁止跨域跳转以避免鉴权头泄漏。
    current_url = url
    raw = b""
    content_type = ""
    redirect_statuses = {301, 302, 303, 307, 308}
    for _ in range(6):
        parsed = _validate_remote_url(current_url)
        with httpx.stream(
            "GET", current_url, headers=headers, timeout=timeout, follow_redirects=False
        ) as resp:
            if resp.status_code in redirect_statuses:
                location = resp.headers.get("location")
                if not location:
                    raise ValueError("远程来源返回了不完整的重定向")
                next_url = urljoin(current_url, location)
                next_parsed = _validate_remote_url(next_url)
                if _url_origin(parsed) != _url_origin(next_parsed):
                    raise ValueError("远程来源不允许跨域重定向，请直接填写最终地址")
                current_url = next_url
                continue

            resp.raise_for_status()
            size = 0
            chunks: list[bytes] = []
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise ValueError("远程选题库超过 20MB 上限")
                chunks.append(chunk)
            raw = b"".join(chunks)
            content_type = resp.headers.get("content-type", "")
            break
    else:
        raise ValueError("远程来源重定向次数过多")

    fmt = detect_format(current_url, source.get("format") or "auto", content_type)
    text = raw.decode("utf-8", errors="replace")
    if fmt == "json":
        return parse_json(text)
    if fmt == "rss":
        return parse_rss(text)
    if fmt == "jsonl":
        return parse_jsonl(text)
    return parse_csv(text)


def sync_remote(cfg, source: dict) -> dict:
    """抓取远程来源 → 写缓存文件 → 更新同步状态（每天由定时任务调用）。"""
    columns, rows = fetch_remote(source)
    fmt = detect_format(str(source.get("url") or ""), source.get("format") or "auto")
    cache = cache_dir(cfg) / f"{source['id']}.{_guess_ext({**source, 'format': fmt})}"
    if fmt == "json":
        cache.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    elif fmt == "jsonl":
        cache.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    elif fmt == "rss":
        cache.write_text(_rows_to_rss(columns, rows), encoding="utf-8")
    elif fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        cache.write_text(buf.getvalue(), encoding="utf-8-sig")
    else:
        raise ValueError("远程来源不支持写入缓存（仅 CSV / JSON / JSONL / RSS）")
    return {
        "last_sync_at": datetime.now().isoformat(timespec="seconds"),
        "last_error": None,
        "rows": len(rows),
        "columns": columns,
        "cache_path": str(cache),
    }


def _rows_to_rss(columns: list[str], rows: list[dict]) -> str:
    """把 RSS 解析结果回写成简单 RSS，便于缓存后离线读取。"""
    esc = lambda s: (str(s) or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    items = "\n".join(
        "<item><title>{}</title><link>{}</link><description>{}</description><pubDate>{}</pubDate></item>".format(
            esc(r.get("title")), esc(r.get("link")), esc(r.get("summary")), esc(r.get("date")))
        for r in rows
    )
    return f"<?xml version=\"1.0\" encoding=\"utf-8\"?><rss version=\"2.0\"><channel><title>cache</title>{items}</channel></rss>"


def sync_all_remote(cfg) -> dict:
    """同步全部启用的远程来源，返回逐源结果（失败不抛出）。"""
    results: dict[str, str] = {}
    sources = load_registry(cfg)
    for source in sources:
        if source.get("kind") != "remote" or not source.get("enabled", True):
            continue
        try:
            info = sync_remote(cfg, source)
            source.update({k: v for k, v in info.items() if k != "cache_path"})
            results[source["id"]] = "ok"
        except Exception as exc:  # noqa: BLE001 — 单源失败不影响其它来源
            source["last_error"] = str(exc)[:200]
            source["last_sync_at"] = datetime.now().isoformat(timespec="seconds")
            results[source["id"]] = "failed"
            logger.warning("选题库远程同步失败 %s: %s", source.get("name"), exc)
    save_registry(cfg, sources)
    return results


# ----------------------------------------------------------------------
# 读取 / 合并
# ----------------------------------------------------------------------

def _topic_from_row(row: dict, mapping: dict[str, str], fallback_dim: str) -> Optional[TopicItem]:
    def field(name: str) -> str:
        raw = mapping.get(name)
        if not raw:
            return ""
        value = row.get(raw)
        return "" if value is None else str(value).strip()

    title = field("title")
    if not title:
        return None
    dimension = field("dimension") or fallback_dim or DEFAULT_DIMENSION
    link = field("link")
    source = field("source") or "kb"
    topic_id = link or source
    if not topic_id:
        topic_id = "kb-" + hashlib.sha1(f"{dimension}|{title}".encode("utf-8")).hexdigest()[:12]
    return TopicItem(
        id=topic_id, title=title, dimension=dimension,
        summary=field("summary"), link=link, source=source,
        date=field("date") or None,
    )


def read_source(cfg, source: dict) -> dict:
    """读取单个来源，返回 {items, status, rows, columns, mapping, error}，不抛出。"""
    base = {
        "id": source.get("id"), "name": source.get("name") or source.get("id"),
        "kind": source.get("kind"), "format": source.get("format") or "auto",
        "display_path": source.get("url") if source.get("kind") == "remote"
        else redact_path(_source_path(cfg, source)),
        "items": [], "status": "ok", "rows": 0, "columns": [], "mapping": {},
        "error": None,
    }
    if not source.get("enabled", True):
        base["status"] = "disabled"
        return base
    try:
        path = _source_path(cfg, source)
        fmt = detect_format(str(source.get("path") or source.get("url") or path.name),
                            source.get("format") or "auto")
        if source.get("kind") == "remote" and not path.exists():
            info = sync_remote(cfg, source)
            source.update({k: v for k, v in info.items() if k != "cache_path"})
        columns, rows = read_table(path, fmt, source.get("table_name"))
        mapping, missing = detect_mapping(columns, source.get("field_mapping") or {})
        if "title" in missing:
            base.update(status="error", error=f"未找到标题列（现有列：{', '.join(columns[:8])}）")
            return base
        fallback = str(source.get("default_dimension") or DEFAULT_DIMENSION)
        items = [it for r in rows if (it := _topic_from_row(r, mapping, fallback)) is not None]
        base.update(items=items, rows=len(rows), columns=columns, mapping=mapping)
        if not items:
            base["status"] = "empty"
        return base
    except Exception as exc:  # noqa: BLE001 — 单源失败不影响其它来源
        base.update(status="error", error=str(exc)[:240])
        logger.warning("选题库来源读取失败 %s: %s", source.get("name"), exc)
        return base


def read_all(cfg) -> tuple[list[TopicItem], dict]:
    """合并全部启用的来源（按 id 去重），返回 (选题, 报告)。"""
    sources = load_registry(cfg)
    report: dict = {
        "sources": [], "kb_count": 0, "errors": 0, "empty": 0,
        "file_type": None, "kb_error": None, "path": None,
    }
    items: list[TopicItem] = []
    seen: set[str] = set()
    for source in sources:
        res = read_source(cfg, source)
        report["sources"].append({
            "id": res["id"], "name": res["name"], "kind": res["kind"],
            "format": res["format"], "display_path": res["display_path"],
            "status": res["status"], "rows": res["rows"], "error": res["error"],
        })
        if res["status"] == "error":
            report["errors"] += 1
            if report["kb_error"] is None:
                report["kb_error"] = f"{res['name']}：{res['error']}"
        elif res["status"] == "empty":
            report["empty"] += 1
        for it in res["items"]:
            if it.id in seen:
                continue
            seen.add(it.id)
            it.source = it.source or f"kb:{source.get('name') or source['id']}"
            items.append(it)
        if report["file_type"] is None and res["format"] != "auto":
            report["file_type"] = res["format"]
        if report["path"] is None and res["display_path"]:
            report["path"] = res["display_path"]
    report["kb_count"] = len(items)
    return items, report


# ----------------------------------------------------------------------
# 来源管理（API 用）
# ----------------------------------------------------------------------

def _redact_remote_url(url: str) -> str:
    """用于界面/API 展示的远程地址：绝不返回 userinfo、查询参数或片段。"""
    try:
        parsed = urlsplit(str(url or ""))
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
        if not scheme or not host:
            return "远程地址"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parsed.port
        default_port = 443 if scheme == "https" else 80
        authority = host if port is None or port == default_port else f"{host}:{port}"
        # 路径、查询参数和片段都可能带 token；固定用占位符，不直接暴露原始路径。
        return f"{scheme}://{authority}/…"
    except (TypeError, ValueError):
        return "远程地址"


def _public_source(cfg, source: dict) -> dict:
    """对外视图：路径脱敏、远程 URL 不回传凭据，请求头只回键名。"""
    out = {
        k: v for k, v in source.items()
        if k not in ("headers", "path", "cache_path", "url")
    }
    if source.get("kind") == "remote":
        out["display_path"] = _redact_remote_url(source.get("url") or "")
        # 旧来源可能曾把完整 URL 自动用作名称，防止名称字段继续泄露凭据。
        raw_name = str(source.get("name") or "")
        if raw_name.lower().startswith(("http://", "https://")):
            out["name"] = _redact_remote_url(raw_name)
    else:
        out["display_path"] = redact_path(_source_path(cfg, source))
    out["header_keys"] = sorted((source.get("headers") or {}).keys())
    return out


def list_sources(cfg) -> dict:
    return {"items": [_public_source(cfg, s) for s in load_registry(cfg)],
            "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
            "supported": list(SUPPORTED_FORMATS)}


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if len(name) > 60:
        raise ValueError("来源名称最长 60 字")
    return name or "选题库来源"


def _decode_upload(filename: str, content_b64: str) -> tuple[str, bytes]:
    try:
        data = base64.b64decode(content_b64.split(",", 1)[-1], validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"文件内容不是合法 base64：{exc}") from exc
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("上传文件超过 20MB 上限")
    safe = Path(filename or "upload.csv").name
    if not safe or safe.startswith("."):
        raise ValueError("文件名不合法")
    return safe, data


def add_upload(cfg, *, filename: str, content_b64: str, name: str = "",
               field_mapping: Optional[dict] = None) -> dict:
    safe, data = _decode_upload(filename, content_b64)
    ext = Path(safe).suffix.lower()
    if ext not in _EXT_FORMAT:
        raise ValueError("只支持 CSV / Excel / JSON / JSONL / SQLite 文件")
    fmt = _EXT_FORMAT[ext]
    if fmt == "xlsx" and not zipfile.is_zipfile(io.BytesIO(data)):
        raise ValueError("文件不是有效的 .xlsx")
    dest = upload_dir(cfg) / f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{safe}"
    dest.write_bytes(data)
    columns, rows = read_table(dest, fmt)
    mapping, missing = detect_mapping(columns, field_mapping or {})
    if "title" in missing:
        dest.unlink(missing_ok=True)
        raise ValueError(f"找不到标题列（现有列：{', '.join(columns[:8])}）")
    sources = load_registry(cfg)
    source = {
        "id": _new_id(), "kind": "file", "name": _validate_name(name or safe),
        "enabled": True, "path": str(dest), "format": fmt,
        "field_mapping": mapping, "headers": {},
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "last_sync_at": None, "last_error": None, "rows": len(rows),
    }
    sources.append(source)
    save_registry(cfg, sources)
    return _public_source(cfg, source)


def add_server(cfg, *, path: str, name: str = "", fmt: str = "auto",
               field_mapping: Optional[dict] = None) -> dict:
    path = (path or "").strip()
    if not path:
        raise ValueError("服务器路径不能为空")
    target = _resolve_server_path(cfg, path)
    if not target.exists():
        raise FileNotFoundError(f"路径不存在：{redact_path(target)}")
    fmt = detect_format(target.name, fmt)
    columns, rows = read_table(target, fmt)
    mapping, missing = detect_mapping(columns, field_mapping or {})
    if "title" in missing:
        raise ValueError(f"找不到标题列（现有列：{', '.join(columns[:8])}）")
    sources = load_registry(cfg)
    source = {
        "id": _new_id(), "kind": "server", "name": _validate_name(name or target.name),
        "enabled": True, "path": str(target), "format": fmt,
        "field_mapping": mapping, "headers": {},
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "last_sync_at": None, "last_error": None, "rows": len(rows),
    }
    sources.append(source)
    save_registry(cfg, sources)
    return _public_source(cfg, source)


def add_remote(cfg, *, url: str, name: str = "", fmt: str = "auto",
               headers: Optional[dict] = None, field_mapping: Optional[dict] = None) -> dict:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("远程地址必须是 http(s) URL")
    source = {
        "id": _new_id(), "kind": "remote", "name": _validate_name(name or _redact_remote_url(url)),
        "enabled": True, "url": url, "format": detect_format(url, fmt),
        "headers": {str(k): str(v) for k, v in (headers or {}).items() if k and v},
        "field_mapping": dict(field_mapping or {}),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "last_sync_at": None, "last_error": None, "rows": None,
    }
    info = sync_remote(cfg, source)
    source.update({k: v for k, v in info.items() if k != "cache_path"})
    # 依据缓存内容探测列名，补齐映射
    path = cache_dir(cfg) / f"{source['id']}.{_guess_ext(source)}"
    try:
        columns, rows = read_table(path, source["format"])
        mapping, missing = detect_mapping(columns, field_mapping or {})
        if "title" in missing:
            raise ValueError(f"远程数据找不到标题列（现有列：{', '.join(columns[:8])}）")
        source["field_mapping"] = mapping
        source["rows"] = len(rows)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    sources = load_registry(cfg)
    sources.append(source)
    save_registry(cfg, sources)
    return _public_source(cfg, source)


def add_default_empty(cfg) -> dict:
    """手动添加第一个来源时没有文件 → 建一个带表头的空 CSV。"""
    source = _default_source(cfg)
    _ensure_empty_file(Path(source["path"]), "csv")
    save_registry(cfg, [source])
    return _public_source(cfg, source)


def update_source(cfg, source_id: str, updates: dict) -> dict:
    sources = load_registry(cfg)
    found = next((s for s in sources if s["id"] == source_id), None)
    if found is None:
        raise KeyError(source_id)
    if "enabled" in updates:
        found["enabled"] = bool(updates["enabled"])
    if "name" in updates:
        found["name"] = _validate_name(str(updates["name"]))
    if "field_mapping" in updates:
        mapping = {str(k): str(v) for k, v in (updates["field_mapping"] or {}).items() if k in SUPPORTED_FIELDS and v}
        columns = read_table(_source_path(cfg, found), detect_format(
            str(found.get("path") or found.get("url") or ""), found.get("format") or "auto"), found.get("table_name"))[0]
        mapping = {k: v for k, v in mapping.items() if v in columns}
        if "title" in mapping:
            found["field_mapping"] = mapping
    if "headers" in updates and found.get("kind") == "remote":
        found["headers"] = {str(k): str(v) for k, v in (updates["headers"] or {}).items() if k and v}
        found["last_sync_at"] = None   # 下次读取重新同步
    if "url" in updates and found.get("kind") == "remote":
        url = str(updates["url"]).strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("远程地址必须是 http(s) URL")
        found["url"] = url
        found["last_sync_at"] = None
    save_registry(cfg, sources)
    return _public_source(cfg, found)


def preview_source(cfg, source_id: str, limit: int = 5) -> dict:
    """字段映射下拉 + 5 行预览：返回列名、自动映射、前 N 行。"""
    sources = load_registry(cfg)
    source = next((s for s in sources if s["id"] == source_id), None)
    if source is None:
        raise KeyError(source_id)
    path = _source_path(cfg, source)
    fmt = detect_format(str(source.get("path") or source.get("url") or path.name),
                        source.get("format") or "auto")
    if source.get("kind") == "remote" and not path.exists():
        sync_remote(cfg, source)
    columns, rows = read_table(path, fmt, source.get("table_name"))
    mapping, missing = detect_mapping(columns, source.get("field_mapping") or {})
    sample = [{k: (str(r.get(k)) if r.get(k) is not None else "") for k in columns}
              for r in rows[:max(1, min(limit, 20))]]
    return {
        "id": source_id, "format": fmt, "columns": columns,
        "mapping": mapping, "missing": missing, "rows": len(rows),
        "sample": sample, "display_path": _public_source(cfg, source)["display_path"],
    }


def delete_source(cfg, source_id: str) -> dict:
    sources = load_registry(cfg)
    target = next((s for s in sources if s["id"] == source_id), None)
    if target is None:
        raise KeyError(source_id)
    if target.get("kind") == "file":
        _source_path(cfg, target).unlink(missing_ok=True)
    for p in (cache_dir(cfg)).glob(f"{source_id}.*"):
        p.unlink(missing_ok=True)
    save_registry(cfg, [s for s in sources if s["id"] != source_id])
    return {"deleted": source_id, "remaining": len(sources) - 1}


def read_all_for_check(cfg) -> dict:
    """自检用：真实读一遍所有来源，返回整体状态与逐源结果。"""
    sources = load_registry(cfg)
    if not sources:
        return {"status": "warn", "message": "没有配置任何选题库来源，只能使用热点转入 / 手动选题",
                "sources": [], "total": 0}
    items, report = read_all(cfg)
    errors = [s for s in report["sources"] if s["status"] == "error"]
    if errors:
        return {"status": "block",
                "message": f"{errors[0]['name']} 读取失败：{errors[0]['error']}",
                "sources": report["sources"], "total": len(items)}
    if not items:
        return {"status": "warn",
                "message": "来源可读但没有任何选题，可上传数据或手动添加选题",
                "sources": report["sources"], "total": 0}
    return {"status": "pass",
            "message": f"{len(report['sources'])} 个来源 · {len(items)} 条选题",
            "sources": report["sources"], "total": len(items)}


def legacy_inspect(cfg) -> dict:
    """兼容旧 /api/kb/inspect：用第一个服务器路径来源做 schema 检查。"""
    source = next((s for s in load_registry(cfg) if s.get("kind") == "server"), None)
    if source is None:
        return {"path": "", "file_type": None, "columns": [], "missing_fields": [],
                "stats": {}, "ok": False, "error": "没有服务器路径来源"}
    res = read_source(cfg, source)
    return {
        "path": res["display_path"], "file_type": res["format"],
        "columns": res["columns"], "mapping": res["mapping"],
        "missing_fields": [], "stats": {"total": res["rows"]},
        "ok": res["status"] == "ok", "error": res["error"],
    }
