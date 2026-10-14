#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cohort Agent / PipelineBuilder 兼容层：一个问句换一份可直接提交的执行合同。

背景。Cohort Agent 原来接的是重版 knowledge-graph-mcp，一次
`route_pipeline_request(query, top_k, data_matcher_mode)` 就拿到完整方案。light 版把
规划推理搬到了调用方模型，本模块把「模型循环」包回 server 内部，让只会调一次工具的
客户端也能用——**不起嵌套 MCP 子进程**，直接调用 mcp_light_server 里的 tool_* 函数。

2026-10 起执行合同只从运行绑定表出（见 run_bindings.py）：
  1. 问句点名了流程（流程 id / 中文名 / 平台展示名 / WDL 名）→ 直接锁定，不经过模型。
     09-28 测试里 6 条「点名了还被换成别的流程」就出在这一步交给了模型。
  2. 单基因表达高低的问法 → gene_boxplot（演示要求，见 `_prefer_gene_boxplot`）。
  3. 其余问句 → run_query() 跑模型循环判断意图，模型只能在已绑定的流程与队列里选。
  4. 选定流程后，execution_params 原样取自绑定（Cromwell 跑通过的那套输入），
     不再查图现拼；流程或队列没有绑定就如实回 unsupported，附上可选队列。

**不做规则降级**。模型或 Neo4j 不可用时返回合法的 `no_candidate` + `unsupported_reason`，
绝不回落到 mcp_light_server.RULES/_predict_baseline——那条路径在去名集上只有 1.4%，
静默降级比明说不可用危险得多。（第 1 步认的是用户写出来的流程名本身，不是关键词猜意图。）
"""

import json
import os
import re
import sys
import time

import run_bindings as rb

HERE = os.path.dirname(os.path.abspath(__file__))


_web = None
_web_ctx = None      # (system_prompt, fc_tools)，进程内只算一次


def _load_web():
    """惰性 import web/server.py。

    放在函数里而不是模块顶层，有两个原因：mcp_light_server 常被当纯 MCP server 跑
    （web/server.py 会把它拉起来当子进程），那种场景根本用不到 LLM 循环，不该为它
    付 import 成本；而且 web/server.py 顶层会读 web/config.local 注入环境变量，
    在不需要 LLM 的进程里静默改环境不是好事。
    """
    global _web
    if _web is not None:
        return _web
    wd = os.path.join(HERE, "web")
    if wd not in sys.path:
        sys.path.insert(0, wd)
    import server as _s          # web/server.py：main() 有 __main__ 卫哨，import 不会起服务
    _web = _s
    return _web


class _LocalMcp:
    """AgentRunner 期望的 MCP 客户端接口（.tools() / .call()），但走进程内直调。

    web 层用的 McpClient 是 stdio 子进程；在 server 自己进程里再 spawn 一个自己
    是纯浪费（每次请求多一次 initialize 往返 + 一份 Neo4j 连接 + 一份卡片加载）。
    这里只要形状对得上就行。
    """

    def __init__(self, srv):
        self.srv = srv

    def tools(self):
        return [{"name": n, "description": t["description"], "inputSchema": t["inputSchema"]}
                for n, t in self.srv.TOOLS.items()]

    def call(self, name, arguments):
        fn = (self.srv.TOOLS.get(name) or {}).get("handler")
        if not fn:
            return {"error": f"未知工具: {name}"}
        try:
            return fn(arguments or {})
        except Exception as e:                      # 与 McpClient 的错误形状保持一致
            return {"error": f"{type(e).__name__}: {e}"}


class _CapturingRunner:
    """把 AgentRunner 的终答截下来。

    AgentRunner 本身是给 SSE 用的：结论通过 emit 事件流出去，run() 不返回东西。
    这里在 _finalize 之后接一手——它返回的 clean 已经是 hydrate + 接地校验之后的
    那份裸 JSON，正是我们要的。用子类而不是解析 emit 事件流：事件里 text_reset/text
    的拼接语义是给前端增量渲染用的，反向重建终答很容易错。
    """

    def __new__(cls, web, *a, **kw):
        base = web.AgentRunner
        impl = type("_Capturing", (base,), {
            "_finalize": lambda self, hist, txt, committed: cls._finalize(base, self, hist, txt, committed),
        })
        return impl(*a, **kw)

    @staticmethod
    def _finalize(base, self, hist, answer_text, committed_text):
        clean, verdict = base._finalize(self, hist, answer_text, committed_text)
        if verdict != "repair":
            self.final_text = clean
        return clean, verdict


def _ctx():
    """系统提示词与模型可见工具列表：与 web 层同源，且同样过滤掉 route_pipeline_request。

    过滤是必须的——把这个工具暴露给循环内的模型，它调一次就再起一整轮模型循环，
    嵌套下去没有底。web/server.py 的 hidden 集里也加了它，两处都要有：这里管
    route_pipeline_request 自己起的循环，那里管网页会话。
    """
    global _web_ctx
    if _web_ctx is not None:
        return _web_ctx
    web = _load_web()
    import mcp_light_server as srv
    hidden = {"get_planning_guide", "validate_plan", "hydrate_plan", "route_pipeline_request"}
    tools = [t for t in _LocalMcp(srv).tools() if t["name"] not in hidden]
    conv = web.mcp_tools_to_gemini if web.LLM_PROVIDER == "gemini" else web.mcp_tools_to_openai
    _web_ctx = (web.load_system_prompt(), conv(web._slim_tools(tools)))
    return _web_ctx


def run_query(query, session_id=None):
    """跑一次完整的规划循环，返回 (plan_dict, error_or_None)。"""
    web = _load_web()
    import mcp_light_server as srv
    system_prompt, fc_tools = _ctx()        # 已含运行绑定清单（web.load_system_prompt 拼的）
    sid = session_id or f"route-{os.getpid()}-{time.time_ns()}"
    runner = _CapturingRunner(web, _LocalMcp(srv), fc_tools, system_prompt, sid, lambda ev: None)
    runner.final_text = ""
    err = None
    try:
        runner.run(query)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    finally:
        with web.SESSIONS_LOCK:                 # 一次性会话，别把历史留在进程里
            web.SESSIONS.pop(sid, None)
    if not runner.final_text:
        return None, err or "模型未给出最终答案"
    obj = web._extract_json_obj(runner.final_text)
    if not isinstance(obj, dict):
        return None, err or "模型终答不是合法 JSON 对象"
    return obj, err


# ---------------------------------------------------------------- 资产形态 ----

# 资产 artifact_type：PipelineBuilder 认的是小写扩展名族，不是图内的 semantic_format
# （RAW_PAIRED_END_R1_FASTQ 这种它不认）。图内 format 属性有一半是 null（T1 节点普遍不填），
# 所以扩展名要能从文件名兜回来。
_EXT_ARTIFACT = [
    (".fastq.gz", "fastq"), (".fq.gz", "fastq"), (".fastq", "fastq"), (".fq", "fastq"),
    (".vcf.gz", "vcf"), (".vcf", "vcf"), (".tbi", "tbi"), (".bam", "bam"), (".bai", "bai"),
    (".cram", "cram"), (".crai", "crai"), (".maf", "maf"), (".gtf", "gtf"), (".bed", "bed"),
    (".fasta", "fasta"), (".fa", "fasta"), (".fai", "fai"), (".dict", "dict"),
    (".h5ad", "h5ad"), (".rds", "rds"), (".xlsx", "xlsx"), (".xls", "xls"),
    (".tsv", "tsv"), (".csv", "csv"), (".txt", "txt"), (".interval_list", "interval_list"),
]

# 卡片 format → artifact 族（卡片写 FASTQ.gz / TSV / XLS 这类大写格式名）
def _norm_fmt(v):
    v = str(v or "").strip().lower().lstrip(".")
    return {"fastq.gz": "fastq", "fq.gz": "fastq", "fq": "fastq",
            "vcf.gz": "vcf", "tar.gz": "tar.gz"}.get(v, v)


def _asset_artifact(a):
    fn = str(a.get("file_name") or a.get("file_path") or "").lower()
    for ext, art in _EXT_ARTIFACT:
        if fn.endswith(ext):
            return art
    return _norm_fmt(a.get("format")) or None


# 问句里的目标基因识别（单基因表达问题判定用）：ENSG 号或全大写基因符号
# （MIR503HG/TP53/BRCA1…）。HRA 队列号与常见缩写不算基因。
_GENE_TOKEN = re.compile(r"(?<![A-Za-z0-9])(ENSG\d{5,}|[A-Z][A-Z0-9\-]{2,9})(?![A-Za-z0-9])")
_GENE_STOP = {"GO", "KEGG", "TPM", "FPKM", "CNV", "OS", "PFS", "TMB", "WES", "WGS",
              "RNA", "DNA", "GSEA", "UMAP", "WGCNA", "KM", "MAF", "VCF", "BAM", "BAI",
              "BED", "TSV", "CSV", "CNCB", "HER2", "ERBB2", "QC", "DE", "GTF", "RDS"}


def _query_gene(q):
    """从问句里取第一个像基因名的 token；取不到返回 None。"""
    for m in _GENE_TOKEN.finditer(str(q or "")):
        t = m.group(1)
        if t.startswith("HRA") or t in _GENE_STOP:
            continue
        return t
    return None


def _prefer_gene_boxplot(query):
    """单基因表达高低优先箱线图；共表达模块/功能问题保留 WGCNA。"""
    text = str(query or "")
    if re.search(r"共表达|模块|co[ -]?expression|module|WGCNA", text, re.I):
        return False
    return bool(_query_gene(text) and re.search(r"表达|expression|上调|下调", text, re.I))


def no_candidate(query, reason):
    """模型/图谱不可用时的合法空回包。**不走规则降级**。"""
    return {"schema_version": "tool-chain/v2",
            "selection_status": "no_candidate",
            "candidates": [], "recommendations": [],
            "candidate_count": 0, "recommendation_count": 0,
            "answer": reason,
            "unsupported_reason": reason,
            "intent": {"query_text": query},
            "data_matcher_mode": "run_binding",
            "planner_metadata": {"used": False, "reason": "upstream_unavailable",
                                 "planning_owner": "caller_model", "arch": "light"}}


# 知识类问法（「什么是 STAR」「gatk 支持哪些输入格式」）提到流程名不等于要跑它：不走点名锁定，
# 交给模型。只认明确的提问句式，且句子里没有「分析/跑/运行/数据」这类执行意图词。
_KNOWLEDGE_Q = re.compile(r"什么是|是什么|是啥|支持哪些|需要哪些|有哪些(参数|输入|输出|格式)|原理|区别|怎么理解")
_RUN_INTENT = re.compile(r"分析|跑|运行|执行|提交|数据|队列|HRA\d{6}", re.I)


def _is_knowledge_question(query):
    return bool(_KNOWLEDGE_Q.search(query) and not _RUN_INTENT.search(query))


def _bound_list():
    return "、".join(f"{g}（{rb.name_cn(g)}）" for g in rb.bound_tools())


def route(query, top_k=3, data_matcher_mode="neo4j", plan_fn=None):
    """route_pipeline_request 的全部实现。返回顶层 tool-chain/v2，不再包一层信封。

    `plan_fn` 只给测试替换模型循环用，默认就是 run_query。
    """
    t0 = time.time()
    query = str(query or "").strip()
    if not query:
        return no_candidate(query, "query 为空")

    kind, val = (None, None) if _is_knowledge_question(query) else rb.resolve_tool(query)
    if kind == "tool":
        return rb.route_tool(val, query, "explicit_name", top_k=top_k, t0=t0)
    if kind == "platform_only":
        return rb.unsupported(query, f"「{val}」是平台上的流程，本系统还没有接入，暂时无法规划和执行。", t0=t0)
    if kind == "unknown":
        return rb.unsupported(query, f"流程目录里没有「{val}」。目前有已验证可运行数据的流程：{_bound_list()}", t0=t0)
    if _prefer_gene_boxplot(query) and rb.bindings_for("gene_boxplot"):
        return rb.route_tool("gene_boxplot", query, "gene_expression_priority", top_k=top_k, t0=t0)

    try:
        plan, err = (plan_fn or run_query)(query)
    except Exception as e:
        return no_candidate(query, f"规划循环异常：{type(e).__name__}: {e}")
    if plan is None:
        return no_candidate(query, f"规划未完成：{err}")
    if plan.get("status") == "rejected":
        out = rb.unsupported(query, plan.get("reason") or "请求被拒绝", t0=t0)
        out["rejected"] = plan
        return out
    recs = plan.get("recommendations") or []
    if not recs:
        status = str(plan.get("selection_status") or "").lower()
        out = no_candidate(query, plan.get("answer") or "模型未给出推荐")
        if status == "information":
            # 知识问答：答案在 answer 里，本来就不该有推荐，也不该带 unsupported_reason
            out["selection_status"] = "information"
            out.pop("unsupported_reason", None)
        out["mcp_timing_ms"] = round((time.time() - t0) * 1000, 1)
        return out
    rec = recs[0]
    gid = rb.tool_id_of(rec.get("pipeline_id") or (rec.get("tool") or {}).get("tool_id"))
    if not gid:
        return rb.unsupported(query, f"模型推荐的流程「{rec.get('pipeline_id')}」不在流程目录里", t0=t0)
    hint = (rec.get("data") or {}).get("study_accessions") or []
    out = rb.route_tool(gid, query, "model", hint_studies=hint, top_k=top_k, t0=t0)
    if plan.get("answer"):
        out["model_answer"] = plan["answer"]
    return out


if __name__ == "__main__":                      # 手工试跑：python3 cohort_adapter.py "问题"
    q = sys.argv[1] if len(sys.argv) > 1 else "我想用癌症数据进行manta_structural_variants分析"
    print(json.dumps(route(q), ensure_ascii=False, indent=2))
