#!/usr/bin/env python3
from __future__ import annotations

"""本地知识库索引构建脚本。

能力：
1. 扫描输入目录中的 DOCX/JSONL；
2. 将 DOCX 转为 JSONL；
3. 写入 SQLite（原文记录 + FTS + n-gram 索引）。
"""

import argparse
import hashlib
import json
import shutil
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Iterable

from docx_to_jsonl import convert as convert_docx_to_jsonl

EXCLUDED_DIRS = {".git", ".venv", "__pycache__"}
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
DEFAULT_KB_ROOT = SKILL_ROOT / "kb"


@dataclass
class PreparedDoc:
    """待入库文档的标准化描述。"""
    source_file: str
    source_kind: str  # 来源类型：docx|jsonl
    jsonl_path: Path
    doc_id: str


def normalize_space(text: str) -> str:
    """统一空白字符，减少不同来源文本的格式噪声。"""
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def norm_for_gram(text: str) -> str:
    """为 n-gram 建模准备文本：小写化并仅保留中英文与数字。"""
    text = normalize_space(text).lower()
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)
    return text


def char_ngrams(text: str, ns: tuple[int, ...] = (2, 3)) -> Counter[str]:
    """按字符生成 n-gram 词频，用于构建模糊召回索引。"""
    cleaned = norm_for_gram(text)
    grams: Counter[str] = Counter()
    for n in ns:
        if len(cleaned) < n:
            continue
        for i in range(len(cleaned) - n + 1):
            grams[cleaned[i : i + n]] += 1
    return grams


def stable_doc_id(source_file: str) -> str:
    """根据逻辑路径生成稳定 doc_id，保证重复导入可去重。"""
    logical_id = str(Path(source_file).with_suffix("")).replace("\\", "/")
    return hashlib.sha1(logical_id.encode("utf-8")).hexdigest()


def safe_jsonl_name(source_file: str) -> str:
    """生成可落盘且基本可读的 JSONL 文件名。"""
    p = Path(source_file)
    stem = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", p.stem)
    digest = hashlib.sha1(source_file.encode("utf-8")).hexdigest()[:10]
    return f"{stem}__{digest}.jsonl"


def iter_files(root: Path, suffixes: set[str], kb_root: Path) -> Iterable[Path]:
    """遍历输入目录并过滤不应入库的文件。"""
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in suffixes:
            continue
        if kb_root == path or kb_root in path.parents:
            continue
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        yield path


def validate_jsonl(path: Path) -> None:
    """校验 JSONL 基本合法性：每行需是 JSON 对象。"""
    with path.open("r", encoding="utf-8") as f:
        for idx, line in enumerate(f, 1):
            raw = line.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError(f"{path} line {idx}: JSON must be object")


def build_content(record: dict) -> str:
    """将结构化记录统一转换为可检索文本 content。"""
    rtype = str(record.get("type", "")).strip()

    if rtype in {"title", "paragraph", "list_item"}:
        return normalize_space(str(record.get("content", "")))

    if rtype == "table_row":
        # 表格行转成“列名: 值”串联文本，便于关键词检索与追溯。
        pieces: list[str] = []
        for k, v in record.items():
            if k in {"type", "table_index", "row_index"}:
                continue
            sval = normalize_space(str(v))
            if not sval:
                continue
            pieces.append(f"{k}: {sval}")
        return " | ".join(pieces)

    if "content" in record:
        text = normalize_space(str(record.get("content", "")))
        if text:
            return text

    pieces = []
    for k, v in record.items():
        if k == "type":
            continue
        sval = normalize_space(str(v))
        if sval:
            pieces.append(f"{k}: {sval}")
    return " | ".join(pieces)


def parse_int_or_none(v) -> int | None:
    """将值安全转换为整数；失败时返回 None。"""
    if v is None:
        return None
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def init_db(conn: sqlite3.Connection) -> None:
    """初始化数据库结构（重建模式）。"""
    cur = conn.cursor()
    cur.executescript(
        """
        DROP TABLE IF EXISTS ngrams;
        DROP TABLE IF EXISTS records;
        DROP TABLE IF EXISTS docs;
        DROP TABLE IF EXISTS records_fts;

        CREATE TABLE docs (
            doc_id TEXT PRIMARY KEY,
            source_file TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            jsonl_path TEXT NOT NULL,
            record_count INTEGER NOT NULL DEFAULT 0,
            ingested_at TEXT NOT NULL
        );

        CREATE TABLE records (
            record_id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT NOT NULL,
            source_file TEXT NOT NULL,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            table_index INTEGER,
            row_index INTEGER,
            raw_json TEXT NOT NULL,
            FOREIGN KEY(doc_id) REFERENCES docs(doc_id)
        );

        CREATE VIRTUAL TABLE records_fts USING fts5(
            record_id UNINDEXED,
            content,
            tokenize='unicode61'
        );

        CREATE TABLE ngrams (
            record_id INTEGER NOT NULL,
            gram TEXT NOT NULL,
            tf INTEGER NOT NULL,
            FOREIGN KEY(record_id) REFERENCES records(record_id)
        );

        CREATE INDEX idx_records_doc ON records(doc_id);
        CREATE INDEX idx_records_source_file ON records(source_file);
        CREATE INDEX idx_ngrams_gram ON ngrams(gram);
        CREATE INDEX idx_ngrams_record_id ON ngrams(record_id);
        """
    )
    conn.commit()


