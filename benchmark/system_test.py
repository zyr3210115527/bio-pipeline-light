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
# 行数上限绕过（0820 实测：以下五种写法都能拿到 27,000+ 行）。结果会原样进调用方模型的
# 上下文，超限即撑爆/被上游静默截断，模型拿半截数据当全集下结论。
for name, bypass in [
    ("UNION 前置分支不受尾部 LIMIT 约束",
     "MATCH (n:T1) RETURN n.file_name UNION MATCH (n:T2) RETURN n.file_name"),
    ("自报超大 LIMIT", "MATCH (n:T1) RETURN n.file_name LIMIT 99999"),
    ("超大 LIMIT 带分号", "MATCH (n:T1) RETURN n.file_name LIMIT 99999;"),
    ("注释里的 LIMIT 骗过检测", "// LIMIT 10\nMATCH (n:T1) RETURN n.file_name"),
    ("行尾注释吞掉追加的 LIMIT", "MATCH (n:T1) RETURN n.file_name // all"),
    ("块注释里的 LIMIT", "/* LIMIT 3 */ MATCH (n:T1) RETURN n.file_name"),
]:
    r = m.tool_read_cypher({"query": bypass})
    check(f"限流绕过「{name}」→ 截断至 ≤500",
          r["status"] == "ok" and len(r["rows"]) <= 500 and r.get("row_count") == len(r["rows"]),
          {"n": len(r.get("rows", [])), "detail": r.get("detail", "")[:80]})
r = m.tool_read_cypher({"query": "MATCH (n:T1) RETURN n.file_name UNION MATCH (n:T2) RETURN n.file_name"})
check("截断时如实上报 truncated + note", r.get("truncated") is True and "截断" in (r.get("note") or ""), r.get("note"))
# 限流加固不能误伤正常查询（注释/字符串扫描写错会把合法语句改坏）
for name, ok_q, want in [
    ("小 LIMIT 原样保留", "MATCH (n:T1) RETURN n.file_name LIMIT 3", 3),
    ("ORDER BY + LIMIT", "MATCH (s:study) RETURN s.study_accession ORDER BY s.study_accession LIMIT 5", 5),
    ("子查询内 LIMIT 不改写",
     "MATCH (s:study) CALL { WITH s MATCH (n:T1) WHERE n.study_accession = s.study_accession "
     "RETURN count(n) AS c } RETURN s.study_accession, c LIMIT 4", 4),
    ("字符串字面量含 LIMIT 字样",
     "MATCH (n:T1) WHERE n.file_name CONTAINS 'LIMIT 5' RETURN n.file_name LIMIT 2", 0),
]:
    r = m.tool_read_cypher({"query": ok_q})
    check(f"限流零误伤「{name}」", r["status"] == "ok" and len(r["rows"]) == want,
          {"n": len(r.get("rows", [])), "detail": r.get("detail", "")[:80]})
r = m.tool_read_cypher({"query": "MATCH (n) DETACH DELETE n"})
check("写入语句 → 拒绝（回归）", r["status"] == "error" and "只读" in r["detail"], r)
# 绕过尝试。前 5 条是查询面守卫认得的写法；后 6 条换等价写法绕开变量识别，
# 靠结果面守卫 _assert_no_sensitive_payload 兜底——查询面正则永远补不全，
# 所以真正的防线是"返回内容里出现患者级属性就整条拒绝"。
for name, bypass in [
    ("整节点 RETURN i", "MATCH (i:individual) RETURN i LIMIT 3"),
    ("别名转手 WITH i AS x", "MATCH (i:individual) WITH i AS x RETURN x LIMIT 3"),
    ("properties() 导出", "MATCH (i:individual) RETURN properties(i) LIMIT 3"),
    ("keys()+动态下标", "MATCH (i:individual) UNWIND keys(i) AS k RETURN i[k] LIMIT 3"),
    ("collect(i) 打包", "MATCH (i:individual) RETURN collect(i)"),
    ("无标签 pattern 取 individual",
     "MATCH (s:sample)-[:in_individual]->(x) RETURN x LIMIT 1"),
    ("WHERE 标签谓词绕开 inline label", "MATCH (n) WHERE n:individual RETURN n LIMIT 1"),
    ("collect 打包后经 WITH 转手",
     "MATCH (i:individual) WITH collect(i) AS c RETURN c LIMIT 1"),
    ("map projection i{.*}", "MATCH (i:individual) RETURN i{.*} LIMIT 1"),
    ("map projection 经 WITH", "MATCH (i:individual) WITH i{.*} AS mm RETURN mm LIMIT 1"),
    ("两级别名 i→z→y", "MATCH (i:individual) WITH i AS z WITH z AS y RETURN y LIMIT 1"),
    ("无标签变量 + UNWIND keys 把属性名当值返回",
     "MATCH (s:sample)-[:in_individual]->(x) UNWIND keys(x) AS k RETURN k LIMIT 30"),
    ("无标签变量 + 动态下标取值",
     "MATCH (s:sample)-[:in_individual]->(x) RETURN x['01_age'] LIMIT 3"),
]:
    r = m.tool_read_cypher({"query": bypass})
    check(f"绕过尝试「{name}」→ 拒绝", r["status"] == "error" and "隐私" in r["detail"], r)
