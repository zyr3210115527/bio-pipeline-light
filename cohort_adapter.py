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


def _fmt_alts(f):
    """卡片 format 的候选集，返回 (原格式集, 格式族集)。

    卡片允许把多种可接受格式写成一串：`FASTQ/FASTQ.gz`、`SAM/BAM/CRAM`、`VCF/VCF.gz`。
    整串当一个格式比，就永远比不上任何资产——0826 的卡里有 5 个**必填**参数是这种写法
    （fastqc.fastqs、samtools.alignment、snpeff.input_vcf 及其两个别名 id），
    结果是这几个工具的第一步恒报 no_confirmed_path，原子链必挂在开头。

    format 里还常带**修饰词**：`coordinate-sorted BAM`、`GATK interval_list`。整串比同样
    比不上——gatk 的 tumor_bam/normal_bam 写的就是 `coordinate-sorted BAM`，而上游
    samtools 的产出声明是干净的 `BAM`，于是 WES 工具链走到 GATK 那步必报
    no_confirmed_path（实测每次都报）。所以再补一层：拆出空格分隔的词，认识的格式词
    也算候选。只认 `_FMT_TOKENS` 里的已知格式词，不拿末尾词兜底——`RSEM index
    directory` 的末尾词是 directory，兜进来会让任意目录型参数互相乱绑。
    """
    raw = set()
    for x in str(f or "").split("/"):
        x = x.strip()
        if not x:
            continue
        raw.add(_norm_fmt(x))
        if " " in x or "-" in x:
            raw |= {t for t in (_norm_fmt(w) for w in re.split(r"[\s\-]+", x))
                    if t in _FMT_TOKENS}
    return raw, {_FMT_FAMILY.get(x, x) for x in raw}


# 参数名里的格式尾巴不参与语义匹配（clinical_xls 的判别信息是 clinical，不是 xls）
_FMT_TOKENS = {"file", "tsv", "csv", "xls", "xlsx", "txt", "bam", "bai", "vcf", "maf",
               "fastq", "fq", "gz", "rds", "h5ad", "path", "input", "matrix"}


def _param_tokens(name):
    """参数名里有判别力的词根（格式尾巴不算，见 `_FMT_TOKENS`）。"""
    toks = [t for t in re.split(r"[_\W]+", str(name or "").lower())
            if t and t not in _FMT_TOKENS]
    # WDL 写 read1/read2，图内文件名是 `_f1`/`_r2`、语义格式是 `..._R1_FASTQ`/`..._R2_FASTQ`，
    # 上游产物又叫 trimmed_r1/trimmed_r2。不折这一层，read1 和 read2 对一对 FASTQ
    # 都没有判别力，绑到哪条只看列举顺序——正反了执行端照跑不报错。
    return toks + [t.replace("read", "r") for t in toks if re.fullmatch(r"read\d", t)]


def _pick_upstream(upstream, param, used, is_arr=False):
    """给一个卡片参数挑上游产物（数组参数收全部同格式产物）。返回列表。

    只比格式是不够的——同一步常同时产出多个同格式产物。

    star 一步就产 genome_bam 和 transcriptome_bam 两个 BAM，只比格式的话
    rsem 的 transcriptome_bam 会绑到 genome_bam；trim_galore 的 trimmed_r1/r2 同理会让
    read2 也绑到 r1。两种都不报错，跑出来是错的结果。
    判据：参数名词根命中 > 更近的上游步骤 > 该步产出声明顺序（genome_bam 声明在
    transcriptome_bam 前，正是 featurecounts 这种只说要 "bam" 的默认取法）。
    """
    _, want = _fmt_alts(param.get("format"))
    toks = _param_tokens(param.get("name"))
    pool = []
    for u in upstream:
        if (u["step_id"], u["name"]) in used:
            continue
        if want and not (_fmt_alts(u["format"])[1] & want):
            continue
        pool.append(((0 if any(t in str(u["name"]).lower() for t in toks) else 1,
                      -u.get("step_no", 0), u.get("pos", 0)), u))
    pool.sort(key=lambda x: x[0])
    # 数组参数收全部：multiqc 的 qc_files 就是要把前面每一步的质控产物汇总成一份报告，
    # 只给一条等于报告里只剩最后一步。
    return [u for _, u in pool] if is_arr else [pool[0][1]] if pool else []


def _pick_assets(assets, used, param, is_arr):
    """给一个卡片参数挑资产。返回选中的资产列表（标量参数最多一个）。

    先按格式族过滤，再按「参数名词根是否出现在文件名里」排序——这一步是必须的：
    immune_infiltration_iobr 的 clinical_xls 和 metainfo_xlsx 在图里都是 .xlsx，
    只比格式的话两个参数会抢同一张表，另一个报缺，而执行端拿着临床表当元信息表跑
    是不会报错的（列名对不上才在流程内部炸，日志里看不出是绑错了）。
    """
    exact, want = _fmt_alts(param.get("format"))
    toks = _param_tokens(param.get("name"))
    pool = []
    for i, a in enumerate(assets):
        if a["asset_id"] in used:
            continue
        f = _norm_fmt(a.get("_fmt"))
        if want and _family(f) not in want:
            continue
        name_hit = any(t in str(a.get("file_name") or "").lower() for t in toks)
        sem_hit = any(t in str(a.get("semantic_format") or "").lower() for t in toks)
        pool.append(((0 if (name_hit or sem_hit) else 1, 0 if f in exact else 1, i), a))
    if not pool:
        return []
    pool.sort(key=lambda x: x[0])
    return [a for _, a in pool] if is_arr else [pool[0][1]]


