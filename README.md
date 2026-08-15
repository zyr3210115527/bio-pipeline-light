# Bio Pipeline Light（轻架构生信链路规划）

用 **DSH 原生 MCP + 一个 Skill** 替代重 MCP（[bio-pipeline-kg-matcher](https://github.com/zyr3210115527/bio-pipeline-kg-matcher)）的生信工具链规划能力：数据面走 `read-cypher`/`get-schema`，推理面走 Skill 手册，把 205MB + LLM 嵌套的重服务降为「1 个 skill + 2 个 MCP 工具」。

```
轻架构 = neo4j-mcp（read-cypher / get-schema）   ← 数据面
       + bio-pipeline-planning skill             ← 推理面（词汇表 + 配方 + tool-chain/v2 契约）
       + bench_light_96.py                       ← 96 例评测（确定性规则面）
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

## 96 例评测结果

对照 `96例问题-数据-工具对应表(1).xlsx`（14 个工具 × 三档问法）：

| 指标 | 轻架构（规则面） | 重 MCP 官方基线 |
|---|---|---|
| 工具 top-1 | **96/96 (100%)** | 96/96 (100%) |
| 工具 top-3 | 96/96 (100%) | — |
| 期望数据文件图内可查 | 174/186 (93.5%) | 实时后端仅 6/96 全可用 |
| 工具正确 + 数据齐全（联合） | 90/96 (93.8%) | — |

- 14 个工具全部 100% 命中（`diff_expr_go/kegg`、`immune_infiltration_iobr`、`rnaseq_unsupervised_cluster`、`wes_somatic_maf_landscape`、`her2_pfs_survival`、`survival_analysis`、`wgcna`、`cellranger_workflow`、`wes_somatic_pair`、`tmb_survival_analysis`、`driver_gene_gender_analysis`、`paired_fastq_to_unmapped_bam`、`rnaseq_singletask`）
- 剩余 6 例（q052–057）工具全对，期望数据（`NVM0598_*.fastq.gz`、`ENCSR142YZV_chr19only_10000_reads_*.fastq.gz`）是 **demo 测试文件，不在 Neo4j 图内**——轻架构以图为准如实报 `missing_from_graph`（重 MCP 靠本地 pipeline 仓库的 example_inputs fixture 才标"可用"）
- 判据说明：工具面可比且打平；数据面轻架构是"期望文件名图内可查"（含多输入工具的 clinical/meta 全部文件），重 MCP 实时验证跑在 0812 交付图上且判据更严格，数字不可直接横比

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
