#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""不走 cypher-shell，纯 HTTP 把 import/ 重建成整个图谱。

    NEO4J_URL=http://host:7480/db/neo4j/tx/commit NEO4J_USER=neo4j NEO4J_PASSWORD=... \
    python3 scripts/load_graph_http.py /path/to/import

`load_graph.sh` 是首选——但它要 cypher-shell，而 `LOAD CSV FROM 'file:///'` 是在
**Neo4j 那台机器**上解析路径的，所以那条路要求你能登上图谱服务器、能往它的 import
目录里放文件。现网那台（192.168.130.24）SSH 不开，只有 HTTP 端口，于是有了这个。

`/tx/commit` 不支持 `CALL {} IN TRANSACTIONS`，所以批处理改在客户端做：CSV 在本地读
成行，按批走 UNWIND + 参数发过去。语义与 scripts/load_graph.cypher 逐条对齐：

  · 空串不落属性（那边是 `coalesce(row[k],'') <> ''`，这边在 Python 里就滤掉了）
  · study/project 的 individual_count / sample_count 转整数
  · individual 按 accession MERGE，重复行后写覆盖先写（批内按序、批间顺序发）
  · tool 的列名重命名照搬（downstream_tools→next_tool、applicable_omics→modal）

灌完自己校验节点/关系总数。**这两个数是从 CSV 行数推出来的，不是抄来的**：每条关系
的两端都必须 MATCH 得上，少一条就说明有孤儿行，会直接报出来。
"""

import csv
import json
import os
import sys
import time
import urllib.request

BATCH = 5000
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _cfg():
    url = os.environ.get("NEO4J_URL", "http://127.0.0.1:7474/db/neo4j/tx/commit")
    user = os.environ.get("NEO4J_USER", "neo4j")
    pw = os.environ.get("NEO4J_PASSWORD")
    if not pw:
        sys.exit("需要 NEO4J_PASSWORD")
    import base64
    return url, "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


URL, AUTH = _cfg()
ROOT = sys.argv[1] if len(sys.argv) > 1 else "import"


def run(stmt, params=None):
    body = {"statements": [{"statement": stmt, "parameters": params or {}}]}
    req = urllib.request.Request(URL, json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": AUTH})
    d = json.load(OPENER.open(req, timeout=300))
    if d.get("errors"):
        raise SystemExit("Neo4j 报错：" + json.dumps(d["errors"], ensure_ascii=False)
                         + "\n语句：" + stmt[:200])
    res = d["results"][0]["data"]
    return res[0]["row"][0] if res and res[0]["row"] else None


def rows(rel_path, ints=()):
    """读 CSV，空串丢掉（等价于图里不落该属性），指定列转整数。"""
    out = []
    with open(os.path.join(ROOT, rel_path), encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            d = {k: v for k, v in r.items() if k and v not in (None, "")}
            for k in ints:
                if k in d:
                    d[k] = int(d[k])
            out.append(d)
    return out


def send(label, data, stmt, key="rows"):
    t = time.time()
    for i in range(0, len(data), BATCH):
        run(stmt, {key: data[i:i + BATCH]})
    print(f"  {label:<32} {len(data):>7} 行  {time.time() - t:5.1f}s", flush=True)
    return len(data)


# ---------- 0. 清空 ----------
print("==> 清空目标库（原图不可恢复，确认过再跑）…", flush=True)
t0 = time.time()
while True:
    n = run("MATCH (n) WITH n LIMIT 10000 DETACH DELETE n RETURN count(n)")
    if not n:
        break
print(f"  已清空  {time.time() - t0:.1f}s", flush=True)

# ---------- 1. 约束与索引 ----------
print("==> 建约束/索引…", flush=True)
for s in [
    "CREATE CONSTRAINT study_key IF NOT EXISTS FOR (s:study) REQUIRE s.study_accession IS UNIQUE",
    "CREATE CONSTRAINT project_key IF NOT EXISTS FOR (p:project) REQUIRE p.project_accession IS UNIQUE",
    "CREATE CONSTRAINT sample_key IF NOT EXISTS FOR (s:sample) REQUIRE s.sample_accession IS UNIQUE",
    "CREATE CONSTRAINT individual_key IF NOT EXISTS FOR (i:individual) REQUIRE i.`00_individual_accession` IS UNIQUE",
    "CREATE CONSTRAINT t1_key IF NOT EXISTS FOR (t:T1) REQUIRE t.t1_id IS UNIQUE",
    "CREATE CONSTRAINT t2_key IF NOT EXISTS FOR (t:T2) REQUIRE t.t2_id IS UNIQUE",
    "CREATE CONSTRAINT tool_key IF NOT EXISTS FOR (t:tool) REQUIRE t.tool_id IS UNIQUE",
    "CREATE CONSTRAINT format_key IF NOT EXISTS FOR (f:format) REQUIRE f.format IS UNIQUE",
    "CREATE CONSTRAINT function_key IF NOT EXISTS FOR (f:function) REQUIRE f.function IS UNIQUE",
    "CREATE CONSTRAINT modal_key IF NOT EXISTS FOR (m:modal) REQUIRE m.modal IS UNIQUE",
    "CREATE CONSTRAINT datalevel_key IF NOT EXISTS FOR (d:datalevel) REQUIRE d.level IS UNIQUE",
    "CREATE INDEX t1_study IF NOT EXISTS FOR (t:T1) ON (t.study_accession)",
    "CREATE INDEX t2_study IF NOT EXISTS FOR (t:T2) ON (t.study_accession)",
    "CREATE INDEX t1_run IF NOT EXISTS FOR (t:T1) ON (t.run_accession)",
    "CREATE INDEX t2_run IF NOT EXISTS FOR (t:T2) ON (t.run_accession)",
    "CREATE INDEX t1_file IF NOT EXISTS FOR (t:T1) ON (t.file_name)",
    "CREATE INDEX t2_file IF NOT EXISTS FOR (t:T2) ON (t.file_name)",
    "CREATE INDEX t1_sem IF NOT EXISTS FOR (t:T1) ON (t.semantic_format)",
    "CREATE INDEX t2_sem IF NOT EXISTS FOR (t:T2) ON (t.semantic_format)",
    "CREATE INDEX sample_study IF NOT EXISTS FOR (s:sample) ON (s.study_accession)",
    "CREATE INDEX individual_study IF NOT EXISTS FOR (i:individual) ON (i.`00_study_accession`)",
    "CREATE INDEX tool_name IF NOT EXISTS FOR (t:tool) ON (t.tool_name)",
]:
    run(s)

# ---------- 2. 参考表 ----------
print("==> 参考表…", flush=True)
N = 0
for f, label, stmt in [
    ("reference/formats.csv", "format",
     "UNWIND $rows AS row CREATE (:format {format: row.`语义格式`, description: row.description})"),
    ("reference/function.csv", "function",
     "UNWIND $rows AS row CREATE (:function {function: row.function, description: row.description})"),
    ("reference/multimodal.csv", "modal",
     "UNWIND $rows AS row CREATE (:modal {modal: row.modal, description: row.description})"),
    ("reference/data_level.csv", "datalevel",
     "UNWIND $rows AS row CREATE (:datalevel {level: row.level, name: row.name, description: row.description})"),
]:
    N += send(label, rows(f), stmt)

# ---------- 3. 实体 ----------
print("==> 实体…", flush=True)
N += send("study", rows("entities/study.csv", ints=("individual_count", "sample_count")),
          "UNWIND $rows AS row CREATE (n:study) SET n = row")
N += send("project", rows("entities/project.csv", ints=("individual_count",)),
          "UNWIND $rows AS row CREATE (n:project) SET n = row")

# individual 唯一需要去重：同一患者进了两个研究，按 accession MERGE，后写覆盖先写
ind = rows("entities/individual.csv")
t = time.time()
for i in range(0, len(ind), 2000):
    run("UNWIND $rows AS row "
        "MERGE (n:individual {`00_individual_accession`: row.`00_individual_accession`}) "
        "SET n += row", {"rows": ind[i:i + 2000]})
uniq = run("MATCH (n:individual) RETURN count(n)")
print(f"  {'individual':<32} {len(ind):>7} 行 → {uniq} 个节点  {time.time() - t:5.1f}s", flush=True)
N += uniq

N += send("sample", rows("entities/sample.csv"),
          "UNWIND $rows AS row CREATE (n:sample) SET n = row")
N += send("T1", rows("entities/T1.csv"),
          "UNWIND $rows AS row CREATE (n:T1) SET n = row")
N += send("T2", rows("entities/T2.csv"),
          "UNWIND $rows AS row CREATE (n:T2) SET n = row")
# tool 的属性名与 CSV 列名不一致（0821 起交付改了列名，图里的属性名保持既有那套）
N += send("tool", rows("entities/tool.csv"),
          "UNWIND $rows AS row CREATE (:tool {tool_id: row.tool_id, tool_name: row.tool_name, "
          "function: row.function, semantic_input: row.semantic_input, "
          "input_format: row.input_format, semantic_output: row.semantic_output, "
          "output_format: row.output_format, next_tool: row.downstream_tools, "
          "modal: row.applicable_omics})")

# ---------- 4. 关系 ----------
print("==> 关系…", flush=True)
R = 0
REL = [
    ("reference/format_subclass.csv", "subclass_of",
     "MATCH (a:format {format: row.child}), (b:format {format: row.parent}) CREATE (a)-[:subclass_of]->(b)"),
    ("relations/study_in_project.csv", "study→project",
     "MATCH (a:study {study_accession: row.study_accession}), (b:project {project_accession: row.project_accession}) CREATE (a)-[:in_project]->(b)"),
    ("relations/individual_in_study.csv", "individual→study",
     "MATCH (a:individual {`00_individual_accession`: row.individual_accession}), (b:study {study_accession: row.study_accession}) CREATE (a)-[:in_study]->(b)"),
    ("relations/sample_in_individual.csv", "sample→individual",
     "MATCH (a:sample {sample_accession: row.sample_accession}), (b:individual {`00_individual_accession`: row.individual_accession}) CREATE (a)-[:in_individual]->(b)"),
    ("relations/T1_in_study.csv", "T1→study",
     "MATCH (a:T1 {t1_id: row.t1_id}), (b:study {study_accession: row.study_accession}) CREATE (a)-[:in_study]->(b)"),
    ("relations/T1_in_sample.csv", "T1→sample",
     "MATCH (a:T1 {t1_id: row.t1_id}), (b:sample {sample_accession: row.sample_accession}) CREATE (a)-[:in_sample]->(b)"),
    ("relations/T1_in_format.csv", "T1→format",
     "MATCH (a:T1 {t1_id: row.t1_id}), (b:format {format: row.semantic_format}) CREATE (a)-[:in_format]->(b)"),
    ("relations/T1_in_level.csv", "T1→level",
     "MATCH (a:T1 {t1_id: row.t1_id}), (b:datalevel {level: row.data_level}) CREATE (a)-[:in_level]->(b)"),
    ("relations/T1_in_modal.csv", "T1→modal",
     "MATCH (a:T1 {t1_id: row.t1_id}), (b:modal {modal: row.modal}) CREATE (a)-[:in_modal]->(b)"),
    ("relations/T2_in_study.csv", "T2→study",
     "MATCH (a:T2 {t2_id: row.t2_id}), (b:study {study_accession: row.study_accession}) CREATE (a)-[:in_study]->(b)"),
    ("relations/T2_in_format.csv", "T2→format",
     "MATCH (a:T2 {t2_id: row.t2_id}), (b:format {format: row.semantic_format}) CREATE (a)-[:in_format]->(b)"),
    ("relations/T2_in_level.csv", "T2→level",
     "MATCH (a:T2 {t2_id: row.t2_id}), (b:datalevel {level: row.data_level}) CREATE (a)-[:in_level]->(b)"),
    ("relations/T2_in_modal.csv", "T2→modal",
     "MATCH (a:T2 {t2_id: row.t2_id}), (b:modal {modal: row.modal}) CREATE (a)-[:in_modal]->(b)"),
    ("relations/T2_generated_from_T1.csv", "T2→T1",
     "MATCH (a:T2 {t2_id: row.t2_id}), (b:T1 {t1_id: row.t1_id}) CREATE (a)-[:generated_from]->(b)"),
    ("relations/tool_has_function.csv", "tool→function",
     "MATCH (a:tool {tool_id: row.tool_id}), (b:function {function: row.function}) CREATE (a)-[:has_function]->(b)"),
    ("relations/tool_has_semantic_input.csv", "tool→input",
     "MATCH (a:tool {tool_id: row.tool_id}), (b:format {format: row.format}) CREATE (a)-[:input]->(b)"),
    ("relations/tool_has_semantic_output.csv", "tool→output",
     "MATCH (a:tool {tool_id: row.tool_id}), (b:format {format: row.format}) CREATE (a)-[:output]->(b)"),
    ("relations/tool_relationship.csv", "tool→next_tool",
     "MATCH (a:tool {tool_id: row.tool_id}), (b:tool {tool_id: row.next_tool_id}) CREATE (a)-[:next_tool {kind: row.kind}]->(b)"),
    ("relations/tool_suitable_for_modal.csv", "tool→modal",
     "MATCH (a:tool {tool_id: row.tool_id}), (b:modal {modal: row.modal}) CREATE (a)-[:suitable_for]->(b)"),
]
for f, label, body in REL:
    R += send(label, rows(f), "UNWIND $rows AS row " + body)

# ---------- 5. 校验 ----------
print("\n==> 校验（期望值来自上面各 CSV 的行数，不是写死的）…", flush=True)
got_n = run("MATCH (n) RETURN count(n)")
got_r = run("MATCH ()-[r]->() RETURN count(r)")
bad = 0
for name, got, exp in [("节点", got_n, N), ("关系", got_r, R)]:
    ok = got == exp
    bad += not ok
    print(f"  {'✓' if ok else '✗'} {name:<6}{got:>8}  (期望 {exp})")
if got_r != R:
    print("  关系少了 → 有 CSV 行的两端在图里 MATCH 不上（孤儿引用），别当成加载中断。")
for lbl in ["study", "project", "individual", "sample", "T1", "T2", "tool",
            "format", "function", "modal", "datalevel"]:
    print(f"    {lbl:<12}{run(f'MATCH (n:`{lbl}`) RETURN count(n)'):>8}")
sys.exit(1 if bad else 0)