def _real_path(a):
    p = str(a.get("file_path") or a.get("path") or "").strip()
    return p if p.startswith("/") and "NOT_FOUND" not in p else ""


# String 型标识参数（sample_id/pair_id/quant_type/group_a_samples…）的确定性解析已
# 收口到 mcp_light_server._resolve_id_params（两条路径共用）：按交付包实跑输入定口径，
# 从绑定文件与队列遍历产出，推不出的由调用方如实报缺（literal_required）。
# candidates 保底条数：一站式流程 + 原子链拆法。见 to_cohort_v2 结尾。
_CAND_CAP = 3


def _meta_param_fmt(srv, name):
    """卡片参数名 → 图内语义格式，覆盖「服务端按队列补」的两族元数据参数。

    XLSX 那对（`clinical_xls`/`metainfo_xlsx`，见 `srv._CLINICAL_PARAM_FMT`）与
    260902 起改吃 CSV 的那组（`individual_csv`/`sample_csv`/`t1_csv`，见
    `srv._CSV_META_PARAM_FMT`）。两族在卡片里互斥，合并成一张表查不会有歧义。"""
    return srv._CLINICAL_PARAM_FMT.get(name) or srv._CSV_META_PARAM_FMT.get(name)


def _add_clinical(srv, assets, study, sem_fmt):
    """把该队列的临床表/样本元信息表补进资产清单，返回新加的那一份（补不出返回 []）。

    这两张表在图内、每个队列各一份、都带真实 file_path，但手册要求调用方**不要**把它们
    写进 assets（写了会被接地校验判"与图内记录不符"），所以到这里 assets 里必然没有。
    六条非 bulk10 流程（driver_gene_gender_analysis / wgcna / her2_pfs_survival /
    immune_infiltration_iobr / survival_analysis / tmb_survival_analysis）把这一对写成
    必填输入——不补就必然两条 no_confirmed_path，整条推荐永远 needs_input。
    260902 起其中五条改吃三张 CSV（individual/sample/T1），同一套补全照用：
    `sem_fmt` 决定查哪一族，`_clinical_pair_files` 按族分组选目录。
    """
    if not study:
        return []
    try:
        pair = srv._clinical_pair_files(study, fmts={sem_fmt})
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


def _asset_roles(srv, assets, chain_facts=None):
    """{asset_id: 'tumor'/'normal'/None}：按文件样本事实判角色（`_file_sample_facts`，
    与 resolve_sample_roles 同一套规则）。chain_facts 已覆盖的文件直接复用，不重复查图；
    图不通时全部 None，由调用方退回不按角色过滤的原逻辑。"""
    fns = {a.get("file_name"): a["asset_id"] for a in assets
           if a.get("asset_id") and a.get("file_name")}
    facts = dict(chain_facts or {})
    need = [fn for fn in fns if fn not in facts]
    if need:
        try:
            facts.update(srv._file_sample_facts(need) or {})
        except Exception:
            pass                     # 图不通不该让绑定失败：角色全 None，走原逻辑
    return {aid: (facts.get(fn) or {}).get("role") for fn, aid in fns.items()}


