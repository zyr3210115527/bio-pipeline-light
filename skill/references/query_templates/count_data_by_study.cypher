//问题描述
//每个研究各有多少 T1 和 T2？用于核对导入完整性。

MATCH (st:study)
OPTIONAL MATCH (t1:T1)-[:in_study]->(st)
WITH st, count(DISTINCT t1) AS t1_count
OPTIONAL MATCH (t2:T2)-[:in_study]->(st)
RETURN st.study_accession, st.title, t1_count, count(DISTINCT t2) AS t2_count
ORDER BY st.study_accession;
