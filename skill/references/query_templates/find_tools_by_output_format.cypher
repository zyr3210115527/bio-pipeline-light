//问题描述
//哪些工具能产出某个语义格式？
//例如 $format = 'MUTATION_ANNOTATION_FORMAT_MAF'

MATCH (t:tool)-[:output]->(f:format {format: $format})
RETURN t.tool_id, t.tool_name
ORDER BY t.tool_id;
