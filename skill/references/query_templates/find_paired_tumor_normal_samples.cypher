//问题描述
//某个研究里每个个体挂了哪些样本，各自是肿瘤还是正常，用于判断能否配对。
//
//注意：0812 的 sample 表自带 tissue_type（Tumor/Normal，9,700 个样本有值），
//所以角色可以直接从图里查，不再需要旁路映射。但有两个 study 她标错了，运行时由
//pipeline_router.STUDY_ROLE_OVERRIDES 覆盖，这条模板给出的是图里的原值：
//  HRA016026 700 个样本全标成 Normal，其中 350 个样本名是 *_Tumor；
//  HRA000071 有 104 个 specimen_type=Blood 的样本标成 Tumor，同批另外 182 个标 Normal。

MATCH (s:sample)-[:in_individual]->(i:individual)
WHERE s.study_accession = $study_accession
WITH i,
     collect(DISTINCT s.sample_accession) AS samples,
     collect(DISTINCT s.sample_name) AS sample_names,
     collect(DISTINCT s.tissue_type) AS tissue_types,
     collect(DISTINCT s.specimen_type) AS specimen_types
WHERE size(samples) > 1
RETURN i.individual_accession AS individual,
       samples,
       sample_names,
       tissue_types,
       specimen_types,
       'Tumor' IN tissue_types AND 'Normal' IN tissue_types AS pairable
ORDER BY individual
LIMIT 50;
