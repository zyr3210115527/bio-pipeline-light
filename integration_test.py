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
# 0821 数据换代：sample_accession 直接写在 T1 上，in_sample 边照它建，run 不再是归属依据。
# 所以缺口口径从 runs_without_sample_node 换成 t1_files_unlinked——前者仍然很大
# （sample 节点每个只记一个 run，按 run 反查必然对不齐），但它已经不代表文件定位不到样本。
check("HRA000087 的 T1 文件几乎全部连上样本（换代后缺口已闭合）",
      cov.get("t1_files_linked_to_sample", 0) >= cov.get("t1_files", 1) - 5,
      json.dumps(cov, ensure_ascii=False))
check("runs_without_sample_node 降级为诊断字段，note 明说不要拿它判队列",
      any("不要拿这个数判断队列" in n for n in (r87.get("notes") or [])),
      str(r87.get("notes"))[:200])
cov71 = (m.tool_resolve_sample_roles({"study": "HRA000071"}) or {}).get("file_coverage") or {}
check("HRA000071 无缺口（不误报）", cov71.get("t1_files_unlinked", -1) <= 2,
      json.dumps(cov71, ensure_ascii=False))

# ── 3c. 回归：0821 数据质量修复（数值类型 / HRA016026 角色 / format 撞车） ──
sec("3c. 数据质量修复回归")
# 数值属性必须是数值型。存成 STRING 时 Cypher 走字典序比较，不报错但静默给错答案：
# 修前「生存>365 天」少算 355 人、「TMB>10」多算 783 人、data_level=1 查出 0 行、
# 队列按 sample_count 排序把 '81' 排在 '698' 前面。这几条断言就是防它复发。
types = m.neo4j_q([
    "MATCH (i:individual) WHERE i.`13_survival_days` IS NOT NULL "
    "RETURN head(collect(valueType(i.`13_survival_days`))), max(i.`13_survival_days`)",
    "MATCH (f:T1) WHERE f.data_level = 1 RETURN count(*)",
    "MATCH (s:study) WHERE s.sample_count IS NOT NULL "
    "RETURN s.study_accession, s.sample_count ORDER BY s.sample_count DESC LIMIT 1",
    "MATCH (i:individual) WHERE i.`11_tmb` > 10 RETURN count(*)"])
vt, maxsurv = types[0][0]
check("13_survival_days 是数值型（不是 STRING）", "STRING" not in str(vt), f"valueType={vt}")
# 存成 STRING 时 max() 是字典序（'995' 最大），float() 后照样 <1000，断言意图不变；
# 这里不能直接拿 str 和 int 比——0821 交付把这列退回 STRING，会当场 TypeError 崩掉，
# 后面十几段测试（含 execution_params 那段）就全都跑不到了。
try:
    _maxsurv = float(maxsurv)
except (TypeError, ValueError):
    _maxsurv = 0.0
check("生存天数最大值 > 1000（字符串比较会卡在 995）", _maxsurv > 1000, f"max={maxsurv!r}")
check("data_level = 1 用数字能查到文件（存 '1' 时返回 0 行）",
      types[1][0][0] > 20000, f"count={types[1][0][0]}")
check("study 按 sample_count 排序 top1 是最大队列 HRA000873",
      types[2][0][0] == "HRA000873", str(types[2][0]))
check("TMB>10 是数值比较的 102 人（字符串比较会给 885）",
      types[3][0][0] == 102, f"count={types[3][0][0]}")
# format 大小写撞车孤儿：小写变体零引用，留着会让「按格式查」得到 0 文件从而误判没数据
fmt = m.neo4j_q(["MATCH (f:format) WHERE NOT (f)--() AND "
                 "EXISTS { MATCH (g:format) WHERE g.format = toUpper(f.format) AND g <> f } "
                 "RETURN collect(f.format)"])[0][0][0]
check("无大小写撞车的 format 孤儿节点", not fmt, str(fmt))
# HRA016026：tissue_type 全是多值 'Tumor,Normal'，默认规则一个都判不出来，
# 靠 sample_name 后缀救回 350 对。这是图里第三大的可配对队列。
r16 = m.tool_resolve_sample_roles({"study": "HRA016026"})
check("HRA016026 角色可判（多值 tissue_type 不再拖垮整个队列）",
      r16.get("role_resolved") is True, json.dumps(r16.get("sample_roles"), ensure_ascii=False))
