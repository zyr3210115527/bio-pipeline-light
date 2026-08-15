#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交付形态真实模型实测：skill + MCP + 真实 LLM（deepseek-v4-flash，无 thinking）"""
import json, os, select, subprocess, sys, time, tempfile, yaml

MCP_PY = os.path.expanduser("~/.dsh/mcp/neo4j-venv/bin/python")
SKILL_MD = os.path.expanduser("~/.dsh/skills/bio-pipeline-planning/SKILL.md")
API_KEY = os.environ.get("DEEPSEEK_API_KEY") or yaml.safe_load(
    open(os.path.expanduser("~/.dsh/.credentials.yaml")))["DEEPSEEK_API_KEY"]
MODEL = "deepseek-v4-flash"
LOG = "/tmp/agent_progress.log"

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

class MCPClient:
    def __init__(self):
        env = dict(os.environ)
        env.update(NEO4J_URI="bolt://localhost:7687", NEO4J_USERNAME="neo4j",
                   NEO4J_PASSWORD="neo4jneo4j", NEO4J_DATABASE="neo4j",
                   NEO4J_READ_ONLY="true", NEO4J_TELEMETRY="false")
        self.p = subprocess.Popen([MCP_PY, "-m", "neo4j_mcp_server"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env, text=True)
        self._send({"jsonrpc":"2.0","id":1,"method":"initialize",
                    "params":{"protocolVersion":"2024-11-05","capabilities":{},
                              "clientInfo":{"name":"bench","version":"1.0"}}})
        self._recv(); self._send({"jsonrpc":"2.0","method":"notifications/initialized","params":{}})
    def _send(self, m):
        self.p.stdin.write(json.dumps(m)+"\n"); self.p.stdin.flush()
    def _recv(self, timeout=60):
        r,_,_ = select.select([self.p.stdout],[],[],timeout)
        return json.loads(self.p.stdout.readline()) if r else None
    def call_tool(self, name, args):
        t0 = time.perf_counter()
        self._send({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":name,"arguments":args}})
        out = self._recv()
        log(f"  MCP {name} -> {(time.perf_counter()-t0)*1000:.0f}ms")
        if out is None:
            return {"content":[{"type":"text","text":"[工具超时无响应]"}]}
        return out.get("result", {})

TOOLS = [
    {"type":"function","function":{"name":"read_cypher","description":"对生信知识图谱执行只读 Cypher 查询（Neo4j）。参数 query 为 Cypher 语句。结果多时加 LIMIT。","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}},
    {"type":"function","function":{"name":"get_schema","description":"获取知识图谱 schema 概览。","parameters":{"type":"object","properties":{},"required":[]}}},
]

def llm_call(messages, tools=None):
    body = {"model":MODEL,"messages":messages,"tools":tools if tools is not None else TOOLS,
            "temperature":1.0,"max_tokens":4000,"thinking":{"type":"disabled"}}
    with tempfile.NamedTemporaryFile("w",suffix=".json",delete=False) as f:
        json.dump(body,f); tmp=f.name
    try:
        r = subprocess.run(["curl","-s","--max-time","150","-X","POST",
            "https://api.deepseek.com/chat/completions",
            "-H","Content-Type: application/json","-H",f"Authorization: Bearer {API_KEY}",
            "--data","@"+tmp], capture_output=True, text=True)
        d = json.loads(r.stdout)
        if "error" in d: raise RuntimeError(f"LLM error: {d['error']}")
        return d
    finally:
        os.unlink(tmp)

def run_query(label, query):
    t0 = time.perf_counter()
    skill = open(SKILL_MD, encoding="utf-8").read()
    system = (f"你是生信分析链路规划 agent。以下是你的操作手册（必须遵守）：\n\n{skill}\n\n"
              "工作方式：先理解需求，用 read_cypher/get_schema 查询图谱核实工具与数据，"
              "最后输出完整答案（工具链 Plan 用 tool-chain/v2 JSON；非生信问题直接拒绝）。")
    messages = [{"role":"system","content":system},{"role":"user","content":query}]
    llm_ms, tool_calls, tokens, steps = [], 0, 0, 0
    trajectory = []
    for step in range(10):
        t1 = time.perf_counter()
        d = llm_call(messages)
        dt = (time.perf_counter()-t1)*1000
        llm_ms.append(dt); steps += 1
        log(f"  step{step} LLM {dt:.0f}ms")
        usage = d.get("usage", {}) or {}
        tokens += usage.get("total_tokens", 0) or 0
        msg = d["choices"][0]["message"]
        tcs = msg.get("tool_calls") or []
        step_rec = {"step": step, "llm_ms": round(llm_ms[-1]),
                    "assistant": (msg.get("content") or "")[:300],
                    "tool_calls": [{"name": tc["function"]["name"],
                                    "args": (tc["function"].get("arguments") or "")[:220]} for tc in tcs]}
        if not tcs:
            messages.append({"role":"assistant","content":msg.get("content") or ""})
            trajectory.append(step_rec)
            break
        # 收敛纪律：第 5 轮后不再记录 tool_calls，直接要求最终作答（保证消息序合法）
        if step >= 3:
            messages.append({"role":"assistant","content":msg.get("content") or ""})
            messages.append({"role":"user","content":"请停止一切工具调用，直接基于已有证据输出最终答案（生信 Plan 用 tool-chain/v2 JSON）。"})
            d = llm_call(messages, tools=[])
            llm_ms.append((time.perf_counter()-t1)*1000)
            usage = d.get("usage", {}) or {}
            tokens += usage.get("total_tokens", 0) or 0
            final_msg = d["choices"][0]["message"]
            messages.append({"role":"assistant","content":final_msg.get("content") or ""})
            trajectory.append(step_rec)
            break
        messages.append({"role":"assistant","content":msg.get("content") or "","tool_calls":tcs})
        for tc in tcs:
            fn = tc["function"]
            try: args = json.loads(fn.get("arguments") or "{}")
            except Exception: args = {}
            res = mcp.call_tool("read-cypher" if fn["name"]=="read_cypher" else "get-schema", args)
            tool_calls += 1
            text = "".join(c.get("text","") for c in res.get("content",[]))
            messages.append({"role":"tool","tool_call_id":tc["id"],"content":text[:12000]})
            step_rec.setdefault("results", []).append(text[:140])
        trajectory.append(step_rec)
    total = (time.perf_counter()-t0)*1000
    final = messages[-1].get("content") or ""
    r = {"label":label,"query":query,"total_ms":round(total),"llm_calls":steps,
         "llm_ms":[round(x) for x in llm_ms],"tool_calls":tool_calls,"tokens":tokens,
         "final":final[:2500]}
    log(f"  DONE total={r['total_ms']}ms tools={tool_calls} tokens={tokens}")
    with open(f"/tmp/agent_final_{label}.json","w") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    with open(f"/tmp/traj_{label}.json","w") as f:
        json.dump({"label":label,"trajectory":trajectory}, f, ensure_ascii=False, indent=1)
    return r

if __name__ == "__main__":
    open(LOG,"w").close()
    mcp = MCPClient()
    log("MCP connected")
    queries = [
        ("免疫浸润", "我想看肝癌样本里的免疫细胞组成，比如 T 细胞、B 细胞和巨噬细胞比例。"),
        ("TMB生存", "我有食管癌的 MAF 文件，想分析肿瘤突变负荷和生存的关系。"),
        ("开放问题", "比较两个肝癌队列的差异表达基因，并对差异基因做 GO 与 KEGG 通路富集。"),
        ("非生信拒绝", "雅思口语怎么准备？"),
    ]
    results = []
    for label, q in queries:
        log(f"=== {label}: {q[:36]}")
        try:
            results.append(run_query(label, q))
        except Exception as e:
            log(f"  FAIL {e}")
    mcp.close()
    json.dump(results, open("/tmp/real_agent_results.json","w"), ensure_ascii=False, indent=1)
    log("ALL DONE -> /tmp/real_agent_results.json")