def ingest_prepared_docs(conn: sqlite3.Connection, docs: list[PreparedDoc]) -> dict:
    """把准备好的 JSONL 文档批量写入 SQLite 索引。"""
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    total_records = 0
    for doc in docs:
        doc_record_count = 0
        with doc.jsonl_path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                raw = line.strip()
                if not raw:
                    continue

                try:
                    record = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{doc.jsonl_path} line {line_no} JSON parse error: {exc}") from exc

                if not isinstance(record, dict):
                    continue

                rtype = normalize_space(str(record.get("type", "unknown"))) or "unknown"
                content = build_content(record)
                if not content:
                    continue

                table_index = parse_int_or_none(record.get("table_index"))
                row_index = parse_int_or_none(record.get("row_index"))
                raw_json = json.dumps(record, ensure_ascii=False)

                cur.execute(
                    """
                    INSERT INTO records (
                        doc_id, source_file, type, content, table_index, row_index, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc.doc_id,
                        doc.source_file,
                        rtype,
                        content,
                        table_index,
                        row_index,
                        raw_json,
                    ),
                )
                rid = int(cur.lastrowid)
                cur.execute(
                    "INSERT INTO records_fts(record_id, content) VALUES (?, ?)",
                    (rid, content),
                )

                # 同步写入 n-gram 倒排表，用于后续模糊匹配召回。
                grams = char_ngrams(content)
                if grams:
                    cur.executemany(
                        "INSERT INTO ngrams(record_id, gram, tf) VALUES (?, ?, ?)",
                        [(rid, g, int(tf)) for g, tf in grams.items()],
                    )

                doc_record_count += 1
                total_records += 1

        cur.execute(
            """
            INSERT INTO docs (doc_id, source_file, source_kind, jsonl_path, record_count, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                doc.doc_id,
                doc.source_file,
                doc.source_kind,
                str(doc.jsonl_path),
                doc_record_count,
                now,
            ),
        )

    conn.commit()
    return {"doc_count": len(docs), "record_count": total_records}


def prepare_inputs(args) -> list[PreparedDoc]:
    """收集输入文件并统一产出可入库的 PreparedDoc 列表。"""
    input_root = args.input_root.resolve()
    kb_root = args.kb_root.resolve()
    kb_jsonl_dir = kb_root / "jsonl"
    kb_jsonl_dir.mkdir(parents=True, exist_ok=True)

    include_docx = args.include_docx
    include_jsonl = args.include_jsonl
    # 未显式指定时，默认两类都收集。
    if not include_docx and not include_jsonl:
        include_docx = True
        include_jsonl = True

    prepared: list[PreparedDoc] = []

    if include_docx:
        for docx_path in iter_files(input_root, {".docx"}, kb_root):
            source_file = str(docx_path.relative_to(input_root)).replace("\\", "/")
            out_jsonl = kb_jsonl_dir / safe_jsonl_name(source_file)
            # 先把 DOCX 结构化为 JSONL，再统一走入库逻辑。
            convert_docx_to_jsonl(docx_path, out_jsonl)
            prepared.append(
                PreparedDoc(
                    source_file=source_file,
                    source_kind="docx",
                    jsonl_path=out_jsonl,
                    doc_id=stable_doc_id(source_file),
                )
            )

    if include_jsonl:
        for jsonl_path in iter_files(input_root, {".jsonl"}, kb_root):
            source_file = str(jsonl_path.relative_to(input_root)).replace("\\", "/")
            validate_jsonl(jsonl_path)

            out_jsonl = kb_jsonl_dir / safe_jsonl_name(source_file)
            # 复制到 kb/jsonl，确保索引输入稳定、可复现。
            shutil.copyfile(jsonl_path, out_jsonl)
            prepared.append(
                PreparedDoc(
                    source_file=source_file,
                    source_kind="jsonl",
                    jsonl_path=out_jsonl,
                    doc_id=stable_doc_id(source_file),
                )
            )

    # 基于 doc_id 去重：若同文档同时存在 docx 与 jsonl，优先保留显式 jsonl。
    dedup: dict[str, PreparedDoc] = {}
    for doc in prepared:
        if doc.doc_id in dedup and dedup[doc.doc_id].source_kind == "docx" and doc.source_kind == "jsonl":
            dedup[doc.doc_id] = doc
        elif doc.doc_id not in dedup:
            dedup[doc.doc_id] = doc

    return sorted(dedup.values(), key=lambda x: x.source_file)


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="Build local KB index from docx/jsonl files.")
    parser.add_argument(
        "--kb-root",
        type=Path,
        default=DEFAULT_KB_ROOT,
        help=f"KB output root, default {DEFAULT_KB_ROOT}",
    )
    parser.add_argument("--input-root", type=Path, default=Path("."), help="Input root directory")
    parser.add_argument("--include-docx", action="store_true", help="Include .docx source files")
    parser.add_argument("--include-jsonl", action="store_true", help="Include .jsonl source files")
    args = parser.parse_args()

    kb_root = args.kb_root.resolve()
    kb_root.mkdir(parents=True, exist_ok=True)
    kb_db = kb_root / "kb.sqlite"

    prepared_docs = prepare_inputs(args)
    if not prepared_docs:
        print("No input files found (.docx/.jsonl). Nothing to ingest.")
        return

    conn = sqlite3.connect(str(kb_db))
    try:
        init_db(conn)
        result = ingest_prepared_docs(conn, prepared_docs)
    finally:
        conn.close()

    print("KB ingest completed")
    print(f"- kb_db: {kb_db}")
    print(f"- docs: {result['doc_count']}")
    print(f"- records: {result['record_count']}")


if __name__ == "__main__":
    main()
