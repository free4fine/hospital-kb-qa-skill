---
name: hospital-kb-qa
description: |
  本地医院信息化标准知识库问答 skill。用于政策条款查询、等级要求、清单抽取、跨标准对比。仅允许使用本地 kb.sqlite/jsonl 证据，禁止联网与外部补全；遇到术语歧义先澄清，再回答。
---

# Hospital KB QA

## 固定入口

必须优先调用：

```bash
.venv/bin/python skills/hospital-kb-qa/scripts/kb_query.py \
  --kb ./skills/hospital-kb-qa/kb/kb.sqlite \
  --question "{{user_input}}" \
  --top-k 12 \
  --source-scope ./skills/hospital-kb-qa/references/source_scope.json \
  --jsonl-root ./skills/hospital-kb-qa/kb/jsonl \
  --json
```

禁止先自由 `jq/rg` 探索；必须先走 `kb_query.py`。

## 固定执行状态机（必须按顺序）

1. 解析问题：识别 `query_type`、口径、等级、关键词、是否要求完整输出。
2. 术语门控：若口径不唯一或等级依赖但不明确，返回 `clarification_required`，只问一个澄清问题并停止。
3. 主检索：先按 `source_scope` 过滤 `source_file`，再用 sqlite 检索。
4. 兜底补检：仅当 `status=no_evidence` 时，允许 `jq/rg` 在同一 source_scope 内补检一次。
5. 受限生成：只基于 `evidence` 输出，不得扩展知识库外结论。

停止条件：

1. 最多 `1 次主检索 + 1 次补检`。
2. 仍无证据时直接 `no_evidence`，不得继续改写问题循环重试。

## 输出契约（必须遵守）

`kb_query.py --json` 输出字段：

1. `status`: `clarification_required | answered | no_evidence`
2. `answerable`
3. `evidence[]`: `source_file/table_index/row_index/content/score/retrieval_method`
4. `draft_answer`
5. `gaps[]`
6. `clarification_question`
7. `suggested_terms[]`
8. `source_policy`（固定 `local_kb_only`）
9. `fallback_used`

回答结构：

1. 结论
2. 依据
3. 引用
4. 不确定点/缺口

## Query Type 路由（简版）

优先级从高到低：

1. `filter_list`: `清单|完整|全部|逐条|明细|打分表|excel|xlsx|附件`
2. `compare`: `对比|比较|差异|区别|分别|哪个更`
3. `locate`: `哪一条|哪一行|哪个章节|附表|附件|条款位置|定位`
4. `theme_summary`: `汇总|归纳|总览|整体情况`
5. 默认 `fact`

强约束：

1. `filter_list` 必须完整展开，不允许摘要代替。
2. 用户要求 `excel/xlsx/附件` 时，优先生成附件，正文仅摘要。
3. 用户指定单一口径时，禁止跨口径召回。

## 表格命中规则（代码优先）

若证据命中 `table_index`，必须按原表输出：

1. 回填同一 `source_file + table_index` 全部行。
2. 按 `row_index` 原顺序输出。
3. 禁止改写字段名、禁止摘要压缩。

## Checklist Scope Map（清单白名单）

清单请求必须限定在以下来源：

1. 电子病历分级清单  
来源：`国家卫健委_电子病历系统应用水平分级评价标准_试行_2018版`  
章节：`附表3`

2. 信息互联互通清单  
来源：`国家医疗健康信息医院信息互联互通标准化成熟度测评方案_2020年版`

3. 智慧服务分级清单  
来源：`医院智慧服务分级评估标准体系_试行_20190801`  
章节：`附件3`

4. 智慧管理分级清单  
来源：`医院智慧管理分级评估具体要求`

若白名单范围内未命中：明确说明“指定范围未检索到明细”，不得跨来源补齐。

## 脚本优先原则（避免模型漂移）

以下逻辑必须由脚本处理，不靠 LLM 自由发挥：

1. 术语归一与澄清门控
2. source_scope 过滤
3. query_type 路由
4. 表格整表回填
5. no_evidence 判定
6. JSON 输出结构校验

## 4 个标准示例（必须对齐）

### 示例 1：术语错位先澄清

用户问题：
`信息互联二级需要什么要求`

期望：

1. `status=clarification_required`
2. `clarification_question=你是不是指“互联互通二级”？`
3. 不输出业务结论

### 示例 2：单口径等级问答

用户问题：
`智慧服务 诊前服务，急救衔接，二级有什么要求`

期望：

1. `status=answered`
2. `evidence` 只来自智慧服务标准 `source_file`
3. 给出可追溯引用（文档 + table_index + row_index）

### 示例 3：完整清单请求

用户问题：
`智慧服务2级，需要完整清单，excel`

期望：

1. 识别为 `filter_list`
2. 走清单白名单来源
3. 输出完整条目并生成附件（xlsx）

### 示例 4：无证据禁止发散

用户问题：
`电子病历3级申报流程`

期望（当库中无流程条款）：

1. `status=no_evidence`
2. 明确证据不足
3. 不输出外部申报流程建议

## 依赖与路径

1. 只使用项目级 `.venv`
2. `source_scope`: `skills/hospital-kb-qa/references/source_scope.json`
3. `term_lexicon`: `skills/hospital-kb-qa/references/term_lexicon.json`
4. kb 数据根目录：`skills/hospital-kb-qa/kb`
