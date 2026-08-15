#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp_light_server.py — 轻架构 stdio MCP server（无第三方依赖）

给前端 agent（Claude Code / Codex / 自研 MCP 客户端，同机 stdio）提供：
  - health_check:           Neo4j 连通与图谱规模
  - plan_bio_analysis:      自然语言生信问题 → tool-chain/v2 plan（轻架构规则面）
  - 生信相关性门：与生信无关的问题直接拒绝（fail-closed）

用法：
  export NEO4J_USER=neo4j NEO4J_PASSWORD=<你的密码>
  python3 mcp_light_server.py

协议：MCP stdio（newline-delimited JSON-RPC），与前端 agent 直接对接。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

NEO4J_URL = os.environ.get("NEO4J_URL", "http://127.0.0.1:7474/db/neo4j/tx/commit")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")

# ---------- 生信相关性门（fail-closed） ----------
BIO_TERMS = [
    "生信", "生物信息", "测序", "fastq", "fq.gz", "bam", "vcf", "maf", "tsv", "bam文件",
    "基因", "表达", "转录组", "rna-seq", "rna seq", "wes", "wgs", "scrna", "sc-rna", "单细胞",
    "突变", "变异", "snp", "indel", "体细胞", "胚系", "germline", "somatic",
    "质控", "比对", "注释", "定量", "count", "tpm", "fpkm", "差异表达", "deg", "差异基因",
    "富集", "go分析", "kegg", "reactome", "gsea", "通路",
    "生存", "kaplan", "km曲线", "log-rank", "cox", "pfs", "os生存",
    "免疫浸润", "免疫细胞", "cibersort", "肿瘤微环境",
    "聚类", "分型", "亚型", "wgcna", "共表达", "hub基因", "hub 基因",
    "细胞通讯", "cellchat", "cellranger", "barcode", "umi", "seurat", "scanpy", "h5ad",
    "肿瘤", "癌症", "癌", "队列", "样本", "病人", "患者", "oncoplot", "tmb", "her2", "erbb2", "egfr",
    "参考基因组", "注释文件", "gtf", "star", "hisat", "bwa", "gatk", "mutect", "snpeff", "bcftools",
    "文库", "接头", "引物", "reads", "read group", "ubam", "uBAM", "染色体", "位点",
]
NON_BIO_TERMS = [
    "雅思", "托福", "口语", "写作", "听力", "阅读机经", "签证", "留学", "申请文书", "高考", "考研",
    "租房", "搬家", "健身", "菜谱", "股票", "基金", "税务", "理财",
    "写代码", "前端", "后端", "react", "vue", "css", "html", "bug", "debug", "部署", "docker",
    "怎么写", "论文", "语法", "翻译", "简历", "面试",
]

def _hits(q, terms):
    low = q.lower()
    out = []
    for t in terms:
        tl = t.lower()
        # 纯字母/数字词用词边界匹配，避免 "react" 误伤 "Reactome"
        if re.fullmatch(r"[a-z0-9]+", tl):
            if re.search(rf"\b{re.escape(tl)}\b", low):
                out.append(t)
        elif tl in low:
            out.append(t)
    return out

def check_relevance(query: str) -> dict:
    """fail-closed：有生信信号才放行；明显非生信或无法确认 → 拒绝。"""
    bio = _hits(query, BIO_TERMS)
    non_bio = _hits(query, NON_BIO_TERMS)
    is_bio = bool(bio)
    if is_bio and not non_bio:
        return {"is_bio": True, "reason": "ok", "bio_hits": bio, "non_bio_hits": non_bio}
    if non_bio and not bio:
        return {"is_bio": False, "reason": "rejected: 非生信问题", "bio_hits": bio, "non_bio_hits": non_bio}
    if bio and non_bio:
        return {"is_bio": False, "reason": "rejected: 问题同时包含生信与非生信信号，无法确认意图", "bio_hits": bio, "non_bio_hits": non_bio}
    return {"is_bio": False, "reason": "rejected: 未检测到生信相关信号（fail-closed）", "bio_hits": bio, "non_bio_hits": non_bio}

