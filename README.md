# hospital-kb-qa-skill（CLI 可集成）

本仓库提供医卫行业信息化政策、标准文档的本地知识库能力，支持：

- `.docx + .jsonl` 批量入库
- 本地 SQLite 索引（关键词 + ngram 补召回）
- 检索后按证据输出回答草稿（含来源定位）

适用于在 opencode、openclaw 或其他可执行命令的平台中作为问答后端调用。

## 目录结构

- `scripts/docx_to_jsonl.py`：Word 转 JSONL
- `scripts/kb_ingest.py`：构建/更新知识库索引
- `scripts/kb_query.py`：问题检索与证据化输出
- `kb/jsonl/`：标准化中间数据
- `kb/kb.sqlite`：知识库索引数据库
- `skills/hospital-kb-qa/SKILL.md`：本地 skill 说明

## 环境要求

- Python 3.10+
- 项目级虚拟环境 `.venv`

安装依赖：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 快速开始

### 1) 构建索引

```bash
.venv/bin/python scripts/kb_ingest.py --kb-root ./kb --input-root . --include-docx --include-jsonl
```

### 2) 查询

```bash
.venv/bin/python scripts/kb_query.py --kb ./kb/kb.sqlite --question "医院应用信息化评估分为多少级" --top-k 12 --json
```

## 查询返回 JSON 契约

`kb_query.py --json` 返回字段：

- `answerable`：`bool`，是否达到可回答阈值
- `evidence`：证据数组，每项包含
  - `source_file`
  - `table_index`
  - `row_index`
  - `content`
  - `score`
- `draft_answer`：按“结论+依据+引用+缺口”组织的回答草稿
- `gaps`：证据不足或不确定点

## 在其他平台接入（opencode/openclaw）

平台只需配置一个命令工具，传入用户问题并读取 stdout JSON：

```bash
.venv/bin/python scripts/kb_query.py --kb ./kb/kb.sqlite --question "{{user_input}}" --top-k 12 --json
```

建议平台侧渲染逻辑：

1. 若 `answerable=true`：展示 `draft_answer`，并展开 `evidence` 引用。
2. 若 `answerable=false`：展示 `draft_answer` 与 `gaps`，提示用户收敛问题范围。

## 数据更新流程

当源文档更新后，重新执行一次入库命令：

```bash
.venv/bin/python scripts/kb_ingest.py --kb-root ./kb --input-root . --include-docx --include-jsonl
```

## 注意事项

- 当前自动入库范围：`.docx + .jsonl`（`.doc` 暂不自动转换）。
- 本仓库包含公开标准材料构建的数据索引，可公开分发。
