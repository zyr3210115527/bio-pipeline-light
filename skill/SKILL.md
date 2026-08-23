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

## 1. Tools (9)

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

## 2. Graph model (0821 delivery: 81,621 nodes / 364,184 relations)

| Node | Key properties (caveats) |
|---|---|
| `tool` (51) | `tool_name`, `function` (Chinese sentence), `semantic_output` (`;`-separated), `catalog_id` (T001…) |
| `function` (90) | Analysis function, Chinese sentence; match with CONTAINS substring |
| `format` (35) | e.g. `RAW_PAIRED_END_R1_FASTQ`, `DNA_VARIANT_VCF_GENERAL`, `MUTATION_ANNOTATION_FORMAT_MAF`, plus `CLINICAL` / `*_META` |
| `modal` (6) | **Only** `WES` / `WGS` / `bulk_RNA` / `sc-RNA` / `Clinical` / `Meta` — never invent spellings like `RNA-seq`. **The node property is `modal`, not `name`** — `(:modal {name:'sc-RNA'})` matches nothing and returns a silent zero. To find a modality's files, filter `T1.strategy = 'sc-RNA'` directly rather than traversing `in_modal` |
| `datalevel` (4) | Properties are `level` / `name` / `description`, **not** `data_level`; 1 raw → 4 knowledge. (File-side `T1.data_level` / `T2.data_level` ARE called data_level.) |
| `study` (20) / `project` (18) | `study_accession`, `tumor_type` (Title Case English, e.g. `Liver Cancer`; query with toLower + CONTAINS — one cancer has multiple spellings, see §4 recipe 3), `title`, `study_description`, `individual_count`, `sample_count` (**only 14/20 studies have it**: HRA000073/HRA000087/HRA002693/HRA006117/HRA007413/HRA016026 are null — sorting/filtering by it silently drops those 6; to size a cohort count `sample` nodes) |
| `individual` (7131) | `individual_accession`; other properties are prefix-grouped: **only `00_*` is operational** (`00_sample_accession` / `00_run_accession` / `00_platform` / `00_strategy` …). **`01_`–`13_` are all patient-level sensitive**: 01_ demographics, 02_ family history, 03_ lifestyle, 04_ hematology, 09_ tumor pathology, 10_ invasion, 11_ molecular (`11_tmb` / `11_msi_score`), 12_ treatment, **13_ survival (`13_survival_days` / `13_survival_status` / `13_pfs_time` … — survival-analysis data lives here)**. Aggregates only (count / avg / IS NOT NULL); per-individual reads are refused by the server guard (§8) |
| `sample` (10465) | `sample_accession`, `sample_name`, `tissue_type`, `specimen_type` (underscore style, e.g. `Patient_Solid_Tissue`), `gender`. **`tissue_type` is not a clean Tumor/Normal binary** (0821: Tumor 5469, Normal 2469, null 1270, multi-value `Tumor,Normal` 700, Blood 557); `specimen_type` also has `;`-separated multi-values. Always judge roles via `resolve_sample_roles`, never equality-matching |
| `T1` | Raw files (FASTQ etc.): `t1_id`, `file_name`, `file_format` (literal), `semantic_format`, `data_level`, `study_accession` (all 6 populated for 28,229); `strategy` 28,222; `platform` / `sample_accession` / `individual_accession` / `sample_name` 28,184; `run_accession` / `experiment_accession` 27,070; `file_path` 26,879; `size` 25,417. **The 45 files missing those values are Clinical / `*_META` aggregate files** (not per-sample by nature) — do not conclude "no platform/sample info in graph" from them |
| `T2` | Result files (VCF/BAM/MAF…): `t2_id`, `file_name`, `format`, `strategy`, `data_level`, `size`, `study_accession` (all 35,572); `semantic_format` 35,570; `file_path` 35,566; `run_accession` 31,717. **T2 has no `platform` / `sample_accession`** — for sample ownership walk `(T2)-[:generated_from]->(T1)-[:in_sample]->(sample)` |

Key relationships: `(tool)-[:next_tool]->(tool)` chains; `(tool)-[:input|output]->(format)` I/O contract;
`(tool)-[:suitable_for]->(modal)`; `(tool)-[:has_function]->(function)`;
`(T1|T2)-[:in_sample|in_format|in_modal|in_level|in_study]->(...)`; `(T2)-[:generated_from]->(T1)`;
`(sample)-[:in_individual]->(individual)`; `(individual)-[:in_study]->(study)`;
`(study)-[:in_project]->(project)`; `(format)-[:subclass_of]->(format)` (specific → generic; walk up
when matching tools by semantic format).

**Numeric fields are INTEGER/FLOAT (0821 re-typed) — compare unquoted, no `toInteger()`**:
`data_level`, `size`, `sample_count`, `individual_count`, `01_age`, `11_tmb`, `11_msi_score`,
`13_survival_days` / `13_dfs_time` / `13_efs_time` / `13_pfs_time`. Write `f.data_level = 1`,
`i.\`13_survival_days\` > 365`, `ORDER BY s.sample_count DESC`. Writing `= '1'` or `> '365'` silently
returns wrong/empty results (pre-0821 these were strings compared lexicographically: `'9' > '60'` held,
max survival days showed 995 instead of 7061, `data_level = 1` returned 0 rows). If results look anomalous
like this, check field types first — do not copy the anomaly into your conclusion.

## 3. Closed tool catalog (truth = bio-pipeline-kg-matcher `data/csv/catalog`; 0821 verified identical to graph)

Runtime catalog: **51 tools = 12 atomic (11 orchestrable; `multiqc` is terminal-only, never orchestrated)
+ 38 pipeline + 1 task_pipeline**, 1:1 with the 51 graph `tool` nodes. Full fields (catalog_id, I/O
formats, omics, variants, slot bindings) in `references/tool_catalog.csv`; ArtifactType vocabulary in
`references/artifact_type.csv`.

