---
name: bio-pipeline-planning
description: Bioinformatics pipeline planning over a Neo4j knowledge graph, producing tool-chain/v2 Plan JSON. Map an analysis request to graph-verified tool/method chains (next_tool, input/output format, suitable_for modal), verify chain integrity, and select data (study/sample/T1/T2). 基于 Neo4j 知识图谱的生信分析链路规划。Use when the user asks for a bioinformatics analysis (RNA-seq、WES/WGS 变异检测、单细胞、生存分析、富集、免疫浸润、WGCNA 等)、想了解能做哪些分析、或要输出工具链 Plan JSON.
whenToUse: 用户提出生信分析需求、询问"能做哪些分析"、"方法链路是否完整"、"选什么数据"、"怎么排 plan"、"输出 plan JSON" 时加载。本 skill 是重 MCP（bio-pipeline-kg-matcher 的 server.py，7 个工具）的轻量替代：数据面走 read-cypher，推理面走本手册，确定性校验面按需用规则近似。
---

# Bio Pipeline Planning (Neo4j KG + tool-chain/v2)

The Neo4j graph (database `neo4j`) is the **only source of truth**. Plan yourself; this server only
provides knowledge and deterministic checks — there is **no "one-call Plan" endpoint**.

- Query interface: `read_cypher(query)` — read-only. Write statements and non-aggregate queries over
  patient-level clinical properties are refused by the server.
- Server env: `NEO4J_URL` / `NEO4J_USER` / `NEO4J_PASSWORD`. Never put address or credentials into
  queries or answers.
- **Never** run CREATE/MERGE/DELETE/SET/LOAD CSV. This graph is a read-only advisory surface.

## 1. Tools (10)

| Tool | Purpose | When to call |
|---|---|---|
| `get_planning_guide()` | Returns this manual | Once, at session start (skip if the client already inlined the manual) |
| `read_cypher(query)` | Read-only Cypher over the graph | Single targeted query |
| `read_cypher_batch(queries)` | Multiple independent read-only queries in ONE call, results in order | **Default for data fetching** — pack every independent query into one batch (≤ 8) |
| `get_study_overview(study)` | Cohort profile in one call: study info + sample count + T1/T2 format/strategy stats + T2 file samples + sample roles & file_coverage | Immediately after a cohort is chosen — replaces the "study info + file inventory + resolve_sample_roles" call cluster |
| `resolve_sample_roles(study \| records)` | Deterministic tumor/normal role judgment | Paired/grouped analysis when a full overview is not needed; per-file roles use `records` mode; never guess roles yourself |
| `validate_atomic_chain(chain)` | Closed-set + next_tool adjacency check for atomic chains | **Once**, after the chain is assembled |
| `validate_execution_chain(steps)` | Pre-submission 5-stage gate → `execution_params`, `submittable` | Only when the user/front-end is about to submit for execution |
| `hydrate_plan(plan)` | Deterministic completion: fills every field the catalog/graph already knows — tool `catalog_id`/`tool_kind`/`name`/`description`/I/O slots, asset `file_path`/`format`/`data_level`/accessions, atomic-chain slots from Knowledge Cards, `match_id`/`rank`/`source`, `planner_metadata`, `data_matcher_mode`, `mcp_timing_ms` | **Once**, on the finished Plan, immediately before `validate_plan` — author only the judgment fields (§9) and let this fill the boilerplate |
| `validate_plan(plan)` | Grounding check of the final Plan | **Once** before final output; re-call only to verify fixes of listed violations |
| `health_check()` | Connectivity, graph size, atomic closed set | Diagnostics only |

## 2. Graph model (0826 delivery: 81,572 nodes / 364,260 relations)

| Node | Key properties (caveats) |
|---|---|
| `tool` (55) | `tool_name`, `function` (controlled term, see below), `semantic_output` (`;`-separated), **`tool_id`** — this is the `T001`…`T055` id (all 55 match `T[0-9]+`). **There is no `catalog_id` property on the graph node** (0/55 carry it); `catalog_id` is what the *closed-set catalog* calls this same value, so join catalog→graph as `catalog_id` → `t.tool_id`, and never write `t.catalog_id` (it returns an all-null column, not an error) |
| `function` (30) | **A controlled vocabulary since the 0826 rebuild — 30 fixed Chinese terms, not free text.** Every one of the 55 tools carries at least one `has_function` edge (89 edges total), so function is now the most reliable way to go from an intent to a tool set. **Match by equality on the exact term**, not CONTAINS: `测序质量评估` `接头与低质量序列修剪` `序列格式转换` `DNA序列比对` `RNA序列比对` `比对后处理与去重` `变异质量校正` `体细胞变异检测` `胚系变异检测` `结构变异检测` `拷贝数变异分析` `基因融合检测` `可变剪接分析` `变异过滤与处理` `变异注释` `突变景观与可视化` `肿瘤演化与克隆推断` `表达定量` `表达矩阵预处理与归一化` `单细胞比对与定量` `单细胞下游分析` `细胞通讯分析` `轨迹推断` `差异表达分析` `功能富集分析` `共表达网络分析` `无监督聚类分析` `免疫浸润与微环境分析` `生存分析` `可视化与报告`. Note there is **no space** in `DNA序列比对` / `RNA序列比对`. `可视化与报告` is a co-tag on 18 tools and discriminates nothing — never select on it alone |
| `format` (42) | e.g. `RAW_PAIRED_END_R1_FASTQ`, `DNA_VARIANT_VCF_GENERAL`, `MUTATION_ANNOTATION_FORMAT_MAF`, plus `CLINICAL` / `*_META` |
| `modal` (6) | **Only** `WES` / `WGS` / `bulk_RNA` / `sc-RNA` / `Clinical` / `Meta` — never invent spellings like `RNA-seq`. **The node property is `modal`, not `name`** — `(:modal {name:'sc-RNA'})` matches nothing and returns a silent zero. To find a modality's files, filter `T1.strategy = 'sc-RNA'` directly rather than traversing `in_modal` |
| `datalevel` (4) | Properties are `level` / `name` / `description`, **not** `data_level`; 1 raw → 4 knowledge. (File-side `T1.data_level` / `T2.data_level` ARE called data_level.) |
| `study` (20) / `project` (18) | `study_accession`, `tumor_type` (Title Case English, e.g. `Liver Cancer`; query with toLower + CONTAINS — one cancer has multiple spellings, see §4 recipe 3), `title`, `study_description`, `individual_count`, `sample_count` (**only 14/20 studies have it**: HRA000073/HRA000087/HRA002693/HRA006117/HRA007413/HRA016026 are null — sorting/filtering by it silently drops those 6; to size a cohort count `sample` nodes) |
| `individual` (7131) | **`00_individual_accession` is the id — there is NO bare `individual_accession` on this label** (that name exists only on `T1`/`T2`; using it here returns an all-null column, not an error). Other properties are prefix-grouped: **only `00_*` is operational** (`00_individual_accession` / `00_sample_accession` / `00_platform` / `00_strategy` …). **`01_`–`13_` are all patient-level sensitive**: 01_ demographics, 02_ family history, 03_ lifestyle, 04_ hematology, 09_ tumor pathology, 10_ invasion, 11_ molecular (`11_tmb` / `11_msi_score`), 12_ treatment, **13_ survival (`13_survival_days` / `13_survival_status` / `13_pfs_time` … — survival-analysis data lives here)**. Aggregates only (count / avg / IS NOT NULL); per-individual reads are refused by the server guard (§8) |
| `sample` (10465) | `sample_accession`, `sample_name`, `tissue_type`, `specimen_type` (underscore style, e.g. `Patient_Solid_Tissue`), `gender`. **`tissue_type` is not a clean Tumor/Normal binary** (0826: Tumor 7045, Normal 2863, Blood 557 — nulls are gone, but `Blood` still breaks equality matching); `specimen_type` does still have `;`-separated multi-values (486 `Organoid;Patient_Solid_Tissue`). Always judge roles via `resolve_sample_roles`, never equality-matching |
| `T1` | Raw files (FASTQ etc.): `t1_id`, `file_name`, `file_format` (literal), `semantic_format`, `data_level`, `study_accession` (all 6 populated for 28,229); `strategy` 28,222; `platform` / `sample_accession` / `individual_accession` / `sample_name` 28,184; `run_accession` / `experiment_accession` 27,070; `file_path` 26,879; `size` 25,417. **The 45 files missing those values are Clinical / `*_META` aggregate files** (not per-sample by nature) — do not conclude "no platform/sample info in graph" from them |
| `T2` | Result files (VCF/BAM/MAF…): `t2_id`, `file_name`, `format`, `strategy`, `data_level`, `size`, `study_accession` (all 35,572); `semantic_format` 35,570; `file_path` 35,566; `run_accession` 31,717. **T2 has no `platform` / `sample_accession`** — for sample ownership walk `(T2)-[:generated_from]->(T1)-[:in_sample]->(sample)` |

Key relationships: `(tool)-[:next_tool]->(tool)` chains; `(tool)-[:input|output]->(format)` I/O contract;
`(tool)-[:suitable_for]->(modal)`; `(tool)-[:has_function]->(function)`;
`(T1|T2)-[:in_sample|in_format|in_modal|in_level|in_study]->(...)`; `(T2)-[:generated_from]->(T1)`;
`(sample)-[:in_individual]->(individual)`; `(individual)-[:in_study]->(study)`;
`(study)-[:in_project]->(project)`; `(format)-[:subclass_of]->(format)` (specific → generic; walk up
when matching tools by semantic format).

**Numeric-looking fields are STRING — re-verified unchanged on the 0826 graph. Wrap in `toInteger()`/`toFloat()` before any
`<` `>` comparison or `ORDER BY`** (verified with `valueType()`): `data_level`, `size`, `01_age`,
`11_tmb`, `11_msi_score`, `13_survival_days` / `13_dfs_time` / `13_efs_time` / `13_pfs_time`.
**Only `study.sample_count` and `study.individual_count` are true INTEGER** (and only 14 studies carry
them). Write `f.data_level = '1'`, `toInteger(i.\`13_survival_days\`) > 365`,
`ORDER BY s.sample_count DESC`.

Both mistakes are silent — neither errors, both return plausible-looking rows:
- Unquoted equality on a STRING column matches nothing: `f.data_level = 1` → **0 rows** (`= '1'` → 28,228).
- Unquoted `>` on a STRING column also yields **0 rows**: `i.\`13_survival_days\` > 365` → **0**
  (`toInteger(...) > 365` → 2,465).
- Quoted `>` compares lexicographically: `> '365'` → 2,110 but `> '99'` → **27**, because `'99' > '365'`
  as text. `ORDER BY` without conversion sorts `'995'` above `'7061'`.

If a count looks impossibly low or a max looks too small, check `valueType()` before believing it.

## 3. Closed tool catalog (truth = bio-pipeline-kg-matcher `data/csv/catalog`, rebuilt from the WDLs on 0823)

> The copies under `references/` (`tool_catalog.csv`, `knowledge_cards_map.json`, `io_slot.csv`) are a
> snapshot of that truth. `knowledge_cards_map.json` was regenerated from the 2026-08-24 delivery bundle
> by `scripts/build_knowledge_cards.py` and now covers **all 55 tools** (was 22 — every pipeline-level
> tool used to be uncarded). The Knowledge Cards are on the serving path: no card ⇒ `validate_plan` skips
> contract validation and emits no `execution_params`.

Runtime catalog: **55 tools = 12 atomic (11 orchestrable; `multiqc` is terminal-only, never orchestrated)
+ 42 pipeline + 1 task_pipeline**, 1:1 with the 55 graph `tool` nodes. Full fields (catalog_id, I/O
formats, omics, variants, slot bindings) in `references/tool_catalog.csv`; ArtifactType vocabulary in
`references/artifact_type.csv`.

- **Atomic closed set (11)**: `bwa` `fastp` `fastqc` `featurecounts` `gatk` `bcftools` `snpeff` `samtools` `star` `trim_galore` `rsem` (`multiqc` terminal-only)
- **task_pipeline (1)**: `rnaseq_singletask`
- **pipeline (42)**: `diff_expr_go` `diff_expr_kegg` `immune_infiltration_iobr` `wes_somatic_maf_landscape` `wes_somatic_pair` `survival_analysis` `tmb_survival_analysis` `her2_pfs_survival` `driver_gene_gender_analysis` `rnaseq_unsupervised_cluster` `wgcna` `wgcna_hub` `wgcna_module_trait` `cellranger_workflow` `paired_fastq_to_unmapped_bam` `cnvkit_cnv_clinical` `cox_model` `km_survival` `gsea_pathway_enrichment` `hvg_pca_gmm` `preprocess_counts` `rmats_alternative_splicing` `scrna_cell_communication` `bootstrap_stability` `gatk_germline_cohort` `manta_structural_variants` `star_fusion` `tumor_evolution_inference` etc. (full table in CSV)

Catalog rules (decide Plan shape):

- `recommendations[]` carries **business pipelines** (42 + task); `candidates[]` carries **only atomic chains that pass closed-set validation** (within the 11).
- **Non-atomized needs** (differential expression, enrichment, WGCNA, survival …) have no atomic
  expression → `candidates[]` returns `unsupported`; **never pad an atomic chain with pipeline nodes**.
  `recommendations[]` still carries the business pipeline as usual.
- **Variant binding**: `gatk` has **only** `paired` (four slots tumor_bam/tumor_bai/normal_bam/normal_bai,
  all required). `GatkWesSomaticWorkflow` is a strict tumor-normal Mutect2 — the old `single`
  (sorted_dedup_bam) entry was deleted on 0823; **there is no single-sample entry**, so a lone tumor BAM
  cannot be routed to `gatk` at all. `fastp` has single_end / paired_end variants.
  Paired tumor/normal WES must use the 4-slot variant and `find_paired_tumor_normal_samples.cypher`.
- The slot model (slot names, `builder_param` / `wdl_target` bindings) is an **execution-side contract**
  from `data/csv/catalog`; it is not in the graph. The graph only says which tools exist and how they chain.
- Data availability semantics: files precisely confirmed via Neo4j are `available`, otherwise
  `missing_from_graph`. Execution-side resources (GTF, reference genomes, indexes) are not part of
  availability judgment.

## 3.1 The bulk10 family (2026-08-24 delivery) — one contract for ten pipelines

Ten of the 38 pipelines share a single input contract and a single proven-data whitelist. They are not
ten separate cases; treat them as one.

