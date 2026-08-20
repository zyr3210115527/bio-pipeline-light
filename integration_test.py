#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bio-pipeline-light v2.1 联测脚本（放在仓库根目录，与 mcp_light_server.py 同级）。

在能访问 Neo4j 的机器上运行：
  export NEO4J_URL=http://192.168.130.24:7480/db/neo4j/tx/commit
  export NEO4J_USER=neo4j NEO4J_PASSWORD=<密码>
  python3 integration_test.py

在 Neo4j 所在机器本机跑时，NEO4J_URL 换成本机 HTTP 端口（默认 7474，容器映射按实际改）。
退出码 0 = 全部通过。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_light_server as m  # noqa: E402

FAILS = []

def sec(t):
    print(f"\n=== {t} ===")

def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)

# ── 1. 连通性与图谱规模 ──
sec("1. health_check")
h = m.tool_health_check({})
check("neo4j 连通", h.get("status") == "ok", json.dumps(h, ensure_ascii=False)[:300])
if h.get("status") != "ok":
    print("\n无法连接 Neo4j，后续测试跳过。检查 NEO4J_URL/NEO4J_PASSWORD 与网络。")
    sys.exit(1)
print(f"  nodes={h['nodes']} tools={h['tools']} atomic_closed_set={len(h['atomic_closed_set'])}")

# ── 2. resolve_sample_roles study 模式依赖的图结构假设 ──
sec("2. 图结构假设：sample 自带 study_accession，等价于 study<-individual<-sample")
rows = m.neo4j_q([
    "MATCH (f:T1) WHERE f.study_accession IS NOT NULL RETURN count(f) AS c",
    "MATCH (:T1)-[:in_sample]->(:sample) RETURN count(*) AS c",
    "MATCH (sp:sample) WHERE sp.study_accession='HRA006117' RETURN count(DISTINCT sp) AS c",
    "MATCH (sp:sample)-[:in_individual]->(:individual)-[:in_study]->(st:study) "
    "WHERE st.study_accession='HRA006117' RETURN count(DISTINCT sp) AS c",
    "MATCH (sp:sample) WHERE NOT (sp)-[:in_individual]->() RETURN count(*) AS c"])
t1_with_study, edges = rows[0][0][0], rows[1][0][0]
direct, traversed, orphan = rows[2][0][0], rows[3][0][0], rows[4][0][0]
check("T1.study_accession", t1_with_study > 0, f"count={t1_with_study}")
check("(T1)-[:in_sample]->(sample)", edges > 0, f"count={edges}")
check("sample.study_accession 直查 == study<-individual<-sample 遍历",
      direct == traversed, f"direct={direct} traversed={traversed}")
check("无游离 sample（都挂到 individual）", orphan == 0, f"orphan={orphan}")

# ── 3. resolve_sample_roles ──
sec("3. resolve_sample_roles（study 模式：HRA000071 覆盖规则 / HRA001272 常规）")
for study in ("HRA000071", "HRA001272"):
    r = m.tool_resolve_sample_roles({"study": study})
    ok = r.get("status") == "ok" and r.get("sample_count", 0) > 0
    check(f"{study} 有样本且可判角色分布", ok,
          json.dumps({k: r.get(k) for k in ("status", "detail", "sample_count")}, ensure_ascii=False))
    if ok:
        print(f"  {study}: sample_roles={r['sample_roles']} role_resolved={r['role_resolved']}")
r71 = m.tool_resolve_sample_roles({"study": "HRA000071"})
if r71.get("status") == "ok" and r71.get("samples"):
    blood = [s for s in r71["samples"] if "blood" in str(s.get("specimen_type") or "").lower()]
    check("HRA000071 覆盖规则（Blood→normal）",
          bool(blood) and all(s["sample_role"] == "normal" for s in blood),
          f"blood_in_first200={len(blood)}")

# ── 3b. 回归：不得走文件路径数样本；run→sample 缺口必须如实报出 ──
sec("3b. 样本清单口径 + run→sample 缺口如实上报")
r6117 = m.tool_resolve_sample_roles({"study": "HRA006117"})
check("HRA006117 样本数按 sample 节点计（835，非文件路径的 570）",
      r6117.get("sample_count") == direct, f"got={r6117.get('sample_count')} want={direct}")
check("无文件的样本计入 unresolved 而非被丢弃",
      (r6117.get("sample_roles") or {}).get("unresolved", 0) > 0,
      json.dumps(r6117.get("sample_roles"), ensure_ascii=False))
r87 = m.tool_resolve_sample_roles({"study": "HRA000087"})
cov = r87.get("file_coverage") or {}
check("HRA000087 报出 run→sample 缺口", cov.get("runs_without_sample_node", 0) > 0,
      json.dumps(cov, ensure_ascii=False))
check("缺口队列附带解释性 note",
      any("run" in n and "sample" in n for n in (r87.get("notes") or [])),
      str(r87.get("notes"))[:160])
cov71 = (m.tool_resolve_sample_roles({"study": "HRA000071"}) or {}).get("file_coverage") or {}
check("HRA000071 无缺口（不误报）", cov71.get("runs_without_sample_node") == 0,
      json.dumps(cov71, ensure_ascii=False))

# ── 4. execution_params：file_name → 图内真实路径回填 ──
sec("4. validate_execution_chain：execution_params 查图回填 + submittable")
rows = m.neo4j_q(["MATCH (n:T2) WHERE n.file_name CONTAINS 'FPKM' AND n.file_path STARTS WITH '/' "
                  "RETURN n.file_name, n.file_path LIMIT 1"])
if rows and rows[0]:
    fname, fpath = rows[0][0][0], rows[0][0][1]
    out = m.tool_validate_execution_chain({"steps": [
        {"tool_id": "diff_expr_go",
         "inputs": {"expression_matrix": {"file_name": fname}}}]})  # 故意不带 file_path，逼它查图
    got = (out.get("execution_params") or {}).get("expression_matrix")
    check("file_name→file_path 回填", got == fpath, f"got={got} expect={fpath}")
    check("submittable=true", out.get("submittable") is True,
          f"missing={out.get('execution_params_missing')} errors={out['validation']['errors']}")
else:
    check("图内存在带真实路径的 FPKM T2 文件", False, "换个 CONTAINS 关键词再试")

# ── 5. 原子链校验（先从图里找一条真实 next_tool 边再验，避免误报） ──
sec("5. validate_atomic_chain（图内真实邻接）")
atomic = json.dumps(sorted(t.lower() for t in m.ATOMIC_IDS))
rows = m.neo4j_q([f"MATCH (a:tool)-[:next_tool]->(b:tool) "
                  f"WHERE toLower(a.tool_name) IN {atomic} AND toLower(b.tool_name) IN {atomic} "
                  f"RETURN a.tool_name, b.tool_name LIMIT 1"])
if rows and rows[0]:
    a, b = rows[0][0][0], rows[0][0][1]
    v = m.tool_validate_atomic_chain({"chain": [a, b]})
    check(f"{a} → {b}", v["status"] == "valid", str(v["violations"]))
else:
    check("图内存在 atomic next_tool 边", False)

sec("结果")
print("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项: {FAILS}")
sys.exit(0 if not FAILS else 1)
