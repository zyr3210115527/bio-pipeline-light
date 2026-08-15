//问题描述
//哪些工具能吃某个语义格式？
//例如 $format = 'RAW_PAIRED_END_R1_FASTQ'

MATCH (t:tool)-[:input]->(f:format {format: $format})
RETURN t.tool_id, t.tool_name
ORDER BY t.tool_id;
