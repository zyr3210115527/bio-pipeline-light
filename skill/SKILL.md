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

## 图谱模型（0819 交付；数量以图内实测为准）

| 节点 | 含义 |
|---|---|
| `tool`（51） | 分析工具/流程：`tool_name`、`function`（中文）、`semantic_output`（`;` 分隔语义产物）、catalog_id（T001…） |
| `function`（90） | 分析功能（中文整句，用 CONTAINS 子串匹配） |
| `format` / `modal` / `datalevel` | 格式（`RAW_PAIRED_END_R1_FASTQ`、`DNA_VARIANT_VCF_GENERAL`、`MUTATION_ANNOTATION_FORMAT_MAF`…，0819 新增 `CLINICAL`/`*_META` 元数据格式）/ 模态 / 层级（1 原始→4 知识） |
| `study` / `project` | 队列：`study_accession`、`tumor_type`（0819 起 Title Case，如 `Liver Cancer`，查询仍用 toLower）、`study_description`、`sample_count` |
| `individual` | 个体：`individual_accession`；临床属性 0819 起按编号前缀分组——`00_*` 基本信息、`01_*` 人口学（`01_age`/`01_gender`/`01_race`）、`09_*` 肿瘤病理（`09_tumor_stage`/`09_tumor_type`…）、`11_*` 分子指标（`11_tmb`/`11_msi_score`）、**`13_*` 生存（`13_survival_days`/`13_survival_status`）——生存分析选数据用这里** |
| `sample` | 样本：`sample_accession`、`tissue_type`（Tumor/Normal）、`specimen_type`（0819 起下划线风格，如 `Patient_Solid_Tissue`）、`gender` |
| `T1` | 原始数据文件（FASTQ 等）：`t1_id`、`file_path`、`file_name`、`file_format`（字面格式）、`semantic_format`、`strategy` |
| `T2` | 分析结果文件（VCF/BAM/MAF…）：`t2_id`、`file_path`、`size`、`format`、`semantic_format` |

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
3. **数据选择（注意英文词表与 T2 现成矩阵）**：
   - 队列：`tumor_type` 是**英文**（`Liver Cancer`/`Glioma`/`Melanoma`/`Esophageal Cancer`…），用 `toLower(s.tumor_type) CONTAINS 'liver'` 等英文关键词；中文查不到。
   - **现成表达矩阵在 T2**（文件名含 `Genes`，如 `HRA001272-Genes-TPM-1.0.tsv`），不在 T1；T1 是原始 FASTQ。格式字段：`semantic_format`（语义格式，如 `TABULAR_BIO_DATA`）≠ `format`/`file_format`（字面格式）。
   - 中间结果复用：T2 已有现成 VCF/MAF/BAM 则标注"复用"，跳过上游重复计算；样本约束用 `tissue_type`/`specimen_type`/`gender`，配对需求用 `find_paired_tumor_normal_samples`。
   - **配对分析先做队列发现**：不要假设某队列可配对，先聚合查询哪些 study 有同个体 Tumor+Normal：
     ```cypher
     MATCH (sp:sample)-[:in_individual]->(i:individual)
     WITH sp.study_accession AS study, i, collect(DISTINCT sp.tissue_type) AS tts
     WHERE 'Tumor' IN tts AND 'Normal' IN tts
     RETURN study, count(i) AS pairable_individuals ORDER BY pairable_individuals DESC
     ```
     已知陷阱：**HRA000071 的血液对照与肿瘤样本在图内不属于同一个体**（572 样本 1:1 对应 572 个体），能做 tumor/normal 分组（resolve_sample_roles 可判角色）但**做不了同个体配对**（wes_somatic_pair 不适用），如实告知用户。
   - **样本角色（tumor/normal）必须用 MCP 工具 `resolve_sample_roles` 判定，绝不自行按名称/直觉猜**：配对或分组分析（wes_somatic_pair、生存、差异表达分组等）选队列前先传 `study` 查 `role_resolved`——为 false 的队列做不了配对/分组，如实报告；逐文件的 `sample_role`/`sample_role_label` 用 `records` 模式判。
   - **队列样本清单以 `sample` 节点为准**：`MATCH (sp:sample) WHERE sp.study_accession = '<HRA*>'`（等价于 `study<-individual<-sample` 遍历）。**不要用 `(T1)-[:in_sample]->(sample)` 数样本**——那只能看到挂了文件的样本，无文件的样本会被静默漏掉（HRA006117 实有 835 个，走文件路径只剩 570）。
   - **文件为什么会 `sample_accession = null`，分两种，不要混为一谈**：
     1. **聚合类文件**（表达矩阵/MAF/临床表/MetaInfo）本就跨样本，无单样本归属，字段 null 属正常；
     2. **按 run 组织的 fastq**（`data_level=1`）应当有样本，但图谱里 `sample` 节点每个只记录**一个** `run_accession`，而文件侧共有 13,063 个 run——**3,758 个 run（29%，牵连 7,516 个 T1 文件）在图里没有对应 sample 节点**，走 `run_accession` 也回连不上（命中 0）。这是**图谱 run→sample 映射缺失**，不是"聚合文件"。此时如实输出 `missing_from_graph` 并说明原因，**绝不按文件名/顺序猜样本归属**。
     `resolve_sample_roles(study=...)` 的 `file_coverage.runs_without_sample_node` 会直接给出该队列缺多少（如 HRA000087 缺 1492/1553、HRA001272 缺 482/1180、HRA000071 缺 0）。规划 FASTQ 起步的链路前先看这个数：缺口大的队列拿不到完整样本级输入。

