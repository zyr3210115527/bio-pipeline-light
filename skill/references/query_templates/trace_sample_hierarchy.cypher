//问题描述
//某个一级数据文件往上追到样本、个体、研究、项目。
//注意 0811 有 6848 行 T1 缺 sample 编号，这类行不会有 in_sample 边。

MATCH (t1:T1 {T1_id: $t1_id})
OPTIONAL MATCH (t1)-[:in_sample]->(s:sample)
OPTIONAL MATCH (s)-[:in_individual]->(i:individual)
OPTIONAL MATCH (t1)-[:in_study]->(st:study)
OPTIONAL MATCH (st)-[:in_project]->(p:project)
RETURN t1.file_name, s.sample_accession, i.individual_accession,
       st.study_accession, p.project_accession;
