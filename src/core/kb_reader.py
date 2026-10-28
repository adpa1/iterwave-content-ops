"""
动态选题库读取 — SQLite/CSV/JSONL auto schema detection（§16.1）。

R6.5 复用检查：Easel 有 easel/kb/ 模块（SQLite 存储 + import），但没有 auto detection。
→ 独立实现。

配置入口：config.topic_kb
  type=auto  → 检测扩展名
  path=...   → 选题库文件路径
  field_mapping → 通用字段 → 原始字段名（type=auto 检测到歧义时必填）

通用字段（设计方案 §16.1）：source / title / summary / link / date / dimension
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .scoring.topic_eval import TopicItem

logger = logging.getLogger(__name__)

SUPPORTED_FIELDS = ("source", "title", "summary", "link", "date", "dimension")


class KbReader:
    """选题库读取器。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.kb_cfg = cfg.topic_kb
        self.path = Path(self.kb_cfg.path)

    # ------------------------------------------------------------------
    def read(self) -> tuple[list[TopicItem], dict]:
        """读取选题库，返回 (TopicItem 列表, 检测报告)。"""
        if not self.path.exists():
            raise FileNotFoundError(f"选题库不存在: {self.path}")

        file_type = self._detect_type()
        mapping = self._resolve_mapping(file_type)
        report = {
            "file_type": file_type,
            "path": str(self.path),
            "field_mapping": mapping,
            "errors": [],
            "stats": {},
        }

        if file_type == "sqlite":
            items = self._read_sqlite(mapping, report)
        elif file_type == "csv":
            items = self._read_csv(mapping, report)
        elif file_type == "jsonl":
            items = self._read_jsonl(mapping, report)
        else:
            raise ValueError(f"不支持的选题库类型: {file_type}")

        # 必要字段校验（必要字段 = title + dimension）
        required = {"title", "dimension"}
        missing = [f for f in required if not any(
            getattr(it, f) for it in items
        )]
        if missing:
            report["errors"].append(f"必要字段缺失: {missing}")
            logger.warning("选题库必要字段缺失: %s", missing)

        # 自动写回 topic_index（增量更新）
        # 这个可以后做，这里只返回 items
        report["stats"] = {
            "total": len(items),
            "unique_dimensions": len({it.dimension for it in items}),
        }
        logger.info("📚 选题库读取完成: %s 条, %s 个维度", len(items), len({it.dimension for it in items}))
        return items, report

    # ------------------------------------------------------------------
    def _detect_type(self) -> str:
        """type=auto 时自动检测。"""
        explicit = self.kb_cfg.type
        if explicit != "auto":
            return explicit

        ext = self.path.suffix.lower()
        mapping = {
            ".db": "sqlite",
            ".sqlite": "sqlite",
            ".sqlite3": "sqlite",
            ".csv": "csv",
            ".jsonl": "jsonl",
            ".ndjson": "jsonl",
        }
        if ext not in mapping:
            raise ValueError(
                f"无法自动检测文件类型（后缀 {ext}），请在 config.yaml topic_kb.type 显式指定"
            )
        return mapping[ext]

    def _resolve_mapping(self, file_type: str) -> dict[str, str]:
        """解析 field_mapping（用户显式配置或 auto 检测）。"""
        configured = self.kb_cfg.field_mapping
        if configured and all(k in SUPPORTED_FIELDS for k in configured.keys()):
            # 用户配了，用用户的
            mapping = {f: configured.get(f, f) for f in SUPPORTED_FIELDS}
        else:
            # auto 模式：假设通用字段名一致
            mapping = {f: f for f in SUPPORTED_FIELDS}
        return mapping

    # ------------------------------------------------------------------
    # Schema 检查（Assets 字段映射 tab）
    # ------------------------------------------------------------------
    def inspect(self) -> dict:
        """轻量 schema 检查（Assets 字段映射 tab）：不读全量行，只查列名 + 计数。

        检测失败必须显式报错，禁止静默猜测（§16.1）。
        """
        if not self.path.exists():
            raise FileNotFoundError(f"选题库不存在: {self.path}")

        file_type = self._detect_type()
        mapping = self._resolve_mapping(file_type)
        report: dict = {
            "path": str(self.path),
            "file_type": file_type,
            "field_mapping": mapping,
            "columns": [],
            "missing_fields": [],
            "stats": {},
            "ok": False,
        }

        if file_type == "sqlite":
            conn = sqlite3.connect(str(self.path))
            try:
                conn.row_factory = sqlite3.Row
                table_name = self.kb_cfg.table_name
                if not table_name:
                    tables = [
                        r[0] for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    ]
                    table_name = tables[0] if tables else None
                if not table_name:
                    report["error"] = "SQLite 文件没有表"
                    return report
                report["table"] = table_name
                cols = [r[1] for r in conn.execute(
                    f"PRAGMA table_info([{table_name}])"
                ).fetchall()]
                report["columns"] = cols
                total = conn.execute(
                    f"SELECT COUNT(*) FROM [{table_name}]"
                ).fetchone()[0]
                report["stats"]["total"] = total
                dim_col = mapping.get("dimension")
                if dim_col in cols:
                    report["stats"]["unique_dimensions"] = conn.execute(
                        f"SELECT COUNT(DISTINCT [{dim_col}]) FROM [{table_name}]"
                    ).fetchone()[0]
            finally:
                conn.close()
        else:
            # CSV / JSONL：只读首行表头
            if file_type == "csv":
                with open(self.path, "r", encoding="utf-8") as f:
                    report["columns"] = next(csv.reader(f), [])
            else:
                with open(self.path, "r", encoding="utf-8") as f:
                    first = f.readline().strip()
                report["columns"] = list(json.loads(first).keys()) if first else []

        cols_set = set(report["columns"])
        report["missing_fields"] = [
            f for f, raw in mapping.items() if raw not in cols_set
        ]
        # title / dimension 必须能解析到真实列，否则阻断该数据源
        report["ok"] = not any(
            f in report["missing_fields"] for f in ("title", "dimension")
        )
        return report

    # ------------------------------------------------------------------
    def _read_sqlite(self, mapping: dict[str, str], report: dict) -> list[TopicItem]:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row

        # 决定读哪张表
        table_name = self.kb_cfg.table_name
        if not table_name:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = [r[0] for r in cur.fetchall()]
            if not tables:
                raise ValueError("SQLite 文件没有表")
            # 选含最多行的表
            counts = []
            for t in tables:
                try:
                    c = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
                    counts.append((c, t))
                except sqlite3.Error:
                    pass
            table_name = max(counts)[1] if counts else tables[0]
            report["auto_table"] = table_name

        # 读字段
        cur = conn.execute(f"PRAGMA table_info([{table_name}])")
        db_cols = [r[1] for r in cur.fetchall()]

        # 构建 SELECT
        select_cols = []
        for field in SUPPORTED_FIELDS:
            raw_col = mapping[field]
            if raw_col in db_cols:
                select_cols.append(f"[{raw_col}] as [{field}]")
            else:
                # 找不到的字段用 NULL 占位
                select_cols.append(f"NULL as [{field}]")

        sql = f"SELECT {', '.join(select_cols)} FROM [{table_name}]"
        rows = conn.execute(sql).fetchall()
        conn.close()

        items = []
        for r in rows:
            d = dict(r)
            tid = d.get("link") or d.get("source") or ""
            items.append(TopicItem(
                id=tid,
                title=d.get("title") or "",
                dimension=d.get("dimension") or "",
                summary=d.get("summary") or "",
                link=d.get("link") or "",
                source=d.get("source") or "",
                date=d.get("date"),
            ))
        return items

    # ------------------------------------------------------------------
    # CSV
    # ------------------------------------------------------------------
    def _read_csv(self, mapping: dict[str, str], report: dict) -> list[TopicItem]:
        with open(self.path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            db_cols = reader.fieldnames or []

            # 反转 mapping：通用字段 → 原始字段（现在 mapping 是通用→原始，要直接用）
            items = []
            for row in reader:
                title = row.get(mapping.get("title", "title"), "")
                dim = row.get(mapping.get("dimension", "dimension"), "")
                link = row.get(mapping.get("link", "link"), "")
                if not title or not dim:
                    report["errors"].append(f"跳过缺必要字段的行: {row.get('link', '?')}")
                    continue
                tid = link or row.get(mapping.get("source", "source"), "")
                items.append(TopicItem(
                    id=tid, title=title, dimension=dim,
                    summary=row.get(mapping.get("summary", "summary"), ""),
                    link=link, source=row.get(mapping.get("source", "source"), ""),
                    date=row.get(mapping.get("date", "date")),
                ))
        return items

    # ------------------------------------------------------------------
    # JSONL
    # ------------------------------------------------------------------
    def _read_jsonl(self, mapping: dict[str, str], report: dict) -> list[TopicItem]:
        items = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    report["errors"].append(f"JSONL 解析错误: {exc}")
                    continue
                title = obj.get(mapping.get("title", "title"), "")
                dim = obj.get(mapping.get("dimension", "dimension"), "")
                link = obj.get(mapping.get("link", "link"), "")
                if not title or not dim:
                    continue
                items.append(TopicItem(
                    id=link or obj.get(mapping.get("source", "source"), ""),
                    title=title, dimension=dim,
                    summary=obj.get(mapping.get("summary", "summary"), ""),
                    link=link, source=obj.get(mapping.get("source", "source"), ""),
                    date=obj.get(mapping.get("date", "date")),
                ))
        return items
