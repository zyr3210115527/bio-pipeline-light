//问题描述
//某个组学模态下有哪些一级数据文件，分布在哪些研究？

MATCH (t1:T1)-[:in_modal]->(m:modal {modal: $modal})
OPTIONAL MATCH (t1)-[:in_study]->(st:study)
RETURN st.study_accession AS study, count(t1) AS files
ORDER BY files DESC;
