---
name: hospital-kb-qa
description: |
  访问医卫信息化相关标准知识库并进行多文档检索问答。适用于：政策条款查询、跨文档比对、等级要求汇总、依据溯源。该 skill 使用本地 JSONL/SQLite 索引，先检索证据再生成“结论 + 依据 + 引用 + 缺口”回答。
---

# Hospital KB QA

## Overview

该 skill 面向本项目知识库（`.docx + .jsonl`），提供两步流程：

1. `ingest`：将文档标准化并构建本地索引。
2. `query`：在多文档检索证据并输出可引用答案草稿。

## Workflow

### 1) 构建/更新知识库索引

在项目根目录执行：

```bash
.venv/bin/python scripts/kb_ingest.py --kb-root ./kb --input-root . --include-docx --include-jsonl
```

产物：

- `kb/jsonl/*.jsonl`：标准化后的中间数据。
- `kb/kb.sqlite`：检索索引库（`records`, `records_fts`, `ngrams`）。

### 2) 提问并检索证据

```bash
.venv/bin/python scripts/kb_query.py --kb ./kb/kb.sqlite --question "这里写用户问题" --top-k 12 --json
```

返回字段：

- `status`：`clarification_required | answered | no_evidence`。
- `answerable`：是否达到可回答阈值。
- `evidence[]`：证据片段，含 `source_file/table_index/row_index/content/score`。
- `draft_answer`：按模板组织的回答草稿。
- `gaps[]`：证据不足或不确定点。
- `clarification_question`：需要澄清时的问题文本。
- `suggested_terms`：候选标准术语列表。
- `source_policy`：固定 `local_kb_only`。

## Answer Policy

默认回答模板：

1. 结论
2. 依据（跨文档）
3. 引用（文档 + table_index + row_index）
4. 不确定点/缺口

强约束：

- 只允许使用本地 `kb.sqlite` 与本地文档，不得联网检索、不得引用外部常识补全。
- 仅基于 `evidence` 输出结论。
- 术语不确定时必须先澄清，且每次只问一个澄清问题。
- `answerable=false` 时必须明确“证据不足，无法给出确定结论”。
- `status=no_evidence` 时禁止补充申报流程、实施路径等知识库外内容。
- 不输出无证据支撑的推断。

## Notes

- 首版接入范围：`docx + jsonl`（`.doc` 暂不自动转换）。
- 如果源文档更新，先重新执行 `ingest` 再 `query`。
- 术语词表文件：`skills/hospital-kb-qa/references/term_lexicon.json`。
- 依赖项目级虚拟环境 `.venv`。
