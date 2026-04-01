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
from typing import Any


SOURCE_POLICY = "local_kb_only"
DEFAULT_TERM_LEXICON = {
    "canonical_terms": [
        {
            "canonical_term": "电子病历",
            "domain": "电子病历分级",
            "aliases": [
                "电子病历分级",
                "电子病历评级",
                "病历分级",
                "病历评级",
                "电子病历应用水平",
                "电子病历应用水平分级",
            ],
        },
        {
            "canonical_term": "互联互通",
            "domain": "互联互通成熟度",
            "aliases": [
                "信息互联",
                "互联信息",
                "信息互通",
                "互联信息化",
                "互联互通成熟度",
                "信息互联互通",
            ],
        },
        {
            "canonical_term": "智慧服务",
            "domain": "智慧服务分级",
            "aliases": ["医院智慧服务", "智慧服务评级", "智慧服务等级", "智慧评级"],
        },
        {
            "canonical_term": "智慧管理",
            "domain": "智慧管理分级",
            "aliases": ["医院智慧管理", "智慧管理评级", "智慧管理等级"],
        },
    ],
    "ambiguous_level_domains": ["电子病历", "互联互通", "智慧服务", "智慧管理"],
}
ZH_NUM = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
NUM_ZH = {v: k for k, v in ZH_NUM.items()}


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


def load_term_lexicon(lexicon_path: Path | None) -> dict[str, Any]:
    """加载术语词表；若文件不可用则回退到内置默认词表。"""
    if lexicon_path is None or not lexicon_path.exists():
        return DEFAULT_TERM_LEXICON

    try:
        data = json.loads(lexicon_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_TERM_LEXICON

    if not isinstance(data, dict):
        return DEFAULT_TERM_LEXICON

    if "canonical_terms" not in data or not isinstance(data.get("canonical_terms"), list):
        return DEFAULT_TERM_LEXICON
    if "ambiguous_level_domains" not in data or not isinstance(data.get("ambiguous_level_domains"), list):
        return DEFAULT_TERM_LEXICON

    return data


def extract_level_token(question: str) -> str | None:
    """提取问题里的等级表达，例如 2级 / 二级。"""
    m = re.search(r"([0-8])\s*级", question)
    if m:
        return f"{m.group(1)}级"

    m = re.search(r"([零一二三四五六七八九])级", question)
    if m:
        n = ZH_NUM.get(m.group(1))
        if n is not None:
            return f"{n}级"
    return None


def extract_all_level_tokens(question: str) -> list[str]:
    """提取问题中所有等级表达，并统一成数字形式（如 2级）。"""
    levels: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"([0-8]|[零一二三四五六七八九])\s*级", question):
        raw = m.group(1)
        if raw.isdigit():
            lv = f"{raw}级"
        else:
            n = ZH_NUM.get(raw)
            if n is None:
                continue
            lv = f"{n}级"
        if lv in seen:
            continue
        seen.add(lv)
        levels.append(lv)
    return levels


def find_nearby_level_token(question: str, anchor: str, max_distance: int = 8) -> str | None:
    """在术语附近优先提取等级表达，避免多对象问句拿错等级。"""
    pos = question.find(anchor)
    if pos < 0:
        return None

    best: tuple[int, str] | None = None
    for m in re.finditer(r"([0-8]|[零一二三四五六七八九])\s*级", question):
        raw = m.group(1)
        if raw.isdigit():
            lv = f"{raw}级"
        else:
            n = ZH_NUM.get(raw)
            if n is None:
                continue
            lv = f"{n}级"

        dist = min(abs(m.start() - pos), abs(m.end() - (pos + len(anchor))))
        if dist > max_distance:
            continue
        if best is None or dist < best[0]:
            best = (dist, lv)

    return best[1] if best else None


def detect_domains_and_alias_hits(question: str, lexicon: dict[str, Any]) -> tuple[set[str], list[tuple[str, str]]]:
    """识别问题命中的标准对象，以及别名命中（alias -> canonical）。"""
    matched_domains: set[str] = set()
    alias_hits: list[tuple[str, str]] = []

    entries = lexicon.get("canonical_terms", [])
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        canonical = str(entry.get("canonical_term", "")).strip()
        aliases = entry.get("aliases", [])
        if not canonical:
            continue

        if canonical in question:
            matched_domains.add(canonical)

        for alias in aliases if isinstance(aliases, list) else []:
            alias = str(alias).strip()
            if not alias:
                continue
            if alias in question:
                matched_domains.add(canonical)
                if alias != canonical:
                    alias_hits.append((alias, canonical))

    return matched_domains, alias_hits


