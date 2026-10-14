#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行绑定表：哪个流程、在哪个队列上、用哪一整套输入参数，确实跑通过。

为什么要有它。2026-09-28 的运行测试（17 条「我想用 X 数据进行 Y 分析」）里，选对流程
且跑通的只有 6 条：6 条明明点了流程名还被模型换成别的流程，5 条流程对了、但每次现场
从图谱拼出来的输入是错的（WES 没给 BED、参考字典对不上、缺 maf/count_tsv）。
黑白名单只管到「流程 × 队列」，具体用哪些文件每次都现算，所以同一个问题两次问
可能拿到两套参数。

这里把「能跑」定死成数据：每条绑定 = 流程 × 队列 × Cromwell 上一次成功运行时平台
真正提交的那份参数（scripts/harvest_bindings.py 生成，scripts/verify_bindings.py 复跑）。
执行合同里的 execution_params 原样取自绑定，不再查图拼路径；没有绑定的流程直接回
「暂无已验证可运行的数据」，不现场凑。

本模块只读文件、不连 Neo4j、不调模型——点名了流程的请求全程确定性。
"""
from __future__ import annotations

import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = "run-bindings/v1"
BINDINGS_DIR = os.environ.get("BIO_BINDINGS_DIR", os.path.join(HERE, "bindings"))
_SKILL_REF = os.environ.get("BIO_SKILL_REF", os.path.join(HERE, "skill", "references"))

# Cromwell 上的 workflowName → 图谱 tool_id。同一流程有过几种 WDL 打包名
# （`task303_gene_boxplot` 是 8 月的任务包名，`gene_boxplot` 是 9 月起平台用的名字），
# 都算这个流程的运行记录。新流程接入时手动加一行——不按名字猜。
WORKFLOW_TOOLS = {
    "BootstrapStabilityWorkflow": "bootstrap_stability",
    "BwaMain": "bwa",
    "BwaMemWorkflow": "bwa",
    "CnvkitCnvClinical": "cnvkit_cnv_clinical",
    "ConvertPairedFastQsToUnmappedBamWf": "paired_fastq_to_unmapped_bam",
    "DiffExprGoWorkflow": "diff_expr_go",
    "DiffExprKeggWorkflow": "diff_expr_kegg",
    "FastQcWorkflow": "fastqc",
    "FastpPairedEndWorkflow": "fastp",
    "GatkGermlineCohort": "gatk_germline_cohort",
    "GatkWesSomaticWorkflow": "gatk",
    "GseaPathwayEnrichment": "gsea_pathway_enrichment",
    "HRA000074_WGCNA": "wgcna",
    "HvgPcaGmmWorkflow": "hvg_pca_gmm",
    "ImmuneInfiltrationIOBRCIBERSORT": "immune_infiltration_iobr",
    "MantaStructuralVariants": "manta_structural_variants",
    "PreprocessCountsWorkflow": "preprocess_counts",
    "RsemQuantificationWorkflow": "rsem",
    "RunRmatsPipeline": "rmats_alternative_splicing",
    "ScrnaCellCommunication": "scrna_cell_communication",
    "StarRnaSeqWorkflow": "star",
    "SurvivalAnalysis": "survival_analysis",
    "TMBSurvivalAnalysis": "tmb_survival_analysis",
    "TrimGaloreWorkflow": "trim_galore",
    "cox_model": "cox_model",
    "de_enrichment": "de_enrichment",
    "deg_enrichment": "deg_enrichment",
    "deg_trend": "deg_trend",
    "driver_gene_gender_analysis": "driver_gene_gender_analysis",
    "gene_boxplot": "gene_boxplot",
    "her2_pfs_survival": "her2_pfs_survival",
    "km_survival": "km_survival",
    "rnaseq_unsupervised_cluster": "rnaseq_unsupervised_cluster",
    "stage_heatmap": "stage_heatmap",
    "task283_de_enrichment": "de_enrichment",
    "task294_wgcna_module_trait": "wgcna_module_trait",
    "task303_gene_boxplot": "gene_boxplot",
    "task305_stage_heatmap": "stage_heatmap",
    "task310_cox_model": "cox_model",
    "task313_wgcna_hub": "wgcna_hub",
    "task383_umap": "umap",
    "task393_breast_cellchat": "breast_cellchat",
    "task395_lung_tme_annotation_cnv": "lung_tme_annotation_cnv",
    "task398_immunotherapy_cellchat": "immunotherapy_cellchat",
    "task403_dataset_matrix_annotation": "dataset_matrix_annotation",
    "task404_dataset_downstream": "dataset_downstream",
    "task419_deg_enrichment": "deg_enrichment",
    "task423_deg_trend": "deg_trend",
    "task438_km_survival": "km_survival",
    "task457_celltype_case_control_de": "celltype_case_control_de",
    "task528_tcell_intervention": "tcell_intervention",
    "task547_ipf_trajectory_regulon": "ipf_trajectory_regulon",
    "umap": "umap",
    "wes_somatic_maf_landscape": "wes_somatic_maf_landscape",
    "wgcna_hub": "wgcna_hub",
    "wgcna_module_trait": "wgcna_module_trait",
}

# 平台界面上的流程展示名（2026-09 运行测试表），用来认出「用户点名的是哪个流程」。
# 卡片自带的中文名（name_cn）、图谱 id、卡片 id、WDL 名会自动并进别名表，这里只补
# 它们覆盖不到的写法。
DISPLAY_ALIASES = {
    "FASTQ 转 uBAM": "paired_fastq_to_unmapped_bam",
    "WES 配对体细胞变异": "wes_somatic_pair",
    "GATK 胚系队列分析": "gatk_germline_cohort",
    "CNVkit 拷贝数变异": "cnvkit_cnv_clinical",
    "Manta 结构变异": "manta_structural_variants",
    "MAF 突变景观": "wes_somatic_maf_landscape",
    "驱动基因性别差异": "driver_gene_gender_analysis",
    "肿瘤克隆演化": "tumor_evolution_inference",
    "RNA-seq 全流程": "rnaseq_singletask",
    "Count 矩阵预处理": "preprocess_counts",
    "无监督聚类": "rnaseq_unsupervised_cluster",
    "高变基因/PCA/GMM": "hvg_pca_gmm",
    "rMATS 可变剪接": "rmats_alternative_splicing",
    "差异表达与 GO": "diff_expr_go",
    "差异表达与 Reactome": "diff_expr_kegg",
    "STAR-Fusion": "star_fusion",
}

# 用户口头常用的软件名（「用 manta 做结构变异」「跑一下 CNVkit」）。只收专有软件名，
# 不收 Cox / KM / TMB 这类多个流程共用的方法名——那种问法交给模型判断。
SOFTWARE_ALIASES = {
    "manta": "manta_structural_variants",
    "cnvkit": "cnvkit_cnv_clinical",
    "gsea": "gsea_pathway_enrichment",
    "rmats": "rmats_alternative_splicing",
    "iobr": "immune_infiltration_iobr",
    "cibersort": "immune_infiltration_iobr",
    "cellranger": "cellranger_workflow",
    "starfusion": "star_fusion",
}

# 平台上有、本系统没有对应流程（没有知识卡片）的名字：点名时如实说不支持，
# 不许落到「意思相近」的别的流程上（09-28 测试里 WGS 单样本就被换成了 Manta）。
PLATFORM_ONLY = {
    "wgs_single_sample": "WGS 单样本",
}

# 绑定参数里允许用户改的字面量：{流程: {参数: 取值来源}}。其余参数一律按绑定原样下发。
# 取值来源 "gene" = 问句里点名的基因（取不到就用绑定原值）。
USER_PARAMS = {
    "her2_pfs_survival": {"gene": "gene"},
}

# 资源类参数：平台按自己的流程配置填（镜像版本、CPU、内存、磁盘），不进绑定的
# execution_params。其余参数——包括卡片里没有的分析参数——一律按跑通时的原值绑定。
_PLATFORM_INPUT = re.compile(
    r"^(docker|docker_image|\w+_docker(_image)?|cpus?|cpu_count|n?threads|num_threads"
    r"|mem(ory)?(_gb|_mb)?|\w+_mem(ory)?(_gb|_mb)?|disks?|disk_gb|disk_size(_gb)?|\w+_disk(_gb|_size(_gb)?)?"
    r"|boot_disk_size_gb|preemptible(_tries)?|max_retries|runtime_\w+)$", re.I)

# 两组对照参数：同一个样本出现在两组里，运行照样 Succeeded，但比较没有意义
GROUP_PAIRS = (("group_a_samples", "group_b_samples"), ("group1_bams", "group2_bams"),
               ("tumor_bams", "normal_bams"))

_DATA_ROOTS = ("/hpcdisk1/cbb_group/data/", "/cbb-data/")
# 宿主机共享存储：这些路径下的才是「文件资产」。/opt/... 这类是容器内路径（脚本、内置参考），
# 原样当参数值下发，不能当成要用户提供的数据文件列进 assets。
_HOST_ROOTS = ("/hpcdisk1/", "/cbb-data/", "/cromwell-share/", "/mnt/")
_REFERENCE_DIRS = ("/hpcdisk1/cbb_group/data/reference/",)
_HRA = re.compile(r"HRA\d{6}", re.I)
_RUN = re.compile(r"(?<![A-Za-z0-9])(HRR\d+)")
_IDENT = re.compile(r"(?<![a-z0-9_])[a-z][a-z0-9]*(?:_[a-z][a-z0-9]*)+(?![a-z0-9_])")

_CARDS: dict = {}          # graph tool_id -> card（knowledge_cards_map.json 原样 + card_id）
_REGISTRY: dict = {}       # graph tool_id -> bindings/<tool_id>.json
_STUDIES: dict = {}        # study -> {"tumor_type", "labels"}
_ALIASES: list = []        # [(编译后的匹配式, tool_id 或 ("platform_only", 展示名))]，长的在前
_DISEASE_TERMS: list = []  # [(编译后的匹配式, {study})]，长的在前


# ------------------------------------------------------------------ 加载 ----

def _load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _norm(s):
    return re.sub(r"\s+", "", str(s or "")).lower()


def _pattern(term):
    """别名 → 匹配式。字符之间允许任意空白（「CNVkit 拷贝数变异」「CNVkit拷贝数变异」都认），
    - 与 _ 互通；以英文字母/数字开头或结尾时加词边界，免得 star 命中 start、gsea 命中 gsea_xxx。
    在原句上匹配而不是先删空白：删了空白英文会粘成一串（use STAR for → usestarfor）。"""
    chars = [c for c in str(term) if not c.isspace()]
    parts = [r"[-_\s]?" if c in "-_" else re.escape(c) for c in chars]
    body = r"\s*".join(parts)
    if chars and re.match(r"[A-Za-z0-9]", chars[0]):
        body = r"(?<![A-Za-z0-9_])" + body
    if chars and re.match(r"[A-Za-z0-9]", chars[-1]):
        body += r"(?![A-Za-z0-9_])"
    return re.compile(body, re.I)


def load(bindings_dir=None):
    """(重新)加载卡片、绑定表、队列表与别名表。模块导入时自动调一次。"""
    global BINDINGS_DIR
    if bindings_dir:
        BINDINGS_DIR = bindings_dir
    _CARDS.clear()
    for card_id, c in _load_json(os.path.join(_SKILL_REF, "knowledge_cards_map.json"), {}).items():
        gid = c.get("graph_tool_id") or card_id
        _CARDS[gid] = dict(c, meta_id=card_id)

    _REGISTRY.clear()
    if os.path.isdir(BINDINGS_DIR):
        for fn in sorted(os.listdir(BINDINGS_DIR)):
            if not fn.endswith(".json") or fn in ("index.json", "studies.json"):
                continue
            doc = _load_json(os.path.join(BINDINGS_DIR, fn), None)
            if isinstance(doc, dict) and doc.get("tool_id") and doc.get("bindings"):
                _REGISTRY[doc["tool_id"]] = doc

    _STUDIES.clear()
    _STUDIES.update(_load_json(os.path.join(BINDINGS_DIR, "studies.json"), {}).get("studies") or {})

    names = {}
    for gid, c in _CARDS.items():
        for a in (gid, c["meta_id"], c.get("name_cn")):
            if a:
                names.setdefault(_norm(a), gid)
    for wf, gid in WORKFLOW_TOOLS.items():
        names.setdefault(_norm(wf), gid)
    for a, gid in list(DISPLAY_ALIASES.items()) + list(SOFTWARE_ALIASES.items()):
        names[_norm(a)] = gid
    for a, label in PLATFORM_ONLY.items():
        names[_norm(a)] = ("platform_only", label)
        names[_norm(label)] = ("platform_only", label)
    _ALIASES[:] = [(_pattern(a), g) for a, g in sorted(names.items(), key=lambda x: -len(x[0]))]

    terms = {}
    for study, info in _STUDIES.items():
        for t in [info.get("tumor_type")] + list(info.get("labels") or []):
            if t:
                terms.setdefault(_norm(t), set()).add(study)
    _DISEASE_TERMS[:] = [(_pattern(t), ss) for t, ss in sorted(terms.items(), key=lambda x: -len(x[0]))]


def card(gid):
    return _CARDS.get(gid) or {}


def name_cn(gid):
    return card(gid).get("name_cn") or gid


def all_tool_ids():
    return sorted(_CARDS)


def tool_id_of(pid):
    """模型/调用方写的流程标识（图谱 id、卡片 id、taskNNN_ 前缀、WDL 名）→ 图谱 tool_id。"""
    pid = str(pid or "").strip()
    if pid in _CARDS:
        return pid
    for gid, c in _CARDS.items():
        if c["meta_id"] == pid:
            return gid
    base = re.sub(r"^task\d+_", "", pid)
    if base in _CARDS:
        return base
    return WORKFLOW_TOOLS.get(pid)


def studies():
    return dict(_STUDIES)


def registry():
    return dict(_REGISTRY)


def is_data_path(s):
    """真实队列数据：在数据根目录下、带 HRA 队列号、且不是参考基因组目录。"""
    s = str(s or "")
    return (s.startswith(_DATA_ROOTS) and not s.startswith(_REFERENCE_DIRS)
            and bool(_HRA.search(s)))


def is_platform_input(name):
    return bool(_PLATFORM_INPUT.match(str(name or "")))


def is_reference_resource(gid, name):
    import mcp_light_server as srv           # 惰性：srv 在导入时也会用到本模块
    return srv._is_reference_resource(srv.KC_MAP.get(gid), name)


# ------------------------------------------------------------- 绑定查询 ----

def bindings_for(gid, include_disabled=False):
    doc = _REGISTRY.get(gid) or {}
    return [b for b in doc.get("bindings") or [] if include_disabled or not b.get("disabled")]


def bound_tools():
    """至少有一条可用绑定的流程。"""
    return sorted(g for g in _REGISTRY if bindings_for(g))


def get(gid, study):
    for b in bindings_for(gid, include_disabled=True):
        if b["study"] == str(study or "").upper():
            return b
    return None


def block_reason(gid, study):
    """「流程 × 队列」能不能用。None = 能用；否则一句可直接展示的原因。"""
    if not gid:
        return None
    who = f"{name_cn(gid)}（{gid}）"
    usable = bindings_for(gid)
    if not usable:
        dis = bindings_for(gid, include_disabled=True)
        if dis:
            return f"{who}的已绑定队列当前全部停用（{dis[0]['disabled'].get('reason', '')}）"
        return f"{who}暂无已验证可运行的数据（Cromwell 上没有该流程用真实队列数据跑通的记录）"
    if not study:
        return None
    b = get(gid, study)
    if b is None:
        return (f"{who}在 {study} 上没有已验证可运行的数据；"
                f"已绑定队列：{'/'.join(x['study'] for x in usable)}")
    if b.get("disabled"):
        return f"{who} × {study} 已停用：{b['disabled'].get('reason', '')}"
    return None


# ------------------------------------------------------------- 问句解析 ----

def _spans(text, items):
    """在原句里找全部命中，丢掉被更长命中整个盖住的短命中。items = [(匹配式, 值)]。"""
    hits = [(m.start(), m.end(), val) for pat, val in items for m in pat.finditer(text)
            if m.end() > m.start()]
    keep = [h for h in hits
            if not any(o[0] <= h[0] and h[1] <= o[1] and (o[1] - o[0]) > (h[1] - h[0]) for o in hits)]
    return sorted(keep)


def resolve_tool(query):
    """问句里点名的流程。

    返回 (kind, value)：
      ("tool", gid)         点名了一个本系统有的流程
      ("platform_only", 名) 点名的是平台有、本系统没有的流程
      ("unknown", 标识符)   点了一个形如 snake_case 的流程 id，但谁都不认识
      (None, None)          没点名，交给模型判断意图
    多个流程同时被点名时取最先出现的那个。
    """
    text = str(query or "")
    hits = _spans(text, _ALIASES)
    if hits:
        val = hits[0][2]
        if isinstance(val, tuple):
            return val
        return "tool", val
    params = {i["name"] for c in _CARDS.values() for i in c.get("inputs") or []}
    for m in _IDENT.finditer(text.lower()):
        tok = m.group(0)
        if tok not in params and not tok.startswith(("hra", "hrr")):
            return "unknown", tok
    return None, None


def _strip_tool_names(gid, query):
    """把问句里该流程自己的名字抹掉。流程中文名常带癌种（「肺癌肿瘤微环境注释与CNV分析」），
    不抹掉就会被当成用户指定了肺癌数据，而这个流程绑定的其实是鼻咽癌队列。"""
    text = str(query or "")
    for i, j, _ in _spans(text, [(p, g) for p, g in _ALIASES if g == gid]):
        text = text[:i] + " " * (j - i) + text[j:]
    return text


def mentioned_studies(query):
    return [s.upper() for s in dict.fromkeys(_HRA.findall(str(query or "")))]


def disease_studies(query):
    """问句里的癌种 → 队列集合，以及命中的词（用于说明）。没提癌种返回 (set(), [])。"""
    text = str(query or "")
    hits = _spans(text, _DISEASE_TERMS)
    found, words = set(), []
    for i, j, ss in hits:
        found |= ss
        words.append(text[i:j])
    return found, list(dict.fromkeys(words))


def _label(study):
    t = (_STUDIES.get(study) or {}).get("label_cn") or (_STUDIES.get(study) or {}).get("tumor_type")
    return f"{study}（{t}）" if t else study


def select(gid, query, hint_studies=()):
    """给定流程，按问句挑一条绑定。

    优先级：问句点名的队列 > 问句里的癌种 > 模型给的队列 > 默认队列。
    点名的队列 / 癌种没有绑定时**不换成别的**——返回 (None, 原因)，让用户知道有哪些可选。
    返回 (主绑定, 备选绑定列表, 说明)。
    """
    usable = bindings_for(gid)
    if not usable:
        return None, [], block_reason(gid, None)
    doc = _REGISTRY[gid]
    by_study = {b["study"]: b for b in usable}
    ranked = sorted(usable, key=lambda b: (b["study"] != doc.get("default_study"),
                                           -(b.get("evidence") or {}).get("succeeded_runs", 0),
                                           b["study"]))
    avail = "、".join(_label(b["study"]) for b in ranked)

    explicit = mentioned_studies(query)
    if explicit:
        hit = [by_study[s] for s in explicit if s in by_study]
        if hit:
            return hit[0], [b for b in hit[1:]], f"使用问句指定的队列 {_label(hit[0]['study'])}"
        return None, [], (f"{name_cn(gid)}（{gid}）在 {'、'.join(explicit)} 上没有已验证可运行的数据；"
                          f"可用队列：{avail}")

    want, words = disease_studies(_strip_tool_names(gid, query))
    if want:
        hit = [b for b in ranked if b["study"] in want]
        if hit:
            return hit[0], hit[1:], f"问句提到「{'、'.join(words)}」，使用 {_label(hit[0]['study'])}"
        return None, [], (f"{name_cn(gid)}（{gid}）在「{'、'.join(words)}」数据上没有已验证可运行的数据；"
                          f"可用队列：{avail}")

    for s in hint_studies or ():
        s = str(s or "").upper()
        if s in by_study:
            return by_study[s], [b for b in ranked if b["study"] != s], f"使用模型选择的队列 {_label(s)}"
    return ranked[0], ranked[1:], f"未指定数据，使用默认队列 {_label(ranked[0]['study'])}"


# ------------------------------------------------------------- 执行合同 ----

def _artifact(path):
    import cohort_adapter
    return cohort_adapter._asset_artifact({"file_name": os.path.basename(path)})


def _apply_user_params(gid, params, query):
    """USER_PARAMS 里登记的字面量按问句覆盖，返回 (参数, 改动说明列表)。"""
    out, notes = dict(params), []
    for name, src in (USER_PARAMS.get(gid) or {}).items():
        if src == "gene":
            import cohort_adapter
            gene = cohort_adapter._query_gene(query)
            if gene and name in out and out[name] != gene:
                notes.append(f"{name}: {out[name]} → {gene}（问句指定）")
                out[name] = gene
    return out, notes


def _binding_payload(gid, b, query, rank, source):
    """一条绑定 → 一个候选对象（assets、tool_chain 步骤、execution_params、绑定出处）。"""
    meta_id = card(gid).get("meta_id") or gid
    params, overrides = _apply_user_params(gid, b["execution_params"], query)
    assets, by_path = [], {}

    def _asset(p):
        if p not in by_path:
            run = _RUN.search(os.path.basename(p))
            a = {"asset_id": f"asset-{rank}-{len(assets) + 1}",
                 "file_name": os.path.basename(p), "path": p, "file_path": p,
                 "artifact_type": _artifact(p), "semantic_format": None,
                 "study_accession": b["study"], "sample_accession": None,
                 "run_accession": run.group(1) if run else None,
                 "match_reason": f"运行绑定：Cromwell {b['source_run']['cromwell_id']} 跑通时的输入"}
            by_path[p] = a
            assets.append(a)
        return {"asset_id": by_path[p]["asset_id"]}

    inputs = {}
    def _host(x):
        return isinstance(x, str) and x.startswith(_HOST_ROOTS)

    for k, v in params.items():
        if _host(v):
            inputs[k] = _asset(v)
        elif isinstance(v, list) and v and all(_host(x) for x in v):
            inputs[k] = [_asset(x) for x in v]
        else:
            inputs[k] = {"value": v}
    cand = {
        "rank": rank,
        "match_id": f"candidate-{rank}",
        "pipeline_id": gid,
        "validation_ok": True,
        "feasibility_status": "ready",
        "study_accession": b["study"],
        "assets": assets,
        "tool_chain": [{"step_id": "step-1", "tool_id": meta_id, "inputs": inputs}],
        "execution_params": params,
        "execution_params_missing": [],
        "selection_source": source,
        "binding": {
            "study": b["study"],
            "tumor_type": b.get("tumor_type"),
            "cromwell_id": b["source_run"]["cromwell_id"],
            "workflow_name": b["source_run"]["workflow_name"],
            "verification": (b.get("verification") or {}).get("status") or "not_rerun",
            "overrides": overrides,
        },
    }
    return cand


def contract(gid, primary, alternatives, query, note, resolved_by, top_k=3, t0=None):
    """一条主绑定（+ 同流程其他队列的备选）→ tool-chain/v2 执行合同。

    形状与旧版 to_cohort_v2 对齐（平台 Dingent / PipelineBuilder 按它读）：
    tool.catalog_status 与 tool.inputs[].builder_param 决定平台收不收这条推荐，
    builder_param 必须与 execution_params 的键逐字一致——两者都取卡片参数名。
    """
    import mcp_light_server as srv
    c = card(gid)
    meta_id = c.get("meta_id") or gid
    slots = srv._card_slots(srv.KC_MAP.get(gid))[0] if srv.KC_MAP.get(gid) else []
    tool = {"tool_id": meta_id, "tool_name": name_cn(gid),
            "catalog_status": "registered" if srv.CATALOG.get(gid) else "unregistered",
            "inputs": [dict(s, builder_param=s.get("name")) for s in slots]}
    cap = max(1, int(top_k or 1))
    cands = [_binding_payload(gid, primary, query, 1, "run_binding")]
    for i, b in enumerate(alternatives[:cap - 1], 2):
        cands.append(_binding_payload(gid, b, query, i, "alternative_binding"))
    top = cands[0]
    rec = {
        "pipeline_id": gid,
        "match_note": note,
        "tool": tool,
        "data": {"status": "available", "assets": top["assets"],
                 "study_accessions": [primary["study"]], "source": "run_binding"},
        "execution_params": top["execution_params"],
        "execution_params_missing": [],
        "binding": top["binding"],
    }
    answer = (f"将使用 {name_cn(gid)}（{gid}）在 {_label(primary['study'])} 上的已验证参数运行"
              f"（Cromwell {primary['source_run']['cromwell_id']} 跑通的同一套输入）。{note}。")
    out = {
        "schema_version": "tool-chain/v2",
        "selection_status": "ready",
        "intent": {"query_text": query, "tool_id": gid, "resolved_by": resolved_by},
        "answer": answer,
        "recommendations": [rec],
        "candidates": cands,
        "candidate_count": len(cands),
        "recommendation_count": 1,
        "data_matcher_mode": "run_binding",
        "planner_metadata": {"used": resolved_by == "model", "arch": "light",
                             "planning_owner": "run_binding" if resolved_by != "model" else "caller_model",
                             "bindings_schema": SCHEMA_VERSION},
    }
    if t0 is not None:
        out["mcp_timing_ms"] = round((time.time() - t0) * 1000, 1)
    return out


def unsupported(query, reason, gid=None, t0=None, **extra):
    """合法的「做不了」回包：说清楚原因，附上这个流程有哪些队列可选。"""
    out = {"schema_version": "tool-chain/v2",
           "selection_status": "unsupported",
           "candidates": [], "recommendations": [],
           "candidate_count": 0, "recommendation_count": 0,
           "answer": reason, "unsupported_reason": reason,
           "intent": {"query_text": query, "tool_id": gid},
           "data_matcher_mode": "run_binding",
           "planner_metadata": {"used": False, "arch": "light", "planning_owner": "run_binding",
                                "bindings_schema": SCHEMA_VERSION}}
    if gid:
        out["bound_studies"] = [b["study"] for b in bindings_for(gid)]
    out.update(extra)
    if t0 is not None:
        out["mcp_timing_ms"] = round((time.time() - t0) * 1000, 1)
    return out


def route_tool(gid, query, resolved_by, hint_studies=(), top_k=3, t0=None):
    """已知流程 → 合同或「做不了」。"""
    if gid not in _CARDS:
        return unsupported(query, f"本系统没有流程 {gid}", t0=t0)
    primary, alts, note = select(gid, query, hint_studies)
    if primary is None:
        return unsupported(query, note, gid=gid, t0=t0)
    return contract(gid, primary, alts, query, note, resolved_by, top_k, t0)


def prompt_section():
    """给模型的约束：只能从已绑定的流程与队列里选。替代旧的 default_studies 提示。"""
    rows = []
    for gid in bound_tools():
        ss = "、".join(_label(b["study"]) for b in bindings_for(gid))
        rows.append(f"- {gid}（{name_cn(gid)}）：{ss}")
    return ("\n\n## 运行绑定（硬约束）\n"
            "只有下列流程有已验证可运行的数据，推荐只能从这里选；数据只能是该流程后面列出的队列。"
            "执行参数由服务端按绑定原样下发，你不需要、也不要自己拼文件路径。"
            "用户要的分析不在下表时，回 rejected（reason=off_topic 或说明暂无已验证数据），"
            "不要换成意思相近的另一个流程。\n" + "\n".join(rows))


load()