# ---------- 语义判别（轻架构规则面，源自 96 例评测） ----------
RULES = [
    (["10x", "cellranger", "CellRanger", "单细胞", "barcode", "Seurat", "Scanpy"], "cellranger_workflow", 3),
    (["uBAM", "unmapped bam", "未比对", "read group"], "paired_fastq_to_unmapped_bam", 3),
    (["肿瘤突变负荷", "tmb", "TMB"], "tmb_survival_analysis", 3),
    (["her2", "HER2", "ERBB2"], "her2_pfs_survival", 3),
    (["驱动基因", "男女", "性别分层"], "driver_gene_gender_analysis", 3),
    (["突变景观", "oncoplot", "Oncoplot", "高频突变", "突变类型", "Top30", "top30"], "wes_somatic_maf_landscape", 3),
    (["体细胞突变检测", "somatic vcf", "体细胞变异", "配对", "tumor-normal", "肿瘤和正常"], "wes_somatic_pair", 3),
    (["免疫浸润", "免疫细胞", "CIBERSORT", "浸润"], "immune_infiltration_iobr", 3),
    (["wgcna", "WGCNA", "共表达", "模块", "hub 基因", "hub基因"], "wgcna", 3),
    (["无监督聚类", "分型", "亚型", "GMM", "聚类数", "聚类稳定性", "聚类"], "rnaseq_unsupervised_cluster", 3),
    (["rRNA", "完整上游", "质控、剪切", "质控、接头", "质控、比对和表达计数", "上游"], "rnaseq_singletask", 2),
    (["egfr", "EGFR"], "survival_analysis", 3),
    (["kegg", "KEGG", "Reactome", "信号通路", "通路富集"], "diff_expr_kegg", 2),
    (["GO", "go 富集", "生物学功能", "生物过程"], "diff_expr_go", 2),
    (["差异表达", "差异基因", "表达不同", "表达差异", "上调", "下调", "deg", "DEG", "limma", "功能"], "diff_expr_go", 1),
]

# 闭集目录（14 个业务 pipeline/task 的关键字段，来自 skill/references/tool_catalog.csv）
CATALOG = {
    "cellranger_workflow": ("单细胞 RNA-seq 完整流程（CellRanger）", ["fq.gz", "bam"], ["count_matrix", "qc"]),
    "diff_expr_go": ("差异表达 + GO 富集（limma + clusterProfiler）", ["tsv"], ["deg_list", "go_table", "plot"]),
    "diff_expr_kegg": ("差异表达 + Reactome/KEGG 通路富集", ["tsv"], ["deg_list", "pathway_table", "plot"]),
    "driver_gene_gender_analysis": ("驱动基因突变频率性别分层分析", ["maf", "xls", "xlsx"], ["stat_table", "plot"]),
    "her2_pfs_survival": ("HER2/ERBB2 表达分层与无进展生存（PFS）分析", ["tsv", "xls", "xlsx"], ["km_curve", "logrank", "table"]),
    "immune_infiltration_iobr": ("免疫浸润分析（IOBR CIBERSORT）", ["tsv", "xls", "xlsx"], ["immune_fraction", "plot"]),
    "paired_fastq_to_unmapped_bam": ("双端 FASTQ 转 uBAM（GATK 起始）", ["fq.gz"], ["ubam"]),
    "rnaseq_singletask": ("RNA-seq 单任务完整上游流程", ["fq.gz"], ["bam", "count_matrix", "expression", "qc"]),
    "rnaseq_unsupervised_cluster": ("表达矩阵无监督聚类/分型（GMM）", ["tsv"], ["cluster", "bic", "plot"]),
    "survival_analysis": ("基因突变状态与 PFS 生存分析（EGFR 等）", ["maf", "xlsx"], ["km_curve", "cox", "table"]),
    "tmb_survival_analysis": ("TMB 计算与生存分析", ["maf", "xlsx"], ["tmb", "km_curve", "table"]),
    "wes_somatic_maf_landscape": ("WES 体细胞突变景观（Oncoplot）", ["maf"], ["oncoplot", "frequency_table"]),
    "wes_somatic_pair": ("配对 tumor-normal WES 体细胞变异检测", ["fq.gz"], ["vcf", "bam", "qc"]),
    "wgcna": ("WGCNA 加权基因共表达网络", ["tsv", "xls", "xlsx"], ["modules", "hub_genes", "plot"]),
}
DISEASE_MAP = [("肝癌", "liver"), ("肝", "liver"), ("胶质瘤", "glioma"), ("黑色素瘤", "melanoma"),
               ("食管癌", "esoph"), ("肺癌", "lung"), ("乳腺癌", "breast"), ("肠癌", "colorect")]