- **Atomic closed set (11)**: `bwa` `fastp` `fastqc` `featurecounts` `gatk` `bcftools` `snpeff` `samtools` `star` `trim_galore` `rsem` (`multiqc` terminal-only)
- **task_pipeline (1)**: `rnaseq_singletask`
- **pipeline (38)**: `diff_expr_go` `diff_expr_kegg` `immune_infiltration_iobr` `wes_somatic_maf_landscape` `wes_somatic_pair` `survival_analysis` `tmb_survival_analysis` `her2_pfs_survival` `driver_gene_gender_analysis` `rnaseq_unsupervised_cluster` `wgcna` `wgcna_hub` `wgcna_module_trait` `cellranger_workflow` `paired_fastq_to_unmapped_bam` `cnvkit_cnv_clinical` `cox_model` `km_survival` `gsea_pathway_enrichment` `hvg_pca_gmm` `preprocess_counts` `rmats_alternative_splicing` `scrna_cell_communication` `bootstrap_stability` etc. (full table in CSV)

Catalog rules (decide Plan shape):

- `recommendations[]` carries **business pipelines** (38 + task); `candidates[]` carries **only atomic chains that pass closed-set validation** (within the 11).
- **Non-atomized needs** (differential expression, enrichment, WGCNA, survival …) have no atomic
  expression → `candidates[]` returns `unsupported`; **never pad an atomic chain with pipeline nodes**.
  `recommendations[]` still carries the business pipeline as usual.
- **Variant binding**: `gatk` has `single` (sorted_dedup_bam) and `paired` (four slots tumor_bam/tumor_bai/
  normal_bam/normal_bai, `exactly_one_variant=true`); `fastp` has single_end / paired_end variants.
  Paired tumor/normal WES must use the 4-slot variant and `find_paired_tumor_normal_samples.cypher`.
- The slot model (slot names, `builder_param` / `wdl_target` bindings) is an **execution-side contract**
  from `data/csv/catalog`; it is not in the graph. The graph only says which tools exist and how they chain.
- Data availability semantics: files precisely confirmed via Neo4j are `available`, otherwise
  `missing_from_graph`. Execution-side resources (GTF, reference genomes, indexes) are not part of
  availability judgment.

## 4. Query cookbook

**15 official Cypher templates** live in `references/query_templates/`, use by name (all directly
executable via `read_cypher`; 0821 audit: 15/15 return rows, see `benchmark/template_audit.py`).
**Copy property names with exact case** — writing `t1_id` as `T1_id` does not error, it silently
returns 0 rows:

| Template | Purpose |
|---|---|
| `find_tools_by_function` | Tools by Chinese function substring (`has_function` CONTAINS) |
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

1. **Request → tool matching**: multi-keyword OR over `tool_name` / `function`. Naming patterns:
   `deg_*` / `de_*` = differential, `wgcna*` = co-expression, `*survival` / `km_*` / `cox_*` = survival,
   `*enrichment` = enrichment, `*cellchat` = cell communication, `tmb_*` = tumor mutation burden.
   Function text is Chinese — include Chinese keywords (e.g. `'差异'`, `'富集'`, `'生存'`).
