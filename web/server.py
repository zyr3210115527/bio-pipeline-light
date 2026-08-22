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

# 墙钟只由模型轮数决定（工具执行全部 <0.5s，端点每轮 8-20s）。以下三个预算把
# 「反复补查 → 反复校验」的长尾结构性封死，超预算即用 tool_choice 硬制终答。
QUERY_ROUND_BUDGET = int(os.environ.get("QUERY_ROUND_BUDGET", "3"))  # 允许的取数轮数
MAX_REPAIRS = int(os.environ.get("MAX_REPAIRS", "1"))                # 接地失败后的修正轮上限
DUP_CALL_LIMIT = int(os.environ.get("DUP_CALL_LIMIT", "2"))          # 重复调用次数达此值即制终答
# 取数类工具（计入取数轮预算）；校验类不计
QUERY_TOOLS = {"read_cypher", "read_cypher_batch", "get_study_overview",
               "resolve_sample_roles", "health_check"}


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
        "information），绝不用散文列表作答。\n"
        "【接地校验由服务端自动执行】本会话**不提供** validate_plan 工具，也不要等它："
        "你输出最终 JSON 后，服务端会自动对它跑接地校验；若 grounded=false，会把 violations "
        "回传给你修正。所以证据够了就**直接输出最终 JSON**，把校验交给服务端。\n"
        "【只写判断性内容，样板交给服务端】服务端在你输出后会自动补全所有「图谱/闭集本来就知道」"
        "的字段，你**不要生成**它们（写了也会被图内事实覆盖，纯属浪费时间）：\n"
        "  · tool 块只写 `tool_id`——catalog_id/tool_kind/name/description/inputs/outputs 全部省略；\n"
        "  · asset 只写 `file_name` 与 `match_reason`——file_path/format/file_format/data_level/"
        "strategy/study_accession/sample_accession/run_accession/specimen_type/read_pair 全部省略"
        "（**尤其不要凭记忆写 file_path**，以图内记录为准）；\n"
        "  · candidates 的 tool_chain 每步只写 `tool_id`（槽位由 Knowledge Card 补）；\n"
        "  · match_id / rank / source / reference_case_id / recommendation_count / candidate_count /"
        " planner_metadata / data_matcher_mode / mcp_timing_ms 一律**不要写**（timing 是服务端运行事实，"
        "编造即错）。\n"
        "  必须由你给出的只有：schema_version、selection_status、intent、每条 recommendation 的"
        " pipeline_id / match_note / data.assets[].file_name+match_reason、candidates 的 tool_chain 顺序。\n"
        f"【取数预算 {QUERY_ROUND_BUDGET} 轮】取数最多 {QUERY_ROUND_BUDGET} 轮"
        "（每轮可并行多个调用 / read_cypher_batch 一次 8 条，把互不依赖的查询全打包进同一轮）；"
        "超预算后系统会强制你直接输出。手册 §8 快照表已含 51 工具的 in→out 与 20 队列画像，"
        "工具匹配/选队列**直接用快照，不要查证**；查询只花在文件级明细（file_name/file_path）。\n"
        "【速度纪律】每轮只保留必要思考（一两句话）；不复述手册或工具返回；工具结果到手即用、"
        "不重复调用；证据足够立即输出，不追求额外确认。\n")
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


def _extract_json_obj(text):
    """从终答文本里剥出那个 JSON 对象：裸对象 / ```json 围栏 / 前后裹散文 / 单元素数组。
    解析不出对象时返回 None。"""
    t = (text or "").strip()
    if not t:
        return None
    for cand in (t, ):
        try:
            v = json.loads(cand)
            if isinstance(v, dict):
                return v
            if isinstance(v, list) and len(v) == 1 and isinstance(v[0], dict):
                return v[0]   # 契约要裸对象：单元素数组自动拆封
        except Exception:
            pass
    mm = re.search(r"```(?:json)?\s*(.+?)\s*```", t, re.S)
    if mm:
        return _extract_json_obj(mm.group(1))
    # 前后裹散文：取第一个 { 到与之配对的 }
    start = t.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    prefix = None
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except Exception:
                    prefix = t[start:i + 1]
                    break   # 括号配平但解析失败 → 交给下面的机械修补
    # 先修整段（多出的收尾符会让上面的扫描在中途就配平，只修前缀会丢内容），修不成再退回前缀
    return _json_syntax_repair(t[start:]) or (_json_syntax_repair(prefix) if prefix else None)