check("HRA016026 判成 350 tumor / 350 normal / 0 unresolved",
      r16.get("sample_roles") == {"tumor": 350, "normal": 350, "unresolved": 0},
      json.dumps(r16.get("sample_roles"), ensure_ascii=False))
# SKILL 的配对队列发现配方必须能看见它（旧写法 'Tumor' IN tts 会整个漏掉）
pair = m.neo4j_q([
    "MATCH (sp:sample)-[:in_individual]->(i:individual) "
    "WITH sp.study_accession AS study, i, "
    "collect(DISTINCT toLower(coalesce(sp.tissue_type,''))) AS tts, "
    "collect(DISTINCT toLower(coalesce(sp.sample_name,''))) AS nms "
    "WHERE (any(t IN tts WHERE t CONTAINS 'tumor') OR any(n IN nms WHERE n ENDS WITH '_tumor')) "
    "AND (any(t IN tts WHERE t CONTAINS 'normal') OR any(n IN nms WHERE n ENDS WITH '_normal')) "
    "RETURN study, count(i) AS c ORDER BY c DESC"])[0]
check("配对队列发现配方能看到 HRA016026（350 个体）",
      ["HRA016026", 350] in [list(x) for x in pair], str(pair[:5]))
# individual 的记账列按边重建过：HRA016026 那 349 行在 CSV 里整体错位一行
ind = m.neo4j_q([
    "MATCH (i:individual) WHERE i.`00_sample_accession` IS NOT NULL "
    "OPTIONAL MATCH (sp:sample)-[:in_individual]->(i) "
    "WITH i, collect(DISTINCT sp.sample_accession) AS real "
    "WHERE any(s IN split(i.`00_sample_accession`, ';') WHERE NOT s IN real) "
    "RETURN count(i)"])[0][0][0]
check("individual.00_sample_accession 与 in_individual 边一致（CSV 错位已修）",
      ind == 0, f"仍有 {ind} 个个体的记账列与边不符")

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

