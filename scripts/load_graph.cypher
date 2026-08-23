// 从 import/ 下的 CSV 重建整个知识图谱。
//
//   JAVA_HOME=<jdk> bin/cypher-shell -u neo4j -p <pw> --file scripts/load_graph.cypher
//
// 约定：**CSV 列名原样进图，不做重命名**。手册与 mcp_light_server 的隐私守卫都是照
// 0821 交付的列名写的（individual 的 `00_`–`13_` 编号前缀、`t1_id`/`t2_id` 小写），
// 改名反而会把图谱和代码/手册拆开。空字符串一律不落属性（等价于 null），与既有图一致。
// 只有 study/project 的 individual_count / sample_count 转成整数——手册教模型拿它做数值
// 比较，字符串会静默比错。

// ---------- 0. 清空 ----------
MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS;

// ---------- 1. 约束（先建索引，否则后面按 key 连边是全表扫）----------
CREATE CONSTRAINT study_key IF NOT EXISTS FOR (s:study) REQUIRE s.study_accession IS UNIQUE;
CREATE CONSTRAINT project_key IF NOT EXISTS FOR (p:project) REQUIRE p.project_accession IS UNIQUE;
CREATE CONSTRAINT sample_key IF NOT EXISTS FOR (s:sample) REQUIRE s.sample_accession IS UNIQUE;
CREATE CONSTRAINT individual_key IF NOT EXISTS FOR (i:individual) REQUIRE i.`00_individual_accession` IS UNIQUE;
CREATE CONSTRAINT t1_key IF NOT EXISTS FOR (t:T1) REQUIRE t.t1_id IS UNIQUE;
CREATE CONSTRAINT t2_key IF NOT EXISTS FOR (t:T2) REQUIRE t.t2_id IS UNIQUE;
CREATE CONSTRAINT tool_key IF NOT EXISTS FOR (t:tool) REQUIRE t.tool_id IS UNIQUE;
CREATE CONSTRAINT format_key IF NOT EXISTS FOR (f:format) REQUIRE f.format IS UNIQUE;
CREATE CONSTRAINT function_key IF NOT EXISTS FOR (f:function) REQUIRE f.function IS UNIQUE;
CREATE CONSTRAINT modal_key IF NOT EXISTS FOR (m:modal) REQUIRE m.modal IS UNIQUE;
CREATE CONSTRAINT datalevel_key IF NOT EXISTS FOR (d:datalevel) REQUIRE d.level IS UNIQUE;
// 连边和查询都按这些属性过滤，没索引时 T1/T2 那两条 3 万行的关系文件会跑成分钟级
CREATE INDEX t1_study IF NOT EXISTS FOR (t:T1) ON (t.study_accession);
CREATE INDEX t2_study IF NOT EXISTS FOR (t:T2) ON (t.study_accession);
CREATE INDEX t1_run IF NOT EXISTS FOR (t:T1) ON (t.run_accession);
CREATE INDEX t2_run IF NOT EXISTS FOR (t:T2) ON (t.run_accession);
CREATE INDEX t1_file IF NOT EXISTS FOR (t:T1) ON (t.file_name);
CREATE INDEX t2_file IF NOT EXISTS FOR (t:T2) ON (t.file_name);
CREATE INDEX t1_sem IF NOT EXISTS FOR (t:T1) ON (t.semantic_format);
CREATE INDEX t2_sem IF NOT EXISTS FOR (t:T2) ON (t.semantic_format);
CREATE INDEX sample_study IF NOT EXISTS FOR (s:sample) ON (s.study_accession);
CREATE INDEX individual_study IF NOT EXISTS FOR (i:individual) ON (i.`00_study_accession`);
CREATE INDEX tool_name IF NOT EXISTS FOR (t:tool) ON (t.tool_name);

// ---------- 2. 参考表 ----------
LOAD CSV WITH HEADERS FROM 'file:///reference/formats.csv' AS row
CREATE (:format {format: row.`语义格式`, description: row.description});

LOAD CSV WITH HEADERS FROM 'file:///reference/function.csv' AS row
CREATE (:function {function: row.function, description: row.description});

LOAD CSV WITH HEADERS FROM 'file:///reference/multimodal.csv' AS row
CREATE (:modal {modal: row.modal, description: row.description});

LOAD CSV WITH HEADERS FROM 'file:///reference/data_level.csv' AS row
CREATE (:datalevel {level: row.level, name: row.name, description: row.description});

