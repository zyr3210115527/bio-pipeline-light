# DSH 集成说明（dsh-mcp-client + skill）

> **本文第 1 节已被 `docs/frontend-mcp-connection.md` 取代（0821）。**
> 现在的数据面是本仓库自带的 `mcp_light_server.py`（7 个工具：`get_planning_guide`、
> `read_cypher`、`resolve_sample_roles`、`validate_atomic_chain`、`validate_execution_chain`、
> `validate_plan`、`health_check`），**不是**官方 `neo4j-mcp-server`。差别不只是换个进程：
> 本 server 的 `read_cypher` 带写入拦截、患者级临床属性守卫（`01_`–`13_` 只许聚合）、
> 自动 LIMIT 500 与截断上报，官方 server 一样都没有；且 SKILL.md 明确禁用 `get_schema`
> （图谱模型已写在手册里，整库 schema 只是浪费轮数）。
> **接前端一律照 `docs/frontend-mcp-connection.md` 做**，本节仅作为历史路径存档。

## 1. ~~MCP 数据面：连接本地 Neo4j（只读）~~（已废弃，见上方说明）

在 DSH profile 的用户 patch 层（`~/.dsh/profiles/web/cordis.patch.yml`）追加：

```yaml
- insert:
    - id: mcp-neo4j
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: neo4j
        transport: stdio
        command: /path/to/venv/bin/python          # 官方 neo4j-mcp-server 的 venv
        args: ['-m', 'neo4j_mcp_server']
        env:
          NEO4J_URI: bolt://192.168.130.24:7690    # 0821 现网；本机开发才是 localhost:7687
          NEO4J_USERNAME: neo4j
          NEO4J_PASSWORD: '***'                    # 走环境变量，别写进仓库（或 !!js process.env.NEO4J_PASSWORD）
          NEO4J_DATABASE: neo4j
          NEO4J_READ_ONLY: 'true'                  # 只读开关：write-cypher 不注册
          NEO4J_TELEMETRY: 'false'
```

- 官方 server：`pip install neo4j-mcp-server`（Python 3.13+，独立 venv）
- Neo4j 需装匹配版本的 APOC（官方 server 用 `apoc.meta` 做 schema 推断），并在 `neo4j.conf` 加 `dbms.security.procedures.unrestricted=apoc.*`
- 生效后 agent 获得两个原生工具：`mcp__neo4j__read-cypher`（只读查询）和 `mcp__neo4j__get-schema`；重启 DSH web 后新会话可见。**这两个工具名只属于这条废弃路径**——现在的工具叫 `read_cypher`（下划线，无 `mcp__neo4j__` 前缀），照本节的名字调会找不到工具
- 备选只读接口（无 MCP 时）：`curl -u neo4j:<pwd> -X POST -H 'Content-Type: application/json' -d '{"statements":[{"statement":"<Cypher>"}]}' http://192.168.130.24:7480/db/neo4j/tx/commit`（本机开发是 `127.0.0.1:7474`）。**注意这条路径绕开了本 server 的隐私守卫，只能运维自查用，不要接给前端模型**

## 2. Skill 推理面：安装 bio-pipeline-planning

```bash
mkdir -p ~/.dsh/skills
cp -r skill ~/.dsh/skills/bio-pipeline-planning
```

- 用户级 skill 根：`~/.dsh/skills`（`dsh-skill-filesystem` 自动扫描，无需配置）
- 新会话的 skill 目录即可见；模型按需加载（`skill(name=bio-pipeline-planning)`）
- skill 内容：
  - 图谱词汇表（format/modal/datalevel/ArtifactType）
  - 闭集工具目录规则（11 atomic / 38 pipeline / 1 task_pipeline；`candidates[]` 只出 atomic，未原子化需求返回 unsupported）
  - 15 条 Cypher 配方（`references/query_templates/`，read-cypher 直接执行）
  - `tool-chain/v2` 输出契约（前端可直接消费的 plan JSON）

## 3. 评测

```bash
export NEO4J_USER=neo4j NEO4J_PASSWORD=<你的密码>
python3 benchmark/bench_light_96.py <96例问题-数据-工具对应表(1).xlsx>
```

输出：`tool_top1 / tool_top3 / data_found / combined` + 逐 case 明细（`bench_light_96_report.json`）。
