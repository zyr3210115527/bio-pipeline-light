//问题描述
//从某个工具出发，沿 next_tool 边能走出哪些工具链？
//0811 只登记了 22 条 next_tool，链长有限。

MATCH path = (t:tool {tool_id: $tool_id})-[:next_tool*1..4]->(downstream:tool)
RETURN [n IN nodes(path) | n.tool_name] AS chain, length(path) AS steps
ORDER BY steps, chain;
