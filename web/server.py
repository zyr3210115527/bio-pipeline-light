#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web/server.py — bio-pipeline-light 网页前端后端（纯标准库，无 pip 依赖）

架构：
  浏览器 ──SSE──> 本服务 ──stdio JSON-RPC──> mcp_light_server.py（Neo4j 知识图谱）
                 本服务 ──SSE──> Gemini generateContent（思考 + function calling）

agent 循环参考 examples/deepseek_agent_loop.py：模型按手册自主调工具，最终输出
tool-chain/v2 Plan 或 rejected 单对象。本服务把思考段 / 工具调用 / 文本增量实时
转成 SSE 事件推给前端展示。

配置（环境变量，或 web/config.local 每行 KEY=VALUE）：
  GEMINI_API_KEY    必填，Gemini 兼容端点的 Bearer key
  GEMINI_BASE_URL   默认 https://llm-center.modelbest.co
  GEMINI_MODEL      默认 gemini-3.7-flash
  NEO4J_USER / NEO4J_PASSWORD / NEO4J_URL   透传给 MCP server 子进程
  PORT              默认 8017
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# ---------- 配置 ----------
def _load_env_file():
    path = os.path.join(HERE, "config.local")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env_file()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_BASE_URL = os.environ.get("GEMINI_BASE_URL", "https://llm-center.modelbest.co").rstrip("/")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")
PORT = int(os.environ.get("PORT", "8017"))

# LLM 提供方：gemini（generateContent）或 openai（chat/completions，如 deepseek-v4-flash / mimo）
# 自定义配置用 LLM_* 命名（避开用户全局 OPENAI_API_KEY 等环境变量的抢占），OPENAI_* 仅作兜底
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini")
OPENAI_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or GEMINI_API_KEY
OPENAI_BASE_URL = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or GEMINI_BASE_URL
OPENAI_MODEL = os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL") or "deepseek-v4-flash-0731"
LLM_MODEL = GEMINI_MODEL if LLM_PROVIDER == "gemini" else OPENAI_MODEL

MAX_ROUNDS = 15              # 工具调用轮数上限（手册纪律约 5-8 次）
MODEL_RESULT_LIMIT = 30000   # 喂回模型的工具结果截断长度
GEMINI_TIMEOUT = 300         # 单次流式读超时（thinking 可能持续几十秒）


# ---------- MCP stdio 客户端（newline-delimited JSON-RPC，全局长驻一个进程） ----------
class McpClient:
    def __init__(self):
        self.lock = threading.Lock()
        self._id = 0
        self._start()

    def _start(self):
        env = dict(os.environ)  # NEO4J_* 由环境透传
        self.p = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "mcp_light_server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, env=env, cwd=REPO)
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "bio-web", "version": "1"}})
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
        with self.lock:
            return self._rpc("tools/list", {})["tools"]

    def call(self, name, arguments):
        """子进程异常时重启一次再试；结果取 structuredContent，退化为 raw 文本。"""
        with self.lock:
            try:
                r = self._rpc("tools/call", {"name": name, "arguments": arguments})
            except Exception:
                self._start()
                r = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if r.get("isError"):
            return {"error": r["content"][0]["text"] if r.get("content") else "MCP error"}
        return r.get("structuredContent") or {"raw": r["content"][0]["text"]}


# ---------- Gemini functionDeclarations 适配 ----------
def _sanitize_schema(node):
    """MCP inputSchema → Gemini parameters 子集（只保留 type/properties/items/required/description/enum）。"""
    if not isinstance(node, dict):
        return {"type": "string"}
    out = {}
    for k in ("type", "description", "enum"):
        if k in node:
            out[k] = node[k]
    if "properties" in node:
        out["type"] = "object"
        out["properties"] = {k: _sanitize_schema(v) for k, v in node["properties"].items()}
        if "required" in node:
            out["required"] = node["required"]
    elif "items" in node:
        out["type"] = "array"
        out["items"] = _sanitize_schema(node["items"])
    if "type" not in out:
        # 无类型标注（如 validate_plan 的 plan：对象或字符串）→ 让模型传 JSON 字符串
        out["type"] = "string"
    return out


