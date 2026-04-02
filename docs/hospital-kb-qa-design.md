# Hospital KB QA 设计文档

更新时间：2026-04-02

## 1. 背景与问题

本项目面向医院信息化标准条款问答，核心诉求不是“泛知识问答”，而是：

1. 结果稳定可复现
2. 证据可追溯可验收
3. 多模型执行时行为可控

历史问题主要集中在：

1. 术语不一致导致检索偏移
2. 多文档混检带来噪声
3. 模型在无证据时发散补全
4. 清单类问题被摘要化，缺少完整条目

## 2. 目标与非目标

### 2.1 目标

1. 构建本地、可审计的知识库检索链路
2. 强约束输出“结论 + 依据 + 引用 + 缺口”
3. 对清单/表格问题输出完整明细
4. 兼容多文档并支持口径白名单过滤

### 2.2 非目标

1. 不做互联网检索
2. 不做开放域常识补全
3. 首版不引入外部 embedding 服务

## 3. 总体架构（三层）

### Layer 1：检索层（Retrieval）

1. 输入问题结构化解析：`scope / level / keywords / query_type`
2. 术语门控：不确定先澄清
3. 来源过滤：先按 `source_scope` 过滤 `source_file`
4. 主检索：`sqlite`（FTS + ngram + 规则重排）
5. 兜底补检：仅 `no_evidence` 时允许 `jq/rg` 本地补检

### Layer 2：结构控制层（Structure Control）

按 `query_type` 选择回答结构：

1. `fact`
2. `list/filter_list`
3. `compare`
4. `locate`
5. `theme_summary`

并执行专项约束：

1. 清单类必须完整展开
2. 表格命中必须整表回填

### Layer 3：受限生成层（Constrained Generation）

1. 只基于 `evidence` 输出
2. 无证据不输出确定性结论
3. 禁止知识库外延展（流程建议/实施路径等）

## 4. 数据与索引设计

## 4.1 输入与标准化

1. `.docx` -> `docx_to_jsonl.py`
2. `.jsonl` 直接接入

标准记录类型：

1. `title`
2. `paragraph`
3. `list_item`
4. `table_row`

## 4.2 SQLite 索引

关键表：

1. `records`
2. `records_fts`（关键词检索）
3. `ngrams`（字符 ngram 补召回）

关键定位字段：

1. `source_file`
2. `table_index`
3. `row_index`

## 5. 查询流程（当前实现）

1. 问题清洗与术语门控
2. `source_scope` 过滤
3. `sqlite` 主检索（FTS + LIKE + ngram）
4. 分数融合与重排
5. 若无证据：触发 `jq/rg` 补检
6. 统一可回答性判定
7. 生成标准 JSON 输出

## 5.1 表格强制规则（代码级）

当命中任意表格行（`table_index != null`）时：

1. 自动回填同一 `source_file + table_index` 的所有行
2. 按 `row_index` 原顺序输出
3. `draft_answer` 进入“原表整表输出”分支，不做摘要改写

## 6. Query Type 路由规则

优先级从高到低：

1. `filter_list`：`清单|完整|全部|逐条|明细|打分表|excel|xlsx|附件`
2. `compare`：`对比|比较|差异|区别|分别|哪个更`
3. `locate`：`哪一条|哪一行|哪个章节|附表|附件|条款位置|定位`
4. `theme_summary`：`汇总|归纳|总览|整体情况`
5. 默认 `fact`

## 7. 输出契约

固定输出字段：

1. `status`
2. `answerable`
3. `evidence[]`
4. `draft_answer`
5. `gaps[]`
6. `clarification_question`
7. `suggested_terms[]`
8. `source_policy=local_kb_only`
9. `fallback_used`

证据字段：

1. `source_file`
2. `table_index`
3. `row_index`
4. `content`
5. `score`
6. `retrieval_method`

## 8. 可控性策略

1. 强边界：仅本地数据源
2. 强顺序：先过滤再检索
3. 强停止：最多一次主检索 + 一次补检
4. 强证据：无证据不结论
5. 强清单：清单问题不可摘要替代

## 9. 演进路线

### 阶段一（当前）

强约束轻量 RAG：

1. `sqlite` 主通道
2. `jq/rg` 兜底
3. `query_type` 路由
4. `scope` 归一
5. checklist 通道
6. 证据驱动输出

### 阶段二（按指标触发）

仅当出现以下问题再引入向量检索：

1. 口语问法召回持续偏低
2. 同义表达漏检严重
3. 主题汇总能力不足
4. 文档规模显著扩大

### 阶段三（混合检索）

1. 关键词召回
2. 向量召回
3. 规则重排
4. 章节/来源硬过滤
5. 结构化答案渲染

## 10. 已知风险与治理

1. 风险：术语词表不全导致澄清增多  
治理：持续维护术语词表与来源映射。

2. 风险：多文档同词冲突  
治理：始终先做 `source_scope` 过滤。

3. 风险：弱模型步骤膨胀  
治理：通过 `SKILL.md` 状态机 + 停止条件收敛执行路径。
