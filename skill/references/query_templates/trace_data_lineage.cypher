//问题描述
//某个二级分析结果是从哪些一级数据产生的？
//
//属性名是小写 `t2_id`（0812 时叫 `T2_id`，0819 起改名）。写成 `T2_id` 语法合法、
//Neo4j 不报错，只会静默返回 0 行——照本模板抄，别改属性名大小写。
//0821 实测图内有 62,630 条 (T2)-[:generated_from]->(T1) 边（按 run_accession 关联，
//替代旧模型的 DERIVED_FROM）。查出 0 行先确认 t2_id 传对了，再下"没有血缘"的结论。

MATCH (t2:T2 {t2_id: $t2_id})-[:generated_from]->(t1:T1)
RETURN t2.t2_id, t2.file_name, t2.semantic_format,
       collect(DISTINCT t1.file_name) AS source_t1_files;