// ---------- 3. 实体 ----------
// 逐 key 动态 SET 而不是 `SET n = row`：空串要当成缺失，不能落成 ''（既有图就是这样，
// 例如只有 1234 个 T1 有 size）。手册教模型用 `IS NOT NULL` 判存在，落了空串就全是假阳性。
LOAD CSV WITH HEADERS FROM 'file:///entities/study.csv' AS row
CALL (row) {
  CREATE (n:study)
  WITH n, row, [k IN keys(row) WHERE coalesce(row[k], '') <> ''] AS ks
  UNWIND ks AS k
  SET n[k] = row[k]
} IN TRANSACTIONS OF 5000 ROWS;
MATCH (s:study) WHERE s.individual_count IS NOT NULL
SET s.individual_count = toInteger(s.individual_count);
MATCH (s:study) WHERE s.sample_count IS NOT NULL
SET s.sample_count = toInteger(s.sample_count);

LOAD CSV WITH HEADERS FROM 'file:///entities/project.csv' AS row
CALL (row) {
  CREATE (n:project)
  WITH n, row, [k IN keys(row) WHERE coalesce(row[k], '') <> ''] AS ks
  UNWIND ks AS k
  SET n[k] = row[k]
} IN TRANSACTIONS OF 5000 ROWS;
MATCH (p:project) WHERE p.individual_count IS NOT NULL
SET p.individual_count = toInteger(p.individual_count);

// individual 是唯一需要去重的实体：7290 行里有 159 个 accession 各出现两次——同一个患者
// 进了两个研究（如 HRI179847 同时在 HRA001748 的 sc-RNA 和 HRA001749 的 WES 里），
// 临床列完全一致，只有 00_study_accession / 00_sample_accession / 00_strategy 不同。
// 按 accession MERGE 得 7131 个节点，正是手册第 29 行写的数字。
// 重复组的那三列**后写覆盖先写**（与既有图一致：现图 HRI179847 的 study 就只剩 HRA001749），
// 多研究归属由 in_study 边承载（7290 条边 > 7131 个节点），手册也是教走边而不是读该属性。
LOAD CSV WITH HEADERS FROM 'file:///entities/individual.csv' AS row
CALL (row) {
  MERGE (n:individual {`00_individual_accession`: row.`00_individual_accession`})
  WITH n, row, [k IN keys(row) WHERE coalesce(row[k], '') <> ''] AS ks
  UNWIND ks AS k
  SET n[k] = row[k]
} IN TRANSACTIONS OF 2000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///entities/sample.csv' AS row
CALL (row) {
  CREATE (n:sample)
  WITH n, row, [k IN keys(row) WHERE coalesce(row[k], '') <> ''] AS ks
  UNWIND ks AS k
  SET n[k] = row[k]
} IN TRANSACTIONS OF 5000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///entities/T1.csv' AS row
CALL (row) {
  CREATE (n:T1)
  WITH n, row, [k IN keys(row) WHERE coalesce(row[k], '') <> ''] AS ks
  UNWIND ks AS k
  SET n[k] = row[k]
} IN TRANSACTIONS OF 5000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///entities/T2.csv' AS row
CALL (row) {
  CREATE (n:T2)
  WITH n, row, [k IN keys(row) WHERE coalesce(row[k], '') <> ''] AS ks
  UNWIND ks AS k
  SET n[k] = row[k]
} IN TRANSACTIONS OF 5000 ROWS;

// tool 的列名跟着交付换过（0821 起 `输入格式`→`input_format` 等全英文），但图里的属性名
// 保持既有那套：modal 来自「适用组学」、next_tool 来自「下游工具」。工具闭集另有
// skill/references/tool_catalog.csv 兜底，这里只是让图谱侧可查。
LOAD CSV WITH HEADERS FROM 'file:///entities/tool.csv' AS row
CREATE (:tool {tool_id: row.tool_id, tool_name: row.tool_name, function: row.function,
               semantic_input: row.semantic_input, input_format: row.input_format,
               semantic_output: row.semantic_output, output_format: row.output_format,
               next_tool: row.downstream_tools, modal: row.applicable_omics});

// ---------- 4. 关系 ----------
LOAD CSV WITH HEADERS FROM 'file:///reference/format_subclass.csv' AS row
MATCH (c:format {format: row.child}), (p:format {format: row.parent})
CREATE (c)-[:subclass_of]->(p);

LOAD CSV WITH HEADERS FROM 'file:///relations/study_in_project.csv' AS row
MATCH (s:study {study_accession: row.study_accession}), (p:project {project_accession: row.project_accession})
CREATE (s)-[:in_project]->(p);

