#!/usr/bin/env python3
from __future__ import annotations

"""将医院标准类 DOCX 文档转换为结构化 JSONL。

输出记录包含：
- title / paragraph / list_item
- table_row（携带 table_index、row_index 与列字段）
"""

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from docx import Document
from docx.document import Document as _Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph


def clean_text(text: str) -> str:
    """统一空白字符，减少版式差异对后续解析的影响。"""
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def iter_block_items(parent):
    """按文档中的实际顺序迭代段落与表格。"""
    if isinstance(parent, _Document):
        parent_elm = parent.element.body
    else:
        parent_elm = parent._tc
    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def heading_level(style_name: str) -> int | None:
    """从段落样式名中提取标题层级（兼容英文 Heading/中文标题）。"""
    if not style_name:
        return None
    if style_name.lower().startswith("heading"):
        m = re.search(r"(\d+)", style_name)
        return int(m.group(1)) if m else 1
    if "标题" in style_name:
        m = re.search(r"(\d+)", style_name)
        return int(m.group(1)) if m else 1
    return None


def is_list_paragraph(paragraph: Paragraph) -> bool:
    """通过 Word 编号属性判断是否为列表段落。"""
    p_pr = paragraph._p.pPr
    return bool(p_pr is not None and p_pr.numPr is not None)


def infer_title_level_from_text(text: str) -> int | None:
    """当样式不可用时，基于常见中文标题编号模式推断层级。"""
    if re.match(r"^[一二三四五六七八九十]+、", text) and len(text) <= 30:
        return 1
    if re.match(r"^[（(][一二三四五六七八九十]+[)）]", text) and len(text) <= 24:
        return 2
    if re.match(r"^\d+[\.、]", text) and len(text) <= 20:
        return 3
    return None


def is_list_text(text: str) -> bool:
    """基于文本前缀判断是否是列表项（如（一）、1.、-）。"""
    return bool(
        re.match(r"^[（(][一二三四五六七八九十]+[)）]", text)
        or re.match(r"^[（(]?\d+[)）\.、]", text)
        or re.match(r"^[\-•·]\s*", text)
    )


def unique_headers(headers: list[str]) -> list[str]:
    """清洗并去重表头，保证生成的 JSON 字段名唯一。"""
    counter: Counter[str] = Counter()
    out: list[str] = []
    for i, h in enumerate(headers, 1):
        key = h if h else f"col_{i}"
        key = re.sub(r"\s+", "", key)
        counter[key] += 1
        if counter[key] > 1:
            key = f"{key}_{counter[key]}"
        out.append(key)
    return out


def looks_like_header_row(row: list[str]) -> bool:
    """用关键词启发式判断首行是否为表头。"""
    keywords = [
        "序号",
        "类别",
        "业务项目",
        "等级",
        "是否为基本项",
        "系统功能评估内容",
        "内容",
        "项目数",
        "总分",
        "应用评估",
    ]
    norm = [re.sub(r"\s+", "", c) for c in row if c]
    if not norm:
        return False
    score = 0
    for cell in norm:
        if any(k in cell for k in keywords):
            score += 1
    return score >= 2


def tc_text(tc) -> str:
    """提取单元格文本（拼接所有 w:t 节点）。"""
    parts = []
    for t in tc.xpath(".//w:t"):
        if t.text:
            parts.append(t.text)
    return clean_text("".join(parts))