def _bind_step(srv, gid, card, assets, upstream, step_id, study_hint=None, chain_facts=None):
    """把资产/上游产物绑到卡片参数上，返回 (inputs 绑定对象, missing[])。

    绑定优先级：上游步骤的同格式输出 > 尚未用掉的同格式资产 > 卡片默认值/可推字面量。
    参考资源（reference_resource）一律不绑——它走执行端容器内默认值，绑上去反而
    会覆盖掉正确的路径。bulk10 的两张 CNCB 元数据表由 server 按队列号推，同理不绑。
    """
    inputs, missing, used, used_up = {}, [], set(), set()
    _role_map = None               # 惰性：只在遇到 tumor_*/normal_* 槽位时查一次图
    _pair_fill = None              # 惰性：角色数组槽的队列级配对补全（_paired_bam_fill）
    # 队列的三个来源，按可信度排：资产自带 > 调用方给的推荐队列 > 从文件名里的队列号认领。
    study = (next((a.get("study_accession") for a in assets if a.get("study_accession")), None)
             or study_hint)
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
        if srv._is_reference_resource(card, name):
            # 容器内默认值，不该出现在提交合同里。这里**不能**再加 `is_file` 限定：
            # reference_resource 标记的判据是「artifact_type 是参考资源」或「默认值是
            # /opt/... 这样的容器内绝对路径」，跟 WDL 把它声明成 File 还是 String 无关。
            # bwa/bcftools/manta 的 reference_fasta 正是 type=String、标记为真的那一类，
            # 从前被 is_file 挡在门外，落到下面「非 File 参数」那条分支报
            # literal_required——0826 抽测 100 例里凭空多报 17 条。
            # 服务端两处同名检查（mcp_light_server.py:679/807）本来就没带这个限定。
            continue
        if gid in srv._BULK10 and name in ("sample_csv", "individual_csv"):
            continue                                   # server 侧按队列号推导，见 _bulk10_params
        if is_file:
            # 上游产物优先于原始资产（**顺序不能反**）：原子链里每一步都拿得到同一批
            # 资产，先挑资产就会把 star 的 read1 绑回原始 FASTQ，把上一步 trim_galore
            # 的产物丢掉——等于跳过了去接头，执行端照跑不报错。
            ups = _pick_upstream(upstream, p, used_up, is_arr)
            if ups:
                for u in ups:
                    used_up.add((u["step_id"], u["name"]))
                ref = [{"from": {"step_id": u["step_id"], "output": u["name"]}} for u in ups]
                inputs[name] = ref if is_arr else ref[0]
                continue
            # 角色槽位（tumor_*/normal_*）按图内样本角色过滤资产池：同格式数组槽
            # 纯按格式抢会错位——cnvkit 的四个 BAM/BAI 槽，先处理的 normal_* 把肿瘤
            # 文件也抢走，tumor_bams/tumor_bais 恒 no_confirmed_path（HRA000021 实测）。
            # 一个角色都判不出（图外/mock）时退回原池，不误伤。
            _role = next((r for r in ("tumor", "normal")
                          if str(name).lower().startswith(r + "_")), None)
            if _role:
                if _role_map is None:
                    _role_map = _asset_roles(srv, assets, chain_facts)
                pool_assets = ([a for a in assets
                                if _role_map.get(a["asset_id"]) == _role]
                               if any(_role_map.values()) else assets)
            else:
                pool_assets = assets
            cand = _pick_assets(pool_assets, used, p, is_arr)
            _meta_fmt = _meta_param_fmt(srv, name) if gid not in srv._BULK10 else None
            if not cand and _meta_fmt:
                cand = _add_clinical(srv, assets, study, _meta_fmt)
            if (not cand and _role and is_arr and study
                    and re.fullmatch(r"(tumor|normal)_(bams|bais)", str(name).lower())):
                # 角色数组槽是队列语义（要放几十对 BAM），调用方只给代表性资产时缺的一侧
                # 由服务端按队列同个体配对补齐——与 tool_validate_execution_chain 同一套
                # srv._paired_bam_fill。已绑进 inputs 的对侧槽位的 run 用来过滤配对集合，
                # 保证两侧数组按对平行；一侧都没绑则按队列全量配对（服务端已按上限截断）。
                if _pair_fill is None:
                    try:
                        _pair_fill = srv._paired_bam_fill(study) or {}
                    except Exception:
                        _pair_fill = {}          # 图不通：留空，走下面如实报缺
                if _pair_fill:
                    _bound_runs = set()
                    _by_id0 = {a.get("asset_id"): a for a in assets}
                    for _pn2, _bnd in inputs.items():
                        if not re.fullmatch(r"(tumor|normal)_(bams|bais)", str(_pn2).lower()):
                            continue
                        for _x in (_bnd if isinstance(_bnd, list) else [_bnd]):
                            _a = _by_id0.get((_x or {}).get("asset_id")) if isinstance(_x, dict) else None
                            _m = re.search(r"HRR\d+", str((_a or {}).get("file_name") or ""))
                            if _m:
                                _bound_runs.add(_m.group(0))
                    _keep = [j for j, (_t, _n) in enumerate(_pair_fill["pair_runs"])
                             if not _bound_runs or _t in _bound_runs or _n in _bound_runs]
                    cand = []
                    for _j in _keep:
                        _p = _pair_fill[name][_j]
                        _fn = _p.rsplit("/", 1)[-1]
                        if any(str(a.get("file_name")) == _fn for a in assets):
                            continue
                        _n0 = 1 + max([int(m.group(1)) for a in assets
                                       if (m := re.match(r"asset-(\d+)$",
                                                         str(a.get("asset_id") or "")))]
                                      or [len(assets)])
                        _item = {"asset_id": f"asset-{_n0}", "file_name": _fn,
                                 "path": _p, "file_path": _p,
                                 "artifact_type": _asset_artifact({"file_name": _fn}),
                                 "semantic_format": None, "study_accession": study,
                                 "sample_accession": None, "run_accession": None,
                                 "match_reason": f"配对角色槽服务端按队列 {study} 同个体配对补齐",
                                 "_fmt": _asset_artifact({"file_name": _fn})}
                        assets.append(_item)
                        cand.append(_item)
            if cand:
                for a in cand:
                    used.add(a["asset_id"])
                inputs[name] = ([{"asset_id": a["asset_id"]} for a in cand] if is_arr
                                else {"asset_id": cand[0]["asset_id"]})
                continue
            if _meta_fmt:
                # 这批元数据表是**服务端按队列补**的（上面 `_add_clinical` 刚试过）。
                # 补不出来只剩两种可能，都跟「图里有没有这个文件」无关，报
                # no_confirmed_path 是错的——实测调用方和读包的人都被这句误导过：
                #   · 队列还没定：调用方补上 assets（或直接给 study_accession）就能解；
                #   · 队列定了却没补上：那是服务端的补全该修，不是谁欠一份文件——这些表
                #     一直都在图里、带完整 file_path。
                # 分开报，转述时才知道该去补队列、还是该来报 bug。
                if required:
                    missing.append({"param": name, "tool_id": (card or {}).get("meta_id") or gid,
                                    "step_id": step_id,
                                    "reason": "study_not_resolved" if not study
                                              else "server_fill_missed"})
                continue
            if required:
                # 到这里是真正的主数据槽（表达矩阵/MAF/FASTQ）：绑不上就是数据侧的事，
                # 要补的是 `file_path`，no_confirmed_path 名副其实。
                missing.append({"param": name, "tool_id": (card or {}).get("meta_id") or gid,
                                "step_id": step_id, "reason": "no_confirmed_path"})
            continue
        # 非 File 参数
        if name in srv._ID_RESOLVABLE:
            # 标识参数等全部 File 槽位绑完后统一确定性解析（见下方第二遍）——第一遍边绑
            # 边推只能看到资产池，tumor_id/normal_id 会推成同一个（都是池里第一个带
            # accession 的资产）；且 T2 资产的样本归属要走 generated_from→T1→sample。
            continue
        if required:
            # 如实报缺。**不许拿交付包 example_inputs 里的值填**——那是别的队列跑过的
            # 分组，填上去执行端会照跑，结果是错的，而且一路绿灯没人发现。
            missing.append({"param": name, "tool_id": (card or {}).get("meta_id") or gid,
                            "step_id": step_id, "reason": "literal_required"})
    # 第二遍：String 型标识参数（sample_id/pair_id/tumor_id/group_*_samples…）的确定性
    # 解析，见 mcp_light_server._resolve_id_params。从「槽位→资产文件名」反推，
    # 角色参数与角色对应槽位对齐（tumor_id ← tumor_* 槽位绑定的文件）。
    if card:
        _by_id = {a["asset_id"]: a for a in assets}
        _bound = {}
        for _pn, _b in inputs.items():
            _items = _b if isinstance(_b, list) else [_b]
            _fns = [str((_by_id.get(x.get("asset_id")) or {}).get("file_name"))
                    for x in _items if isinstance(x, dict) and x.get("asset_id")]
            _fns = [f for f in _fns if f and f != "None"]
            if _fns:
                _bound[_pn] = _fns
        _id_vals, _id_missing = srv._resolve_id_params(gid, card, _bound, study,
                                                       chain_facts=chain_facts)
        for _n, _v in _id_vals.items():
            inputs[_n] = {"value": _v}
        for _n in _id_missing:
            missing.append({"param": _n, "tool_id": card.get("meta_id") or gid,
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


def _mk_assets(srv, raw, acc=None):
    """plan 里的 assets → PipelineBuilder 资产。缺真实绝对路径的一律标出来。

    **按 file_name 回图里补齐**，不只信调用方写了什么。手册明令模型「不要凭记忆写
    file_path」，所以 plan 里的 assets 经常只有文件名——hydrate_plan 补上了就有路径，
    没补上（图不通、或调用方直接拿模型原始 plan 过来）这里就是唯一一次补救机会。
    不补的话整条链恒 no_confirmed_path，而文件明明在图里躺着。
    同名跨队列时用 `acc` 消歧（见 mcp_light_server._node_rank）。
    顺手补 semantic_format：hydrate_plan 的 _ASSET_FIELDS 不含它（前端不用），但它是
    区分同扩展名不同用途的表（临床表 vs 样本元信息表）最硬的信号，绑定要靠它。
    一次批量往返，不逐个查。

    **调用方给了的字段一律不覆盖**——它可能特意选了同名文件里的另一份。
    """
    raw = raw or []
    need = [a.get("file_name") for a in raw
            if a.get("file_name") and not (a.get("semantic_format") and _real_path(a)
                                           and a.get("study_accession")
                                           and a.get("sample_accession"))]
    facts = {}
    if need:
        try:
            facts = srv._asset_facts(need, acc)
        except Exception:
            facts = {}                       # 图不通不该让整条翻译失败，退回按文件名匹配
    out = []
    for i, a in enumerate(raw, 1):
        fn = str(a.get("file_name") or "")
        f = facts.get(a.get("file_name")) or {}
        # 五个旧版工具的 Clinical/MetaInfo 走 analysis 目录那份，图内扁平目录下的同名
        # 新版表不能拿来顶（见 mcp_light_server._LEGACY5_PATHS / _legacy5_pair）。
        path = _real_path(a) or srv._LEGACY5_PATHS.get(fn) or _real_path(f)
        sem = a.get("semantic_format") or f.get("semantic_format")
        item = {"asset_id": f"asset-{i}",
                "file_name": a.get("file_name"),
                "path": path or None,           # PipelineBuilder 认 path
                "file_path": path or None,      # 兼容只读 file_path 的老代码
                "artifact_type": _asset_artifact(a),
                "semantic_format": sem,
                "study_accession": a.get("study_accession") or f.get("study_accession"),
                "sample_accession": a.get("sample_accession") or f.get("sample_accession"),
                "run_accession": a.get("run_accession") or f.get("run_accession"),
                "match_reason": a.get("match_reason"),
                "_fmt": _asset_artifact(a)}
        out.append(item)
    return out


def _strip(assets):
    return [{k: v for k, v in a.items() if not k.startswith("_")} for a in assets]


def _alt_gids(srv, assets, taken, n):
    """补位候选：挑「同一批数据还喂得进去」的闭集流程，按能吃下的必填槽位数排序。

    调用方（Cohort Agent）按固定三个候选位读结果，而原子链写不写、写几条完全由模型决定
    ——同一个问句实测三次只出现 0~1 次，少一位那边就是空白。这里做的是**补位**：
    排序质量不做要求，但形状必须是合同要求的那一套（真绑定、真路径、如实的
    feasibility），不能塞占位符——调用方会拿它去提交执行。
    纯格式比对不查图：补位不值得多一次往返。一个都比不上时按闭集固定顺序取，
    保证「有 rank1 就有 rank3」这条对外承诺不因数据情况而破例。
    """
    fam = {_family(a.get("_fmt")) for a in assets if a.get("_fmt")}
    scored, rest = [], []
    for gid, card in sorted((srv.KC_MAP or {}).items()):
        if gid in taken or (card or {}).get("meta_id") in taken or gid not in srv.CATALOG:
            continue
        req = [p for p in (card.get("inputs") or []) if p.get("required") and p.get("format")]
        hit = sum(1 for p in req if fam & _fmt_alts(p["format"])[1])
        (scored if hit else rest).append((-hit, gid))
    scored.sort()
    return [g for _, g in (scored + rest)[:max(0, n)]]


def _mk_filler(srv, gid, assets, rank):
    """把 `_alt_gids` 选出的流程建成一条候选，绑定与路径解析与 rank1 走同一套。"""
    card = srv.KC_MAP.get(gid)
    tool_id = (card or {}).get("meta_id") or gid
    inputs, missing = _bind_step(srv, gid, card, assets, [], "step-1")
    params, pmiss = _flat_params(srv, gid, inputs,
                                 {a["asset_id"]: a for a in assets}, tool_id, "step-1")
    missing = missing + pmiss
    has_path = any(a["path"] for a in assets)
    ready = has_path and not missing
    # 实跑黑/白名单闸门（与 validate_plan / validate_execution_chain 同一套）：
    # 补位候选同样不许荐出「跑挂过 / 未验证」的组合
    _acc = next((a["study_accession"] for a in assets if a.get("study_accession")), None)
    _blocked = srv._failed_run(gid, _acc) or srv._unproven_combo(gid, _acc)
    if _blocked:
        missing.append({"param": "*", "tool_id": tool_id, "step_id": "step-1",
                        "reason": "proven_run_blocked", "detail": _blocked})
        ready = False
    return {
        "rank": rank,
        "match_id": f"candidate-{rank}",
        "pipeline_id": tool_id,
        "validation_ok": ready,
        "feasibility_status": "ready" if ready else (
            "missing_data" if not has_path else "missing_inputs"),
        "study_accession": next((a["study_accession"] for a in assets
                                 if a.get("study_accession")), None),
        "assets": _strip(assets),
        "tool_chain": [{"step_id": "step-1", "tool_id": tool_id, "inputs": inputs}],
        "execution_params": params,
        "execution_params_missing": missing,
        # 如实标注来源：这条不是模型选的，是服务端为补足候选位挑的同数据可跑流程。
        # 调用方要区分「模型认为可选」和「服务端凑数」时看这个字段。
        "selection_source": "server_fill",
    }


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
    rank1_assets = []

    for i, rec in enumerate(recs, 1):
        pid = rec.get("pipeline_id") or (rec.get("tool") or {}).get("tool_id") or ""
        gid = meta_to_graph.get(pid, pid)
        card = srv.KC_MAP.get(gid)
        cat = srv.CATALOG.get(gid)
        data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
        # 声明了**恰好一个**队列才拿来消歧：多个队列说明这批 assets 本就混着队列，
        # 拿其中一个当锚点会把另一批的同名文件全体拽错边。
        accs = {str(s) for s in (data.get("study_accessions") or []) if s}
        assets = _mk_assets(srv, data.get("assets"),
                            next(iter(accs)) if len(accs) == 1 else None)
        if i == 1:
            rank1_assets = assets      # 补位候选复用这批已解析好的资产，见 `_alt_gids`

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

        # 队列号：卡片的 `study_accessions` 优先（模型点名了队列却没给资产时，这是唯一
        # 的锚）、其次资产反查。`_complete_assets` 会照着它把该队列的元数据表补齐——
        # 少了这一步，模型说「HRA016026 做 wgcna」而没给资产时，三张表全报
        # study_not_resolved，读起来像"缺数据"，其实只差把队列号交给补全。
        inputs, missing = _bind_step(srv, gid, card, assets, [], "step-1",
                                     next(iter(accs)) if len(accs) == 1 else None)
        # 路径解析：绑定对象里只有 asset_id，执行合同还要给出扁平的 execution_params
        params, pmiss = _flat_params(srv, gid, inputs,
                                     {a["asset_id"]: a for a in assets},
                                     tool["tool_id"], "step-1")
        missing.extend(pmiss)

        has_path = any(a["path"] for a in assets)
        rec["data"] = dict(data, status=("available" if has_path else "missing"),
                           assets=_strip(assets),
                           study_accessions=data.get("study_accessions")
                           or sorted({a["study_accession"] for a in assets if a["study_accession"]}),
                           source="neo4j")
        rec["execution_params"] = params
        rec["execution_params_missing"] = missing

        # 实跑黑/白名单闸门（与 validate_plan / validate_execution_chain 同一套）：
        # 明确跑挂过的组合、或有成功记录的工具选了表外队列，都不得标 ready——
        # 不然执行平台拿到的「可提交」合同里就混着已知跑不了的组合。
        _cand_acc = (next(iter(accs)) if len(accs) == 1 else None) or next(
            (a["study_accession"] for a in assets if a.get("study_accession")), None)
        _blocked = srv._failed_run(gid, _cand_acc) or srv._unproven_combo(gid, _cand_acc)
        if _blocked:
            missing.append({"param": "*", "tool_id": tool["tool_id"], "step_id": "step-1",
                            "reason": "proven_run_blocked", "detail": _blocked})
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

    # 模型给了原子链就用它的顺序，多步链按上游产物串起来。
    # 原子链是**同一个请求的另一种拆法**，数据仍是推荐里那一份，但模型极少在 candidates
    # 里把 assets 再抄一遍（它已经写在 recommendations[0].data.assets）。不兜这一层，
    # 原子链就恒是空壳：assets=[] → study 推不出 → 第一步文件槽 no_confirmed_path、
    # sample_id/report_id 也跟着 literal_required（这两个字面量本就是从资产的
    # sample_accession/study_accession 推的），整条永远 missing_data。
    #
    # **原子链无条件前置成 rank1，模型自己写的那份重复条目删掉。** 原子链和一站式推荐
    # 描述的是同一件事的两种粒度（`wgcna` 一条 vs `trim_galore → star → rsem`），并列成
    # 两条候选会被读成「两个互相独立的选择」，而模型写原子链的本意就是「这条按这个顺序
    # 执行」。分开的代价：一站式那条永远停在单步、串不起上游；原子链那条又因为脱离
    # 一站式而丢了队列关联。前置后 pipeline_id 仍是流程名，tool_chain 是展开后的完整
    # 步骤，调用方一次拿到「做什么 + 怎么做」。
    fallback = _rec_assets(recs)
    rec_pool = [r for r in recs if (r.get("data") or {}).get("assets")]
    atomic = None
    for c in out.get("candidates") or []:
        if _is_atomic_chain(c):
            atomic = c
            break
    if atomic:
        # 原子链自己是**没有队列**的：模型写它时往往只写 tool_chain，把队列留在
        # recommendations[0] 里。合并前先把那一条的 assets/study 借过来，否则前置之后
        # rank1 从"队列在、链路是单步"变成"链路全了、队列没了"——数据反而比不改更差。
        conv = _convert_atomic(srv, atomic, meta_to_graph, 0,
                               _rec_assets(rec_pool) or fallback,
                               _rec_study(rec_pool) or _rec_study(recs))
        one_stop = [c for c in candidates if c.get("pipeline_id")]
        # **能跑通的才前置。** 链和一站式流程描述同一件事的两种粒度，前置哪一条只看
        # 谁真的跑得起来：链 ready 就一定前置（这是模型拆链的本意）；链跑不通、而模型
        # 选的那条流程跑得通，就把流程留在 rank1——把一条缺数据/缺绑定的空壳顶上去，
        # 只会把模型原来那个能执行的答案挤到 rank2 之外。
        # 两边都跑不通时照样前置：这时候给的是"这条链要这么走、还差这些"，比一个单步
        # 流程名更有信息量，顶层状态也仍是模型自己判的 missing_from_graph。
        if conv and (conv.get("feasibility_status") == "ready"
                     or not any(c["feasibility_status"] == "ready" for c in one_stop)):
            steps = conv.get("tool_chain") or []
            step_ids = {s.get("tool_id") for s in steps}
            # 一站式候选若**被原子链覆盖**就并进原子链、不再单列。覆盖有两种写法：
            # 链里直接含这个流程名（`… → wgcna`），或者链止于它的上游（`… → rsem`
            # 而一站式是 wgcna）——后者是模型最常写的：它只展开"到 wgcna 得先做
            # 什么"，终点留在一站式里。两种都是同一件事，留着就是重复。
            def _covered(c):
                if c["pipeline_id"] in step_ids:
                    return True
                # 上游覆盖：一站式流程的**文件**输入槽正好是这条链的产物。
                # 这是模型最常写的形态——它只展开"到 wgcna 得先做什么"，终点留在一站式里，
                # 于是 `… → rsem` 和 `wgcna` 是同一条流水线的两截，留着 rank2 就是重复。
                gid = meta_to_graph.get(c["pipeline_id"], c["pipeline_id"])
                card = srv.KC_MAP.get(gid)
                slots = (srv._card_slots(card)[0] if card
                         else srv._graph_tool_io(gid)[0])
                need = [s for s in slots
                        if s.get("is_file") and not s.get("optional")
                        and not srv._is_reference_resource(card, s.get("name"))]
                if not need:
                    return False
                for s in need:
                    _, want = _fmt_alts(s.get("format"))
                    if not want:
                        continue        # 卡片没写格式，_pick_upstream 会放宽，这里同样放宽
                    if not (produced_up & want):
                        return False
                return True
            # 链上每一步产出过的格式族集合，供上面判上游覆盖。
            produced_up = set()
            for st in steps:
                g = meta_to_graph.get(st.get("tool_id"), st.get("tool_id"))
                for o in (srv.KC_MAP.get(g) or {}).get("outputs") or []:
                    _, fam = _fmt_alts(o.get("format"))
                    produced_up |= fam
            rest = [c for c in one_stop if not _covered(c)]
            conv["rank"] = 1
            conv["match_id"] = "candidate-1"
            # 一站式那条的 pipeline_id 是流程名，原子链没有——补上，调用方要靠它。
            if one_stop:
                conv["pipeline_id"] = conv.get("pipeline_id") or one_stop[0].get("pipeline_id")
            if not conv.get("study_accession"):
                conv["study_accession"] = next(
                    (c.get("study_accession") for c in one_stop if c.get("study_accession")), None)
            if not conv.get("assets"):
                conv["assets"] = next(
                    (c.get("assets") for c in one_stop if c.get("assets")), [])
            candidates = [conv] + rest
            for i, c in enumerate(candidates, 1):
                c["rank"] = i
                c.setdefault("match_id", f"candidate-{i}")

    # 候选位补足到 _CAND_CAP：有 rank1 就必须有 rank3（见 `_alt_gids`）。
    # 只在已经有真候选时补——拒答题的 candidates 本就该是空的，凑数等于把「这题不该做」
    # 变成「这题有三个方案」。
    if candidates and len(candidates) < _CAND_CAP:
        taken = {c.get("pipeline_id") for c in candidates}
        taken |= {s.get("tool_id") for c in candidates for s in (c.get("tool_chain") or [])}
        for gid in _alt_gids(srv, rank1_assets, taken, _CAND_CAP - len(candidates)):
            candidates.append(_mk_filler(srv, gid, rank1_assets, len(candidates) + 1))

    # 「不在白名单里的压根不返回」：命中实跑黑/白名单的候选直接从合同里拿掉，
    # 不是标个不可用——平台上「标出来但跑不了」和「能跑」长得太像，已经误导过一次。
    candidates = [c for c in candidates
                  if not any(m.get("reason") == "proven_run_blocked"
                             for m in (c.get("execution_params_missing") or []))]

    # candidates 的上限**不跟 top_k 走**。top_k 限的是推荐条数（light 严格 top-1，
    # 实际就 1 条），而 candidates 是「同一个请求的另几种拆法」——一站式流程 + 原子链。
    # 两者共用一个上限时，调用方传 top_k=1 会把原子链整条截掉，rank2/rank3 凭空消失。
    out["candidates"] = candidates[:max(_CAND_CAP, int(top_k or 0))] if candidates else []
    out["candidate_count"] = len(out["candidates"])
    out["recommendation_count"] = len(recs)
    # 补位候选不参与 ready 判定：它是服务端凑数挑的，不代表模型认为这题有解。
    # 算进来的话，rank1 明明缺数据、却因为某个同数据可跑的补位项 ready 而把整题标成
    # ready，调用方会直接提交执行。
    ready_any = any(c["feasibility_status"] == "ready" for c in out["candidates"]
                    if c.get("selection_source") != "server_fill")
    # 模型自己判的 `missing_from_graph` **优先于**这里算出来的 needs_input。它表达的
    # 不是"还差几个参数"，而是"这个队列在图里就没有这条流程要的主数据"——决定性判断，
    # 不是待补的输入。覆盖成 needs_input，调用方会去补 assets 重试，而资产层上一次
    # 已经"贴心"地跨队列递过别的 study 的文件（见 SKILL 里那条跨队列纪律）。
    declared = str(out.get("selection_status") or "").lower()
    if ready_any:
        out["selection_status"] = "ready"
    elif not out["candidates"]:
        st = declared
        if st not in ("information", "unsupported"):
            out["selection_status"] = "no_candidate"
        if out["selection_status"] != "information":
            # `information` 是知识问答，答案在 answer 里，本来就不该有推荐——
            # 给它安一个 unsupported_reason 会让调用方以为规划失败了。
            out.setdefault("unsupported_reason",
                           "闭集内没有可执行的候选：" + (out.get("answer") or "模型未给出推荐"))
    elif declared == "missing_from_graph":
        out["selection_status"] = "missing_from_graph"
        out.setdefault("unsupported_reason", "图内没有这条流程要的主数据；" + "；".join(
            f"{m['tool_id']}.{m['param']} ({m['reason']})"
            for c in out["candidates"] if c.get("selection_source") != "server_fill"
            for m in c["execution_params_missing"][:3]))
    else:
        out["selection_status"] = "needs_input"
        out.setdefault("unsupported_reason", "；".join(
            f"{m['tool_id']}.{m['param']} ({m['reason']})"
            for c in out["candidates"] for m in c["execution_params_missing"][:3]))
    out.setdefault("intent", {"query_text": query})
    out["data_matcher_mode"] = "neo4j"
    return out


def _flat_params(srv, gid, inputs, by_id, tool_id, step_id):
    """绑定对象（asset_id / value / from）→ 扁平 execution_params，返回 (params, missing)。

    `from` 是上游步骤产物，执行端自己串，这里不出参数。资产绑上了但没有真实路径的，
    如实报 no_confirmed_path——绑定成功不等于跑得动。
    """
    params, missing = {}, []
    def _miss(name):
        missing.append({"param": name, "tool_id": tool_id,
                        "step_id": step_id, "reason": "no_confirmed_path"})
    for name, b in (inputs or {}).items():
        if isinstance(b, dict) and "asset_id" in b:
            p = (by_id.get(b["asset_id"]) or {}).get("path")
            if p:
                params[name] = p
            else:
                _miss(name)
        elif isinstance(b, list):
            ids = [x for x in b if isinstance(x, dict) and "asset_id" in x]
            if not ids:
                continue                       # 整组都是上游产物，执行端自己串
            ps = [(by_id.get(x["asset_id"]) or {}).get("path") for x in ids]
            if all(ps) and len(ids) == len(b):
                params[name] = ps
            else:
                _miss(name)
        elif isinstance(b, dict) and "value" in b:
            params[name] = b["value"]
    if gid in srv._BULK10:
        # 十条 bulk10 的 sample_csv/individual_csv 由队列号推，路径不在图里
        params.update(srv._bulk10_params(gid, params.get("expr", ""), {}, []))
    return params, missing


def _rec_assets(recs):
    """推荐里已解析好的原始 assets，供原子链在自己没写 assets 时兜底。

    只在**队列唯一**时兜：多条推荐落在不同队列，说明拿哪一份给原子链是没依据的，
    硬塞一份等于替调用方选队列——宁可如实报缺。这与 `_node_rank` 的锚点纪律一致。
    """
    pools = [((r.get("data") or {}).get("assets") or []) for r in recs]
    pools = [p for p in pools if any(a.get("file_name") for a in p)]
    if not pools:
        return []
    accs = {str(a.get("study_accession") or "") for p in pools for a in p
            if a.get("study_accession")}
    return pools[0] if len(accs) <= 1 else []


def _is_atomic_chain(cand):
    """`candidates[]` 里这一条是**模型自己拆的原子链**吗？

    判据只有一条：它没有 `pipeline_id`——一站式推荐有（`wgcna`、`wes_somatic_pair` 这类
    流程名），原子链没有（它只是一串步骤）。

    注意**不能**靠「步骤带不带 `outputs`」区分。`validate_atomic_chain` 的返回也是
    同样形状的 `{"tool_chain": [{"tool_id", "inputs", "outputs"}]}`，看着像 IO 回显，但它
    正是模型拿到的**正确展开结果**（模型先传一版短名链，工具回一版补全后的完整链，
    模型再把这一版写进终答）。按 `outputs` 去挡，会把肺癌 wgcna 那条
    `trim_galore → star → rsem` 一起挡掉——rank1 从展开好的链退化回单步 `wgcna`，
    正是这条规则要避免的事。回显与真链形状相同，**形状上分不开，也不该分**。

    （真回显若混进来，它在 `_convert_atomic` 里会因为没有资产而算成 missing_data；
    那是资产层的事，见 `to_cohort_v2` 里借 `recommendations` 资产的兜底。）
    """
    return bool(cand.get("tool_chain") or cand.get("chain")) and not cand.get("pipeline_id")


def _rec_study(recs):
    """推荐里已判定的队列号，**只在唯一时**返回（与 `_rec_assets` 同一条纪律）。

    多条推荐落在不同队列 = 拿哪一份当锚是没依据的，宁可不给；给了就会把 A 队列的
    元数据表挂到 B 队列的链上，执行端照跑不报错。多队列写成
    `HRA001748;HRA001749` 时是一个字符串、仍算唯一，原样带回。"""
    accs = {str(s) for r in recs
            for s in ((r.get("data") or {}).get("study_accessions") or []) if s}
    return next(iter(accs)) if len(accs) == 1 else None


def _convert_atomic(srv, cand, meta_to_graph, rank, fallback=None, study_hint=None):
    """模型给出的原子链 candidates → 执行合同（步骤间按格式串上游产物）。

    `fallback` 是推荐里已解析好的 assets（见 `_rec_assets`）：原子链自己没写 assets 时
    用它，不然整条 rank2 是空壳。第一步之外的槽位本来就靠上游产物串，多出来的资产
    绑不上也不会乱绑——`_pick_assets` 按格式挑。
    """
    chain = cand.get("tool_chain") or cand.get("chain") or []
    if not chain:
        return None
    raw = ((cand.get("data") or {}).get("assets")) or cand.get("assets") or fallback
    accs = {str(s) for s in ((cand.get("data") or {}).get("study_accessions") or []) if s}
    accs |= {str(a.get("study_accession")) for a in (raw or []) if a.get("study_accession")}
    # `study` 是补元数据表的锚：链的每一步都要它。模型常把队列留在 recommendations 里、
    # 链里一个字不提，此时 `raw` 可能是空（调用方还没补数据），assets 里也就推不出队列——
    # 于是 `individual_csv/sample_csv/t1_csv` 报 study_not_resolved，整条链看着像缺数据，
    # 实际只缺一个调用方补得上的队列号。`study_hint` 就是那条推荐里已判定过的队列。
    study = next(iter(accs)) if len(accs) == 1 else (study_hint or None)
    assets = _mk_assets(srv, raw, study)
    by_id = {a["asset_id"]: a for a in assets}
    # 链级样本事实一次算好，逐步共享：中游步骤的文件输入是上游产物，自己没有图内文件
    # 可查，但同一条数据在链里流动，sample_id 等标识沿链一致（见 srv._resolve_id_params）。
    _chain_fns = [a["file_name"] for a in assets if a.get("file_name")]
    chain_facts = srv._file_sample_facts(_chain_fns) if _chain_fns else None
    steps, missing, upstream = [], [], []
    for idx, s in enumerate(chain, 1):
        tid = srv._step_tool_id(s) if isinstance(s, dict) else str(s)
        gid = meta_to_graph.get(tid, tid)
        card = srv.KC_MAP.get(gid)
        sid = f"step-{idx}"
        # 资产给每一步，不只给第一步。第一步之外靠上游串文件（上面那条优先级保证了
        # 不会绑回原始输入），但 sample_id/report_id 这类字面量是从资产的
        # sample_accession/study_accession 推的——不给资产就每步都 literal_required，
        # 一条六步的链能凭空多报四五条缺失。
        inputs, miss = _bind_step(srv, gid, card, assets, upstream, sid, study,
                                  chain_facts=chain_facts)
        tool_id = (card or {}).get("meta_id") or gid
        # 绑定对象里只有 asset_id，执行端要的是扁平路径——与 rank1 同一套解析。
        # 顺带堵一个洞：没有这一步，path 为空的资产也能安静绑上槽位，
        # 整条链照样报 ready（`has_path` 是 any，挡不住其中一份没路径）。
        params, pmiss = _flat_params(srv, gid, inputs, by_id, tool_id, sid)
        missing.extend(miss + pmiss)
        steps.append({"step_id": sid, "tool_id": tool_id,
                      "inputs": inputs, "execution_params": params})
        for pos, o in enumerate((card or {}).get("outputs") or []):
            if o.get("format"):
                upstream.append({"step_id": sid, "name": o["name"], "format": o["format"],
                                 "step_no": idx, "pos": pos})
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
