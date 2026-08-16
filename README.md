# Bio Pipeline Light（轻架构生信链路规划）

用 **DSH 原生 MCP + 一个 Skill** 替代重 MCP（[bio-pipeline-kg-matcher](https://github.com/zyr3210115527/bio-pipeline-kg-matcher)）的生信工具链规划能力：**推理交给调用方的模型，MCP 只给知识与校验**。

> **本仓库的真实论点**：205MB 重 MCP → 110KB（skill + read-cypher）。96 例上，一张 60 行关键词表即可与重服务打平——**该任务的工具选择本身是平凡的，重架构从未在此挣到收益**。真正需要模型智能的是"去名"的意图理解（见三臂评测）。

```
轻架构 = skill（bio-pipeline-planning，推理手册）
       + MCP（get_planning_guide / read_cypher / validate_atomic_chain）
       + benchmark/（污染度检测、三臂评测、去名集、真实模型测试）
```

## 与重 MCP 的对照

| 重 MCP 工具 | 轻架构替代 | 说明 |
|---|---|---|
| `health_check` | bash + `RETURN 1` | 直接替代 |
| `query_data_availability` | `read-cypher` + 15 条官方模板 | 1:1 替代（模板见 `skill/references/query_templates/`） |
| `list_pipeline_capabilities` / `list_workflow_methods` | `read-cypher` + `tool_catalog.csv` | 目录查询 |
| `render_pipeline_answer` | 模型天生能力 | 零成本 |
| `route_pipeline_request`（业务推荐 + 数据证据） | Skill 5 步流程 + read-cypher | ✅ 覆盖（96/96 打平） |
| `route_pipeline_request`（原子候选）/ `validate_tool_chain` | 规则近似 | ⚠️ 确定性闭集校验需薄工具（`workflow_composer` 核心），见"边界" |

砍掉的部分：MCP 内嵌 LLM 调用（180s 超时/16k 输出）、双数据匹配器（CSV+Neo4j 对比）、113 个 CSV、157 个 audit 文件、16 个 demo cassettes。

## 评测方法论（先测污染，再上三臂）

**改动一：污染度检测。** 96 例测试集中，**96/96 (100%) 的问题文本直接包含答案触发词**（78 词关键词表逐条命中）。在这套集子上拿 100% 不说明任何事——它就是"答案泄漏集"。

**改动二：三臂评测**（`benchmark/bench_three_arms.py`）：

| 臂 | 是什么 | 原集 96 | 去名集 70 |
|---|---|---|---|
| floor | 永远猜最高频工具 | ~12.5% | **7.1%**（随机线） |
| ceiling | 60 行关键词表（本仓库 RULES） | **100%** | **1.4%**（词表一拆就碎） |
| SUT（严格 top-1，结构化） | skill + read-cypher + 真实模型 | — | **35.8%（67 例样本）** |

**评分口径演进（诚实记录）**：最初"87.1%"是**子串匹配**（expected 出现在全文即算）——已弃用，存在 `survival_analysis ⊂ tmb_survival_analysis` 误判与"提到即命中"送分。改为与 ceiling 同口径的**严格 top-1**（解析 tool-chain/v2 `recommendations[0].tool.pipeline_id`）后，SUT 降至 35.8%。三个口径并列报告：

| 口径 | 数值（67 例） | 含义 |
|---|---|---|
| 子串（旧，已弃） | 85.1% | 软：提到即算 |
| **严格 top-1** | **35.8%** | 硬：与 ceiling 可比，SUT 仍为词表的 25 倍 |
| 格式合规率 | 52% | 模型半数写散文，契约执行不彻底 |
| 结构化子集内准确率 | 69% | 遵守契约时的工具选择质量 |

**两个系统性发现**（比绝对数字更重要）：① **生存族 0/5**——图谱内 km_survival/cox_model/survival_analysis/tmb/her2 意图重叠，模型稳定选到合理但非期望的工具，是"题库单解 vs 图谱多解"的标注问题；② **输出契约执行仅 52%**——即使强制"单个 JSON"，模型仍频繁写散文，需要更强的输出纪律或结构化校验层。

**改动三：去名集**（`benchmark/data/de_named_set.json`，14 工具 × 5 = 70 例）：只写意图、不出现任何规则触发词（已程序化验证**零重叠**）。ceiling 在去名集从 100% 崩到 1.4%，正说明原集的 100% 全是词表泄漏。

**改动四：推理归模型。** MCP server 不内嵌 if-else 推理：`get_planning_guide` 给手册、`read_cypher` 给数据、`validate_atomic_chain` 给确定性校验；关键词表降级为 `rule_baseline_plan` 对照臂（非推荐路径）。

**数据面**（96 例，已拆含水）：期望文件图内可查 174/186（93.5%），其中**精确命中 138/186（74.2%）**，仅宽匹配命中 36（19.4%）；6 例（q052–057）期望数据是 demo 文件（`NVM0598_*`、`ENCSR142YZV_chr19only_*`），不在 Neo4j 图内——以图为准如实报 `missing_from_graph`。

## 给前端 agent 的 MCP 接口（stdio，同机/局域网）

仓库自带 `mcp_light_server.py`（无第三方依赖的 stdio MCP server，v2），前端 agent 直接接。**推理留给调用方自己的模型**：

- `get_planning_guide()` —— 返回 SKILL.md 全文，调用方模型读后自行规划
- `read_cypher(query)` —— 数据面：通用只读查询（有写入守卫）
- `validate_atomic_chain(chain)` —— 确定性闭集校验（11 个 atomic + 图内 next_tool 邻接；输出 Knowledge Card meta.id + 卡内 IO 名，图谱 id / meta.id 均可入参）
- `validate_execution_chain(steps)` —— **提交前把关（场景1）**：五阶段探查（注册/卡契约必填输入/绑定结构/数据探查/链流转），输出 tool-chain-validation/v1.1 逐阶段报告，errors 清零才可提交
- `rule_baseline_plan(query)` —— **对照臂**：关键词基线，非推荐路径，仅供与模型路径对比
- `health_check()` —— Neo4j 连通、规模、atomic 闭集

```json
{ "mcpServers": { "bio-pipeline-light": {
  "type": "stdio", "command": "python3",
  "args": ["/path/to/bio-pipeline-light/mcp_light_server.py"],
  "env": { "NEO4J_USER": "neo4j", "NEO4J_PASSWORD": "***" } } } }
```

仓库根目录已有 `.mcp.json`（Claude Code 打开即用）。完整说明见 `docs/frontend-mcp-connection.md`。

## 目录结构

```
├── skill/                          # bio-pipeline-planning skill 本体（装到 ~/.dsh/skills/）
│   ├── SKILL.md                    #   词汇表 + 闭集目录规则 + 15 配方 + tool-chain/v2 契约
│   └── references/
│       ├── query_templates/        #   15 条官方 Cypher 模板（源自 bio-pipeline-kg-matcher）
│       ├── tool_catalog.csv        #   11 atomic + 38 pipeline + 1 task_pipeline 闭集目录
│       └── artifact_type.csv       #   ArtifactType 词表（tool 槽位 artifact 取值）
├── benchmark/
│   ├── bench_light_96.py           # 96 例评测（语义判别 + 目录复检 + 图查询）
│   └── bench_light_96_report.json  # 逐 case 明细
├── examples/
│   └── plan_immune_infiltration_v2.json  # tool-chain/v2 示例 plan（真实数据）
└── docs/integration.md             # dsh-mcp-client 配置 + skill 安装
```

## 快速开始

1. **配 MCP**（DSH）：在 profile 的 `cordis.patch.yml` 加 `dsh-mcp-client` 行，指向官方 `neo4j-mcp-server`（stdio，`NEO4J_READ_ONLY=true`），详见 `docs/integration.md`。
2. **装 skill**：`cp -r skill ~/.dsh/skills/bio-pipeline-planning`，新会话即可见。
3. **跑评测**：
   ```bash
   export NEO4J_USER=neo4j NEO4J_PASSWORD=<你的密码>
   python3 benchmark/bench_light_96.py <96例问题-数据-工具对应表.xlsx>
   ```

## 边界与后续

- Skill 替代的是**数据面 + 推理面**；原子链的**确定性闭集校验**（`workflow_composer.py` 132KB 核心：Knowledge Card、GATK tumor/normal 四槽、逐链资产证据）仍是代码逻辑。需要 96 例评测级严格保证时，写一个几百行的薄 DSH 插件工具 `validate_atomic_chain` 即可，不需要再挂整个重 MCP。
- 数据 `file_path` 是图谱记录（可能指向另一台服务器），执行端资源（GTF/索引/参考基因组）不参与可用性判定。

## License

MIT
