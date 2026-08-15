//问题描述
//某个工具的完整输入输出签名是什么？

MATCH (t:tool {tool_id: $tool_id})
OPTIONAL MATCH (t)-[:input]->(fi:format)
OPTIONAL MATCH (t)-[:output]->(fo:format)
RETURN t.tool_id, t.tool_name,
       collect(DISTINCT fi.format) AS inputs,
       collect(DISTINCT fo.format) AS outputs;