def level_variants(level_token: str | None) -> list[str]:
    """把 2级 转成 [2级, 二级] 这种可检索变体。"""
    if not level_token:
        return []
    m = re.match(r"([0-9])级", level_token)
    if not m:
        return [level_token]
    n = int(m.group(1))
    zh = NUM_ZH.get(n)
    out = [level_token]
    if zh:
        out.append(f"{zh}级")
    return out


def augment_question_for_retrieval(question: str, lexicon: dict[str, Any]) -> str:
    """按识别到的标准对象补充检索提示词，提高短问句召回。"""
    q = normalize_space(question)
    matched_domains, _ = detect_domains_and_alias_hits(q, lexicon)
    level_token = extract_level_token(q)
    extras: list[str] = []

    if level_token:
        extras.extend(level_variants(level_token))

    for entry in lexicon.get("canonical_terms", []):
        if not isinstance(entry, dict):
            continue
        canonical = str(entry.get("canonical_term", "")).strip()
        domain = str(entry.get("domain", "")).strip()
        if canonical and canonical in matched_domains:
            extras.append(canonical)
            if domain:
                extras.append(domain)
            # 通用等级字段提示词，适用于“等级要求: 二级”等表达。
            extras.extend(["等级", "等级要求", "评审指标"])

    seen = set()
    uniq = []
    for x in extras:
        if not x:
            continue
        if x in seen:
            continue
        seen.add(x)
        uniq.append(x)

    return q if not uniq else f"{q} {' '.join(uniq)}"


def build_clarification_result(
    reason: str,
    suggestions: list[str],
    clarification_question: str,
) -> dict[str, Any]:
    """构造术语澄清响应，停止后续检索。"""
    return {
        "status": "clarification_required",
        "answerable": False,
        "evidence": [],
        "draft_answer": f"我仅使用本地知识库。{reason}。{clarification_question}",
        "gaps": [reason],
        "clarification_question": clarification_question,
        "suggested_terms": suggestions,
        "source_policy": SOURCE_POLICY,
    }


def term_gate(question: str, lexicon: dict[str, Any]) -> dict[str, Any] | None:
    """检索前术语门控：疑似错词或歧义时先返回澄清问题。"""
    q = normalize_space(question)
    level_token = extract_level_token(q)
    matched_domains, alias_hits = detect_domains_and_alias_hits(q, lexicon)

    if alias_hits:
        # 仅问一个澄清问题，避免发散。
        alias, canonical = alias_hits[0]
        nearby_level = find_nearby_level_token(q, alias) or level_token
        suggestions = [f"{canonical}{nearby_level}" if nearby_level else canonical]
        reason = f"术语“{alias}”与知识库标准口径可能不一致"
        ask = f"你是不是指“{suggestions[0]}”？请确认后我再回答。"
        return build_clarification_result(reason, suggestions, ask)

    if level_token and not matched_domains:
        domains = [str(x) for x in lexicon.get("ambiguous_level_domains", []) if str(x).strip()]
        suggestions = [f"{d}{level_token}" for d in domains[:4]]
        reason = f"“{level_token}”缺少标准对象"
        ask = f"请先确认你要查询哪一类：{' / '.join(suggestions)}。"
        return build_clarification_result(reason, suggestions, ask)

    # 同时命中多个对象且非并列提问时，先澄清口径。
    has_parallel_marker = any(x in q for x in ["和", "与", "及", "对比", "比较", "分别"])
    if len(matched_domains) > 1 and not has_parallel_marker:
        suggestions = sorted(matched_domains)
        if level_token:
            suggestions = [f"{d}{level_token}" for d in suggestions]
        reason = "问题可能对应多个标准口径"
        ask = f"请确认本次查询对象：{' / '.join(suggestions[:4])}。"
        return build_clarification_result(reason, suggestions[:4], ask)

    return None


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


