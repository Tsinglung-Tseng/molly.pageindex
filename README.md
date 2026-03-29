# molly.pageindex

> Fork of [VectifyAI/PageIndex](https://github.com/VectifyAI/PageIndex) — Vectorless, Reasoning-based RAG for Obsidian Vault.

本项目在 PageIndex 原版基础上，为个人 Obsidian Vault 做了深度定制，作为 Molly 系统的文档检索引擎。

## 相较原版的改动

- **SiliconFlow API**：对接 SiliconFlow，支持 Qwen 系列模型（`OPENAI_API_KEY` / `CHATGPT_API_KEY` 均可）
- **MCP Server**：后台 watchdog 自动索引 + `search_notes` / `grep_notes` 工具
- **Web UI**：本地搜索界面，支持 AI 问答、搜索历史（SQLite 持久化）、sidebar tab 状态持久化
- **深链接**：搜索结果直接跳转到对应标题锚点（`obsidian://` deep link）
- **并行检索**：多文档 Stage 2 并行加速，显著降低搜索延迟
- **每日 Vault 报告**：扫描 Vault 变更，通过 Telegram 推送每日摘要
- **PM2 部署**：`ecosystem.config.js` 统一管理 Web UI 进程

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `OPENAI_API_KEY` | ✅ | SiliconFlow API Key（或用 `CHATGPT_API_KEY`，二选一） |
| `OPENAI_BASE_URL` | ✅ | `https://api.siliconflow.cn/v1` |
| `PAGEINDEX_MODEL` | 可选 | 默认 `Qwen/Qwen3-32B` |

推荐在项目根目录创建 `.env` 文件（已加入 `.gitignore`）：

```env
OPENAI_API_KEY=your_key
OPENAI_BASE_URL=https://api.siliconflow.cn/v1
PAGEINDEX_MODEL=Qwen/Qwen3-32B
```

## 安装

```bash
uv sync
```

## 运行

```bash
# Web UI（http://127.0.0.1:7842）
pm2 start ecosystem.config.js
# 或直接运行
bash start_web.sh

# MCP Server（供 Claude Code 调用）
uv run python mcp_server.py

# 每日报告（手动触发）
python daily_report.py

# 批量索引整个 Vault
python batch_index.py /path/to/vault
```

## 注册为 MCP Server

在 Claude Code 的 `settings.json` 中添加：

```json
{
  "mcpServers": {
    "pageindex": {
      "command": "/path/to/molly.pageindex/.venv/bin/python",
      "args": ["/path/to/molly.pageindex/mcp_server.py"],
      "env": {
        "OPENAI_API_KEY": "your_key",
        "OPENAI_BASE_URL": "https://api.siliconflow.cn/v1"
      }
    }
  }
}
```

注册后可在 Claude Code 中直接调用 `search_notes` 和 `grep_notes`。

## 原版说明

PageIndex 是一个无向量数据库的 RAG 系统，通过构建文档树状索引 + LLM 推理实现类人检索。详见 [原版 README](https://github.com/VectifyAI/PageIndex)。
