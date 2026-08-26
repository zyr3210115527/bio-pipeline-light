# 前端 agent 连接方法（MCP stdio）

本仓库自带一个**无第三方依赖的 stdio MCP server**（`mcp_light_server.py`），前端 agent（Claude Code、Codex、自研 MCP 客户端，**同机/局域网可拉起本机进程**）直接接入即可查 Neo4j 知识图谱并生成 `tool-chain/v2` 工具链 Plan。

## 暴露的工具

| 工具 | 作用 |
|---|---|
| `get_planning_guide()` | 返回 SKILL.md 全文（调用方模型自己读、自己规划——**本 server 不做推理**） |
| `read_cypher(query)` | 数据面：通用只读 Cypher 查询（三重守卫：拒写入；患者级临床属性 `01_`–`13_`（全部编号前缀，只放行 `00_*` 操作性标识）仅聚合/存在性判断；无 LIMIT 自动加 LIMIT 500） |
| `read_cypher_batch(queries)` | 批量只读 Cypher：多条独立查询一次调用（逐条同等守卫），打包省轮数 |
| `get_study_overview(study)` | 队列画像一包到底：study 信息 + 样本数 + T1/T2 分布与文件样例 + 角色分布（替代多查组合） |
| `resolve_sample_roles(study \| records)` | 确定性样本角色判定（tumor/normal）：队列角色分布 + `role_resolved`，或对给定样本记录逐条判角色 |
| `validate_atomic_chain(chain)` | 确定性闭集校验：11 个 atomic 工具 + 图内 next_tool 邻接 |
| `validate_execution_chain(steps)` | 提交前把关：五阶段报告 + `execution_params` + `submittable` |
| `validate_plan(plan)` | 接地校验：整份 Plan 的工具/文件/路径/队列号逐一到图与目录核验，`grounded=false` 即含编造内容 |
| `health_check` | Neo4j 连通性、图谱规模、atomic 闭集 |
| `route_pipeline_request(query, top_k, data_matcher_mode)` | **兼容层**：一次调用返回顶层 `tool-chain/v2` 执行合同。只给「一个 query 换一个答案」的客户端用，见下文专节 |

**server 端没有规则规划器。** 上表除 `route_pipeline_request` 外的工具都不做推理：Plan 由调用方模型产出——读 `get_planning_guide`、按手册查 `read_cypher`、产出 tool-chain/v2、提交前过 `validate_execution_chain`（`submittable=true` 才可提交）。非生信问题的拒绝由 SKILL.md 指导调用方模型执行（输出 `{"status":"rejected",...}` 单对象）。

`route_pipeline_request` 是这条规则的**包装**而不是例外：它内部照样起一轮模型循环（用 server 自己配的 LLM）走完全同一套流程，只是把这轮循环关进了一次工具调用里。**已经自己在跑 agent 循环的客户端不要用它**——那等于两个模型套娃，慢一倍且丢失你自己的会话上下文。

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

## 单次调用模式：`route_pipeline_request`（Cohort Agent 兼容层）

给**只能调一次工具**的客户端用：上游 agent 把用户原话丢进来，拿回一份可直接提交给 PipelineBuilder 的执行合同，中间的手册、查图、校验、补全全在 server 内部走完。上游不需要认识 `get_planning_guide`，也不需要自己拼 Cypher。

```json
{"name": "route_pipeline_request",
 "arguments": {"query": "我想对肝癌 bulk RNA-seq 数据做免疫浸润分析",
               "top_k": 3, "data_matcher_mode": "neo4j"}}
```

`top_k` 默认 3；`data_matcher_mode` 只有 `neo4j` 一种实现（数据匹配本来就只走图），传别的值会照常执行并在返回里附 `data_matcher_note` 说明被忽略了。

内部一轮完整循环：`query → get_planning_guide → 模型 → read_cypher / read_cypher_batch / get_study_overview → validate_atomic_chain → hydrate_plan → validate_plan → 合同转换 → tool-chain/v2`。它复用 `web/server.py` 的 `AgentRunner`（进程内直调，不再 spawn 一个 MCP 子进程），所以网页端积累的请求对冲、漏调用回收、轮数收敛这些长尾治理对它同样生效。

需要的环境变量（和网页服务同一套，写在 `web/config.local` 或进程 env 里）：

| 变量 | 说明 |
|---|---|
| `LLM_API_KEY` | 必填，没有就直接返回 `no_candidate` |
| `LLM_BASE_URL` | OpenAI 兼容端点 |
| `LLM_MODEL` | 模型名 |
| `LLM_TIMEOUT` | 单次请求超时秒数 |

### 返回：顶层就是 tool-chain/v2

**没有 `{"status":"ok","plan":{...}}` 外层信封**，`schema_version` 在最顶层：

