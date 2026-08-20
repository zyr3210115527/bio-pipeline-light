#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""system_test.py — light 架构端到端系统测试（模拟调用方模型按 SKILL.md 配方行事）

每个场景 = 一个用户 prompt + 调用方模型应执行的查询/校验序列 + 结果断言。
运行：export NEO4J_URL=... NEO4J_USER=... NEO4J_PASSWORD=... && python3 benchmark/system_test.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mcp_light_server as m  # noqa: E402

FAILS = []

def sec(t):
    print(f"\n{'='*70}\n{t}\n{'='*70}")

def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (f"  [{str(detail)[:180]}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)

def q(s):
    return m.neo4j_q([s])[0]

# ────────────────────────────────────────────────────────────────────
sec("S1 「我想用肝癌数据做GO富集分析」— 师兄场景全链路")
# 配方1: 功能检索
rows = q("MATCH (t:tool)-[:has_function]->(f:function) WHERE f.function CONTAINS 'GO' AND f.function CONTAINS '富集' RETURN t.tool_name")
tools = [r[0] for r in rows]
check("功能检索命中 diff_expr_go", "diff_expr_go" in tools, tools)
# 配方3: 队列 + 现成 T2 矩阵
rows = q("MATCH (s:study) WHERE toLower(s.tumor_type) CONTAINS 'liver' OR toLower(s.tumor_type) CONTAINS 'hepatocell' "
         "RETURN s.study_accession, s.sample_count ORDER BY toInteger(s.sample_count) DESC LIMIT 1")
check("肝癌队列解析", bool(rows), rows)
study = rows[0][0] if rows else None
rows = q(f"MATCH (n:T2) WHERE n.study_accession = '{study}' AND n.file_name CONTAINS 'Genes' AND "
         "(n.file_name CONTAINS 'FPKM' OR n.file_name CONTAINS 'TPM') RETURN n.file_name, n.file_path LIMIT 2")
check(f"{study} 现成表达矩阵", bool(rows), rows)
matrix = rows[0][0] if rows else None
# 提交前把关
out = m.tool_validate_execution_chain({"steps": [
    {"tool_id": "diff_expr_go", "inputs": {"expression_matrix": {"file_name": matrix}}}]})
check("execution_params 回填真实路径",
      (out["execution_params"].get("expression_matrix") or "").startswith("/"), out["execution_params"])
check("submittable=true", out["submittable"] is True, out["execution_params_missing"])

# ────────────────────────────────────────────────────────────────────
sec("S2 「肿瘤和癌旁样本相比，哪些通路的基因表达变化最明显？」— 去名意图")
rows = q("MATCH (t:tool)-[:has_function]->(f:function) WHERE f.function CONTAINS '通路' OR f.function CONTAINS '差异' "
         "RETURN DISTINCT t.tool_name")
tools = [r[0] for r in rows]
check("数据面支撑通路/差异检索", any(t in tools for t in ("diff_expr_kegg", "gsea_pathway_enrichment")), tools)
# 分组前提: 该队列 tumor/normal 可分
r = m.tool_resolve_sample_roles({"study": "HRA001272"})
check("HRA001272 分组可行（role_resolved）", r.get("role_resolved") is True, r.get("sample_roles"))

# ────────────────────────────────────────────────────────────────────
sec("S3 「找一个有配对肿瘤/正常样本的队列做体细胞突变检测」— 配对 WES")
# 调用方模型应先发现可配对队列（SKILL 配方3），再对目标队列跑配对模板
rows = q("MATCH (sp:sample)-[:in_individual]->(i:individual) "
         "WITH sp.study_accession AS study, i, collect(DISTINCT sp.tissue_type) AS tts "
         "WHERE 'Tumor' IN tts AND 'Normal' IN tts "
         "RETURN study, count(i) AS pairable ORDER BY pairable DESC LIMIT 3")
check("可配对队列发现查询", bool(rows), rows)
target = rows[0][0] if rows else "HRA000873"
print(f"    可配对队列 top3: {rows} → 选 {target}")
tpl = open(os.path.join(os.path.dirname(m.__file__), "skill", "references",
                        "query_templates", "find_paired_tumor_normal_samples.cypher")).read()
import re as _re
params = {p: target for p in set(_re.findall(r"\$(\w+)", tpl))}
import urllib.request, base64
body = json.dumps({"statements": [{"statement": tpl, "parameters": params}]}).encode()
h = {"Content-Type": "application/json",
     "Authorization": "Basic " + base64.b64encode(f"{m.NEO4J_USER}:{m.NEO4J_PASSWORD}".encode()).decode()}
d = json.load(urllib.request.urlopen(urllib.request.Request(m.NEO4J_URL, data=body, headers=h), timeout=60))
if d.get("errors"):
    check("配对模板可执行", False, d["errors"][0]["message"][:150])
else:
    prows = [r["row"] for r in d["results"][0]["data"]]
    pairable = [r for r in prows if r and (True in r or "true" in [str(x).lower() for x in r])]
    check("配对模板可执行", True)
    check(f"{target} 存在可配对个体", bool(pairable), f"rows={len(prows)}")
r71 = m.tool_resolve_sample_roles({"study": "HRA000071"})
check("角色判定支撑分组（HRA000071 tumor>0 且 normal>0）", r71.get("role_resolved") is True, r71.get("sample_roles"))

# ────────────────────────────────────────────────────────────────────
sec("S4 「从FASTQ开始RNA-seq上游到表达定量，给原子工具链」— 原子链 + 卡契约")
v = m.tool_validate_atomic_chain({"chain": ["fastp", "star", "samtools", "featurecounts"]})
check("原子链闭集+邻接校验", v["status"] == "valid", v["violations"])
check("输出 Knowledge Card meta.id", all(tc.get("tool_id") for tc in v["tool_chain"]),
      [tc.get("tool_id") for tc in v["tool_chain"]])
# 绑真实 T1 FASTQ 走提交前把关（T1 file_path 是占位符时应如实 missing，不伪造）
rows = q("MATCH (n:T1)-[:in_study]->(s:study {study_accession:'HRA001272'}) "
         "WHERE n.file_name ENDS WITH '.fastq.gz' RETURN n.file_name, n.file_path LIMIT 2")
if rows:
    fq, fq_path = rows[0][0], rows[0][1]
    card = m.KC_MAP.get("fastp")
    bindings = {i["name"]: {"file_name": fq} for i in card["inputs"] if i.get("required", True)}
    out = m.tool_validate_execution_chain({"steps": [{"tool_id": "fastp", "inputs": bindings}]})
    real = str(fq_path or "").startswith("/")
    if real:
        check("T1 真实路径回填", all(p.startswith("/") for p in out["execution_params"].values()), out)
    else:
        check("T1 占位路径→如实 missing 不伪造", not out["submittable"] and out["execution_params_missing"],
              out["execution_params"])
else:
    check("HRA001272 有 T1 fastq", False)

# ────────────────────────────────────────────────────────────────────
sec("S5 「用肝癌队列做生存分析」— 生存族 + 临床数据")
rows = q("MATCH (t:tool)-[:has_function]->(f:function) WHERE f.function CONTAINS '生存' RETURN DISTINCT t.tool_name")
surv = [r[0] for r in rows]
check("生存族工具可检索", bool(surv), surv)
rows = q("MATCH (i:individual)-[:in_study]->(s:study) WHERE toLower(s.tumor_type) CONTAINS 'liver' "
         "AND i.`13_survival_days` IS NOT NULL RETURN s.study_accession, count(i) ORDER BY count(i) DESC")
check("肝癌队列有生存数据（新列 13_survival_days）", bool(rows), rows)
print(f"    生存族工具: {surv} | 生存数据: {rows}")

# ────────────────────────────────────────────────────────────────────
sec("S6 契约与拒绝门指引在手册内（get_planning_guide）")
g = m.tool_get_planning_guide({})
check("手册可读", g["status"] == "ok")
for key in ("tool-chain/v2", "rejected", "resolve_sample_roles", "submittable", "绝不虚构"):
    check(f"手册含关键指引「{key}」", key in g["skill"])

# ────────────────────────────────────────────────────────────────────
sec("S7 「肝癌免疫浸润，用现成TPM」— TPM 优先")
rows = q("MATCH (n:T2) WHERE n.study_accession IN ['HRA001272','HRA006499','HRA001748','HRA001749'] "
         "AND n.file_name CONTAINS 'Genes' AND n.file_name CONTAINS 'TPM' RETURN n.study_accession, n.file_name")
check("肝癌队列存在 TPM 矩阵", bool(rows), rows)
if rows:
    out = m.tool_validate_execution_chain({"steps": [
        {"tool_id": "immune_infiltration_iobr", "inputs": {"expression_tsv": {"file_name": rows[0][1]}}}]})
    check("TPM 路径回填 + submittable", out["submittable"] is True,
          {"params": out["execution_params"], "missing": out["execution_params_missing"]})

# ────────────────────────────────────────────────────────────────────
sec("S8 「HRA001272的病人都多少岁？」— 隐私守卫（服务端确定性拒绝）")
r = m.tool_read_cypher({"query": "MATCH (i:individual) RETURN i.`01_age` LIMIT 5"})
check("直接取个体年龄 → 拒绝", r["status"] == "error" and "隐私" in r["detail"], r)
r = m.tool_read_cypher({"query": "MATCH (i:individual) WHERE i.`01_age` > 60 RETURN i.individual_accession LIMIT 5"})
check("按年龄筛个体（再识别）→ 拒绝", r["status"] == "error" and "隐私" in r["detail"], r)
r = m.tool_read_cypher({"query": "MATCH (i:individual) RETURN collect(i.`01_age`)"})
check("collect 打包导出 → 拒绝", r["status"] == "error" and "隐私" in r["detail"], r)
r = m.tool_read_cypher({"query": "MATCH (i:individual) RETURN i.`13_survival_days`, i.`01_gender` LIMIT 3"})
check("生存天数+性别个体值 → 拒绝", r["status"] == "error" and "隐私" in r["detail"], r)
r = m.tool_read_cypher({"query": "MATCH (i:individual) RETURN count(i.`01_age`), avg(toInteger(i.`01_age`))"})
check("聚合统计（count/avg，含嵌套转换）→ 放行", r["status"] == "ok", r)
r = m.tool_read_cypher({"query": "MATCH (i:individual)-[:in_study]->(s:study) "
                        "WHERE i.`13_survival_days` IS NOT NULL RETURN s.study_accession, count(i) LIMIT 10"})
check("存在性判断（IS NOT NULL）→ 放行", r["status"] == "ok", r)
r = m.tool_read_cypher({"query": "MATCH (n:T1) RETURN n.file_name"})
check("无 LIMIT 自动限流 ≤500", r["status"] == "ok" and len(r["rows"]) <= 500, len(r.get("rows", [])))
r = m.tool_read_cypher({"query": "MATCH (n) DETACH DELETE n"})
check("写入语句 → 拒绝（回归）", r["status"] == "error" and "只读" in r["detail"], r)
# 绕过尝试
for name, bypass in [
    ("整节点 RETURN i", "MATCH (i:individual) RETURN i LIMIT 3"),
    ("别名转手 WITH i AS x", "MATCH (i:individual) WITH i AS x RETURN x LIMIT 3"),
    ("properties() 导出", "MATCH (i:individual) RETURN properties(i) LIMIT 3"),
    ("keys()+动态下标", "MATCH (i:individual) UNWIND keys(i) AS k RETURN i[k] LIMIT 3"),
    ("collect(i) 打包", "MATCH (i:individual) RETURN collect(i)"),
]:
    r = m.tool_read_cypher({"query": bypass})
    check(f"绕过尝试「{name}」→ 拒绝", r["status"] == "error" and "隐私" in r["detail"], r)
# 合法工作负载回归：15 条官方模板必须全部通过守卫（不触发误杀）
import glob as _glob
tpl_dir = os.path.join(os.path.dirname(m.__file__), "skill", "references", "query_templates")
blocked = []
for f in sorted(_glob.glob(os.path.join(tpl_dir, "*.cypher"))):
    try:
        m._assert_read_only(open(f).read())
        m._assert_privacy(open(f).read())
    except ValueError as e:
        blocked.append((os.path.basename(f), str(e)[:60]))
check("15 条官方模板零误杀", not blocked, blocked)
# 配对发现查询（SKILL 配方3）零误杀
try:
    m._assert_privacy("MATCH (sp:sample)-[:in_individual]->(i:individual) "
                      "WITH sp.study_accession AS study, i, collect(DISTINCT sp.tissue_type) AS tts "
                      "WHERE 'Tumor' IN tts AND 'Normal' IN tts RETURN study, count(i) ORDER BY count(i) DESC")
    check("配对发现查询零误杀", True)
except ValueError as e:
    check("配对发现查询零误杀", False, str(e)[:100])

# ────────────────────────────────────────────────────────────────────
sec("S9 拒绝纪律指引（无关问题/隐私问询，调用方模型主路径）")
g = m.tool_get_planning_guide({})["skill"]
for key in ("拒绝纪律", "off_topic", "privacy", "隐私红线", "不要改写绕过"):
    check(f"手册含「{key}」", key in g)

# ────────────────────────────────────────────────────────────────────
sec("S10 接地校验 validate_plan — 防调用方模型用内部知识编造")
real_plan = json.load(open(os.path.join(os.path.dirname(m.__file__), "examples",
                                        "plan_go_enrichment_liver_v2.json")))
r = m.tool_validate_plan({"plan": real_plan})
check("真实示例 Plan → grounded=true", r.get("grounded") is True, r.get("violations"))
fake = json.loads(json.dumps(real_plan))
fake["recommendations"][0]["pipeline_id"] = "deseq2_seurat_magic"
fake["recommendations"][0]["tool"]["tool_id"] = "deseq2_seurat_magic"
r = m.tool_validate_plan({"plan": fake})
check("编造工具名（DESeq2 式内部知识）→ 打回", r.get("grounded") is False and
      any("闭集" in x or "编造" in x for x in r["violations"]), r.get("violations"))
fake = json.loads(json.dumps(real_plan))
fake["recommendations"][0]["data"]["assets"][0]["file_name"] = "HRA999999-Genes-TPM-1.0.tsv"
r = m.tool_validate_plan({"plan": fake})
check("编造文件名 → 打回", r.get("grounded") is False, r.get("violations"))
fake = json.loads(json.dumps(real_plan))
fake["recommendations"][0]["data"]["assets"][0]["file_path"] = "/fake/path/made/up.tsv"
r = m.tool_validate_plan({"plan": fake})
check("篡改文件路径 → 打回", r.get("grounded") is False, r.get("violations"))
fake = json.loads(json.dumps(real_plan))
fake["recommendations"][0]["data"]["study_accessions"] = ["HRA424242"]
r = m.tool_validate_plan({"plan": fake})
check("编造队列号 → 打回", r.get("grounded") is False, r.get("violations"))
r = m.tool_validate_plan({"plan": {"status": "rejected", "reason": "off_topic: 与生信分析无关"}})
check("rejected 单对象 → 放行", r.get("grounded") is True, r)
r = m.tool_validate_plan({"plan": json.dumps(real_plan, ensure_ascii=False)})
check("字符串形式入参 → 同样可验", r.get("grounded") is True, r.get("violations"))

sec("总结")
print("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项:\n  - " + "\n  - ".join(FAILS))
sys.exit(0 if not FAILS else 1)
