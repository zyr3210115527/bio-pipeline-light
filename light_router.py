#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
light_router.py — 轻架构 route_pipeline_request（tool-chain/v2 对齐重 MCP）

给 mcp_light_server.py 的 route_pipeline_request 工具提供：
  - intent 提取（规则）：analysis_goal / disease / omics_type / input_hint / quant_hint / requested_outputs
  - 数据匹配器（轻）：队列候选 + 文件候选（重版 assets 字段形状）+ matched/expected/missing
  - 候选链装配：图内 next_tool 可达的 atomic 链（闭集校验），输出 candidates[]
  - slot 增强：io_slot.csv 的 artifact / dimension / variant / variant_alias_for
"""
from __future__ import annotations
import csv
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_REF = os.environ.get("BIO_SKILL_REF", os.path.join(HERE, "skill", "references"))

# ---------- slot 模型（io_slot.csv：artifact/dimension/variant） ----------
SLOTS: dict = {}   # (tool_id, slot_name) -> {artifact, dimension, dimension_value, variant, variant_alias_for, required, wdl_type}
def load_slots() -> None:
    global SLOTS
    path = os.path.join(SKILL_REF, "io_slot.csv")
    if not os.path.exists(path):
        return
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tid = (row.get("tool_id") or "").replace("tool_id:", "")
            sname = row.get("slot_name") or ""
            if not tid or not sname:
                continue
            SLOTS[(tid, sname)] = {
                "artifact": row.get("artifact") or "",
                "dimension": row.get("dimension") or "",
                "dimension_value": row.get("dimension_value") or "",
                "variant": row.get("variant") or "",
                "variant_alias_for": row.get("variant_alias_for") or "",
                "required": (row.get("required") or "").strip().lower() == "true",
                "wdl_type": row.get("wdl_type") or "",
                "direction": (row.get("direction") or "").strip(),
            }
load_slots()

# ---------- 意图提取（规则） ----------
DISEASE_MAP = [("肝癌", "liver"), ("肝细胞癌", "liver"), ("胶质瘤", "glioma"), ("黑色素瘤", "melanoma"),
               ("食管癌", "esoph"), ("肺癌", "lung"), ("乳腺癌", "breast"), ("肠癌", "colorect"),
               ("结直肠癌", "colorect"), ("卵巢癌", "ovarian"), ("胰腺癌", "pancreas")]
OMICS_HINTS = [("单细胞", "sc-RNA"), ("scrna", "sc-RNA"), ("rna-seq", "bulk RNA-seq"), ("转录", "bulk RNA-seq"),
               ("wes", "WES"), ("wgs", "WGS"), ("外显子", "WES"), ("全基因组", "WGS")]
FMT_HINTS = [("fq.gz", "fq.gz"), ("fastq", "fq.gz"), ("bam", "bam"), ("vcf", "vcf"), ("maf", "maf"),
             ("tsv", "tsv"), ("tpm", "tpm"), ("fpkm", "fpkm"), ("counts", "counts"), ("表达矩阵", "expression")]
QUANT_HINTS = [("counts", "counts"), ("tpm", "tpm"), ("fpkm", "fpkm")]
OUTPUT_HINTS = [("vcf", "vcf"), ("maf", "maf"), ("oncoplot", "oncoplot"), ("生存曲线", "survival_curve"),
                ("免疫细胞比例", "immune_fraction"), ("差异基因", "deg_list"), ("富集", "enrichment"),
                ("表达矩阵", "expression_matrix"), ("计数", "count_matrix"), ("聚类", "clusters")]

def intent_extract(query: str, top_pipeline: str = "", pipeline_name: str = "") -> dict:
    q = query.lower()
    disease = next((d for d, _ in DISEASE_MAP if d in query), None)
    omics = next((o for k, o in OMICS_HINTS if k in q), None)
    input_hint = next((f for k, f in FMT_HINTS if k in q), None)
    quant = next((q_ for k, q_ in QUANT_HINTS if k in q), None)
    outputs = [o for k, o in OUTPUT_HINTS if k in q]
    studs = re.findall(r"HRA\d{6}", query)
    goal = pipeline_name or top_pipeline
    return {"query_text": query,
            "analysis_goal": goal if goal else None,
            "disease": disease,
            "disease_term": disease,
            "disease_source": "rule" if disease else None,
            "disease_status": None,
            "disease_studies": [],
            "omics_type": omics,
            "input_hint": input_hint,
            "quant_hint": quant,
            "requested_outputs": outputs,
            "study_accessions": studs,
            "source": "rule",
            "ambiguous": not (disease or omics or input_hint or outputs)}

# ---------- 数据匹配器（轻） ----------
def neo4j_q(statements):
    import subprocess, tempfile
    user = os.environ.get("NEO4J_USER", "neo4j")
    pw = os.environ.get("NEO4J_PASSWORD", "")
    if not pw:
        raise RuntimeError("set NEO4J_PASSWORD")
    url = os.environ.get("NEO4J_URL", "http://127.0.0.1:7474/db/neo4j/tx/commit")
    payload = json.dumps({"statements": [{"statement": s} for s in statements]})
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(payload); tmp = f.name
    try:
        r = subprocess.run(["curl","-s","--max-time","20","-u",f"{user}:{pw}","-X","POST",
            "-H","Content-Type: application/json","-d","@"+tmp,url], capture_output=True, text=True)
        d = json.loads(r.stdout)
        if d.get("errors"):
            raise RuntimeError("; ".join(e.get("message","") for e in d["errors"])[:300])
        return [[row["row"] for row in res.get("data", [])] for res in d.get("results", [])]
    finally:
        os.unlink(tmp)

def _slot_file_pattern(slot_name: str, description: str) -> str:
    """按槽位语义推断图内文件名的匹配模式。"""
    n = (slot_name or "").lower()
    d = (description or "").lower()
    if "tpm" in n:
        return "GenesTPM"
    if "expression" in n or "矩阵" in d:
        return "Genes"
    if "clinical" in n or "临床" in d:
        return "Clinical"
    if "metainfo" in n or "meta" in n or "映射" in d or "mapping" in d:
        return "MetaInfo"
    if "vcf" in n or "variant" in n:
        return "vcf"
    if "maf" in n or "mut" in n:
        return "maf"
    if "count" in n:
        return "counts"
    return ""

def _cohort_keywords(disease: str) -> list:
    kw = dict(DISEASE_MAP).get(disease)
    if not kw:
        return []
    return {"liver": ["liver", "hepatocell", "hcc"],
            "glioma": ["glioma", "gliom"],
            "melanoma": ["melanoma"],
            "esoph": ["esoph"],
            "lung": ["lung"],
            "breast": ["breast", "mammary"],
            "colorect": ["colorect", "colon", "rectal"],
            "ovarian": ["ovarian"],
            "pancreas": ["pancreas"]}.get(kw, [kw])

def match_data(pipeline_id: str, input_formats: list, intent: dict, limit: int = 5) -> dict:
    """槽位驱动的数据匹配器：按 io_slot 槽位语义找齐资产（对齐重版 matched/expected/missing）。"""
    disease = intent.get("disease")
    # ── 队列解析：多关键词 + 按样本数排序（命中 HRA001272 这类大队列） ──
    kws = _cohort_keywords(disease) if disease else []
    cohort = []
    if kws:
        conds = " OR ".join(f"toLower(s.tumor_type) CONTAINS '{k}' OR toLower(s.title) CONTAINS '{k}'" for k in kws)
        rows = neo4j_q([f"MATCH (s:study) WHERE {conds} RETURN s.study_accession, s.title, s.sample_count, s.tumor_type ORDER BY s.sample_count DESC LIMIT {limit}"])
        if rows:
            cohort = [{"study_accession": r[0], "title": r[1], "sample_count": r[2], "tumor_type": r[3],
                       "match_reason": f"癌种匹配: {disease} ({kws[0]})"} for r in rows[0]]
    studs = [c["study_accession"] for c in cohort] or intent.get("study_accessions") or []
    primary_study = studs[0] if studs else None  # 参考资产优先锁定队列（重版行为）
    # ── 槽位驱动匹配（io_slot.csv 有 pipeline 槽位） ──
    slots = [(k[1], v) for k, v in SLOTS.items() if k[0] == pipeline_id and v.get("required")]
    assets, matched, refs = [], 0, []
    used_slots = []
    for sname, slot in slots:
        pat = _slot_file_pattern(sname, "")
        if not pat:
            continue
        used_slots.append(sname)
        if pat == "GenesTPM":
            pat_sql = "(n.file_name CONTAINS 'Genes' AND n.file_name CONTAINS 'TPM')"
            stmt = f"MATCH (n:T2) WHERE {pat_sql}"
        else:
            stmt = f"MATCH (n:T2) WHERE n.file_name CONTAINS '{pat}'"
        if primary_study:
            stmt += f" AND n.study_accession = '{primary_study}'"
        elif studs:
            stmt += f" AND n.study_accession IN {json.dumps(studs)}"
        _TAIL = """ OPTIONAL MATCH (n)-[:in_sample]->(sp:sample)
 RETURN n.file_name, n.format, n.strategy, n.data_level, n.study_accession,
        sp.sample_accession, sp.tissue_type, sp.specimen_type, n.file_path LIMIT 2"""
        stmt_fallback = None
        if studs and primary_study:
            stmt_fallback = f"MATCH (n:T2) WHERE n.file_name CONTAINS '{pat}' AND n.study_accession IN {json.dumps(studs)}" + _TAIL
        rows = neo4j_q([stmt + _TAIL])
        if not (rows and rows[0]) and stmt_fallback:
            rows = neo4j_q([stmt_fallback])   # 主队列无此槽位文件 → 回退到候选队列
        if rows and rows[0]:
            matched += 1
            r = rows[0][0]
            assets.append({
                "name": r[0], "graph_status": "available", "source": "T2",
                "t2_id": f"{r[0]}::{r[8]}" if r[8] else None,
                "file_id": r[0], "file_name": r[0], "files": r[0], "format": r[1],
                "strategy": r[2] or "", "data_level": r[3] or "", "study_accession": r[4],
                "sample_id": r[5], "run_accession": None, "individual_accession": None,
                "individual_name": None, "sample_name": None,
                "specimen_type": r[7], "specimen_types": r[7], "tissue_type": r[6],
                "sample_role": None, "sample_role_label": None,
                "read_pair": None, "file_path": r[8],
                "input_role": sname,
                "match_reason": f"槽位 {sname} 文件名模式 {pat}"})
            refs.append(r[0])
    # 兜底：无槽位时按 input_formats 匹配
    if not used_slots:
        for fmt in input_formats:
            f = fmt.strip().lower()
            if not f or f == "null":
                continue
            stmt = f"MATCH (n:T2)-[:in_format]->(f:format) WHERE toLower(f.format) CONTAINS '{f}'"
            if studs:
                stmt += f" AND n.study_accession IN {json.dumps(studs)}"
            stmt += " RETURN n.file_name, n.format, n.file_path LIMIT 2"
            rows = neo4j_q([stmt])
            if rows and rows[0]:
                matched += 1
                for r in rows[0]:
                    assets.append({"name": r[0], "graph_status": "available", "source": "T2",
                                   "t2_id": None, "file_id": r[0], "file_name": r[0], "files": r[0],
                                   "format": r[1], "strategy": "", "data_level": "",
                                   "study_accession": studs[0] if studs else None,
                                   "sample_id": None, "run_accession": None, "individual_accession": None,
                                   "individual_name": None, "sample_name": None, "specimen_type": None,
                                   "specimen_types": None, "tissue_type": None, "sample_role": None,
                                   "sample_role_label": None, "read_pair": None, "file_path": r[2],
                                   "input_role": fmt, "match_reason": f"格式匹配 {fmt}"})
                    refs.append(r[0])
    expected = len(used_slots) if used_slots else len([f for f in input_formats if f.strip().lower() not in ("", "null")])
    missing = [sname for sname in used_slots if sname not in [a["input_role"] for a in assets]]
    return {"status": "available" if assets else "information", "source": "neo4j",
            "assets": assets, "matched_count": matched, "expected_count": expected,
            "missing_asset_names": missing, "study_accessions": studs,
            "reference_asset_names": refs, "cohort_candidates": cohort}

# ---------- 候选链装配（图内 atomic 链） ----------
def assemble_candidates(query: str, start_hint: str = "", limit: int = 3) -> list:
    """从入口 atomic 工具出发，枚举图内 next_tool 链（≤4 步），闭集校验后输出 candidates[]。"""
    starts = ["fastp", "trim_galore", "paired_fastq_to_unmapped_bam", "star"]
    if start_hint and start_hint in starts:
        starts = [start_hint]
    cands = []
    for st in starts:
        rows = neo4j_q([f"MATCH p=(a:tool)-[:next_tool*1..4]->(b:tool) WHERE toLower(a.tool_name) = '{st}' RETURN [n IN nodes(p) | n.tool_name] AS chain LIMIT 5"])
        if not rows:
            continue
        for r in rows[0]:
            chain = r[0]
            steps = [{"step_id": f"s{i+1}", "tool_id": t, "inputs": [],
                      "selection_reason": "next_tool 图内可达"} for i, t in enumerate(chain)]
            cands.append({"rank": len(cands) + 1,
                          "match_note": f"原子链 {st} → {' → '.join(chain[1:])}",
                          "workflow_mode": "atomic",
                          "match_id": f"candidate-{st}-{len(cands)+1}",
                          "study_accession": None, "validation_ok": True,
                          "feasibility_status": "feasible",
                          "assets": [], "tool_chain": steps})
            if len(cands) >= limit:
                return cands
    return cands

# ---------- 主路由 ----------
def route_pipeline_request(query: str, top_k: int = 3) -> dict:
    """tool-chain/v2 信封（对齐重 MCP）。规则+图，无 server 内 LLM。"""
    from mcp_light_server import _predict_baseline, CATALOG  # noqa: F401
    top = _predict_baseline(query)
    intent = intent_extract(query, top[0] if top else "",
                            CATALOG.get(top[0], {}).get("tool_name", "") if top else "")
    if not top:
        return {"schema_version": "tool-chain/v2", "selection_status": "no_candidate",
                "candidate_count": 0, "candidates": [],
                "recommendation_count": 0, "recommendations": [],
                "unsupported_reason": "未匹配到图谱内工具；请补充数据格式/分析类型描述。",
                "intent": intent,
                "planner_metadata": {"used": False, "status": "force_rule", "calls": 0, "stages": []}}
    pid = top[0]
    cat = CATALOG.get(pid, {})
    in_fmts = [f.strip() for f in (cat.get("input_format") or "").split(",")]
    data = match_data(pid, in_fmts, intent)
    intent["disease_studies"] = [c["study_accession"] for c in data.get("cohort_candidates", [])]
    intent["disease_status"] = "matched" if intent["disease_studies"] else ("unmatched" if intent.get("disease") else None)
    cands = assemble_candidates(query, start_hint="", limit=3)
    rec = {"rank": 1, "match_id": f"recommendation-{pid}",
           "pipeline_id": pid, "match_note": f"语义规则匹配：{cat.get('tool_name','')}",
           "tool": {"tool_id": pid, "catalog_id": cat.get("catalog_id", ""),
                    "tool_kind": cat.get("tool_kind", ""), "name": cat.get("tool_name", pid),
                    "description": cat.get("description", ""),
                    "inputs": [], "outputs": [],
                    "catalog_status": "from_catalog", "source": cat.get("catalog_source", "")},
           "data": data, "source": "deterministic_rule+neo4j", "reference_case_id": None}
    return {"schema_version": "tool-chain/v2", "selection_status": "information",
            "candidate_count": len(cands), "candidates": cands,
            "recommendation_count": 1, "recommendations": [rec],
            "unsupported_reason": None, "intent": intent,
            "planner_metadata": {"used": False, "status": "force_rule", "calls": 0, "stages": []}}
