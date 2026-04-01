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

## Checklist Output Contract (强制)

当用户问题中出现以下任一意图词时，视为“清单请求”：

- `清单` `完整` `全部` `全量` `逐条` `逐项` `明细` `核查表` `打分表` `Excel` `表格`

清单请求的强制行为：

1. 必须检索明细证据，不得只返回摘要文字。
2. 必须生成 Excel 附件（`output/*.xlsx`），并在回复中给出文件路径。
3. 回复文字可以摘要，但必须明确“完整明细见附件”。
4. 若证据不足，必须在回复中明确缺口；禁止用常识补全条目。

清单请求的执行流程：

1. 先执行术语门控：若 `status=clarification_required`，只问一个澄清问题并停止。
2. 术语确认后，执行检索命令并提高召回量（建议 `--top-k 200`）：
   - `.venv/bin/python scripts/kb_query.py --kb ./kb/kb.sqlite --question "<用户问题>" --top-k 200 --json`
3. 将 `evidence[]` 逐条写入 Excel，不做概括性合并。

Excel 最低字段要求（每条证据一行）：

- `序号`
- `source_file`
- `table_index`
- `row_index`
- `content`
- `score`

输出约束：

- 禁止“仅摘要无附件”。
- 禁止把“抽样命中”描述为“完整覆盖”。
- 如果用户明确要“完整清单”，正文必须包含一句：`完整明细已导出为附件 Excel。`

## Checklist Scope Map (章节索引白名单)

清单请求时，必须优先在以下章节范围内抽取明细；禁止跨口径扩展到未指定章节。

1. 电子病历分级标准清单  
   来源：`国家卫健委_电子病历系统应用水平分级评价标准_试行_2018版`  
   章节：`附表3. 电子病历系统应用水平分级评分标准`

2. 信息系统互联互通分级标准清单  
   来源：`国家医疗健康信息医院信息互联互通标准化成熟度测评方案_2020年版`  
   章节：`附表3 医院现有信息系统互联互通标准对比表`

3. 智慧服务分级标准清单  
   来源：`医院智慧服务分级评估标准体系_试行_20190801`  
   章节：`医院智慧服务分级评估具体要求`

4. 智慧管理分级标准清单  
   来源：`医院智慧管理分级评估具体要求`  
   章节：`医院智慧管理分级评估具体要求`

执行要求：

- 若用户指定口径，只在对应白名单章节抽取。
- 若用户同时指定多个口径，分别按各自章节抽取后合并输出。
- 若白名单章节未命中，必须明确“指定章节未检索到明细”，不得改去其它章节补齐。

## Proactive Next-Step Trigger

当回答“条款/要求/清单”类问题后，且用户明确提出“要模板/表格/执行产物”时，才给出下一步建议（如 Excel/JSON 核查表）：

1. 当前回答可结构化为表格字段（条款、状态、证据、责任人、期限、得分等）。
2. 已有知识库证据可直接映射到模板，不需要额外外部数据。

建议输出模板（单句）：

- `如果你愿意，我下一步可以把这份内容转成一张“<任务名>自评打分表（Excel/JSON）”，让你直接做逐条核查。`

## Notes

- 首版接入范围：`docx + jsonl`（`.doc` 暂不自动转换）。
- 如果源文档更新，先重新执行 `ingest` 再 `query`。
- 术语词表文件：`skills/hospital-kb-qa/references/term_lexicon.json`。
- 依赖项目级虚拟环境 `.venv`。
