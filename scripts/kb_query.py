#!/usr/bin/env python3
from __future__ import annotations

"""本地知识库检索与问答草稿生成脚本。

流程概览：
1. 对用户问题做清洗与分词；
2. 分别用 FTS 关键词召回和字符 n-gram 召回；
3. 融合打分并筛选证据；
4. 生成带引用位置的回答草稿。
"""

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


def normalize_space(text: str) -> str:
    """统一空白字符，避免换行/全角空格影响检索。"""
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def norm_for_gram(text: str) -> str:
    """为 n-gram 建模准备文本：小写化并仅保留中英文与数字。"""
    text = normalize_space(text).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def char_ngrams(text: str, ns: tuple[int, ...] = (2, 3)) -> Counter[str]:
    """按字符生成 n-gram 词频，用于模糊匹配打分。"""
    cleaned = norm_for_gram(text)
    grams: Counter[str] = Counter()
    for n in ns:
        if len(cleaned) < n:
            continue
        for i in range(len(cleaned) - n + 1):
            grams[cleaned[i : i + n]] += 1
    return grams


def extract_terms(question: str) -> list[str]:
    """抽取检索关键词（中文短语、英文词、数字），并做去重。"""
    terms = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]+", question)
    uniq: list[str] = []
    seen = set()
    for t in terms:
        t = t.strip()
        if len(t) < 2:
            continue
        if t in seen:
            continue
        seen.add(t)
        uniq.append(t)
    return uniq