| tool_id | docker image tag | what it produces | group labels |
|---|---|---|---|
| `de_enrichment` | `task283_de_enrichment` | DE + enrichment | yes |
| `deg_enrichment` | `task419_deg_enrichment` | DE + functional enrichment | yes |
| `deg_trend` | `task423_deg_trend` | DE trend analysis | yes |
| `gene_boxplot` | `task303_gene_boxplot` | per-gene box plots | yes |
| `stage_heatmap` | `task305_stage_heatmap` | tumour-stage heatmap | no |
| `umap` | `task383_umap` | UMAP of the expression matrix | no |
| `wgcna_module_trait` | `task294_wgcna_module_trait` | WGCNA module↔trait | no |
| `wgcna_hub` | `task313_wgcna_hub` | WGCNA hub genes | no |
| `cox_model` | `task310_cox_model` | Cox proportional hazards | no |
| `km_survival` | `task438_km_survival` | Kaplan-Meier | no |

**`taskNNN_` is a docker/Cromwell job-name prefix, not part of the tool id.** Write `cox_model` in
`steps[].tool_id`; the tag `task310_cox_model` appears only in the container reference. Writing
`task310_cox_model` as a tool_id fails closed-set validation.

**Inputs — one required file, that is all.** Each WDL declares 40–50 parameters; every one except `expr`
has a working default. Bind `expr` and stop.

- `expr` — **required**, the study-level gene **counts** matrix
  `/hpcdisk1/cbb_group/data/analysis/{STUDY}/{STUDY}-Genes-counts-1.0.tsv`.
- `sample_csv`, `individual_csv` — CNCB-native metadata at `/cbb-data/gsa/agent/{STUDY}/sample.csv`
  and `individual.csv`. **Do not write them into `inputs`, and do not query the graph for them.**
  The server derives both from the study accession in `expr`, the same way it derives the clinical
  table + sample-metainfo pair — but unlike that pair, these two CSVs are **not in the graph** at all
  (the clinical pair is; you just never have to look it up). No bulk10 study has them (only
  HRA000001 has nodes with those names) — binding them makes `validate_plan` match HRA000001's node
  by `file_name` and reject the plan with `asset file_path 与图内记录不符`.
- `case_label` / `control_label` — the four label-taking pipelines above. The server supplies the
  literals `case` / `control`; override only if the user names different groups.

**Data selection is restricted per pipeline, not per family.** The 26 proven Cromwell runs are recorded
in `references/bulk10_proven_runs.tsv` (one row per run). Each pipeline may only be paired with the
cohorts *it* has run on:

| tool_id | cohorts with a proven run |
|---|---|
| `de_enrichment`, `deg_enrichment`, `deg_trend`, `gene_boxplot`, `stage_heatmap` | `HRA003107` only |
| `wgcna_module_trait`, `wgcna_hub` | `HRA003107`, `HRA007167` |
| `cox_model`, `km_survival` | `HRA003107`, `HRA000073`, `HRA000074`, `HRA002693`, `HRA006117` |
| `umap` | all seven: `HRA003107`, `HRA000073`, `HRA000074`, `HRA000122`, `HRA002693`, `HRA006117`, `HRA007167` |

The union is seven cohorts, but **do not plan against the union** — `de_enrichment` on `HRA007167`, or
`km_survival` on `HRA000122`, are combinations that have never been run, and the server rejects them.
`HRA003107` is the only cohort every one of the ten has run on; when the user does not name a cohort,
it is the safe default. `HRA000122` is reachable by `umap` alone.

Two further studies (`HRA001272`, `HRA007413`) do have a `Genes-counts` file in the graph and will look
like valid candidates — they are not: no bulk10 pipeline has ever run on either, HRA001272's path
carries an extra `/RNAseq/` segment, and HRA007413's matrix is 1.2 MB. If the user asks for a
pipeline×cohort pair outside the table, say which cohorts that pipeline does support and offer one of
them rather than substituting silently. **`validate_plan` now checks this table on every
recommendation**, not just at submission time: an unproven pipeline×cohort pair is a violation and
sends the plan back for repair.

**Quantification flavour: counts, always.** Not a methodological inference — every proven run of all ten
pipelines used `{STUDY}-Genes-counts-1.0.tsv`. Normalisation happens inside the pipeline. Do not pick the
TPM or FPKM version for these ten.

**Outputs**, uniform across all ten: `results_tar_gz` (result archive), `output_manifest` (file
manifest), `run_summary` (run summary table).

**One per-study exception**: `cox_model` reads survival status from `native_status_source_col` and the
proven runs set it to `13_vital_status` for `HRA000073`, `HRA000074`, `HRA002693`, `HRA006117`. On
`HRA003107` — cox_model's fifth and last proven cohort — the default applies. The server fills this in.

## 4. Query cookbook

**15 official Cypher templates** live in `references/query_templates/`, use by name (all runnable as-is
via `read_cypher`). **Copy property names with exact case** — writing `t1_id` as `T1_id` does not error,
it silently returns 0 rows:

| Template | Purpose |
|---|---|
| `find_tools_by_function` | Tools by function term. Since 0826 `function` is a closed 30-term vocabulary — **match by equality on a term from §2's list**; CONTAINS still works but a partial term silently matches nothing (`'差异'` no longer hits `差异表达分析`'s node under equality, and under CONTAINS it hits only that one) |
| `find_tools_by_input_format` / `find_tools_by_output_format` | Tools by input/output format. **`bootstrap_stability`, `hvg_pca_gmm`, `multiqc` have no `input` edge in the graph** — input-format queries never find them; when needed, query by `has_function` or tool name, and do not conclude "tool not in graph" |
| `find_tool_input_output` | Single tool's I/O contract |
| `find_tools_by_modal` | Tools by modal |
| `trace_next_tool_chain` | Walk `next_tool` from a tool |
| `recommend_next_tools_via_output_match` | Downstream tools where prev output = next input |
| `trace_paths_from_input_format_to_output_format` | Feasible paths input format → output format |
| `find_t1_by_study_and_format` / `find_t1_by_modal` / `count_data_by_study` / `count_by_semantic_format` | Data file lookup & counting |
| `find_paired_tumor_normal_samples` | Per-individual tumor/normal pairing in a study (`pairable` bool) |
| `trace_sample_hierarchy` | individual → sample → run → file lineage |
| `trace_data_lineage` | T2 → `generated_from` → T1 lineage |

Standard recipes:

1. **Request → tool matching**: **start from `function`, not from `tool_name`.** Since the 0826 rebuild
   `function` is a closed 30-term vocabulary (§2) covering all 55 tools, so map the request onto one term
   and match it by equality — one query, complete recall, no keyword guessing. `我想做生存分析` → `生存分析`
   → the 5 survival tools; `做个差异表达` → `差异表达分析` → the 8 DE tools. Only when no term fits should you
   fall back to keyword OR over `tool_name`, where the naming patterns are: `deg_*` / `de_*` = differential,
   `wgcna*` = co-expression, `*survival` / `km_*` / `cox_*` = survival, `*enrichment` = enrichment,
   `*cellchat` = cell communication, `tmb_*` = tumor mutation burden. **`function` narrows to a family, it does
   not pick the member** — the family still has to be split by cohort/input/output, which is what §12.1 is for.
