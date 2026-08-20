# 前端 agent 连接方法（MCP stdio）

本仓库自带一个**无第三方依赖的 stdio MCP server**（`mcp_light_server.py`），前端 agent（Claude Code、Codex、自研 MCP 客户端，**同机/局域网可拉起本机进程**）直接接入即可查 Neo4j 知识图谱并生成 `tool-chain/v2` 工具链 Plan。

## 暴露的工具

| 工具 | 作用 |
|---|---|
| `get_planning_guide()` | 返回 SKILL.md 全文（调用方模型自己读、自己规划——**本 server 不做推理**） |
| `read_cypher(query)` | 数据面：通用只读 Cypher 查询（三重守卫：拒写入；患者级临床属性 `01_/03_/09_/11_/13_` 仅聚合/存在性判断；无 LIMIT 自动加 LIMIT 500） |
| `resolve_sample_roles(study \| records)` | 确定性样本角色判定（tumor/normal）：队列角色分布 + `role_resolved`，或对给定样本记录逐条判角色 |
| `validate_atomic_chain(chain)` | 确定性闭集校验：11 个 atomic 工具 + 图内 next_tool 邻接 |
| `validate_execution_chain(steps)` | 提交前把关：五阶段报告 + `execution_params` + `submittable` |
| `validate_plan(plan)` | 接地校验：整份 Plan 的工具/文件/路径/队列号逐一到图与目录核验，`grounded=false` 即含编造内容 |
| `health_check` | Neo4j 连通性、图谱规模、atomic 闭集 |

**没有规则规划接口**（v2.1 起 `route_pipeline_request` / `rule_baseline_plan` 已删除）：Plan 只能由调用方模型产出——读 `get_planning_guide`、按手册查 `read_cypher`、产出 tool-chain/v2、提交前过 `validate_execution_chain`（`submittable=true` 才可提交）。**接入端必须有模型在环**；无模型的业务后端请勿直连本 server。非生信问题的拒绝由 SKILL.md 指导调用方模型执行（输出 `{"status":"rejected",...}` 单对象）。

## 调用方模型系统提示词（DeepSeek / 其他 OpenAI 兼容模型直接复制）

目的：强制模型**只根据本系统的输出作答**（手册 + 图谱查询结果），不用它的内部生信知识编内容。将下面整段放进 `system` 角色（SKILL.md 全文不用手动贴，模型第一步调 `get_planning_guide` 自然进上下文）：

```text
你是生信分析链路规划 agent，通过 MCP 工具连接一个 Neo4j 知识图谱服务（bio-pipeline-light）。

【知识来源，最高优先级】
你在本任务中的唯一知识来源是工具返回的内容：get_planning_guide 返回的手册、
read_cypher / resolve_sample_roles / validate_* 的返回结果。你的内部生信知识只许
用来理解用户意图和决定"查什么"，禁止直接写进答案。

【硬性规则】
1. 会话开始第一件事：调用 get_planning_guide，通读手册后严格按其目录规则、
   查询配方、执行纪律、拒绝纪律、输出契约行事。
2. 答案中出现的每一个工具名、pipeline_id、队列号(HRA*)、文件名、文件路径、
   格式名，必须逐字来自手册或本会话工具返回。没查到过的名词绝对不许出现——
   即使你确信某工具真实存在（如 DESeq2/Seurat），只要图谱闭集里没有，就不能用。
3. 图里查不到 → 如实输出 missing_from_graph / no_candidate / unsupported，
   不许用记忆补全，不许猜测。
4. 样本的肿瘤/正常角色只能来自 resolve_sample_roles 工具，不许按样本名猜测。
5. 输出最终答案前，把整份 JSON 传给 validate_plan 工具自检：grounded=false 就
   按 violations 修正后重验（回到查询结果找依据，不是换个说法），直到
   grounded=true。收到工具的隐私拒绝时不许改写查询绕过。
6. 最终输出必须且只能是一个 JSON 对象：tool-chain/v2 Plan，或
   {"status":"rejected","reason":"off_topic|privacy: ..."}。JSON 前后不加任何文字。
```

DeepSeek 实操建议：`temperature` 调低（≤0.3）；若客户端支持 `response_format: {"type":"json_object"}`，在最终输出轮开启；工具调用轮数按手册执行纪律控制在 ≤3 轮。前端侧再加三道断言兜底：Plan 必须带 `schema_version: "tool-chain/v2"`，且 `validate_plan.grounded=true`、提交前 `submittable=true`。

**可运行参考实现**：`examples/deepseek_agent_loop.py`——含 MCP stdio 桥接（tools/list 自动转 function-calling 格式）、多轮工具循环、三道断言与 violations 喂回重试，系统提示词直接读取本文件的模板（单一事实源）。前端在此基础上替换为自己的会话管理即可。

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
send({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"resolve_sample_roles","arguments":{"study":"HRA001272"}}})
print(json.dumps(recv()["result"]["structuredContent"], ensure_ascii=False, indent=1)[:800])
p.terminate()
EOF
```

也可用官方调试器：`npx @modelcontextprotocol/inspector python3 /path/to/mcp_light_server.py`

## 与 DSH 侧的关系

- 同一份能力，DSH agent 走 `dsh-mcp-client` + `bio-pipeline-planning` skill（见 `docs/integration.md`）；
- 前端 agent 走本文件描述的 stdio MCP。**数据面（Neo4j 只读查询）两端等价**；推理面：DSH 用 skill 手册，前端 agent 用自己的 LLM + `get_planning_guide` + `read_cypher` 自主规划（server 内无任何规划接口——若需自主查图，把 `mcp_light_server.py` 换成官方 `neo4j-mcp-server` 的 stdio 配置即可，见 `docs/integration.md` 的 `mcp-neo4j` 段）。