def mcp_tools_to_gemini(tools):
    return [{"functionDeclarations": [
        {"name": t["name"], "description": t.get("description", ""),
         "parameters": _sanitize_schema(t.get("inputSchema", {}))}
        for t in tools]}]


# web 层工具短描述（每轮随请求重发，越短越省 tokens；语义与 MCP 原描述一致）
TOOL_DESC_SHORT = {
    "read_cypher": "对 Neo4j 图谱执行只读 Cypher（拒写入；individual 的 01_–13_ 仅聚合/IS NOT NULL；无 LIMIT 自动 500）",
    "read_cypher_batch": "多条相互独立的只读 Cypher 一次调用（≤8 条，逐条同守卫），结果按序在 results[]。互不依赖的查询全部打包一轮",
    "get_study_overview": "队列画像一包到底：基本信息+样本数+T1/T2 分布+T2 文件样例+角色分布（sample_roles/role_resolved/file_coverage）",
    "resolve_sample_roles": "确定性 tumor/normal 判定（study 给分布+role_resolved；records 逐条判）。配对/分组选数据前必须调，不许猜",
    "validate_atomic_chain": "atomic 闭集+next_tool 邻接校验。链组装完后调 1 次",
    "validate_execution_chain": "提交执行端前五阶段把关：execution_params+submittable。仅提交场景用",
    "validate_plan": "最终 Plan 接地校验。仅最终输出前调 1 次，中途不校验草稿",
    "health_check": "Neo4j 连通性/规模/闭集诊断",
}


def _slim_tools(tools):
    """用短描述替换 MCP 原描述（仅 web 模型可见层；MCP server 端不变）。"""
    return [{**t, "description": TOOL_DESC_SHORT.get(t["name"], t.get("description", ""))}
            for t in tools]


def mcp_tools_to_openai(tools):
    return [{"type": "function", "function": {
        "name": t["name"], "description": t.get("description", ""),
        "parameters": _sanitize_schema(t.get("inputSchema", {}))}
        } for t in tools]


def load_system_prompt():
    """系统提示词取 docs/frontend-mcp-connection.md 的权威模板（单一事实源），末尾加网页交互说明。"""
    doc = open(os.path.join(REPO, "docs", "frontend-mcp-connection.md"), encoding="utf-8").read()
    mm = re.search(r"```text\n(.*?)```", doc, re.S)
    if not mm:
        raise RuntimeError("docs/frontend-mcp-connection.md 中未找到系统提示词模板（```text 块）")
    prompt = mm.group(1) + "\n\n【交互方式】你正在通过网页聊天与用户交互；最终答案仍严格按契约输出单个 JSON 对象，不加任何前后文字。\n"
    if LLM_PROVIDER == "openai":
        # 该模型通道会把思考写进正文、给 JSON 裹散文——显式封堵
        prompt += (
            "\n【输出通道纪律】content 通道只允许承载两种内容：工具调用，或最终的那个 JSON 对象。"
            "推理、解释、过渡语一律走 reasoning 通道，绝不要写进 content；最终 JSON 前后不得有任何文字。\n"
            "【拒绝即直出】命中拒绝纪律（off_topic / privacy）时直接输出 rejected 单对象，"
            "不要调用 validate_plan，也不要附加解释。\n")
    # 手册内联：省掉 get_planning_guide 独占的一轮往返；工具本身仍可用。
    # web 层默认用精简版手册 manual_compact.md（全部硬事实保留，~21KB≈7k tokens，
    # 整体落进端点 8192-token 缓存帽）；缺失时回退 skill/SKILL.md 全量版
    compact = os.path.join(HERE, "manual_compact.md")
    guide_path = compact if os.path.exists(compact) else os.path.join(REPO, "skill", "SKILL.md")
    guide = open(guide_path, encoding="utf-8").read()
    prompt += ("\n\n【手册已内联】规划手册（即 get_planning_guide 返回内容）全文已在下方给出，"
               "本会话**不需要**再调用 get_planning_guide，直接从下文手册行事：\n\n" + guide)
    # 契约在长上下文中要放在末尾（近因位置），防长手册稀释
    prompt += (
        "\n\n【最终输出契约——最高优先级，覆盖一切】你的最后一条消息必须且只能是一个"
        " tool-chain/v2 JSON 对象（或 rejected 单对象）：不要散文、不要 markdown 围栏、"
        "不要任何前后解释文字，也不要包进数组 []。回答数据分布/清单类问题也用 JSON（selection_status 可为 "
        "information），绝不用散文列表作答。输出前调一次 validate_plan 自检（rejected 除外）。\n"
        "【速度纪律】每轮只保留必要思考（一两句话）；不复述手册或工具返回；工具结果到手即用、"
        "不重复调用；validate_plan 只在最终输出前调一次，探索中途不校验草稿；"
        "证据足够立即输出，不追求额外确认。\n")
    return prompt


