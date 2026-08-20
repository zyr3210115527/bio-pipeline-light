#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deepseek_agent_loop.py — 前端接入参考实现（DeepSeek / OpenAI 兼容模型 + bio-pipeline-light MCP）

演示 README「前端接入五步」的完整代码形态：
  MCP tools/list → 转成 function-calling tools → 模型多轮调工具 → 产出 tool-chain/v2
  → validate_plan 接地断言（失败把 violations 喂回重试）→ 输出最终 Plan。

用法：
  pip install openai
  export DEEPSEEK_API_KEY=sk-...            # 或任何 OpenAI 兼容服务的 key
  export DEEPSEEK_BASE_URL=https://api.deepseek.com   # 默认 deepseek 官方
  export DEEPSEEK_MODEL=deepseek-chat
  export NEO4J_USER=neo4j NEO4J_PASSWORD=... \
         NEO4J_URL=http://192.168.130.24:7480/db/neo4j/tx/commit
  python3 examples/deepseek_agent_loop.py "我想用肝癌数据做go富集分析"

提交执行端前，前端还应对用户确认的链路调 validate_execution_chain，
拿 submittable=true 与 execution_params 再下发（本脚本止步于产出接地 Plan）。
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MAX_ROUNDS = 12          # 工具调用轮数上限（手册纪律约 5-8 次调用）
MAX_GROUND_RETRY = 2     # validate_plan 打回后的重试次数

# ---------- 极简 MCP stdio 客户端（newline-delimited JSON-RPC） ----------
class McpClient:
    def __init__(self):
        self.p = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "mcp_light_server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self._id = 0
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "deepseek-loop", "version": "1"}})
        self._notify("notifications/initialized")

    def _rpc(self, method, params):
        self._id += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id,
                                       "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())["result"]

    def _notify(self, method):
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": {}}) + "\n")
        self.p.stdin.flush()

    def tools(self):
        """MCP inputSchema 原样映射为 OpenAI function-calling tools，无需手写。"""
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["inputSchema"]}}
                for t in self._rpc("tools/list", {})["tools"]]

    def call(self, name, arguments):
        r = self._rpc("tools/call", {"name": name, "arguments": arguments})
        return r.get("structuredContent") or {"raw": r["content"][0]["text"]}

# ---------- 系统提示词：取 docs 里的权威版本，避免两处维护 ----------
def load_system_prompt():
    doc = open(os.path.join(REPO, "docs", "frontend-mcp-connection.md"), encoding="utf-8").read()
    mm = re.search(r"```text\n(.*?)```", doc, re.S)
    if not mm:
        sys.exit("docs/frontend-mcp-connection.md 中未找到系统提示词模板（```text 块）")
    return mm.group(1)

# ---------- 主循环 ----------
def extract_json(text):
    """模型偶尔包 markdown 围栏，剥掉后解析。"""
    mm = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    return json.loads(mm.group(1) if mm else text)

def run(query):
    from openai import OpenAI  # OpenAI 兼容 SDK，DeepSeek 官方推荐用法
    llm = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"],
                 base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    mcp = McpClient()
    tools = mcp.tools()
    messages = [{"role": "system", "content": load_system_prompt()},
                {"role": "user", "content": query}]

    plan = None
    ground_retries = 0
    for _ in range(MAX_ROUNDS):
        resp = llm.chat.completions.create(model=model, messages=messages,
                                           tools=tools, temperature=0.2).choices[0].message
        if resp.tool_calls:
            messages.append({"role": "assistant", "content": resp.content or "",
                             "tool_calls": [tc.model_dump() for tc in resp.tool_calls]})
            for tc in resp.tool_calls:
                out = mcp.call(tc.function.name, json.loads(tc.function.arguments or "{}"))
                print(f"[tool] {tc.function.name}", file=sys.stderr)
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(out, ensure_ascii=False)})
            continue
        # 模型给出最终答案 → 三道断言
        try:
            plan = extract_json(resp.content)
        except (json.JSONDecodeError, AttributeError):
            messages += [{"role": "assistant", "content": resp.content or ""},
                         {"role": "user", "content": "输出不是合法的单个 JSON 对象，"
                          "请只输出一个 tool-chain/v2 JSON 或 rejected 单对象。"}]
            continue
        if plan.get("status") == "rejected":
            break  # 拒绝对象直接放行（off_topic / privacy）
        if plan.get("schema_version") != "tool-chain/v2":
            messages += [{"role": "assistant", "content": resp.content},
                         {"role": "user", "content": "缺 schema_version: tool-chain/v2，请修正后重新输出。"}]
            plan = None
            continue
        g = mcp.call("validate_plan", {"plan": plan})
        if g.get("grounded"):
            break  # 接地通过
        # 打回：violations 喂回，让模型回到查询结果修正（不许换个编法）
        ground_retries += 1
        if ground_retries > MAX_GROUND_RETRY:
            sys.exit(f"接地校验多次未通过，如实失败：{g.get('violations')}")
        messages += [{"role": "assistant", "content": resp.content},
                     {"role": "user", "content":
                      "validate_plan 打回（grounded=false），逐条修正后重验再输出。"
                      "修正依据必须来自已有查询结果，查不到就如实 missing_from_graph：\n"
                      + json.dumps(g.get("violations"), ensure_ascii=False)}]
        plan = None

    if plan is None:
        sys.exit("超出轮数上限仍未产出合规 Plan")
    print(json.dumps(plan, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "我想用肝癌数据做go富集分析")