# ---------- Neo4j（read-cypher 等价，curl） ----------
def neo4j_q(statements):
    if not NEO4J_PASSWORD:
        return None
    payload = json.dumps({"statements": [{"statement": s} for s in statements]})
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(payload)
        tmp = f.name
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "20", "-u", f"{NEO4J_USER}:{NEO4J_PASSWORD}",
             "-X", "POST", "-H", "Content-Type: application/json", "-d", "@" + tmp, NEO4J_URL],
            capture_output=True, text=True)
        d = json.loads(r.stdout)
        return [[row["row"] for row in res.get("data", [])] for res in d.get("results", [])]
    except Exception:
        return None
    finally:
        os.unlink(tmp)

def predict_tool(query):
    scores = {}
    for terms, pid, w in RULES:
        if any(t.lower() in query.lower() for t in terms):
            scores[pid] = scores.get(pid, 0) + w
    if "GO" in query:
        scores["diff_expr_go"] = scores.get("diff_expr_go", 0) + 1
        if "kegg" not in query.lower() and "Reactome" not in query:
            scores.pop("diff_expr_kegg", None)
    elif "通路" in query:
        scores["diff_expr_kegg"] = scores.get("diff_expr_kegg", 0) + 1
        if "GO" not in query and "kegg" not in query.lower() and "Reactome" not in query:
            scores.pop("diff_expr_go", None)
    return [pid for pid, _ in sorted(scores.items(), key=lambda kv: -kv[1])][:3]

# ---------- Plan 组装 ----------
def build_plan(query):
    intent = {"query_text": query, "source": "rule", "ambiguous": False}
    top = predict_tool(query)
    if not top:
        return {"schema_version": "tool-chain/v2", "selection_status": "no_candidate",
                "candidate_count": 0, "candidates": [], "recommendation_count": 0,
                "recommendations": [], "intent": intent,
                "unsupported_reason": "未匹配到图谱内工具；请补充数据格式/分析类型描述。"}
    pid = top[0]
    name, inputs, outputs = CATALOG.get(pid, (pid, [], []))
    study_candidates, format_counts = [], {}
    disease = next((d for d, _ in DISEASE_MAP if d in query), None)
    if disease:
        kw = dict(DISEASE_MAP)[disease]
        rows = neo4j_q([f"MATCH (s:study) WHERE toLower(s.tumor_type) CONTAINS '{kw}' RETURN s.study_accession, s.title, s.sample_count, s.tumor_type LIMIT 5"])
        if rows:
            study_candidates = [{"study_accession": r[0], "title": r[1], "sample_count": r[2], "tumor_type": r[3]} for r in rows[0]]
    for fmt in inputs:
        rows = neo4j_q([f"MATCH (n:T1)-[:in_format]->(f:format) WHERE toLower(f.format) = toLower('{fmt}') RETURN count(n) AS c",
                        f"MATCH (n:T2)-[:in_format]->(f:format) WHERE toLower(f.format) = toLower('{fmt}') RETURN count(n) AS c"])
        if rows and rows[0] and rows[1]:
            format_counts[fmt] = {"T1": rows[0][0][0], "T2": rows[1][0][0]}
    return {
        "schema_version": "tool-chain/v2",
        "selection_status": "information",
        "candidate_count": 0, "candidates": [],
        "recommendation_count": 1,
        "recommendations": [{
            "rank": 1, "match_id": f"recommendation-{pid[:16]}",
            "pipeline_id": pid, "match_note": f"语义规则匹配：{name}",
            "tool": {"tool_id": pid, "name": name, "inputs": inputs, "outputs": outputs},
            "data": {"status": "available" if format_counts else "information",
                     "source": "neo4j",
                     "study_candidates": study_candidates,
                     "format_file_counts": format_counts},
            "source": "deterministic_rule+neo4j", "reference_case_id": None,
        }],
        "intent": intent,
        "planner_metadata": {"used": False, "status": "force_rule", "calls": 0, "stages": []},
        "data_matcher_mode": "neo4j",
    }

