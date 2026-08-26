#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cohort Agent / PipelineBuilder 兼容层：把 light 的规划结果翻译成执行合同。

背景。Cohort Agent 原来接的是重版 knowledge-graph-mcp，一次
`route_pipeline_request(query, top_k, data_matcher_mode)` 就拿到完整方案。
light 版把规划推理搬到了调用方模型（server 只出手册 + 取数 + 确定性校验），
这个工具连同 rule_baseline_plan 一起下线了。本模块把「模型循环」包回 server 内部，
让只会调一次工具的客户端也能用 light——**不起嵌套 MCP 子进程**，直接调用
mcp_light_server 里的 tool_* 函数。

两件事：
  1. run_query()    复用 web/server.py 的 AgentRunner 跑完整循环。那套循环里压着
     长尾治理的全部成果（请求对冲、泄漏调用回收、reasoning 段捞回终答、取数预算
     硬收敛、服务端接地校验后的一轮修正）——另写一份必然退化成几个月前的 p99，
     所以宁可 import 它，也不复制一份出来各自漂移。
  2. to_cohort_v2() 把 light 的 tool-chain/v2 信封补成执行合同：asset_id/path、
     step_id/tool_id/inputs 绑定对象、tool.inputs[].builder_param、execution_params。

**不做规则降级**。模型或 Neo4j 不可用时返回合法的 `no_candidate` + `unsupported_reason`，
绝不回落到 mcp_light_server.RULES/_predict_baseline——那条路径在去名集上只有 1.4%，
静默降级比明说不可用危险得多。
"""

import json
import os
import re
import sys
import time

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
    system_prompt, fc_tools = _ctx()
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


# ---------------------------------------------------------------- 合同翻译 ----

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


# 格式族：只在「同一族但卡片和图内写法不同」时才放宽。交付卡按 WDL 参数名写死
# `clinical_xls`，图内那张表却是 .xlsx（CNCB 后来换的格式），严格比就永远绑不上。
# tsv/csv 不设族——bulk10 的 expr(TSV) 和 sample_csv(CSV) 是两份不同的表，混了就跑错。
_FMT_FAMILY = {"xls": "excel", "xlsx": "excel", "fastq": "fastq", "fq": "fastq"}


def _family(f):
    f = _norm_fmt(f)
    return _FMT_FAMILY.get(f, f)


# 参数名里的格式尾巴不参与语义匹配（clinical_xls 的判别信息是 clinical，不是 xls）
_FMT_TOKENS = {"file", "tsv", "csv", "xls", "xlsx", "txt", "bam", "bai", "vcf", "maf",
               "fastq", "fq", "gz", "rds", "h5ad", "path", "input", "matrix"}


def _pick_assets(assets, used, param, is_arr):
    """给一个卡片参数挑资产。返回选中的资产列表（标量参数最多一个）。

    先按格式族过滤，再按「参数名词根是否出现在文件名里」排序——这一步是必须的：
    immune_infiltration_iobr 的 clinical_xls 和 metainfo_xlsx 在图里都是 .xlsx，
    只比格式的话两个参数会抢同一张表，另一个报缺，而执行端拿着临床表当元信息表跑
    是不会报错的（列名对不上才在流程内部炸，日志里看不出是绑错了）。
    """
    want = _family(param.get("format"))
    toks = [t for t in re.split(r"[_\W]+", str(param.get("name") or "").lower())
            if t and t not in _FMT_TOKENS]
    pool = []
    for i, a in enumerate(assets):
        if a["asset_id"] in used:
            continue
        f = _norm_fmt(a.get("_fmt"))
        if want and _family(f) != want:
            continue
        name_hit = any(t in str(a.get("file_name") or "").lower() for t in toks)
        sem_hit = any(t in str(a.get("semantic_format") or "").lower() for t in toks)
        pool.append(((0 if (name_hit or sem_hit) else 1, 0 if f == _norm_fmt(param.get("format")) else 1, i), a))
    if not pool:
        return []
    pool.sort(key=lambda x: x[0])
    return [a for _, a in pool] if is_arr else [pool[0][1]]


def _real_path(a):
    p = str(a.get("file_path") or a.get("path") or "").strip()
    return p if p.startswith("/") and "NOT_FOUND" not in p else ""


# 可从资产确定性推出来的字面量参数。**这不是编造**：sample_id/pair_id 这类就是图内
# 记录的 accession，quant_type 就写在矩阵文件名里（HRA001272-Genes-TPM-1.0.tsv）。
# 推不出来的（group_a_samples 这种要人来分组的）一律如实报缺，不给猜的值。
_ID_PARAMS = {"sample_id", "sample_name", "pair_id", "dataset_id", "report_id",
              "sample_accession", "tumor_id", "normal_id", "output_prefix"}
_FLAVOR = re.compile(r"(?<![A-Za-z])(logCPM|FPKM|TPM|counts?)(?![A-Za-z])", re.I)


def _derive_literal(name, assets, study):
    """按参数名从已选资产推字面量；推不出返回 None。"""
    if name == "quant_type":
        for a in assets:
            m = _FLAVOR.search(str(a.get("file_name") or ""))
            if m:
                return m.group(1)
        return None
    if name not in _ID_PARAMS:
        return None
    if name in ("sample_id", "sample_name", "sample_accession", "tumor_id", "normal_id"):
        for key in ("sample_accession", "run_accession"):
            v = next((a.get(key) for a in assets if a.get(key)), None)
            if v:
                return str(v)
        return None
    # pair_id / dataset_id / report_id / output_prefix：队列号是稳定且有意义的标识
    return f"{study}_{name.rsplit('_', 1)[0]}" if study and name == "pair_id" else (study or None)


def _add_clinical(srv, assets, study, sem_fmt):
    """把该队列的临床表/样本元信息表补进资产清单，返回新加的那一份（补不出返回 []）。

    这两张表在图内、每个队列各一份、都带真实 file_path，但手册要求调用方**不要**把它们
    写进 assets（写了会被接地校验判"与图内记录不符"），所以到这里 assets 里必然没有。
    六条非 bulk10 流程（driver_gene_gender_analysis / wgcna / her2_pfs_survival /
    immune_infiltration_iobr / survival_analysis / tmb_survival_analysis）把这一对写成
    必填输入——不补就必然两条 no_confirmed_path，整条推荐永远 needs_input。
    """
    if not study:
        return []
    try:
        pair = srv._clinical_pair_files(study)
    except Exception:
        return []                    # 图不通不该让整条翻译失败，如实走报缺那条路
    hit = pair.get(sem_fmt)
    if not hit:
        return []
    fn, fp = hit
    if any(str(a.get("file_name") or "").lower() == fn.lower() for a in assets):
        return []
    n = 1 + max([int(m.group(1)) for a in assets
                 if (m := re.match(r"asset-(\d+)$", str(a.get("asset_id") or "")))] or [len(assets)])
    item = {"asset_id": f"asset-{n}", "file_name": fn, "path": fp, "file_path": fp,
            "artifact_type": _asset_artifact({"file_name": fn}),
            "semantic_format": sem_fmt, "study_accession": study,
            "sample_accession": None, "run_accession": None,
            "match_reason": f"流程声明需要 {sem_fmt}，按队列 {study} 由服务端补齐",
            "_fmt": _asset_artifact({"file_name": fn})}
    assets.append(item)
    return [item]


def _bind_step(srv, gid, card, assets, upstream, step_id):
    """把资产/上游产物绑到卡片参数上，返回 (inputs 绑定对象, missing[])。

    绑定优先级：上游步骤的同格式输出 > 尚未用掉的同格式资产 > 卡片默认值/可推字面量。
    参考资源（reference_resource）一律不绑——它走执行端容器内默认值，绑上去反而
    会覆盖掉正确的路径。bulk10 的两张 CNCB 元数据表由 server 按队列号推，同理不绑。
    """
    inputs, missing, used = {}, [], set()
    study = next((a.get("study_accession") for a in assets if a.get("study_accession")), None)
    if not study:
        # hydrate_plan 没跑成（图不通/模型直出）时字段是空的，但图内文件名本身就带队列号，
        # 从文件名兜一层——临床表补全全靠这个队列号，兜不到就只能如实报缺。
        study = next((m.group(0) for a in assets
                      if (m := srv._HRA.search(str(a.get("file_name") or "")))), None)
    for p in (card or {}).get("inputs") or []:
        name = p.get("name")
        typ = p.get("type") or "File"
        is_file = srv._is_file_type(typ)
        is_arr = srv._is_array_type(typ)
        required = bool(p.get("required"))
        if is_file and srv._is_reference_resource(card, name):
            continue                                   # 容器内默认值，不该出现在提交合同里
        if gid in srv._BULK10 and name in ("sample_csv", "individual_csv"):
            continue                                   # server 侧按队列号推导，见 _bulk10_params
        if is_file:
            cand = _pick_assets(assets, used, p, is_arr)
            if not cand and gid not in srv._BULK10 and name in srv._CLINICAL_PARAM_FMT:
                cand = _add_clinical(srv, assets, study, srv._CLINICAL_PARAM_FMT[name])
            if not cand:
                up = next((u for u in upstream
                           if _family(u["format"]) == _family(p.get("format"))), None)
                if up:
                    inputs[name] = {"from": {"step_id": up["step_id"], "output": up["name"]}}
                    continue
            if cand:
                for a in cand:
                    used.add(a["asset_id"])
                inputs[name] = ([{"asset_id": a["asset_id"]} for a in cand] if is_arr
                                else {"asset_id": cand[0]["asset_id"]})
                continue
            if required:
                # 临床表补不出来的原因分两种：队列还没定（调用方补 assets 就能解）vs
                # 队列定了但图里没这张表（数据侧的事）。混成一个 reason 会把前者误导成
                # "去数据侧要文件"——师兄 0826 反馈的正是这一条。
                missing.append({"param": name, "tool_id": (card or {}).get("meta_id") or gid,
                                "step_id": step_id,
                                "reason": "study_not_resolved"
                                          if (name in srv._CLINICAL_PARAM_FMT and not study)
                                          else "no_confirmed_path"})
            continue
        # 非 File 参数
        lit = _derive_literal(name, assets, study)
        if lit is not None:
            inputs[name] = {"value": lit}
        elif required:
            # 如实报缺。**不许拿交付包 example_inputs 里的值填**——那是别的队列跑过的
            # 分组，填上去执行端会照跑，结果是错的，而且一路绿灯没人发现。
            missing.append({"param": name, "tool_id": (card or {}).get("meta_id") or gid,
                            "step_id": step_id, "reason": "literal_required"})
    # 二选一约束：组内参数各自 required=false，只看必填查不出「一个都没给」。
    # bulk10 的两张 CNCB 元数据表由 server 按队列号推，含它们的组不算调用方欠的。
    for grp in (card or {}).get("require_any") or []:
        if gid in srv._BULK10 and set(grp) & {"sample_csv", "individual_csv"}:
            continue
        if not any(n in inputs for n in grp):
            missing.append({"param": "|".join(grp), "tool_id": (card or {}).get("meta_id") or gid,
                            "step_id": step_id, "reason": "require_any_unbound"})
    return inputs, missing


def _mk_assets(srv, raw):
    """plan 里的 assets → PipelineBuilder 资产。缺真实绝对路径的一律标出来。

    顺手补 semantic_format：hydrate_plan 的 _ASSET_FIELDS 不含它（前端不用），但它是
    区分同扩展名不同用途的表（临床表 vs 样本元信息表）最硬的信号，绑定要靠它。
    一次批量往返，不逐个查。
    """
    raw = raw or []
    need = [a.get("file_name") for a in raw
            if a.get("file_name") and not a.get("semantic_format")]
    facts = {}
    if need:
        try:
            facts = srv._asset_facts(need)
        except Exception:
            facts = {}                       # 图不通不该让整条翻译失败，退回按文件名匹配
    out = []
    for i, a in enumerate(raw, 1):
        path = _real_path(a)
        sem = a.get("semantic_format") or (facts.get(a.get("file_name")) or {}).get("semantic_format")
        item = {"asset_id": f"asset-{i}",
                "file_name": a.get("file_name"),
                "path": path or None,           # PipelineBuilder 认 path
                "file_path": path or None,      # 兼容只读 file_path 的老代码
                "artifact_type": _asset_artifact(a),
                "semantic_format": sem,
                "study_accession": a.get("study_accession"),
                "sample_accession": a.get("sample_accession"),
                "run_accession": a.get("run_accession"),
                "match_reason": a.get("match_reason"),
                "_fmt": _asset_artifact(a)}
        out.append(item)
    return out


def _strip(assets):
    return [{k: v for k, v in a.items() if not k.startswith("_")} for a in assets]


def to_cohort_v2(plan, query, top_k=3):
    """light 的 tool-chain/v2 → Cohort/PipelineBuilder 执行合同。

    只补字段、不改判断：选哪个流程、选哪份数据仍然全部来自模型 + 图谱，这里做的是
    把「IO 描述」翻成「执行绑定」，以及把 ready 与否算出来。
    """
    import mcp_light_server as srv
    out = dict(plan or {})
    out["schema_version"] = "tool-chain/v2"
    meta_to_graph = {c["meta_id"]: gid for gid, c in srv.KC_MAP.items() if gid != c["meta_id"]}

    recs = (out.get("recommendations") or [])[:max(1, int(top_k or 1))]
    out["recommendations"] = recs
    candidates = []

    for i, rec in enumerate(recs, 1):
        pid = rec.get("pipeline_id") or (rec.get("tool") or {}).get("tool_id") or ""
        gid = meta_to_graph.get(pid, pid)
        card = srv.KC_MAP.get(gid)
        cat = srv.CATALOG.get(gid)
        data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
        assets = _mk_assets(srv, data.get("assets"))

        # —— tool 块：Dingent 会按 catalog_status / builder_param 静默过滤 ——
        tool = dict(rec.get("tool") or {})
        tool["tool_id"] = (card or {}).get("meta_id") or pid
        tool["catalog_status"] = "registered" if cat else "unregistered"
        slots = tool.get("inputs")
        if not slots:
            slots = srv._card_slots(card)[0] if card else srv._graph_tool_io(gid)[0]
        # builder_param 就是卡片参数名——execution_params 的键与它必须逐字一致，
        # 对不上 Dingent 不报错，直接把整条推荐丢掉。
        tool["inputs"] = [dict(s, builder_param=s.get("name")) for s in slots]
        rec["tool"] = tool

        inputs, missing = _bind_step(srv, gid, card, assets, [], "step-1")
        # 路径解析：绑定对象里只有 asset_id，执行合同还要给出扁平的 execution_params
        by_id = {a["asset_id"]: a for a in assets}
        params = {}
        for name, b in inputs.items():
            if isinstance(b, dict) and "asset_id" in b:
                p = by_id[b["asset_id"]]["path"]
                if p:
                    params[name] = p
                else:
                    missing.append({"param": name, "tool_id": tool["tool_id"],
                                    "step_id": "step-1", "reason": "no_confirmed_path"})
            elif isinstance(b, list):
                ps = [by_id[x["asset_id"]]["path"] for x in b if by_id[x["asset_id"]]["path"]]
                if len(ps) == len(b):
                    params[name] = ps
                else:
                    missing.append({"param": name, "tool_id": tool["tool_id"],
                                    "step_id": "step-1", "reason": "no_confirmed_path"})
            elif isinstance(b, dict) and "value" in b:
                params[name] = b["value"]
        if gid in srv._BULK10:
            # 十条 bulk10 的 sample_csv/individual_csv 由队列号推，路径不在图里
            params.update(srv._bulk10_params(gid, params.get("expr", ""), {}, []))

        has_path = any(a["path"] for a in assets)
        rec["data"] = dict(data, status=("available" if has_path else "missing"),
                           assets=_strip(assets),
                           study_accessions=data.get("study_accessions")
                           or sorted({a["study_accession"] for a in assets if a["study_accession"]}),
                           source="neo4j")
        rec["execution_params"] = params
        rec["execution_params_missing"] = missing

        ready = bool(cat) and has_path and not missing
        candidates.append({
            "rank": i,
            "match_id": rec.get("match_id") or f"candidate-{i}",
            "pipeline_id": pid,
            "validation_ok": ready,
            "feasibility_status": "ready" if ready else (
                "missing_data" if not has_path else "missing_inputs"),
            "study_accession": (rec["data"]["study_accessions"] or [None])[0],
            "assets": _strip(assets),
            "tool_chain": [{"step_id": "step-1", "tool_id": tool["tool_id"], "inputs": inputs}],
            "execution_params": params,
            "execution_params_missing": missing,
        })

    # 模型给了原子链就用它的顺序，多步链按上游产物串起来
    for c in out.get("candidates") or []:
        conv = _convert_atomic(srv, c, meta_to_graph, len(candidates) + 1)
        if conv:
            candidates.append(conv)

    out["candidates"] = candidates[:max(1, int(top_k or 1))] if candidates else []
    out["candidate_count"] = len(out["candidates"])
    out["recommendation_count"] = len(recs)
    ready_any = any(c["feasibility_status"] == "ready" for c in out["candidates"])
    if ready_any:
        out["selection_status"] = "ready"
    elif not out["candidates"]:
        st = str(out.get("selection_status") or "").lower()
        if st not in ("information", "unsupported"):
            out["selection_status"] = "no_candidate"
        if out["selection_status"] != "information":
            # `information` 是知识问答，答案在 answer 里，本来就不该有推荐——
            # 给它安一个 unsupported_reason 会让调用方以为规划失败了。
            out.setdefault("unsupported_reason",
                           "闭集内没有可执行的候选：" + (out.get("answer") or "模型未给出推荐"))
    else:
        out["selection_status"] = "needs_input"
        out.setdefault("unsupported_reason", "；".join(
            f"{m['tool_id']}.{m['param']} ({m['reason']})"
            for c in out["candidates"] for m in c["execution_params_missing"][:3]))
    out.setdefault("intent", {"query_text": query})
    out["data_matcher_mode"] = "neo4j"
    return out


def _convert_atomic(srv, cand, meta_to_graph, rank):
    """模型给出的原子链 candidates → 执行合同（步骤间按格式串上游产物）。"""
    chain = cand.get("tool_chain") or cand.get("chain") or []
    if not chain:
        return None
    assets = _mk_assets(srv, ((cand.get("data") or {}).get("assets")) or cand.get("assets"))
    steps, missing, upstream = [], [], []
    for idx, s in enumerate(chain, 1):
        tid = srv._step_tool_id(s) if isinstance(s, dict) else str(s)
        gid = meta_to_graph.get(tid, tid)
        card = srv.KC_MAP.get(gid)
        sid = f"step-{idx}"
        inputs, miss = _bind_step(srv, gid, card, assets if idx == 1 else [], upstream, sid)
        missing.extend(miss)
        steps.append({"step_id": sid,
                      "tool_id": (card or {}).get("meta_id") or gid,
                      "inputs": inputs})
        for o in (card or {}).get("outputs") or []:
            if o.get("format"):
                upstream.append({"step_id": sid, "name": o["name"], "format": o["format"]})
    has_path = any(a["path"] for a in assets)
    ready = has_path and not missing
    return {"rank": rank,
            "match_id": cand.get("match_id") or f"candidate-{rank}",
            "validation_ok": ready,
            "feasibility_status": "ready" if ready else (
                "missing_data" if not has_path else "missing_inputs"),
            "study_accession": next((a["study_accession"] for a in assets
                                     if a["study_accession"]), None),
            "assets": _strip(assets),
            "tool_chain": steps,
            "execution_params_missing": missing}


def no_candidate(query, reason):
    """模型/图谱不可用时的合法空回包。**不走规则降级**。"""
    return {"schema_version": "tool-chain/v2",
            "selection_status": "no_candidate",
            "candidates": [], "recommendations": [],
            "candidate_count": 0, "recommendation_count": 0,
            "answer": reason,
            "unsupported_reason": reason,
            "intent": {"query_text": query},
            "data_matcher_mode": "neo4j",
            "planner_metadata": {"used": False, "reason": "upstream_unavailable",
                                 "planning_owner": "caller_model", "arch": "light"}}


def route(query, top_k=3, data_matcher_mode="neo4j"):
    """route_pipeline_request 的全部实现。返回顶层 tool-chain/v2，不再包一层信封。"""
    t0 = time.time()
    query = str(query or "").strip()
    if not query:
        return no_candidate(query, "query 为空")
    if data_matcher_mode and data_matcher_mode != "neo4j":
        # light 只有 neo4j 一种数据匹配后端；重版的 mock/offline 模式没有对应实现，
        # 静默当 neo4j 跑比假装支持安全，但要在回包里说清楚。
        pass
    try:
        plan, err = run_query(query)
    except Exception as e:
        return no_candidate(query, f"规划循环异常：{type(e).__name__}: {e}")
    if plan is None:
        return no_candidate(query, f"规划未完成：{err}")
    if plan.get("status") == "rejected":
        out = no_candidate(query, plan.get("reason") or "请求被拒绝")
        out["selection_status"] = "unsupported"
        out["rejected"] = plan
        return out
    try:
        out = to_cohort_v2(plan, query, top_k)
    except Exception as e:
        return no_candidate(query, f"合同翻译失败：{type(e).__name__}: {e}")
    if data_matcher_mode and data_matcher_mode != "neo4j":
        out["data_matcher_note"] = f"light 仅支持 neo4j 数据匹配，已忽略 {data_matcher_mode}"
    out["mcp_timing_ms"] = round((time.time() - t0) * 1000, 1)
    return out


if __name__ == "__main__":                      # 手工试跑：python3 cohort_adapter.py "问题"
    q = sys.argv[1] if len(sys.argv) > 1 else "我想对肝癌 bulk RNA-seq 做免疫浸润分析"
    print(json.dumps(route(q), ensure_ascii=False, indent=2))
