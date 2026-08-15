//问题描述
//哪些工具承担某个功能？
//注意：0811 的 function 是整句中文描述，用 CONTAINS 做子串匹配而不是等值匹配。

MATCH (t:tool)-[:has_function]->(f:function)
WHERE f.function CONTAINS $keyword
RETURN t.tool_id, t.tool_name, f.function
ORDER BY t.tool_id;