LOAD CSV WITH HEADERS FROM 'file:///relations/individual_in_study.csv' AS row
CALL (row) {
  MATCH (i:individual {`00_individual_accession`: row.individual_accession}),
        (s:study {study_accession: row.study_accession})
  CREATE (i)-[:in_study]->(s)
} IN TRANSACTIONS OF 5000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///relations/sample_in_individual.csv' AS row
CALL (row) {
  MATCH (sp:sample {sample_accession: row.sample_accession}),
        (i:individual {`00_individual_accession`: row.individual_accession})
  CREATE (sp)-[:in_individual]->(i)
} IN TRANSACTIONS OF 5000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///relations/T1_in_study.csv' AS row
CALL (row) {
  MATCH (t:T1 {t1_id: row.t1_id}), (s:study {study_accession: row.study_accession})
  CREATE (t)-[:in_study]->(s)
} IN TRANSACTIONS OF 5000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///relations/T1_in_sample.csv' AS row
CALL (row) {
  MATCH (t:T1 {t1_id: row.t1_id}), (sp:sample {sample_accession: row.sample_accession})
  CREATE (t)-[:in_sample]->(sp)
} IN TRANSACTIONS OF 5000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///relations/T1_in_format.csv' AS row
CALL (row) {
  MATCH (t:T1 {t1_id: row.t1_id}), (f:format {format: row.semantic_format})
  CREATE (t)-[:in_format]->(f)
} IN TRANSACTIONS OF 5000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///relations/T1_in_level.csv' AS row
CALL (row) {
  MATCH (t:T1 {t1_id: row.t1_id}), (d:datalevel {level: row.data_level})
  CREATE (t)-[:in_level]->(d)
} IN TRANSACTIONS OF 5000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///relations/T1_in_modal.csv' AS row
CALL (row) {
  MATCH (t:T1 {t1_id: row.t1_id}), (m:modal {modal: row.modal})
  CREATE (t)-[:in_modal]->(m)
} IN TRANSACTIONS OF 5000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///relations/T2_in_study.csv' AS row
CALL (row) {
  MATCH (t:T2 {t2_id: row.t2_id}), (s:study {study_accession: row.study_accession})
  CREATE (t)-[:in_study]->(s)
} IN TRANSACTIONS OF 5000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///relations/T2_in_format.csv' AS row
CALL (row) {
  MATCH (t:T2 {t2_id: row.t2_id}), (f:format {format: row.semantic_format})
  CREATE (t)-[:in_format]->(f)
} IN TRANSACTIONS OF 5000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///relations/T2_in_level.csv' AS row
CALL (row) {
  MATCH (t:T2 {t2_id: row.t2_id}), (d:datalevel {level: row.data_level})
  CREATE (t)-[:in_level]->(d)
} IN TRANSACTIONS OF 5000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///relations/T2_in_modal.csv' AS row
CALL (row) {
  MATCH (t:T2 {t2_id: row.t2_id}), (m:modal {modal: row.modal})
  CREATE (t)-[:in_modal]->(m)
} IN TRANSACTIONS OF 5000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///relations/T2_generated_from_T1.csv' AS row
CALL (row) {
  MATCH (t2:T2 {t2_id: row.t2_id}), (t1:T1 {t1_id: row.t1_id})
  CREATE (t2)-[:generated_from]->(t1)
} IN TRANSACTIONS OF 5000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///relations/tool_has_function.csv' AS row
MATCH (t:tool {tool_id: row.tool_id}), (f:function {function: row.function})
CREATE (t)-[:has_function]->(f);

LOAD CSV WITH HEADERS FROM 'file:///relations/tool_has_semantic_input.csv' AS row
MATCH (t:tool {tool_id: row.tool_id}), (f:format {format: row.format})
CREATE (t)-[:input]->(f);

LOAD CSV WITH HEADERS FROM 'file:///relations/tool_has_semantic_output.csv' AS row
MATCH (t:tool {tool_id: row.tool_id}), (f:format {format: row.format})
CREATE (t)-[:output]->(f);

LOAD CSV WITH HEADERS FROM 'file:///relations/tool_relationship.csv' AS row
MATCH (a:tool {tool_id: row.tool_id}), (b:tool {tool_id: row.next_tool_id})
CREATE (a)-[:next_tool {kind: row.kind}]->(b);

LOAD CSV WITH HEADERS FROM 'file:///relations/tool_suitable_for_modal.csv' AS row
MATCH (t:tool {tool_id: row.tool_id}), (m:modal {modal: row.modal})
CREATE (t)-[:suitable_for]->(m);
