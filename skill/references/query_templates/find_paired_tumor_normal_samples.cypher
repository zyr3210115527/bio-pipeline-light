//问题描述
//某个研究里每个个体挂了哪些样本，各自是肿瘤还是正常，用于判断能否配对。
//
//注意：sample 自带 tissue_type（Tumor/Normal），角色可直接从图里查。0821 交付里
//tissue_type 已是干净二值（无 `Tumor,Normal` 多值单元），HRA016026 为 350 Tumor +
//350 Normal。角色判定的唯一权威仍是 resolve_sample_roles，本模板只给图内原值。
//
//属性名注意：individual 节点上的编号是 `00_individual_accession`（带 00_ 前缀），
//**不叫 `individual_accession`**——写错不会报错，只会让整列返回 null。
//（T1/T2 文件节点上倒是叫 `individual_accession`，两者不一致，别互相套用。）

MATCH (s:sample)-[:in_individual]->(i:individual)
WHERE s.study_accession = $study_accession
WITH i,
     collect(DISTINCT s.sample_accession) AS samples,
     collect(DISTINCT s.sample_name) AS sample_names,
     collect(DISTINCT s.tissue_type) AS tissue_types,
     collect(DISTINCT s.specimen_type) AS specimen_types
WHERE size(samples) > 1
RETURN i.`00_individual_accession` AS individual,
       samples,
       sample_names,
       tissue_types,
       specimen_types,
       'Tumor' IN tissue_types AND 'Normal' IN tissue_types AS pairable
ORDER BY individual
LIMIT 50;
