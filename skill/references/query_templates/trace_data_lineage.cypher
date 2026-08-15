//问题描述
//某个二级分析结果是从哪些一级数据产生的？
//0811 用 generated_from 按 run_accession 关联，替代旧模型的 DERIVED_FROM。

MATCH (t2:T2 {T2_id: $t2_id})-[:generated_from]->(t1:T1)
RETURN t2.T2_id, t2.file_name, t2.semantic_format,
       collect(DISTINCT t1.file_name) AS source_t1_files;