2. **Chain assembly + verification**: for each hop, intersect upstream `output` / `semantic_output`
   with downstream `input` formats. Report gaps honestly ("graph missing: <hop>, expected input
   <format>; suggestion <filler tool or note>") — **never fabricate a tool that does not exist**.
3. **Data selection** (English vocab; ready-made matrices live in T2):
   - Cohort: `tumor_type` is **English Title Case** — match with `toLower(s.tumor_type) CONTAINS '<english>'`;
     Chinese matches nothing. All values measured 0821 (20 studies): `Liver Cancer`,
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
     studies have same-individual Tumor+Normal first. `tissue_type` has multi-value cells (HRA016026's
     700 samples are all `'Tumor,Normal'`), so tolerate both multi-values and name suffixes:
     ```cypher
     MATCH (sp:sample)-[:in_individual]->(i:individual)
     WITH sp.study_accession AS study, i,
          collect(DISTINCT toLower(coalesce(sp.tissue_type,''))) AS tts,
          collect(DISTINCT toLower(coalesce(sp.sample_name,''))) AS nms
     WHERE (any(t IN tts WHERE t CONTAINS 'tumor')  OR any(n IN nms WHERE n ENDS WITH '_tumor'))
       AND (any(t IN tts WHERE t CONTAINS 'normal') OR any(n IN nms WHERE n ENDS WITH '_normal'))
     RETURN study, count(i) AS pairable_individuals ORDER BY pairable_individuals DESC
     ```
     Pairable cohorts measured 0821 (individuals): HRA000873 1015, HRA000021 508, **HRA016026 350**,
     HRA001272 206, HRA003107 155, HRA001749 84, HRA007169 76, HRA006499 72. The naive form
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
     **Cohorts whose roles cannot be resolved (0821 measured — do not burn rounds retrying)**:
     HRA000001 (557 all Blood, no tumor/control signal), HRA000074 (543/693 no `tissue_type`),
     HRA005191 (243 none), HRA002693 (213/655 none), HRA006117 (265/835 none), HRA000122 (6/287 none).
     Upstream simply never provided values — no query rewrite will find them. Tell the user roles are
     incomplete, or switch to a pairable cohort above.
     **This blocks per-sample pairing only.** Cohort-level analyses on these same cohorts (differential
     expression, enrichment, clustering, immune deconvolution, survival) run off the aggregate matrix /
     MAF and do their own grouping internally — never downgrade one to `no_candidate` just because
     tumor/normal roles are unresolvable, and do not call `resolve_sample_roles` to pick an aggregate
     file in the first place.
   - **Cohort sample lists come from `sample` nodes**: `MATCH (sp:sample) WHERE sp.study_accession = '<HRA*>'`
     (equivalent to the `study<-individual<-sample` traversal). **Do not count samples via
     `(T1)-[:in_sample]->(sample)`** — that only sees file-attached samples and silently drops the rest
     (HRA006117 has 835 samples; via files only 570 remain).
   - **Two kinds of `sample_accession = null` on files — do not conflate**:
     1. **Aggregate files** (expression matrices / MAF / clinical tables / MetaInfo) are cross-sample by
        nature; null is normal;
     2. **run-organized fastq** (`data_level=1`) should have samples. **Post-0821 this class is basically
        zero**: the new export puts `sample_accession` directly on T1 (no run hop) — 28,184 of 28,229 T1
        have `in_sample` edges, the remaining 45 are all aggregates; every T1 with `run_accession` is
        linked. (Pre-0821 the graph was T1→run→sample two-hop and each sample recorded only one run,
        orphaning 3,758 runs / 7,516 T1 — **that gap no longer exists; do not reject cohorts on the old
        conclusion**.)
     Judge gaps **only** by `resolve_sample_roles(study=...)` → `file_coverage.t1_files_unlinked`
     (files truly lacking `in_sample` edges; e.g. HRA000087 2/3108, HRA001272 2/2362, all aggregates).
     The sibling field `runs_without_sample_node` stays large (1492/1553, 482/1180) — it is a
     **diagnostic field, not a gap**: each sample node records only one run, so run-based back-lookup
     never reconciles; using it to judge cohorts kills good cohorts. If `t1_files_unlinked` really is
     large, output `missing_from_graph` honestly — **never guess sample ownership from file names or order**.

## 5. Planning pipeline (5 steps)

1. **Parse the request**: analysis type, modal, target artifacts (ask first if unclear: existing data
   format / cohort / grouping).
2. **Match** (recipe 1): candidate tools + functions + formats; decide business pipeline vs atomic
   chain (catalog rules, §3).
3. **Assemble & verify the chain** (recipe 2): data → preprocessing → alignment → quantification/variant
   → downstream; annotate tool, input format, output format, verification point per hop; list gaps honestly.
4. **Select data** (recipe 3): cohort + format + sample constraints + pairing; give file counts, sources,
   `file_path`, availability flags.
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
   calls; re-validating does not improve the answer, it only burns rounds. (0821 observation: a model on
   low thinking budget called `validate_plan` 7–10 times until the round cap, `grounded` true throughout.)
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

## 9. Output contract: tool-chain/v2 (front-end truth = bio-pipeline-kg-matcher's pipeline_router)

**Hard rule — violation counts as task failure**: the final answer must be **exactly one tool-chain/v2
JSON object** — no prose, no markdown fences, no multiple candidates, no text before or after the JSON.
`recommendations[0]` is the single strict top-1 recommendation; `candidates[]` is filled only when an
atomic chain is possible. **When `selection_status` is `information` / `unsupported` / `no_candidate`,
`recommendations` may be empty** — for pure data-distribution or inventory questions do not invent a
pipeline just to fill the slot (that is fabrication); every other status requires a rank-1 entry. Human-readable note fields (`match_note` etc.) may be written in the user's
language.

**`read_cypher` row cap (affects conclusion correctness)**: at most **500 rows** per call. When exceeded,
the return carries `truncated: true` and `row_count` — **what you hold is then a truncated sample, not the
full set**: never conclude "there are N in total / all are / there is no other" from it. For totals re-query
with `count(...)` / aggregation; for details add stricter filters (study accession, format, data_level).
Only results without `truncated` are complete result sets.

**Naming contract (Knowledge Card alignment)**: an atomic tool's `tool_id` must use the Knowledge Card's
`meta.id` (e.g. `bwa_mem_paired`, not `bwa`); `tool_chain.inputs` and output references use card-defined
I/O names (e.g. `read1` / `aligned_sam`). Mapping in `references/knowledge_cards_map.json` (12 atomic
cards). Pipeline-level tools (no card, e.g. `diff_expr_go`) keep the graph tool_id and are annotated with
`"card": null` next to `tool_id`.

**Author judgment fields only — `hydrate_plan` fills the rest.** Do not hand-write any field the
catalog/graph already knows; `hydrate_plan` fills them deterministically and overwrites what you wrote
with the graph's own facts, so authoring them only costs generation time and invites fabrication
(measured: models invented `mcp_timing_ms` and `file_path` values). Leave out
`match_id` / `rank` / `source` / `reference_case_id` / `recommendation_count` / `candidate_count` /
`planner_metadata` / `data_matcher_mode` / `mcp_timing_ms`; inside `tool` write only `tool_id`
(catalog_id, tool_kind, name, description, inputs, outputs are filled); inside each asset write only
`file_name` and `match_reason` (**never write `file_path` from memory**); inside `candidates[].tool_chain`
write only each step's `tool_id`. What you must supply: `schema_version`, `selection_status`, `intent`,
and per recommendation `pipeline_id`, `match_note`, `data.assets[].file_name` + `match_reason`, plus the
tool_chain ordering.

**Assets: supply the primary datum only.** The primary datum is the pipeline's core input — the
expression matrix, the MAF, or the FASTQ pair. `hydrate_plan` completes the rest deterministically:

- When the pipeline declares a `CLINICAL_DATA_EXCEL` input slot, the study's clinical table **and** its
  sample-metadata table are appended (`METADATA_SAMPLE_INFO` is the sample↔patient join table — without
  it the clinical fields cannot be attached to the matrix/MAF, and the graph always delivers the two
  together, one of each per study).
- The expression matrix is normalised to the pipeline's default quantification flavour. A study carries
  FPKM, TPM and counts versions whose graph properties are identical
  (`semantic_format` = `TABULAR_BIO_DATA`, `data_level` = 2) — only the file name distinguishes them, so
  the pipeline decides, not the caller. The **first** flavour named in the catalog description wins, so
  that a description like "适用于 FPKM/TPM 定量数据" resolves to one file rather than two possible
  answers. When the description names no flavour, the method itself decides: the **WGCNA family runs on
  raw `counts`** (its own guidance is counts/VST, not TPM), while anything that **compares one gene's
  level across samples** — KM/Cox survival grouping, box plots, stage heatmaps, UMAP, pre-ranked GSEA —
  needs length- and depth-normalised **TPM**, since counts are not comparable between samples.
- When the required semantic format has **exactly one** study-level delivery file in that cohort — a name
  beginning `HRA<digits>-`, e.g. `HRA007169-SomaticSNV-1.0.maf` — a per-sample file you picked
  (`HRR1725089.maf`, one patient out of 77) is swapped for it. "Exactly one" is what keeps this safe:
  FASTQ has no study-level file so nothing moves, and expression matrices have three, so the flavour rule
  above decides those instead. Only the aggregate-plus-per-sample formats (MAF, somatic CNV) land here.

What is not completed for you: the primary datum itself. It must be a file that actually exists in the
graph, and `assets` must be non-empty whenever `selection_status` is `ok` — if the graph holds no usable
data, say so with `no_candidate` plus a `match_note`, rather than shipping a recommendation with no data.
This holds even when the request names no cohort: locate a cohort by cancer type / omics, filter by
`semantic_format`, and take the **first file under `ORDER BY n.file_name`** as the representative sample
(an f1/r2 pair for paired-end sequencing). Order it explicitly — a bare `LIMIT` makes the same question
resolve to different files on different runs. When a cancer type spans several cohorts and the user named
none, take the one with the most samples — it has the widest coverage and, being a property of the graph
rather than of the phrasing, makes the same question resolve to the same cohort every time: glioma →
**HRA000074** (693 samples, over HRA000073's 325 and HRA000071's 572), liver → **HRA001272** (698;
use it for mutation, expression and raw data alike),
esophageal → HRA003107, AML → HRA006117. Melanoma splits by data type instead: expression matrices live
in HRA007167, WES/MAF in HRA007169. Single-cell (10x / CellRanger) exists in only three cohorts —
**HRA001748** (571 files), HRA000087, HRA005191. Always re-check that the chosen cohort actually
carries the semantic format you need — HRA000073/74 are RNA-only, so a MAF analysis against them
finds nothing.

The schema below shows the **hydrated** result — i.e. what `hydrate_plan` returns and what the front-end
consumes, not what you type. When delivering to a front-end / for integration, produce this JSON
(front-ends read only the `result.structuredContent` layer of JSON-RPC):

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

Key points: `assets` carry per-file provenance and `match_reason`; `inputs/outputs` `artifact` values use
the ArtifactType vocabulary (`references/artifact_type.csv`); `candidates[]` is filled only for viable
atomic chains, else empty with `selection_status` explaining why. **Single-sample assets (FASTQ/BAM etc.)
carry `sample_role` / `sample_role_label` only when you already hold a `resolve_sample_roles` result;
otherwise set them to null. Aggregate assets (matrices/MAF/clinical tables) are always null.** More
generally: **any contract field you cannot fill goes to null with a one-line note in `match_note` — never
spend a round chasing a single field, and never withhold a recommendation over one.** (Measured failure:
a case deliberating over `sample_role` produced 40k characters of reasoning, hit the token ceiling, and
returned an empty answer after 200s.) For paired/grouped analyses, attach `alternatives[]`
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
## 四、可执行性：工具环境（实测 which）、数据可达性（实测路径）、参考文件、算力估计
```

## 10. Pre-submission gate (execution-contract validation, scenario 1)

When the user/front-end is about to **submit a chain to the execution side** (or asks "can this chain
run / what is missing"), call `validate_execution_chain` for a 5-stage probe instead of answering
"it runs" directly:

1. **Registration**: every tool_id known (graph / Knowledge Card)
2. **Card contract**: each step's required Knowledge Card inputs all present (missing one → reject)
3. **Binding structure**: File-input bindings must be objects (file_id/file_name); scalar types must match
   the card declaration
4. **Data probe**: unbound File inputs → count in-graph candidate files (by format family + optional cohort)
5. **Chain flow**: next_tool adjacency + up/downstream format continuity

Output is a `tool-chain-validation/v1.1` stage-by-stage report plus `execution_params` (input name → real
in-graph file path; only `/`-rooted confirmed paths, never fabricated), `execution_params_missing`, and
`submittable`. **Submit only when errors are zero and `submittable=true`**; on `submittable=false` do not
claim "this chain runs" — list `execution_params_missing` honestly. When a pipeline-level tool has no card,
warn explicitly that contract validation was skipped.

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
- Read-only throughout; any write intent is explained first, executed only after approval.

## 12. Appendix: measured snapshots (whitelist sources; re-verify after graph updates)

Measured on the connected graph. Tool/cohort matching should start here, not with exploratory queries.

### 12.1 Tool catalog snapshot (51)

Several pipelines differ by exactly one discriminating detail; the full descriptions below carry it,
so read to the end of the row rather than matching on the opening clause. The families that actually
get confused in practice:

- `diff_expr_go` (GO functional enrichment) vs `diff_expr_kegg` (pathway / Reactome enrichment) — both
  are limma two-group DE on an expression matrix alone; the enrichment target is the only difference.
- `gsea_pathway_enrichment` does **not** pre-select DEGs (pre-ranked GSEA over all genes), and
  `deg_enrichment` / `de_enrichment` are for when the user **explicitly supplies sample metadata and a
  clinical table**, or asks for survival association. Phrasing like "case group vs control group" does
  *not* select them — every DE pipeline groups samples; that is not a discriminator.
  Neither is the answer to a plain "find DEGs, then enrich them" request.
- `survival_analysis` stratifies by a **named gene's mutation status** (MAF) and `tmb_survival_analysis`
  by **TMB median**. Grouping by a gene's **expression level** is `her2_pfs_survival` — it is the
  default for that whole shape, whatever the gene (HER2/ERBB2 is only its default, not its scope).
  `km_survival` and `cox_model` are generalised variants: pick them only when the user explicitly asks
  for overall survival or multivariate Cox modelling rather than expression-stratified PFS.
- `rnaseq_unsupervised_cluster` is the end-to-end chain from counts; `preprocess_counts`,
  `hvg_pca_gmm` and `bootstrap_stability` are single steps carved out of it and take logCPM.
- `wgcna` is the full co-expression chain and **the default for any co-expression / hub-gene request** —
  asking for "stable modules" or "related pathways" does not move you off it. `wgcna_hub` applies only
  when the user wants grouping parsed automatically from raw CNCB metadata; `wgcna_module_trait` only
  when they explicitly want survival analysis layered on top of the co-expression network.

| tool | function | modal | inputs | outputs |
|---|---|---|---|---|
| `bcftools` | 对 GATK 过滤后的体细胞 VCF 文件进行后处理，包括提取 PASS 位点、基于参考基因组进行左对齐和拆分多等位基因位点、建立 Tabix 索引，并生成详细的 QC 统计文件（记录数、SNP/Indel 计数、FILTER 分布、DP/AF 等）。输出可直接用于 SnpEff 注释。 | WES | DNA_VARIANT_VCF_GENERAL,DNA_VARIANT_INDEX_TBI,REFERENCE_GENOME_FASTA | DNA_VARIANT_VCF_GENERAL,TABULAR_BIO_DATA,DNA_VARIANT_INDEX_TBI |
| `bootstrap_stability` | 对聚类分析执行Bootstrap重采样，通过比较不同运行间的ARI/NMI评估聚类稳定性， 统计最佳K的频率，并利用标签置换零分布评估稳定性显著性。 输入为logCPM表达矩阵，输出包含稳定性指标、最佳K频率、置换检验P值及可视化图表。 | bulk_RNA | - | TABULAR_BIO_DATA,VISUALIZATION_RESULT |
| `breast_cellchat` | 基于CellChat方法分析乳腺癌单细胞转录组数据中的细胞间通讯网络。输入为Seurat格式的RDS文件，通过比较肿瘤与正常组织中的配体-受体互作，揭示肿瘤微环境中的细胞通讯变化。输出包括通讯网络分析结果、质控报告和运行日志。 | bulk_RNA,sc-RNA | SCRNA_OBJECT_RDS,REFERENCE_GENOME_FASTA | VISUALIZATION_RESULT,QC_STATS_REPORT |
| `bwa` | 基于 BWA-MEM 算法的双端测序比对流程。输入为 R1/R2 FASTQ 文件和样本 ID， 输出为未排序的 SAM 文件及 BWA 运行日志。适用于全外显子组测序数据的比对步骤， 后续需配合 SAMtools 完成排序和索引。 | WES | REFERENCE_GENOME_FASTA,RAW_PAIRED_END_R1_FASTQ,RAW_PAIRED_END_R2_FASTQ | DNA_GENOMIC_ALIGNMENT_BAM |
| `cellranger_workflow` | 基于 10x Genomics CellRanger 的单细胞 RNA 测序数据分析流程。 包含 FASTQ 质控、序列比对、基因表达定量及结果可视化，适用于 10x Chromium 平台产生的单细胞转录组数据。 | sc-RNA,bulk_RNA | RAW_SINGLE_END_FASTQ,DNA_GENOMIC_ALIGNMENT_BAM | TABULAR_BIO_DATA,DNA_GENOMIC_ALIGNMENT_BAM,QC_STATS_REPORT |
| `celltype_case_control_de` | 对单细胞RNA-seq数据中指定的细胞类型进行病例-对照差异表达分析。输入为Seurat RDS文件，输出包括差异表达结果、分析摘要、质控报告等。适用于配对或非配对的病例-对照研究设计。 | sc-RNA,bulk_RNA | SCRNA_OBJECT_RDS,TABULAR_BIO_DATA,REFERENCE_GENOME_FASTA | QC_STATS_REPORT |
| `cnvkit_cnv_clinical` | 对肿瘤队列的配对肿瘤/正常 WGS 或 WES BAM 运行 CNVkit，生成样本级分段、离散拷贝数、scatter/diagram 图，并可选汇总高频基因 CNV 与临床分期及总生存的探索性关联。输入为样本ID、肿瘤/正常BAM/BAI数组，输出包括CNV分段文件、BED文件、可视化图以及临床关联分析结果。 | Clinical,WES,WGS | DNA_GENOMIC_ALIGNMENT_BAM,CLINICAL_DATA_EXCEL,TABULAR_BIO_DATA | VISUALIZATION_RESULT,TABULAR_BIO_DATA |
| `cox_model` | 整合基因表达矩阵与临床元数据，执行 Cox 比例风险回归分析和 Kaplan-Meier 生存曲线绘制。 支持自定义样本分组、生存时间/状态列映射，输出风险比、P 值及前 N 个显著基因。 适用于癌症预后标志物筛选和临床亚组生存差异分析场景。 | Clinical,bulk_RNA | CLINICAL_DATA_EXCEL,METADATA_SAMPLE_INFO,TABULAR_BIO_DATA | QC_STATS_REPORT,TABULAR_BIO_DATA |
| `dataset_downstream` | 对单细胞RNA-seq数据集进行标准化下游分析，包括基因排序、细胞类型注释和恶性细胞标记。 输入为Seurat RDS文件和基因排序文件，输出包括压缩的结果文件、运行摘要和质量控制报告。 | sc-RNA | TABULAR_BIO_DATA,REFERENCE_GENOME_FASTA,SCRNA_OBJECT_RDS | QC_STATS_REPORT |
| `dataset_matrix_annotation` | 该流程用于对单细胞RNA-seq数据集进行矩阵注释和细胞类型标注。输入为Seurat RDS格式的整合数据文件，输出包括注释结果压缩包、运行摘要、输入质量控制报告和分析清单等文件。 | sc-RNA | TABULAR_BIO_DATA,SCRNA_OBJECT_RDS,REFERENCE_GENOME_FASTA | QC_STATS_REPORT |
| `de_enrichment` | 本流程整合 CNCB 元数据，执行差异表达分析并生成富集分析结果。支持自动样本分组、生存分析关联，输出火山图、热图及富集分析可视化。适用于具有临床元数据的 bulk RNA-seq 数据。 | bulk_RNA,Clinical | METADATA_SAMPLE_INFO,CLINICAL_DATA_EXCEL,TABULAR_BIO_DATA | QC_STATS_REPORT,TABULAR_BIO_DATA |
| `deg_enrichment` | 本流程整合表达矩阵、样本元数据和临床信息，执行差异表达分析并生成火山图、热图及功能富集分析结果。 支持自动分组识别、生存分析关联，适用于批量 RNA-seq 数据的标准化差异表达与富集分析场景。 | bulk_RNA,Clinical | TABULAR_BIO_DATA,METADATA_SAMPLE_INFO,CLINICAL_DATA_EXCEL | TABULAR_BIO_DATA,QC_STATS_REPORT |
| `deg_trend` | 本流程用于差异表达基因(DEG)的趋势分析与可视化。输入基因表达矩阵、样本元数据和临床信息，自动完成样本分组、差异分析，并生成火山图、热图、箱线图和趋势图等多种可视化结果。适用于批量 RNA-seq 数据的临床关联分析场景。 | bulk_RNA,Clinical | METADATA_SAMPLE_INFO,CLINICAL_DATA_EXCEL,TABULAR_BIO_DATA | TABULAR_BIO_DATA,VISUALIZATION_RESULT,QC_STATS_REPORT |
| `diff_expr_go` | 基于表达矩阵进行差异基因分析（limma）并针对上下调基因分别进行 GO 功能富集（clusterProfiler）。 适用于 FPKM/TPM 定量数据的两组比较场景，输出差异基因列表及 GO 富集结果表。 | bulk_RNA | TABULAR_BIO_DATA | TABULAR_BIO_DATA |
| `diff_expr_kegg` | 基于 limma 包进行两组样本差异表达分析，并使用 ReactomePA 对上下调基因进行通路富集。 适用于人类基因表达矩阵（FPKM/TPM），输出差异基因列表及富集结果。 | bulk_RNA | TABULAR_BIO_DATA | TABULAR_BIO_DATA |
| `driver_gene_gender_analysis` | 该流程基于 WES MAF 文件、临床表和 MetaInfo 表，对驱动基因的突变频率进行性别分层分析。 通过卡方检验比较男性和女性样本中每个驱动基因的突变率，并输出统计结果表、诊断表及多种可视化图表（分组柱状图、瀑布图、热图、火山图）。 | Clinical,WES | CLINICAL_DATA_EXCEL,MUTATION_ANNOTATION_FORMAT_MAF | TABULAR_BIO_DATA,VISUALIZATION_RESULT,MUTATION_ANNOTATION_FORMAT_MAF |
| `fastp` | 对双端测序FASTQ文件进行质量过滤、接头修剪和质控报告生成。输入为样本ID和双端FASTQ文件，输出为修剪后的FASTQ文件以及HTML和JSON格式的质控报告。适用于WES等双端测序数据的预处理步骤。 | WES | RAW_PAIRED_END_R1_FASTQ,RAW_PAIRED_END_R2_FASTQ | QC_STATS_REPORT,RAW_PAIRED_END_R2_FASTQ,RAW_PAIRED_END_R1_FASTQ |
| `fastqc` | 对输入的 FASTQ 文件进行质量评估，生成 HTML 和 ZIP 格式的 FastQC 报告。 适用于 WES、WGS、RNA-seq 和单细胞测序等多种测序数据类型，可接收原始或修剪后的 FASTQ 文件。 | bulk_RNA,sc-RNA,WES,WGS | RAW_PAIRED_END_R1_FASTQ,RAW_PAIRED_END_R2_FASTQ | QC_STATS_REPORT |
| `featurecounts` | 该流程使用 featureCounts 工具对 RNA-seq 比对后的 BAM 文件进行基因水平计数。 输入为最终 BAM 文件和 GTF 注释文件，输出为基因计数矩阵、统计摘要和运行日志。 适用于 RNA-seq 定量分析中的基因表达计数步骤。 | bulk_RNA | DNA_GENOMIC_ALIGNMENT_BAM | QC_STATS_REPORT,TABULAR_BIO_DATA |
| `gatk` | 基于 GATK 最佳实践的全外显子组（WES）肿瘤-正常配对体细胞变异检测流程。 流程对肿瘤和正常样本分别进行 MarkDuplicates 标记重复、BaseRecalibrator 碱基质量校正， 然后使用 Mutect2 进行体细胞变异检测，并通过 FilterMutectCalls 进行过滤， 同时评估样本污染和构建读段方向偏倚模型。 | WES | DNA_ALIGNMENT_INDEX_BAI,REFERENCE_GENOME_FASTA,TARGET_INTERVAL_LIST,DNA_GENOMIC_ALIGNMENT_BAM | DNA_GENOMIC_ALIGNMENT_BAM,QC_STATS_REPORT,DNA_VARIANT_INDEX_TBI,DNA_VARIANT_VCF_GENERAL |
| `gene_boxplot` | 基于基因表达矩阵和临床元数据生成箱线图、火山图、热图等可视化结果。支持从 CNCB 原生格式元数据自动映射样本分组信息，可整合生存分析和肿瘤分期数据。适用于 bulk RNA-seq 数据的探索性可视化分析。 | Clinical,bulk_RNA | METADATA_SAMPLE_INFO,TABULAR_BIO_DATA,CLINICAL_DATA_EXCEL | QC_STATS_REPORT,TABULAR_BIO_DATA |
| `gsea_pathway_enrichment` | 本流程基于limma moderated t统计量构建全基因排序，使用fgseaMultilevel执行预排序GSEA。 输入为表达矩阵和样本元数据，输出包括通路富集结果、显著通路、排序基因列表及可视化图表。 适用于病例-对照转录组比较分析，支持协变量校正和配对设计。 | bulk_RNA | TABULAR_BIO_DATA | VISUALIZATION_RESULT,TABULAR_BIO_DATA,QC_STATS_REPORT |
| `her2_pfs_survival` | 基于 TPM 表达矩阵、临床信息及样本元信息，分析特定基因（默认 HER2）表达水平与无进展生存期（PFS）的关联。 流程自动匹配样本 accession，执行 Winsorizing 处理，生成 KM 生存曲线、Logrank 统计量及质量控制报告。 | Clinical,bulk_RNA | CLINICAL_DATA_EXCEL,TABULAR_BIO_DATA | VISUALIZATION_RESULT,QC_STATS_REPORT,TABULAR_BIO_DATA |
| `hvg_pca_gmm` | 从logCPM表达矩阵中筛选高变基因，执行PCA降维，并在候选K范围内拟合高斯混合模型（GMM），最终依据BIC选择最佳聚类数。输入为预处理后的logCPM矩阵，输出包括高变基因统计、PCA结果、GMM聚类指标及可视化图表。 | bulk_RNA,sc-RNA | - | TABULAR_BIO_DATA,VISUALIZATION_RESULT,QC_STATS_REPORT |
| `immune_infiltration_iobr` | 基于 IOBR 包的 CIBERSORT 算法进行免疫细胞浸润分析流程。 输入基因表达 TPM 矩阵、临床信息和样本元数据，输出免疫细胞比例估计、可靠性评估及可视化图表。 适用于批量 RNA-seq 数据的肿瘤微环境免疫细胞组成分析。 | bulk_RNA,Clinical | CLINICAL_DATA_EXCEL,TABULAR_BIO_DATA | TABULAR_BIO_DATA,VISUALIZATION_RESULT,QC_STATS_REPORT |
| `immunotherapy_cellchat` | 基于CellChat的免疫治疗细胞通讯分析流程。输入Seurat格式的单细胞RNA-seq数据，通过比较响应者与非响应者之间的细胞通讯网络差异，揭示免疫治疗相关的细胞间相互作用机制。输出包括通讯网络分析结果、质控报告和运行日志。 | sc-RNA | SCRNA_OBJECT_RDS,REFERENCE_GENOME_FASTA | VISUALIZATION_RESULT,QC_STATS_REPORT |
| `ipf_trajectory_regulon` | 对特发性肺纤维化(IPF)单细胞RNA-seq数据进行轨迹推断和调控子分析。输入为Seurat RDS对象，输出包括分析结果压缩包、运行摘要、质控报告和文件清单等。 | bulk_RNA,sc-RNA | SCRNA_OBJECT_RDS,METADATA_SAMPLE_INFO,REFERENCE_GENOME_FASTA | QC_STATS_REPORT |
| `km_survival` | 整合基因表达矩阵与临床元数据，执行 Kaplan-Meier 生存分析和 Cox 比例风险模型。 支持样本分组、肿瘤分期过滤和生存数据验证，输出生存曲线及统计结果。 | bulk_RNA,Clinical | TABULAR_BIO_DATA,METADATA_SAMPLE_INFO,CLINICAL_DATA_EXCEL | TABULAR_BIO_DATA,QC_STATS_REPORT |
| `lung_tme_annotation_cnv` | 基于单细胞RNA-seq数据对肺癌肿瘤微环境进行细胞类型注释和拷贝数变异(CNV)分析。 输入为Seurat RDS文件和基因排序文件，输出包括压缩的结果包、运行摘要、质控报告和分析清单。 | sc-RNA | SCRNA_OBJECT_RDS,TABULAR_BIO_DATA,REFERENCE_GENOME_FASTA | QC_STATS_REPORT |
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
| `stage_heatmap` | 本流程用于生成基于肿瘤分期的基因表达热图可视化。整合表达矩阵、元数据文件和临床信息文件，自动匹配样本信息并筛选目标分期样本，输出分期热图及样本映射报告。适用于 CNCB 等公共数据库来源的 bulk RNA-seq 数据可视化分析。 | Clinical,bulk_RNA | TABULAR_BIO_DATA,METADATA_SAMPLE_INFO,CLINICAL_DATA_EXCEL | VISUALIZATION_RESULT,TABULAR_BIO_DATA,QC_STATS_REPORT |
| `star` | 该流程使用 STAR 比对工具对 RNA-seq 数据进行 rRNA 去除和基因组比对。流程包含两个步骤：首先将 reads 比对到 rRNA 参考索引以去除 rRNA 污染，然后将未比对的 reads 比对到基因组参考索引，输出未排序的基因组 BAM、转录组 BAM、基因计数文件和日志。 | bulk_RNA | REFERENCE_GENOME_FASTA,RAW_PAIRED_END_R2_FASTQ,RAW_PAIRED_END_R1_FASTQ | RNA_TRANSCRIPTOME_ALIGNMENT_BAM,DNA_GENOMIC_ALIGNMENT_BAM,REFERENCE_GENOME_FASTA,RAW_PAIRED_END_R1_FASTQ |
| `survival_analysis` | 基于 WDL 1.0 和 Cromwell 的生存分析流程，用于评估指定基因突变状态与无进展生存期（PFS）的关系。 流程整合了突变提取、Log-rank 检验、Kaplan-Meier 曲线绘制及单因素 Cox 回归分析，最终生成汇总报告。 | WES,Clinical | CLINICAL_DATA_EXCEL,MUTATION_ANNOTATION_FORMAT_MAF | QC_STATS_REPORT,VISUALIZATION_RESULT,CLINICAL_DATA_EXCEL |
| `tcell_intervention` | 该流程用于对单细胞RNA-seq数据进行T细胞干预前后的比较分析。输入为Seurat RDS文件，通过指定细胞类型、时间点和患者信息等元数据列，进行差异表达分析，输出包括压缩的结果文件、运行摘要、质控报告和分析清单等。 | bulk_RNA,sc-RNA | TABULAR_BIO_DATA,REFERENCE_GENOME_FASTA,METADATA_SAMPLE_INFO,SCRNA_OBJECT_RDS | QC_STATS_REPORT |
| `tmb_survival_analysis` | 从MAF文件和临床数据计算病人级肿瘤突变负荷（TMB），按TMB中位数将病人分为高/低组， 进行Kaplan-Meier生存分析和log-rank检验，输出生存曲线、TMB分布图及统计结果表。 适用于肿瘤队列的预后分析场景。 | WES,Clinical | MUTATION_ANNOTATION_FORMAT_MAF,CLINICAL_DATA_EXCEL | QC_STATS_REPORT,TABULAR_BIO_DATA,VISUALIZATION_RESULT |
| `trim_galore` | 基于 Trim Galore 工具的 FASTQ 文件接头修剪与质量控制流程。支持单端和双端测序数据，可指定接头序列，输出修剪后的 FASTQ 文件和修剪报告。 | bulk_RNA | RAW_PAIRED_END_R1_FASTQ,RAW_PAIRED_END_R2_FASTQ | RAW_PAIRED_END_R1_FASTQ,RAW_PAIRED_END_R2_FASTQ,QC_STATS_REPORT |
| `umap` | 基于基因表达矩阵进行 UMAP 降维可视化分析，整合 CNCB 元数据和临床信息。 支持自动样本分组、生存分析数据提取，输出降维结果及样本信息报告。 | Clinical,bulk_RNA | TABULAR_BIO_DATA,METADATA_SAMPLE_INFO,CLINICAL_DATA_EXCEL | TABULAR_BIO_DATA,VISUALIZATION_RESULT,QC_STATS_REPORT |
| `wes_somatic_maf_landscape` | 本流程用于全外显子测序（WES）队列的体细胞突变景观分析。输入标准 MAF 文件，经过滤处理后绘制 Top N 突变基因 Oncoplot 及突变类型分布图。适用于癌症基因组学中的突变谱可视化与总结。 | WES | MUTATION_ANNOTATION_FORMAT_MAF | TABULAR_BIO_DATA,VISUALIZATION_RESULT,MUTATION_ANNOTATION_FORMAT_MAF |
| `wes_somatic_pair` | 用于单个病人配对 tumor-normal WES 数据的体细胞变异分析流程。包含 FASTQ 质控、BWA 比对、Mutect2 变异检测、SnpEff 注释及 MultiQC 汇总报告。 输出包括过滤后的 VCF 文件、BAM 文件及完整的质控报告。 | WGS,WES | DNA_VARIANT_VCF_GENERAL,REFERENCE_GENOME_FASTA,RAW_PAIRED_END_R1_FASTQ,RAW_PAIRED_END_R2_FASTQ | DNA_VARIANT_INDEX_TBI,DNA_GENOMIC_ALIGNMENT_BAM,QC_STATS_REPORT,DNA_VARIANT_VCF_GENERAL |
| `wgcna` | 基于基因表达矩阵和临床表型数据执行 WGCNA 共表达网络分析，包括样本 QC、模块识别、模块 - 性状关联、hub 基因筛选及 bootstrap 稳定性评估。 适用于转录组数据的系统性分析，输出模块划分结果、关键 hub 基因列表及功能富集分析结果。 | bulk_RNA,Clinical | CLINICAL_DATA_EXCEL,TABULAR_BIO_DATA | TABULAR_BIO_DATA,VISUALIZATION_RESULT |
| `wgcna_hub` | 基于 WGCNA 算法构建基因共表达网络，识别与表型性状相关的关键模块和 Hub 基因。 支持从 CNCB 等平台的原始元数据自动解析样本分组和临床信息，输出模块 - 性状关联、候选 Hub 基因列表及功能富集结果。 | Clinical,bulk_RNA | METADATA_SAMPLE_INFO,CLINICAL_DATA_EXCEL,TABULAR_BIO_DATA | TABULAR_BIO_DATA,QC_STATS_REPORT |
| `wgcna_module_trait` | 基于 WGCNA 算法构建基因共表达网络，识别功能模块并分析与临床性状的关联关系。 支持 GO/KEGG/Hallmark 富集分析、生存分析和 hub 基因筛选，适用于批量 RNA-seq 数据的系统生物学研究。 | bulk_RNA,Clinical | CLINICAL_DATA_EXCEL,METADATA_SAMPLE_INFO,TABULAR_BIO_DATA | TABULAR_BIO_DATA,QC_STATS_REPORT |

### 12.2 Study snapshot (20; sample_count property is null for 6 studies — sample-node counts fill the gap)

| study_accession | tumor_type | sample_count(prop) | sample nodes |
|---|---|---|---|
| HRA000001 | Natural | 557 | 557 |
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
| HRA007167 | melanoma | 168 | 81 |
| HRA007169 | melanoma | 81 | 168 |
| HRA007413 | acute myeloid leukemia | null | 373 |
| HRA016026 | lung cancer | null | 700 |
