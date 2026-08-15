//问题描述
//哪些工具适用于某个组学模态？
//modal 取值：WES / WGS / bulk_RNA / sc-RNA / Clinical / Meta

MATCH (t:tool)-[:suitable_for]->(m:modal {modal: $modal})
RETURN t.tool_id, t.tool_name
ORDER BY t.tool_id;