def rerank_with_question_signals(
    question: str,
    lexicon: dict[str, Any],
    base_scores: dict[int, float],
    rows_map: dict[int, sqlite3.Row],
) -> dict[int, float]:
    """在融合分基础上，按“对象/等级”问题信号做轻量重排。"""
    q = normalize_space(question)
    matched_domains, _ = detect_domains_and_alias_hits(q, lexicon)

    domain_terms: set[str] = set()
    for entry in lexicon.get("canonical_terms", []):
        if not isinstance(entry, dict):
            continue
        canonical = str(entry.get("canonical_term", "")).strip()
        domain = str(entry.get("domain", "")).strip()
        if canonical and canonical in matched_domains:
            domain_terms.add(canonical)
            if domain:
                domain_terms.add(domain)

    q_level_tokens = extract_all_level_tokens(q)
    q_level_vars: set[str] = set()
    for lv in q_level_tokens:
        q_level_vars.update(level_variants(lv))

    out: dict[int, float] = {}
    for rid, base in base_scores.items():
        row = rows_map.get(rid)
        if row is None:
            out[rid] = base
            continue

        content = str(row["content"] or "")
        source_file = str(row["source_file"] or "")
        search_space = f"{source_file} {content}"
        boost = 0.0

        if domain_terms:
            if any(term in search_space for term in domain_terms):
                boost += 0.12
            else:
                boost -= 0.08

        if q_level_vars:
            row_has_any_level = bool(re.search(r"([0-8]|[零一二三四五六七八九])\s*级", content))
            row_has_target_level = any(v in content for v in q_level_vars)
            row_has_level_field = ("等级要求" in content) or ("等级" in content)
            if row_has_target_level and row_has_level_field:
                boost += 0.12
            elif row_has_target_level:
                boost += 0.06
            elif row_has_any_level:
                boost -= 0.20

        out[rid] = min(1.0, max(0.0, base + boost))

    return out


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


def needs_process_evidence(question: str) -> bool:
    """问题是否明确在询问流程/路径。"""
    q = normalize_space(question)
    return any(x in q for x in ["申报流程", "流程", "实施路径", "实施步骤"])


def has_process_evidence(evidence: list[dict]) -> bool:
    """命中证据中是否存在流程类条款。"""
    return any(
        any(x in str(ev.get("content", "")) for x in ["申报流程", "流程", "步骤", "路径"])
        for ev in evidence
    )