2. **Chain assembly + verification**: for each hop, intersect upstream `output` / `semantic_output`
   with downstream `input` formats. Report gaps honestly ("graph missing: <hop>, expected input
   <format>; suggestion <filler tool or note>") — **never fabricate a tool that does not exist**.
3. **Data selection** (English vocab; ready-made matrices live in T2).

   **Gate first — is the data already fixed?** For the ten bulk10 pipelines the answer is a lookup, not
   a search: the pipeline determines the cohort set (§3.1 table) and the file is always
   `{STUDY}-Genes-counts-1.0.tsv`. **Run no data query for them** — no `tumor_type` matching, no
   `find_t1_by_*`, no `resolve_sample_roles`. A graph search there can only produce a cohort that has
   never been run, which the server rejects. Everything below applies to the other 41 tools, where the
   cohort genuinely has to be discovered.

   - Cohort: `tumor_type` is **English Title Case** — match with `toLower(s.tumor_type) CONTAINS '<english>'`;
     Chinese matches nothing. All values re-measured on 0826 — unchanged from 0821 (20 studies): `Liver Cancer`,
     `Hepatocellular Carcinoma`, `Lung Cancer`, `Non-Small Cell Lung Carcinoma`, `Malignant Glioma`,
     `Melanoma`, `Esophageal Cancer`, `Colorectal Adenocarcinoma`, `Nasopharynx Carcinoma`,
     `Acute Myeloid Leukemia`, `Acute T Cell Leukemia`, plus one null.
     **Known trap**: liver queries with only `CONTAINS 'liver'` return HRA001748/HRA001749/HRA006499
     but **miss HRA001272** (`Hepatocellular Carcinoma`) — the very cohort with the ready TPM matrix used
     in this manual's example. Use `... CONTAINS 'liver' OR ... CONTAINS 'hepatocell'`. Lung is fine with
     one word (`Lung Cancer` and `Non-Small Cell Lung Carcinoma` both contain `lung`).
     When unsure, fetch all 20 studies' `study_accession` + `tumor_type` in **one** query (20 rows) and
     pick by eye — cheaper than keyword trial-and-error.
   - **Ready-made expression matrices are in T2** (file names contain `Genes`, e.g.
     `HRA001272-Genes-TPM-1.0.tsv`), not T1 (raw FASTQ). `semantic_format` (e.g. `TABULAR_BIO_DATA`)
     ≠ `format` / `file_format` (literal).
   - Reuse intermediates: if T2 already has the VCF/MAF/BAM, mark it "reuse" and skip upstream compute.
     Sample constraints use `tissue_type` / `specimen_type` / `gender`; pairing needs use
     `find_paired_tumor_normal_samples`.
   - **Paired analysis: cohort discovery first** — never assume a cohort is pairable; aggregate which
     studies have same-individual Tumor+Normal first. HRA016026 is exactly 350 `Tumor` + 350 `Normal`
     on 0826, but `tissue_type` graph-wide is **not** a clean binary (`Blood` 557, `Organoid` 30 — §12.4),
     so tolerate name suffixes as a fallback:
     ```cypher
     MATCH (sp:sample)-[:in_individual]->(i:individual)
     WITH sp.study_accession AS study, i,
          collect(DISTINCT toLower(coalesce(sp.tissue_type,''))) AS tts,
          collect(DISTINCT toLower(coalesce(sp.sample_name,''))) AS nms
     WHERE (any(t IN tts WHERE t CONTAINS 'tumor')  OR any(n IN nms WHERE n ENDS WITH '_tumor'))
       AND (any(t IN tts WHERE t CONTAINS 'normal') OR any(n IN nms WHERE n ENDS WITH '_normal'))
     RETURN study, count(i) AS pairable_individuals ORDER BY pairable_individuals DESC
     ```
     Pairable cohorts measured on the 0826 delivery (individuals): HRA000873 1015, HRA000021 508,
     **HRA016026 350**, HRA001272 206, HRA003107 155, HRA007169 76, HRA006499 72, HRA001749 56,
     HRA000122 42, HRA001748 30. Two changes from 0821: HRA001749 dropped 84 → 56, and **HRA000122
     became pairable** (0821 had it in the unresolvable list; 0826 gives it 245 Tumor / 42 Normal).
     Note HRA000122 is still restricted to `umap` by the fixed-data whitelist in §3 — pairable does
     not mean freely usable. Every pair here is Tumor+Normal; **no cohort pairs via Blood**. The naive form
     (`'Tumor' IN tts`) **misses HRA016026 entirely** — the third-largest pairable cohort.
     Known trap: **HRA000071's blood controls and tumor samples belong to different individuals**
     (572 samples 1:1 to 572 individuals) — usable for tumor/normal grouping (`resolve_sample_roles`
     can judge roles) but **not same-individual pairing** (`wes_somatic_pair` not applicable); say so
     honestly. Prefer HRA016026 for ready pairing (350 individuals × exactly 2 samples,
     `L####_Tumor` / `L####_Normal`, 350/350 paired).
   - **Sample roles (tumor/normal) must come from `resolve_sample_roles` — never infer from names or
     intuition**: before selecting a cohort for paired/grouped analysis (wes_somatic_pair, survival,
     grouped differential expression), call `study` mode and check `role_resolved`; cohorts with false
     cannot do pairing/grouping — report honestly. Per-file `sample_role` / `sample_role_label` use
     `records` mode.
     **Cohorts whose roles cannot be resolved (re-measured on 0826 — do not burn rounds retrying)**:
     HRA000001 (557, all `Blood`), HRA000074 (693, all `Tumor`), HRA002693 (655, all `Tumor`),
     HRA006117 (835, all `Tumor`), HRA005191 (243, all `Tumor`).
     **The 0821 reason for these is now wrong and the old wording has been removed**: it read
     "543/693 no `tissue_type`", but 0826 filled every null graph-wide (see §12.4), so these samples
     all *have* a `tissue_type` — they are single-valued cohorts with no counterpart arm, which is a
     different fact with the same consequence. HRA000122 is **no longer on this list** (now 245 Tumor /
     42 Normal → 42 pairable individuals). Do not re-query hoping the nulls were the problem; the
     cohort simply has one arm. Tell the user there is no control arm, or switch to a pairable cohort above.
     **This blocks per-sample pairing only.** Cohort-level analyses on these same cohorts (differential
     expression, enrichment, clustering, immune deconvolution, survival) run off the aggregate matrix /
     MAF and do their own grouping internally — never downgrade one to `no_candidate` just because
     tumor/normal roles are unresolvable, and do not call `resolve_sample_roles` to pick an aggregate
     file in the first place.
   - **Cohort sample lists come from `sample` nodes**: `MATCH (sp:sample) WHERE sp.study_accession = '<HRA*>'`
     (equivalent to the `study<-individual<-sample` traversal). **Do not count samples via
     `(T1)-[:in_sample]->(sample)`** — that only sees file-attached samples and silently drops the rest
     (HRA006117 has 835 samples; via files only 570 remain).
   - **Two kinds of `sample_accession = null` on files — do not conflate**: **aggregate files**
     (expression matrices / MAF / clinical tables / MetaInfo) are cross-sample by nature, null is normal;
     **run-organized fastq** (`data_level=1`) should have samples, but that class is basically
     zero — `sample_accession` sits directly on T1 (no run hop). Re-measured on 0826 and unchanged
     from 0821: 28,184 of 28,229 T1 have `in_sample` edges, the remaining 45 are all aggregates.
     **Do not reject cohorts on the pre-0821 conclusion that runs orphan files.**
     Judge gaps **only** by `resolve_sample_roles(study=...)` → `file_coverage.t1_files_unlinked`
     (files truly lacking `in_sample` edges; e.g. HRA000087 2/3108, HRA001272 2/2362, all aggregates).
     Its sibling `runs_without_sample_node` stays large (1492/1553, 482/1180) and is a **diagnostic
     field, not a gap** — each sample node records only one run, so run-based back-lookup never
     reconciles; judging cohorts by it kills good cohorts. If `t1_files_unlinked` really is large, output
     `missing_from_graph` honestly — **never guess sample ownership from file names or order**.

## 5. Planning pipeline (5 steps)

1. **Parse the request**: analysis type, modal, target artifacts (ask first if unclear: existing data
   format / cohort / grouping).
2. **Match** (recipe 1): candidate tools + functions + formats; decide business pipeline vs atomic
   chain (catalog rules, §3).
3. **Assemble & verify the chain** (recipe 2): data → preprocessing → alignment → quantification/variant
   → downstream; annotate tool, input format, output format, verification point per hop; list gaps honestly.
4. **Select data** — **bulk10 first**: if the chosen tool is one of the ten (§3.1), the cohort comes from
   its proven-run row and the file is `{STUDY}-Genes-counts-1.0.tsv`; write it down and move to step 5
   with **zero queries**. Otherwise use recipe 3: cohort + format + sample constraints + pairing; give
   file counts, sources, `file_path`, availability flags.
5. **Output**: **tool-chain/v2 JSON** (§9). Keep total tool calls within the round budget (§6).

## 6. Efficiency discipline (HARD — round budget)

**Target: ≤ 3 tool rounds, ≤ 6 queries.** Rounds are the only wall-clock cost (one round = one full
model inference, tens of seconds); queries are nearly free (< 0.5s each). Consequences:

1. **List-then-fire (most important)**: before each round, write down every question you still need
   answered; fire **all** whose parameters are known right now **in that same round** — 2–4 calls per
   round is the norm, 1 call is the exception. Spreading 6 queries over 6 rounds is 2× slower than
   3 rounds. Never interleave "query → look → query again" when the queries were knowable upfront.
   **Snapshot-first**: tool matching and cohort picking need **no query at all** — use the measured
   snapshot tables in §12 (they are whitelist sources); spend `read_cypher` only on file-level data
   (T1/T2 inventories, paths) and freshness checks.
   Typical parallel bundles:
   - "tools by function" + "cohorts by cancer type" — independent, fire together in round 1;
   - "T2 ready matrices of the cohort" + "`resolve_sample_roles` for the cohort" — both depend only on a
     known study_accession, fire together;
   - "`validate_atomic_chain`" + "upstream T1 file lookup" — fire together.
   Only true serial dependencies (need study_accession to fetch its files) may split rounds.
2. **Standard trajectory — 3 rounds** (deviate only with a reason):
   - R1: match tools and pick cohort(s) **from the §12 snapshots (no query)**; then issue **one**
     `get_study_overview(study)` per chosen cohort **plus one** `read_cypher_batch` with every targeted
     query the overview cannot answer (e.g. specific T1 FASTQ lists, a tool's I/O contract),
     **plus** `validate_atomic_chain` when an atomic chain is planned — all in the same round.
   - R2: compose the Plan from R1 evidence — judgment fields only (§9) — then call `hydrate_plan`
     and `validate_plan` **in the same round** (hydrate first; validate the hydrated Plan).
   - R3: if `grounded=true`, output the final JSON immediately. (R4 only to re-validate after fixing
     listed violations from existing evidence, §7.4.)
   Rejection cases (§8) answer in **1 round, zero tool calls**.
3. **No `get_schema`**: the graph model is fully listed in §2; use targeted queries for specific fields.
4. **One merged query beats several small ones**: e.g. candidate cohorts + per-cohort T1/T2 inventory in
   a single statement:
   ```cypher
   MATCH (s:study) WHERE toLower(s.tumor_type) CONTAINS 'liver'
   OPTIONAL MATCH (f:T1)-[:in_study]->(s) RETURN s.study_accession, s.sample_count,
     collect(DISTINCT f.format) AS t1_formats LIMIT 10
   ```
   Merging (one statement, several facts) and parallel issuing (several statements, one round) are
   independent and stack.
5. **No cohort polling**: pick cohorts from the measured tables in §4.3 (pairable cohorts, unresolvable-role
   cohorts, tumor_type spellings). Do not probe studies one by one; if probing is unavoidable, batch all
   candidates' calls in one round.
6. **Query once, then reuse**: same tool contract / same cohort is queried at most once; later steps cite
   the earlier result. Never repeat a call with identical arguments.
7. **Converge**: once tools and data status are clear, stop querying and output the Plan. If a query
   returns empty, check keyword language (Chinese/English) and target table (T1/T2) before rewriting —
   never re-fire the same failing query. **Hard ceiling: 6 query rounds.** If you still cannot separate
   same-family tools after that (e.g. the survival family `km_survival` / `cox_model` /
   `survival_analysis` / `tmb_survival_analysis` overlaps, or an out-of-catalog need like scRNA
   clustering), pick the best-supported option, state the ambiguity in `match_note`, set
   `selection_status` accordingly (`unsupported` when nothing fits), and output — do not keep querying.
8. **After `validate_plan` returns `grounded=true`, output the final JSON immediately** — no further tool
   calls; re-validating does not improve the answer, it only burns rounds.
   **Budget: at most 2 `validate_plan` calls per session** (one check + one re-check after fixing
   violations) and **at most 1 `validate_atomic_chain` call per final chain** — assemble the chain from
   the §12.1 I/O formats and `next_tool` adjacency first, validate the finished chain once.
   **Never validate drafts mid-exploration**: `validate_plan` runs only when the Plan is final — a
   grounded draft you then keep editing wastes a full round twice.

## 7. Grounding discipline (top priority: answers come only from this manual and graph query results)

Your internal bioinformatics knowledge may **only** be used to understand user intent and decide what to
query; answer content must be fully grounded:

1. **Noun whitelist**: every `tool_id` / `pipeline_id`, cohort id (HRA*), file name, file path, format
   name, and sample id appearing in the answer/Plan must come **verbatim** from this manual (including
   `references/` files) or this session's tool returns. Not sure a noun was actually seen → re-query to
   confirm, or do not use it.
2. **No knowledge completion**: tools/data/chain hops not found in the graph → honestly output
   `missing_from_graph` / `no_candidate` / `unsupported`. Never complete from training knowledge — even
   if you "know" a tool really exists (e.g. DESeq2, Seurat): if it is not in the closed catalog, it must
   not appear in the answer. **绝不虚构** graph-unverifiable content.
3. **Traceable evidence**: `match_note` / `match_reason` must map to an actual query result; sample roles
   come only from `resolve_sample_roles`; paths come only from graph records or `validate_execution_chain`'s
   `execution_params`.
4. **Self-check before output**: run `hydrate_plan` on the finished Plan, then submit its output to
   `validate_plan`; on `grounded=false`, fix the
   listed `violations` **from already-fetched evidence** (re-query at most the specific violated item —
   do not restart exploration), then re-validate once, until `grounded=true`.

## 8. Rejection discipline (judge first, reject on hit, call no query tools)

- **Off-topic** (chit-chat, coding help, study-abroad/life consulting — anything unrelated to
  bioinformatics analysis planning) → output a single object:
  `{"status":"rejected","reason":"off_topic: <one-line note>"}`.
- **Patient privacy** (individual-level clinical information: a patient's or patients' age, sex, race,
  family history, smoking history, blood counts, pathological stage, vascular invasion, treatment,
  survival time …, or requests like "list X for all patients") → output a single object:
  `{"status":"rejected","reason":"privacy: patient-level clinical data is not provided; aggregate statistics only"}`.
  Legitimate aggregate needs ("how many samples have survival data") are served normally with
  count / IS NOT NULL aggregate queries.
- Server-side backstop: `read_cypher` refuses non-aggregate queries over `individual`'s **`01_`–`13_`
  numbered-prefix** clinical properties (only `00_*` operational identifiers pass). When you receive that
  refusal, do not rewrite the query to bypass it; explain the privacy boundary to the user honestly.
- **Causal claims** ("does feature X cause lung adenocarcinoma?", "can you infer the complete causal
  mechanism of T-ALL from this data?", "prove A drives B") → **give no recommendation at all**. Every
  tool in the closed set analyses observational data; it yields association and covariation, never
  causation. The correct answer states that epistemic boundary, then says what *is* obtainable
  (differential expression, co-expression modules, survival association) and what causal inference would
  require (intervention experiments, time-series cohorts, Mendelian-randomisation data). **This outranks
  the "if the tool exists, rank1 is mandatory" rule below** — answering a causal question with a `wgcna`
  recommendation implies the question is answerable from the data at hand, and it is not.

**Only those two categories are refusals. `unsupported` / `no_candidate` are not refusals, and they are
never a licence to skip the answer** — both still require a top-level `answer` (§9), and that answer must
give the **nearest feasible path**. The most common measured over-refusal is dismissing a **modality
mismatch** in one line: "unsupervised clustering on WES data" gets judged `unsupported` on the grounds
that every clustering pipeline in the closed set consumes an expression matrix, and the answer stops
there. The correct response explains that WES must first go `fastp` → `bwa` → `gatk` / `bcftools` to
yield variants, that only after matricisation can it reach `hvg_pca_gmm` /
`rnaseq_unsupervised_cluster`, and precisely which step is missing. Before writing `unsupported`, answer
three questions: (1) is there a same-family pipeline for a different modality? (2) does it become
feasible when split into two stages? (3) is there another route to what the user actually wants
(cluster assignments / enrichment results / survival curves)? Only when all three come back empty is
`unsupported` correct, and even then `answer` must state *what is missing*, not merely "unsupported".

**`no_candidate` means "no tool", never "no data".** Keeping those two apart matters: conflating them is
the single largest source of residual error in the measured set. "Cell–cell communication analysis in an
APL cohort" has tools — `scrna_cell_communication`, `breast_cellchat`, `immunotherapy_cellchat` — the
graph merely holds no single-cell cohort for that cancer type; answering `no_candidate` with empty
recommendations reports a *data* gap as a *tool* gap. **If the tool exists, rank1 is mandatory**: keep
the status at `ok`, put "no matching cohort/file in the graph" in `match_note`, and name a substitutable
existing cohort in `answer`. `no_candidate` plus empty recommendations is correct only when not one of
the 55 closed-set tools can do the thing at all. Input-modality mismatches follow the same rule — "I have
WES and want cluster assignments" leads with `rnaseq_unsupervised_cluster`, "somatic calling starting
from a MAF" leads with `wes_somatic_pair`, and `match_note` / `answer` carry the gap (expression
quantification missing in the first; MAF is the end product of calling, so the second must restart from
FASTQ). Empty recommendations leave the user with nothing executable.

## 9. Output contract: tool-chain/v2 (front-end truth = bio-pipeline-kg-matcher's pipeline_router)

**Hard rule — violation counts as task failure**: the final answer must be **exactly one tool-chain/v2
JSON object** — no prose, no markdown fences, no multiple candidates, no text before or after the JSON.

**`recommendations` holds at most one entry** (strict top-1; `recommendations[0]` *is* the
recommendation). When a second pipeline is worth mentioning, put the trade-off into `match_note` as one
sentence rather than emitting a second entry — measured across 165 cases, each extra recommendation adds
~1,700 characters and ~8 seconds to the answer (one recommendation averages 15.7 s, two average 24.7 s)
and the execution end never consumes anything past index 0. `candidates[]` is filled only when an atomic
chain is possible.

**When there is no recommendation to give, the top-level `answer` field *is* the deliverable.**
When `selection_status` is `unsupported` / `no_candidate` / `missing_from_graph`, `recommendations` may be
empty; **every other status requires a rank-1 entry**. But **an empty `recommendations` does
not license an empty answer**: in that case you must write a top-level `answer` — two to five sentences,
in the user's language, naming every relevant `tool_id`, semantic format and study accession explicitly.
`match_note` lives *inside* `recommendations[i]` and therefore has nowhere to live when the list is empty;
do not park the answer there, and do not invent `note` / `summary` / `explanation` — the front end and
`validate_plan` recognise only `answer`. Shipping an empty shell (no recommendations, no answer) answers
nothing and is reported as a grounding violation. Human-readable fields (`answer`, `match_note`) may be
written in the user's language.

**Every question ships a pipeline — `information` is retired.** Questions *about the tools* used to be
answered with `selection_status: "information"` and an empty `recommendations`; that route is gone.
Answer the property in `answer` **and give a rank-1 recommendation as well**. What it cost: across 450
graded cases, 73 (16.2%) returned `information` with empty `recommendations`, and in 66 of those the
tool named in `answer` was the correct one — the model got it right and the execution end still received
nothing submittable. The server now rejects an empty `recommendations` unless the status is
`unsupported` / `no_candidate` / `missing_from_graph`.

**Where rank-1 comes from, by question shape** (the old "does it state an analysis goal" test no longer
decides anything — *every* question gets a rank-1):

- **The question names a tool** ("which input formats does fastp take", "can A's output feed B", "how do
  A and B differ") → rank-1 is that tool; for comparisons take the one **mentioned first** and name the
  other in `match_note`.
- **The question gives only an analysis goal** ("I want to do X", "to do X", "what data and which tool
  does X need", "…what are they respectively") → rank-1 is the closest pipeline in the closed set for X.
  **A "what is it / what are they respectively" phrasing does not change this** — the question asks
  *what to use in order to accomplish X*, not for tool metadata, and that is exactly where the 73 cases
  above went wrong. A follow-on "which candidate tools are there / how do their inputs and outputs
  differ" still gets rank-1, with the comparison in `match_note`.
  **When the goal is not achievable as stated, still lead with the nearest viable pipeline** and spell
  out the missing step in `match_note` / `answer` — "unsupervised clustering on BAM" recommends
  `rnaseq_unsupervised_cluster` while noting that quantification to counts must come first. Only when
  not even a nearest candidate exists may `recommendations` be empty with `unsupported`.
  **But "lead with a rank-1" never means forcing one that cannot run**: when the pipeline exists and it
  is the user's *cohort* that falls outside its proven list, switch to one of the cohorts that pipeline
  does support (swap the `assets` too) and say so in `match_note` — "this pipeline has only run on
  A/B/C, using A". Do not leave the original cohort in place, and do not tell the user to change cohorts
  to suit the tool.

**"Always lead with a rank-1" has exactly one exception: what the user handed you is not any
pipeline's primary input.** For the two shapes below, return empty `recommendations` with a
top-level `answer` stating what is missing. **Do not force a rank-1.**

- **The user holds only companion metadata, no primary analysis datum.** "I have the study metadata",
  "for level-1 file metadata" — clinical tables, sample-metainfo tables and level-1 file metadata are
  all companion files; without a primary datum (expression matrix / MAF / FASTQ) no pipeline in the
  closed set can run. **Do not pick a "most generic consumer" as rank-1** — the two failures forced
  `immune_infiltration_iobr` and `fastqc` respectively, while their own `match_note` already said
  "metadata is not the primary datum, an expression matrix is required". Put that conclusion in
  `answer`: name the companion file, say which kind of primary datum it needs, then return empty
  `recommendations`.
- **The user holds a companion or intermediate artifact that no closed-set pipeline takes as its
  primary input** — "BAM index data", "variant statistics report", "splice-junction table".
  **Swapping the input for a different file from the same cohort and recommending anyway is just as
  wrong** — in the measured run the model wrote in its own `answer` that
  `rmats_alternative_splicing` "does not consume a splice-junction table, it needs an alignment BAM",
  then recommended it anyway "using the same cohort's alignment BAM"; two others pushed a BAM index
  to `gatk` (because its four slots include `tumor_bai`) and read "variant statistics report" as MAF
  to push `wes_somatic_maf_landscape`. The test is **whether this file is itself some pipeline's
  primary input**, not whether some other file in the same cohort could make a pipeline run.

  Both return empty `recommendations` with **`selection_status: unsupported`**. **Do not use
  `no_candidate`**: the server has a deterministic fallback for "`no_candidate` + empty
  recommendations" that scans `answer` for the first closed-set tool id and promotes it to rank-1
  (it exists to catch plans that claim nothing matches while naming a tool in the same breath — see
  `mcp_light_server.py`). Both cases must name tools in `answer` to explain what would make the
  analysis runnable, so `no_candidate` gets silently overturned and the rejection is lost — that is
  exactly how 6 cases were lost in the measured run.

  The test is **whether the file the user handed you is itself some pipeline's primary input**, not
  whether some other file in the same cohort could make a pipeline run. These two cover that one
  thing and nothing else — **do not extrapolate**. Any question that names a project, cohort or
  cancer type, whose goal is achievable, and merely asks you to pick a tool or a dataset, **still
  gets a rank-1**, however imperfect the data. In particular, **"the project name is not in the
  graph" is not a reason to refuse** — see the next bullet; that misjudgement alone cost 10 ordinary
  questions in one measured run.

- **A named project is present until proven otherwise, and you look it up on `project.project_name`.**
  Project names live on the `project` node's **`project_name`** property, **not on `study.title`** —
  `study.title` is mostly a DAC or institution label ("DAC for AM data", "Shanghai Institute of
  Hematology,", "CASPMI", "CBB"), and HRA000071's title is NULL outright, so matching a project name
  against `study.title` returns 0 hits every time. The correct lookup:

  ```cypher
  MATCH (p:project) WHERE p.project_name CONTAINS '<distinctive fragment>'
  RETURN p.project_name, p.study_accession, p.tumor_type
  ```

  `study_accession` is the cohort id; a few hold two joined by `;` (`HRA001748;HRA001749`,
  `HRA007167;HRA007169`). `project.title` is NULL for all 18 — do not read it. If one fragment misses,
  try a shorter one; **do not conclude the project is absent and refuse**.

- **Giving a rank-1 does not mean `selection_status: ok`.** The status reports **whether the data side
  is complete**, not whether you produced a recommendation. When the tool exists but the cohort the
  user named lacks the primary datum that pipeline needs, **still give rank-1, and set
  `missing_from_graph`** — and leave `assets` empty rather than attaching a file that does not satisfy
  the pipeline. Eleven measured cases failed exactly here: the `answer` was right every time ("the CGGA
  RNA-seq cohort has no VCF/MAF in the graph", "CASPMI is all Blood with no tumour samples"), but the
  status said `ok`, and one attached an `HRR000001.vcf.gz` it had just declared nonexistent.
  Write `ok` only when the graph **positively confirms** the data that pipeline consumes (§3).

  The question shapes below are *asking for a completeness verdict*. When the verdict is "not
  complete", the status is always `missing_from_graph` (rank-1 still given); writing `ok` is a wrong
  answer:
  - "Can project X provide **complete** data and tools for Y?" / "To do Y, are project X's data and
    tools **all in place**?" — three steps, and **do not stop after the first hit**:
    ① `project.project_name` → `study_accession` for the cohort id;
    ② take **every** semantic format in Y's pipeline card input column (§8.1) — "every", most
    pipelines declare more than one;
    ③ look each one up in that cohort's `d.semantic_format`; **any one missing means not complete**,
    and `answer` names each missing item.
    All 8 measured failures stopped at step ② with a single format: asked whether
    `cellranger_workflow` was in place, whose card reads `RAW_SINGLE_END_FASTQ,DNA_GENOMIC_ALIGNMENT_BAM`
    — **two** entries — the model found 160 FASTQ pairs in HRA001748, wrote "data complete / status ok",
    and never checked `DNA_GENOMIC_ALIGNMENT_BAM`, of which that cohort holds none. Likewise HRA000021
    for "variant filtering": it has 1016 `DNA_ALIGNMENT_BQSR_BAM`, but `bcftools` wants
    `DNA_VARIANT_VCF_GENERAL` + `DNA_VARIANT_INDEX_TBI` and has neither. In short: **"has data" is
    not "has the several things this pipeline needs".**
    (This does not conflict with §4's "an empty query is not grounds for `no_candidate`": that rule
    governs **picking a pipeline** — still give rank-1; this one governs **reporting status** — if
    something is missing, write `missing_from_graph`. Two different things.)
  - "Does project X meet the conditions for recommending data and tools for **survival analysis**?" —
    survival analysis needs a registered cancer type, and the test is **`project.tumor_type` being
    null** (7 of the 18 projects are: CGGA-WES, CGGA-RNA-seq(325), "Single-cell RNA analysis…",
    "Multi-center RNA sequencing…", "Aging and leukemia", "Multi-omics research of AML",
    "Multi-omics Landscape of CNS Tumors"). Null means the condition is not met. **Do not substitute
    `study.tumor_type`** — 19 of 20 cohorts carry it (only HRA000001/CASPMI is null), so using it
    always misjudges these as "met".
  - "Which data can tool X process?" — put the attributes in `answer` and give X as rank-1, but when
    the graph holds **no file at all** that X can consume (`multiqc`, `bootstrap_stability`,
    `hvg_pca_gmm`), the status is `missing_from_graph`, not `ok`.

  All of the above is also written into the output contract at the end of the system prompt
  (`web/server.py`); the two must stay in sync.

- **Two analyses at once** ("immune infiltration + WGCNA") → pick the primary leg for rank-1 and name
  the other in `match_note`. **When two analyses are named side by side, rank-1 is the one mentioned
  first in the question** ("complete both alternative-splicing analysis and somatic variant calling" →
  rank-1 is the splicing pipeline). This ordering is a hard rule; do not reorder by which leg feels more
  upstream or more fundamental.

**Rank-1 ordering for broad controlled function words (hard rule — follow the table, do not pick your
own).** A few controlled words carry a large slate of tools, and "closest match" cannot separate them:
in testing, one identical question — "the goal is visualisation and reporting" — returned `umap`,
`deg_trend` and `gene_boxplot` across three runs, and another returned `bcftools` while its own `answer`
argued that only multiqc is report-centric. When you hit one of these words, take rank-1 from the table:

| Controlled word | Default rank-1 | Deviate only when |
|---|---|---|
| **Visualisation & reporting** | `umap` | Cohort is `HRA003107` and the question asks for boxplots → `gene_boxplot`, or stage heatmaps → `stage_heatmap`. **`multiqc` is whole-pipeline QC aggregation and `bcftools` is VCF filtering — neither answers this word**, unless the question explicitly says "QC report aggregation", which gives `multiqc` |
| **Functional enrichment** | `diff_expr_go` | Question names KEGG → `diff_expr_kegg`; names GSEA / pre-ranked / whole-gene ranking → `gsea_pathway_enrichment`; names cohort `HRA003107` → `deg_enrichment` |
| **Differential expression** | `diff_expr_go` | **Data is single-cell (Seurat RDS / scRNA cohort) → `celltype_case_control_de`; never hand a single-cell cohort the bulk `diff_expr_go`**; T-cell pre/post intervention → `tcell_intervention`; cohort `HRA003107` with a trend request → `deg_trend` |
| **Survival analysis** | `km_survival` | Multivariate / covariates / hazard ratios → `cox_model`; a MAF in hand or TMB in the question → `tmb_survival_analysis`. **`her2_pfs_survival` is only for questions naming HER2 or PFS, and `survival_analysis` only for "mutation status of gene X vs PFS"** — both are PFS-specific and must not stand in for a general OS survival analysis |
| **Co-expression network** | `wgcna` | Cohort is `HRA003107` / `HRA007167`: hub genes → `wgcna_hub`, module-trait association → `wgcna_module_trait` |
| **Expression quantification** | `featurecounts` | **When the preceding chain step is `star`, always use `rsem`** (STAR emits a transcriptome-coordinate BAM; featureCounts only accepts a genome-coordinate BAM), i.e. the `表达定量` chain is fixed as `star`→`rsem`; also use `rsem` when the question asks for TPM/FPKM or allele-level quantification. The two are mutually exclusive — do not chain them. **Give `rnaseq_singletask` only when the question explicitly asks for the whole raw-FASTQ-to-matrix pipeline** — a single-step phrasing like "my goal is expression quantification" always gets the atomic tool |
| **Somatic variant calling** | `gatk` | No alternatives. **`wes_somatic_pair` is the full paired-WES pipeline (FASTQ → alignment → Mutect2) and is only correct when the question explicitly says "whole pipeline / tool chain / starting from raw data"**; "which tool completes somatic variant calling" is always `gatk` |

**Single step ≠ whole pipeline (the general rule behind those two rows).** When the controlled term
names **one stage of a pipeline** (expression quantification, somatic variant calling, post-alignment
processing and deduplication, …), rank1 is the **atomic tool that performs that stage**. One-stop
pipelines like `rnaseq_singletask` and `wes_somatic_pair` cover an **entire chain** and may take rank1
only when the user explicitly wants the entire chain ("starting from FASTQ", "the whole workflow").
To point out that a one-stop option exists, say so in one sentence in `match_note` — do not let it
displace rank1.

**The default cohort when none is named is a hard rule too**: all ten bulk10 pipelines default to
`HRA003107`, the only cohort every one of them has run on. Do not re-pick by "largest sample count" —
that is how runs landed on `HRA001272` (19 times) and `HRA006117`, and no bulk10 pipeline has ever run
on `HRA001272`, so `validate_plan` rejects it outright. Non-bulk10 pipelines pick from the cohort table
in §8.2; either way, note in `match_note` that the user named no cohort and X was chosen as the proven
default.

Where tool properties are looked up (they go into `answer`; **they do not excuse dropping rank-1**):

| Question shape | Source of truth | `answer` must state |
|---|---|---|
| "which **input** formats does X take", "which tools accept F as input" | §12.1 column 4 — **zero queries** | every input format of X; or each matching tool, listed in full |
| "which **output** formats does X produce", "which output formats differ between A and B" | §12.1 has **no output column** — query the graph | each side's outputs, then the intersection / difference |
| "can X's output feed Y", "what must line up when chaining A then B" | X's outputs ∩ Y's inputs | the joining semantic format by name, or exactly which link is missing |
| "designing a workflow from A, B, C — what is redundant, what is missing" | §12.1 summaries + the chaining query above | each tool's position in the chain, then the duplicates and the gaps |

```cypher
// Tool I/O. tool_name is exactly §12.1's first column (55/55 match); OPTIONAL keeps the row
// alive when a tool has no output edge at all.
MATCH (t:tool) WHERE t.tool_name IN ['gene_boxplot','ipf_trajectory_regulon']
OPTIONAL MATCH (t)-[:input]->(i:format) OPTIONAL MATCH (t)-[:output]->(o:format)
RETURN t.tool_name, collect(DISTINCT i.format), collect(DISTINCT o.format)
// Can X's output feed Y? Returns the joining formats; an empty array means they do not connect.
MATCH (a:tool {tool_name:'bwa'})-[:output]->(f:format)<-[:input]-(b:tool {tool_name:'gatk'})
RETURN collect(f.format)
```

**An empty result is itself the answer.** "Which tools output FASTQ?" returns zero rows; the correct
response is "no tool in the closed set produces FASTQ — it is an upstream input only", not an empty shell
and not another round probing a different predicate.

**`read_cypher` row cap (affects conclusion correctness)**: at most **500 rows** per call. When exceeded,
the return carries `truncated: true` and `row_count` — **what you hold is then a truncated sample, not the
full set**: never conclude "there are N in total / all are / there is no other" from it. For totals re-query
with `count(...)` / aggregation; for details add stricter filters (study accession, format, data_level).
Only results without `truncated` are complete result sets.

**Naming contract (Knowledge Card alignment)**: an atomic tool's `tool_id` must use the Knowledge Card's
`meta.id` (e.g. `bwa_mem_paired`, not `bwa`); `tool_chain.inputs` and output references use card-defined
I/O names (e.g. `read1` / `aligned_sam`). Mapping in `references/knowledge_cards_map.json` — **all 55
tools are carded now**; only these nine have a `meta.id` different from the graph `tool_id`: `bcftools`,
`bwa`, `fastp`, `featurecounts`, `gatk`, `rsem`, `samtools`, `snpeff`, `star`. For the other 46 the two
are identical, so pipeline-level tools keep the graph tool_id as written.

**Author judgment fields only — `hydrate_plan` fills the rest.** Do not hand-write any field the
catalog/graph already knows; `hydrate_plan` fills them deterministically and overwrites what you wrote
with the graph's own facts, so authoring them only costs generation time and invites fabrication. Leave out
`match_id` / `rank` / `source` / `reference_case_id` / `recommendation_count` / `candidate_count` /
`planner_metadata` / `data_matcher_mode` / `mcp_timing_ms`; inside `tool` write only `tool_id`
(catalog_id, tool_kind, name, description, inputs, outputs are filled); inside each asset write only
`file_name` and `match_reason` (**never write `file_path` from memory**); inside `candidates[].tool_chain`
write only each step's `tool_id`. What you must supply: `schema_version`, `selection_status`, `intent`,
the top-level `answer` whenever `recommendations` is empty,
and per recommendation `pipeline_id`, `match_note`, `data.assets[].file_name` + `match_reason`, plus the
tool_chain ordering.

**Do not forget the chain.** When the question says "tool **chain**", "which **pipeline**", "what is the
**workflow**", or when the goal inherently needs an upstream step (raw FASTQ before alignment needs QC /
adapter trimming; expression quantification needs alignment first), **list every step's `tool_id` in
execution order in `candidates[0].tool_chain`** — returning a lone rank-1 is an incomplete answer.
rank-1 is the chain's **principal step** (for an alignment question that is `bwa`/`star`, not `fastp`),
with the upstream steps in the tool_chain: `DNA alignment` → `fastp` → `bwa`; `RNA alignment` →
`trim_galore` → `star`; `expression quantification` → `star` → `rsem`. The refusal and status rules in §7
govern **whether to recommend and what status to write**; they say nothing about **whether to expand the
chain — always expand it**. (Measured: after a long block of refusal rules was appended to the end of the
output contract, subsequence hits on the 9 chain cases fell from 6/9 to 1/9 as the model collapsed every
answer to a single rank-1. This paragraph is what pulls it back.)

**Assets: supply the primary datum only.** The primary datum is the pipeline's core input — the
expression matrix, the MAF, or the FASTQ pair. `hydrate_plan` completes the rest deterministically:

- When the pipeline needs a clinical table, the study's clinical table **and** its
  sample-metadata table are appended (`METADATA_SAMPLE_INFO` is the sample↔patient join table — without
  it the clinical fields cannot be attached to the matrix/MAF, and the graph always delivers the two
  together, one of each per study). The trigger is "the graph's io declaration lists
  `CLINICAL_DATA_EXCEL` **or** the delivery card declares `clinical_xls`+`metainfo_xlsx` (the other
  spelling is `clinical_file`+`metainfo_file`) as required params" — that covers
  `driver_gene_gender_analysis`, `wgcna`, `her2_pfs_survival`, `immune_infiltration_iobr`,
  `survival_analysis` and `tmb_survival_analysis`, whose graph-side io declarations do *not* mention
  `CLINICAL_DATA_EXCEL` at all. For those six, give the primary datum as the only asset; both tables
  and their `execution_params` entries are filled server-side. **So do not write them — and do not
  query for them either.** The
  server fetches both from the study accession alone; you never need to know their file names or whether
  they live on `T1` or `T2`. **Hunting for them is the single largest waste of wall-clock in this
  project** — probing `T2` by `format`, coming back empty, then re-probing `T1` by `strategy`, at tens of
  seconds a round.
- The same applies to the bulk10 family's `sample_csv` / `individual_csv` (§3.1) — server-derived from
  the study accession, never written and never queried.
- The expression matrix is normalised to the pipeline's default quantification flavour. A study's FPKM,
  TPM and counts versions have identical graph properties (`semantic_format` = `TABULAR_BIO_DATA`,
  `data_level` = 2) — only the file name distinguishes them, so the pipeline decides, not the caller.
  The **first** flavour named in the catalog description wins ("适用于 FPKM/TPM 定量数据" → FPKM). When
  the description names none: the **WGCNA family and the whole bulk10 family (§3.1) take raw `counts`**;
  `gsea_pathway_enrichment` needs a ranking over all genes and takes **TPM**.
- When the required semantic format has **exactly one** study-level delivery file in that cohort — a name
  beginning `HRA<digits>-`, e.g. `HRA007169-SomaticSNV-1.0.maf` — a per-sample file you picked
  (`HRR1725089.maf`) is swapped for it. "Exactly one" keeps this safe: FASTQ has no study-level file so
  nothing moves, expression matrices have three so the flavour rule decides instead. Only MAF and
  somatic CNV land here.

What is not completed for you: the primary datum itself. It must be a file that actually exists in the
graph, and `assets` must be non-empty whenever `selection_status` is `ok` — if the graph holds no usable
data, say so with `no_candidate` plus a `match_note`, rather than shipping a recommendation with no data.
This holds even when the request names no cohort: locate one by cancer type / omics, filter by
`semantic_format`, and take the **first file under `ORDER BY n.file_name`** as the representative sample
(an f1/r2 pair for paired-end). Order explicitly — a bare `LIMIT` resolves the same question to different
files on different runs. When a cancer type spans several cohorts and the user named none, take the one
with the most samples, so the same question always resolves the same way:

| Cancer type | Expression / raw | Mutation (MAF) |
|---|---|---|
| Glioma | **HRA000074** (693, over HRA000073's 325 and HRA000071's 572) | **HRA000071** — the only glioma cohort with a MAF, exactly one file `HRA000071-SomaticSNV-1.0.maf`. HRA000073/74 have none, so an Oncoplot request resolves here and must **not** be answered `no_candidate` |
| Liver | **HRA001272** (698) — mutation, expression and raw alike | HRA001272 |
| Melanoma | HRA007167 | HRA007169 |
| Esophageal | HRA003107 | — |
| AML | HRA006117 | — |

Graph-wide, only seven cohorts carry any MAF: HRA000873, HRA016026, HRA001272, HRA006499, HRA001749,
HRA007169 and HRA000071 (the last cohort-level only; the others also carry per-run `HRR*.maf`).

Three cohorts carry single-cell data: **HRA001748** (10x, liver cancer, 320 paired FASTQ files named
like `HRR572934_f1.fq.gz` / `HRR572934_r2.fq.gz` — the default cohort for any 10x / CellRanger
request), **HRA005191** (NSCLC, 484 T1 files) and **HRA000087** (Smart-seq2, nasopharyngeal carcinoma;
14 T2 files, no T1). The 0821 mislabelling of HRA001748 / HRA000087 as `bulk_RNA` **was fixed in the
0826 delivery**, so `strategy = 'sc-RNA'` is now trustworthy: it returns HRA005191 484 + HRA001748 320
on T1, and HRA005191 289 + HRA001748 236 + HRA000087 14 on T2. Filtering on it no longer misses the
10x cohort.

**`RAW_SINGLE_END_FASTQ` matches zero files in the whole graph.** `cellranger_workflow` declares it
as an input, but 10x raw reads are stored as ordinary paired-end FASTQ — `RAW_PAIRED_END_R1_FASTQ` /
`RAW_PAIRED_END_R2_FASTQ`. So filtering by a pipeline's *declared* input format comes back empty and
**must not be read as `no_candidate`** — select single-cell raw data by cohort accession plus the
paired-end FASTQ semantic formats.

**`sample.strategy` is semicolon-multi-valued with unstable ordering** (`WES;bulk_RNA` and
`bulk_RNA;WES` both occur; single-cell samples read `bulk_RNA;sc-RNA`) — always match it with
`CONTAINS`, never `=`, or you drop all 242 HRA005191 single-cell samples at once. Only T1/T2 carry a
single-valued `strategy`, drawn from six values: bulk_RNA, WES, WGS, sc-RNA, Clinical, Meta. As of the
0821 delivery WXS has been folded into WES, and Targeted-Capture / TCR-Seq / Unknow are gone.

Always re-check that the chosen cohort actually
carries the semantic format you need — HRA000073/74 are RNA-only, so a MAF analysis against them
finds nothing.

The schema below shows the **hydrated** result — i.e. what `hydrate_plan` returns and what the front-end
consumes, not what you type. When delivering to a front-end / for integration, produce this JSON
(front-ends read only the `result.structuredContent` layer of JSON-RPC):

```json
{
  "schema_version": "tool-chain/v2",
  "selection_status": "ok | no_candidate | unsupported | ...",
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
  "planner_metadata": {"used":false,"reason":"no_server_side_planner","planning_owner":"caller_model","arch":"light"},
  "data_matcher_mode": "neo4j", "mcp_timing_ms": 1151.2
}
```

Tool-property shape (the property goes in `answer`, **and rank-1 is still given** — the old
`information` + empty-`recommendations` route is retired; this should normally settle in one round with
zero or one query):

```json
{"schema_version":"tool-chain/v2","selection_status":"ok","candidates":[],
 "recommendations":[{"rank":1,"pipeline_id":"gatk","tool":{"tool_id":"gatk"},
   "match_note":"gatk is named first in the question, so it takes rank 1; tmb_survival_analysis sits further downstream — input-format difference is spelled out in answer."}],
 "answer":"gatk takes REFERENCE_GENOME_FASTA, DNA_GENOMIC_ALIGNMENT_BAM, DNA_ALIGNMENT_INDEX_BAI and TARGET_INTERVAL_LIST; tmb_survival_analysis takes MUTATION_ANNOTATION_FORMAT_MAF and CLINICAL_DATA_EXCEL. The two input sets are disjoint — there is no shared input format. They are consecutive rather than interchangeable: gatk's variant output must be converted to MAF before tmb_survival_analysis can consume it.",
 "intent":{"query_text":"...","analysis_goal":"tool input-format comparison","disease":null,
           "omics_type":null,"input_hint":null,"requested_outputs":[],"study_accessions":[],
           "source":"rule","ambiguous":false}}
```

Key points: `assets` carry per-file provenance and `match_reason`; `inputs/outputs` `artifact` values use
the ArtifactType vocabulary (`references/artifact_type.csv`); `candidates[]` is filled only for viable
atomic chains, else empty with `selection_status` explaining why. **Single-sample assets (FASTQ/BAM etc.)
carry `sample_role` / `sample_role_label` only when you already hold a `resolve_sample_roles` result;
otherwise set them to null. Aggregate assets (matrices/MAF/clinical tables) are always null.** More
generally: **any contract field you cannot fill goes to null with a one-line note in `match_note` — never
spend a round chasing a single field, and never withhold a recommendation over one.** For paired/grouped
analyses, attach `alternatives[]`
under `data` (other viable cohorts: `study_accession` / `label` / `sample_roles` stats / `role_resolved` /
`selected`, sourced likewise from `resolve_sample_roles` and cohort queries). Execution parameters are
transcribed only from `validate_execution_chain`'s `execution_params` / `submittable` — never assemble
paths yourself.

When a human-facing summary is explicitly requested in addition, use this template (the JSON contract
above remains the default and final answer):

```markdown
# 分析：<名称>（模态：<modal>；数据层级：<level>）
## 一、数据：队列 <HRAxxxxx>（<n> 样本），输入 <format> × <n>，路径 <dir>；可复用 T2 现成 <format>
## 二、方法链路：| # | 工具 | 输入格式 | 输出格式 | 验证点 |（逐环节）
## 三、链路完整性：✅ 完整 / ⚠️ 缺：<环节>（建议 <X>）
## 四、可执行性：`validate_execution_chain` 的 `submittable` / 缺失项、数据可达性（图内 `file_path`）、参考文件
```

## 10. Pre-submission gate (execution-contract validation, scenario 1)

When the user/front-end is about to **submit a chain to the execution side** (or asks "can this chain
run / what is missing"), call `validate_execution_chain` for a 5-stage probe instead of answering
"it runs" directly:

1. **Registration**: every tool_id known (graph / Knowledge Card)
2. **Card contract**: each step's required Knowledge Card inputs all present (missing one → reject)
3. **Binding structure**: File-input bindings must be objects (file_id/file_name) — an `Array[File]` input
   may also take a non-empty array of such objects; scalar types must match the card declaration
4. **Data probe**: unbound File inputs → count in-graph candidate files (by format family + optional cohort)
5. **Chain flow**: next_tool adjacency + up/downstream format continuity

Output is a `tool-chain-validation/v1.2` stage-by-stage report plus `execution_params` (**key = the
Knowledge Card param name**, value = real in-graph file path; only `/`-rooted confirmed paths, never
fabricated), `execution_params_by_step`, `execution_params_missing`, and `submittable`. **Submit only
when errors are zero and `submittable=true`**; on `submittable=false` do not claim "this chain runs" —
list `execution_params_missing` honestly. When a pipeline-level tool has no card, warn explicitly that
contract validation was skipped.

Six things to get right when transcribing execution params:

- **`tool_id` in `execution_params_by_step` / `execution_params_missing` is the Knowledge Card
  `meta.id`, not the graph tool id you passed in** — send `star`, get back
  `star_rrna_and_genome_alignment` (same convention as `normalized_steps`; card-less pipeline tools echo
  the id you sent). Match steps by the `step` index, not by string-comparing `tool_id` against your
  request.
- **For multi-step chains, `execution_params_by_step` is authoritative** (`[{step, tool_id, params}]`).
  `execution_params` is a flat convenience view keyed by bare param name; when the same name resolves to
  different paths in different steps (e.g. both `trim_galore` and `star` declare `read1`) that key is
  **dropped from the flat view** and listed in `execution_params_ambiguous`. A param absent from the flat
  view is not missing — read it from `by_step`.
- **`Array[File]` params carry a list of paths**, not a string (`fastqc.fastqs`, `multiqc.qc_files`).
  Never transcribe one as a single path.
- **Reference/index resources never appear in `execution_params` and are never reported missing.** Each
  card marks them (`reference_resource: true`; for the few pipeline-level cards whose delivery package
  omits the field, a server-side fallback table supplies it) — currently 14 params across nine tools:
  `star.rrna_star_index`, `star.genome_star_index`, `rsem.rsem_index`, `featurecounts.gtf_file`,
  `gatk.interval_list`, `manta_structural_variants.reference_fasta`/`.reference_fai`,
  `bwa.reference_fasta`, `bcftools.reference_fasta`, plus the pipeline-level
  `rnaseq_singletask.rrna_star_index`/`.star_genome_index`/`.rsem_index`/`.gtf_file` and
  `wes_somatic_pair.interval_list`. They carry card defaults and are resolved inside the
  execution container. **The marker alone decides this — whether the param is declared `File` or
  `String` is irrelevant**; `bwa`/`bcftools`/`manta`'s `reference_fasta` is a `String`-typed reference
  resource. Do not go hunting for their paths in the graph, and do not call a chain
  unrunnable because they are "absent".
  Note `bcftools.filtered_vcf_index` is **not** one of them despite the name — it is the companion `.tbi`
  of a data file and must be bound.
- **Some cards declare either/or inputs (`require_any`).** Members of a group are individually optional,
  but the group as a whole must get at least one binding — a plain required-input check cannot catch
  "none of them given". `scrna_cell_communication` needs `seurat_rds` or `combined_counts`;
  `paired_fastq_to_unmapped_bam` needs `sample_name` or `sample_accession`. Supplying neither is a
  contract error and the report names the group. The bulk10 sample tables have the same shape, but the
  server derives those from the study accession (§3.1), so they are neither expected nor reported.
- `execution_params_missing` elements are objects `{param, tool_id, step, reason}`. `reason =
  no_confirmed_path` means the binding was fine but the graph has no confirmed path for that asset (the
  data side needs to fill in `file_path`) — do not restate it as "the user did not bind it".
  `reason = study_not_resolved` is a different thing: the study accession was never pinned down (empty
  `assets`, or assets spanning several studies), so the server had no cohort to derive the clinical
  pair from. **That one is fixed by choosing the data, not by asking the data side for a file.**

## 11. Boundaries and principles

- **Privacy red line**: on `individual`, **all numbered-prefix properties `01_`–`13_`** except `00_*`
  (operational identifiers) are patient-level sensitive data — 01_ demographics, 02_ family history,
  03_ lifestyle, 04_ hematology, 09_ pathology, 10_ invasion, 11_ molecular markers, 12_ treatment,
  13_ survival. Planning uses only aggregates and existence checks (count / IS NOT NULL); no individual's
  clinical property value ever appears in answers, Plans, or logs. **Judge sensitivity by the numeric
  prefix, not by whether the field name looks clinical** — upstream may add new numbered columns anytime.
  Sample-level `tissue_type` / `specimen_type` / `gender` used as grouping constraints is operational use;
  do not enumerate them per individual.
- The graph is a "map of methods and data"; whether a tool is installed or a path is reachable on this
  machine must be **checked for real** (`which`, `ls`) — never pretend.
- Answering "what analyses are possible": first give the analysis families the graph covers, then drill
  into chains for families of interest.
- For execution by front/back-ends: `file_path` in a plan is a graph record (it may point to another
  server) — state its source honestly.

## 12. Appendix: measured snapshots (whitelist sources; re-verify after graph updates)

Measured on the connected graph. Tool/cohort matching should start here, not with exploratory queries.

### 12.1 Tool catalog snapshot (55)

Several pipelines differ by exactly one discriminating detail; the full descriptions below carry it,
so read to the end of the row rather than matching on the opening clause. The families that actually
get confused in practice:

- `diff_expr_go` (GO functional enrichment) vs `diff_expr_kegg` (pathway / Reactome enrichment) — both
  are limma two-group DE on an expression matrix alone; the enrichment target is the only difference.
- `gsea_pathway_enrichment` does **not** pre-select DEGs (pre-ranked GSEA over all genes).
  `deg_enrichment` / `de_enrichment` are bulk10 members (§3.1): they parse grouping out of CNCB-native
  metadata themselves, so they fit any two-group DE-plus-enrichment request — but **only on `HRA003107`**,
  the sole cohort either has run on. `de_enrichment` returns DE + enrichment, `deg_enrichment` adds the
  functional-enrichment panel; `deg_trend` is the same family (also HRA003107-only) when the user wants
  the trend/box/volcano visual set. Phrasing like "case group vs control group" does not discriminate
  among them — every DE pipeline groups samples. On any other cohort, use `diff_expr_go` /
  `diff_expr_kegg`, which take a matrix alone.
  **Pick the bulk10 pair only when the question names a cohort** (or names the tool itself). Measured
  across 22 DE questions in the 165-case set: not one plain "差异表达分析 / do DE plus enrichment"
  request meant `HRA003107`, yet the bulk10 siblings were chosen three times and were wrong each time.
  Default a cohort-less DE request to `diff_expr_go` / `diff_expr_kegg`, split by which enrichment
  word the question uses — GO → `diff_expr_go`; KEGG / Reactome / pathway → `diff_expr_kegg`; neither
  named → `diff_expr_go`.
- `survival_analysis` stratifies by a **named gene's mutation status** (MAF) and `tmb_survival_analysis`
  by **TMB median**. Grouping by a gene's **expression level** is `her2_pfs_survival` — it is the
  default for that whole shape, whatever the gene (HER2/ERBB2 is only its default, not its scope).
  `km_survival` (KM) and `cox_model` (multivariate Cox) are the bulk10 survival pair: they read survival
  time and status straight from `individual.csv`, so they are the right answer for overall-survival
  questions on their five proven cohorts (`HRA003107`, `HRA000073`, `HRA000074`, `HRA002693`,
  `HRA006117`) — and only those.
- `rnaseq_unsupervised_cluster` is the end-to-end chain from counts; `preprocess_counts`,
  `hvg_pca_gmm` and `bootstrap_stability` are single steps carved out of it and take logCPM.
- `wgcna` is the full co-expression chain and the default for a co-expression / hub-gene request on any
  cohort other than `HRA003107` / `HRA007167`. On those two, prefer the bulk10 pair: `wgcna_hub` for
  hub-gene output, `wgcna_module_trait` for module↔trait association — both parse grouping from CNCB
  metadata with no clinical table to bind.

| tool | function | modal | inputs | outputs |
|---|---|---|---|---|
| `bcftools` | 对 GATK 过滤后的体细胞 VCF 文件进行后处理，包括提取 PASS 位点、基于参考基因组进行左对齐和拆分多等位基因位点、建立 Tabix 索引，并生成详细的 QC 统计文件（记录数、SNP/Indel 计数、FILTER 分布、DP/AF 等）。输出可直接用于 SnpEff 注释。 | WES | DNA_VARIANT_VCF_GENERAL,DNA_VARIANT_INDEX_TBI,REFERENCE_GENOME_FASTA | DNA_VARIANT_VCF_GENERAL,TABULAR_BIO_DATA,DNA_VARIANT_INDEX_TBI |
| `bootstrap_stability` | 对聚类分析执行Bootstrap重采样，通过比较不同运行间的ARI/NMI评估聚类稳定性， 统计最佳K的频率，并利用标签置换零分布评估稳定性显著性。 输入为logCPM表达矩阵，输出包含稳定性指标、最佳K频率、置换检验P值及可视化图表。 | bulk_RNA | - | TABULAR_BIO_DATA,VISUALIZATION_RESULT |
| `breast_cellchat` | 基于CellChat方法分析乳腺癌单细胞转录组数据中的细胞间通讯网络。输入为Seurat格式的RDS文件，通过比较肿瘤与正常组织中的配体-受体互作，揭示肿瘤微环境中的细胞通讯变化。输出包括通讯网络分析结果、质控报告和运行日志。 | bulk_RNA,sc-RNA | SCRNA_OBJECT_RDS,REFERENCE_GENOME_FASTA | VISUALIZATION_RESULT,QC_STATS_REPORT |
| `bwa` | 基于 BWA-MEM 算法的双端测序比对流程。输入为 R1/R2 FASTQ 文件和样本 ID， 输出为未排序的 SAM 文件及 BWA 运行日志。适用于全外显子组测序数据的比对步骤， 后续需配合 SAMtools 完成排序和索引。 | WES | REFERENCE_GENOME_FASTA,RAW_PAIRED_END_R1_FASTQ,RAW_PAIRED_END_R2_FASTQ | DNA_GENOMIC_ALIGNMENT_BAM |
| `cellranger_workflow` | 基于 10x Genomics CellRanger 的单细胞 RNA 测序数据分析流程。 包含 FASTQ 质控、序列比对、基因表达定量及结果可视化，适用于 10x Chromium 平台产生的单细胞转录组数据。 | sc-RNA,bulk_RNA | RAW_SINGLE_END_FASTQ,DNA_GENOMIC_ALIGNMENT_BAM | TABULAR_BIO_DATA,DNA_GENOMIC_ALIGNMENT_BAM,QC_STATS_REPORT |
| `celltype_case_control_de` | 对单细胞RNA-seq数据中指定的细胞类型进行病例-对照差异表达分析。输入为Seurat RDS文件，输出包括差异表达结果、分析摘要、质控报告等。适用于配对或非配对的病例-对照研究设计。 | sc-RNA,bulk_RNA | SCRNA_OBJECT_RDS,TABULAR_BIO_DATA,REFERENCE_GENOME_FASTA | QC_STATS_REPORT |
| `cnvkit_cnv_clinical` | 对肿瘤队列的配对肿瘤/正常 WGS 或 WES BAM 运行 CNVkit，生成样本级分段、离散拷贝数、scatter/diagram 图，并可选汇总高频基因 CNV 与临床分期及总生存的探索性关联。输入为样本ID、肿瘤/正常BAM/BAI数组，输出包括CNV分段文件、BED文件、可视化图以及临床关联分析结果。 | Clinical,WES,WGS | DNA_GENOMIC_ALIGNMENT_BAM,CLINICAL_DATA_EXCEL,TABULAR_BIO_DATA | VISUALIZATION_RESULT,TABULAR_BIO_DATA |
| `cox_model` | 整合基因表达矩阵与临床元数据，执行 Cox 比例风险回归分析和 Kaplan-Meier 生存曲线绘制。 支持自定义样本分组、生存时间/状态列映射，输出风险比、P 值及前 N 个显著基因。 适用于癌症预后标志物筛选和临床亚组生存差异分析场景。 | Clinical,bulk_RNA| TABULAR_BIO_DATA(counts, required) | RESULT_ARCHIVE,OUTPUT_MANIFEST,RUN_SUMMARY |
| `dataset_downstream` | 对单细胞RNA-seq数据集进行标准化下游分析，包括基因排序、细胞类型注释和恶性细胞标记。 输入为Seurat RDS文件和基因排序文件，输出包括压缩的结果文件、运行摘要和质量控制报告。 | sc-RNA | TABULAR_BIO_DATA,REFERENCE_GENOME_FASTA,SCRNA_OBJECT_RDS | QC_STATS_REPORT |
| `dataset_matrix_annotation` | 该流程用于对单细胞RNA-seq数据集进行矩阵注释和细胞类型标注。输入为Seurat RDS格式的整合数据文件，输出包括注释结果压缩包、运行摘要、输入质量控制报告和分析清单等文件。 | sc-RNA | TABULAR_BIO_DATA,SCRNA_OBJECT_RDS,REFERENCE_GENOME_FASTA | QC_STATS_REPORT |
| `de_enrichment` | 本流程整合 CNCB 元数据，执行差异表达分析并生成富集分析结果。支持自动样本分组、生存分析关联，输出火山图、热图及富集分析可视化。适用于具有临床元数据的 bulk RNA-seq 数据。 **HRA003107 only — pick only when the question names that cohort; a cohort-less DE request goes to `diff_expr_go`/`diff_expr_kegg`.** | bulk_RNA,Clinical| TABULAR_BIO_DATA(counts, required) + case/control labels | RESULT_ARCHIVE,OUTPUT_MANIFEST,RUN_SUMMARY |
| `deg_enrichment` | 本流程整合表达矩阵、样本元数据和临床信息，执行差异表达分析并生成火山图、热图及功能富集分析结果。 支持自动分组识别、生存分析关联，适用于批量 RNA-seq 数据的标准化差异表达与富集分析场景。 **HRA003107 only — same rule as `de_enrichment`: no cohort named, do not pick it.** | bulk_RNA,Clinical| TABULAR_BIO_DATA(counts, required) + case/control labels | RESULT_ARCHIVE,OUTPUT_MANIFEST,RUN_SUMMARY |
| `deg_trend` | 本流程用于差异表达基因(DEG)的趋势分析与可视化。输入基因表达矩阵、样本元数据和临床信息，自动完成样本分组、差异分析，并生成火山图、热图、箱线图和趋势图等多种可视化结果。适用于批量 RNA-seq 数据的临床关联分析场景。 | bulk_RNA,Clinical| TABULAR_BIO_DATA(counts, required) + case/control labels | RESULT_ARCHIVE,OUTPUT_MANIFEST,RUN_SUMMARY |
| `diff_expr_go` | 基于表达矩阵进行差异基因分析（limma）并针对上下调基因分别进行 GO 功能富集（clusterProfiler）。 适用于 FPKM/TPM 定量数据的两组比较场景，输出差异基因列表及 GO 富集结果表。 **No cohort restriction — the default for a generic DE/enrichment request; pick it when the question says GO, or says nothing about the enrichment target.** | bulk_RNA | TABULAR_BIO_DATA | TABULAR_BIO_DATA |
| `diff_expr_kegg` | 基于 limma 包进行两组样本差异表达分析，并使用 ReactomePA 对上下调基因进行通路富集。 适用于人类基因表达矩阵（FPKM/TPM），输出差异基因列表及富集结果。 **No cohort restriction — pick it when the question says KEGG / Reactome / pathway.** | bulk_RNA | TABULAR_BIO_DATA | TABULAR_BIO_DATA |
| `driver_gene_gender_analysis` | 该流程基于 WES MAF 文件、临床表和 MetaInfo 表，对驱动基因的突变频率进行性别分层分析。 通过卡方检验比较男性和女性样本中每个驱动基因的突变率，并输出统计结果表、诊断表及多种可视化图表（分组柱状图、瀑布图、热图、火山图）。 | Clinical,WES | CLINICAL_DATA_EXCEL,MUTATION_ANNOTATION_FORMAT_MAF | TABULAR_BIO_DATA,VISUALIZATION_RESULT,MUTATION_ANNOTATION_FORMAT_MAF |
| `fastp` | 对双端测序FASTQ文件进行质量过滤、接头修剪和质控报告生成。输入为样本ID和双端FASTQ文件，输出为修剪后的FASTQ文件以及HTML和JSON格式的质控报告。适用于WES等双端测序数据的预处理步骤。 | WES | RAW_PAIRED_END_R1_FASTQ,RAW_PAIRED_END_R2_FASTQ | QC_STATS_REPORT,RAW_PAIRED_END_R2_FASTQ,RAW_PAIRED_END_R1_FASTQ |
| `fastqc` | 对输入的 FASTQ 文件进行质量评估，生成 HTML 和 ZIP 格式的 FastQC 报告。 适用于 WES、WGS、RNA-seq 和单细胞测序等多种测序数据类型，可接收原始或修剪后的 FASTQ 文件。 | bulk_RNA,sc-RNA,WES,WGS | RAW_PAIRED_END_R1_FASTQ,RAW_PAIRED_END_R2_FASTQ | QC_STATS_REPORT |
| `featurecounts` | 该流程使用 featureCounts 工具对 RNA-seq 比对后的 BAM 文件进行基因水平计数。 输入为最终 BAM 文件和 GTF 注释文件，输出为基因计数矩阵、统计摘要和运行日志。 适用于 RNA-seq 定量分析中的基因表达计数步骤。 | bulk_RNA | DNA_GENOMIC_ALIGNMENT_BAM | QC_STATS_REPORT,TABULAR_BIO_DATA |
| `gatk` | 基于 GATK 最佳实践的全外显子组（WES）肿瘤-正常配对体细胞变异检测流程。 流程对肿瘤和正常样本分别进行 MarkDuplicates 标记重复、BaseRecalibrator 碱基质量校正， 然后使用 Mutect2 进行体细胞变异检测，并通过 FilterMutectCalls 进行过滤， 同时评估样本污染和构建读段方向偏倚模型。 | WES | DNA_ALIGNMENT_INDEX_BAI,REFERENCE_GENOME_FASTA,TARGET_INTERVAL_LIST,DNA_GENOMIC_ALIGNMENT_BAM | DNA_GENOMIC_ALIGNMENT_BAM,QC_STATS_REPORT,DNA_VARIANT_INDEX_TBI,DNA_VARIANT_VCF_GENERAL |
| `gatk_germline_cohort` | GATK 最佳实践的**队列级胚系**变异检测：HaplotypeCaller 逐样本产 gVCF → GenomicsDB 合并 → 联合分型 → VQSR 过滤，输出队列 VCF/TBI 与质控统计。与 `gatk`（原子工具，走 Mutect2 体细胞分支）分工不同：**要胚系、要队列联合分型就用它**；单病人配对的体细胞检测走 `wes_somatic_pair`。 | WGS,WES,Clinical | DNA_GENOMIC_ALIGNMENT_BAM,TARGET_INTERVAL_LIST,REFERENCE_GENOME_FASTA,METADATA_SAMPLE_INFO,CLINICAL_DATA_EXCEL,DNA_VARIANT_VCF_GENERAL,DNA_VARIANT_INDEX_TBI | DNA_VARIANT_VCF_GENERAL,DNA_VARIANT_INDEX_TBI,DNA_GENOMIC_ALIGNMENT_BAM,QC_STATS_REPORT,TABULAR_BIO_DATA,VISUALIZATION_RESULT |
| `gene_boxplot` | 基于基因表达矩阵和临床元数据生成箱线图、火山图、热图等可视化结果。支持从 CNCB 原生格式元数据自动映射样本分组信息，可整合生存分析和肿瘤分期数据。适用于 bulk RNA-seq 数据的探索性可视化分析。 | Clinical,bulk_RNA| TABULAR_BIO_DATA(counts, required) + case/control labels | RESULT_ARCHIVE,OUTPUT_MANIFEST,RUN_SUMMARY |
| `gsea_pathway_enrichment` | 本流程基于limma moderated t统计量构建全基因排序，使用fgseaMultilevel执行预排序GSEA。 输入为表达矩阵和样本元数据，输出包括通路富集结果、显著通路、排序基因列表及可视化图表。 适用于病例-对照转录组比较分析，支持协变量校正和配对设计。 | bulk_RNA | TABULAR_BIO_DATA | VISUALIZATION_RESULT,TABULAR_BIO_DATA,QC_STATS_REPORT |
| `her2_pfs_survival` | 基于 TPM 表达矩阵、临床信息及样本元信息，分析特定基因（默认 HER2）表达水平与无进展生存期（PFS）的关联。 流程自动匹配样本 accession，执行 Winsorizing 处理，生成 KM 生存曲线、Logrank 统计量及质量控制报告。 | Clinical,bulk_RNA | CLINICAL_DATA_EXCEL,TABULAR_BIO_DATA | VISUALIZATION_RESULT,QC_STATS_REPORT,TABULAR_BIO_DATA |
| `hvg_pca_gmm` | 从logCPM表达矩阵中筛选高变基因，执行PCA降维，并在候选K范围内拟合高斯混合模型（GMM），最终依据BIC选择最佳聚类数。输入为预处理后的logCPM矩阵，输出包括高变基因统计、PCA结果、GMM聚类指标及可视化图表。 | bulk_RNA,sc-RNA | - | TABULAR_BIO_DATA,VISUALIZATION_RESULT,QC_STATS_REPORT |
| `immune_infiltration_iobr` | 基于 IOBR 包的 CIBERSORT 算法进行免疫细胞浸润分析流程。 输入基因表达 TPM 矩阵、临床信息和样本元数据，输出免疫细胞比例估计、可靠性评估及可视化图表。 适用于批量 RNA-seq 数据的肿瘤微环境免疫细胞组成分析。 | bulk_RNA,Clinical | CLINICAL_DATA_EXCEL,TABULAR_BIO_DATA | TABULAR_BIO_DATA,VISUALIZATION_RESULT,QC_STATS_REPORT |
| `immunotherapy_cellchat` | 基于CellChat的免疫治疗细胞通讯分析流程。输入Seurat格式的单细胞RNA-seq数据，通过比较响应者与非响应者之间的细胞通讯网络差异，揭示免疫治疗相关的细胞间相互作用机制。输出包括通讯网络分析结果、质控报告和运行日志。 | sc-RNA | SCRNA_OBJECT_RDS,REFERENCE_GENOME_FASTA | VISUALIZATION_RESULT,QC_STATS_REPORT |
| `ipf_trajectory_regulon` | 对特发性肺纤维化(IPF)单细胞RNA-seq数据进行轨迹推断和调控子分析。输入为Seurat RDS对象，输出包括分析结果压缩包、运行摘要、质控报告和文件清单等。 | bulk_RNA,sc-RNA | SCRNA_OBJECT_RDS,METADATA_SAMPLE_INFO,REFERENCE_GENOME_FASTA | QC_STATS_REPORT |
| `km_survival` | 整合基因表达矩阵与临床元数据，执行 Kaplan-Meier 生存分析和 Cox 比例风险模型。 支持样本分组、肿瘤分期过滤和生存数据验证，输出生存曲线及统计结果。 | bulk_RNA,Clinical| TABULAR_BIO_DATA(counts, required) | RESULT_ARCHIVE,OUTPUT_MANIFEST,RUN_SUMMARY |
| `lung_tme_annotation_cnv` | 基于单细胞RNA-seq数据对肺癌肿瘤微环境进行细胞类型注释和拷贝数变异(CNV)分析。 输入为Seurat RDS文件和基因排序文件，输出包括压缩的结果包、运行摘要、质控报告和分析清单。 | sc-RNA | SCRNA_OBJECT_RDS,TABULAR_BIO_DATA,REFERENCE_GENOME_FASTA | QC_STATS_REPORT |
| `manta_structural_variants` | Manta 结构变异检测：从比对 BAM 调用大片段缺失/重复/倒位/易位，输出 SV VCF 与统计表。**闭集内唯一做结构变异的流程**（function `结构变异检测` 只此一条），SNV/InDel 不归它管——那是 `wes_somatic_pair` / `gatk_germline_cohort`。 | WGS,WES | DNA_GENOMIC_ALIGNMENT_BAM,REFERENCE_GENOME_FASTA | DNA_VARIANT_VCF_GENERAL,DNA_VARIANT_INDEX_TBI,TABULAR_BIO_DATA,QC_STATS_REPORT |
| `multiqc` | 接收任意数量的上游质控文件（如 FastQC、fastp、SAMtools、BCFtools、SnpEff 等）， 生成交互式 MultiQC HTML 汇总报告及实际使用的配置文件。适用于 WES、WGS、RNA-seq 等流程。 | bulk_RNA,WES,WGS | - | QC_STATS_REPORT |
| `paired_fastq_to_unmapped_bam` | 将双端 FASTQ 测序数据转换为未比对的 BAM 文件 (uBAM)，并添加完整的 Read Group 信息。 适用于 GATK 最佳实践流程的起始步骤，输出可用于后续变异检测流程的标准化 BAM 文件。 | WES | RAW_PAIRED_END_R2_FASTQ,RAW_PAIRED_END_R1_FASTQ,DNA_GENOMIC_ALIGNMENT_BAM | DNA_GENOMIC_ALIGNMENT_BAM |
| `preprocess_counts` | 对RNA-seq原始count矩阵执行样本质量控制、低表达基因过滤和logCPM标准化。 输入为基因ID为第一列、其余列为样本count值的TSV矩阵，输出标准化后的logCPM矩阵及QC统计文件。 | bulk_RNA | TABULAR_BIO_DATA | TABULAR_BIO_DATA,QC_STATS_REPORT |
| `rmats_alternative_splicing` | 比较两组 bulk RNA-seq 数据中的差异剪接事件，覆盖 skipped exon（SE）、mutually exclusive exon（MXE）、 alternative 5'/3' splice site（A5SS/A3SS）和 retained intron（RI），并为代表性事件生成 sashimi plot。 流程从已比对 BAM 开始，不包含 FASTQ 质控和比对。 | bulk_RNA | RNA_TRANSCRIPTOME_ALIGNMENT_BAM,REFERENCE_GENOME_FASTA | TABULAR_BIO_DATA,QC_STATS_REPORT,DNA_GENOMIC_ALIGNMENT_BAM,VISUALIZATION_RESULT |
| `rnaseq_singletask` | 涵盖从原始测序数据到表达量定量的全流程分析，包括质控、去接头、rRNA 去除、比对、定量及报告生成。 适用于单样本或批量单任务提交场景，支持单端/双端测序数据自动识别。 | bulk_RNA | RAW_PAIRED_END_R1_FASTQ,RAW_PAIRED_END_R2_FASTQ,REFERENCE_GENOME_FASTA | DNA_GENOMIC_ALIGNMENT_BAM,RNA_TRANSCRIPTOME_ALIGNMENT_BAM,RAW_PAIRED_END_R2_FASTQ,TABULAR_BIO_DATA |
| `rnaseq_unsupervised_cluster` | 本流程针对 RNA-seq count 矩阵进行无监督聚类分析，涵盖数据预处理、高变基因筛选、PCA 降维及高斯混合模型聚类。 同时通过 Bootstrap 重采样评估聚类稳定性并计算显著性 P 值，适用于无样本元数据场景的探索性分析。 | bulk_RNA | TABULAR_BIO_DATA | TABULAR_BIO_DATA,VISUALIZATION_RESULT,QC_STATS_REPORT |
| `rsem` | 该流程基于 RSEM 工具，接收 STAR 比对生成的转录组 BAM 文件和 RSEM 索引，进行基因和转录本水平的表达定量分析。 输入为转录组 BAM 文件和 RSEM 索引目录，输出包括基因表达量结果、转录本表达量结果、统计文件和运行日志。 | bulk_RNA | RNA_TRANSCRIPTOME_ALIGNMENT_BAM | TABULAR_BIO_DATA,QC_STATS_REPORT |
| `samtools` | 基于SAMtools工具集的比对后处理流程，支持对BAM文件进行排序、索引、去重及多种比对统计。 适用于WES/WGS和RNA-seq数据，通过remove_duplicates参数切换普通排序/去重模式。 | WGS,bulk_RNA,WES | DNA_GENOMIC_ALIGNMENT_BAM | DNA_ALIGNMENT_INDEX_BAI,DNA_GENOMIC_ALIGNMENT_BAM,QC_STATS_REPORT |
| `scrna_cell_communication` | 该流程整合 CellPhoneDB 和 NicheNet 进行单细胞转录组细胞通讯分析。输入为 Seurat RDS 对象或 h5ad 格式的表达矩阵及细胞元数据，输出包括样本级别的 CellPhoneDB 结果、差异通讯分析结果以及 NicheNet 配体-受体和配体-靶基因预测结果。支持 HRA000087 数据集的自动预处理模式。 | sc-RNA,bulk_RNA | TABULAR_BIO_DATA,SCRNA_OBJECT_RDS,METADATA_SAMPLE_INFO | VISUALIZATION_RESULT,SCRNA_OBJECT_RDS,QC_STATS_REPORT,TABULAR_BIO_DATA |
| `snpeff` | 基于 SnpEff 工具对 VCF 文件进行变异效应注释的独立流程。输入为未压缩或压缩的 VCF 文件，输出包含注释后的 VCF、HTML/CSV 格式的统计报告以及运行日志。适用于 WES/WGS 体细胞或胚系突变的生物学效应预测。 | WES,WGS | DNA_VARIANT_VCF_GENERAL,REFERENCE_GENOME_FASTA | DNA_VARIANT_VCF_GENERAL,QC_STATS_REPORT |
| `stage_heatmap` | 本流程用于生成基于肿瘤分期的基因表达热图可视化。整合表达矩阵、元数据文件和临床信息文件，自动匹配样本信息并筛选目标分期样本，输出分期热图及样本映射报告。适用于 CNCB 等公共数据库来源的 bulk RNA-seq 数据可视化分析。 | Clinical,bulk_RNA| TABULAR_BIO_DATA(counts, required) | RESULT_ARCHIVE,OUTPUT_MANIFEST,RUN_SUMMARY |
| `star` | 该流程使用 STAR 比对工具对 RNA-seq 数据进行 rRNA 去除和基因组比对。流程包含两个步骤：首先将 reads 比对到 rRNA 参考索引以去除 rRNA 污染，然后将未比对的 reads 比对到基因组参考索引，输出未排序的基因组 BAM、转录组 BAM、基因计数文件和日志。 | bulk_RNA | REFERENCE_GENOME_FASTA,RAW_PAIRED_END_R2_FASTQ,RAW_PAIRED_END_R1_FASTQ | RNA_TRANSCRIPTOME_ALIGNMENT_BAM,DNA_GENOMIC_ALIGNMENT_BAM,REFERENCE_GENOME_FASTA,RAW_PAIRED_END_R1_FASTQ |
| `star_fusion` | STAR-Fusion 基因融合检测：从**双端 FASTQ 起步**比对并识别融合转录本，输出融合事件表与 HTML 报告。**闭集内唯一做基因融合的流程**。注意起点是 FASTQ 不是表达矩阵——手上只有 counts 矩阵时它做不了，这是数据缺口不是工具缺口（按 §8 给 rank1 并在 match_note 说明）。与 `star`（原子比对工具）同名前缀但不是一回事。 | RNA,Clinical | METADATA_SAMPLE_INFO,RAW_PAIRED_END_R1_FASTQ,RAW_PAIRED_END_R2_FASTQ | TABULAR_BIO_DATA,QC_STATS_REPORT |
| `survival_analysis` | 基于 WDL 1.0 和 Cromwell 的生存分析流程，用于评估指定基因突变状态与无进展生存期（PFS）的关系。 流程整合了突变提取、Log-rank 检验、Kaplan-Meier 曲线绘制及单因素 Cox 回归分析，最终生成汇总报告。 | WES,Clinical | CLINICAL_DATA_EXCEL,MUTATION_ANNOTATION_FORMAT_MAF | QC_STATS_REPORT,VISUALIZATION_RESULT,CLINICAL_DATA_EXCEL |
| `tcell_intervention` | 该流程用于对单细胞RNA-seq数据进行T细胞干预前后的比较分析。输入为Seurat RDS文件，通过指定细胞类型、时间点和患者信息等元数据列，进行差异表达分析，输出包括压缩的结果文件、运行摘要、质控报告和分析清单等。 | bulk_RNA,sc-RNA | TABULAR_BIO_DATA,REFERENCE_GENOME_FASTA,METADATA_SAMPLE_INFO,SCRNA_OBJECT_RDS | QC_STATS_REPORT |
| `tmb_survival_analysis` | 从MAF文件和临床数据计算病人级肿瘤突变负荷（TMB），按TMB中位数将病人分为高/低组， 进行Kaplan-Meier生存分析和log-rank检验，输出生存曲线、TMB分布图及统计结果表。 适用于肿瘤队列的预后分析场景。 | WES,Clinical | MUTATION_ANNOTATION_FORMAT_MAF,CLINICAL_DATA_EXCEL | QC_STATS_REPORT,TABULAR_BIO_DATA,VISUALIZATION_RESULT |
| `trim_galore` | 基于 Trim Galore 工具的 FASTQ 文件接头修剪与质量控制流程。支持单端和双端测序数据，可指定接头序列，输出修剪后的 FASTQ 文件和修剪报告。 | bulk_RNA | RAW_PAIRED_END_R1_FASTQ,RAW_PAIRED_END_R2_FASTQ | RAW_PAIRED_END_R1_FASTQ,RAW_PAIRED_END_R2_FASTQ,QC_STATS_REPORT |
| `tumor_evolution_inference` | 肿瘤演化与克隆推断：由变异/表达数据重建克隆结构与演化关系，输出克隆分配表与演化树图。**闭集内唯一做克隆演化的流程**。它推断的是克隆谱系，**不是因果机制**——「能否推断致病因果」仍按 §8 的因果拒绝纪律处理，不要拿这条去顶。 | sc-RNA,WGS,RNA | DNA_GENOMIC_ALIGNMENT_BAM,TABULAR_BIO_DATA,DNA_VARIANT_VCF_GENERAL | TABULAR_BIO_DATA,VISUALIZATION_RESULT,QC_STATS_REPORT |
| `umap` | 基于基因表达矩阵进行 UMAP 降维可视化分析，整合 CNCB 元数据和临床信息。 支持自动样本分组、生存分析数据提取，输出降维结果及样本信息报告。 | Clinical,bulk_RNA| TABULAR_BIO_DATA(counts, required) | RESULT_ARCHIVE,OUTPUT_MANIFEST,RUN_SUMMARY |
| `wes_somatic_maf_landscape` | 本流程用于全外显子测序（WES）队列的体细胞突变景观分析。输入标准 MAF 文件，经过滤处理后绘制 Top N 突变基因 Oncoplot 及突变类型分布图。适用于癌症基因组学中的突变谱可视化与总结。 | WES | MUTATION_ANNOTATION_FORMAT_MAF | TABULAR_BIO_DATA,VISUALIZATION_RESULT,MUTATION_ANNOTATION_FORMAT_MAF |
| `wes_somatic_pair` | 用于单个病人配对 tumor-normal WES 数据的体细胞变异分析流程。包含 FASTQ 质控、BWA 比对、Mutect2 变异检测、SnpEff 注释及 MultiQC 汇总报告。 输出包括过滤后的 VCF 文件、BAM 文件及完整的质控报告。 | WGS,WES | DNA_VARIANT_VCF_GENERAL,REFERENCE_GENOME_FASTA,RAW_PAIRED_END_R1_FASTQ,RAW_PAIRED_END_R2_FASTQ | DNA_VARIANT_INDEX_TBI,DNA_GENOMIC_ALIGNMENT_BAM,QC_STATS_REPORT,DNA_VARIANT_VCF_GENERAL |
| `wgcna` | 基于基因表达矩阵和临床表型数据执行 WGCNA 共表达网络分析，包括样本 QC、模块识别、模块 - 性状关联、hub 基因筛选及 bootstrap 稳定性评估。 适用于转录组数据的系统性分析，输出模块划分结果、关键 hub 基因列表及功能富集分析结果。 | bulk_RNA,Clinical | CLINICAL_DATA_EXCEL,TABULAR_BIO_DATA | TABULAR_BIO_DATA,VISUALIZATION_RESULT |
| `wgcna_hub` | 基于 WGCNA 算法构建基因共表达网络，识别与表型性状相关的关键模块和 Hub 基因。 支持从 CNCB 等平台的原始元数据自动解析样本分组和临床信息，输出模块 - 性状关联、候选 Hub 基因列表及功能富集结果。 | Clinical,bulk_RNA| TABULAR_BIO_DATA(counts, required) | RESULT_ARCHIVE,OUTPUT_MANIFEST,RUN_SUMMARY |
| `wgcna_module_trait` | 基于 WGCNA 算法构建基因共表达网络，识别功能模块并分析与临床性状的关联关系。 支持 GO/KEGG/Hallmark 富集分析、生存分析和 hub 基因筛选，适用于批量 RNA-seq 数据的系统生物学研究。 | bulk_RNA,Clinical| TABULAR_BIO_DATA(counts, required) | RESULT_ARCHIVE,OUTPUT_MANIFEST,RUN_SUMMARY |

### 12.2 Study snapshot (20; sample_count property is null for 6 studies — sample-node counts fill the gap)

| study_accession | tumor_type | sample_count(prop) | sample nodes |
|---|---|---|---|
| HRA000001 | *(null; study_type = Healthy Study)* | 557 | 557 |
| HRA000021 | esophageal cancer | 1016 | 1016 |
| HRA000071 | malignant glioma | 572 | 572 |
| HRA000073 | malignant glioma | null | 325 |
| HRA000074 | malignant glioma | 572 | 693 |
| HRA000087 | nasopharynx carcinoma | null | 61 |
| HRA000122 | acute T cell leukemia | 287 | 287 |
| HRA000873 | colorectal adenocarcinoma | 2030 | 2030 |
| HRA001272 | hepatocellular carcinoma | 698 | 698 |
| HRA001748 | liver cancer | 160 | 160 |
| HRA001749 | liver cancer | 178 | 178 |
| HRA002693 | acute myeloid leukemia | null | 655 |
| HRA003107 | esophageal cancer | 310 | 310 |
| HRA005191 | non-small cell lung carcinoma | 243 | 243 |
| HRA006117 | acute myeloid leukemia | null | 835 |
| HRA006499 | liver cancer | 482 | 523 |
| HRA007167 | melanoma | 81 | 81 |
| HRA007169 | melanoma | 168 | 168 |
| HRA007413 | acute myeloid leukemia | null | 373 |
| HRA016026 | lung cancer | null | 700 |

### 12.3 Derived-data snapshot (T2 — this table *is* the answer; do not spend rounds rediscovering it)

**T1 holds raw reads only**: `RAW_PAIRED_END_R1_FASTQ` / `R2`, 14092 files each, plus one
`CLINICAL_DATA_EXCEL` and one `METADATA_SAMPLE_INFO` per cohort (19 each). **There is no BAM, VCF,
MAF or matrix anywhere in T1.** Every alignment, variant-calling and quantification product lives in
**T2**. A BAM query written as `MATCH (t:T1)` returns zero rows by construction — never read that as
`no_candidate`.

**On T2, `format` is a lower-case file extension** (`bam` 9465, `vcf.gz` 7291, `bai` 6177,
`gz.tbi` 5788, `maf` 2355, `vcf` 1301, `tab` 430, `h5` 403) **while `semantic_format` carries the
upper-case semantic name.** So `WHERE t.format CONTAINS 'BAM'` can never match — query
`semantic_format` for semantics, and lower-case for extensions.

| T2 semantic_format | count | cohorts (example file_name) |
|---|---|---|
| `DNA_VARIANT_VCF_GENERAL` | 8310 | HRA000873(3045), HRA001272(1909), HRA016026(1050), HRA006499(1014), HRA000071(572), HRA007169(380), HRA001749(336) |
| `DNA_ALIGNMENT_BQSR_BAM` | 6177 | HRA000873(2030), HRA000021(1016), HRA006499(763), HRA001272(750), HRA016026(700), HRA000071(572), HRA001749(178), HRA007169(168) |
| `DNA_ALIGNMENT_INDEX_BAI` | 6177 | same cohorts — companion index |
| `DNA_VARIANT_INDEX_TBI` | 5788 | same as VCF — companion index |
| `RNA_TRANSCRIPTOME_ALIGNMENT_BAM` | 3288 | HRA000074(693), HRA006117(570), HRA002693(442), HRA001272(430), HRA007167(391), HRA000073(325), HRA003107(310), HRA000122(124) (`HRR025534Aligned.sortedByCoord.out.bam`) |
| `MUTATION_ANNOTATION_FORMAT_MAF` | 2355 | the seven-cohort whitelist above |
| `TABULAR_BIO_DATA` | 592 | expression matrices — 9 cohorts × 3 flavours (FPKM/TPM/counts), file name contains `Genes` |
| `RNA_SPLICEJUNCTION_TAB` | 430 | **HRA001272 only** (`HRR1402797SJ.out.tab`, STAR splice junctions) |
| `SCRNA_MATRIX_H5` | 403 | HRA005191(243), HRA001748(160) — ready-made single-cell matrices |
| `DNA_SOMATIC_SV_VCF` | 286 | structural variants |
| `BIO_DATA_CONTAINER_OBJECT` | 18 | **Seurat RDS objects**: HRA001748(10), HRA005191(6), HRA000087(2) |
| `SOMATIC_CNV_TSV` | 4 | copy number |

**Two names in §13's "input formats" column have zero nodes in the graph.** They are what the
delivery card declares, not graph semantic names. Querying them returns 0 rows every time — do not
conclude `no_candidate` from that:

| what the card says | what to actually query |
|---|---|
| `SCRNA_OBJECT_RDS` (0 nodes) | `BIO_DATA_CONTAINER_OBJECT` (18 — these *are* the Seurat RDS files) |
| `DNA_GENOMIC_ALIGNMENT_BAM` (0 nodes) | `DNA_ALIGNMENT_BQSR_BAM` (DNA, 6177) or `RNA_TRANSCRIPTOME_ALIGNMENT_BAM` (RNA, 3288) |

**An index is a companion file, not a second dataset.** Every BAM has a BAI and every `vcf.gz` has a
`gz.tbi`; the cards declare `tumor_bai` / `filtered_vcf_index` as required slots, so one missing index
means the run will not start. Both naming shapes occur in the graph:
`HRR1402616.BQSR.bam` → `HRR1402616.BQSR.bai` (extension swapped) and `X.bam` → `X.bam.bai`
(suffix appended); `HRS1029945.snv.vcf.gz` → `….vcf.gz.tbi`. Same directory. The server fills these
in — just give the primary data file.

**Only one single-cell RDS actually runs**: `HRA000087-merge.rds`
(`/hpcdisk1/cbb_group/data/analysis/HRA000087/HRA000087-Seurat-RDS-files/`). The other 16 have
incompatible object structures, so all nine RDS-consuming pipelines
(`breast_cellchat` / `scrna_cell_communication` / `lung_tme_annotation_cnv` …) use that one.

**Alternative splicing**: rMATS-style analysis consumes RNA alignment BAMs — take
`RNA_TRANSCRIPTOME_ALIGNMENT_BAM` (row 5). `RNA_SPLICEJUNCTION_TAB` is STAR's precomputed junction
table and exists only for HRA001272.

### 12.4 Untrustworthy sample fields (measured on the 0826 delivery; never filter on these)

The delivery overwrote **sample-level facts with study-level defaults**. The bad values are
neither empty nor malformed — every cell is populated and every value looks plausible on its own — so
a query against them returns rows happily and simply selects the wrong samples. Four rules:

1. **`tumor_descriptor` can no longer separate primary / metastatic / recurrent.** The whole graph
   holds only `Primary` 8551, `Metastasis` 12, null 1902 — the former `Metastatic` (210) and
   `Recurrent` (407) were flattened into `Primary`, and **1512 samples with `tissue_type = 'Normal'`
   are now tagged `Primary`** (a normal blood draw labelled "primary tumour" — self-contradictory).
   Read the site from **`sample_name` suffixes** instead. HRA001272 encodes them as: `PT` primary 143,
   `NC` adjacent-normal control 85, `LM` lung met 65, `PM` peritoneal met 31, `RT` recurrent 28,
   `BM` bone met 20, `AGM` adrenal-gland met 19, `LNM` lymph-node met 19, `BRM` brain met 5,
   `KM` kidney met 2 (e.g. `M019_LM1_S2010-10889_2`).

2. **`biospecimen_anatomic_site` is the study's primary site, not the sample's.** All 698 HRA001272
   samples read `Liver And Intrahepatic Bile Ducts` even though their names show ten distinct
   metastatic sites (above). **Filtering metastatic site on this property is always wrong.**
   HRA006499 was likewise collapsed to a single value.

3. **`gender` is case-inconsistent**: `Male` 6474 / `Female` 3931 / `male` 56 / `female` 3, plus one
   literal `missing`. Always compare `toLower(s.gender)`; `= 'Male'` silently drops 56 samples.

4. **`specimen_type` is applied inconsistently across cohorts.** `Peritumoral` survives only in
   **HRA000021** (508); the 525 adjacent-normal samples of HRA001272 / HRA003107 / HRA001749 /
   HRA007169 / HRA001748 / HRA006499 were folded into `Patient_Solid_Tissue`. A new semicolon
   multi-value `Organoid;Patient_Solid_Tissue` (486) also appears — match with `CONTAINS`, never `=`.
   Reassuringly, all 525 folded samples retain `tissue_type = 'Normal'`, so **tumour/normal pairing,
   which keys on `tissue_type`, is unaffected**. A standalone `Organoid` (30) also appears alongside
   the multi-value.

**`tissue_type` no longer has nulls.** The 0821 delivery left 829 samples null; 0826 fills all of
them — the graph now reads Tumor 7045 / Normal 2863 / Blood 557. An `IS NOT NULL` guard is no longer
needed, but `Blood` still means this is **not** a clean Tumor/Normal binary.
**HRA000071's `tissue_type` was genuinely fixed in 0821**:
`Blood`/`Normal` 286 plus `Patient_Solid_Tissue`/`Tumor` 286, matching the 286 `B_` and 286 `T_`
sample-name prefixes exactly (the old data was the wrong one). That cohort can be trusted directly.
