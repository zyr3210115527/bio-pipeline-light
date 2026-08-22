# Bio Pipeline Light Web 前端

Kimi 风格网页聊天前端：实时展示**思考段（thinking）**与**工具调用卡片**，后端把
Gemini generateContent（thinking + function calling）与 `mcp_light_server.py`
（Neo4j 知识图谱）桥接成 agent 循环——推理归模型，MCP 只给知识与校验。

## 运行

```bash
# 配置（任选其一）：环境变量，或 web/config.local（每行 KEY=VALUE，已 gitignore）
export GEMINI_API_KEY=sk-...          # Gemini 兼容端点 Bearer key
export NEO4J_USER=neo4j NEO4J_PASSWORD=...   # 透传给 MCP server

python3 web/server.py                 # 默认 http://127.0.0.1:8017
```

可选环境变量：`GEMINI_BASE_URL`（默认 `https://llm-center.modelbest.co`）、
`GEMINI_MODEL`（默认 `gemini-3.7-flash`）、`NEO4J_URL`（默认本机 7474）、`PORT`。

## 说明

- 纯 Python 标准库 + 单文件 HTML/JS，无 pip / npm 依赖。
- 非流式 `generateContent` 在该代理上会挂起，后端统一走
  `:streamGenerateContent?alt=sse`，并把 `thought: true` 部件作为思考段推送。
- 系统提示词直接取 `docs/frontend-mcp-connection.md` 的权威模板；工具列表来自
  MCP `tools/list`，schema 自动适配为 Gemini functionDeclarations。
- `manual_compact.md`：web 层默认使用的精简手册（21KB≈7k tokens，落进端点 8192 缓存帽；全部坑表/闭集/契约保留）。缺失时回退 `skill/SKILL.md` 全量版。工具描述在适配层另有短版（`TOOL_DESC_SHORT`），MCP server 端不变。
- 会话历史（含 `thoughtSignature`）保存在服务端内存，`POST /api/reset` 清除。
- 前端：思考段（流式/可折叠）、工具调用时间线（参数/返回可展开）、
  **tool-chain/v2 Plan 编排视图**（状态徽章、意图 chips、atomic 链步骤流、I/O 契约双列、
  数据资产表含角色徽章、备选队列、原始 JSON 折叠）；散文兜底为 Markdown 渲染。
- 接口：`GET /` 页面；`GET /api/health` 健康检查；`POST /api/chat`
  `{session, message}` SSE 事件流（`thought` / `text` / `tool_call` /
  `tool_result` / `done` / `error`）。
