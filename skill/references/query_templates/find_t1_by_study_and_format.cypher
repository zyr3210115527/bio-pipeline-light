//问题描述
//某个研究下有哪些指定语义格式的一级数据文件？

MATCH (t1:T1)-[:in_study]->(st:study {study_accession: $study_accession})
MATCH (t1)-[:in_format]->(f:format {format: $format})
RETURN t1.T1_id, t1.file_name, t1.file_path, t1.strategy, t1.platform
ORDER BY t1.file_name
LIMIT 100;