## 接地纪律（最高优先级：答案只能来自本手册与图谱查询结果）

你的内部生信知识**只许用来理解用户意图、决定查什么**；答案内容必须全部接地：

1. **名词白名单**：回答/Plan 中出现的每一个 `tool_id`/`pipeline_id`、队列号（HRA*）、文件名、文件路径、格式名、样本号，都必须**逐字来自本手册（含 references/ 目录文件）或本会话的工具返回结果**。不确定某名词是否查到过 → 重新查询确认，或不要使用它。
2. **禁止知识补全**：图里查不到的工具/数据/链路环节，如实输出 `missing_from_graph` / `no_candidate` / `unsupported`，**绝不用训练知识补全**——即使你"知道"某个工具（如 Seurat、DESeq2）真实存在，只要它不在闭集目录里，就不能出现在答案中。
3. **证据可追溯**：`match_note`/`match_reason` 要能对应到某次查询结果；样本角色一律来自 `resolve_sample_roles`，路径一律来自图谱记录或 `validate_execution_chain` 的 `execution_params`。
4. **输出前自检**：最终 JSON 先交给 `validate_plan` 工具核验；`grounded=false` 时按 `violations` 逐条修正（回到查询结果找依据，而不是换个编法）再验，直到 `grounded=true` 才输出。

## 执行纪律（步数优化，必须遵守）

目标：**工具调用 ≤ 3 轮（每轮 ≤ 2 条查询），总查询 ≤ 6 条**；证据充分立即输出，不反复核实。

1. **禁止 `get_schema`**：图谱模型、标签、关键关系已在本手册列出，不需要整库 schema；需要具体字段时用定向查询。
2. **一次查全，禁止零碎查询**：优先用合并查询，例如"候选队列 + 每队列 T1/T2 文件清单"一条语句带回：
   ```cypher
   MATCH (s:study) WHERE toLower(s.tumor_type) CONTAINS 'liver'
   OPTIONAL MATCH (f:T1)-[:in_study]->(s) RETURN s.study_accession, s.sample_count,
     collect(DISTINCT f.format) AS t1_formats LIMIT 10
   ```
3. **查过即用，不重复核实**：同一工具契约/同一队列只查一次；后续步骤引用已查结果，不再重复发相同查询。
4. **先想后查**：每次查询前明确"这条要回答什么问题、用什么配方"；想不清楚就先按配方走，不要自由发挥新查询。
5. **按配方优先**：15 条模板能覆盖的查询直接用模板，不要自行改写结构。
6. **收敛**：证据足够（工具确定 + 数据现状清楚）即停止查询，直接输出 Plan；若查询结果为空，先检查关键词语言（中文/英文）与目标表（T1/T2），不要重复同一失败查询。

## 提交前把关（执行契约校验，场景1）

当用户/前端要**提交链到执行端**（或问"这条链能不能跑/缺什么"）时，调用 `validate_execution_chain` 做五阶段探查，而不是直接回答"能跑"：

1. **注册校验**：每个 tool_id 已知（图谱/Knowledge Card）
2. **卡契约**：每步 Knowledge Card 必填输入是否齐全（缺一个就拒）
3. **绑定结构**：File 输入 binding 必须是对象（file_id/file_name），标量类型须匹配卡声明
4. **数据探查**：未绑定的 File 输入 → 图内查候选文件数（按格式族 + 可选队列过滤）
5. **链流转**：next_tool 邻接 + 上下游格式衔接

输出 `tool-chain-validation/v1.1` 逐阶段报告，并附 `execution_params`（输入名 → 图内真实文件路径，只认 `/` 开头的确认路径，绝不伪造）、`execution_params_missing`、`submittable`。**errors 清零且 `submittable=true` 才可提交**；`submittable=false` 时不得宣称"这条链能跑"，把 `execution_params_missing` 如实列给用户。pipeline 级工具无卡时明确警告"跳过契约校验"。

## 规划流程（5 步）

1. **解析请求**：分析类型、模态、目标产物（必要时先问清：已有数据格式/队列/分组）。
2. **匹配**（配方 1）：候选工具 + 函数 + 格式；判定业务 pipeline vs atomic 链（见目录规则）。
3. **组装验链**（配方 2）：数据→预处理→比对→定量/变异→下游；每步标注工具、输入格式、输出格式、验证点；缺口如实列出。
4. **选数据**（配方 3）：队列 + 格式 + 样本约束 + 配对；给出文件数量、来源、`file_path`、可用性标记。
5. **输出**：按需给"可读 plan"（下表）或 **tool-chain/v2 JSON**（下节）。**全程工具调用控制在 ≤3 轮（见执行纪律）**。

