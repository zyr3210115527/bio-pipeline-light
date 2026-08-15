---
name: bio-pipeline-planning
description: 基于 Neo4j 知识图谱的生信分析链路规划与工具链 Plan 生成。把用户的分析请求映射到图谱中的工具/方法链路（next_tool、input/output format、suitable_for modal），验证链路完整性，选择合适的数据（study/sample/T1/T2），按 tool-chain/v2 契约输出可交给前端/后端执行的结构化 Plan。Use when the user asks for a bioinformatics analysis (RNA-seq、WES/WGS 变异检测、单细胞、生存分析、富集、免疫浸润、WGCNA 等)、想了解能做哪些分析、或要输出工具链 Plan JSON。
whenToUse: 用户提出生信分析需求、询问"能做哪些分析"、"方法链路是否完整"、"选什么数据"、"怎么排 plan"、"输出 plan JSON" 时加载。本 skill 是重 MCP（bio-pipeline-kg-matcher 的 server.py，7 个工具）的轻量替代：数据面走 read-cypher，推理面走本手册，确定性校验面按需用规则近似。
---

# 生信分析链路规划（Neo4j 知识图谱 + tool-chain/v2）

以本机 Neo4j（bolt://localhost:7687，库 neo4j）为唯一事实源。查询接口：
- 优先 MCP 工具 `mcp__neo4j__read-cypher`（参数 `query`，只读；写语句会被拒）
- 备用：`curl -u neo4j:<pwd> -X POST -H 'Content-Type: application/json' -d '{"statements":[{"statement":"<Cypher>"}]}' http://127.0.0.1:7474/db/neo4j/tx/commit`
- **绝不**执行 CREATE/MERGE/DELETE/SET；本图谱是只读咨询面。

## 图谱模型（0812 交付；数量以图内实测为准）

| 节点 | 含义 |
|---|---|
| `tool`（51） | 分析工具/流程：`tool_name`、`function`（中文）、`semantic_output`（`;` 分隔语义产物）、catalog_id（T001…） |
| `function`（90） | 分析功能（中文整句，用 CONTAINS 子串匹配） |
| `format` / `modal` / `datalevel` | 格式（`RAW_PAIRED_END_R1_FASTQ`、`DNA_VARIANT_VCF_GENERAL`、`MUTATION_ANNOTATION_FORMAT_MAF`…）/ 模态 / 层级（1 原始→4 知识） |
| `study` / `project` | 队列：`study_accession`、`tumor_type`、`study_description`、`sample_count` |
| `individual` / `sample` | 个体/样本：`sample_accession`、`tissue_type`（Tumor/Normal）、`specimen_type`、`gender` |
| `T1` | 原始数据文件（FASTQ 等）：`file_path`、`file_name`、`format`、`strategy` |
| `T2` | 分析结果文件（VCF/BAM/MAF…）：`file_path`、`size`、`format` |

关键关系：`(tool)-[:next_tool]->(tool)` 链路；`(tool)-[:input|output]->(format)` I/O 契约；`(tool)-[:suitable_for]->(modal)`；`(tool)-[:has_function]->(function)`；`(T1|T2)-[:in_sample|in_format|in_modal|in_level|in_study]->(...)`；`(T2)-[:generated_from]->(T1)`；`(sample)-[:in_individual]->(individual)`。

## 闭集工具目录（0812，真源 = bio-pipeline-kg-matcher repo）

运行时目录 **50 个**：**11 个 atomic**（可编排）+ **38 个 pipeline** + **1 个 task_pipeline**。完整字段（catalog_id、input/output format、omics、变体、slot 绑定）见 `references/tool_catalog.csv`；ArtifactType 词表见 `references/artifact_type.csv`。

- **atomic 闭集（11）**：`bwa` `fastp` `fastqc` `featurecounts` `gatk` `bcftools` `snpeff` `samtools` `star` `trim_galore` `rsem`（`multiqc` 仅收尾，不参与编排）
- **task_pipeline（1）**：`rnaseq_singletask`
- **pipeline 业务工具（38）**：`diff_expr_go` `diff_expr_kegg` `immune_infiltration_iobr` `wes_somatic_maf_landscape` `wes_somatic_pair` `survival_analysis` `tmb_survival_analysis` `her2_pfs_survival` `driver_gene_gender_analysis` `rnaseq_unsupervised_cluster` `wgcna` `wgcna_hub` `wgcna_module_trait` `cellranger_workflow` `paired_fastq_to_unmapped_bam` `cnvkit_cnv_clinical` `cox_model` `km_survival` `gsea_pathway_enrichment` `hvg_pca_gmm` `preprocess_counts` `rmats_alternative_splicing` `scrna_cell_communication` `bootstrap_stability` 等（全表见 CSV）

