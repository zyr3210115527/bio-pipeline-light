//问题描述
//从某个输入语义格式出发，经由 next_tool 能否到达目标输出语义格式？

MATCH (fin:format {format: $input_format})<-[:input]-(start:tool)
MATCH (fout:format {format: $output_format})<-[:output]-(finish:tool)
MATCH path = (start)-[:next_tool*0..4]->(finish)
RETURN [n IN nodes(path) | n.tool_name] AS chain, length(path) AS steps
ORDER BY steps
LIMIT 20;