```json
{
  "schema_version": "tool-chain/v2",
  "selection_status": "ready",
  "candidates": [{
    "rank": 1, "match_id": "cand-1", "pipeline_id": "immune_infiltration_iobr",
    "validation_ok": true, "feasibility_status": "ready", "study_accession": "HRA001272",
    "assets": [{"asset_id": "asset-1", "file_name": "HRA001272-Genes-TPM-1.0.tsv",
                "path": "/hpcdisk1/.../HRA001272-Genes-TPM-1.0.tsv",
                "file_path": "/hpcdisk1/.../HRA001272-Genes-TPM-1.0.tsv",
                "artifact_type": "tsv", "semantic_format": "TABULAR_BIO_DATA",
                "study_accession": "HRA001272", "match_reason": "..."}],
    "tool_chain": [{"step_id": "step-1", "tool_id": "immune_infiltration_iobr",
                    "inputs": {"expression_tsv": {"asset_id": "asset-1"},
                               "clinical_xls":   {"asset_id": "asset-2"},
                               "metainfo_xlsx":  {"asset_id": "asset-3"}}}],
    "execution_params": {"expression_tsv": "/hpcdisk1/...", "clinical_xls": "/hpcdisk1/..."},
    "execution_params_missing": []
  }],
  "recommendations": [ ... ],
  "planner_metadata": {"used": false, "reason": "no_server_side_planner",
                       "planning_owner": "caller_model", "arch": "light"},
  "mcp_timing_ms": 5842
}
```

几处执行端会踩的细节：

- `assets[].path` 和 `file_path` 同值双写。PipelineBuilder 读 `path`，图里存的字段叫 `file_path`，只给一个就有一端拿到空。
- `tool_chain[].inputs` 是**执行合同**（`{asset_id}` / `{value}` / `{from:{step_id,output}}`），不是 `validate_atomic_chain` 返回的那种 IO 描述数组。描述数组原样提交必被拒。
- `recommendations[].execution_params` 的键与 `recommendations[].tool.inputs[].builder_param` **逐字节相同**，`tool.catalog_status == "registered"`、`data.status == "available"`——Dingent 少一条就静默丢掉整条推荐，不报错。

### `selection_status` 五种取值

| 值 | 含义 | 上游该做什么 |
|---|---|---|
| `ready` | 至少一条候选参数绑全，可直接提交 | 取 `feasibility_status=="ready"` 的候选提交 |
| `needs_input` | 找到了流程，但有参数只能由人给（典型是差异表达的 `group_a_samples`/`group_b_samples`） | 读 `execution_params_missing` 逐项问用户 |
| `information` | **已废弃**，不再产出 | 历史返回可能还带它；新版一律出 rank1 推荐，问工具属性的问句把属性放 `answer`、推荐照给 |
| `unsupported` | 非生信问题或触碰隐私红线，被拒 | 把 `unsupported_reason` 给用户 |
| `no_candidate` | 闭集里没有能干这件事的流程，或模型/Neo4j 不可用 | 看 `unsupported_reason` 与 `planner_metadata.reason` |

**不存在降级路径。** 模型不可用、Neo4j 连不上、返回不可解析，都走 `no_candidate` + `unsupported_reason` + `planner_metadata.reason="upstream_unavailable"`，**不会退回词表规则拼一个看起来像样的 Plan**——那种「一路绿灯但内容是编的」比直接报错难查得多。同理，`ready` 的判定只认图里查得到的绝对路径：文件名匹配不到、`NOT_FOUND`、非绝对路径，一律不给 `ready`。非 File 的必填参数只在能确定性推导时才填（accession、矩阵口径 TPM/FPKM/counts 之类），推不出来的如实进 `execution_params_missing`，绝不拿交付包里别的队列跑过的分组值顶上。

### 接 PipelineBuilder

`candidates[i]` 转成 PipelineBuilder 的 `agent_input` 后喂给 `plan_tool_chain_submission`，应得到 `can_submit: true` 且 `plan_hash` 非空。本仓库不含 PipelineBuilder MCP，这一步只能在执行端验证。

### 自测

```bash
python3 tests/test_cohort_compat.py --offline   # 只验合同形状，不调 LLM（需 Neo4j）
python3 tests/test_cohort_compat.py             # 追加 3 例真实模型循环
```

## 前置条件

- 可达的 Neo4j 实例，账号 `neo4j`。**默认值是本机开发地址，现网不是本机**——现网为 HTTP `http://192.168.130.24:7480/db/neo4j/tx/commit`、bolt `bolt://192.168.130.24:7690`，已灌 0826 交付（81,572 节点 / 364,260 关系 / 55 工具 / 30 受控功能词）。接前必须显式设 `NEO4J_URL`，否则会静默连本机 7474 然后报「Neo4j 请求失败」。
- 环境变量：`NEO4J_PASSWORD`（必填）、`NEO4J_USER`（默认 neo4j）、`NEO4J_URL`（默认 `http://127.0.0.1:7474/db/neo4j/tx/commit`，仅适用于本机开发）。口令走环境变量，不要写进仓库文件。
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
        "NEO4J_URL": "http://192.168.130.24:7480/db/neo4j/tx/commit"
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
