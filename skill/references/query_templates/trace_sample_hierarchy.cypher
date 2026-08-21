//问题描述
//某个一级数据文件往上追到样本、个体、研究、项目。
//
//属性名是小写 `t1_id`（0812 时叫 `T1_id`，0819 起改名）。写成 `T1_id` 不会报错，
//只会静默返回 0 行——模型拿到空结果就会退回内部知识去猜，务必照本模板抄。
//0821 实测：28,229 个 T1 里 28,184 个有 in_sample 边，剩下 45 个全是聚合类文件
//（Clinical/各种 *_META，本就跨样本），追到 study 为止即可，s.sample_accession 为
//null 属正常。某队列到底缺多少，看 resolve_sample_roles 的 file_coverage
//.t1_files_unlinked（别看 runs_without_sample_node，那是诊断字段不是缺口）。

MATCH (t1:T1 {t1_id: $t1_id})
OPTIONAL MATCH (t1)-[:in_sample]->(s:sample)
OPTIONAL MATCH (s)-[:in_individual]->(i:individual)
OPTIONAL MATCH (t1)-[:in_study]->(st:study)
OPTIONAL MATCH (st)-[:in_project]->(p:project)
RETURN t1.file_name, s.sample_accession, i.individual_accession,
       st.study_accession, p.project_accession;
