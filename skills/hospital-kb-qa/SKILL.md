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

## Fixed Execution State Machine (强制)

所有模型必须按以下状态机执行，不得跳步，不得改写成自由探索流程。

### Layer 1: Retrieval（证据检索层）

1. 先解析问题并结构化：
   - `query_type`
   - `scope`（标准口径）
   - `level`（级别）
   - `topic_keywords`
   - `need_complete_output`（是否要求完整清单）
2. 先做术语/口径澄清判定：
   - 口径不唯一
   - 等级不唯一且问题依赖等级
   - 同时引用多个冲突标准名
   命中任一条件：只输出一个澄清问题并停止。
3. 检索顺序固定：
   - 先按 `source_scope` 过滤 `source_file`
   - 再执行 `sqlite` 检索（FTS/结构化）
   - 仅当 `status=no_evidence` 才允许 `jq/rg` 补检

### Layer 2: Structure Control（结构控制层）

根据 `query_type` 决定输出结构：

1. `fact`：结论 -> 依据 -> 引用 -> 缺口
2. `list` / `filter_list`：完整条目列表 -> 引用 -> 缺口（不得摘要替代）
3. `compare`：按口径 A/B 分别列依据 -> 对比结论 -> 引用 -> 缺口
4. `locate`：定位结果（文档/章节/表/行）-> 原文片段 -> 引用
5. `theme_summary`：主题归纳 -> 支撑证据 -> 引用 -> 缺口

表格命中强制规则：

1. 若命中证据包含 `table_index`（即命中表格行），必须按原表整表输出。
2. 整表范围定义为同一 `source_file + table_index` 的全部行，按 `row_index` 原顺序输出。
3. 禁止对表格内容做摘要、改写、合并重述或字段重命名。
4. 可在表格后追加引用与缺口说明，但不得改动表格正文内容。

### Layer 3: Constrained Generation（受限生成层）

1. 只允许基于 `evidence` 生成回答，禁止知识库外补全。
2. `list/filter_list` 必须完整展开，不得仅给概述。
3. `answerable=false` 必须明确“证据不足，无法给出确定结论”。
4. `status=no_evidence` 禁止给申报流程、实施路径等外延建议。
5. 表格命中时优先输出“原表整表”，不得输出加工版表格。

### Stop Conditions（停止条件）

1. 每个问题最多：`1 次 sqlite 主检索 + 1 次 jq/rg 补检`。
2. 若仍无证据：直接返回 `no_evidence`，禁止继续改写问题重试。
3. 若需要澄清：只问 1 个澄清问题并停止等待用户确认。

## Query Type Routing Rules (可执行约束)

按以下优先级进行 `query_type` 路由（从上到下匹配，命中即停止）：

1. `filter_list`：出现 `清单|完整|全部|逐条|明细|打分表|excel|xlsx|附件`
2. `compare`：出现 `对比|比较|差异|区别|分别|哪个更`
3. `locate`：出现 `哪一条|哪一行|哪个章节|附表|附件|条款位置|定位`
4. `theme_summary`：出现 `汇总|归纳|总览|整体情况`
5. 其他默认 `fact`

补充规则：

1. `filter_list` 必须走 Checklist Scope Map 对应来源与章节。
2. 用户指定单一口径时，禁止跨口径召回。
3. 用户明确要求 `excel/xlsx` 时，必须输出可下载附件（正文仅摘要）。
4. 多口径问题先分口径检索，再合并渲染，禁止混合后再猜测归类。
5. 任何 `table_index` 命中的答案，必须先完成整表回填再组织文字说明。

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
