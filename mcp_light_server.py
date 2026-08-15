#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp_light_server.py — 轻架构 stdio MCP server（无第三方依赖）

交付形态 = skill + MCP：**推理留给调用方的模型**，本 server 只提供知识与校验：

  get_planning_guide()      → 返回 SKILL.md 全文（调用方模型自己读、自己规划）
  read_cypher(query)        → 数据面：通用只读 Cypher 查询（有只读守卫）
  validate_atomic_chain(chain) → 确定性闭集校验（11 个 atomic 工具 + 图内 next_tool 邻接）
  health_check()            → Neo4j 连通与图谱规模

兼容/对照臂（明确标注，非推荐路径）：
  rule_baseline_plan(query) → 关键词基线规划，仅供与模型路径对照，不用于生产

目录数据（tool_catalog.csv）启动时从 skill/references/ 读取，不内嵌拷贝。
用法：export NEO4J_USER=neo4j NEO4J_PASSWORD=<密码> && python3 mcp_light_server.py
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
NEO4J_URL = os.environ.get("NEO4J_URL", "http://127.0.0.1:7474/db/neo4j/tx/commit")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")
SKILL_REF = os.environ.get("BIO_SKILL_REF", os.path.join(HERE, "skill", "references"))
SKILL_MD = os.environ.get("BIO_SKILL_MD", os.path.join(os.path.dirname(SKILL_REF), "SKILL.md"))
_SAFE_TOKEN = re.compile(r"^[a-zA-Z0-9_\-\u4e00-\u9fff ]+$")

# ---------- 目录加载（从 skill/references/tool_catalog.csv，不内嵌） ----------
ATOMIC_IDS: set[str] = set()
CATALOG: dict[str, dict] = {}

def load_catalog() -> None:
    global ATOMIC_IDS, CATALOG
    path = os.path.join(SKILL_REF, "tool_catalog.csv")
    if not os.path.exists(path):
        return
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tid = (row.get("tool_id") or "").replace("tool_id:", "")
            kind = row.get("tool_kind") or ""
            if not tid:
                continue
            CATALOG[tid] = {
                "tool_id": tid,
                "catalog_id": row.get("catalog_id") or "",
                "tool_kind": kind,
                "tool_name": row.get("tool_name") or tid,
                "description": (row.get("description") or "").strip(),
                "input_format": row.get("input_format") or "",
                "output_format": row.get("output_format") or "",
                "omics": row.get("omics") or "",
            }
            if kind == "atomic" and tid != "multiqc":
                ATOMIC_IDS.add(tid)

load_catalog()

# ---------- 生信相关性门（仅 rule_baseline_plan 使用） ----------
BIO_TERMS = [
    "生信", "生物信息", "测序", "fastq", "fq.gz", "bam", "vcf", "maf", "tsv",
    "基因", "表达", "转录组", "rna-seq", "wes", "wgs", "scrna", "单细胞",
    "突变", "变异", "体细胞", "somatic", "质控", "比对", "注释", "定量",
    "count", "tpm", "fpkm", "差异表达", "deg", "差异基因", "富集", "kegg", "reactome", "gsea",
    "生存", "kaplan", "cox", "pfs", "免疫浸润", "免疫细胞", "cibersort",
    "聚类", "分型", "亚型", "wgcna", "共表达", "hub", "细胞通讯", "cellchat",
    "cellranger", "barcode", "umi", "seurat", "scanpy", "h5ad",
    "肿瘤", "癌症", "癌", "队列", "样本", "病人", "患者", "oncoplot", "tmb", "her2", "erbb2", "egfr",
    "参考基因组", "gtf", "star", "hisat", "bwa", "gatk", "mutect", "snpeff", "bcftools",
    "reads", "read group", "ubam", "染色体", "位点",
]
NON_BIO_TERMS = [
    "雅思", "托福", "口语", "写作", "听力", "签证", "留学", "高考", "考研",
    "租房", "搬家", "健身", "菜谱", "股票", "基金", "税务",
    "写代码", "前端", "后端", "react", "vue", "css", "html", "bug", "部署", "docker",
    "语法", "翻译", "简历", "面试",
]

def _hits(q, terms):
    low = q.lower()
    out = []
    for t in terms:
        tl = t.lower()
        if re.fullmatch(r"[a-z0-9]+", tl):
            if re.search(rf"\b{re.escape(tl)}\b", low):
                out.append(t)
        elif tl in low:
            out.append(t)
    return out

def check_relevance(query):
    bio = _hits(query, BIO_TERMS)
    non_bio = _hits(query, NON_BIO_TERMS)
    if bio and not non_bio:
        return {"is_bio": True, "reason": "ok", "bio_hits": bio, "non_bio_hits": []}
    if non_bio and not bio:
        return {"is_bio": False, "reason": "rejected: 非生信问题", "bio_hits": [], "non_bio_hits": non_bio}
    if bio and non_bio:
        return {"is_bio": False, "reason": "rejected: 生信与非生信信号并存，无法确认意图", "bio_hits": bio, "non_bio_hits": non_bio}
    return {"is_bio": False, "reason": "rejected: 未检测到生信信号（fail-closed）", "bio_hits": [], "non_bio_hits": []}

# ---------- Neo4j 数据面（curl，只读守卫） ----------
_WRITE_RE = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET\s|REMOVE|DROP|DETACH|FOREACH)\b|CALL\s+dbms\.|db\.create",
    re.IGNORECASE)