def core_question_phrases(question: str) -> list[str]:
    """提取问题核心短语，用于后续“短语精确命中”判断。"""
    q = normalize_space(question)
    for marker in [
        "的要求是什么",
        "是什么",
        "有哪些",
        "有什么",
        "有何",
        "要求",
        "如何",
        "请问",
        "？",
        "?",
    ]:
        q = q.replace(marker, "")

    phrases = re.findall(r"[\u4e00-\u9fff]{4,}|[A-Za-z0-9_]{4,}", q)
    seen = set()
    out = []
    for p in phrases:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def fts_retrieve(conn: sqlite3.Connection, question: str, limit: int) -> tuple[dict[int, float], dict[int, sqlite3.Row]]:
    """基于 FTS 召回候选记录，并输出 record_id -> score 映射。

    返回两个结构：
    - scores: record_id 到分数的映射
    - rows_map: record_id 到原始行数据的映射（避免后续重复查库）
    """
    cur = conn.cursor()
    terms = extract_terms(question)
    scores: dict[int, float] = {}
    rows_map: dict[int, sqlite3.Row] = {}

    if terms:
        # 限制关键词数量，避免 FTS 查询表达式过长。
        fts_query = " OR ".join(f'"{t.replace(chr(34), " ")}"' for t in terms[:10])
    else:
        fts_query = question.replace('"', " ").strip()

    if fts_query:
        try:
            rows = cur.execute(
                """
                SELECT
                    r.record_id,
                    r.source_file,
                    r.type,
                    r.table_index,
                    r.row_index,
                    r.content,
                    bm25(records_fts) AS rank
                FROM records_fts
                JOIN records r ON r.record_id = CAST(records_fts.record_id AS INTEGER)
                WHERE records_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()

            for row in rows:
                rid = int(row["record_id"])
                rank = float(row["rank"]) if row["rank"] is not None else 1000.0
                # 某些 SQLite FTS/BM25 配置会返回负值，这里统一为正值参与转换。
                if rank < 0:
                    rank = abs(rank)
                score = 1.0 / (1.0 + rank)
                if score > scores.get(rid, 0.0):
                    scores[rid] = score
                    rows_map[rid] = row
        except sqlite3.OperationalError:
            # FTS 表不存在或查询异常时，降级走 LIKE 召回，不中断流程。
            pass

    # 对中文或符号较多的问题，LIKE 往往更稳，作为兜底召回。
    like_terms = terms[:8] if terms else [question.strip()]
    like_terms = [t for t in like_terms if t]
    if like_terms:
        where = " OR ".join(["content LIKE ?"] * len(like_terms))
        params = [f"%{t}%" for t in like_terms] + [limit]
        rows = cur.execute(
            f"""
            SELECT record_id, source_file, type, table_index, row_index, content
            FROM records
            WHERE {where}
            LIMIT ?
            """,
            params,
        ).fetchall()

        for row in rows:
            rid = int(row["record_id"])
            content = row["content"]
            match_count = sum(1 for t in like_terms if t in content)
            score = match_count / max(1, len(like_terms))
            score = min(1.0, 0.75 * score)
            if score > scores.get(rid, 0.0):
                scores[rid] = score
                rows_map[rid] = row

    return scores, rows_map


def ngram_retrieve(conn: sqlite3.Connection, question: str, limit: int) -> dict[int, float]:
    """基于字符 n-gram 的重叠度召回，补足关键词检索的漏召回。"""
    q_grams = char_ngrams(question)
    if not q_grams:
        return {}

    grams = list(q_grams.keys())
    if not grams:
        return {}

    cur = conn.cursor()
    record_overlap: dict[int, float] = defaultdict(float)

    # 分批查询，避免 IN 子句过长。
    batch_size = 300
    for i in range(0, len(grams), batch_size):
        batch = grams[i : i + batch_size]
        placeholders = ",".join(["?"] * len(batch))
        rows = cur.execute(
            f"SELECT record_id, gram, tf FROM ngrams WHERE gram IN ({placeholders})",
            batch,
        ).fetchall()

        for row in rows:
            rid = int(row["record_id"])
            gram = row["gram"]
            tf = int(row["tf"])
            record_overlap[rid] += min(tf, q_grams.get(gram, 0))

    denom = max(1.0, float(sum(q_grams.values())))
    scores = {rid: min(1.0, overlap / denom) for rid, overlap in record_overlap.items()}

    sorted_ids = sorted(scores.keys(), key=lambda rid: scores[rid], reverse=True)[:limit]
    return {rid: scores[rid] for rid in sorted_ids}


def fetch_rows_by_ids(conn: sqlite3.Connection, ids: list[int]) -> dict[int, sqlite3.Row]:
    """按 record_id 批量回表读取详情。"""
    if not ids:
        return {}
    cur = conn.cursor()

    out: dict[int, sqlite3.Row] = {}
    batch_size = 300
    for i in range(0, len(ids), batch_size):
        batch = ids[i : i + batch_size]
        placeholders = ",".join(["?"] * len(batch))
        rows = cur.execute(
            f"""
            SELECT record_id, source_file, type, table_index, row_index, content
            FROM records
            WHERE record_id IN ({placeholders})
            """,
            batch,
        ).fetchall()
        for row in rows:
            out[int(row["record_id"])] = row
    return out


def combine_scores(
    kw_scores: dict[int, float],
    ng_scores: dict[int, float],
) -> dict[int, float]:
    """融合两路召回分数并给予双命中轻微加分。"""
    combined: dict[int, float] = {}
    all_ids = set(kw_scores) | set(ng_scores)
    for rid in all_ids:
        kw = kw_scores.get(rid, 0.0)
        ng = ng_scores.get(rid, 0.0)
        score = 0.70 * kw + 0.30 * ng
        if kw > 0 and ng > 0:
            score += 0.05
        combined[rid] = min(1.0, score)
    return combined


def make_draft_answer(question: str, evidence: list[dict], answerable: bool, gaps: list[str]) -> str:
    """将证据片段整理为可读的回答草稿。"""
    if not evidence:
        return "结论：证据不足，无法给出确定结论。\n依据（跨文档）：未检索到有效证据。"

    if answerable:
        lines = [
            "结论：根据当前知识库命中条款，可形成有证据支撑的回答。",
            "依据（跨文档）：",
        ]

        by_doc: dict[str, list[dict]] = defaultdict(list)
        for ev in evidence[:8]:
            by_doc[ev["source_file"]].append(ev)

        for doc, evs in by_doc.items():
            lines.append(f"- {doc}：")
            for ev in evs[:2]:
                lines.append(f"  - {ev['content'][:120]}")

        lines.append("引用：")
        for i, ev in enumerate(evidence[:8], 1):
            table = ev.get("table_index")
            row = ev.get("row_index")
            loc = f"table={table}, row={row}" if table is not None or row is not None else "table/row=n/a"
            lines.append(f"[{i}] {ev['source_file']} ({loc})")

        if gaps:
            lines.append("不确定点/缺口：")
            for g in gaps:
                lines.append(f"- {g}")

        return "\n".join(lines)

    lines = [
        "结论：证据不足，无法给出确定结论。",
        "依据（跨文档）：以下为相关但置信度不足的片段。",
    ]
    for i, ev in enumerate(evidence[:6], 1):
        table = ev.get("table_index")
        row = ev.get("row_index")
        loc = f"table={table}, row={row}" if table is not None or row is not None else "table/row=n/a"
        lines.append(f"[{i}] {ev['source_file']} ({loc}) {ev['content'][:140]}")

    if gaps:
        lines.append("不确定点/缺口：")
        for g in gaps:
            lines.append(f"- {g}")

    return "\n".join(lines)


def query_kb(kb_path: Path, question: str, top_k: int) -> dict:
    """执行完整检索链路，返回可回答性判断与证据。"""
    question = normalize_space(question)
    if not question:
        return {
            "answerable": False,
            "evidence": [],
            "draft_answer": "结论：证据不足，无法给出确定结论。",
            "gaps": ["问题为空，请提供具体问题。"],
        }

    conn = sqlite3.connect(str(kb_path))
    conn.row_factory = sqlite3.Row
    try:
        kw_scores, kw_rows = fts_retrieve(conn, question, max(20, top_k * 4))
        ng_scores = ngram_retrieve(conn, question, max(20, top_k * 6))
        combined = combine_scores(kw_scores, ng_scores)

        ranked_ids = sorted(
            combined.keys(),
            key=lambda rid: (combined[rid], kw_scores.get(rid, 0.0), ng_scores.get(rid, 0.0)),
            reverse=True,
        )

        if not ranked_ids:
            gaps = ["知识库中未检索到相关片段。"]
            return {
                "answerable": False,
                "evidence": [],
                "draft_answer": make_draft_answer(question, [], False, gaps),
                "gaps": gaps,
            }

        need_ids = ranked_ids[: max(top_k * 3, 30)]
        rows_cache = dict(kw_rows)
        missing = [rid for rid in need_ids if rid not in rows_cache]
        if missing:
            rows_cache.update(fetch_rows_by_ids(conn, missing))

        evidence: list[dict] = []
        top_combined_score = float(combined.get(ranked_ids[0], 0.0))
        # 仅保留相对高分片段：绝对下限 + 相对 top 分数阈值。
        min_keep_score = max(0.08, top_combined_score * 0.35)
        for rid in ranked_ids:
            row = rows_cache.get(rid)
            if row is None:
                continue
            score = float(combined.get(rid, 0.0))
            if score < min_keep_score:
                continue
            evidence.append(
                {
                    "source_file": row["source_file"],
                    "table_index": row["table_index"],
                    "row_index": row["row_index"],
                    "content": row["content"],
                    "score": round(score, 4),
                }
            )
            if len(evidence) >= top_k:
                break

        if not evidence:
            gaps = ["知识库中未检索到可用证据。"]
            return {
                "answerable": False,
                "evidence": [],
                "draft_answer": make_draft_answer(question, [], False, gaps),
                "gaps": gaps,
            }

        top_score = evidence[0]["score"]
        strong_count = sum(1 for ev in evidence if ev["score"] >= 0.30)
        core_phrases = core_question_phrases(question)
        # 规则化可回答判断：高分即通过，中分需多证据，低分需核心短语强命中。
        has_exact_phrase_hit = any(
            any(phrase in ev["content"] for phrase in core_phrases) for ev in evidence[:3]
        )
        answerable = bool(
            top_score >= 0.48
            or (top_score >= 0.34 and strong_count >= 2)
            or (has_exact_phrase_hit and top_score >= 0.18)
        )

        gaps: list[str] = []
        if not answerable:
            gaps.append("命中证据与问题关联度不足，无法形成确定结论。")

        draft_answer = make_draft_answer(question, evidence, answerable, gaps)
        return {
            "answerable": answerable,
            "evidence": evidence,
            "draft_answer": draft_answer,
            "gaps": gaps,
        }
    finally:
        conn.close()


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="Query local KB sqlite and return evidence-grounded answer draft.")
    parser.add_argument("--kb", type=Path, required=True, help="Path to kb sqlite, e.g. ./kb/kb.sqlite")
    parser.add_argument("--question", required=True, help="User question")
    parser.add_argument("--top-k", type=int, default=12, help="Number of evidence rows")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    args = parser.parse_args()

    result = query_kb(args.kb, args.question, args.top_k)

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return

    print(result["draft_answer"])
    if result["gaps"]:
        print("\n不确定点/缺口：")
        for g in result["gaps"]:
            print(f"- {g}")


if __name__ == "__main__":
    main()
