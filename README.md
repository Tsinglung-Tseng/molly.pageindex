# molly.pageindex

> Fork of [VectifyAI/PageIndex](https://github.com/VectifyAI/PageIndex) — Vectorless, Reasoning-based RAG for Obsidian Vault.

本项目在 PageIndex 原版基础上，为个人 Obsidian Vault 做了深度定制，作为 Molly 系统的文档检索引擎。

## 相较原版的改动

- **SiliconFlow API**：对接 SiliconFlow，支持 Qwen 系列模型（`LLM_API_KEY` 或原生 `OPENAI_API_KEY` 均可）
- **MCP Server**：后台 watchdog 自动索引 + `search_notes` / `grep_notes` 工具
- **Web UI**：本地搜索界面，支持 AI 问答、搜索历史（SQLite 持久化）、sidebar tab 状态持久化
- **深链接**：搜索结果直接跳转到对应标题锚点（`obsidian://` deep link）
- **并行检索**：多文档 Stage 2 并行加速，显著降低搜索延迟
- **每日 Vault 报告**：扫描 Vault 变更，通过 Telegram 推送每日摘要

## 安装

```bash
uv sync
```

## 在 Molly 中配置

本项目作为 Molly 的托管子服务运行，配置和启动由 Molly 框架负责。需注册两个独立进程。

### 服务一：Note Indexer (web)

| 字段 | 值 |
|------|----|
| 启动命令 | `.venv/bin/python3 web_ui.py` |
| Ready 信号 | stdout 出现 `MOLLY_READY` |

### 服务二：Note Indexer (mcp)

| 字段 | 值 |
|------|----|
| 启动命令 | `.venv/bin/python3 mcp_server.py` |
| Ready 信号 | stderr 出现 `PageIndex MCP server starting` |

> **两个服务共用同一份环境变量，配置一次即可。**

### 环境变量

| 变量 | 必填 | 说明 |
|------|:----:|------|
| `VAULT_PATH` | ✅ | Obsidian Vault 根目录绝对路径 |
| `VAULT_NAME` | ✅ | Vault 名称，用于 `obsidian://` 深链接 |
| `LLM_API_KEY` | ✅ | SiliconFlow API Key（或用原生 `OPENAI_API_KEY`，二选一） |
| `LLM_SERVICE_BASE_URL` | ✅ | `https://api.siliconflow.cn/v1` |
| `PAGEINDEX_MODEL` | | 默认 `Qwen/Qwen3-32B` |
| `WEB_HOST` | | 默认 `127.0.0.1` |
| `WEB_PORT` | | 默认 `7842` |
| `RESULTS_DIR` | | 索引结果目录，默认 `<项目目录>/results` |
| `MAX_WORKERS` | | 并行索引线程数，默认 `3` |
| `TG_TOKEN` | | Telegram Bot Token，留空则自动禁用日报 |
| `TG_CHAT_ID` | | Telegram Chat ID |

### 注意事项

1. 两个服务共用同一个 `RESULTS_DIR`，不要配置成不同路径
2. 服务一（web）只读索引，不监控文件变化
3. 服务二（mcp）负责监控 vault 目录、自动触发索引，以及 Claude Code 的 `search_notes` / `grep_notes` 工具
4. `VAULT_PATH` 是 Vault 根目录的绝对路径，不是子目录
5. `TG_TOKEN` / `TG_CHAT_ID` 留空或不注入时，Telegram 日报自动禁用，不报错

## 独立运行

不通过 Molly 直接运行时，在项目根目录创建 `.env` 文件（已加入 `.gitignore`）：

```env
VAULT_PATH=/path/to/your/vault
VAULT_NAME=MyVault
LLM_API_KEY=your_key
LLM_SERVICE_BASE_URL=https://api.siliconflow.cn/v1
PAGEINDEX_MODEL=Qwen/Qwen3-32B
```

```bash
# Web UI（http://127.0.0.1:7842）
bash start_web.sh

# MCP Server（供 Claude Code 调用）
uv run python mcp_server.py

# 每日报告（手动触发）
uv run python daily_report.py

# 批量索引整个 Vault
uv run python batch_index.py

# 手动索引单个文件（结果写入 ./results/<basename>_structure.json）
uv run python run_pageindex.py --md_path /path/to/note.md
```

## 注册为 MCP Server（Claude Code）

在 Claude Code 的 `settings.json` 中添加：

```json
{
  "mcpServers": {
    "pageindex": {
      "command": "/path/to/molly.pageindex/.venv/bin/python",
      "args": ["/path/to/molly.pageindex/mcp_server.py"],
      "env": {
        "VAULT_PATH": "/path/to/your/vault",
        "VAULT_NAME": "MyVault",
        "LLM_API_KEY": "your_key",
        "LLM_SERVICE_BASE_URL": "https://api.siliconflow.cn/v1"
      }
    }
  }
}
```

注册后可在 Claude Code 中直接调用 `search_notes`、`find_notes` 和 `grep_notes`。

## 原版说明

PageIndex 是一个无向量数据库的 RAG 系统，通过构建文档树状索引 + LLM 推理实现类人检索。详见 [原版 README](https://github.com/VectifyAI/PageIndex)。