def _salvage_json(t):
    """从一段自由文本里捞出**最后**一个完整的契约对象。

    用在「终答被整份写进 reasoning_content、content 一个字没吐」的场合（实测 96 例里
    c29/c36 两例）。推理段里散落着大量半截 JSON，所以按契约首字段 schema_version /
    status 锚定，并取最后一个配平成功的——那才是模型想清楚之后的结论。
    判定要卡死在契约上：`data` 块里的 `"status": "available"` 同样能被锚中并解析成
    一个合法 dict，认它就等于把半个 data 块当终答交出去。"""
    best = None
    for m in re.finditer(r'\{\s*"(?:schema_version|status)"', t or ""):
        obj = _extract_json_obj(t[m.start():])
        if not isinstance(obj, dict):
            continue
        if obj.get("schema_version") == "tool-chain/v2" or \
                (obj.get("status") == "rejected" and obj.get("reason")):
            best = obj
    return best


# 端点偶发把 function call 当普通文本吐出来：DSML 标记原样出现在 content 里，
# 本轮既没有 tool_calls 也没有合法终答。全角 ｜ 不稳定，只锚定 ASCII 部分。
_DSML_INVOKE = re.compile(r'invoke name="([A-Za-z0-9_]+)"(.*?)(?:</[^<>]*invoke>|\Z)', re.S)
_DSML_PARAM = re.compile(r'parameter name="([A-Za-z0-9_]+)"[^<>]*>(.*?)(?:</[^<>]*parameter>|\Z)',
                         re.S)


def _parse_leaked_tool_calls(text):
    """把泄漏成文本的 DSML 工具调用解析回真正的调用，返回 [] 表示没泄漏。

    不修的话代价是双份的：这一轮空烧，下一轮模型还会被服务端「你的 JSON 语法有误」
    的提示带偏（实测 c86 因此直接交了空答案）。"""
    if "tool_calls>" not in text or 'invoke name="' not in text:
        return []
    calls = []
    for i, m in enumerate(_DSML_INVOKE.finditer(text)):
        args = {k: v.strip() for k, v in _DSML_PARAM.findall(m.group(2))}
        if args:
            calls.append({"id": f"leaked_{i}", "name": m.group(1), "args": args})
    return calls


def _json_syntax_repair(frag):
    """模型偶发写出语法坏掉的 JSON（实测：`[],,"k"` 双逗号、多出的收尾 `}`）。
    这里只做**不改语义**的机械修补，修不好返回 None（由调用方回抛给模型重出）。"""
    s = (frag or "").strip()
    if not s:
        return None
    # 1) 逗号病：`,,` / `,}` / `,]`（只在字符串外算）
    out, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == ",":
            if out and out[-1] == ",":       # 连续逗号：吞掉
                continue
        elif ch in "}]" and out and out[-1] == ",":
            out.pop()                        # 尾随逗号
        out.append(ch)
    s = "".join(out)
    # 2) 括号数量对不上：多余收尾符丢弃、缺的补齐；
    #    另一种实测怪相是整篇引号翻倍（`""key""：""v""`，CSV 式转义），整体折半即可还原
    cands = [s, _rebalance(s)]
    if s.count('""') >= 4:
        halved = s.replace('""', '"')
        cands += [halved, _rebalance(halved)]
    for cand in cands:
        try:
            v = json.loads(cand)
            if isinstance(v, dict):
                return v
        except Exception:
            pass
    return None


