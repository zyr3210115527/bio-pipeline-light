//问题描述
//某个研究下有哪些指定语义格式的一级数据文件？
//
//属性名是小写 `t1_id`（0812 时叫 `T1_id`，0819 起改名）。写成 `T1_id` 语法合法、
//不报错，返回行数也照旧，只是那一整列全是 null——0821 就是这么漏过去的，只有逐列
//看才发现。照抄本模板，别改属性名大小写。
//T1 实有字段（0821 实测 28,229 个 T1）：t1_id / file_name / file_format /
//semantic_format / data_level / study_accession（全量），strategy 28,222，
//platform / sample_accession / individual_accession 28,184（缺的 45 个是
//Clinical、*_META 这类聚合文件，本就不属于单个样本），run_accession 27,070，
//file_path 26,879，size 25,417。所以聚合类文件的 platform/sample_accession 为
//null 属正常，不要据此判定"图里没有平台信息"。

MATCH (t1:T1)-[:in_study]->(st:study {study_accession: $study_accession})
MATCH (t1)-[:in_format]->(f:format {format: $format})
RETURN t1.t1_id, t1.file_name, t1.file_path, t1.strategy, t1.platform,
       t1.semantic_format, t1.data_level
ORDER BY t1.file_name
LIMIT 100;
