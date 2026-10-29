"""
动态选题库读取 — SQLite / CSV / JSONL / JSON / Excel，自动识别（§16.1）。

配置入口：config.topic_kb（来源见 kb_sources：本地路径 / 上传 / 远程地址）
  type=auto      → 按扩展名检测
  field_mapping  → 通用字段 → 原始列名；留空 = 列名与通用字段同名
  default_dimension → 行里没有维度时使用（RSS、简单表格常见）

通用字段（设计方案 §16.1）：source / title / summary / link / date / dimension
未配置选题库（path 为空）时返回空列表，不报错：系统仍可用热点转入与手动添加的选题。
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from .kb_sources import (
    MAX_LOCAL_SOURCE_BYTES,
    MAX_TOPIC_ROWS,
    KbSourceError,
    display_path,
    extract_json_rows,
    read_xlsx_rows,
    resolve_kb_path,
)
from .scoring.topic_eval import TopicItem

logger = logging.getLogger(__name__)

SUPPORTED_FIELDS = ("source", "title", "summary", "link", "date", "dimension")
EXT_TYPES = {
    ".db": "sqlite", ".sqlite": "sqlite", ".sqlite3": "sqlite",
    ".csv": "csv", ".jsonl": "jsonl", ".ndjson": "jsonl",
    ".json": "json", ".xlsx": "xlsx",
}

# 常见同义列名：用户没配映射时自动认出来（中文表格、英文导出都能直接用）
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("title", "标题", "选题", "题目", "name", "headline", "topic"),
    "dimension": ("dimension", "dim", "维度", "分类", "类别", "栏目", "category", "tag", "标签"),
    "summary": ("summary", "摘要", "简介", "描述", "内容", "description", "desc", "content"),
    "link": ("link", "url", "链接", "地址", "href"),
    "date": ("date", "pub_date", "日期", "发布时间", "时间", "published", "created_at"),
    "source": ("source", "来源", "出处", "author", "作者"),
}


class KbNotConfigured(KbSourceError):
    """没有配置选题库。"""


class KbReader:
    """选题库读取器（每次读取时重新解析路径，配置在线修改后立即生效）。"""

    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def kb_cfg(self):
        return self.cfg.topic_kb

    @property
    def path(self) -> Optional[Path]:
        return resolve_kb_path(self.cfg)

    # ------------------------------------------------------------------
    def _require_path(self) -> Path:
        p = self.path
        if p is None:
            raise KbNotConfigured("还没有配置选题库")
        if not p.exists():
            if self.kb_cfg.source_type == "remote":
                raise KbSourceError("远程选题库还没有同步过，请先点「立即同步」")
            raise KbSourceError(f"选题库文件不存在：{display_path(p)}")
        try:
            if p.stat().st_size > MAX_LOCAL_SOURCE_BYTES:
                raise KbSourceError("本地选题库超过 100MB 上限")
        except OSError as exc:
            raise KbSourceError("无法读取选题库文件") from exc
        return p

    def read(self) -> tuple[list[TopicItem], dict]:
        """读取选题库，返回 (TopicItem 列表, 检测报告)。未配置时返回空列表。"""
        p = self.path
        if p is None:
            return [], {"file_type": None, "path": "", "field_mapping": {}, "errors": [],
                        "stats": {"total": 0, "unique_dimensions": 0}, "configured": False}
        p = self._require_path()
        file_type = self._detect_type(p)
        report = {
            "file_type": file_type,
            "path": display_path(p),
            "field_mapping": {},
            "errors": [],
            "stats": {},
            "configured": True,
        }
        if file_type == "sqlite":
            columns, rows = self._sqlite_rows(p, report)
        else:
            columns, rows = self._file_rows(p, file_type, report)
        if len(rows) > MAX_TOPIC_ROWS:
            raise KbSourceError(f"选题库超过 {MAX_TOPIC_ROWS:,} 行上限")
        mapping = self._resolve_mapping(columns)
        report["field_mapping"] = mapping
        items = self._to_items(rows, mapping, report)

        report["stats"] = {
            "total": len(items),
            "unique_dimensions": len({it.dimension for it in items}),
        }
        logger.info("📚 选题库读取完成: %s 条, %s 个维度", len(items), report["stats"]["unique_dimensions"])
        return items, report

    # ------------------------------------------------------------------
    def _detect_type(self, p: Path) -> str:
        explicit = self.kb_cfg.type
        if explicit and explicit != "auto":
            return explicit
        ext = p.suffix.lower()
        if ext not in EXT_TYPES:
            raise KbSourceError(f"无法识别文件类型（后缀 {ext or '无'}），支持 CSV、Excel、JSON、JSONL、SQLite")
        return EXT_TYPES[ext]

    def _resolve_mapping(self, columns: list[str]) -> dict[str, str]:
        """通用字段 → 原始列名。用户配置优先，其次按同义列名自动识别。"""
        configured = {k: v for k, v in (self.kb_cfg.field_mapping or {}).items()
                      if k in SUPPORTED_FIELDS and v}
        lower = {c.lower(): c for c in columns}
        mapping: dict[str, str] = {}
        for f in SUPPORTED_FIELDS:
            if f in configured and configured[f] in columns:
                mapping[f] = configured[f]
                continue
            for alias in FIELD_ALIASES[f]:
                if alias in columns:
                    mapping[f] = alias
                    break
                if alias.lower() in lower:
                    mapping[f] = lower[alias.lower()]
                    break
            else:
                mapping[f] = configured.get(f, f)
        return mapping

    # ------------------------------------------------------------------
    @staticmethod
    def _quote_identifier(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def _pick_table(self, conn) -> str:
        table_name = self.kb_cfg.table_name
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        if table_name and table_name in tables:
            return table_name
        if not tables:
            raise KbSourceError("SQLite 文件里没有表")
        counts = []
        for table in tables:
            try:
                ident = self._quote_identifier(table)
                counts.append((conn.execute(f"SELECT COUNT(*) FROM {ident}").fetchone()[0], table))
            except sqlite3.Error:
                pass
        return max(counts)[1] if counts else tables[0]

    def _sqlite_rows(self, p: Path, report: dict, limit: Optional[int] = None):
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            table = self._pick_table(conn)
            report["table"] = table
            ident = self._quote_identifier(table)
            total_rows = int(conn.execute(f"SELECT COUNT(*) FROM {ident}").fetchone()[0])
            report.setdefault("stats", {})["total_rows"] = total_rows
            if total_rows > MAX_TOPIC_ROWS and not limit:
                raise KbSourceError(f"SQLite 选题库超过 {MAX_TOPIC_ROWS:,} 行上限")
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({ident})").fetchall()]
            sql = f"SELECT * FROM {ident}" + (f" LIMIT {int(limit)}" if limit else "")
            rows = [dict(r) for r in conn.execute(sql).fetchall()]
        finally:
            conn.close()
        return cols, rows

    def _file_rows(self, p: Path, file_type: str, report: dict, limit: Optional[int] = None):
        cols: list[str] = []
        rows: list[dict] = []
        if file_type == "csv":
            with open(p, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                cols = [c.strip() for c in (reader.fieldnames or [])]
                for i, r in enumerate(reader):
                    rows.append({(k or "").strip(): v for k, v in r.items()})
                    if limit and i + 1 >= limit:
                        break
        elif file_type == "jsonl":
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        report["errors"].append("有一行不是合法 JSON，已跳过")
                        continue
                    if isinstance(obj, dict):
                        rows.append(obj)
                    if limit and len(rows) >= limit:
                        break
        elif file_type == "json":
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise KbSourceError("JSON 文件格式不正确") from exc
            rows = extract_json_rows(data)
            if not rows:
                raise KbSourceError("JSON 里没有找到选题列表（需要数组，或 items/data 字段里的数组）")
            if limit:
                rows = rows[:limit]
        elif file_type == "xlsx":
            cols, rows = read_xlsx_rows(p)
            if limit:
                rows = rows[:limit]
        else:
            raise KbSourceError(f"不支持的选题库类型：{file_type}")
        if not cols:
            seen: dict[str, None] = {}
            for r in rows[:50]:
                for k in r.keys():
                    seen.setdefault(str(k), None)
            cols = list(seen)
        report.setdefault("stats", {})["total_rows"] = report.get("stats", {}).get("total_rows", len(rows))
        return cols, rows

    def _to_items(self, rows: list[dict], mapping: dict[str, str], report: dict) -> list[TopicItem]:
        default_dim = (self.kb_cfg.default_dimension or "").strip()
        items: list[TopicItem] = []
        skipped = 0
        defaulted = 0
        for r in rows:
            def g(field: str) -> str:
                v = r.get(mapping.get(field, field))
                return "" if v is None else str(v).strip()
            title = g("title")
            if not title:
                skipped += 1
                continue
            dim = g("dimension")
            if not dim:
                if not default_dim:
                    skipped += 1
                    continue
                dim = default_dim
                defaulted += 1
            link = g("link")
            source = g("source")
            tid = link or source
            if not tid or tid == source:
                # 没有链接时用标题生成稳定 id，避免同来源的选题互相覆盖
                tid = "kb:" + hashlib.md5(f"{title}|{dim}".encode()).hexdigest()[:16]
            items.append(TopicItem(
                id=tid, title=title, dimension=dim,
                summary=g("summary"), link=link, source=source,
                date=g("date") or None,
            ))
        if skipped:
            report["errors"].append(f"{skipped} 行缺少标题{'或维度' if not default_dim else ''}，已跳过")
        if defaulted:
            report["defaulted_dimension"] = defaulted
        return items

    # ------------------------------------------------------------------
    # Schema 检查 + 预览（选题库设置页）
    # ------------------------------------------------------------------
    def inspect(self, preview_rows: int = 5) -> dict:
        """列名、行数、自动识别的映射、前几行预览。未配置时返回 configured=False。"""
        p = self.path
        report: dict = {
            "configured": p is not None,
            "path": display_path(p) if p else "",
            "source_type": self.kb_cfg.source_type,
            "file_type": None,
            "field_mapping": {},
            "columns": [],
            "missing_fields": [],
            "preview": [],
            "stats": {},
            "errors": [],
            "ok": False,
        }
        if p is None:
            report["message"] = "还没有配置选题库。可以上传文件、填写远程地址或服务器路径；也可以先不配，只用热点转入和手动添加的选题。"
            return report
        p = self._require_path()
        file_type = self._detect_type(p)
        report["file_type"] = file_type
        if file_type == "sqlite":
            cols, rows = self._sqlite_rows(p, report, limit=preview_rows)
        else:
            cols, rows = self._file_rows(p, file_type, report, limit=None if file_type == "json" else 2000)
            report["stats"]["total_rows"] = len(rows)
            rows = rows[:max(preview_rows, 0) or 5] if rows else []
        mapping = self._resolve_mapping(cols)
        report["columns"] = cols
        report["field_mapping"] = mapping
        cols_set = set(cols)
        report["missing_fields"] = [f for f, raw in mapping.items() if raw not in cols_set]
        report["preview"] = [
            {f: ("" if r.get(mapping[f]) is None else str(r.get(mapping[f]))[:120]) for f in SUPPORTED_FIELDS}
            for r in rows[:preview_rows]
        ]
        report["stats"]["total"] = report["stats"].get("total_rows", 0)
        has_dim = "dimension" not in report["missing_fields"] or bool((self.kb_cfg.default_dimension or "").strip())
        report["ok"] = "title" not in report["missing_fields"] and has_dim
        if "title" in report["missing_fields"]:
            report["message"] = "没有找到「标题」列，请在字段对应里选择哪一列是标题"
        elif "dimension" in report["missing_fields"]:
            report["message"] = (f"没有维度列，所有选题会归到默认维度「{self.kb_cfg.default_dimension}」"
                                 if has_dim else "没有找到「维度」列，请选择维度列或设置默认维度")
        return report