# ── 4b. 执行契约：三条「错得像对」的回包（纯逻辑，图查询 stub 成放行） ──
# 这几条以前全绿，因为老测试只覆盖了 card-less 分支（binding 是 dict 就当 File），
# 那一支根本不看 type 字段。断言一律落在「不能声称可提交」上，不只是字段值——
# 这类错的特征是回包字段齐全、值都合理、errors 为空，光看值判断不出严重性。
sec("4b. validate_execution_chain：执行契约回归（P0-1/P0-2/P0-3）")
_real_q = m.neo4j_q
m.neo4j_q = lambda stmts: [[[1]] for _ in stmts]      # 隔离纯逻辑，与图连不连得上无关
try:
    _f = lambda p: {"file_name": p.rsplit("/", 1)[-1], "file_path": p}

    # P0-1　fastqs 是 Array[File]+（WDL 非空数组）。三处类型判断都在拿字符串精确相等比
    # ("File","Array[File]")，带 + 后缀的全漏掉。fastqc 只有这一个输入，三处全漏等于这张卡
    # 在执行参数解析里完全不存在：回包 execution_params={} 且 missing=[]——零个参数、
    # 且一个都不缺，消费方按 not missing 判可提交，就把一个参数根本没解析出来的链当能跑的。
    r = m.tool_validate_execution_chain({"steps": [
        {"tool_id": "fastqc", "inputs": {"fastqs": _f("/d/a_R1.fq.gz")}}]})
    check("P0-1 Array[File]+ 必须被解析（不许 params 和 missing 同时为空）",
          bool(r["execution_params"]) and r["submittable"] is True,
          f"params={r['execution_params']} missing={r['execution_params_missing']}")
    check("P0-1 Array 参数的值是路径数组（消费方不能假定一定是 str）",
          isinstance(r["execution_params"].get("fastqs"), list), str(r["execution_params"]))

    # P0-2　read2 是 File?（可选）。老判据只收 required=true，于是**用户明确绑了的可选文件
    # 被丢掉**。后果比 P0-1 更隐蔽：执行端拿到只有 read1 的参数，trim_galore/STAR 里
    # is_paired = defined(read2) 变 false，**双端数据静默按单端跑完，一路绿灯，结果是错的**。
    r = m.tool_validate_execution_chain({"steps": [
        {"tool_id": "trim_galore", "inputs": {"read1": _f("/d/a_R1.fq.gz"),
                                              "read2": _f("/d/a_R2.fq.gz")}}]})
    check("P0-2 已绑定的可选 File? 必须进 execution_params（否则双端静默跑成单端）",
          r["execution_params"].get("read2") == "/d/a_R2.fq.gz", str(r["execution_params"]))

    # P0-3　这 5 个 File 参数是带卡片默认值的参考资源，既不该报缺、也不该被映射下发。
    # 报缺 → 一条完全正常的 RNA-seq 比对链被判不可提交；被映射 → 用户随手塞的路径
    # 覆盖掉容器内的正确默认值。
    r = m.tool_validate_execution_chain({"steps": [
        {"tool_id": "star", "inputs": {"read1": _f("/d/a_R1.fq.gz"), "read2": _f("/d/a_R2.fq.gz")}}]})
    check("P0-3 参考资源未绑定不算缺（正常 STAR 链必须可提交）",
          r["submittable"] is True,
          f"errors={r['validation']['errors']} missing={r['execution_params_missing']}")
    r = m.tool_validate_execution_chain({"steps": [
        {"tool_id": "star", "inputs": {"read1": _f("/d/a_R1.fq.gz"), "read2": _f("/d/a_R2.fq.gz"),
                                       "rrna_star_index": _f("/ref/rrna"),
                                       "genome_star_index": _f("/ref/genome")}}]})
    check("P0-3 参考资源即使被绑定也不许下发（会覆盖容器内默认值）",
          not ({"rrna_star_index", "genome_star_index"} & set(r["execution_params"])),
          str(r["execution_params"]))

    # P0-3 的坑：filtered_vcf_index 名字里带 index，却是**数据文件的伴随 .tbi**，没有卡片
    # 默认值，缺了 bcftools 的 ln -sf 读不到索引、执行直接失败。白名单必须是显式二元组，
    # 按 "index" 关键字猜就会把它一起豁免掉——重版踩过这个坑，0823 才修。
    r = m.tool_validate_execution_chain({"steps": [
        {"tool_id": "bcftools", "inputs": {"filtered_vcf": _f("/d/a.vcf.gz")}}]})
    check("P0-3 坑位 filtered_vcf_index 不是参考资源，缺了必须报错且不可提交",
          r["submittable"] is False and any("filtered_vcf_index" in e for e in r["validation"]["errors"]),
          f"errors={r['validation']['errors']} submittable={r['submittable']}")

    # P1-1/P1-2 契约形状：键必须与卡片参数名完全一致（不许 `tool.param` 点号拼接，
    # 否则同一字段在单步/多步下是两种键空间）；missing 是带 reason 的对象。
    r = m.tool_validate_execution_chain({"steps": [
        {"tool_id": "trim_galore", "inputs": {"read1": _f("/d/a_R1.fq.gz"), "read2": _f("/d/a_R2.fq.gz")}},
        {"tool_id": "fastqc",      "inputs": {"fastqs": _f("/d/a_val_1.fq.gz")}}]})
    check("P1-1 多步链的键不带 `tool.` 前缀，逐步参数在 execution_params_by_step",
          not any("." in k for k in r["execution_params"])
          and sum(len(s["params"]) for s in r["execution_params_by_step"]) == 3,
          json.dumps(r["execution_params_by_step"], ensure_ascii=False))
    r = m.tool_validate_execution_chain({"steps": [
        {"tool_id": "trim_galore", "inputs": {"read1": {"file_name": "查不到的文件.fq.gz"}}}]})
    check("P1-2 missing 是带 reason 的对象（下游要靠它分辨该找数据侧还是目录侧）",
          bool(r["execution_params_missing"])
          and r["execution_params_missing"][0].get("reason") == "no_confirmed_path",
          str(r["execution_params_missing"]))
    check("P1-2 schema_version 升到 v1.2", r["schema_version"] == "tool-chain-validation/v1.2",
          r["schema_version"])
finally:
    m.neo4j_q = _real_q

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