# LOAD CSV / apoc 不是写入语句，但能读本地文件、外带数据，必须一并挡在只读守卫里
for name, q in [
    ("LOAD CSV 读本地文件", "LOAD CSV FROM 'file:///etc/passwd' AS r RETURN r LIMIT 1"),
    ("LOAD CSV 外带(SSRF)", "LOAD CSV FROM 'http://127.0.0.1:7474/' AS r RETURN r LIMIT 1"),
    ("apoc.export 导出全图", "CALL apoc.export.json.all(null,{stream:true}) YIELD data RETURN data"),
]:
    r = m.tool_read_cypher({"query": q})
    check(f"{name} → 拒绝", r["status"] == "error" and "只读" in r["detail"], r)
# 敏感前缀覆盖范围：0821 实测漏了 02/04/10/12 四类，能直接查出个体级治疗方案、
# 脉管侵犯、家族史（"HRI264436 → 3+7 regimen"）。改为按前缀区间覆盖 01_–13_，
# 只放行 00_（操作性标识）。这几条防的是"上游一加编号列就又漏一类"。
for name, q in [
    ("12_ 治疗史（个体级方案）",
     "MATCH (i:individual) RETURN i.individual_accession, "
     "i.`12_treatment_regimens_for_non_surgical_patients` LIMIT 3"),
    ("04_ 血液学指标", "MATCH (i:individual) RETURN i.`04_hemoglobin_concentration_g_l` LIMIT 3"),
    ("02_ 家族史", "MATCH (i:individual) RETURN i.`02_family_history` LIMIT 3"),
    ("10_ 脉管侵犯", "MATCH (i:individual) RETURN i.`10_vessel_invasion` LIMIT 3"),
]:
    r = m.tool_read_cypher({"query": q})
    check(f"敏感前缀「{name}」→ 拒绝", r["status"] == "error" and "隐私" in r["detail"], r)
for name, q in [
    ("00_ 操作性标识放行",
     "MATCH (i:individual) WHERE i.`00_sample_accession` IS NOT NULL RETURN count(*)"),
    ("12_ 存在性判断放行", "MATCH (i:individual) WHERE i.`12_surgery` IS NOT NULL RETURN count(*)"),
    # 属性名里嵌了数字（..._109_l）不能被当成 09_ 病理属性误杀：前缀必须落在名字开头
    ("04_platelet_count_109_l 聚合放行", "MATCH (i:individual) RETURN avg(i.`04_platelet_count_109_l`)"),
]:
    r = m.tool_read_cypher({"query": q})
    check(f"敏感前缀零误伤「{name}」", r["status"] == "ok", r)
# 合法工作负载不能被上面的加固误杀
for name, q in [
    ("count(节点)", "MATCH (i:individual) RETURN count(i) AS n"),
    ("count 别名后再 RETURN", "MATCH (i:individual) WITH count(i) AS n RETURN n"),
    ("点取非临床字段带别名", "MATCH (i:individual) RETURN i.individual_accession AS acc LIMIT 3"),
    ("点取+聚合混合投影",
     "MATCH (i:individual) RETURN i.individual_accession AS acc, count(i) AS c LIMIT 3"),
    ("生存队列发现（存在性+聚合）",
     "MATCH (i:individual)-[:in_study]->(st:study) WHERE i.`13_survival_days` IS NOT NULL "
     "RETURN st.study_accession AS s, count(*) AS n ORDER BY n DESC LIMIT 5"),
]:
    r = m.tool_read_cypher({"query": q})
    check(f"合法查询「{name}」→ 放行", r["status"] == "ok", r)
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
sec("S8b 工具入参 Cypher 注入（tool_id 直接进字面量的路径）")
def _flow_stage(steps):
    r = m.tool_validate_execution_chain({"steps": steps})
    return next((s for s in r["stages"] if s["stage"] == "chain_flow"), {})