## tool-chain/v2 输出契约（前端真源 = bio-pipeline-kg-matcher 的 pipeline_router）

**输出契约（硬性规则，违反即视为未完成任务）**：最终答案**必须且只能输出一个 tool-chain/v2 JSON 对象**——不要散文、不要 markdown、不要列多个候选、不要在 JSON 前后加任何解释。`recommendations[0]` 是唯一推荐（严格 top-1）；`candidates[]` 只在能做原子链时填充。

**拒绝纪律（先判再查，命中即拒，不调用任何查询工具）**：
- **无关问题**（闲聊、代码求助、留学/生活咨询等一切与生信分析规划无关的请求）→ 输出 `{"status":"rejected","reason":"off_topic: <一句话说明>"}` 单对象。
- **患者隐私问询**（询问个体层面的临床信息：某个/某些病人的年龄、性别、种族、吸烟史、病理分期、生存时间等，或要求"列出所有病人的 X"）→ 输出 `{"status":"rejected","reason":"privacy: 患者级临床数据不对外提供，仅支持聚合统计"}` 单对象。合法的聚合需求（"有生存数据的样本有多少"）照常服务，用 count/IS NOT NULL 聚合查询。
- 服务端双保险：`read_cypher` 会拒绝对 `01_/03_/09_/11_/13_` 前缀临床属性的非聚合查询——收到该拒绝时不要改写绕过，向用户如实说明隐私边界。

**`read_cypher` 结果上限（会影响结论正确性，务必注意）**：单次最多返回 **500 行**。超出时返回体带 `truncated: true` 和 `row_count`，**这时手上是截断样本，不是全集**——绝不能据此下"共有 N 个 / 全部都是 / 没有其他"这类全称结论。要总数就改用 `count(...)`/聚合重查，要细节就加更严格的过滤条件（队列号、format、data_level）再查。不带 `truncated` 的结果才是完整结果集。

**命名契约（Knowledge Card 对齐）**：原子工具的 `tool_id` 必须用 Knowledge Card 的 `meta.id`（如 `bwa_mem_paired` 而非 `bwa`），`tool_chain.inputs` 与输出引用用卡内定义的输入输出名称（如 `read1`/`aligned_sam`）。映射表见 `references/knowledge_cards_map.json`（12 张原子卡）。pipeline 级工具（无卡，如 `diff_expr_go`）维持图谱 tool_id，并在 `tool_id` 旁标注 `"card": null`。

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

要点：`assets` 逐文件带溯源与 `match_reason`；`inputs/outputs` 的 `artifact` 用 ArtifactType 词表（`references/artifact_type.csv`）；`candidates[]` 只在能做原子链时填，否则空 + `selection_status` 说明。**单样本类资产（FASTQ/BAM 等）必须带 `sample_role`/`sample_role_label`（用 `resolve_sample_roles` 判定）；聚合类资产（矩阵/MAF/临床表）这些字段置 null。** 配对/分组分析的 `data` 下建议附 `alternatives[]`（其他可选队列：`study_accession`/`label`/`sample_roles` 统计/`role_resolved`/`selected`，数据同样来自 `resolve_sample_roles` 与队列查询）。执行参数一律以 `validate_execution_chain` 返回的 `execution_params`/`submittable` 为准转录进 plan，不自行拼路径。

## 可读 Plan 模板（面向人）

```markdown
# 分析：<名称>（模态：<modal>；数据层级：<level>）
## 一、数据：队列 <HRAxxxxx>（<n> 样本），输入 <format> × <n>，路径 <dir>；可复用 T2 现成 <format>
## 二、方法链路：| # | 工具 | 输入格式 | 输出格式 | 验证点 |（逐环节）
## 三、链路完整性：✅ 完整 / ⚠️ 缺：<环节>（建议 <X>）
## 四、可执行性：工具环境（实测 which）、数据可达性（实测路径）、参考文件、算力估计
```

## 边界与原则

- **隐私红线**：`individual` 的编号临床属性（`01_` 人口学、`03_` 生活史、`09_` 病理、`11_` 分子指标、`13_` 生存）是患者级敏感数据。规划过程只做聚合统计与存在性判断（count / IS NOT NULL）；绝不在回答、Plan、日志里出现任何个体的临床属性值。样本的 `tissue_type`/`specimen_type`/`gender` 作为分组约束属操作性使用，不逐个体罗列。
- 图谱是"方法与数据的地图"；工具是否已安装、数据路径本机是否可达要**实际检查**（which、ls），不假装可读。
- 回答"能做哪些分析"：先给图谱覆盖的分析族，再对感兴趣族给链路。
- 前端/后端要执行时，plan 里的 `file_path` 是图谱记录（可能指向另一台服务器），如实说明来源。
- 全程只读；任何写意图先说明方案再执行。
