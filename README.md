# hospital-kb-qa-skill

本项目提供“医院信息化标准知识库”本地检索能力，定位是可审计、可复现、可控输出的问答后端。

当前主策略：

1. `sqlite` 主检索（FTS + ngram）
2. `source_scope` 口径白名单先过滤
3. 仅在 `no_evidence` 时触发 `jq/rg` 本地补检
4. 仅基于证据输出，不做知识库外补全

## 核心能力

- `.docx + .jsonl` 批量入库
- 统一索引：`records` / `records_fts` / `ngrams`
- 查询状态机：`clarification_required | answered | no_evidence`
- 证据可追溯：`source_file + table_index + row_index`
- 表格命中强制整表输出（代码级）

## 项目结构

- `skills/hospital-kb-qa/scripts/docx_to_jsonl.py`：Word 转 JSONL（标题/段落/列表/表格行）
- `skills/hospital-kb-qa/scripts/kb_ingest.py`：入库与索引构建
- `skills/hospital-kb-qa/scripts/kb_query.py`：查询、证据融合、状态输出
- `skills/hospital-kb-qa/SKILL.md`：Skill 约束与执行协议
- `skills/hospital-kb-qa/references/source_scope.json`：口径来源白名单
- `skills/hospital-kb-qa/references/term_lexicon.json`：术语词表（可维护配置）
- `skills/hospital-kb-qa/kb/jsonl/`：标准化中间数据
- `skills/hospital-kb-qa/kb/kb.sqlite`：本地索引库
- `docs/hospital-kb-qa-design.md`：应用设计文档
- `docs/hospital-kb-qa-script-boundary.md`：脚本与模型职责边界（防漂移）

## 环境要求

- Python 3.10+
- 必须使用项目级虚拟环境 `.venv`

## 安装

```bash
git clone https://github.com/free4fine/hospital-kb-qa-skill.git
cd hospital-kb-qa-skill
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 快速开始

### 1) 构建/更新索引

```bash
.venv/bin/python skills/hospital-kb-qa/scripts/kb_ingest.py \
  --kb-root ./skills/hospital-kb-qa/kb \
  --input-root . \
  --include-docx \
  --include-jsonl
```

### 2) 查询

```bash
.venv/bin/python skills/hospital-kb-qa/scripts/kb_query.py \
  --kb ./skills/hospital-kb-qa/kb/kb.sqlite \
  --question "智慧服务 诊前服务，急救衔接，二级有什么要求" \
  --top-k 12 \
  --source-scope ./skills/hospital-kb-qa/references/source_scope.json \
  --jsonl-root ./skills/hospital-kb-qa/kb/jsonl \
  --json
```

### 3) 关闭补检（调试/验收）

```bash
.venv/bin/python skills/hospital-kb-qa/scripts/kb_query.py \
  --kb ./skills/hospital-kb-qa/kb/kb.sqlite \
  --question "..." \
  --source-scope ./skills/hospital-kb-qa/references/source_scope.json \
  --no-jq-rg-fallback \
  --json
```

## 查询输出契约（JSON）

`skills/hospital-kb-qa/scripts/kb_query.py --json` 返回：

- `status`：`clarification_required | answered | no_evidence`
- `answerable`：是否达到可回答阈值
- `evidence[]`：
  - `source_file`
  - `table_index`
  - `row_index`
  - `content`
  - `score`
  - `retrieval_method`：`sqlite_main | jq_rg_fallback`
- `draft_answer`：证据驱动回答草稿
- `gaps[]`：缺口与不确定点
- `clarification_question`：澄清问题（可空）
- `suggested_terms[]`：建议术语（可空）
- `source_policy`：固定 `local_kb_only`
- `fallback_used`：是否触发补检

## 当前强约束

1. 只允许本地知识库，不联网，不外部补全。
2. 先按 `source_scope` 过滤，再检索。
3. 主通道为 `sqlite`，仅 `no_evidence` 时补检。
4. 命中表格行时，自动按 `source_file + table_index` 回填整表并按原顺序输出。

## 典型接入方式（Agent/平台）

平台配置单命令即可：

```bash
.venv/bin/python skills/hospital-kb-qa/scripts/kb_query.py \
  --kb ./skills/hospital-kb-qa/kb/kb.sqlite \
  --question "{{user_input}}" \
  --top-k 12 \
  --source-scope ./skills/hospital-kb-qa/references/source_scope.json \
  --jsonl-root ./skills/hospital-kb-qa/kb/jsonl \
  --json
```

建议前端渲染：

1. `status=clarification_required`：只展示澄清问题，等待用户确认。
2. `status=answered`：展示 `draft_answer` 与 `evidence` 引用。
3. `status=no_evidence`：展示 `draft_answer` + `gaps`，提示用户收敛口径或补充条件。

## 数据更新

源文档更新后，重新执行：

```bash
.venv/bin/python skills/hospital-kb-qa/scripts/kb_ingest.py \
  --kb-root ./skills/hospital-kb-qa/kb \
  --input-root . \
  --include-docx \
  --include-jsonl
```

## 说明

- 当前自动接入范围：`.docx + .jsonl`（`.doc` 暂不自动转换）。
- 详细设计见 [docs/hospital-kb-qa-design.md](docs/hospital-kb-qa-design.md)。
- 职责边界见 [docs/hospital-kb-qa-script-boundary.md](docs/hospital-kb-qa-script-boundary.md)。
