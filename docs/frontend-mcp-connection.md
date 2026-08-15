# 前端 agent 连接方法（MCP stdio）

本仓库自带一个**无第三方依赖的 stdio MCP server**（`mcp_light_server.py`），前端 agent（Claude Code、Codex、自研 MCP 客户端，**同机/局域网可拉起本机进程**）直接接入即可查 Neo4j 知识图谱并生成 `tool-chain/v2` 工具链 Plan。

## 暴露的工具

| 工具 | 作用 |
|---|---|
| `health_check` | Neo4j 连通性与图谱规模（只读） |
| `plan_bio_analysis(query)` | 自然语言生信需求 → `tool-chain/v2` Plan（匹配工具 + 队列候选 + 格式文件数） |

**拒绝门（fail-closed）**：`plan_bio_analysis` 收到与生信无关的问题（雅思/签证/前端/股票/天气等）直接拒绝，返回：

```json
{ "status": "rejected", "reason": "rejected: 非生信问题", "bio_hits": [], "non_bio_hits": ["雅思", "口语"] }
```

96 例生信问题回归：**96/96 放行，0 误杀**（含 "Reactome 通路" 等易误伤场景，词边界匹配）。

## 前置条件

- 本机 Neo4j 运行中（bolt 7687），账号 `neo4j`
- 环境变量：`NEO4J_PASSWORD`（必填）、`NEO4J_USER`（默认 neo4j）、`NEO4J_URL`（默认 `http://127.0.0.1:7474/db/neo4j/tx/commit`）
- 依赖：仅 `python3` 标准库 + 本机 `curl`（无 pip 依赖）

## 连接方式（按前端 agent 类型）

### 1. Claude Code / 兼容 `.mcp.json` 的客户端（推荐）

仓库根目录已有 `.mcp.json`，Claude Code 打开本仓库即自动加载。确认环境变量后直接问：

```bash
export NEO4J_PASSWORD=<你的密码>
claude
> 我想看肝癌样本里的免疫细胞组成，怎么分析？
```

### 2. 其他 MCP 客户端（Codex / 自研）

把下面这段 `mcpServers` 填进客户端的 MCP 配置（`${NEO4J_PASSWORD}` 换成实际值或环境变量引用）：

```json
{
  "mcpServers": {
    "bio-pipeline-light": {
      "type": "stdio",
      "command": "python3",
      "args": ["/path/to/bio-pipeline-light/mcp_light_server.py"],
      "env": {
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "***",
        "NEO4J_URL": "http://127.0.0.1:7474/db/neo4j/tx/commit"
      }
    }
  }
}
```

自研客户端：spawn 该进程，按 MCP stdio 协议（newline-delimited JSON-RPC）通信，业务数据在 `tools/call` 返回的 `result.structuredContent`。

### 3. 手动测试（不依赖 agent）

```bash
export NEO4J_PASSWORD=<你的密码>
python3 - <<'EOF'
import subprocess, json, select
p = subprocess.Popen(["python3", "mcp_light_server.py"], stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, text=True)
def send(m): p.stdin.write(json.dumps(m) + "\n"); p.stdin.flush()
def recv():
    r, _, _ = select.select([p.stdout], [], [], 30)
    return json.loads(p.stdout.readline()) if r else None
send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}})
recv()
send({"jsonrpc":"2.0","method":"notifications/initialized","params":{}})
send({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})
print("tools:", [t["name"] for t in recv()["result"]["tools"]])
send({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"plan_bio_analysis","arguments":{"query":"肝癌样本免疫浸润怎么分析"}}})
print(json.dumps(recv()["result"]["structuredContent"], ensure_ascii=False, indent=1)[:800])
p.terminate()
EOF
```

也可用官方调试器：`npx @modelcontextprotocol/inspector python3 /path/to/mcp_light_server.py`

## 与 DSH 侧的关系

- 同一份能力，DSH agent 走 `dsh-mcp-client` + `bio-pipeline-planning` skill（见 `docs/integration.md`）；
- 前端 agent 走本文件描述的 stdio MCP。**数据面（Neo4j 只读查询）两端等价**；推理面：DSH 用 skill 手册，前端 agent 直接用 `plan_bio_analysis` 拿到 Plan（或用自己的 LLM + `read-cypher` 自主规划——若需自主查图，把 `mcp_light_server.py` 换成官方 `neo4j-mcp-server` 的 stdio 配置即可，见 `docs/integration.md` 的 `mcp-neo4j` 段）。
