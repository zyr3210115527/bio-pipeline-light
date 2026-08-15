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

XLSX = sys.argv[1] if len(sys.argv) > 1 else "/Users/zhouyiran/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/wxid_g28u5bj9tazm32_13f8/msg/file/2026-07/96例问题-数据-工具对应表(1).xlsx"
SKILL_REF = os.path.expanduser("~/.dsh/skills/bio-pipeline-planning/references")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")
if not NEO4J_PASSWORD:
    sys.exit("set NEO4J_PASSWORD (and optionally NEO4J_USER) before running")
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
# 格式/模态提示 → 用目录 input_format 复检
FMT_HINTS = [("fastq", "fq.gz"), ("fq.gz", "fq.gz"), ("fpkm", "tsv"), ("tpm", "tsv"),
             ("counts", "tsv"), ("表达矩阵", "tsv"), ("maf", "maf"), ("MAF", "maf")]

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

def load_catalog():
    rows = []
    with open(os.path.join(SKILL_REF, "tool_catalog.csv")) as f:
        header = f.readline().strip().split(",")
        for line in f:
            # CSV 里 description 含引号换行，这里用简单解析：取前 4 列 + 后 4 列
            parts = line.strip().split(",", 5)
            if len(parts) < 4:
                continue
            tool_id = parts[0].replace("tool_id:", "")
            tool_kind = parts[9] if len(parts) > 9 else ""
            # 简化：只取关键列（identity, labels, catalog_id, catalog_source, description, input_format, omics, output_format, tool_id, tool_kind, tool_name）
            cols = line.strip().split("\t") if "\t" in line else line.strip().split(",", 12)
            rows.append({
                "tool_id": tool_id,
                "catalog_id": cols[2] if len(cols) > 2 else "",
                "input_format": cols[5] if len(cols) > 5 else "",
                "omics": cols[6] if len(cols) > 6 else "",
                "output_format": cols[7] if len(cols) > 7 else "",
                "tool_kind": cols[9] if len(cols) > 9 else "",
                "tool_name": cols[10] if len(cols) > 10 else "",
            })
    return rows

def neo4j_q(statements):
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
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top = [pid for pid, _ in ranked]
    return top[:3]

def data_status(filename):
    """配方 3：期望文件在图内是否可查（T1/T2 的 file_name 子串/全名匹配）。"""
    base = filename.split("(")[0].strip()  # 去掉 "(bytes)" 等后缀
    base = base.split("/")[-1]
    results = neo4j_q([
        f'MATCH (n:T1) WHERE n.file_name CONTAINS "{base}" RETURN "T1" AS src, count(*) AS c',
        f'MATCH (n:T2) WHERE n.file_name CONTAINS "{base}" RETURN "T2" AS src, count(*) AS c',
        f'MATCH (n) WHERE n.file_name = "{base}" RETURN labels(n)[0] AS src, count(*) AS c',
    ])
    found = any(int(r[0][1]) > 0 for r in results if r)
    return found

def main():
    cases = read_xlsx_rows(XLSX)
    cat = load_catalog()
    print(f"cases={len(cases)} unique_tools={len(set(c['expected'] for c in cases))}")
    tool_top1 = tool_top3 = data_all = data_found_total = data_file_total = combined = 0
    misses, per_tool = [], Counter()
    detail = []
    for idx, c in enumerate(cases, 1):
        pred = predict_tool(c["q"])
        exp = c["expected"]
        t1 = bool(pred) and pred[0] == exp
        t3 = exp in pred
        # 数据面
        statuses = {f: data_status(f) for f in c["data"]}
        all_found = all(statuses.values())
        tool_top1 += t1
        tool_top3 += t3
        if all_found:
            data_all += 1
        data_found_total += sum(statuses.values())
        data_file_total += len(c["data"])
        if t1 and all_found:
            combined += 1
        per_tool[(exp, "tool")] += 1
        per_tool[(exp, "hit")] += 1 if t1 else 0
        if not (t1 and all_found):
            misses.append({"id": f"q{idx:03d}", "q": c["q"][:60], "expected": exp,
                           "predicted": pred, "data": {k: v for k, v in statuses.items()}})
        detail.append({"id": f"q{idx:03d}", "expected": exp, "predicted": pred, "tool_hit": t1,
                       "data_all_found": all_found, "data_status": statuses})
    n = len(cases)
    report = {
        "cases": n,
        "tool_top1": tool_top1, "tool_top1_rate": round(tool_top1 / n, 4),
        "tool_top3": tool_top3, "tool_top3_rate": round(tool_top3 / n, 4),
        "data_all_found": data_all, "data_all_found_rate": round(data_all / n, 4),
        "data_file_found": data_found_total, "data_file_total": data_file_total,
        "data_file_found_rate": round(data_found_total / data_file_total, 4),
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
