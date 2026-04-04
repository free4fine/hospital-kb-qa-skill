# Hospital KB QA 脚本职责边界

## 目标

把“可程序化且必须一致”的逻辑下沉到脚本，降低不同模型的行为漂移。

## A. 必须由脚本处理（硬约束）

1. 问题归一化：术语别名、等级表达（如“二级/2级”）统一。
2. 术语门控：歧义时返回 `clarification_required`，不直接答题。
3. 口径过滤：先按 `source_scope` 过滤 `source_file`，再检索。
4. 检索执行：sqlite 主检索；仅 `no_evidence` 时触发 `jq/rg` 补检。
5. query_type 路由：`fact/filter_list/compare/locate/theme_summary`。
6. 表格整表输出：命中 `table_index` 后回填整表并按行序输出。
7. 证据门控：`answerable=false` 时禁止输出确定性结论。
8. 输出契约：JSON 字段完整性与状态机一致性校验。

## B. 可以由模型处理（受限自由）

1. 基于证据的文字组织（语句顺序、措辞）。
2. 多文档证据的并排解释（不改变事实内容）。
3. 用户友好的摘要（前提：不替代清单/整表正文）。

## C. 禁止由模型处理

1. 自行选择数据源（必须服从脚本返回的 `source_scope` 结果）。
2. 无证据时外推流程、经验建议、行业常识。
3. 对表格字段重命名、合并改写、删行。
4. 通过多轮改写问题反复尝试绕过状态机。

## D. 推荐调用顺序

1. `kb_query.py --json`（唯一主入口）
2. 若 `status=clarification_required`：仅返回澄清问题
3. 若 `status=answered`：按返回证据渲染
4. 若 `status=no_evidence`：返回缺口与建议补充条件

## E. 回归用例（最小集）

1. 术语错位：`信息互联二级要求` -> `clarification_required`
2. 单口径：`智慧服务二级要求` -> 只命中智慧服务 `source_file`
3. 清单请求：`电子病历2级完整清单` -> `filter_list` + 完整输出
4. 无证据：`XXX申报流程`（库内无流程）-> `no_evidence`