def _rebalance(s):
    """括号兜底：与栈顶不配的收尾符丢弃，收尾不足的按栈补齐。"""
    keep, stack, in_str, esc = [], [], False, False
    for ch in s:
        if in_str:
            keep.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                continue                     # 多余/错配的收尾符：丢弃
            stack.pop()
        keep.append(ch)
    return "".join(keep) + "".join(reversed(stack))


def _repair_hint(violations):
    """把 validate_plan 的 violations 翻成一句可直接执行的修法。

    只给一次修正轮，所以提示必须具体到「改哪个字段、改成什么」——轨迹里最常见的
    `recommendations 为空`（信息型回答）曾在通用提示下连续两轮不收敛。"""
    v = " ".join(str(x) for x in violations)
    hints = []
    if "recommendations 为空" in v:
        hints.append("要么把 selection_status 改成 information/unsupported/no_candidate"
                     "（信息型、需求超出闭集、图内查无——这三种状态允许 recommendations 为空），"
                     "要么从手册 §8.1 闭集目录里选语义最贴近的 pipeline 补一条 rank1 推荐。")
    if "不在闭集目录" in v or "非闭集 atomic" in v:
        hints.append("pipeline_id/tool_id 必须逐字取自手册 §8.1 的 51 个工具名（不能为 null、"
                     "不能自造），atomic 链只能用 §3 的 11 个可编排 atomic。")
    if "图内不存在" in v or "file_path 与图内记录不符" in v:
        hints.append("assets 只保留本会话查询结果里逐字出现过的 file_name/file_path，"
                     "查不到的直接删掉并把 data.status 标 missing_from_graph。")
    if "schema_version" in v:
        hints.append("补上 \"schema_version\":\"tool-chain/v2\"。")
    if "data.assets 为空" in v:
        hints.append("查一条 Cypher 把该流程要的文件挑出来（按 semantic_format 过滤、"
                     "`ORDER BY n.file_name` 取最靠前的一份，配对测序取 f1/r2 一对），"
                     "填进 assets；临床表与元信息表不用你写，服务端会补。")
    return ("".join(hints) + " ") if hints else ""


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
        self.query_rounds = 0     # 已消耗的取数轮数
        self.repairs = 0          # 接地失败后的修正轮数
        self.syntax_repairs = 0   # JSON 语法坏掉后的重出轮数（与接地修正各自计数）
        self.force_final = False  # True → 本轮 tool_choice=none，模型只能出终答
        self.seen_calls = {}      # 调用签名 -> 结果（抑制重复查询）
        self.dup_hits = 0
        for rnd in range(1, MAX_ROUNDS + 1):
            try:
                thought_text, answer_text, calls, finish_reason, raw = self._stream_round(
                    hist, tool_choice=("none" if self.force_final else "auto"))
            except _RoundBroken:
                answer_text, calls, finish_reason, raw = "", [], None, None
            print(f"[web] round {rnd}: calls={len(calls)} text={len(answer_text)} "
                  f"thought={len(thought_text)} finish={finish_reason} "
                  f"qrounds={self.query_rounds} forced={self.force_final}", file=sys.stderr)

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
            if not calls and answer_text:
                # 工具调用泄漏成文本：解析回真正的调用，并把这段标记从文本里抹掉，
                # 免得它被当成终答再触发一轮「JSON 语法有误」的无效修补
                leaked = _parse_leaked_tool_calls(answer_text)
                if leaked:
                    calls = leaked
                    answer_text = ""
                    self.emit({"type": "text_reset", "keep": committed_text})
                    print(f"[web] round {rnd}: 工具调用泄漏成文本，已解析回 "
                          f"{[c['name'] for c in leaked]}", file=sys.stderr)
            if not calls and not answer_text.strip() and thought_text:
                # 模型把整份终答写进了 reasoning_content，content 一个字没吐。
                # 推理段里那个配平的契约对象就是它的结论，捞出来当终答，省一整轮重出。
                salv = _salvage_json(thought_text)
                if salv is not None:
                    answer_text = json.dumps(salv, ensure_ascii=False, separators=(",", ":"))
                    print(f"[web] round {rnd}: 终答只出现在 reasoning 段，已捞回",
                          file=sys.stderr)
            if calls:
                # 拒绝判定是终局：模型偶发「先吐 rejected 对象、同一轮又顺手查一把图」
                # （实测 q12/q13 查 count(n) 纯属多余，白烧一轮且把 JSON 留在流里污染终答）。
                # 这种情况直接按终答收，忽略后面的调用。
                early = _extract_json_obj(answer_text)
                if isinstance(early, dict) and early.get("status") == "rejected" \
                        and early.get("reason"):
                    clean, _ = self._finalize(hist, answer_text, committed_text)
                    self._append_final(hist, clean, raw)
                    self.emit({"type": "done", "rounds": rnd, "finishReason": finish_reason})
                    return
                # 模型请求调工具：助手条目先入历史，逐个执行，结果再入历史
                self._append_assistant(hist, answer_text, calls, raw)
                entries = [self._call_tool(c) for c in calls]
                if LLM_PROVIDER == "gemini":
                    hist.append({"role": "user", "parts": entries})  # 并行调用合并为一条
                else:
                    hist.extend(entries)
                committed_text += len(answer_text)
                if any(c["name"] in QUERY_TOOLS for c in calls):
                    self.query_rounds += 1
                self._apply_budget(hist)
                continue
            # 终答：服务端自动接地校验（省掉模型自己调 validate_plan 的一整轮）
            clean, verdict = self._finalize(hist, answer_text, committed_text)
            if verdict == "repair":
                continue
            self._append_final(hist, clean, raw)
            self.emit({"type": "done", "rounds": rnd, "finishReason": finish_reason})
            return
        self.emit({"type": "error", "message": f"超过 {MAX_ROUNDS} 轮工具调用仍未给出最终答案"})

    # ---------- 收敛预算：取数轮/重复调用用尽即硬制终答 ----------
    def _apply_budget(self, hist):
        if self.force_final:
            return
        why = None
        if self.query_rounds >= QUERY_ROUND_BUDGET:
            why = f"取数预算（{QUERY_ROUND_BUDGET} 轮）已用尽"
        elif self.dup_hits >= DUP_CALL_LIMIT:
            why = "检测到重复查询"
        if why:
            self.force_final = True
            hist.append(self._user_entry(
                f"【系统】{why}。停止一切查询，你的下一条消息必须就是最终 JSON 对象本身"
                "（服务端会自动做接地校验）；证据不足的部分如实标 unsupported / "
                "missing_from_graph，并在 match_note 说明。"))

    # ---------- 终答处理：规范化 + 服务端接地校验 ----------
    def _finalize(self, hist, answer_text, committed_text):
        """返回 (最终文本, "done"|"repair")。

        1) 规范化：模型偶发把 JSON 裹进散文/围栏，服务端剥出裸对象后重推前端（0 成本修格式）；
        2) 补全：hydrate_plan 把「图谱/闭集本来就知道」的字段（工具描述、I/O 槽位、asset 的
           file_path/format/data_level、原子链槽位、planner_metadata、mcp_timing_ms）由服务端
           确定性填上——模型少生成一半 token（实测终答生成均 30.6s，其中约 52% 是样板），
           且这些字段不再有被编造的机会；
        3) 接地校验：服务端直接跑 validate_plan（<0.5s），grounded=false 时把 violations
           回传给模型修正——把原本占一整轮模型延迟的自检搬到服务端。"""
        obj = _extract_json_obj(answer_text)
        if obj is None:
            # 连机械修补都救不回来：花一轮让模型只重出 JSON，比直接交付一段解析不了的
            # 文本划算（下游只认裸对象）。前提是这段文本**确实是在写 JSON**——文本里
            # 连 `{` 都没有时说明模型压根没在出终答（实测 c86 是工具调用泄漏），
            # 这时再喊「你的 JSON 语法有误」只会把它带偏，改成要它重出一份终答。
            if self.syntax_repairs < MAX_REPAIRS and answer_text.strip():
                self.syntax_repairs += 1
                self.force_final = True
                hist.append({"role": "assistant", "content": answer_text}
                            if LLM_PROVIDER == "openai"
                            else {"role": "model", "parts": [{"text": answer_text}]})
                hist.append(self._user_entry(
                    ("【系统】你刚才输出的 JSON 语法有误（括号/逗号不配对），无法解析。"
                     "内容不用改，只把同一份结论**重新完整输出一遍合法 JSON 对象**："
                     if "{" in answer_text else
                     "【系统】你刚才没有输出最终答案。现在直接给出 tool-chain/v2 JSON 对象：") +
                    "跳过思考，直接从 { 开始、到 } 结束，不要散文和围栏。"))
                self.emit({"type": "text_reset", "keep": 0})
                return answer_text, "repair"
            return answer_text, "done"   # 修正预算已用尽：原样交付，不空转
        hydrated = False
        try:
            hres = self.mcp.call("hydrate_plan", {"plan": obj})
            if hres.get("status") == "ok" and isinstance(hres.get("plan"), dict):
                obj = hres["plan"]
                hydrated = bool(hres.get("filled"))
        except Exception:
            pass                          # 补全失败不影响交付，交给下面的校验兜底
        clean = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        if clean != answer_text.strip() or committed_text:
            # 整条流清空后只推这一份裸 JSON。keep=0 是关键：前面几轮如果漏出过文字
            # （模型爱在工具轮里先写一段 JSON 再调工具），留着会和终答拼成两个对象、
            # 整体解析失败。契约本来就只认最后一个对象，中途文字一律不算数。
            self.emit({"type": "text_reset", "keep": 0})
            self.emit({"type": "text", "delta": clean})
            self.emit({"type": "normalized",
                       "reason": "hydrated" if hydrated else "stripped_prose_or_fence"})

        t0 = time.time()
        try:
            res = self.mcp.call("validate_plan", {"plan": obj})
        except Exception as e:
            res = {"status": "error", "detail": str(e)}
        # 合成事件：前端 Plan 视图与回归脚本仍能看到接地结论（auto=服务端代跑）
        self.emit({"type": "tool_call", "id": "auto_validate", "name": "validate_plan",
                   "args": {"plan": "<final>"}, "auto": True})
        self.emit({"type": "tool_result", "id": "auto_validate", "name": "validate_plan",
                   "ok": res.get("status") != "error", "result": res, "auto": True,
                   "duration_ms": int((time.time() - t0) * 1000)})
        violations = res.get("violations") or []
        if res.get("grounded") is False and violations and self.repairs < MAX_REPAIRS:
            self.repairs += 1
            self.force_final = True   # 修正轮只许出终答，不许再开查询长尾
            hist.append({"role": "assistant", "content": clean} if LLM_PROVIDER == "openai"
                        else {"role": "model", "parts": [{"text": clean}]})
            hist.append(self._user_entry(
                "【系统】服务端接地校验未通过：" + "；".join(str(v) for v in violations[:6]) + "。" +
                _repair_hint(violations) +
                "请用已有证据修正后，直接重新输出完整的最终 JSON 对象（只输出对象本身）。"))
            self.emit({"type": "text_reset", "keep": 0})
            return clean, "repair"
        return clean, "done"

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
    def _stream_round(self, hist, tool_choice="auto"):
        """返回 (thought, text, calls[{id,name,args}], finish_reason, raw)。首个事件前失败静默重试。"""
        for attempt in range(1, 7):
            try:
                if LLM_PROVIDER == "openai":
                    return self._round_openai(hist, tool_choice)
                return self._round_gemini(hist, tool_choice)
            except _RoundBroken:
                raise
            except Exception as e:
                if attempt < 6:
                    time.sleep(1.5 * attempt)
                    continue  # 代理偶发挂起/400，静默重试
                raise RuntimeError(f"模型接口调用失败：{e}") from e

    def _round_gemini(self, contents, tool_choice="auto"):
        payload = {
            "systemInstruction": {"parts": [{"text": self.system_prompt}]},
            "contents": contents,
            "tools": self.fc_tools,
            "toolConfig": {"functionCallingConfig": {
                "mode": "NONE" if tool_choice == "none" else "AUTO"}},
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

    def _round_openai(self, messages, tool_choice="none"):
        payload = {"model": OPENAI_MODEL,
                   "messages": [{"role": "system", "content": self.system_prompt}] + messages,
                   "temperature": 0.2, "max_tokens": 16384, "stream": True,
                   # 逼终答时**整段抽掉 tools**，不只是 tool_choice="none"：实测该端点
                   # 会无视 none 继续发调用（c01 在接地修正轮后又查了 6 轮、12 轮 92.6s）。
                   # 工具 schema 不在请求里，模型就无从调起——这是结构性的，不靠模型自觉。
                   **({"tools": self.fc_tools, "tool_choice": tool_choice}
                      if tool_choice != "none" else {}),
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
        """执行一次 MCP 工具调用并推送事件，返回 provider 原生的工具结果历史条目。
        参数完全相同的重复调用直接走缓存并回一条「别再查」的指令——轨迹里这类空转
        （同一批 format 查 3-4 遍）每次要烧掉一整轮模型延迟。"""
        name, args, call_id = call["name"], call["args"] or {}, call.get("id")
        self.emit({"type": "tool_call", "id": call_id, "name": name, "args": args})
        sig = name + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True)
        t0 = time.time()
        cached = sig in self.seen_calls
        if cached:
            self.dup_hits += 1
            result = dict(self.seen_calls[sig])
            result["_note"] = ("重复调用：本次参数与此前完全相同，返回同一结果。"
                               "不要再重复查询，用现有证据直接输出最终 JSON。")
            ok = True
        else:
            try:
                result = self.mcp.call(name, args)
                ok = not (isinstance(result, dict) and "error" in result)
            except Exception as e:
                result, ok = {"error": str(e)}, False
            if ok and isinstance(result, dict):
                self.seen_calls[sig] = result
        dur = int((time.time() - t0) * 1000)
        self.emit({"type": "tool_result", "id": call_id, "name": name, "cached": cached,
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
    # web 模型可见工具的两处过滤（MCP server 端一律保留，其他客户端不受影响）：
    #  · get_planning_guide：手册已内联进系统提示词，web 会话零调用
    #  · validate_plan：纯校验闸门、无信息产出，改由服务端在终答后自动跑——省掉模型
    #    自己调它的一整轮（轨迹里它还常被连调 2-3 次，每次一轮模型延迟）
    #    validate_atomic_chain 保留：它会返回 Knowledge Card 的 meta_id/槽位名，是信息源
    #  · hydrate_plan：确定性补全，同样由服务端在终答后自动跑，模型无需感知
    hidden = {"get_planning_guide", "validate_plan", "hydrate_plan"}
    model_tools = _slim_tools([t for t in tools if t["name"] not in hidden])
    server.fc_tools = (mcp_tools_to_gemini if LLM_PROVIDER == "gemini" else mcp_tools_to_openai)(model_tools)
    server.system_prompt = load_system_prompt()
    print(f"[web] Bio Pipeline Light Web 已启动: http://127.0.0.1:{PORT} "
          f"(provider={LLM_PROVIDER}, model={LLM_MODEL})", file=sys.stderr)
    threading.Thread(target=_warmup, args=(mcp, server.system_prompt), daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