def expand_table(table: Table) -> list[list[str]]:
    """展开 Word 表格为二维矩阵，并处理横向/纵向合并单元格。"""
    tr_list = list(table._tbl.tr_lst)
    if not tr_list:
        return []

    # 优先使用表格网格定义的列数；缺失时再从每行 gridSpan 推断。
    max_cols = len(table.columns) if table.columns else 0
    if max_cols == 0:
        for tr in tr_list:
            col_count = 0
            for tc in tr.tc_lst:
                tc_pr = tc.tcPr
                span = int(tc_pr.gridSpan.val) if tc_pr is not None and tc_pr.gridSpan is not None else 1
                col_count += span
            max_cols = max(max_cols, col_count)
    if max_cols == 0:
        return []

    matrix: list[list[str]] = []
    prev_row = [""] * max_cols

    for tr in tr_list:
        row = [None] * max_cols
        col = 0

        for tc in tr.tc_lst:
            while col < max_cols and row[col] is not None:
                col += 1
            if col >= max_cols:
                break

            tc_pr = tc.tcPr
            span = int(tc_pr.gridSpan.val) if tc_pr is not None and tc_pr.gridSpan is not None else 1

            vmerge_state = None
            if tc_pr is not None and tc_pr.vMerge is not None:
                vmerge_state = str(tc_pr.vMerge.val) if tc_pr.vMerge.val is not None else "continue"

            text = tc_text(tc)
            for offset in range(span):
                c = col + offset
                if c >= max_cols:
                    break
                if vmerge_state == "continue":
                    row[c] = prev_row[c]
                else:
                    row[c] = text

            col += span

        # 未填充列通常来自纵向合并造成的省略，沿用上一行同列值。
        for c in range(max_cols):
            if row[c] is None:
                row[c] = prev_row[c] if prev_row[c] else ""

        matrix.append([clean_text(v) for v in row])
        prev_row = matrix[-1]

    # 最后再做一次向下填充，兜底清理残余空白。
    for r in range(1, len(matrix)):
        for c in range(max_cols):
            if not matrix[r][c] and matrix[r - 1][c]:
                matrix[r][c] = matrix[r - 1][c]

    return matrix


def convert(input_path: Path, output_path: Path) -> None:
    """执行 DOCX -> JSONL 转换主流程。"""
    doc = Document(str(input_path))
    records: list[dict] = []
    table_index = 0
    # 按列数缓存最近表头，处理跨页/续表场景下“无表头数据块”。
    schema_by_cols: dict[int, list[str]] = {}

    first_text_seen = False
    for block in iter_block_items(doc):
        if isinstance(block, Paragraph):
            text = clean_text(block.text)
            if not text:
                continue

            if not first_text_seen:
                # 首个正文块通常是文档标题，统一标记为一级标题。
                records.append({"type": "title", "level": 1, "content": text})
                first_text_seen = True
                continue

            lvl = heading_level(block.style.name if block.style else "")
            if lvl is not None:
                records.append({"type": "title", "level": lvl, "content": text})
            elif (inferred_lvl := infer_title_level_from_text(text)) is not None:
                records.append({"type": "title", "level": inferred_lvl, "content": text})
            elif is_list_paragraph(block):
                records.append({"type": "list_item", "content": text})
            elif is_list_text(text):
                records.append({"type": "list_item", "content": text})
            else:
                records.append({"type": "paragraph", "content": text})
            continue

        table_index += 1
        matrix = expand_table(block)
        if not matrix:
            continue

        col_count = len(matrix[0])
        if looks_like_header_row(matrix[0]):
            headers = unique_headers(matrix[0])
            schema_by_cols[col_count] = headers
            data_rows = matrix[1:]
        else:
            headers = schema_by_cols.get(col_count, [f"col_{i+1}" for i in range(col_count)])
            data_rows = matrix

        for offset, row in enumerate(data_rows, 1):
            if not any(row):
                continue
            item = {"type": "table_row", "table_index": table_index, "row_index": offset}
            for k, v in zip(headers, row):
                item[k] = v
            records.append(item)

    with output_path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="Convert DOCX to JSONL (title/paragraph/list/table_row).")
    parser.add_argument("input", type=Path, help="Input .docx path")
    parser.add_argument("-o", "--output", type=Path, help="Output .jsonl path")
    args = parser.parse_args()

    input_path = args.input
    output_path = args.output or input_path.with_suffix(".jsonl")
    convert(input_path, output_path)
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