**目录规则（决定 Plan 形态）**：
- `recommendations[]` 可给**业务 pipeline**（38 + task）；`candidates[]` **只出通过闭集校验的 atomic 链**（11 个内）。
- **未原子化需求**：差异表达、富集、WGCNA、生存分析等没有原子表达 → `candidates[]` 返回 `unsupported`，**不得拿 pipeline 节点凑原子链**；但 `recommendations[]` 照常给业务 pipeline。
- **变体绑定**：`gatk` 有 `single`（sorted_dedup_bam）与 `paired`（tumor_bam/tumor_bai/normal_bam/normal_bai 四槽，`exactly_one_variant=true`）；`fastp` 有 single_end/paired_end 变体。配对的肿瘤/正常 WES 必须用四槽并查 `find_paired_tumor_normal_samples.cypher`。
- slot 模型（槽位名、`builder_param`/`wdl_target` 绑定）是**执行端合同**，来自 `data/csv/catalog`，不在图里；图只负责"有哪些工具、怎么连"。
- 数据可用性语义：经 Neo4j 精确确认的文件标 `available`，否则 `missing_from_graph`；执行端资源（GTF、参考基因组、索引）不参与可用性判定。

## 查询配方

**15 条官方 Cypher 模板**在本 skill 的 `references/query_templates/`，按名取用（都是 read-cypher 直接可执行的语句）：

| 模板 | 用途 |
|---|---|
| `find_tools_by_function` | 按功能中文子串找工具（`has_function` CONTAINS） |
| `find_tools_by_input_format` / `find_tools_by_output_format` | 按输入/输出格式找工具 |
| `find_tool_input_output` | 单工具的 I/O 契约 |
| `find_tools_by_modal` | 按模态找工具 |
| `trace_next_tool_chain` | 从某工具沿 `next_tool` 走链 |
| `recommend_next_tools_via_output_match` | 按"上一工具输出 = 下一工具输入"推荐下游 |
| `trace_paths_from_input_format_to_output_format` | 输入格式→输出格式的可行路径 |
| `find_t1_by_study_and_format` / `find_t1_by_modal` / `count_data_by_study` / `count_by_semantic_format` | 数据文件查询与计数 |
| `find_paired_tumor_normal_samples` | 某 study 内每个个体的肿瘤/正常配对（`pairable` 布尔） |
| `trace_sample_hierarchy` | individual → sample → run → 文件的层级溯源 |
| `trace_data_lineage` | T2 → `generated_from` → T1 数据血缘 |

**常用组合配方**（翻译成中文语义）：
1. **请求→工具匹配**：分词多关键词 OR 检索 `tool_name`/`function`；注意命名模式（`deg_*`/`de_*`=差异、`wgcna*`=共表达、`*survival`/`km_*`/`cox_*`=生存、`*enrichment`=富集、`*cellchat`=细胞通讯、`tmb_*`=突变负荷）。
2. **链路组装+验链**：逐环节核对上一工具 `output`/`semantic_output` 与下一工具 `input` 的 format 交集；缺口如实报（"图谱缺：<环节>，期望输入 <format>；建议 <可补工具或说明>"），**绝不虚构不存在的工具**。
3. **数据选择**：队列（`tumor_type`/`study_accession`）→ 首步输入格式的 T1 文件 → 中间结果复用（T2 已有现成 VCF/MAF/BAM 则标注"复用"，跳过上游重复计算）→ 样本约束（`tissue_type`、`specimen_type`、`gender`、配对需求用 `find_paired_tumor_normal_samples`）。

## 规划流程（5 步）

