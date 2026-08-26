# Bio Pipeline Light 架构详解

> 面向没有接触过本项目的读者。不假设你了解 MCP、知识图谱或 Agent，但也不用比喻绕弯——直接讲机制、协议、代码和实测数据。
>
> 术语首次出现时给定义，第 12 章有速查表。

**目录**

1. [系统定位与输入输出](#1-系统定位与输入输出)
2. [背景：原架构的问题](#2-背景原架构的问题)
3. [架构总览：四层结构](#3-架构总览四层结构)
4. [MCP 协议：机制与报文](#4-mcp-协议机制与报文)
5. [工具调用与 Agent 循环](#5-工具调用与-agent-循环)
6. [第一层：知识图谱](#6-第一层知识图谱)
7. [第二层：Skill](#7-第二层skill)
8. [第三层：十个 MCP 工具](#8-第三层十个-mcp-工具)
9. [如何保证 Cypher 写对，写错会怎样](#9-如何保证-cypher-写对写错会怎样)
10. [Benchmark](#10-benchmark)
11. [与原架构的对比](#11-与原架构的对比)
12. [后续优化方向](#12-后续优化方向)
13. [名词速查表](#13-名词速查表)

---

## 1. 系统定位与输入输出

**输入**：自然语言的生信分析需求。

> "我想看看肝癌病人里哪些基因表达有差异，再做个通路富集。"

**输出**：一份结构化的分析方案 JSON（契约名 `tool-chain/v2`）：

```json
{"schema_version":"tool-chain/v2","selection_status":"ok",
 "recommendations":[{
   "pipeline_id":"diff_expr_kegg",
   "match_note":"命中差异表达+KEGG富集；肝癌队列 HRA001272 有现成 TPM 矩阵",
   "tool":{"tool_id":"diff_expr_kegg","catalog_id":"...","tool_kind":"pipeline",
           "inputs":[...],"outputs":[...]},
   "data":{"assets":[{"file_name":"HRA001272-Genes-TPM-1.0.tsv",
                      "file_path":"/data/.../HRA001272-Genes-TPM-1.0.tsv",
                      "format":"tsv","data_level":"3",
                      "match_reason":"癌种/队列匹配; 格式匹配"}],
           "study_accessions":["HRA001272"]}}],
 "intent":{...}}
```

**系统边界**：只做规划和数据核对，不执行任何生信软件。不生成 BAM，不跑比对，不做统计。执行是下游的事。

**三个技术难点**，分别对应架构里的三块设计：

| 难点 | 说明 | 对应设计 |
|---|---|---|
| 意图→工具映射模糊 | "看看哪些基因不一样"可能指差异表达、突变景观或拷贝数变异。用户不用工具名说话 | Skill 手册（§7） |
| 幻觉 | 模型会编造格式合理、命名规范、路径像模像样的**不存在的文件**，且置信度与说真话时完全相同 | 接地校验（§7.5、§8.8） |
| 延迟 | 模型每推理一轮耗时几秒到几十秒。来回七八轮用户就等两分钟 | 轮数纪律 + 服务端代跑（§5.4） |

---

## 2. 背景：原架构的问题

原项目 **bio-pipeline-kg-matcher**（下称重版）是一个 205 MB 的服务，包含：

- 内嵌的大模型调用（180 秒超时，16k 输出上限）
- 两套数据匹配器（CSV 一套、Neo4j 一套，互相对比）
- 113 个 CSV、157 个审计文件、16 个演示录像
- `workflow_composer.py`：132 KB 的 if-else 规则

调 `route_pipeline_request` 一次返回现成 Plan。

**问题：**

1. **推理硬编码在服务端。** "用户说'差异'就选 diff_expr"写死在代码里。新增流程要改代码重新部署；用户换个说法（"哪些基因在两组之间不一样"）规则就失配。
2. **体积绝大部分是死重量。** 113 个 CSV、157 个审计文件、16 个录像对规划本身零贡献。
3. **评测集本身不难。** 96 例测试集的题面大多直接含答案触发词（问"请做 WGCNA 共表达网络分析"，答案就是 `wgcna`），工具选择这一环本身是平凡的。light 在这套集子上工具 top-1 拿满分（§10.1），但这更多说明任务本身不难，而非架构优越。

**轻架构的主张：推理交给调用方的大模型，服务端只提供知识和确定性校验。**

- 服务端不做推理，没有"一次调用出 Plan"的接口
- 推理写成手册（Skill）交给模型读，纯文本，改一行即生效
- 凡能确定性算出的一律不让模型算：工具是否在闭集、文件是否在图里、路径是否真实

体积 205 MB → 约 110 KB。

---

## 3. 架构总览：四层结构

```
┌──────────────────────────────────────────────────────────────────┐
│  第四层  编排层  web/server.py（纯标准库，零第三方依赖）             │
│  · 启动时把 manual_compact.md 全文内联进系统提示词                  │
│  · function-calling 循环：模型请求调工具 → 转发 MCP → 回灌结果      │
│  · 终答后自动跑 hydrate_plan + validate_plan（模型不可见）          │
│  · SSE 推流：思考段 + 工具调用可视化                                │
│  · MAX_ROUNDS=15，QUERY_ROUND_BUDGET=3（取数轮数硬预算）            │
└──────────┬───────────────────────────────────┬───────────────────┘
           │ 系统提示词内联                      │ JSON-RPC over stdio
   ┌───────▼────────┐                 ┌─────────▼──────────┐
   │ 第二层  Skill   │                 │ 第三层  MCP 工具    │
   │ SKILL.md       │  get_planning_  │ mcp_light_server.py│
   │  英文全量 709行 │  guide 返回 →   │  10 个工具          │
   │ manual_compact │                 │  查图/画像/校验/补全 │
   │  .md 中文 369行 │                 │  三重守卫           │
   │ 派生关系，必须同步│                └─────────┬──────────┘
   └────────────────┘                           │ 只读 Cypher (HTTP)
                                      ┌─────────▼──────────┐
                                      │ 第一层  Neo4j 图谱  │
                                      │ 81,628 节点         │
                                      │ 364,184 关系        │
                                      │ 唯一事实来源，只读   │
                                      └────────────────────┘
```

| 层 | 实体 | 职责 | 关键约束 |
|---|---|---|---|
| 知识图谱 | Neo4j 数据库 | 记录工具、数据、链路关系 | 唯一事实来源，只读 |
| Skill | 两份 Markdown | 规划方法论 | 纯文本，改完重启即生效 |
| MCP 工具 | 一个 Python 进程 | 查询与确定性校验 | 不做推理 |
| 编排层 | 一个 HTTP 服务 | 串联模型与工具 | 纯标准库 |

---

## 4. MCP 协议：机制与报文

### 4.1 MCP 解决什么问题

**MCP = Model Context Protocol**，Anthropic 提出的开放标准，规定大模型客户端如何发现和调用外部工具。

在 MCP 之前，每接一个工具后端都要写专用适配代码：怎么声明工具、参数怎么传、结果怎么回、错误怎么表达，每家一套。MCP 把这四件事标准化，于是任意 MCP 客户端（Claude Code、Cursor、我们自己的网页服务）都能接任意 MCP server，无需为对方定制。

我们的 `mcp_light_server.py` 是一个 **stdio 传输的 MCP server**：通过标准输入读请求、标准输出写响应，无网络监听。客户端把它作为子进程拉起。

### 4.2 传输层与消息格式

底层是 **JSON-RPC 2.0**，一行一个 JSON 对象（换行分隔）：

```python
def _send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()

def main():
    for line in sys.stdin:
        msg = json.loads(line)
        method = msg.get("method")
        ...
```

`flush()` 是必需的：stdout 在管道里默认全缓冲，不刷新客户端会一直阻塞等待。

### 4.3 四个方法与握手时序

服务端实现四个方法（外加对未知方法回 `-32601`）：

```
客户端                                     server
   │ ── initialize ──────────────────────────▶│
   │ ◀── protocolVersion 2024-11-05,          │
   │     capabilities{tools:{listChanged:false}},
   │     serverInfo{bio-pipeline-light, 2.1.0}│
   │ ── notifications/initialized（无需回复）──▶│
   │ ── tools/list ──────────────────────────▶│
   │ ◀── tools[]：每个含 name/description/     │
   │     inputSchema（JSON Schema）           │
   │ ── tools/call {name, arguments} ────────▶│
   │ ◀── result{content[], structuredContent} │
   │ ── ping ────────────────────────────────▶│（保活）
```

**`tools/list` 的返回是模型能力的全部来源。** 模型不会读我们的源码，它只看到这三样：

```json
{"name": "read_cypher",
 "description": "数据面：对 Neo4j 知识图谱执行只读 Cypher 查询。三重守卫：写入语句拒绝；患者级临床属性（`01_`–`13_` 全部编号前缀…）只允许聚合统计或 IS NOT NULL 存在性判断…；无 LIMIT 自动加 LIMIT 500。",
 "inputSchema": {"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}
```

**推论：description 是提示词工程的一部分，不是文档注释。** 模型靠它判断何时调、怎么调。所以 `read_cypher_batch` 的描述里直接写了"不要在多轮里逐条发"——这是写给模型的行为约束，不是给人看的说明。

### 4.4 tools/call 的返回与错误约定

成功时返回两份内容：

```json
{"jsonrpc":"2.0","id":7,
 "result":{"content":[{"type":"text","text":"{...缩进后的 JSON...}"}],
           "structuredContent":{...原始对象...}}}
```

- `content[]` 是给模型看的文本
- `structuredContent` 是给程序用的对象

编排层优先取后者，避免二次解析：

```python
if r.get("isError"):
    return {"error": r["content"][0]["text"] if r.get("content") else "MCP error"}
return r.get("structuredContent") or {"raw": r["content"][0]["text"]}
```

**关键约定：工具执行失败不返回 JSON-RPC error，而是返回带 `isError: true` 的正常 result。**

```python
try:
    out = tool["handler"](args)
    _send({... "result": {"content":[...], "structuredContent": out}})
except Exception as e:
    _send({... "result": {"content":[{"type":"text","text":f"error: {e}"}], "isError": True}})
```

区别在于责任归属：JSON-RPC `error` 表示协议层出错（方法不存在、报文格式错），客户端程序该处理；`isError` 表示工具业务出错（Cypher 语法错、隐私守卫拒绝），**这个信息应该回灌给模型让它自己改**。

这个设计对本项目至关重要：模型写错 Cypher 时，拿到的是可读的中文错误说明，下一轮能据此修正。详见 §9.4。

### 4.5 客户端配置

```json
{ "mcpServers": { "bio-pipeline-light": {
  "type": "stdio", "command": "python3",
  "args": ["/path/to/bio-pipeline-light/mcp_light_server.py"],
  "env": { "NEO4J_USER": "neo4j", "NEO4J_PASSWORD": "***" } } } }
```

仓库根目录已有 `.mcp.json`，Claude Code 打开即用。

---

## 5. 工具调用与 Agent 循环

### 5.1 function calling 机制

现代大模型支持 **function calling**：把可用工具的名称、描述、参数 schema 随请求一起发给模型，模型在生成过程中可以输出一个结构化的调用请求而非普通文本。

请求侧（OpenAI 兼容格式）：

```json
{"model":"...","messages":[...],"temperature":0.2,"max_tokens":16384,"stream":true,
 "tools":[{"type":"function","function":{"name":"read_cypher",
           "description":"...","parameters":{...inputSchema...}}}]}
```

响应侧，模型返回 `tool_calls` 而非 `content`：

```json
{"role":"assistant","content":"",
 "tool_calls":[{"id":"call_0","type":"function",
   "function":{"name":"read_cypher",
               "arguments":"{\"query\":\"MATCH (s:study) ... RETURN s.study_accession\"}"}}]}
```

注意 `arguments` 是**字符串化的 JSON**，不是对象——这是 OpenAI 格式的规定，解析时要 `json.loads` 一次。

### 5.2 什么是 Agent

**Agent** 指模型在一个循环里自主决定下一步动作，而非单次问答。本系统的循环：

```
1. 系统提示词（含手册全文）+ 用户问题 → 模型
2. 模型返回 tool_calls 或最终答案
3. 若是 tool_calls：
     对每个调用 → MCP tools/call → 拿到结果
     结果以 role:"tool" 消息追加进历史 → 回到 1
4. 若是最终答案：进入终答后处理（§5.5）
```

历史的组装（`_append_assistant`）：

```python
hist.append({"role": "assistant", "content": text or "",
             "tool_calls": [{"id": c.get("id") or f"call_{i}", "type": "function",
                             "function": {"name": c["name"],
                                          "arguments": json.dumps(c["args"] or {})}}
                            for i, c in enumerate(calls)]})
```

工具结果随后以 `{"role":"tool","tool_call_id":...,"content":...}` 追加。**`tool_call_id` 必须与请求里的 `id` 一致**，否则模型无法把结果对应回它发起的调用。

**自主性的边界**：模型能决定调什么、调几次、何时停，但不能决定有哪些工具可用（服务端过滤）、不能绕过守卫（服务端拦截）、不能无限循环（`MAX_ROUNDS=15`）。

### 5.3 两个真实的工程问题

**问题一：模型把工具调用泄漏成普通文本。**

端点偶发把 function call 当文本吐出来，本轮既无 `tool_calls` 也无合法终答，整轮产出为零。服务端做了抢救性解析：

```python
_DSML_INVOKE = re.compile(r'invoke name="([A-Za-z0-9_]+)"(.*?)(?:</[^<>]*invoke>|\Z)', re.S)

def _parse_leaked_tool_calls(text):
    """把泄漏成文本的工具调用解析回真正的调用，返回 [] 表示没泄漏。
    不修的话代价是双份的：这一轮空烧，下一轮模型还会被服务端
    「你的 JSON 语法有误」的提示带偏（实测 c86 因此直接交了空答案）。"""
    if "tool_calls>" not in text or 'invoke name="' not in text:
        return []
    ...
```

值得注意的是失败模式：不修不仅浪费一轮，还会让服务端发出误导性的报错，把模型带向更坏的方向。

**问题二：思考预算的三态。**

`_thinking_cfg` 有三种状态：`disabled`、`enabled + reasoning_effort: low`、**完全省略该字段**。三者语义不同——省略不等于禁用，禁用会让模型跳过推理直接答，实测反而更慢更差（因为答错要重来）。

### 5.4 轮数是唯一的成本

| 动作 | 耗时 |
|---|---|
| 模型推理一轮 | 几秒 ~ 几十秒 |
| 执行一条 Cypher | < 0.5 秒 |

**结论：轮数是唯一成本，查询次数几乎免费。** 由此派生出手册 §6 的效率纪律和服务端的两个机制：

**机制一：取数轮数硬预算。** `QUERY_ROUND_BUDGET = 3`。手册里的纪律是软约束，模型可能不遵守；这个预算由服务端强制——用尽后告知模型"预算已用尽"，逼它收敛。

**机制二：能合并的调用做成一个工具。** 见 §8.4 的 `get_study_overview`。

### 5.5 终答后的服务端处理

模型给出终答后，服务端自动执行两步，模型不感知：

```
终答 JSON → hydrate_plan（补全样板字段） → validate_plan（接地校验） → SSE 推给前端
```

各省一轮模型延迟。若 `grounded=false`，把 `violations` 回灌给模型，要求用**已查到的证据**修正（不许重新探索），再验一次。

### 5.6 模型可见工具的过滤

MCP server 端 10 个工具一个不删（其他客户端要用），但网页会话对模型隐藏三个：

| 隐藏 | 原因 |
|---|---|
| `get_planning_guide` | 手册已内联进系统提示词，再调是浪费一轮 |
| `validate_plan` | 纯校验闸门无信息产出，服务端代跑 |
| `hydrate_plan` | 确定性补全，服务端代跑 |

模型实际看到 7 个。

**判据：工具有无信息产出。** `validate_atomic_chain` 保留给模型，因为它返回 Knowledge Card 的 `meta_id` 和槽位名——模型拿到后要用。只回 yes/no 的闸门型工具则服务端代跑。

---

## 6. 第一层：知识图谱

### 6.1 图数据库与 Cypher

关系数据库用表和外键，图数据库用**节点**和**关系**。表达"STAR 的下游是 featureCounts"：

```cypher
(STAR) -[:next_tool]-> (featureCounts)
```

**Cypher** 是 Neo4j 的查询语言：

```cypher
MATCH (t:tool)-[:next_tool]->(next:tool)
WHERE t.tool_name = 'star'
RETURN next.tool_name
```

`MATCH` 描述图形模式，`(变量:标签)` 是节点，`-[:类型]->` 是有向关系。多跳查询（"从 FASTQ 出发能走到哪些终产物"）在图里是沿边遍历，在关系库里要多层嵌套 JOIN。

### 6.2 图的规模与构成

**81,628 节点 / 364,184 关系**（0821 交付，`valueType` 与 count 均已实测复核）。

| 节点标签 | 数量 | 内容 |
|---|---|---|
| `T2` | 35,572 | **加工产物文件**：BAM / VCF / MAF / 表达矩阵 |
| `T1` | 28,229 | **原始数据文件**：FASTQ、临床表、样本元信息 |
| `sample` | 10,465 | 样本 |
| `individual` | 7,131 | 病人个体 |
| `function` | 90 | 分析功能（中文） |
| `tool` | 51 | 工具/流程 |
| `format` | 42 | 数据格式 |
| `study` | 20 | 研究队列 |
| `project` | 18 | 项目 |
| `modal` | 6 | 组学模态 |
| `datalevel` | 4 | 数据层级 1–4 |

主要关系：

| 关系 | 含义 |
|---|---|
| `(tool)-[:next_tool]->(tool)` | 工具链接续 |
| `(tool)-[:input\|output]->(format)` | 格式契约 |
| `(tool)-[:suitable_for]->(modal)` | 适用组学 |
| `(tool)-[:has_function]->(function)` | 可做的分析 |
| `(T1\|T2)-[:in_sample]->(sample)` | 文件归属样本（28,184 条） |
| `(T2)-[:generated_from]->(T1)` | 产物溯源 |
| `(sample)-[:in_individual]->(individual)` | 样本取自个体 |
| `(individual)-[:in_study]->(study)` | 个体属于队列 |

### 6.3 数据质量：0821 交付的已知问题

**T1 只有 FASTQ。** 所有 BAM/VCF/MAF/表达矩阵都在 T2。查 T1 找 BAM 返回零行，是查错了标签而非数据缺失。

**脏字段：样本级事实被队列级默认值覆盖。** 实测：

```
tumor_descriptor:  Primary 8551 | null 1902 | Metastasis 12
tissue_type:       Tumor 6258 | Normal 2821 | null 829 | Blood 557
tissue_type='Normal' 且 tumor_descriptor='Primary' 的样本：1470
```

原有的 `Metastatic`(210) 和 `Recurrent`(407) 被压平成 `Primary`；1,470 个正常样本被标成"原发肿瘤"——自相矛盾但每格都有值。

`biospecimen_anatomic_site` 同理：HRA001272 全部 698 个样本都写 `Liver And Intrahepatic Bile Ducts`，尽管样本名显示十种不同转移部位（`LM` 肺转移 65、`PM` 腹膜转移 31、`BM` 骨转移 20…）。

**这些坏值不空、不畸形、单看都合理**，所以查询会正常返回一堆行，只是选中了错的样本。手册 §12.4 因此设了**不可信字段黑名单**，直接禁止模型在这些字段上做筛选。

---

## 7. 第二层：Skill

### 7.1 两份派生文件

| 文件 | 语言 | 规模 | 消费方 |
|---|---|---|---|
| `skill/SKILL.md` | 英文全量 | 709 行 / 73 KB | 外部 MCP 客户端（`get_planning_guide` 返回） |
| `web/manual_compact.md` | 中文精简 | 369 行 / 37 KB | 网页服务（启动时内联进系统提示词） |

**派生关系：改一份必须同步另一份。** 内容对应，详略和语言不同。

分两份的原因：外部客户端需要可独立分发的自包含手册；网页服务把手册内联进系统提示词，省掉"调 `get_planning_guide` 取手册"这一整轮，既然要内联就要尽量短。

### 7.2 Skill 与代码的区别

Skill 不是代码，不被执行，而是读进模型上下文作为行动指南。

| | 代码规则 | Skill 手册 |
|---|---|---|
| 执行者 | CPU 机械执行 | 模型理解后判断 |
| 遇到未覆盖的情况 | 失败或走 else | 按手册精神外推 |
| 修改成本 | 改代码、测试、部署 | 改一行文字 |

**Skill 写给模型，不写给人。** 这决定了文风：只保留操作性内容，删掉说服性叙述。见 §7.6。

### 7.3 十二节的内容与作用

| 节 | 标题 | 作用 |
|---|---|---|
| §1 | Tools (10) | 十个工具的调用时机速查 |
| §2 | 图谱模型 | 节点属性、类型、已知坑 |
| §3 | 闭集工具目录 | 55 个工具清单 + 组装规则 |
| §4 | 查询配方 | 15 条官方 Cypher 模板 |
| §5 | 规划五步法 | 解析→匹配→组链→选数据→出 Plan |
| §6 | **效率纪律** | 轮数预算、并行发查询、何时必须停 |
| §7 | **接地纪律** | 名词白名单、禁止内部知识补全 |
| §8 | 拒绝纪律 | `off_topic` / `privacy` 两类 |
| §9 | 输出契约 | tool-chain/v2 字段定义 |
| §10 | 提交前把关 | 执行参数校验 |
| §11 | 边界与原则 | 隐私红线 |
| §12 | **实测快照** | 队列表、T2 产物清单、脏字段黑名单 |

### 7.4 §12 实测快照：用空间换轮数

篇幅最大的一节，把本可查数据库得到的事实直接写进手册：

```
| study_accession | tumor_type | sample nodes |
| HRA001272 | hepatocellular carcinoma | 698 |
| HRA016026 | lung cancer | 700 |
| HRA000074 | malignant glioma | 693 |
```

以及 T2 产物分布：

```
| DNA_VARIANT_VCF_GENERAL | 8310 | HRA000873(3045), HRA001272(1909), ... |
| MUTATION_ANNOTATION_FORMAT_MAF | 2355 | 仅七个队列 |
```

**理由**：查一次库不到 0.5 秒，但需要模型多想一轮（几秒到几十秒）。把高频、低变动的事实内联，模型开口即用。

**代价**：图谱换版本时这一节必须逐条重查。这个代价已写进 README，且 §12.1 建议做成自动检查。

### 7.5 §6 效率纪律与 §7 接地纪律

**效率纪律四条：**

1. **清单式开火**：每轮先列出还需要知道什么，把所有参数已知的查询在同一轮全部发出。6 条查询分 6 轮发比分 3 轮慢一倍。只有真正的串行依赖（必须先拿队列号才能查其文件）才允许分轮。
2. **标准轨迹三轮**：R1 从 §12 快照选工具和队列并发出必要查询 → R2 组装 Plan → R3 输出。拒绝类问题 1 轮、零工具调用。
3. **查过不再查**：同一契约、同一队列最多查一次。
4. **该停就停**：查空先检查关键词语言和目标标签，不许重发同一条失败查询。硬上限 6 轮；到顶仍分不出同族工具就选证据最强的，在 `match_note` 里说明歧义。

**接地纪律四条：**

1. **名词白名单**：答案里每个工具名、队列号、文件名、路径、格式名，必须逐字来自手册或本次会话的工具返回。
2. **禁止内部知识补全**：图里没有就输出 `missing_from_graph` / `no_candidate` / `unsupported`。哪怕"知道"DESeq2、Seurat 存在——不在 55 个闭集里就不许出现。
3. **证据可追溯**：`match_note` 对应真实查询结果；样本角色只来自 `resolve_sample_roles`；路径只来自图谱。
4. **输出前自检**：先 `hydrate_plan` 后 `validate_plan`，`grounded=false` 按 `violations` 修。

**为什么接地必须由代码执行**：模型编造时的置信度与说真话时相同，不会自报不确定。唯一可靠的办法是拿去和图谱逐条比对——这是确定性工作，不该寄望于"模型努力不编"。

### 7.6 精简原则

手册里有两类文字：

| 类型 | 例子 | 处理 |
|---|---|---|
| 操作性规则 | "T2 的 `format` 是小写扩展名，查语义要用 `semantic_format`" | 一字不删 |
| 说服性叙述 | "实测 29 个多轮例子里 13 个栽在这" | 删论据，留结论 |

后者是写给人的——向读者证明规则的来历。模型不需要被说服，只需要被告知。

已删：96 例分项统计、`validate_plan` 被连调 7–10 次的观察、pre-0821 孤儿计数、某案例推理 4 万字撞 token 上限。全部保留了对应的结论和禁令。队列默认选择由散文改表格，事实零丢失、篇幅减半。

**核对方式**：提取新旧版全部标识符（HRA*/文件名/反引号字段名）与 3 位以上数字，做集合差集，确认丢失项全部来自被删叙述。SKILL.md 726→709 行。

---

## 8. 第三层：十个 MCP 工具

| 类别 | 工具 |
|---|---|
| 取手册 | `get_planning_guide` |
| 查数据 | `read_cypher` / `read_cypher_batch` / `get_study_overview` / `resolve_sample_roles` |
| 校验 | `validate_atomic_chain` / `validate_execution_chain` / `validate_plan` |
| 补全与运维 | `hydrate_plan` / `health_check` |

### 8.1 `get_planning_guide()`

**作用**：返回 SKILL.md 全文（约 73 KB）。
**参数**：无。
**调用**：`{"name":"get_planning_guide","arguments":{}}`
**时机**：会话开始一次。网页服务不调（已内联），但服务端必须保留——其他客户端靠它取手册。

### 8.2 `read_cypher(query)`

**作用**：执行一条只读 Cypher。
**参数**：`query`（必需，string）。
**调用**：

```json
{"name":"read_cypher","arguments":{
  "query":"MATCH (s:study) WHERE toLower(s.tumor_type) CONTAINS 'liver' RETURN s.study_accession, s.sample_count"}}
```

**返回**：行数组；超 500 行截断并带 `truncated: true` 与 `row_count`。

**三重守卫**（实现见 `_assert_read_only` / `_assert_privacy` / `_assert_no_sensitive_payload`）：

**① 拒绝写入。**

```python
_WRITE_RE = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET\s|REMOVE|DROP|DETACH|FOREACH|LOAD\s+CSV)\b"
    r"|CALL\s+dbms\.|db\.create|apoc\.(?:load|export|cypher|trigger)", re.IGNORECASE)
```

**② 隐私守卫。** `individual` 上除 `00_*`（操作性标识）外，`01_`–`13_` 全部编号前缀均为患者级敏感数据：01_ 人口学、02_ 家族史、03_ 生活史、04_ 血液学、09_ 病理、10_ 侵犯、11_ 分子、12_ 治疗、13_ 生存。只允许聚合（`count`/`avg`/`min`/`max`）或存在性判断（`IS NOT NULL`）。

**按编号前缀区间判定，不按字段名枚举：**

```python
_SENSITIVE_PROP = r"`?(?<![\w])(?:0[1-9]|1[0-3])_\w+`?"
```

代码注释记录了这条设计的来历：

> 0821 实测：此前只列了 01/03/09/11/13，漏掉的 02/04/10/12 能直接查出个体级治疗方案（"HRI264436 → 3+7 regimen"）、脉管侵犯、家族史——覆盖范围必须按前缀区间取，不能靠手工枚举，否则上游一加编号就又漏一类。

还有一处细节：前缀必须锚定在属性名开头（`(?<![\w])`），否则 `04_platelet_count_109_l` 里的 `09_l` 会被误判为 09_ 病理属性，而 `04_` 本身反倒漏网。

守卫另防三种绕过：`properties()`/`keys()`、动态下标 `i['13_...']`、整节点 RETURN。别名会**追踪到不动点**（`collect(i) AS c`、`i AS z` 再 `z AS y`）。

**③ 结果面兜底。** 查询面正则只认识它见过的写法，换个等价写法就能绕过。所以另有一层检查**返回内容**：只要结果里出现患者级属性名——不管作为 map 的键还是被 `UNWIND keys(x)` 当值返回——整条拒绝。

**④ 自动限流。** 无 `LIMIT` 自动加 `LIMIT 500`。

守卫实测（11/11 通过）：

```
✓ 允许 00_ 点取 / count 聚合 / avg 敏感字段 / IS NOT NULL
✓ 拒绝 取个体生存值 / 按敏感值筛选 / properties() / 整节点 RETURN
✓ 拒绝 动态下标 / 写入语句 / 无标签别名导出
```

代码里对这层防护的定位很克制：

> 正则守卫是尽力而为的纵深防御层，主防线是调用方模型的拒绝纪律与部署信任边界。

### 8.3 `read_cypher_batch(queries)`

**作用**：一次调用执行多条**互相独立**的查询，结果按序返回。
**参数**：`queries`（必需，string 数组，≤ 8 条）。
**调用**：

```json
{"name":"read_cypher_batch","arguments":{"queries":[
  "MATCH (t:tool)-[:has_function]->(f:function) WHERE f.name CONTAINS '差异' RETURN t.tool_name",
  "MATCH (s:study) WHERE toLower(s.tumor_type) CONTAINS 'liver' RETURN s.study_accession"]}}
```

**时机**：凡不依赖上一条返回值的查询，全部打包。

**收益**：3 个独立问题分 3 轮问 = 3× 模型推理时间；打包 = 1× 推理 + 3×0.5 秒。功能相同，时间差数倍。

### 8.4 `get_study_overview(study)`

**作用**：一次返回队列的全部画像：study 信息、sample 节点数、T1/T2 格式与策略分布、T2 文件样例、样本角色分布、`role_resolved`、`file_coverage`。
**参数**：`study`（必需，string）。
**调用**：`{"name":"get_study_overview","arguments":{"study":"HRA001272"}}`
**时机**：选定队列后立即调。

**设计动机**：选定队列后模型通常连问三件事——队列什么情况、有哪些文件、样本能否分肿瘤/正常。三者**参数相同**（只需队列号）、**互不依赖**，但逐条问就是 3 轮。焊成一个工具后，模型连"要并行发"这个判断都不用做。

> 接口设计通则：当使用者总把 A、B、C 三个调用连用，就合成一个新接口。省的不是服务端计算，是调用方的决策成本与往返延迟。

### 8.5 `resolve_sample_roles(study | records)`

**作用**：确定性判定样本的 tumor/normal 角色。规则从重版移植，是代码不是模型判断。

**模式一（队列）**：

```json
{"name":"resolve_sample_roles","arguments":{"study":"HRA016026"}}
```
返回 `sample_roles`（全量统计）、`role_resolved`、`file_coverage`。`samples` 明细默认只回 20 条预览（`SAMPLE_PREVIEW=20`，上限 200），超出带 `samples_truncated`——**统计看 `sample_roles`，不要数 `samples` 数组**。

**模式二（记录）**：

```json
{"name":"resolve_sample_roles","arguments":{"records":[
  {"sample_accession":"HRS123456","tissue_type":"Tumor","sample_name":"L0012_Tumor"}]}}
```

**时机**：配对/分组分析、需逐样本挑文件时必须调。选队列级矩阵或 MAF **不需要**——那些流程自带分组逻辑。

**为什么不让模型自己判**：`tissue_type` 看似二值，实测为 `Tumor` 6258 / `Normal` 2821 / **null 829** / `Blood` 557。写 `= 'Tumor'` 会静默漏掉 1,386 个 null 和 Blood 样本；`specimen_type` 还有 486 个分号多值（`Organoid;Patient_Solid_Tissue`）。手册配套写死："样本角色只能来自 `resolve_sample_roles`，永远不许从文件名或直觉推断。"

**两张配套实测表**（已内联手册，模型不用查）：

- **可同个体配对的队列**：HRA000873 1015、HRA000021 508、HRA016026 350、HRA001272 206、HRA003107 155、HRA001749 84、HRA007169 76、HRA006499 72。
  陷阱：**HRA000071 的血液对照与肿瘤样本不属同一个体**——能分组，不能同个体配对。要现成配对优先 HRA016026（350 个体各 2 样本，`L####_Tumor`/`L####_Normal`）。
- **角色判不出的队列**：HRA000001（557 个全 Blood）、HRA000074（693 里 543 个缺 `tissue_type`）、HRA005191、HRA002693、HRA006117、HRA000122。
  **关键：这只卡"逐样本配对"。** 这些队列的队列级分析（差异表达、富集、聚类、免疫浸润、生存）照常可做——那些流程用汇总矩阵/MAF，内部自行分组。**不许因角色判不出就报 `no_candidate`。**

### 8.6 `validate_atomic_chain(chain)`

**两个概念**：

- **atomic tool（原子工具）**：单个软件，如 `bwa`/`gatk`/`star`，可串成链。闭集共 **11 个**：`bcftools`、`bwa`、`fastp`、`fastqc`、`featurecounts`、`gatk`、`rsem`、`samtools`、`snpeff`、`star`、`trim_galore`（加终端专用的 `multiqc` 共 12 张卡）。
- **pipeline（业务流程）**：打包好的完整分析，如 `diff_expr_go`，不参与串链。

**作用**：校验两件事——每个工具在闭集内；相邻两步在图里真有 `next_tool` 边。
**参数**：`chain`（必需，有序 tool_id 数组）。
**调用**：`{"name":"validate_atomic_chain","arguments":{"chain":["trim_galore","star","featurecounts"]}}`
**返回**：校验结果 + 规范化 `tool_chain`。**附加价值**：返回的是 Knowledge Card 的 `meta.id` 与卡内 IO 名——传 `star` 回来 `star_rrna_and_genome_alignment`，并告知输入槽为 `read1`/`read2`。这是它对模型可见的原因。
**时机**：链组装完调**一次**，不要边探索边调。

### 8.7 `validate_execution_chain(steps)`

**作用**：投给执行端之前的五阶段体检，并算出可直接下发的执行参数。

五阶段：① 注册校验 ② 卡契约必填输入 ③ 绑定结构 ④ 数据探查（文件在图里是否有确认路径）⑤ 链流转（上游产物能否喂下游）。

**参数**：`steps`（必需，`[{tool_id, inputs:{名: 绑定}}]`）。

```json
{"name":"validate_execution_chain","arguments":{"steps":[
 {"tool_id":"trim_galore","inputs":{
   "read1":{"file_name":"a_R1.fq.gz","file_path":"/data/a_R1.fq.gz"},
   "read2":{"file_name":"a_R2.fq.gz","file_path":"/data/a_R2.fq.gz"}}},
 {"tool_id":"star","inputs":{
   "read1":{"file_name":"a_R1_val_1.fq.gz","file_path":"/data/a_R1_val_1.fq.gz"},
   "read2":{"file_name":"a_R2_val_2.fq.gz","file_path":"/data/a_R2_val_2.fq.gz"}}}]}}
```

**返回**（`tool-chain-validation/v1.2`）：

| 字段 | 含义 |
|---|---|
| `validation` | 五阶段报告，含 `errors` |
| `execution_params` | 扁平视图 `{参数名: 真实路径}`，键为 Knowledge Card 参数名 |
| `execution_params_by_step` | **多步链以此为准** |
| `execution_params_ambiguous` | 跨步同名冲突的参数 |
| `execution_params_missing` | `{param, tool_id, step, reason}` 对象数组 |
| `submittable` | 能否提交 |

**判定：`errors` 清零且 `submittable=true`。**

**四个消费方必须知道的细节：**

**① `Array[File]` 参数的值是数组不是字符串。** `fastqc.fastqs`、`multiqc.qc_files` 类型为 `Array[File]+`。

**② 参考资源既不映射也不报缺。** 5 个带卡片默认值的参考/索引资源（执行端容器已有）：`star.rrna_star_index`、`star.genome_star_index`、`rsem.rsem_index`、`featurecounts.gtf_file`、`gatk.interval_list`。

**判定必须用显式白名单，不能用关键词猜。** 反例：

```
bcftools_somatic_postprocess   filtered_vcf_index   File   TBI   required=true
```

名字带 `index` 但**不是**参考资源——它是数据文件的 `.tbi` 伴随索引，无默认值，缺了跑不起来。重版按 "index" 关键词判，把这条必需绑定判成"既不映射也不报缺"，静默消失，0823 才修。因此 light 用 **`(卡片 id, 参数名)` 二元组白名单，正好 5 条**。

**③ 多步链看 `by_step`。** `trim_galore.read1` 与 `star.read1` 同名但指向不同文件（后者吃前者的产物）。扁平字典一键一值，处理方式是**把冲突键从扁平视图剔除**，放进 `ambiguous`，逐步值在 `by_step` 完整保留。**宁可缺，不给静默覆盖的错路径。** 手册配套写明："扁平视图里没有的参数不等于缺，去 `by_step` 取。"

**④ 回包 `tool_id` 是卡片 `meta.id`，不是入参的图谱 id。** 传 `star` 回 `star_rrna_and_genome_alignment`。**按 `step` 下标取，不要用 `tool_id` 字符串匹配请求。**

**一个"错得像对"的历史缺陷**：0823 前给它一条完全正确的 `fastqc` 单步链，返回

```
errors: []   execution_params: {}   params_missing: []   submittable: True
```

"零个参数、且一个都不缺"——签名是错的，但不报错、字段齐全、值都合理。消费方按 `not missing` 判可提交，就把一个参数都没解析出来的链送去执行。

根因：`fastqc` 输入类型是 `Array[File]+`，而三处代码用字符串精确相等判类型（`if type in ("File","Array[File]")`），带 `+` 后缀匹配不上，三处全漏，等于这张卡在参数解析里不存在。修法是抽出 `_base_type()`/`_is_file_type()`/`_is_array_type()` 统一调用。

> 最危险的缺陷不是崩溃，而是返回一份格式完美、字段齐全、看不出异常的错误答案。补测试时特意验证过每条新断言在改动前都会失败——能过的测试不等于能失败的测试。

### 8.8 `validate_plan(plan)`

**作用**：接地校验——工具是否在 55 个闭集目录、文件名是否在图里、路径是否为图谱记录、队列号是否真实。
**参数**：`plan`（必需，对象或字符串）。
**返回**：`grounded`（布尔）+ `violations`（违规清单）。
**时机**：输出终答前一次；`grounded=false` 修正后复验。手册限每会话最多 2 次。
**网页服务中对模型不可见**，服务端在终答后自动跑。

### 8.9 `hydrate_plan(plan)`

**作用**：把图谱和闭集目录本就知道的字段由服务端填上。

服务端填：`tool` 的 `catalog_id`/`tool_kind`/`name`/`description`/`inputs`/`outputs`；asset 的 `file_path`/`format`/`data_level`/各 accession；原子链卡内槽位；`match_id`/`rank`/`source`/`recommendation_count`/`candidate_count`；`planner_metadata`/`data_matcher_mode`/`mcp_timing_ms`。

模型只写判断性内容：`schema_version`、`selection_status`、`intent`、`pipeline_id`、`match_note`、asset 的 `file_name` + `match_reason`、candidates 链的步骤顺序。

**理由三条**：服务端本就知道；模型写了也会被图内事实覆盖；模型写 `file_path` 这类字段极易凭记忆编造。手册因此写死：这些字段一律不要生成。配套还有一条——**JSON 不缩进不美化**，缩进也是 token。

### 8.10 `health_check()`

**参数**：无。
**返回**：

```json
{"neo4j":{"status":"ok","nodes":81628,"tools":51,
 "atomic_closed_set":["bcftools","bwa","fastp","fastqc","featurecounts",
                      "gatk","rsem","samtools","snpeff","star","trim_galore"]},
 "model":"openai:deepseek-v4-flash","gemini_configured":false}
```

诊断用，不参与规划。

---

## 9. 如何保证 Cypher 写对，写错会怎样

这一章是本文档的重点。**Cypher 写错的绝大多数后果不是报错，而是静默返回错误结果**——这是整个系统里最难防的失败模式。

### 9.1 两类失败：报错 vs 静默

只有**语法错误**会真的报错：

```cypher
MATCH (n:T2 RETURN n
```
```
Neo.ClientError.Statement.SyntaxError:
Invalid input 'RETURN': expected a parameter, '&', ')', ':', 'WHERE', '{' or '|'
(line 1, column 13)
```

这类错误无害——它会被 `isError` 回灌给模型，模型下一轮改掉。

**危险的是语义错误**：语法完全合法，Neo4j 开开心心执行，返回 0 行或错误的行。以下全部为实测：

| 写法 | 结果 | 正确写法 | 结果 |
|---|---|---|---|
| `t.Format = 'bam'`（属性名大小写错） | **0** | `t.format = 'bam'` | 9,465 |
| `t.format CONTAINS 'BAM'`（值大小写错） | **0** | `t.format CONTAINS 'bam'` | 9,465 |
| `MATCH (n:Tool)`（标签大小写错） | **0** | `MATCH (n:tool)` | 51 |
| `-[:IN_SAMPLE]->`（关系类型大小写错） | **0** | `-[:in_sample]->` | 28,184 |
| `f.data_level = 1`（数值型误判） | **0** | `f.data_level = '1'` | 28,228 |
| `i.13_survival_days > 365` | **0** | `toInteger(...) > 365` | 2,465 |

**Neo4j 对不存在的属性、标签、关系类型一律按"没匹配上"处理，不报错。** 这是图数据库的 schema-optional 设计带来的必然结果。

### 9.2 后果为什么严重

对本系统而言，"0 行"不是一个中性结果，它会被解读成**业务结论**：

```
模型查 HRA000071 有没有 MAF 文件
  → 属性名写错 → 返回 0 行
  → 模型判断"该队列无 MAF"
  → 输出 selection_status: "no_candidate"
  → 用户得到"做不了 Oncoplot"
```

而实际上 HRA000071 是**唯一有 MAF 的胶质瘤队列**（`HRA000071-SomaticSNV-1.0.maf`）。手册 §12 为此专门写了一条："Oncoplot 请求必须解析到这里，**不得答 `no_candidate`**。"

**一次静默的零行，等价于一次自信的错误答案。**

更隐蔽的是"返回了行但行是错的"。词典序比较就是典型：

```
13_survival_days > '365'  → 2110 行
13_survival_days > '99'   → 27 行      ← 阈值调低反而更少
toInteger(...) > 365      → 2465 行
toInteger(...) > 99       → 3164 行
```

文本比较里 `'99' > '365'`（首字符 `9` > `3`），所以"生存超过 99 天"的人比"超过 365 天"的还少。结果非空、格式正常，只是完全错误。`ORDER BY` 不转换同样如此：字符串降序的前五是 `'995','995','994','990','990'`，而真实最大值是 **7061**。

### 9.3 六道防线

**防线一：手册内联 schema，让模型不必猜。**

§2 逐标签列出属性名、类型、非空计数。属性名一律用反引号原样给出，并明确警告大小写敏感：

> **Copy property names with exact case** — writing `t1_id` as `T1_id` does not error, it silently returns 0 rows.

**防线二：15 条官方 Cypher 模板，让模型抄而不是编。**

`skill/references/query_templates/` 下 15 条模板全部可直接运行，覆盖工具查询、数据查询、链路追溯、配对发现。手册 §4 要求按名引用。抄模板的正确率远高于现编。

**防线三：模板审计脚本，检测"整列 null"。**

`benchmark/template_audit.py` 逐条实跑 15 条模板，断言两件事：**返回行数 > 0**，且**没有整列为 null**。

后一条正是针对静默失败设计的——属性名写错时查询照样返回行，只是那一列全是 null。

**这次审计抓到一个真实的 bug。** `find_paired_tumor_normal_samples.cypher` 写的是：

```cypher
RETURN i.individual_accession AS individual
```

审计报告：

```
FAIL find_paired_tumor_normal_samples.cypher:
     rows=50 但整列为 null：individual——属性名写错或该字段在此标签上不存在
```

实测 `individual` 节点上的属性键是：

```
['00_strategy', '00_individual_accession', '00_individual_name',
 '00_project_accession', '00_sample_accession', '00_study_accession',
 '00_population_type']
```

**id 叫 `00_individual_accession`，不叫 `individual_accession`。** 而 `T1`/`T2` 文件节点上**确实**有裸的 `individual_accession`（28,184 个非空）——两个标签命名不一致，最容易互相套用。

这条模板返回 50 行、`pairable` 列也算得对，只有个体编号那列全是 null。不跑审计根本看不出来。已修正为 ``i.`00_individual_accession` ``，15/15 全部通过。

连带修了同一个错误名的另外两处：手册 §2 的 `individual` 属性说明，以及**隐私守卫的报错文案**——它在拒绝整节点导出时会建议"请显式点取非临床字段（如 individual_accession）"，照做会得到一整列 null。已改为 `` `00_individual_accession` ``。

**防线四：`valueType()` 核对字段类型。**

不要相信字段"看起来像数字"。实测 0821 图谱：

| 字段 | valueType | 非空数 |
|---|---|---|
| `T1.data_level` / `T2.data_level` | **STRING** | 28,229 / 35,572 |
| `T1.size` / `T2.size` | **STRING** | 25,417 / 35,572 |
| `individual.01_age` | **STRING** | 5,845 |
| `individual.11_tmb` / `11_msi_score` | **STRING** | 1,523 / 508 |
| `individual.13_survival_days` | **STRING** | 4,441 |
| `individual.13_dfs_time` / `13_efs_time` / `13_pfs_time` | **STRING** | 761 / 1,012 / 675 |
| `study.sample_count` / `individual_count` | INTEGER | 14 / 14 |

**只有 study 上的两个计数是真 INTEGER，其余全是 STRING。**

**这次核对推翻了手册里的一条错误指令。** 精简前两份手册都写着：

> **数值字段不加引号、不用 toInteger**（0821 已改 INTEGER/FLOAT）：`data_level`/`size`/`sample_count`/…/`13_*`。写 `i.13_survival_days > 365`，写 `> '365'` 静默查不到。

事实与之相反——手册给出的示例查询本身返回 **0 行**：

```
f.data_level = 1              → 0        f.data_level = '1'        → 28228
i.13_survival_days > 365      → 0        toInteger(...) > 365      → 2465
```

即手册在教模型写一条必然查空的查询，还附带一句"别用 toInteger"。两份手册均已按实测改写，并补上词典序陷阱的具体数字。

> 这类错误比缺失文档更有害：缺失时模型会去查，写错时模型会照做。

**防线五：服务端守卫拦截非法查询。** 见 §8.2。写入语句、患者级个体取值、整节点导出会被直接拒绝并返回可读原因。

**防线六：接地校验兜住最终结果。** 即使查询全对，Plan 里的文件名和路径仍要过 `validate_plan` 与图谱逐条比对。这是最后一道。

### 9.4 错误如何回到模型

工具报错不走 JSON-RPC error，而是 `isError: true` 的正常返回（§4.4），编排层转成 `{"error": "..."}` 回灌进对话历史。模型看到的是可读中文：

```
read_cypher 隐私守卫：`13_survival_days` 是患者级临床属性（01_人口学/02_家族史/
03_生活史/04_血液学/09_病理/10_侵犯/11_分子指标/12_治疗史/13_生存），只允许聚合
统计（count/avg/min/max…）或存在性判断（IS NOT NULL），不允许返回或按值筛选个体
数据。请改写为聚合查询，或直接拒绝用户的隐私问询。
```

**错误信息的写法直接影响下一轮质量**：不只说"被拒绝"，而是说明**为什么**、**允许什么**、**下一步怎么做**。§5.3 那个泄漏解析的例子从反面印证了同一点——服务端发出误导性报错时，模型会被带向更坏的结果。

### 9.5 手册里的具体反坑规则

| 坑 | 规则 |
|---|---|
| T1 查不到 BAM | T1 只有 FASTQ；BAM/VCF/MAF/矩阵全在 T2 |
| `format CONTAINS 'BAM'` 空 | `format` 是小写扩展名（`bam`/`vcf.gz`/`maf`）；语义名在 `semantic_format`（`DNA_ALIGNMENT_BQSR_BAM`） |
| `datalevel` 节点查不到 `data_level` | 节点属性是 `level`/`name`/`description`；文件侧才叫 `data_level` |
| T2 查不到 `sample_accession` | T2 无该属性；样本归属走 `(T2)-[:generated_from]->(T1)-[:in_sample]->(sample)` |
| 中英文关键词 | `tumor_type` 是英文（`toLower` + `CONTAINS`），`function.name` 是中文 |
| `LIMIT` 不定序 | 取代表文件必须 `ORDER BY n.file_name`，否则同一问题在不同运行解析到不同文件 |

**查空之后的纪律**（手册 §6）：先检查关键词语言和目标标签，**不许重发同一条失败查询**。这既是效率要求，也是正确性要求——重发不会得到不同结果，只会烧掉一轮预算。

---

## 10. Benchmark

### 10.1 96 例测试集：工具选择 96/96

接手时的 96 例测试集，light 架构的成绩（`benchmark/bench_light_96_report.json`）：

| 指标 | 数值 | 说明 |
|---|---|---|
| **工具 top-1** | **96 / 96 = 100%** | 第一推荐即命中期望工具 |
| 工具 top-3 | 96 / 96 = 100% | — |
| 数据面全中 | 90 / 96 = 93.8% | 一例的全部期望文件都在图里且匹配上 |
| 期望文件命中 | 174 / 186 = 93.5% | 精确命中 138（74.2%），宽匹配 36（19.4%） |
| **工具+数据同时正确** | **90 / 96 = 93.8%** | 综合口径 |

**工具选择满分。** 6 例数据面未全中（q052–057）期望的是 demo 文件（`NVM0598_*`、`ENCSR142YZV_chr19only_*`），**这些文件不在 0821 图谱里**——按接地纪律以图为准，如实报 `missing_from_graph`，不编造路径。这是设计行为，不是缺陷。

复跑：

```bash
python3 benchmark/bench_light_96.py
```

### 10.2 其他口径

同一份结果按不同口径统计（`benchmark/data/three_arms_results.json`，67 例子样本）：

| 口径 | 数值 | 含义 |
|---|---|---|
| 结构化子集内准确率 | **68.6%**（24/35） | 模型输出合规 JSON 时的工具选择质量 |
| 格式合规率 | 52%（35/67） | 最终答案含可解析的 tool-chain/v2 JSON |

这两个数与 §10.1 的 96/96 不冲突，因为**统计对象不同**：§10.1 看的是最终解析出的工具是否命中，这里看的是模型自己有没有按契约输出。

**格式合规率 52% 是本项目最重要的一个测量结果，它直接决定了架构形态：**

即使系统提示词强制"输出单个 JSON"，模型仍有近半写散文。**所以校验与补全层必须存在，且必须在服务端。** 不能指望提示词写得够严——这就是 `hydrate_plan` + `validate_plan` 由服务端在终答后自动代跑（§5.5）、而非交给模型自觉调用的根本原因。

前端接入因此要求三道断言：① `schema_version: "tool-chain/v2"`；② `validate_plan` 返回 `grounded=true`；③ 提交前 `submittable=true`。失败回灌重试 2–3 次，仍失败如实报错。

### 10.3 已知的工具族歧义

有 11 例（在 67 例子样本口径下）属于选了"合理但非期望"的工具，其中大半是**题库单解 vs 图谱多解**的标注问题：

```
survival_analysis  → 选了 tmb_survival_analysis   (2)
her2_pfs_survival  → 选了 km_survival             (2)
rnaseq_singletask  → 选了 fastp-star-featurecounts     ← 后者就是前者的原子链展开
```

`km_survival` / `cox_model` / `survival_analysis` / `tmb_survival_analysis` / `her2_pfs_survival` 五者意图高度重叠，题库只允许一个答案。

该发现推动手册 §12.1 增加同族判别规则：`survival_analysis` 按**指定基因突变状态**分组、`tmb_survival_analysis` 按 **TMB 中位数**分组、按**基因表达水平**分组的是 `her2_pfs_survival`。

### 10.4 24 例回归门禁

日常改动的快速门禁：24 个代表性问题，8 并行。

**标准**：通过率不许掉、均时应下降、轨迹必须落盘（`web/tests/trajectories_<时间戳>/`）。

本轮两次改动（手册精简、修正过时事实）的全部实测：

| 运行 | 通过 | mean | p50 | p90 | p99 | max | >30s |
|---|---|---|---|---|---|---|---|
| 精简前基线 | 24/24 | 7.1 | 5.8 | 16.7 | 21.5 | 21.5 | 0 |
| 精简后 #1 | 24/24 | 7.9 | 6.9 | 17.7 | 36.5 | 36.5 | 1 |
| 精简后 #2 | 24/24 | 7.1 | 6.8 | 17.7 | 22.7 | 22.7 | 0 |
| 精简后 #3 | 24/24 | 6.9 | 5.9 | 15.2 | 17.9 | 17.9 | 0 |
| 修正后 #1 | 24/24 | 8.2 | 5.2 | 18.9 | 41.5 | 41.5 | 1 |
| 修正后 #2 | 24/24 | 7.0 | 6.1 | 15.5 | 27.3 | 27.3 | 0 |
| 修正后 #3 | 24/24 | 6.6 | 6.4 | **13.6** | 21.0 | 21.0 | 0 |
| **修正后合并 n=72** | **72/72** | 7.3 | 6.1 | **14.6** | 27.3 | 41.5 | 1 |
| 类型/属性名修正后 #1 | 24/24 | 8.3 | 5.2 | 17.3 | 21.8 | **21.8** | **0** |
| 类型/属性名修正后 #2 | 24/24 | 13.1 | 9.0 | 28.4 | 70.3 | 70.3 | 2 |
| 类型/属性名修正后 #3 | 22/24 | 12.8 | 9.5 | 30.7 | 62.4 | 62.4 | 3 |

**最后两次运行不计入评估**：模型端点账户余额耗尽，中途开始返回 `HTTP 402 Insufficient Balance`。用一条最小请求（`max_tokens: 1`）直接探测端点，同样返回 402，确认是账户问题而非本项目改动。第 3 次的两个 FAIL 全部是 `err=模型接口 HTTP 402`、`fmt=empty`；第 2 次的 70.3 秒是端点在余额耗尽前的降级重试。**第 1 次是余额耗尽前唯一完整的运行**：24/24，max 21.8 秒，**零例越过 30 秒**——这是本轮改动能拿到的唯一有效测量。

**诚实读法：**

- 有效样本只有 n=24（第 1 次）。**这不足以对均时下结论**，只能说通过率没掉、且该次无一例破 30 秒。
- 待账户充值后需补跑两次，才能与"修正后合并 n=72"的 p90 14.6 秒作可比对照。
- 不受模型端点影响的三项确定性测试全部重跑并通过（见 §10.5），改动的正确性有独立证据。

**此前两轮改动（手册精简、事实修正）的读法仍然成立：**

- 通过率 **144/144**，跨两轮改动没掉。
- 合并 p90 **14.6 秒**，优于修正前任何单次（基线 16.7）。
- 均时在 6.6–8.2 秒间波动，基线 7.1。**结论是持平，不是下降。** 两轮改动合计只动了系统提示词约 0.5%，本不应产生可测加速；收益在正确性与可维护性。
- **仍有一例会越过 30 秒**：q08 三次跑出 41.5 / 27.3 / 13.6 秒，方差极大。慢的那次走 4 轮且 `validate_atomic_chain` 被调两次（手册要求一次）。这是模型未守纪律，不是新引入的退化（修正前最慢的也是 q08）。列为长尾首要目标。

> n=24 的单次测量噪声可能大于效应。两轮改动各跑三遍才下结论，且下的都是"持平"。

### 10.5 其他测试

| 测试 | 内容 | 现状 |
|---|---|---|
| `benchmark/system_test.py` | 12 场景 101 断言 | **101/101 全部通过** ✅ |
| `benchmark/template_audit.py` | 15 条模板逐条实跑，断言有行且无整列 null | **15/15** ✅（修 `00_individual_accession` 后） |
| `integration_test.py` | 集成测试 | 28 PASS / 7 FAIL，7 条全是 0821 数据问题 |
| 守卫用例 | 隐私/只读守卫 11 例 | **11/11** ✅ |

这三项**不经过模型端点**，纯查图谱与本地逻辑，因此不受本轮 402 影响，是改动正确性的独立证据。

**`system_test.py` 这次抓到的一条，恰好是本章主题的又一个实例。** 断言「`04_platelet_count_109_l` 聚合放行」失败，报的却不是守卫误伤：

```
FAIL 敏感前缀零误伤「04_platelet_count_109_l 聚合放行」
     [{'status':'error','detail':'AVG(NodeProperty(0,280)) can only handle
       numerical values, duration, or null, but received: String'}]
```

守卫其实**放行了**——查询被送到了 Neo4j，是 Neo4j 拒绝对 STRING 做 `avg()`。实测该字段 974 个非空值全是 `STRING NOT NULL`。测试用例本身带着和手册同一个错误假设（§9.4），写的是 `avg(i.\`04_platelet_count_109_l\`)`。改成 `avg(toFloat(...))` 后返回 66.45，断言通过。

**换言之：同一个"数值字段其实是字符串"的错误假设，同时存在于手册、集成测试和系统测试三处。** 这正是 §12.1 把手册新鲜度检查排在首位的理由——错误假设不会自己暴露，它会被抄进每一个下游。

**`integration_test.py` 的一个教训**：这套测试曾崩在第 109 行——`13_survival_days` 是字符串，`str > int` 抛 TypeError——**第 4 段之后的十几条断言从未被执行过**。加 `float()` 兜底后套件能跑到底，报出 7 个失败，**其中 5 条是首次暴露**。

> 一个从未跑完的测试套件比没有测试更危险：它提供虚假的安全感。

（同源关系：那次崩溃、system_test 的这次失败、以及手册里写反的整段类型指令，根因都是 `13_*`/`04_*` 等字段的实际存储类型是 STRING。崩溃和报错暴露了代码侧的假设，手册侧的同一假设一直没人验，直到这次逐条核对。）

---

## 11. 与原架构的对比

| 维度 | 重版 | light | 性质 |
|---|---|---|---|
| 体积 | 205 MB | ~110 KB | 约 1800 倍 |
| 依赖 | 多个第三方包 | 零第三方依赖 | 部署从配环境变为拷文件 |
| 推理位置 | 服务端硬编码（132 KB `workflow_composer.py`） | 调用方模型 + 手册 | 改规则从改代码变为改文本 |
| Plan 来源 | 一次调用 `route_pipeline_request` | 接口已删除，模型产出 | 无静默降级路径 |
| 降级行为 | 模型不可用时退回内置词表 | 无降级，要么模型在环要么如实报错 | 不以词表输出冒充正常结果 |
| 样本角色 | 内置推断 | `resolve_sample_roles` 双模式工具 | 规则抽成可复用工具 |
| 提交判定 | 有 `execution_params`/`submittable` | 对齐到 v1.2 | 增 `by_step`/`ambiguous`/结构化 `missing` |
| 拒绝无关问题 | 内置词表相关性门 | 手册 §8 拒绝纪律 | 模型理解意图 |
| 患者隐私 | 无专门防护 | 三重守卫（前缀判定 + 结果面兜底） | 从无到有 |
| 响应时间 | 内嵌 LLM 超时 180 秒 | mean ≈ 7 秒，p90 ≈ 14.6 秒 | 一个数量级 |
| 工具 top-1（96 例） | 100% | **100%（96/96）** | 打平 |
| 数据面（96 例） | — | **93.8%**（工具+数据同时正确） | 新增接地校验 |
| 回归手段 | 96 例一套 | 96 例 + 24 例门禁 + 模板审计 + 101 断言 | 多层门禁 |

**工具替代对照：**

| 重版 | light |
|---|---|
| `health_check` | `health_check`（1:1） |
| `query_data_availability` | `read_cypher` + 15 条模板（1:1） |
| `list_pipeline_capabilities` / `list_workflow_methods` | `read_cypher` + `tool_catalog.csv` |
| `render_pipeline_answer` | 模型天生能力（零成本删除） |
| `route_pipeline_request`（业务推荐） | Skill 五步流程（96/96 打平） |
| `route_pipeline_request`（原子候选）/ `validate_tool_chain` | `validate_atomic_chain` |

砍掉：MCP 内嵌 LLM 调用、双数据匹配器、113 个 CSV、157 个 audit 文件、16 个 demo cassettes。

**README 里的自我评价：**

> 205MB 重 MCP → 110KB。96 例上工具选择与重服务打平（均为 100%）——**该任务的工具选择本身是平凡的，重架构从未在此挣到收益**。

真正的价值是：用 1/1800 的体积拿到同样的 96/96；额外拿到 93.8% 的数据面接地（重版没有这一层）；把响应时间从 180 秒超时压到 p90 14.6 秒；以及从无到有的患者隐私三重守卫。

**六条可复用的工程判据：**

1. **显式白名单优于关键词启发式。** 参考资源用 `(卡片 id, 参数名)` 二元组列举，因为 `bcftools.filtered_vcf_index` 名字带 index 却不是参考资源。
2. **宁可缺，不给静默覆盖的错值。** 跨步同名参数冲突时剔除并列入 `ambiguous`。
3. **按结构判定，不按名字判定。** 隐私守卫按 `01_`–`13_` 前缀区间拦，上游新增编号列自动被覆盖。
4. **有信息产出的工具给模型，纯闸门型服务端代跑。**
5. **能确定性算出的绝不让模型算。** 样本角色、文件路径、闭集成员资格全是代码的活。
6. **测试要能失败。** 补断言时验证过它们在改动前确实失败。

---

## 12. 后续优化方向

### 12.1 近期

**手册新鲜度检查排第一，因为这次连抓两类错。**

写本文档时逐条核对图谱，发现两处手册与实测不符：

- **§2 的 `tissue_type` 分布是 pre-0821 快照**：手册写 Tumor 5469 / Normal 2469 / null 1270 / 多值 `Tumor,Normal` 700 / Blood 557；实测为 Tumor 6258 / Normal 2821 / null 829 / Blood 557，**多值单元 0 个**。而同一文件的 §12.4 写着"829 个为空"，与实测一致——**同一份手册两节自相矛盾，且长期无人发现**。
- **数值字段类型整段写反**（§9.4）：手册称 0821 已改 INTEGER/FLOAT 并要求不加引号、不用 `toInteger`，实测 13 个字段里 11 个是 STRING，手册给的示例查询本身返回 0 行。

加上模板里的 `individual_accession`（§9.3），三处都属于同一类：**不报错、只让结果安静地错**。

| 项 | 现状 | 建议 |
|---|---|---|
| **手册新鲜度检查** | 靠人记得；本轮抓到三处漏网 | 写 `check_manual_freshness.py`：把手册里的实测数字与属性名逐条回查图谱，不符即报警。**优先级最高** |
| q08 长尾 | 41.5 / 27.3 / 13.6 秒，慢的一次走 4 轮且 `validate_atomic_chain` 调两次 | 唯一还会破 30 秒的用例。先看轨迹确认是否"改链后重验"，是则在手册补"允许复验一次但须与其他查询同轮发" |
| `integration_test.py` 7 个失败 | 0821 数据问题（字符串型数值、format 大小写孤儿、`00_sample_accession` 与边错位 349 个） | 逐条与交付方对齐，部分需上游修数据 |
| `io_slot.csv` 的 `required` 反了 | gatk 四个 paired BAM/BAI 槽表内为 false、卡片为 true；`interval_list` 相反 | 不影响生产（走卡片不走槽表），但随 skill 分发，建议上游核对 WDL 重建 |
| `light_router.load_slots()` | 键不含 direction，同名输入/输出行相撞（17 处） | 在已下线的离线对照臂，按交接文档不动，记录在案 |
| `QUERY_ROUND_BUDGET` | 默认 3，未做 A/B | 试 2，用 24 例门禁验证 |

### 12.2 中期

**扩大测试覆盖。** 96 例只覆盖部分工具，55 个闭集工具里有相当一部分从未被任何用例考过。补齐才能看清哪些工具族真的分不开。

**解决题库单解 vs 图谱多解。** 两条路：改评分（用 `has_function` 关系自动生成等价类，允许一题多解）或改图谱（补同族判别性描述）。后者已做一轮，可量化验证效果。

**攻格式合规率。** 52% 太低。**注意排除项**：`response_format: json_object` 和单纯在提示词里加输出格式约束都已试过，无效。可行方向是服务端加强"散文→JSON"抢救解析（现有 `stripped_prose_or_fence` 兜底，可先统计救回率）。

**长尾治理。** p99 目前 21–27 秒。轨迹显示慢例普遍是多轮取数。可统计哪些问题类型稳定需要 3 轮以上，考虑像 `get_study_overview` 那样再合并一个聚合接口。

### 12.3 远期：从图谱内 55 个工具到全量生信工具

**现状边界**：闭集 55 个工具（11 原子 + 42 pipeline + 1 task_pipeline），都在图里、都有格式契约和 `next_tool` 边。可靠性绝对，但天花板低——用户问 ATAC-seq、Seurat 单细胞聚类、nf-core RNA-seq，只能回 `unsupported`。

**生态规模参照**：Bioconda 9,000+ 包、nf-core 100+ 流程、Galaxy ToolShed 8,000+ 工具、Bioconductor 2,300+ R 包、bio.tools 约 30,000 条注册记录。当前覆盖 55 个。

**路径 A：扩闭集（保守）。** 每接一个工具手写 Knowledge Card、加图节点和边。可靠性不变，但不可扩展——`next_tool` 边是 O(n²)，人工维护到 500 个工具不现实。适合核心高频工具。

**路径 B：分层可信度（推荐）。** 把二元的"闭集内/外"改为三层：

```
L1  核心闭集（现 51 个）
    有卡片、有图边、有格式契约
    → 可进 candidates[]，可做执行参数解析，submittable 可为 true
L2  已注册工具（从 bio.tools / Bioconda 元数据自动导入）
    有名称、描述、IO 格式声明，未经人工核验
    → 可进 recommendations[]，标记 tier:"registered"
    → submittable 一律 false，必须人工确认
L3  模型建议（模型自身知识，图谱不知道）
    → 只进新字段 suggestions[]，明确标注未经核实
    → 绝不进 recommendations[]，绝不给路径
```

**接地纪律不放松，只是分层。** 现规则是"不在闭集就不许提"，改为"可以提，但必须带可信度标签且不给执行参数"。这样用户问 ATAC-seq 能得到"图谱无现成流程，但 bio.tools 注册了 MACS2 / Genrich，属 L2 建议，需自行确认"，而非一句 `unsupported`；同时绝不会把未核验工具当成可执行方案。

工程量：一个 bio.tools/Bioconda 元数据导入器（两者都有开放 API 和结构化元数据）、新增 `tier` 字段、Plan 契约升 v3、手册加一节分层规则。**架构不需推翻。**

**路径 C：自动构建 Knowledge Card（终局）。**

现有卡片是人写的 YAML。但重版 0823 那轮的槽表**就是从 WDL 自动重建的**（commit `58f6be6`，246 行 19 列）。

**WDL 是机器可读的**：`task` 定义里明确写着每个参数的名称、类型（`File`/`Array[File]+`/`File?`/`String`）、是否可选。同理适用于 nf-core 的 `nextflow_schema.json`（严格 JSON Schema）、Galaxy 的 XML `<inputs>`/`<outputs>`、CWL 的 `CommandLineTool`、Snakemake 的 rule。

```
WDL / nextflow_schema.json / Galaxy XML / CWL
   ↓ 解析器（每种格式一个，确定性代码）
Knowledge Card（自动生成）
   ↓ 格式契约推断
图谱节点 + input/output 边
   ↓ 格式匹配：A 的 output ∩ B 的 input ≠ ∅
next_tool 边（自动推导）
```

**最后一步是关键**：`next_tool` 现由人工维护，但它本质就是上下游格式的交集非空。两边格式声明都自动提取后，这条边可以算出来。跨过这一步，天花板从"人能写多少卡"变为"社区发布了多少条流程定义"。

**风险**：自动提取的契约质量参差（部分工具参数描述很差）；格式命名不统一（`fastq` / `FASTQ_GZ` / `reads`），**需要一层格式本体映射**，本身是不小的工程；自动推导的边有假阳性（格式对得上不代表生物学上该这么接）。

**因此务实做法：自动提取 → 进 L2 → 人工审核后升 L1。B 和 C 配套，不是二选一。**

**顺带解决两个老问题：**

- **图谱构建自动化**：现由 31 个 CSV 经 `load_graph.cypher` 灌入，CSV 上游手工产出。契约可自动提取后，工具子图（tool/function/format/next_tool）可自动生成，人只维护数据子图。
- **版本漂移**：`skill/references/` 下三个文件是上游快照，会漂移（本轮同步 `io_slot.csv` 时曾误把一个未版本化的 pre-0823 副本当成真值）。契约能从上游源文件重新生成后，漂移在源头消失。

### 12.4 更远：跳出规划边界

**接执行端闭环。** `validate_execution_chain` 已能算出可下发的 `execution_params`，差投递与追踪。接 Cromwell 或 Nextflow 即可从"这是方案"变为"已在运行，进度 30%"。

**结果回灌图谱。** 分析产出的新 T2 若能自动回灌，图会自我生长，下次同类问题可答"该分析已有人跑过，结果在此，可直接复用"。手册已有复用纪律（"T2 有现成 VCF/MAF/BAM 就标复用、跳过上游"），只是当前 T2 是静态的。

**多轮交互式规划。** 现为一问一答。真实场景常是"先看有哪些数据 → 这个队列不错 → 用它做差异表达 → 再加富集"，需要跨轮上下文管理与方案增量修改。

**方案对比。** 同一需求给 2–3 个方案并标注数据要求、预期产出、计算量。现为严格 top-1（只推一个），但对真实用户未必最优——尤其在 §10.3 那类同族工具歧义上，并列两个候选比强行选一个更诚实。

---

## 13. 名词速查表

| 名词 | 含义 |
|---|---|
| **MCP** | Model Context Protocol，模型调用外部工具的开放标准。本项目用 stdio 传输 + JSON-RPC 2.0 |
| **JSON-RPC 2.0** | 轻量远程调用协议。请求含 `method`/`params`/`id`，响应含 `result` 或 `error` |
| **stdio 传输** | MCP server 作为子进程运行，经标准输入输出通信，无网络监听 |
| **inputSchema** | 工具参数的 JSON Schema。模型据此生成调用参数 |
| **isError** | MCP 约定：工具业务失败返回带此标记的正常 result（而非 JSON-RPC error），以便回灌给模型自行修正 |
| **function calling** | 模型输出结构化工具调用请求而非文本的能力。`arguments` 是字符串化 JSON |
| **tool_call_id** | 工具结果与调用请求的关联键，必须一致 |
| **Agent** | 模型在循环中自主决定下一步动作，而非单次问答 |
| **round / 轮** | 一次完整模型推理。**本系统唯一的时间成本**（几秒~几十秒） |
| **Skill** | 写给模型的操作手册，读进上下文作为行动指南，不是被执行的代码 |
| **Neo4j / Cypher** | 图数据库及其查询语言 |
| **schema-optional** | 图数据库不强制预定义属性。**代价是属性名写错不报错，只返回零行或整列 null** |
| **valueType()** | Cypher 函数，返回属性的实际存储类型。核对"看着像数字"的字段必用 |
| **接地 / grounding** | 答案中每个具体名词都能追溯到真实来源 |
| **幻觉** | 模型编造看似合理实则不存在的内容，置信度与说真话时相同 |
| **闭集** | 允许出现的工具是固定有限集合。本项目 51 个，其中 11 个原子工具可编排 |
| **atomic tool / pipeline** | 单个软件（可串链）/ 打包流程（不串链） |
| **Knowledge Card** | 描述原子工具接口契约的结构化文件：参数名、类型、必需性、格式 |
| **tool-chain/v2** | 系统输出契约的 schema 版本 |
| **T1 / T2** | T1 = 原始 FASTQ 及临床/元信息表；T2 = 一切加工产物（BAM/VCF/MAF/矩阵） |
| **HRA\*\*\*\*\*\*** | 队列编号，如 HRA001272 = 肝癌 698 样本 |
| **MAF** | Mutation Annotation Format。全图仅 7 个队列有 |
| **严格 top-1** | 只看 `recommendations[0]` 是否命中期望工具，不看后续候选 |
| **p90 / p99** | 百分位延迟，长尾优化的核心指标 |
| **WDL / CWL / Nextflow** | 机器可读的工作流描述语言，自动化扩展的入口 |

---

*基于 2026-08-24 仓库状态。图谱数字取自 0821 交付并经 `valueType()` 与 count 实测复核；benchmark 数字取自 `benchmark/` 报告；回归数字取自 `web/tests/trajectories_20260824_*`。数据换版本后第 6、9、10 章的具体数字需重新核对。*
