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
.venv/bin/python scripts/kb_query.py --kb ./kb/kb.sqlite --question "这里写用户问题" --top-k 12 --source-scope ./skills/hospital-kb-qa/references/source_scope.json --jsonl-root ./kb/jsonl --json
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
- `fallback_used`：是否触发 `jq/rg` 补检。
- `evidence[].retrieval_method`：`sqlite_main | jq_rg_fallback`。

## Answer Policy

默认回答模板：

1. 结论
2. 依据（跨文档）
3. 引用（文档 + table_index + row_index）
4. 不确定点/缺口

输出风格约束（给其他 agent 的强约束）：

- 回答必须直接命中用户问题，先给结论，禁止寒暄和铺垫。
- 要满足“结论 + 依据 + 引用 + 缺口”结构前提下，避免重复与冗长解释。

强约束：

- 只允许使用本地 `kb.sqlite` 与本地文档，不得联网检索、不得引用外部常识补全。
- 检索主通道必须是 `sqlite`；仅当 `status=no_evidence` 候选场景时才允许触发 `jq/rg` 补检。
- `jq/rg` 补检必须继续受 `source_scope` 限制，禁止跨白名单来源兜底。
- 仅基于 `evidence` 输出结论。
- 术语不确定时必须先澄清，且每次只问一个澄清问题。
- `answerable=false` 时必须明确“证据不足，无法给出确定结论”。
- `status=no_evidence` 时禁止补充申报流程、实施路径等知识库外内容。
- 不输出无证据支撑的推断。

## Source Scope Filter (强制)

查询顺序必须固定为：

1. 先根据口径映射过滤 `source_file`
2. 再执行 FTS / n-gram 检索与排序

口径来源映射文件：

- `skills/hospital-kb-qa/references/source_scope.json`

## Checklist Scope Map (章节索引白名单)

清单请求时，必须优先在以下章节范围内抽取明细；禁止跨口径扩展到未指定章节。

1. 电子病历分级标准清单  
   来源：`国家卫健委_电子病历系统应用水平分级评价标准_试行_2018版`  
   章节：`附表3`

2. 信息系统互联互通分级标准清单  
   来源：`国家医疗健康信息医院信息互联互通标准化成熟度测评方案_2020年版`  
   章节：`分级标准清单相关表格（按用户等级要求筛选）`

3. 智慧服务分级标准清单  
   来源：`医院智慧服务分级评估标准体系_试行_20190801`  
   章节：`附件3`

4. 智慧管理分级标准清单  
   来源：`医院智慧管理分级评估具体要求`  
   章节：`分级标准清单相关章节（按用户等级要求筛选）`

执行要求：

- 若用户指定口径，只在对应白名单来源与章节抽取。
- 若用户同时指定多个口径，分别抽取后合并输出。
- 若白名单范围未命中，必须明确“指定范围未检索到明细”，不得改去其它来源补齐。

## Notes

- 首版接入范围：`docx + jsonl`（`.doc` 暂不自动转换）。
- 如果源文档更新，先重新执行 `ingest` 再 `query`。
- 术语词表文件：`skills/hospital-kb-qa/references/term_lexicon.json`。
- 口径来源白名单：`skills/hospital-kb-qa/references/source_scope.json`。
- 依赖项目级虚拟环境 `.venv`。