1. **解析请求**：分析类型、模态、目标产物（必要时先问清：已有数据格式/队列/分组）。
2. **匹配**（配方 1）：候选工具 + 函数 + 格式；判定业务 pipeline vs atomic 链（见目录规则）。
3. **组装验链**（配方 2）：数据→预处理→比对→定量/变异→下游；每步标注工具、输入格式、输出格式、验证点；缺口如实列出。
4. **选数据**（配方 3）：队列 + 格式 + 样本约束 + 配对；给出文件数量、来源、`file_path`、可用性标记。
5. **输出**：按需给"可读 plan"（下表）或 **tool-chain/v2 JSON**（下节）。

## tool-chain/v2 输出契约（前端真源 = bio-pipeline-kg-matcher 的 pipeline_router）

当输出给前端/联调时，产出此 JSON（前端只读 JSON-RPC `result.structuredContent` 那一层）：

```json
{
  "schema_version": "tool-chain/v2",
  "selection_status": "information | ok | no_candidate | unsupported | ...",
  "candidate_count": 0,
  "candidates": [],
  "recommendation_count": 1,
  "recommendations": [{
    "rank": 1,
    "match_id": "recommendation-<hex>",
    "pipeline_id": "immune_infiltration_iobr",
    "match_note": "命中 xxx，适合 yyy。",
    "tool": {
      "tool_id": "immune_infiltration_iobr", "catalog_id": null, "tool_kind": "pipeline",
      "name": "免疫浸润分析 (IOBR CIBERSORT)", "description": "...",
      "inputs": [{"name":"expression_tsv","type":"File","is_file":true,"optional":false,
                  "artifact":"expression_tpm_matrix","formats":["tsv"],"description":"...",
                  "dimension":"","dimension_value":"","variant":"","variant_alias_for":""}],
      "outputs": [{"name":"cibersort_full_tsv","artifact":"...","formats":["tsv"],...}]
    },
    "data": {
      "status": "available", "source": "neo4j",
      "assets": [{"file_name":"HRA001272-Genes-TPM-1.0.tsv","format":"tsv",
                  "strategy":"","data_level":"","study_accession":"HRA001272",
                  "sample_accession":"","run_accession":"","individual_accession":"",
                  "specimen_types":"","read_pair":null,
                  "file_path":"/hpcdisk1/.../HRA001272-Genes-TPM-1.0.tsv",
                  "match_reason":"癌种/队列匹配; 格式匹配 tsv; ..."}],
      "matched_count": 3, "expected_count": 3,
      "missing_asset_names": [], "study_accessions": ["HRA001272"]
    },
    "source": "deterministic_rule+neo4j", "reference_case_id": null
  }],
  "intent": {"query_text":"...","analysis_goal":"免疫浸润分析","disease":"肝癌",
             "omics_type":"bulk RNA-seq","input_hint":"tpm","quant_hint":null,
             "requested_outputs":[],"study_accessions":[],"source":"rule","ambiguous":false},
  "planner_metadata": {"used":false,"status":"force_rule","calls":0,"stages":[]},
  "data_matcher_mode": "neo4j", "mcp_timing_ms": 1151.2
}
```

要点：`assets` 逐文件带溯源与 `match_reason`；`inputs/outputs` 的 `artifact` 用 ArtifactType 词表（`references/artifact_type.csv`）；`candidates[]` 只在能做原子链时填，否则空 + `selection_status` 说明。

## 可读 Plan 模板（面向人）

```markdown
# 分析：<名称>（模态：<modal>；数据层级：<level>）
## 一、数据：队列 <HRAxxxxx>（<n> 样本），输入 <format> × <n>，路径 <dir>；可复用 T2 现成 <format>
## 二、方法链路：| # | 工具 | 输入格式 | 输出格式 | 验证点 |（逐环节）
## 三、链路完整性：✅ 完整 / ⚠️ 缺：<环节>（建议 <X>）
## 四、可执行性：工具环境（实测 which）、数据可达性（实测路径）、参考文件、算力估计
```

## 边界与原则

- 图谱是"方法与数据的地图"；工具是否已安装、数据路径本机是否可达要**实际检查**（which、ls），不假装可读。
- 回答"能做哪些分析"：先给图谱覆盖的分析族，再对感兴趣族给链路。
- 前端/后端要执行时，plan 里的 `file_path` 是图谱记录（可能指向另一台服务器），如实说明来源。
- 全程只读；任何写意图先说明方案再执行。
