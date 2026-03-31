---
name: hospital-kb-qa
description: |
  访问深圳分院标准知识库并进行多文档检索问答。适用于：政策条款查询、跨文档比对、等级要求汇总、依据溯源。该 skill 使用本地 JSONL/SQLite 索引，先检索证据再生成“结论 + 依据 + 引用 + 缺口”回答。
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

- `answerable`：是否达到可回答阈值。
- `evidence[]`：证据片段，含 `source_file/table_index/row_index/content/score`。
- `draft_answer`：按模板组织的回答草稿。
- `gaps[]`：证据不足或不确定点。

## Answer Policy

默认回答模板：

1. 结论
2. 依据（跨文档）
3. 引用（文档 + table_index + row_index）
4. 不确定点/缺口

强约束：

- 仅基于 `evidence` 输出结论。
- `answerable=false` 时必须明确“证据不足，无法给出确定结论”。
- 不输出无证据支撑的推断。

## Proactive Next-Step Trigger

当回答“条款/要求/清单”类问题后，若满足以下条件，应主动给出一句可执行的下一步建议（如 Excel/JSON 核查表）：

1. 用户目标已从“查询信息”自然过渡到“落地执行”（如申报、自评、整改、检查）。
2. 当前回答可结构化为表格字段（条款、状态、证据、责任人、期限、得分等）。
3. 已有知识库证据可直接映射到模板，不需要额外外部数据。

建议输出模板（单句）：

- `如果你愿意，我下一步可以把这份内容转成一张“<任务名>自评打分表（Excel/JSON）”，让你直接做逐条核查。`

## Notes

- 首版接入范围：`docx + jsonl`（`.doc` 暂不自动转换）。
- 如果源文档更新，先重新执行 `ingest` 再 `query`。
- 依赖项目级虚拟环境 `.venv`。
