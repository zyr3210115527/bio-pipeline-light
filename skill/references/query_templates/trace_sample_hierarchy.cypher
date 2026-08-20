//问题描述
//某个一级数据文件往上追到样本、个体、研究、项目。
//
//0819 实测：6,969 行 T1 没有 sample 编号、拿不到 in_sample 边，对它们只能追到 study。
//根因是图谱里 sample 节点每个只记录一个 run_accession（文件侧共 13,063 个 run，
//其中 3,758 个没有对应 sample 节点，牵连 7,516 个 T1 文件），用 run_accession 回连也命中 0。
//所以这类文件查出来 s.sample_accession = null 是**图谱缺映射**，不是"聚合文件本就没样本"，
//不要按文件名顺序猜归属。缺口大小用 resolve_sample_roles 的 file_coverage 看。
//另：属性名 0819 起是小写 t1_id（0812 时叫 T1_id），写错不会报错，只会静默返回 0 行。

MATCH (t1:T1 {t1_id: $t1_id})
OPTIONAL MATCH (t1)-[:in_sample]->(s:sample)
OPTIONAL MATCH (s)-[:in_individual]->(i:individual)
OPTIONAL MATCH (t1)-[:in_study]->(st:study)
OPTIONAL MATCH (st)-[:in_project]->(p:project)
RETURN t1.file_name, s.sample_accession, i.individual_accession,
       st.study_accession, p.project_accession;
