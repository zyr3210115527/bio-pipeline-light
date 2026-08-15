#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻架构 96-case 评测：skill 配方（bio-pipeline-planning）+ Neo4j(read-cypher 等价) + 规则推理
对照 96例问题-数据-工具对应表(1).xlsx 的期望（数据文件、工具）评估：
  - tool_top1 / tool_top3 / tool_exact
  - data_found（期望文件在图内是否存在）/ data_found_all
  - combined（工具 top1 且数据全找到）
运行方式：python3 bench_light_96.py <xlsx路径>
"""
import json, subprocess, sys, tempfile, os
from collections import Counter

if len(sys.argv) < 2:
    sys.exit("用法: python3 bench_light_96.py <96例问题-数据-工具对应表.xlsx>")
XLSX = sys.argv[1]
SKILL_REF = os.path.expanduser("~/.dsh/skills/bio-pipeline-planning/references")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")
NEO4J_AUTH = f"{NEO4J_USER}:{NEO4J_PASSWORD}"
NEO4J_URL = "http://127.0.0.1:7474/db/neo4j/tx/commit"

# ---------- 工具语义判别规则（skill 词汇表） ----------
# (触发词列表, pipeline_id, 权重)。顺序即特异性优先级。
RULES = [
    (["10x", "cellranger", "CellRanger", "单细胞", "barcode", "Seurat", "Scanpy"], "cellranger_workflow", 3),
    (["uBAM", "unmapped bam", "未比对", "read group"], "paired_fastq_to_unmapped_bam", 3),
    (["肿瘤突变负荷", "tmb", "TMB"], "tmb_survival_analysis", 3),
    (["her2", "HER2", "ERBB2"], "her2_pfs_survival", 3),
    (["驱动基因", "男女", "性别分层"], "driver_gene_gender_analysis", 3),
    (["突变景观", "oncoplot", "Oncoplot", "高频突变", "突变类型", "Top30", "top30"], "wes_somatic_maf_landscape", 3),
    (["体细胞突变检测", "somatic vcf", "体细胞变异", "配对", "tumor-normal", "tumor 和 normal", "肿瘤和正常"], "wes_somatic_pair", 3),
    (["免疫浸润", "免疫细胞", "CIBERSORT", "浸润"], "immune_infiltration_iobr", 3),
    (["wgcna", "WGCNA", "共表达", "模块", "hub 基因", "hub基因"], "wgcna", 3),
    (["无监督聚类", "分型", "亚型", "GMM", "聚类数", "聚类稳定性", "聚类"], "rnaseq_unsupervised_cluster", 3),
    (["rRNA", "完整上游", "质控、剪切", "质控、接头", "质控、比对和表达计数", "上游"], "rnaseq_singletask", 2),
    (["egfr", "EGFR"], "survival_analysis", 3),
    (["kegg", "KEGG", "Reactome", "信号通路", "通路富集"], "diff_expr_kegg", 2),
    (["GO", "go 富集", "生物学功能", "生物过程"], "diff_expr_go", 2),
    (["差异表达", "差异基因", "表达不同", "表达差异", "上调", "下调", "deg", "DEG", "limma", "功能"], "diff_expr_go", 1),
]
# 格式/模态提示 → 用目录 input_format 复检（predict_tool 中实现）
FMT_HINTS = [("fastq", "fq.gz"), ("fq.gz", "fq.gz"), ("fpkm", "tsv"), ("tpm", "tsv"),
             ("counts", "tsv"), ("表达矩阵", "tsv"), ("maf", "maf"), ("MAF", "maf")]
FORMAT_HINT_TO_FMT = {kw.lower(): fmt for kw, fmt in FMT_HINTS}

def read_xlsx_rows(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["Sheet1"]
    cases = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue
        q, d, t = (row[1], row[2], row[3])
        if not q or not t:
            continue
        files = [f.strip() for f in (d or "").split("\n") if f.strip()]
        cases.append({"q": q, "data": files, "expected": t.strip()})
    return cases

import csv as _csv

CATALOG: dict = {}

def load_catalog():
    """用 csv 模块正经解析 tool_catalog.csv，返回 {tool_id: {...}}；模块级 CATALOG 供 predict_tool 复检。"""
    global CATALOG
    path = os.path.join(SKILL_REF, "tool_catalog.csv")
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        for row in reader:
            tid = (row.get("tool_id") or "").replace("tool_id:", "")
            if not tid:
                continue
            CATALOG[tid] = {
                "tool_id": tid,
                "catalog_id": row.get("catalog_id") or "",
                "input_format": row.get("input_format") or "",
                "omics": row.get("omics") or "",
                "output_format": row.get("output_format") or "",
                "tool_kind": row.get("tool_kind") or "",
                "tool_name": row.get("tool_name") or tid,
            }
    return CATALOG

def neo4j_q(statements):
    if not NEO4J_PASSWORD:
        raise RuntimeError("set NEO4J_PASSWORD (and optionally NEO4J_USER) to query Neo4j")
    payload = json.dumps({"statements": [{"statement": s} for s in statements]})
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(payload)
        tmp = f.name
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "25", "-u", NEO4J_AUTH, "-X", "POST",
             "-H", "Content-Type: application/json", "-d", "@" + tmp, NEO4J_URL],
            capture_output=True, text=True)
        d = json.loads(r.stdout)
        return [[row["row"] for row in res.get("data", [])] for res in d.get("results", [])]
    finally:
        os.unlink(tmp)

def predict_tool(q):
    """skill 配方 1：语义判别打分 + 目录格式复检；返回 top-3。"""
    scores = {}
    for terms, pid, w in RULES:
        hit = any(t.lower() in q.lower() for t in terms)
        if hit:
            scores[pid] = scores.get(pid, 0) + w
    # GO vs KEGG 特判：互斥修正 + "通路"弱信号
    if "GO" in q:
        scores["diff_expr_go"] = scores.get("diff_expr_go", 0) + 1
        scores.pop("diff_expr_kegg", None) if "kegg" not in q.lower() and "Reactome" not in q else None
    elif "通路" in q:
        scores["diff_expr_kegg"] = scores.get("diff_expr_kegg", 0) + 1
        # 含"通路"且未提 GO → KEGG 胜出（互斥）
        if "GO" not in q and "kegg" not in q.lower() and "Reactome" not in q:
            scores.pop("diff_expr_go", None)
    # 目录格式复检：问题里出现的格式提示，命中候选工具 input_format 则加权
    q_low = q.lower()
    fmt_hits = {fmt for kw, fmt in FMT_HINTS if kw.lower() in q_low}
    if fmt_hits:
        for pid in scores:
            cat = CATALOG.get(pid)
            if cat and any(f in (cat.get("input_format") or "").lower() for f in fmt_hits):
                scores[pid] += 0.5
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top = [pid for pid, _ in ranked]
    return top[:3]

def data_status(filename):
    """配方 3：期望文件图内可查性。先精确匹配（file_name 全等），再宽匹配（CONTAINS）回退。
    返回 (exact_found, found)，两个数分开报：精确命中 vs 仅宽匹配命中。"""
    base = filename.split("(")[0].strip()  # 去掉 "(bytes)" 等后缀
    base = base.split("/")[-1]
    results = neo4j_q([
        f'MATCH (n:T1) WHERE n.file_name = "{base}" RETURN "T1" AS src, count(*) AS c',
        f'MATCH (n:T2) WHERE n.file_name = "{base}" RETURN "T2" AS src, count(*) AS c',
        f'MATCH (n) WHERE n.file_name CONTAINS "{base}" RETURN "T1orT2" AS src, count(*) AS c',
    ])
    exact = any(int(r[0][1]) > 0 for r in results[:2] if r)
    fuzzy = any(int(r[0][1]) > 0 for r in results[2:] if r)
    return exact, (exact or fuzzy)

def main():
    cases = read_xlsx_rows(XLSX)
    cat = load_catalog()
    print(f"cases={len(cases)} unique_tools={len(set(c['expected'] for c in cases))} catalog={len(cat)}")
    tool_top1 = tool_top3 = data_all = data_found_total = data_exact_total = data_file_total = combined = 0
    misses, per_tool = [], Counter()
    detail = []
    for idx, c in enumerate(cases, 1):
        pred = predict_tool(c["q"])
        exp = c["expected"]
        t1 = bool(pred) and pred[0] == exp
        t3 = exp in pred
        # 数据面：精确 + 宽匹配分开计
        statuses = {f: data_status(f) for f in c["data"]}
        all_found = all(v[1] for v in statuses.values())
        tool_top1 += t1
        tool_top3 += t3
        if all_found:
            data_all += 1
        data_found_total += sum(1 for v in statuses.values() if v[1])
        data_exact_total += sum(1 for v in statuses.values() if v[0])
        data_file_total += len(c["data"])
        if t1 and all_found:
            combined += 1
        per_tool[(exp, "tool")] += 1
        per_tool[(exp, "hit")] += 1 if t1 else 0
        if not (t1 and all_found):
            misses.append({"id": f"q{idx:03d}", "q": c["q"][:60], "expected": exp,
                           "predicted": pred, "data": {k: (v[1], "exact" if v[0] else "fuzzy") for k, v in statuses.items()}})
        detail.append({"id": f"q{idx:03d}", "expected": exp, "predicted": pred, "tool_hit": t1,
                       "data_all_found": all_found, "data_status": statuses})
    n = len(cases)
    report = {
        "cases": n,
        "tool_top1": tool_top1, "tool_top1_rate": round(tool_top1 / n, 4),
        "tool_top3": tool_top3, "tool_top3_rate": round(tool_top3 / n, 4),
        "data_all_found": data_all, "data_all_found_rate": round(data_all / n, 4),
        "data_file_found": data_found_total, "data_file_exact": data_exact_total,
        "data_file_total": data_file_total,
        "data_file_found_rate": round(data_found_total / data_file_total, 4),
        "data_file_exact_rate": round(data_exact_total / data_file_total, 4),
        "combined_top1_and_data": combined, "combined_rate": round(combined / n, 4),
        "misses": misses,
    }
    print(json.dumps({k: v for k, v in report.items() if k != "misses"}, ensure_ascii=False, indent=1))
    print("\n--- per-tool top1 命中 ---")
    for tid in sorted(set(c["expected"] for c in cases)):
        tot = sum(1 for c in cases if c["expected"] == tid)
        hit = sum(1 for c, d in zip(cases, detail) if c["expected"] == tid and d["tool_hit"])
        print(f"  {tid}: {hit}/{tot}")
    print(f"\n--- 未完全命中 {len(misses)} 例 ---")
    for m in misses[:25]:
        print(f"  {m['id']} exp={m['expected']} pred={m['predicted']} data={m['data']}")
    out = os.path.expanduser("~/Documents/dsh/bench_light_96_report.json")
    with open(out, "w") as f:
        json.dump({"report": report, "detail": detail}, f, ensure_ascii=False, indent=1)
    print(f"\nreport saved: {out}")

if __name__ == "__main__":
    main()