# ---------- LLM 流式调用 ----------
def _sse_post(url, payload, api_key):
    """POST 并按 SSE data: 行 yield 解析后的 JSON（两家接口共用）。HTTP 错误带上游正文。"""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    try:
        resp = urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT)
    except urllib.error.HTTPError as e:
        detail = e.read()[:500].decode("utf-8", "replace")
        raise RuntimeError(f"模型接口 HTTP {e.code}：{detail}") from e
    with resp:
        while True:
            line = resp.readline()
            if not line:
                return
            line = line.strip()
            if line.startswith(b"data:"):
                data = line[5:].strip()
                if data and data != b"[DONE]":
                    yield json.loads(data)


def gemini_stream(payload):
    url = f"{GEMINI_BASE_URL}/v1beta/models/{GEMINI_MODEL}:streamGenerateContent?alt=sse"
    return _sse_post(url, payload, GEMINI_API_KEY)


def openai_stream(payload):
    url = f"{OPENAI_BASE_URL}/v1/chat/completions"
    return _sse_post(url, payload, OPENAI_API_KEY)


# ---------- 会话与 agent 循环 ----------
SESSIONS = {}          # session_id -> {"provider": str, "history": [...]}（provider 原生格式）
SESSIONS_LOCK = threading.Lock()


class _RoundBroken(Exception):
    """本轮流不可用（如 tool 参数 JSON 被掐断）：回滚本轮文本后重试。"""