def query_kb(kb_path: Path, question: str, top_k: int, lexicon_path: Path | None = None) -> dict:
    """执行完整检索链路，返回可回答性判断与证据。"""
    question = normalize_space(question)
    lexicon = load_term_lexicon(lexicon_path)
    if not question:
        return {
            "status": "no_evidence",
            "answerable": False,
            "evidence": [],
            "draft_answer": "结论：证据不足，无法给出确定结论。",
            "gaps": ["问题为空，请提供具体问题。"],
            "clarification_question": None,
            "suggested_terms": [],
            "source_policy": SOURCE_POLICY,
        }

    gated = term_gate(question, lexicon)
    if gated is not None:
        return gated

    conn = sqlite3.connect(str(kb_path))
    conn.row_factory = sqlite3.Row
    try:
        retrieval_question = augment_question_for_retrieval(question, lexicon)
        kw_scores, kw_rows = fts_retrieve(conn, retrieval_question, max(20, top_k * 4))
        ng_scores = ngram_retrieve(conn, retrieval_question, max(20, top_k * 6))
        combined = combine_scores(kw_scores, ng_scores)

        if not combined:
            gaps = ["知识库中未检索到相关片段。"]
            return {
                "status": "no_evidence",
                "answerable": False,
                "evidence": [],
                "draft_answer": make_draft_answer(question, [], False, gaps),
                "gaps": gaps,
                "clarification_question": None,
                "suggested_terms": [],
                "source_policy": SOURCE_POLICY,
            }

        pre_ranked_ids = sorted(
            combined.keys(),
            key=lambda rid: (combined[rid], kw_scores.get(rid, 0.0), ng_scores.get(rid, 0.0)),
            reverse=True,
        )
        need_ids = pre_ranked_ids[: max(top_k * 4, 40)]
        rows_cache = dict(kw_rows)
        missing = [rid for rid in need_ids if rid not in rows_cache]
        if missing:
            rows_cache.update(fetch_rows_by_ids(conn, missing))

        adjusted_scores = rerank_with_question_signals(question, lexicon, combined, rows_cache)
        ranked_ids = sorted(
            adjusted_scores.keys(),
            key=lambda rid: (adjusted_scores[rid], kw_scores.get(rid, 0.0), ng_scores.get(rid, 0.0)),
            reverse=True,
        )

        # 若问题明确等级，优先输出匹配等级的证据，降低跨等级噪声。
        q_level_tokens = extract_all_level_tokens(question)
        q_level_vars: set[str] = set()
        for lv in q_level_tokens:
            q_level_vars.update(level_variants(lv))
        if q_level_vars:
            matched_level_ids = []
            other_ids = []
            for rid in ranked_ids:
                row = rows_cache.get(rid)
                content = str(row["content"] or "") if row is not None else ""
                if any(v in content for v in q_level_vars):
                    matched_level_ids.append(rid)
                else:
                    other_ids.append(rid)
            ranked_ids = matched_level_ids + other_ids

        evidence: list[dict] = []
        if not ranked_ids:
            gaps = ["知识库中未检索到相关片段。"]
            return {
                "status": "no_evidence",
                "answerable": False,
                "evidence": [],
                "draft_answer": make_draft_answer(question, [], False, gaps),
                "gaps": gaps,
                "clarification_question": None,
                "suggested_terms": [],
                "source_policy": SOURCE_POLICY,
            }

        top_combined_score = float(adjusted_scores.get(ranked_ids[0], 0.0))
        # 仅保留相对高分片段：绝对下限 + 相对 top 分数阈值。
        min_keep_score = max(0.08, top_combined_score * 0.35)
        for rid in ranked_ids:
            row = rows_cache.get(rid)
            if row is None:
                continue
            content = str(row["content"] or "")
            if q_level_vars and ("等级要求" in content) and (not any(v in content for v in q_level_vars)):
                continue
            score = float(adjusted_scores.get(rid, 0.0))
            if score < min_keep_score:
                continue
            evidence.append(
                {
                    "source_file": row["source_file"],
                    "table_index": row["table_index"],
                    "row_index": row["row_index"],
                    "content": content,
                    "score": round(score, 4),
                }
            )
            if len(evidence) >= top_k:
                break

        if not evidence:
            gaps = ["知识库中未检索到可用证据。"]
            return {
                "status": "no_evidence",
                "answerable": False,
                "evidence": [],
                "draft_answer": make_draft_answer(question, [], False, gaps),
                "gaps": gaps,
                "clarification_question": None,
                "suggested_terms": [],
                "source_policy": SOURCE_POLICY,
            }

        top_score = evidence[0]["score"]
        strong_count = sum(1 for ev in evidence if ev["score"] >= 0.30)
        level_token = extract_level_token(question)
        lvl_vars = set(level_variants(level_token))
        core_phrases = core_question_phrases(question)
        # 规则化可回答判断：高分即通过，中分需多证据，低分需核心短语强命中。
        has_exact_phrase_hit = any(
            any(phrase in ev["content"] for phrase in core_phrases) for ev in evidence[:3]
        )
        has_level_evidence = bool(
            lvl_vars
            and any(
                ("等级要求" in ev["content"] or "等级:" in ev["content"])
                and any(v in ev["content"] for v in lvl_vars)
                for ev in evidence[:6]
            )
        )
        answerable = bool(
            top_score >= 0.48
            or (top_score >= 0.34 and strong_count >= 2)
            or (has_exact_phrase_hit and top_score >= 0.18)
            or (has_level_evidence and top_score >= 0.10)
        )

        gaps: list[str] = []
        if needs_process_evidence(question) and not has_process_evidence(evidence):
            answerable = False
            gaps.append("问题要求“流程/步骤”，但本地知识库未命中对应流程条款。")
        if not answerable:
            if not gaps:
                gaps.append("命中证据与问题关联度不足，无法形成确定结论。")

        draft_answer = make_draft_answer(question, evidence, answerable, gaps)
        return {
            "status": "answered" if answerable else "no_evidence",
            "answerable": answerable,
            "evidence": evidence,
            "draft_answer": draft_answer,
            "gaps": gaps,
            "clarification_question": None,
            "suggested_terms": [],
            "source_policy": SOURCE_POLICY,
        }
    finally:
        conn.close()


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="Query local KB sqlite and return evidence-grounded answer draft.")
    parser.add_argument("--kb", type=Path, required=True, help="Path to kb sqlite, e.g. ./kb/kb.sqlite")
    parser.add_argument("--question", required=True, help="User question")
    parser.add_argument("--top-k", type=int, default=12, help="Number of evidence rows")
    parser.add_argument(
        "--term-lexicon",
        type=Path,
        default=Path("./skills/hospital-kb-qa/references/term_lexicon.json"),
        help="Path to term lexicon json",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    args = parser.parse_args()

    result = query_kb(args.kb, args.question, args.top_k, args.term_lexicon)

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