def _assert_read_only(query):
    if _WRITE_RE.search(query):
        raise ValueError("read_cypher 只允许只读查询（检测到写入语句）")

def neo4j_q(statements):
    if not NEO4J_PASSWORD:
        raise RuntimeError("set NEO4J_PASSWORD (and optionally NEO4J_USER)")
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
        if d.get("errors"):
            raise RuntimeError("; ".join(e.get("message", "") for e in d["errors"])[:500])
        return [[row["row"] for row in res.get("data", [])] for res in d.get("results", [])]
    finally:
        os.unlink(tmp)

# ---------- 工具 ----------
def tool_get_planning_guide(args):
    try:
        text = open(SKILL_MD, encoding="utf-8").read()
        return {"status": "ok", "skill": text, "source": SKILL_MD}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

def tool_read_cypher(args):
    query = (args.get("query") or "").strip()
    if not query:
        return {"status": "error", "detail": "query 不能为空"}
    try:
        _assert_read_only(query)
        rows = neo4j_q([query])
        return {"status": "ok", "columns_unknown": True, "rows": rows[0] if rows else []}
    except Exception as e:
        return {"status": "error", "detail": str(e)[:500]}

def tool_validate_atomic_chain(args):
    chain = args.get("chain") or []
    if not isinstance(chain, list) or not chain:
        return {"status": "error", "detail": "chain 必须是非空 tool_id 列表"}
    unknown = [t for t in chain if t not in CATALOG]
    non_atomic = [t for t in chain if t in CATALOG and CATALOG[t]["tool_kind"] != "atomic"]
    violations = []
    if unknown:
        violations.append(f"未知工具: {unknown}")
    if non_atomic:
        violations.append(f"非 atomic（闭集外）: {non_atomic}")
    # 图内 next_tool 邻接校验（图节点无 tool_id，用 toLower(tool_name) 匹配；入参过白名单）
    adjacency_ok = []
    for a, b in zip(chain[:-1], chain[1:]):
        if not (_SAFE_TOKEN.fullmatch(str(a)) and _SAFE_TOKEN.fullmatch(str(b))):
            violations.append(f"非法 tool_id 字符: {a}->{b}")
            continue
        rows = neo4j_q([f"MATCH (a:tool)-[:next_tool]->(b:tool) WHERE toLower(a.tool_name) = '{str(a).lower()}' AND toLower(b.tool_name) = '{str(b).lower()}' RETURN count(*) AS c"])
        if rows and rows[0] and rows[0][0][0] > 0:
            adjacency_ok.append((a, b))
    missing_edges = [(a, b) for a, b in zip(chain[:-1], chain[1:]) if (a, b) not in adjacency_ok]
    if missing_edges:
        violations.append(f"图谱中无 next_tool 边: {missing_edges}")
    return {"status": "valid" if not violations else "invalid",
            "chain": chain, "violations": violations, "adjacency_ok": adjacency_ok,
            "atomic_closed_set_size": len(ATOMIC_IDS)}

# 关键词基线（对照臂，非推荐路径）
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

def _predict_baseline(query):
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

def tool_rule_baseline_plan(args):
    """对照臂：关键词基线。仅供与模型路径对比，不用于生产（见 README 三臂结论）。"""
    query = (args.get("query") or "").strip()
    rel = check_relevance(query)
    if not rel["is_bio"]:
        return {"status": "rejected", "reason": rel["reason"], "bio_hits": rel["bio_hits"], "non_bio_hits": rel["non_bio_hits"]}
    top = _predict_baseline(query)
    return {"status": "ok", "baseline": True, "top_pipelines": top,
            "note": "关键词基线（ceiling arm），非推荐路径；推理请用 get_planning_guide + read_cypher"}

def tool_health_check(args):
    try:
        rows = neo4j_q(["MATCH (n) RETURN count(n) AS nodes", "MATCH (n:tool) RETURN count(n) AS tools"])
        return {"status": "ok", "nodes": rows[0][0][0], "tools": rows[1][0][0],
                "atomic_closed_set": sorted(ATOMIC_IDS)}
    except Exception as e:
        return {"status": "unavailable", "detail": str(e)[:300]}

TOOLS = {
    "get_planning_guide": {
        "description": "返回生信链路规划 skill 全文（SKILL.md）。调用方模型应读取它后自行规划；本 server 不做推理。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": tool_get_planning_guide,
    },
    "read_cypher": {
        "description": "数据面：对 Neo4j 知识图谱执行只读 Cypher 查询（有只读守卫，写入语句会被拒）。",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "description": "只读 Cypher，结果多时加 LIMIT"}}, "required": ["query"]},
        "handler": tool_read_cypher,
    },
    "validate_atomic_chain": {
        "description": "确定性闭集校验：给定 atomic 工具链，校验闭集成员 + 图内 next_tool 邻接。",
        "inputSchema": {"type": "object", "properties": {"chain": {"type": "array", "items": {"type": "string"}, "description": "atomic tool_id 有序列表"}}, "required": ["chain"]},
        "handler": tool_validate_atomic_chain,
    },
    "rule_baseline_plan": {
        "description": "（对照臂，非推荐路径）关键词基线规划。仅用于与模型路径对比。",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        "handler": tool_rule_baseline_plan,
    },
    "health_check": {
        "description": "检查 Neo4j 连通性、图谱规模与 atomic 闭集。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": tool_health_check,
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
                              "serverInfo": {"name": "bio-pipeline-light", "version": "2.0.0"}}})
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
