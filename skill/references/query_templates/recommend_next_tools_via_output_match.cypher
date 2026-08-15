//问题描述
//某个工具之后还能接哪些工具？
//依据是输出语义格式与下游输入语义格式相接，不依赖 next_tool 边。

MATCH (t1:tool {tool_id: $tool_id})-[:output]->(f:format)
MATCH (t2:tool)-[:input]->(f)
WHERE t1 <> t2
RETURN t1.tool_name AS current_tool,
       f.format AS intermediate_format,
       collect(DISTINCT t2.tool_name) AS candidate_next_tools;