# 这条路径不经过 _assert_read_only：注入既能跑任意 Cypher（写操作可达），
# 又能把图里不存在的邻接伪造成 passed=True，等于架空提交前把关。
for name, payload in [
    ("闭合引号 + UNION 伪造邻接", "zzz' RETURN 1 AS c UNION MATCH (n:study) RETURN 1 AS c //"),
    ("闭合引号 + 注释截断", "zzz' RETURN 1 AS c //"),
    ("恒真条件", "zzz' OR '1'='1"),
]:
    st = _flow_stage([{"tool_id": payload, "inputs": {}}, {"tool_id": "star", "inputs": {}}])
    check(f"注入「{name}」→ 不得判定通过", st.get("passed") is False, st)
for name, chain in [
    ("fastp→star", ["fastp", "star"]),
    ("四步原子链", ["fastp", "star", "samtools", "featurecounts"]),
]:
    st = _flow_stage([{"tool_id": t, "inputs": {}} for t in chain])
    check(f"真实链「{name}」→ 仍judged通过（零误伤）", st.get("passed") is True, st)
r = m.tool_resolve_sample_roles({"study": "x' RETURN 1 AS a UNION MATCH (i:individual) RETURN i.`01_age` AS a //"})
check("resolve_sample_roles 注入 → 拒绝", r.get("status") == "error", r)

# ────────────────────────────────────────────────────────────────────
sec("S8c resolve_sample_roles 返回体瘦身（返回体过大 → 调用方截断成非法 JSON）")
# 0820 实测：samples 上限写死 200 条时，HRA001272 返回体 51,782 字符，调用方 harness 按
# 12,000 字符截断后是**非法 JSON**，模型收到一段砍断的记录且毫无提示。这是 read_cypher
# 行数上限同一类问题的漏网。判定口径：既要小，也要如实说自己被截了。
r = m.tool_resolve_sample_roles({"study": "HRA001272"})
size = len(json.dumps(r, ensure_ascii=False))
check("默认返回体 < 12000 字符（调用方不会截断）", size < 12000, f"{size} 字符")
check("默认预览条数 = SAMPLE_PREVIEW", len(r["samples"]) == m.SAMPLE_PREVIEW, len(r["samples"]))
check("截断如实上报 samples_truncated", r.get("samples_truncated") is True, r.get("samples_shown"))
check("截断说明进 notes", any("预览" in n and "全集" in n for n in r["notes"]), r["notes"])
# 统计口径不能跟着预览一起缩水——sample_roles 必须覆盖全部样本，否则模型会低估队列规模
check("sample_roles 仍覆盖全部样本（不随预览缩水）",
      sum(r["sample_roles"].values()) == r["sample_count"] > len(r["samples"]),
      {"roles": r["sample_roles"], "total": r["sample_count"]})
check("role_resolved / file_coverage 未受影响",
      r["role_resolved"] is True and r["file_coverage"]["runs_without_sample_node"] >= 0,
      r["file_coverage"])
r2 = m.tool_resolve_sample_roles({"study": "HRA001272", "sample_limit": 200})
check("显式 sample_limit=200 仍可取成批明细", len(r2["samples"]) == 200, len(r2["samples"]))
r3 = m.tool_resolve_sample_roles({"study": "HRA001272", "sample_limit": 99999})
check("sample_limit 超上限被夹到 SAMPLE_LIMIT_MAX", len(r3["samples"]) == m.SAMPLE_LIMIT_MAX, len(r3["samples"]))
r4 = m.tool_resolve_sample_roles({"study": "HRA001272", "sample_limit": "abc"})
check("sample_limit 非法值 → 回落默认值而非报错",
      r4["status"] == "ok" and len(r4["samples"]) == m.SAMPLE_PREVIEW, r4.get("detail"))
r5 = m.tool_resolve_sample_roles({"study": "HRA000071"})
check("小队列不误报 truncated", ("samples_truncated" in r5) == (r5["sample_count"] > len(r5["samples"])),
      {"total": r5["sample_count"], "shown": len(r5["samples"])})

# ────────────────────────────────────────────────────────────────────
sec("S9 拒绝纪律指引（无关问题/隐私问询，调用方模型主路径）")
g = m.tool_get_planning_guide({})["skill"]
for key in ("Rejection discipline", "off_topic", "privacy", "Privacy red line", "bypass"):
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
