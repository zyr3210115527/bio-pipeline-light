//问题描述
//T1 和 T2 各自的语义格式分布。

MATCH (d)-[:in_format]->(f:format)
WHERE d:T1 OR d:T2
RETURN labels(d)[0] AS level, f.format AS semantic_format, count(d) AS files
ORDER BY level, files DESC;