class AgentRunner:
    """一次用户提问的完整 agent 循环，事件通过 emit(dict) 实时推给前端。
    支持两家 LLM：gemini（contents/parts/functionCall/thoughtSignature）与
    openai（messages/tool_calls/reasoning_content），历史按 provider 原生格式保存。"""

    def __init__(self, mcp, fc_tools, system_prompt, session_id, emit):
        self.mcp = mcp
        self.fc_tools = fc_tools      # provider 形态的 tools 负载
        self.system_prompt = system_prompt
        self.session_id = session_id
        self.emit = emit

    # ---------- 主循环 ----------
    def run(self, user_message):
        with SESSIONS_LOCK:
            sess = SESSIONS.setdefault(self.session_id,
                                       {"provider": LLM_PROVIDER, "history": []})
            if sess["provider"] != LLM_PROVIDER:  # 切换 provider 后历史格式不兼容，重置
                sess["provider"], sess["history"] = LLM_PROVIDER, []
        hist = sess["history"]
        hist.append(self._user_entry(user_message))

        empty_retries = 0
        committed_text = 0  # 已提交（之前轮次）的文本长度；本轮流被掐断时回滚到这儿
        nudged = False
        for rnd in range(1, MAX_ROUNDS + 1):
            if rnd == 6 and not nudged:
                # 轮数预算告警（与手册 §6 的 6 轮查询硬上限对齐）：防多轮空转
                nudged = True
                hist.append(self._user_entry(
                    "【系统】查询轮数已达硬上限。停止继续查询，下一轮直接用已有证据输出最终 JSON"
                    "（证据不足就如实 unsupported / missing_from_graph，并在 match_note 说明）。"))
            try:
                thought_text, answer_text, calls, finish_reason, raw = self._stream_round(hist)
            except _RoundBroken:
                answer_text, calls, finish_reason, raw = "", [], None, None
            print(f"[web] round {rnd}: calls={len(calls)} text={len(answer_text)} "
                  f"thought={len(thought_text)} finish={finish_reason}", file=sys.stderr)

            def _truncated_json(t):
                """契约答案是单个 JSON；以 { 开头却解析不了 → 流被中途掐断。"""
                t = t.strip()
                if not t.startswith("{"):
                    return False
                try:
                    json.loads(t)
                    return False
                except Exception:
                    return True

            if not calls and finish_reason is None and (not answer_text or _truncated_json(answer_text)):
                # 流被中途掐断（只吐了思考段，或 JSON 写到一半）：重试本轮，前端回滚本轮文本；
                # 附一条短指令让重试跳过长篇思考，避免按原样再空烧一轮
                empty_retries += 1
                if empty_retries <= 3:
                    self.emit({"type": "text_reset", "keep": committed_text})
                    hist.append(self._user_entry(
                        "（系统：上一轮输出中断。请跳过思考、直接给出工具调用或最终 JSON。）"))
                    time.sleep(1)
                    continue
                self.emit({"type": "error",
                           "message": "模型流多次被中断（finishReason 缺失），请重试"})
                return
            empty_retries = 0
            if calls:
                # 模型请求调工具：助手条目先入历史，逐个执行，结果再入历史
                self._append_assistant(hist, answer_text, calls, raw)
                entries = [self._call_tool(c) for c in calls]
                if LLM_PROVIDER == "gemini":
                    hist.append({"role": "user", "parts": entries})  # 并行调用合并为一条
                else:
                    hist.extend(entries)
                committed_text += len(answer_text)
                continue
            # 终答
            self._append_final(hist, answer_text, raw)
            self.emit({"type": "done", "rounds": rnd, "finishReason": finish_reason})
            return
        self.emit({"type": "error", "message": f"超过 {MAX_ROUNDS} 轮工具调用仍未给出最终答案"})

    # ---------- provider 分发：历史条目 ----------
    def _user_entry(self, text):
        if LLM_PROVIDER == "openai":
            return {"role": "user", "content": text}
        return {"role": "user", "parts": [{"text": text}]}

    def _append_assistant(self, hist, text, calls, raw):
        if LLM_PROVIDER == "openai":
            hist.append({"role": "assistant", "content": text or "",
                         "tool_calls": [{"id": c.get("id") or f"call_{i}",
                                         "type": "function",
                                         "function": {"name": c["name"],
                                                      "arguments": json.dumps(c["args"] or {}, ensure_ascii=False)}}
                                        for i, c in enumerate(calls)]})
            return
        text_sig, fc_parts = raw
        model_parts = []
        if text:
            part = {"text": text}
            if text_sig:
                part["thoughtSignature"] = text_sig
            model_parts.append(part)
        model_parts.extend(fc_parts)  # 整段保留（含 thoughtSignature / id）
        hist.append({"role": "model", "parts": model_parts})

    def _append_final(self, hist, text, raw):
        if not text:
            return
        if LLM_PROVIDER == "openai":
            hist.append({"role": "assistant", "content": text})
            return
        text_sig, _ = raw or (None, None)
        part = {"text": text}
        if text_sig:
            part["thoughtSignature"] = text_sig
        hist.append({"role": "model", "parts": [part]})

    # ---------- provider 分发：流式一轮 ----------
    def _stream_round(self, hist):
        """返回 (thought, text, calls[{id,name,args}], finish_reason, raw)。首个事件前失败静默重试。"""
        for attempt in range(1, 7):
            try:
                if LLM_PROVIDER == "openai":
                    return self._round_openai(hist)
                return self._round_gemini(hist)
            except _RoundBroken:
                raise
            except Exception as e:
                if attempt < 6:
                    time.sleep(1.5 * attempt)
                    continue  # 代理偶发挂起/400，静默重试
                raise RuntimeError(f"模型接口调用失败：{e}") from e

    def _round_gemini(self, contents):
        payload = {
            "systemInstruction": {"parts": [{"text": self.system_prompt}]},
            "contents": contents,
            "tools": self.fc_tools,
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 16384,
                                 "thinkingConfig": {"includeThoughts": True,
                                                    "thinkingBudget": 4096}},
        }
        thought_buf, text_buf, text_sig, fc_parts = [], [], None, []
        finish_reason = None
        for ev in gemini_stream(payload):
            for cand in ev.get("candidates", []):
                if cand.get("finishReason"):
                    finish_reason = cand["finishReason"]
                for part in cand.get("content", {}).get("parts", []):
                    if part.get("thought"):
                        thought_buf.append(part.get("text", ""))
                        self.emit({"type": "thought", "delta": part.get("text", "")})
                    elif "functionCall" in part:
                        fc_parts.append(part)
                    elif "text" in part:
                        text_buf.append(part["text"])
                        if part["text"]:
                            self.emit({"type": "text", "delta": part["text"]})
                        if part.get("thoughtSignature"):
                            text_sig = part["thoughtSignature"]
        calls = [{"id": p["functionCall"].get("id"), "name": p["functionCall"].get("name", "?"),
                  "args": p["functionCall"].get("args") or {}} for p in fc_parts]
        return "".join(thought_buf), "".join(text_buf), calls, finish_reason, (text_sig, fc_parts)

    def _round_openai(self, messages):
        payload = {"model": OPENAI_MODEL,
                   "messages": [{"role": "system", "content": self.system_prompt}] + messages,
                   "tools": self.fc_tools, "temperature": 0.2, "max_tokens": 16384, "stream": True,
                   # thinking 显式开关：开启时推理走 reasoning_content（content 更干净但慢 2-3 倍）；
                   # 关闭后由提示词末尾的输出契约保证格式。THINKING=off 可关。
                   **({"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
                      if os.environ.get("THINKING", "on") != "off" else {})}
        thought_buf, text_buf = [], []
        slots = {}  # tool_calls index -> {id, name, args_str}
        finish_reason = None
        for ev in openai_stream(payload):
            for ch in ev.get("choices", []):
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
                delta = ch.get("delta") or {}
                r = delta.get("reasoning_content")
                if r:
                    thought_buf.append(r)
                    self.emit({"type": "thought", "delta": r})
                c = delta.get("content")
                if c:
                    text_buf.append(c)
                    self.emit({"type": "text", "delta": c})
                for tc in delta.get("tool_calls") or []:
                    slot = slots.setdefault(tc.get("index", 0), {"id": None, "name": None, "args": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]
        calls = []
        for idx in sorted(slots):
            s = slots[idx]
            try:
                args = json.loads(s["args"]) if s["args"] else {}
            except Exception as e:
                raise _RoundBroken(f"工具参数 JSON 被掐断: {e}")  # 断流 → 重试本轮
            calls.append({"id": s["id"], "name": s["name"] or "?", "args": args})
        norm = {"stop": "STOP", "length": "MAX_TOKENS"}.get(finish_reason, finish_reason)
        return "".join(thought_buf), "".join(text_buf), calls, norm, None

    def _call_tool(self, call):
        """执行一次 MCP 工具调用并推送事件，返回 provider 原生的工具结果历史条目。"""
        name, args, call_id = call["name"], call["args"] or {}, call.get("id")
        self.emit({"type": "tool_call", "id": call_id, "name": name, "args": args})
        t0 = time.time()
        try:
            result = self.mcp.call(name, args)
            ok = not (isinstance(result, dict) and "error" in result)
        except Exception as e:
            result, ok = {"error": str(e)}, False
        dur = int((time.time() - t0) * 1000)
        self.emit({"type": "tool_result", "id": call_id, "name": name,
                   "ok": ok, "result": result, "duration_ms": dur})
        # 喂回模型：过长截断
        body = result if isinstance(result, dict) else {"result": result}
        s = json.dumps(body, ensure_ascii=False)
        if len(s) > MODEL_RESULT_LIMIT:
            body = {"truncated": True, "result_head": s[:MODEL_RESULT_LIMIT]}
        if LLM_PROVIDER == "openai":
            return {"role": "tool", "tool_call_id": call_id,
                    "content": json.dumps(body, ensure_ascii=False)}
        fr = {"name": name, "response": body}
        if call_id:
            fr["id"] = call_id
        return {"functionResponse": fr}


# ---------- HTTP 服务 ----------
INDEX_HTML = os.path.join(HERE, "index.html")


class Handler(BaseHTTPRequestHandler):
    server_version = "BioPipelineLightWeb/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[web] %s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, open(INDEX_HTML, "rb").read(), "text/html; charset=utf-8")
        elif self.path == "/api/health":
            try:
                neo4j = self.server.mcp.call("health_check", {})
            except Exception as e:
                neo4j = {"status": "unavailable", "detail": str(e)[:200]}
            self._send(200, json.dumps({
                "neo4j": neo4j, "model": f"{LLM_PROVIDER}:{LLM_MODEL}",
                "gemini_configured": bool(GEMINI_API_KEY)}, ensure_ascii=False))
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send(400, '{"error":"bad json"}')
            return
        if self.path == "/api/chat":
            self._handle_chat(body)
        elif self.path == "/api/reset":
            with SESSIONS_LOCK:
                SESSIONS.pop(body.get("session", ""), None)
            self._send(200, '{"ok":true}')
        else:
            self._send(404, '{"error":"not found"}')

    def _handle_chat(self, body):
        session = body.get("session") or "default"
        message = (body.get("message") or "").strip()
        if not message:
            self._send(400, '{"error":"empty message"}')
            return
        api_key = OPENAI_API_KEY if LLM_PROVIDER == "openai" else GEMINI_API_KEY
        if not api_key:
            self._send(500, '{"error":"LLM API key 未配置（web/config.local 或环境变量）"}')
            return
        # SSE
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(obj):
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        try:
            runner = AgentRunner(self.server.mcp, self.server.fc_tools,
                                 self.server.system_prompt, session, emit)
            runner.run(message)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 前端中断
        except Exception as e:
            try:
                emit({"type": "error", "message": str(e)})
            except Exception:
                pass


def _warmup(mcp, system_prompt):
    """启动预热（后台线程，失败不影响服务）：
    1) health_check 预热 MCP→Neo4j 链路并尽早暴露凭据问题；
    2) 带完整系统提示词发一个 ping，让 LLM 端缓存 46KB 前缀（实测命中后每轮省 2-3s）。"""
    t0 = time.time()
    try:
        h = mcp.call("health_check", {})
        print(f"[web] 预热 Neo4j: {h.get('status')} ({time.time()-t0:.1f}s)", file=sys.stderr)
    except Exception as e:
        print(f"[web] 预热 Neo4j 失败: {e}", file=sys.stderr)
    if LLM_PROVIDER == "openai":
        try:
            t1 = time.time()
            payload = {"model": OPENAI_MODEL, "stream": False, "max_tokens": 16,
                       "messages": [{"role": "system", "content": system_prompt},
                                    {"role": "user", "content": "ping"}]}
            req = urllib.request.Request(f"{OPENAI_BASE_URL}/v1/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {OPENAI_API_KEY}"})
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read())
            cached = (d.get("usage", {}).get("prompt_tokens_details") or {}).get("cached_tokens")
            print(f"[web] 预热 LLM 提示词缓存: {time.time()-t1:.1f}s, cached_tokens={cached}",
                  file=sys.stderr)
        except Exception as e:
            print(f"[web] 预热 LLM 失败: {e}", file=sys.stderr)


def main():
    if not (OPENAI_API_KEY if LLM_PROVIDER == "openai" else GEMINI_API_KEY):
        print("[web] 警告：LLM API key 未配置，请在 web/config.local 或环境变量中设置", file=sys.stderr)
    mcp = McpClient()
    tools = mcp.tools()
    print(f"[web] MCP 已连接，工具：{[t['name'] for t in tools]}", file=sys.stderr)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.mcp = mcp
    # 手册已内联进系统提示词：get_planning_guide 对 web 模型零调用（6 轮全量回归 96 次会话 0 次），
    # 从模型可见工具列表滤掉省 tokens；MCP server 端保留（其他客户端没有内联手冊，仍靠它取手册）
    model_tools = _slim_tools([t for t in tools if t["name"] != "get_planning_guide"])
    server.fc_tools = (mcp_tools_to_gemini if LLM_PROVIDER == "gemini" else mcp_tools_to_openai)(model_tools)
    server.system_prompt = load_system_prompt()
    print(f"[web] Bio Pipeline Light Web 已启动: http://127.0.0.1:{PORT} "
          f"(provider={LLM_PROVIDER}, model={LLM_MODEL})", file=sys.stderr)
    threading.Thread(target=_warmup, args=(mcp, server.system_prompt), daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
