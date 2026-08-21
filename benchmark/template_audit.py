#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""template_audit.py — 逐条执行 skill/references/query_templates/ 下的官方 Cypher 模板。

为什么需要这个：模板是给调用方模型照抄的。图谱换版改了属性名（0819 把 `T1_id`
改成 `t1_id`）之后，模板语法仍然合法、Neo4j 也不报错，只是静默返回 0 行——模型
拿到空结果就会退回内部知识去猜。所以"能跑通"不够，必须断言**每条模板都返回行**。

用法: NEO4J_PASSWORD=... python3 benchmark/template_audit.py
"""
import glob
import importlib.util
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("m", os.path.join(ROOT, "mcp_light_server.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# 绑定参数必须用图里真实存在的值，否则 0 行是参数选错、不是模板坏了。
# tool_id 是 T001 这种编号（fastp 是 tool_name），modal 只有 6 个取值。
PARAMS = {
    "$study_accession": "'HRA001272'",
    "$modal": "'bulk_RNA'",
    "$format": "'RAW_PAIRED_END_R1_FASTQ'",
    "$tool_id": "'T001'",
    "$keyword": "'比对'",
    "$input_format": "'RAW_PAIRED_END_R1_FASTQ'",
    "$output_format": "'DNA_VARIANT_VCF_GENERAL'",
}

fails = []
tdir = os.path.join(ROOT, "skill", "references", "query_templates")
paths = sorted(glob.glob(os.path.join(tdir, "*.cypher")))
if not paths:
    print(f"FAIL 模板目录为空: {tdir}")
    sys.exit(1)

for path in paths:
    name = os.path.basename(path)
    body = "\n".join(l for l in open(path, encoding="utf-8") if not l.startswith("//"))
    query = body.strip().rstrip(";")
    if not query:
        continue
    # $t1_id/$t2_id 要挑**有对应边**的那种文件，否则 0 行是选样不巧、不是模板坏了：
    # 0821 实测 35,572 个 T2 里 4,259 个没有 generated_from 边（本就不是从 T1 算出来的），
    # 随手 LIMIT 1 有一成几率抽中它们，把好模板判成失效。T1 同理（45 个聚合文件无 in_sample）。
    for var, col in (("$t1_id", "MATCH (f:T1)-[:in_sample]->() RETURN f.t1_id LIMIT 1"),
                     ("$t2_id", "MATCH (f:T2)-[:generated_from]->(:T1) RETURN f.t2_id LIMIT 1")):
        if var in query:
            query = query.replace(var, "'%s'" % m.neo4j_q([col])[0][0][0])
    for var, val in PARAMS.items():
        query = query.replace(var, val)
    unbound = re.findall(r"\$\w+", query)
    if unbound:
        print(f"FAIL {name}: 未绑定参数 {unbound}（本脚本 PARAMS 需补）")
        fails.append(name)
        continue
    try:
        rows = m.neo4j_q([query])[0]
    except Exception as e:
        print(f"FAIL {name}: {str(e)[:150]}")
        fails.append(name)
        continue
    if rows:
        print(f"PASS {name}: rows={len(rows)}")
    else:
        print(f"FAIL {name}: 返回 0 行——属性名/标签可能已随图谱换版失效")
        fails.append(name)

print(f"\n{len(paths) - len(fails)}/{len(paths)} 条模板可用")
if fails:
    print("失效模板:", ", ".join(fails))
    sys.exit(1)
print("全部通过 ✅")
