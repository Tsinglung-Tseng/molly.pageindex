# molly.pageindex

> Fork of [VectifyAI/PageIndex](https://github.com/VectifyAI/PageIndex) — Vectorless, Reasoning-based RAG for Obsidian Vault.

本项目在 PageIndex 原版基础上，为个人 Obsidian Vault 做了深度定制，作为 Molly 系统的文档检索引擎。

## 相较原版的改动

- **SiliconFlow API**：对接 SiliconFlow，支持 Qwen 系列模型
- **MCP Server**：后台 watchdog 自动索引 + `search_notes` / `grep_notes` 工具
- **Web UI**：本地搜索界面，支持 AI 问答、搜索历史（SQLite 持久化）、sidebar tab 状态持久化
- **深链接**：搜索结果直接跳转到对应标题锚点（`obsidian://` deep link）
- **并行检索**：多文档 Stage 2 并行加速，显著降低搜索延迟
- **增量索引 + Telegram 日报**：每日定时扫描 Vault 变更，发送 Telegram 摘要；首次运行自动对根目录文件建立初始索引
- **Vault 隔离存储**：results / state / history 均按 vault_name 隔离，支持多 vault 并存；结果写入 `results/<vault_name>/`

## 安装

```bash
uv sync
```

## 配置

所有配置在 `config.yaml`（从 `config.example.yaml` 复制，已加入 `.gitignore`）：

```yaml
llm_api_key: "sk-..."
llm_base_url: https://api.siliconflow.cn/v1
model: Qwen/Qwen3.5-35B-A3B

vault_path: ~/obsidian/MyVault   # 不通过 Molly 运行时填写

telegram:
  enabled: true
  token: ""
  chat_id: ""
```

## 在 Molly 中配置

本项目作为 Molly 的托管子服务运行。需注册两个独立进程。

### 服务一：Note Indexer (web)

| 字段 | 值 |
|------|----|
| 启动命令 | `uv run python main.py --host 127.0.0.1 --port 7842` |

### 服务二：Note Indexer (mcp)

| 字段 | 值 |
|------|----|
| 启动命令 | `.venv/bin/python mcp_server.py` |

### Molly 注入的环境变量

| 变量 | 说明 |
|------|------|
| `MOLLY_VAULT_PATH` | Vault 根目录绝对路径（必填） |
| `MOLLY_LLM_MODEL` | 模型名，覆盖 config.yaml |
| `MOLLY_LLM_API_KEY` | API Key，覆盖 config.yaml |
| `MOLLY_LLM_API_URL` | Base URL，覆盖 config.yaml |

其余配置（端口、并发数、TG 等）在 `config.yaml` 中设置。

## 独立运行

直接在项目目录下运行，确保 `config.yaml` 已配置 `vault_path`：

```bash
# Supervisor（Web UI + 每日定时索引）
uv run python main.py

# MCP Server（供 Claude Code 调用）
uv run python mcp_server.py

# 手动触发增量索引（首次运行：对根目录文件建立初始索引；后续：只处理变更文件）
uv run python batch_index.py

# 批量索引调试（MCP Inspector）
npx @modelcontextprotocol/inspector \
  .venv/bin/python mcp_server.py
```

## 注册为 MCP Server（Claude Code）

在 `~/.claude/settings.json` 中添加：

```json
{
  "mcpServers": {
    "pageindex": {
      "command": "/path/to/molly.pageindex/.venv/bin/python",
      "args": ["/path/to/molly.pageindex/mcp_server.py"]
    }
  }
}
```

注册后可在 Claude Code 中直接调用 `search_notes`、`find_notes`、`grep_notes`。

## 数据目录（Vault 隔离）

| 数据 | 路径 |
|------|------|
| 索引结果 | `results/<vault_name>/` |
| 状态快照 | `.vault_state_<vault_name>.json` |
| 历史数据库 | `history_<vault_name>.db` |

切换 vault 只需改 `MOLLY_VAULT_PATH`，数据自动隔离。

## 原版说明

PageIndex 是一个无向量数据库的 RAG 系统，通过构建文档树状索引 + LLM 推理实现类人检索。详见 [原版 README](https://github.com/VectifyAI/PageIndex)。