# ---------- MCP 工具 ----------
def tool_health_check(args):
    rows = neo4j_q(["MATCH (n) RETURN count(n) AS nodes", "MATCH (n:tool) RETURN count(n) AS tools"])
    if rows is None:
        return {"status": "unavailable", "detail": "Neo4j 不可达（请检查 NEO4J_USER/NEO4J_PASSWORD/NEO4J_URL）"}
    return {"status": "ok", "nodes": rows[0][0][0], "tools": rows[1][0][0]}

def tool_plan_bio_analysis(args):
    query = (args.get("query") or "").strip()
    if not query:
        return {"status": "rejected", "reason": "query 不能为空"}
    rel = check_relevance(query)
    if not rel["is_bio"]:
        return {"status": "rejected", "reason": rel["reason"],
                "bio_hits": rel["bio_hits"], "non_bio_hits": rel["non_bio_hits"]}
    plan = build_plan(query)
    plan["relevance"] = {"is_bio": True, "bio_hits": rel["bio_hits"], "non_bio_hits": []}
    return {"status": "ok", "plan": plan}

TOOLS = {
    "health_check": {
        "description": "检查 Neo4j 后端连通性与图谱规模（只读）。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": tool_health_check,
    },
    "plan_bio_analysis": {
        "description": "输入生信分析需求（自然语言），返回 tool-chain/v2 工具链 Plan（含匹配工具、数据候选与格式文件数）。与生信无关的问题会被拒绝（fail-closed）。",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "description": "生信分析需求，如：我想看肝癌样本里的免疫细胞组成"}}, "required": ["query"]},
        "handler": tool_plan_bio_analysis,
    },
}

# ---------- MCP stdio 协议 ----------
def _send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": msg.get("id"),
                   "result": {"protocolVersion": "2024-11-05",
                              "capabilities": {"tools": {"listChanged": False}},
                              "serverInfo": {"name": "bio-pipeline-light", "version": "1.0.0"}}})
        elif method == "notifications/initialized":
            pass
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": msg.get("id"), "result": {}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": msg.get("id"),
                   "result": {"tools": [{"name": n, "description": t["description"], "inputSchema": t["inputSchema"]}
                                        for n, t in TOOLS.items()]}})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {}) or {}
            tool = TOOLS.get(name)
            if not tool:
                _send({"jsonrpc": "2.0", "id": msg.get("id"),
                       "result": {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}})
                continue
            try:
                out = tool["handler"](args)
                text = json.dumps(out, ensure_ascii=False, indent=1)
                _send({"jsonrpc": "2.0", "id": msg.get("id"),
                       "result": {"content": [{"type": "text", "text": text}], "structuredContent": out}})
            except Exception as e:  # noqa: BLE001
                _send({"jsonrpc": "2.0", "id": msg.get("id"),
                       "result": {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}})
        elif msg.get("id") is not None:
            _send({"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32601, "message": "method not found"}})

if __name__ == "__main__":
    main()
