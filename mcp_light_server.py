#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp_light_server.py — 轻架构 stdio MCP server（无第三方依赖）

交付形态 = skill + MCP：**推理只能来自调用方的模型**，本 server 只提供知识与确定性校验，
不存在任何规则规划路径（词表基线仅存在于 benchmark 对照臂与 light_router.py，不暴露为 MCP 工具）：

  get_planning_guide()      → 返回 SKILL.md 全文（调用方模型自己读、自己规划）
  read_cypher(query)        → 数据面：通用只读 Cypher 查询（只读守卫 + 患者隐私守卫 + 自动 LIMIT）
  resolve_sample_roles(...) → 确定性样本角色判定（tumor/normal，规则移植自重版，不许模型猜）
  validate_atomic_chain(chain) → 确定性闭集校验（11 个 atomic 工具 + 图内 next_tool 邻接）
  validate_execution_chain(steps) → 提交前把关，输出 execution_params / submittable
  validate_plan(plan)       → 接地校验：整份 Plan 的名词逐一到图/目录核验，防模型编造
  health_check()            → Neo4j 连通与图谱规模

目录数据（tool_catalog.csv）启动时从 skill/references/ 读取，不内嵌拷贝。
用法：export NEO4J_USER=neo4j NEO4J_PASSWORD=<密码> && python3 mcp_light_server.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
NEO4J_URL = os.environ.get("NEO4J_URL", "http://127.0.0.1:7474/db/neo4j/tx/commit")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")
SKILL_REF = os.environ.get("BIO_SKILL_REF", os.path.join(HERE, "skill", "references"))
SKILL_MD = os.environ.get("BIO_SKILL_MD", os.path.join(os.path.dirname(SKILL_REF), "SKILL.md"))
_SAFE_TOKEN = re.compile(r"^[a-zA-Z0-9_\-\u4e00-\u9fff ]+$")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9._\-]+$")   # \u56fe\u5185 file_name / accession \u767d\u540d\u5355
KC_MAP: dict = {}   # graph tool_id -> Knowledge Card（meta.id + 卡内 IO 名）

def load_knowledge_cards() -> None:
    """加载 skill/references/knowledge_cards_map.json：graph tool_id -> card。"""
    global KC_MAP
    path = os.path.join(SKILL_REF, "knowledge_cards_map.json")
    if not os.path.exists(path):
        return
    try:
        cards = json.load(open(path))
    except Exception:
        return
    for card_id, c in cards.items():
        gid = c.get("graph_tool_id") or card_id
        KC_MAP[gid] = {"meta_id": card_id,
                       "inputs": c.get("inputs", []),
                       "outputs": c.get("outputs", [])}
        if gid != card_id:
            KC_MAP.setdefault(card_id, KC_MAP[gid])

load_knowledge_cards()

# ---------- 目录加载（从 skill/references/tool_catalog.csv，不内嵌） ----------
ATOMIC_IDS: set[str] = set()
CATALOG: dict[str, dict] = {}

def load_catalog() -> None:
    global ATOMIC_IDS, CATALOG
    path = os.path.join(SKILL_REF, "tool_catalog.csv")
    if not os.path.exists(path):
        return
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tid = (row.get("tool_id") or "").replace("tool_id:", "")
            kind = row.get("tool_kind") or ""
            if not tid:
                continue
            CATALOG[tid] = {
                "tool_id": tid,
                "catalog_id": row.get("catalog_id") or "",
                "tool_kind": kind,
                "tool_name": row.get("tool_name") or tid,
                "description": (row.get("description") or "").strip(),
                "input_format": row.get("input_format") or "",
                "output_format": row.get("output_format") or "",
                "omics": row.get("omics") or "",
            }
            if kind == "atomic" and tid != "multiqc":
                ATOMIC_IDS.add(tid)

load_catalog()

# ---------- 样本角色推断（确定性知识，移植自重版 pipeline_router._sample_role） ----------
# 角色判定必须确定、可审计（tumor/normal 弄反 = 配对分析出错），因此放在 server 而不是让模型猜。
STUDY_ROLE_OVERRIDES: dict = {
    # HRA000071（胶质瘤）：286 个 T_ 组织标 Tumor 没问题，286 个血样的 tissue_type
    # 却分裂成 104 Tumor / 182 Normal。血样在该研究里是配对对照，按 specimen 统一判。
    "HRA000071": ("specimen_type", {"blood": "normal", "patient solid tissue": "tumor"}),
    # HRA016026：700 个样本的 tissue_type 全是多值 'Tumor,Normal'——上游把个体层面
    # 两个样本的取值并进了同一个格子，逐样本看等于没有信息，默认规则一个都判不出来，
    # 整个队列 role_resolved=false 被拒。但 sample_name 是干净的：350 个 L####_Tumor
    # + 350 个 L####_Normal，且 350 个个体各正好 2 个样本，是一个完整的配对队列
    # （0821 实测 350/350 成对）。这是图里最大的一个可配对队列，不救回来
    # wes_somatic_pair 这类需求会白白错过它。按名字后缀判，不碰 tissue_type。
    "HRA016026": ("name_suffix", {"_tumor": "tumor", "_normal": "normal"}),
}
SAMPLE_ROLE_LABELS = {"tumor": "肿瘤样本（实验组）", "normal": "正常样本（对照组）"}

def sample_role(record: dict):
    """推断样本角色；推不出返回 None，不猜。聚合类文件（表达矩阵/MAF/临床表）本无单样本角色。"""
    study = str(record.get("study_accession") or "").strip()
    rule = STUDY_ROLE_OVERRIDES.get(study)
    if rule:
        kind, mapping = rule
        if kind == "study_constant":
            return str(mapping)
        if kind == "specimen_type":
            # 0819 图谱清洗把空格规范成下划线（Patient_Solid_Tissue），归一化后新旧取值都能命中
            sp = str(record.get("specimen_type") or record.get("specimen_types") or "").strip().lower().replace("_", " ")
            return mapping.get(sp)
        if kind == "name_suffix":
            # 长后缀优先，"_Normal" 才不会被 "N" 抢走（各 study 命名习惯不同，规则随 study 配）
            name = str(record.get("sample_name") or "").strip().lower()
            for suffix in sorted(mapping, key=len, reverse=True):
                if name.endswith(str(suffix).lower()):
                    return mapping[suffix]
        return None
    return {"tumor": "tumor", "normal": "normal"}.get(str(record.get("tissue_type") or "").strip().lower())

# ---------- Neo4j 数据面（curl，只读守卫 + 隐私守卫） ----------
_WRITE_RE = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET\s|REMOVE|DROP|DETACH|FOREACH|LOAD\s+CSV)\b"
    r"|CALL\s+dbms\.|db\.create|apoc\.(?:load|export|cypher|trigger)",
    re.IGNORECASE)
# individual 的编号前缀属性里，除 `00_`（操作性标识：sample/run/project 编号、平台、
# 建库策略等，规划要靠它们连数据）之外**全是患者级敏感数据**：01_ 人口学、02_ 家族史、
# 03_ 生活史、04_ 血液学指标、09_ 肿瘤病理、10_ 侵犯情况、11_ 分子指标、12_ 治疗史、
# 13_ 生存。规划只允许聚合统计或存在性判断，不允许取个体值。
# 0821 实测：此前只列了 01/03/09/11/13，漏掉的 02/04/10/12 能直接查出个体级治疗方案
# （"HRI264436 → 3+7 regimen"）、脉管侵犯、家族史——覆盖范围必须按前缀区间取，
# 不能靠手工枚举，否则上游一加编号就又漏一类。
# 前缀必须落在属性名开头（`(?<![\w])`）：不加这条时 `04_platelet_count_109_l` 里的
# `09_l` 会被当成 09_ 病理属性误杀，而 04_ 本身反倒漏网。
_SENSITIVE_PROP = r"`?(?<![\w])(?:0[1-9]|1[0-3])_\w+`?"
_SENSITIVE_RE = re.compile(_SENSITIVE_PROP)
_ALLOW_NULLCHECK_RE = re.compile(
    rf"(?:[\w.]+\.)?{_SENSITIVE_PROP}\s+IS\s+(?:NOT\s+)?NULL", re.IGNORECASE)
_ALLOW_AGG_RE = re.compile(
    r"\b(?:count|avg|sum|min|max|stdev\w*|percentile\w*)\s*\((?:[^()]|\([^()]*\))*\)",
    re.IGNORECASE)

def _assert_read_only(query):
    if _WRITE_RE.search(query):
        raise ValueError("read_cypher 只允许只读查询（检测到写入语句）")

def _assert_privacy(query):
    """患者级临床属性只许聚合/存在性判断：去掉允许形态后仍出现敏感属性 → 拒绝。
    另防整节点绕过：individual 变量禁止 properties()/keys()/动态下标/整体 RETURN。
    说明：正则守卫是尽力而为的纵深防御层，主防线是调用方模型的拒绝纪律与部署信任边界。"""
    residual = _ALLOW_NULLCHECK_RE.sub(" ", query)
    residual = _ALLOW_AGG_RE.sub(" ", residual)
    hit = _SENSITIVE_RE.search(residual)
    if hit:
        raise ValueError(
            f"read_cypher 隐私守卫：{hit.group(0)} 是患者级临床属性（01_人口学/02_家族史/"
            "03_生活史/04_血液学/09_病理/10_侵犯/11_分子指标/12_治疗史/13_生存），"
            "只允许聚合统计（count/avg/min/max…）或存在性判断（IS NOT NULL），"
            "不允许返回或按值筛选个体数据。请改写为聚合查询，或直接拒绝用户的隐私问询。")
    # individual 绑定变量：inline 标签 (i:individual)、WHERE 标签谓词 n:individual、
    # 以及 -[:in_individual]->(x) 这种目标端不写标签的写法（不认这两种就能整节点导出）
    ind_vars = set(re.findall(r"(?<![\w.])(\w+)\s*:\s*individual\b", query, re.IGNORECASE))
    ind_vars |= set(re.findall(r"-\s*\[[^\]]*in_individual[^\]]*\]\s*->\s*\(\s*(\w+)",
                               query, re.IGNORECASE))
    ind_vars.discard("")
    # 别名追踪到不动点：collect(i) AS c / i{.*} AS m / i AS z 再 z AS y 都要跟上。
    # 只在 WITH/RETURN 的投影项里找，且逐项按逗号切——否则 `MATCH (i:individual)
    # RETURN i.individual_accession AS acc` 会从标签声明处一路匹配到 acc，把正常查询误杀。
    # 先抹掉 count(i)/id(i) 这类合法聚合，否则 `RETURN count(i) AS n` 也会被误判成导出。
    scrub = re.sub(r"\b(?:count|id|elementId)\s*\(\s*(?:DISTINCT\s+)?\w+\s*\)", " ",
                   query, flags=re.IGNORECASE)
    items = []
    for mm in re.finditer(r"\b(?:WITH|RETURN)\b(.*?)(?=\b(?:MATCH|OPTIONAL|WHERE|UNWIND|CALL|"
                          r"WITH|RETURN|UNION|ORDER|SKIP|LIMIT)\b|$)", scrub, re.IGNORECASE | re.S):
        items += mm.group(1).split(",")
    for _ in range(4):
        grew = False
        for item in items:
            alias = re.search(r"\bAS\s+(\w+)\s*$", item.strip(), re.IGNORECASE)
            if not alias or alias.group(1) in ind_vars:
                continue
            for v in ind_vars:
                # v 后面不能跟 . 或 : —— 点取字段和标签声明都不算整节点别名
                if re.search(rf"(?<![\w.]){re.escape(v)}(?![\w.:])", item):
                    ind_vars.add(alias.group(1))
                    grew = True
                    break
        if not grew:
            break
    for v in ind_vars:
        if re.search(rf"\b(?:properties|keys)\s*\(\s*{v}\b", query, re.IGNORECASE) \
                or re.search(rf"\b{v}\s*\[", query):
            raise ValueError(
                f"read_cypher 隐私守卫：禁止对 individual 节点（变量 {v}）使用 properties()/keys()/"
                "动态属性访问——这会导出患者级临床属性。请显式点取非临床字段（如 individual_accession）。")
        # RETURN 段禁止整节点导出（count(v)/id(v) 允许；v.prop 点取由属性守卫把关）
        for mseg in re.finditer(r"\bRETURN\b(.*?)(?=\b(?:MATCH|WHERE|UNWIND|CALL|UNION|ORDER|SKIP|LIMIT)\b|$)",
                                query, re.IGNORECASE | re.S):
            seg = mseg.group(1)
            seg = re.sub(rf"\bcount\s*\(\s*(?:DISTINCT\s+)?{v}\s*\)", " ", seg, flags=re.IGNORECASE)
            seg = re.sub(rf"\b(?:id|elementId)\s*\(\s*{v}\s*\)", " ", seg, flags=re.IGNORECASE)
            if re.search(rf"(?<![\w.]){v}(?![\w.])", seg):
                raise ValueError(
                    f"read_cypher 隐私守卫：禁止整体 RETURN individual 节点（变量 {v}）——"
                    "请显式点取所需的非临床字段（如 {v}.individual_accession）或用 count() 聚合。")

def _assert_no_sensitive_payload(rows):
    """结果面兜底守卫（与查询写法无关）。

    查询面的正则只能识别它认得的写法；换个等价写法（无标签变量、WHERE 标签谓词、
    collect() 打包、map projection、多级别名…）就能绕过。这一层改为检查**返回内容**：
    只要结果里出现患者级临床属性——不管是 map 的键，还是 `UNWIND keys(x)` 把属性名
    当值返回——整条拒绝。查询面守卫留着是为了快速失败和给出可操作的报错。"""
    bad = set()

    def walk(v, depth=0):
        if depth > 12 or len(bad) >= 5:
            return
        if isinstance(v, dict):
            for k, sub in v.items():
                if _SENSITIVE_RE.fullmatch(str(k)):
                    bad.add(str(k))
                walk(sub, depth + 1)
        elif isinstance(v, (list, tuple)):
            for sub in v:
                walk(sub, depth + 1)
        elif isinstance(v, str) and _SENSITIVE_RE.fullmatch(v):
            bad.add(v)

    walk(rows)
    if bad:
        raise ValueError(
            f"read_cypher 隐私守卫（结果面）：返回内容包含患者级临床属性 "
            f"{sorted(bad)}——不论查询怎么写都不放行。请只点取非临床字段"
            "（individual_accession 等），或改成 count/avg 等聚合。")

MAX_ROWS = 500
# resolve_sample_roles 的 samples 预览条数。给 20 是因为模型在选队列这一步只需要
# sample_roles/role_resolved/file_coverage，明细看个形状就够；真要逐样本用 records 模式或
# read_cypher 定向查。上限 200 保留给确实需要成批明细的调用方（显式传 sample_limit）。
SAMPLE_PREVIEW = 20
SAMPLE_LIMIT_MAX = 200

def _scan(query, blank_strings):
    """把注释替换成等长空白；blank_strings=True 时连字符串字面量一起抹掉。

    等长替换是关键：抹完之后偏移量与原串一一对应，可以在 probe 上定位、在 clean 上改写。
    """
    out, i, n = [], 0, len(query)
    while i < n:
        c = query[i]
        if c in "'\"`":
            j = i + 1
            while j < n:
                if query[j] == "\\":
                    j += 2
                    continue
                if query[j] == c:
                    break
                j += 1
            lit = query[i:min(j + 1, n)]
            out.append(" " * len(lit) if blank_strings else lit)
            i += len(lit)
        elif query.startswith("//", i) or query.startswith("/*", i):
            if query[i + 1] == "/":
                j = query.find("\n", i)
                j = n if j < 0 else j
            else:
                j = query.find("*/", i + 2)
                j = n if j < 0 else j + 2
            out.append(" " * (j - i))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)

def _ensure_limit(query):
    """尽量把 LIMIT 收到 MAX_ROWS 以内。**这只是优化，不是防线**——真正的行数上限由
    tool_read_cypher 的结果面截断兜底（与查询写法无关）。

    0820 实测的四种绕过，都是"在原串上用正则找 LIMIT"这个思路本身的问题：
      1. `... UNION ...`        —— 尾部 LIMIT 只作用于最后一支，前面几支整表返回（27,582 行）
      2. `LIMIT 99999`          —— 有 LIMIT 就原样放行，上限形同虚设（27,196 行）
      3. `// LIMIT 10\nMATCH…`  —— 注释里的 LIMIT 骗过检测，真查询没有上限（27,196 行）
      4. `RETURN x // all`      —— 追加的 LIMIT 落进行尾注释被吞掉（27,196 行）
    所以先 _scan 掉注释和字符串再判定，且判定不通过时不猜、交给结果面截断。
    """
    clean = _scan(query, blank_strings=False)   # 注释已变空白，可安全追加
    probe = _scan(query, blank_strings=True)    # 再抹掉字面量，仅用于判定
    if re.search(r"\bUNION\b", probe, re.IGNORECASE):
        return clean
    mm = re.search(r"\bLIMIT\s+(\d+)\s*;?\s*$", probe, re.IGNORECASE)
    if mm:
        if int(mm.group(1)) <= MAX_ROWS:
            return clean
        return clean[:mm.start()].rstrip() + f" LIMIT {MAX_ROWS}"
    if re.search(r"\bLIMIT\b", probe, re.IGNORECASE):
        return clean            # LIMIT 在中间子句/子查询里，改写风险大于收益
    return clean.rstrip().rstrip(";") + f" LIMIT {MAX_ROWS}"

NEO4J_TIMEOUT = os.environ.get("NEO4J_TIMEOUT", "20")

def neo4j_q(statements):
    if not NEO4J_PASSWORD:
        raise RuntimeError("set NEO4J_PASSWORD (and optionally NEO4J_USER)")
    payload = json.dumps({"statements": [{"statement": s} for s in statements]})
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(payload)
        tmp = f.name
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", NEO4J_TIMEOUT, "-u", f"{NEO4J_USER}:{NEO4J_PASSWORD}",
             "-X", "POST", "-H", "Content-Type: application/json", "-d", "@" + tmp, NEO4J_URL],
            capture_output=True, text=True)
        # curl 超时/连不上时 stdout 是空的，直接 json.loads 会抛 JSONDecodeError（"Expecting
        # value: line 1 column 1"）——调用方模型看到这个完全不知道是数据库没连上还是查询写错了，
        # 只会瞎改查询重试。这里把传输层失败和 Cypher 报错区分开，各自给可操作的信息。
        if r.returncode != 0 or not r.stdout.strip():
            hint = "查询超时" if r.returncode == 28 else f"curl 退出码 {r.returncode}"
            raise RuntimeError(
                f"Neo4j 请求失败（{hint}，上限 {NEO4J_TIMEOUT}s，地址 {NEO4J_URL}）："
                f"{(r.stderr or '').strip()[:200] or '无响应'}。"
                "这不是查询语法问题——请缩小查询范围（加过滤条件/改聚合），或让运维确认服务可达。")
        try:
            d = json.loads(r.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"Neo4j 返回的不是 JSON（可能是认证失败或代理页面）：{r.stdout.strip()[:200]}")
        if d.get("errors"):
            raise RuntimeError("; ".join(e.get("message", "") for e in d["errors"])[:500])
        return [[row["row"] for row in res.get("data", [])] for res in d.get("results", [])]
    finally:
        os.unlink(tmp)

# ---------- 工具 ----------
def tool_get_planning_guide(args):
    try:
        text = open(SKILL_MD, encoding="utf-8").read()
        return {"status": "ok", "skill": text, "source": SKILL_MD}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

def tool_read_cypher(args):
    query = (args.get("query") or "").strip()
    if not query:
        return {"status": "error", "detail": "query 不能为空"}
    try:
        _assert_read_only(query)
        _assert_privacy(query)
        rows = neo4j_q([_ensure_limit(query)])
        rows = rows[0] if rows else []
        _assert_no_sensitive_payload(rows)
        # 结果面硬截断：_ensure_limit 只能处理它看得懂的写法，UNION/子查询里的 LIMIT
        # 一律漏网。这里按实际行数截断，并如实告知被截断——调用方模型不能拿半截结果当全集。
        out = {"status": "ok", "columns_unknown": True,
               "row_count": min(len(rows), MAX_ROWS), "rows": rows[:MAX_ROWS]}
        if len(rows) > MAX_ROWS:
            out["truncated"] = True
            out["note"] = (f"结果 {len(rows)} 行，已截断为前 {MAX_ROWS} 行。"
                           "请改用 count()/聚合或加更严格的过滤条件重查，"
                           "不要基于截断结果下「共有多少/全部是」这类结论。")
        return out
    except Exception as e:
        return {"status": "error", "detail": str(e)[:500]}

def tool_read_cypher_batch(args):
    """批量只读查询：一次调用执行多条相互独立的 Cypher（等效于同一轮并行多个 read_cypher）。
    供调用方模型把互不依赖的查询打包，直接减少推理轮数。每条独立走完整守卫与截断。"""
    queries = args.get("queries")
    if not isinstance(queries, list) or not queries:
        return {"status": "error", "detail": "queries 必须是非空字符串数组"}
    if len(queries) > 8:
        return {"status": "error", "detail": "单次最多 8 条；更多请拆分批次，避免把整库探索塞进一轮"}
    results = []
    for q in queries:
        if not isinstance(q, str) or not q.strip():
            results.append({"status": "error", "detail": "空查询"})
            continue
        results.append(tool_read_cypher({"query": q}))
    return {"status": "ok", "count": len(results), "results": results}


def tool_get_study_overview(args):
    """队列画像一包到底（确定性聚合，替代「队列信息 + T1/T2 清单 + 角色分布」这组高频多查组合）：
    study 基本信息 + sample 节点数 + T1/T2 格式与策略分布 + T2 现成文件样例 + 样本角色分布。
    文件级明细仍走 read_cypher 定向查；本工具回答「这个队列有什么、能不能配对/分组」。"""
    study = (args.get("study") or "").strip()
    if not study or not _SAFE_FILE.fullmatch(study):
        return {"status": "error", "detail": "需要合法 study 队列号（如 HRA001272）"}
    try:
        base = neo4j_q([f"MATCH (s:study {{study_accession: '{study}'}}) RETURN s.tumor_type, "
                        f"s.title, s.individual_count, s.sample_count"])[0]
        if not base:
            return {"status": "error", "detail": f"图内无队列 {study}"}
        n_samples = neo4j_q([f"MATCH (sp:sample) WHERE sp.study_accession = '{study}' "
                             f"RETURN count(sp)"])[0][0][0]
        t1 = neo4j_q([f"MATCH (f:T1) WHERE f.study_accession = '{study}' "
                      f"RETURN count(f), collect(DISTINCT f.format), collect(DISTINCT f.strategy)"])[0]
        t2 = neo4j_q([f"MATCH (f:T2) WHERE f.study_accession = '{study}' "
                      f"RETURN count(f), collect(DISTINCT f.format), collect(DISTINCT f.strategy)"])[0]
        t2_files = neo4j_q([f"MATCH (f:T2) WHERE f.study_accession = '{study}' "
                            f"RETURN f.file_name, f.format, f.file_path LIMIT 20"])[0]
    except Exception as e:
        return {"status": "error", "detail": str(e)[:300]}
    roles = tool_resolve_sample_roles({"study": study, "sample_limit": 0})
    out = {
        "status": "ok", "study_accession": study,
        "tumor_type": base[0][0], "title": base[0][1],
        "individual_count": base[0][2], "sample_count_prop": base[0][3],
        "sample_nodes": n_samples,
        "t1": {"count": t1[0][0] if t1 else 0,
               "formats": t1[0][1] if t1 else [], "strategies": t1[0][2] if t1 else []},
        "t2": {"count": t2[0][0] if t2 else 0,
               "formats": t2[0][1] if t2 else [], "strategies": t2[0][2] if t2 else [],
               "sample_files": [{"file_name": r[0], "format": r[1], "file_path": r[2]}
                                 for r in t2_files]},
    }
    # 角色分布整段并入（配对/分组判定的唯一权威来源）
    for k in ("sample_roles", "role_resolved", "file_coverage"):
        if k in roles:
            out[k] = roles[k]
    return out


def tool_resolve_sample_roles(args):
    """确定性样本角色判定（不查 LLM、不猜）。两种用法：
    - records: 对调用方提供的样本记录逐条判 tumor/normal（离线，不查图）
    - study:   查图统计该队列的角色分布（tumor/normal/unresolved）+ role_resolved"""
    records = args.get("records")
    if records:
        out = []
        for r in records:
            role = sample_role(r or {})
            out.append({**(r or {}), "sample_role": role,
                        "sample_role_label": SAMPLE_ROLE_LABELS.get(role or "")})
        return {"status": "ok", "records": out}
    study = (args.get("study") or "").strip()
    if not study:
        return {"status": "error", "detail": "需要 study（队列号）或 records（样本记录数组）参数"}
    if not _SAFE_FILE.fullmatch(study):
        return {"status": "error", "detail": "非法 study 格式"}
    # samples 明细默认只回 SAMPLE_PREVIEW 条。0820 实测：上限写死 200 条时 HRA001272 的返回体
    # 有 51,782 字符（其中 samples 占 99%），调用方 harness 按 12,000 字符截断后**是非法 JSON**
    # ——模型收到一段砍断的记录，且没有任何"被截断了"的提示。这和 read_cypher 的行数上限是
    # 同一类问题（那个修了，这个漏了）。而模型在这一步真正要的是 sample_roles / role_resolved /
    # file_coverage（合计 200 多字符），逐样本明细该走 records 模式或 read_cypher 定向查。
    try:
        sample_limit = int(args.get("sample_limit", SAMPLE_PREVIEW))
    except (TypeError, ValueError):
        sample_limit = SAMPLE_PREVIEW
    sample_limit = max(0, min(sample_limit, SAMPLE_LIMIT_MAX))
    try:
        rows = neo4j_q([
            # 队列样本以 sample 节点为准（等价于 study<-individual<-sample 遍历，sample 自带
            # study_accession）。不要走 (T1)-[:in_sample]->(sample)：只有挂到文件的样本才会
            # 出现，无文件的样本会被静默丢掉（如 HRA006117 少 265/835）。
            f"MATCH (sp:sample) WHERE sp.study_accession = '{study}' RETURN DISTINCT "
            "sp.sample_accession, sp.sample_name, sp.tissue_type, sp.specimen_type, sp.run_accession",
            # 文件侧可解析度：fastq 这类按 run 组织的文件靠 in_sample 边落到样本，
            # 边缺失的部分是图谱里 run→sample 映射不全，如实报出来，不要让调用方看到裸 null。
            f"MATCH (f:T1) WHERE f.study_accession = '{study}' RETURN count(*), "
            "sum(CASE WHEN (f)-[:in_sample]->() THEN 1 ELSE 0 END)",
            f"MATCH (f:T1) WHERE f.study_accession = '{study}' AND f.run_accession IS NOT NULL "
            "WITH collect(DISTINCT f.run_accession) AS fr "
            f"OPTIONAL MATCH (sp:sample) WHERE sp.study_accession = '{study}' AND sp.run_accession IS NOT NULL "
            "WITH fr, collect(DISTINCT sp.run_accession) AS sr "
            "RETURN size(fr), size(sr), size([r IN fr WHERE NOT r IN sr])"])
    except Exception as e:
        return {"status": "error", "detail": str(e)[:300]}
    counts = {"tumor": 0, "normal": 0, "unresolved": 0}
    samples = []
    for r in (rows[0] if rows else []):
        rec = {"study_accession": study, "sample_accession": r[0], "sample_name": r[1],
               "tissue_type": r[2], "specimen_type": r[3], "run_accession": r[4]}
        role = sample_role(rec)
        counts[role if role in ("tumor", "normal") else "unresolved"] += 1
        if len(samples) < sample_limit:
            samples.append({**rec, "sample_role": role,
                            "sample_role_label": SAMPLE_ROLE_LABELS.get(role or "")})
    files, linked = (rows[1][0] if rows[1] else [0, 0])
    file_runs, sample_runs, orphan_runs = (rows[2][0] if rows[2] else [0, 0, 0])
    cover = {"t1_files": files, "t1_files_linked_to_sample": linked,
             "t1_files_unlinked": files - linked, "runs_on_files": file_runs,
             "runs_on_samples": sample_runs, "runs_without_sample_node": orphan_runs}
    notes = ["聚合类文件（表达矩阵/MAF/临床表）本就跨样本，sample_accession 为 null 属正常"]
    # 判缺口只看 t1_files_unlinked（真的没有 in_sample 边的文件数）。
    # 0821 数据换代后 run→sample 不再是样本归属的依据：新导出把 sample_accession 直接
    # 写在 T1 上，in_sample 边照它建。而 sample 节点仍然每个只记一个 run_accession，
    # 所以 runs_without_sample_node 依旧很大（HRA000087 1492/1553、HRA001272 482/1180），
    # 但同一批队列的 t1_files_linked_to_sample 是 3106/3108、2360/2362——文件全连上了。
    # 旧口径拿 orphan_runs 报警会把好队列判死，这里降级成诊断字段，不再据它下结论。
    if files - linked:
        notes.append(f"本队列 {files - linked}/{files} 个 T1 文件没有 in_sample 边，"
                     "无法定位到样本——如实标 missing_from_graph，不要猜测归属")
    if orphan_runs:
        notes.append(f"runs_without_sample_node={orphan_runs}/{file_runs} 只是诊断信息："
                     "sample 节点每个仅记录一个 run_accession，所以按 run 反查必然对不齐。"
                     "样本归属以 in_sample 边为准（见 t1_files_linked_to_sample），"
                     "**不要拿这个数判断队列可不可用**。")
    total = sum(counts.values())
    out = {"status": "ok", "study": study,
           "sample_roles": counts,
           "role_resolved": counts["tumor"] > 0 and counts["normal"] > 0,
           "samples": samples, "sample_count": total,
           "file_coverage": cover, "notes": notes}
    # 截断必须如实上报，否则模型会把预览当全集，下"这队列只有 N 个样本"这类全称结论。
    if total > len(samples):
        out["samples_truncated"] = True
        out["samples_shown"] = len(samples)
        notes.append(f"samples 只是前 {len(samples)} 条预览（该队列共 {total} 个样本），"
                     f"角色统计以 sample_roles 为准（已覆盖全部 {total} 个）。"
                     f"要更多明细：加大 sample_limit（上限 {SAMPLE_LIMIT_MAX}），"
                     "或用 read_cypher 加过滤条件定向查——不要拿预览当全集。")
    return out

def tool_validate_atomic_chain(args):
    chain = args.get("chain") or []
    if not isinstance(chain, list) or not chain:
        return {"status": "error", "detail": "chain 必须是非空 tool_id 列表"}
    # 反向映射：跳过别名键（gid == meta_id 的是 card_id 别名），只留 图谱id -> meta.id
    meta_to_graph = {c["meta_id"]: gid for gid, c in KC_MAP.items() if gid != c["meta_id"]}
    def _norm(t):
        t = str(t)
        return (meta_to_graph[t], t) if t in meta_to_graph else (t, t)
    unknown = [t for t in chain if _norm(t)[0] not in CATALOG]
    non_atomic = [t for t in chain if _norm(t)[0] in CATALOG and CATALOG[_norm(t)[0]]["tool_kind"] != "atomic"]
    violations = []
    if unknown:
        violations.append(f"未知工具: {unknown}")
    if non_atomic:
        violations.append(f"非 atomic（闭集外）: {non_atomic}")
    # 图内 next_tool 邻接校验（图节点无 tool_id，用 toLower(tool_name) 匹配；入参过白名单；
    # 同时接受 Knowledge Card 的 meta.id，先归一化到图谱 tool_id）
    adjacency_ok = []
    for a, b in zip(chain[:-1], chain[1:]):
        if not (_SAFE_TOKEN.fullmatch(str(a)) and _SAFE_TOKEN.fullmatch(str(b))):
            violations.append(f"非法 tool_id 字符: {a}->{b}")
            continue
        ga, _ = _norm(a); gb, _ = _norm(b)
        rows = neo4j_q([f"MATCH (a:tool)-[:next_tool]->(b:tool) WHERE toLower(a.tool_name) = '{ga.lower()}' AND toLower(b.tool_name) = '{gb.lower()}' RETURN count(*) AS c"])
        if rows and rows[0] and rows[0][0][0] > 0:
            adjacency_ok.append((a, b))
    missing_edges = [(a, b) for a, b in zip(chain[:-1], chain[1:]) if (a, b) not in adjacency_ok]
    if missing_edges:
        violations.append(f"图谱中无 next_tool 边: {missing_edges}")
    # 输出对齐 Knowledge Card：tool_id 用 meta.id，inputs/outputs 用卡内名称
    tool_chain = []
    for t in chain:
        gid, given = _norm(t)
        card = KC_MAP.get(gid)
        if card:
            def _slot(d):
                return {"name": d.get("name"), "type": d.get("type"),
                        "optional": not bool(d.get("required", True)),
                        "formats": [d["format"]] if d.get("format") else []}
            tool_chain.append({"tool_id": card["meta_id"], "input_as": given,
                               "inputs": [_slot(i) for i in card["inputs"]],
                               "outputs": [_slot(o) for o in card["outputs"]]})
        else:
            tool_chain.append({"tool_id": str(t), "inputs": [], "outputs": [],
                               "note": "无 Knowledge Card（pipeline 级工具或未收录）"})
    return {"status": "valid" if not violations else "invalid",
            "chain": chain, "tool_chain": tool_chain,
            "violations": violations, "adjacency_ok": adjacency_ok,
            "atomic_closed_set_size": len(ATOMIC_IDS)}

def tool_validate_execution_chain(args):
    """场景1：提交前执行契约把关（多阶段探查）。
    steps: [{tool_id, inputs:{name: binding}}]；binding 可为字符串或对象{file_id/file_name/format}。
    五阶段：注册 → 卡契约(必填输入) → 绑定结构 → 数据探查(图内候选) → 链流转。
    """
    steps = args.get("steps") or []
    cohort = (args.get("cohort") or "").strip()
    if not isinstance(steps, list) or not steps:
        return {"status": "error", "detail": "steps 必须是非空数组 [{tool_id, inputs}]"}
    errors, warnings, stages, normalized = [], [], [], []
    meta_to_graph = {c["meta_id"]: gid for gid, c in KC_MAP.items() if gid != c["meta_id"]}
    def _norm(t):
        t = str(t); return (meta_to_graph[t], t) if t in meta_to_graph else (t, t)
    # ── stage 1 注册校验 ──
    reg_bad = []
    for s in steps:
        gid, given = _norm(s.get("tool_id"))
        if gid not in CATALOG:
            reg_bad.append(given)
    stages.append({"stage": "registry", "passed": not reg_bad,
                   "findings": [] if not reg_bad else [f"未知工具: {reg_bad}"]})
    if reg_bad: errors.append(f"未知工具: {reg_bad}")
    # ── stage 2/3 卡契约 + 绑定结构 ──
    for s in steps:
        gid, given = _norm(s.get("tool_id"))
        card = KC_MAP.get(gid)
        bindings = s.get("inputs") or {}
        if not card:
            warnings.append(f"{given}: 无 Knowledge Card（pipeline 级或未收录），跳过契约校验")
            normalized.append({"tool_id": given, "card": None})
            continue
        # 必填输入检查
        missing = [i["name"] for i in card["inputs"] if i.get("required", True) and i["name"] not in bindings]
        if missing:
            errors.append(f"{card['meta_id']} 缺必填输入: {missing}")
        # 绑定结构检查（对齐重版：binding 必须为对象）
        bad_bind = []
        for i in card["inputs"]:
            b = bindings.get(i["name"])
            if b is None:
                continue
            if i.get("type") in ("File", "Array[File]"):
                if not isinstance(b, dict):
                    bad_bind.append(f"{i['name']} binding 必须为对象")
            elif i.get("type") in ("Boolean", "Int", "Float"):
                if not isinstance(b, (bool, int, float)):
                    bad_bind.append(f"{i['name']} binding 类型应为 {i['type']}")
        if bad_bind:
            errors.extend(f"{card['meta_id']}: {x}" for x in bad_bind)
        normalized.append({"tool_id": card["meta_id"], "inputs": {k: v for k, v in bindings.items()}})
    stages.append({"stage": "knowledge_card_contract",
                   "passed": not any("缺必填输入" in e for e in errors),
                   "findings": [e for e in errors if "缺必填输入" in e]})
    stages.append({"stage": "binding_structure",
                   "passed": not any("binding" in e for e in errors),
                   "findings": [e for e in errors if "binding" in e]})
    # ── stage 4 数据探查（File 输入 → 图内候选） ──
    probes = []
    for s in steps:
        gid, given = _norm(s.get("tool_id"))
        card = KC_MAP.get(gid)
        if not card:
            continue
        for i in card["inputs"]:
            if i.get("type") not in ("File", "Array[File]") or not i.get("required", True):
                continue
            b = (s.get("inputs") or {}).get(i["name"])
            fmt = (i.get("format") or "").upper()
            kw = next((k for k in ("FASTQ", "BAM", "BAI", "VCF", "TSV", "GTF", "FASTA", "TBI") if k in fmt), None)
            bound = isinstance(b, dict) and bool(b.get("file_id") or b.get("file_name"))
            probe = {"tool": card["meta_id"], "input": i["name"], "format": i.get("format"),
                     "bound": bound}
            if kw and not bound:
                rows = neo4j_q([f"MATCH (n:T1) WHERE toLower(n.format) CONTAINS '{kw.lower()}' OR toLower(n.file_name) CONTAINS '.{kw.lower()}' RETURN count(n) AS c",
                                f"MATCH (n:T2) WHERE toLower(n.format) CONTAINS '{kw.lower()}' OR toLower(n.file_name) CONTAINS '.{kw.lower()}' RETURN count(n) AS c"])
                t1 = rows[0][0][0] if rows and rows[0] else 0
                t2 = rows[1][0][0] if rows and rows[1] else 0
                probe["graph_candidates"] = {"T1": t1, "T2": t2}
            probes.append(probe)
    stages.append({"stage": "data_availability", "passed": True, "findings": [], "probes": probes})
    # ── stage 5 链流转（next_tool 邻接） ──
    # tool_id 直接进 Cypher 字面量，**必须先过白名单**（同 validate_atomic_chain 的做法）。
    # 0820 实测漏了这道校验的后果：steps=[{"tool_id": "zzz' RETURN 1 AS c UNION MATCH
    # (n:study) RETURN 1 AS c //"}, ...] 能闭合引号注入任意 Cypher——既绕开 _assert_read_only
    # （这条路径根本不经过它，写操作可达），又能把一条图里不存在的邻接伪造成 passed=True，
    # 等于把提交前把关这道门整个架空。校验失败就不查图，直接记违规。
    flow_bad = []
    gids = [_norm(str(s.get("tool_id")))[0] for s in steps]
    for a, b in zip(gids[:-1], gids[1:]):
        if not (_SAFE_TOKEN.fullmatch(a) and _SAFE_TOKEN.fullmatch(b)):
            flow_bad.append((a, b))
            continue
        rows = neo4j_q([f"MATCH (a:tool)-[:next_tool]->(b:tool) WHERE toLower(a.tool_name) = '{a.lower()}' AND toLower(b.tool_name) = '{b.lower()}' RETURN count(*) AS c"])
        if not (rows and rows[0] and rows[0][0][0] > 0):
            flow_bad.append((a, b))
    stages.append({"stage": "chain_flow", "passed": not flow_bad,
                   "findings": [] if not flow_bad else [f"图谱中无 next_tool 边: {flow_bad}"]})
    if flow_bad: errors.append(f"图谱中无 next_tool 边: {flow_bad}")
    # ── 执行参数解析（对齐重版 execution_params/submittable：只认真实 "/" 开头路径，不伪造） ──
    def _real_path(binding):
        if not isinstance(binding, dict):
            return ""
        path = str(binding.get("file_path") or "").strip()
        if path.startswith("/") and "NOT_FOUND" not in path:
            return path
        fname = str(binding.get("file_name") or binding.get("file_id") or "").strip()
        if fname and _SAFE_FILE.fullmatch(fname):
            try:
                rows = neo4j_q([f"MATCH (n) WHERE (n:T1 OR n:T2) AND n.file_name = '{fname}' RETURN n.file_path LIMIT 1"])
                p = str(rows[0][0][0] or "") if rows and rows[0] else ""
                if p.startswith("/") and "NOT_FOUND" not in p:
                    return p
            except Exception:
                pass
        return ""
    execution_params, exec_missing = {}, []
    for s in steps:
        gid, given = _norm(s.get("tool_id"))
        card = KC_MAP.get(gid)
        bindings = s.get("inputs") or {}
        if card:
            file_inputs = [i["name"] for i in card["inputs"]
                           if i.get("type") in ("File", "Array[File]") and i.get("required", True)]
        else:   # pipeline 级无卡：有对象 binding 的输入都当 File 处理
            file_inputs = [k for k, v in bindings.items() if isinstance(v, dict)]
        for name in file_inputs:
            key = f"{given}.{name}" if len(steps) > 1 else name
            path = _real_path(bindings.get(name))
            if path:
                execution_params[key] = path
            else:
                exec_missing.append(key)
    submittable = not errors and not exec_missing
    return {"schema_version": "tool-chain-validation/v1.1", "mode": "execution_contract",
            "valid": not errors, "validation": {"ok": not errors, "errors": errors, "warnings": warnings},
            "stages": stages, "normalized_steps": normalized,
            "execution_params": execution_params,
            "execution_params_missing": exec_missing,
            "submittable": submittable,
            "hint": "提交前把关：errors 清零且 execution_params_missing 为空（submittable=true）才可提交执行端"}

# route_pipeline_request / rule_baseline_plan 已下线（v2.1）：规则规划路径与架构主张
# （推理必来自调用方模型）冲突。关键词基线仅保留给 benchmark 三臂评测的 ceiling 对照臂
# 与 light_router.py（离线对照）——去名集上它只有 1.4%，生产路径不允许静默降级到这里。
RULES = [
    (["10x", "cellranger", "CellRanger", "单细胞", "barcode", "Seurat", "Scanpy"], "cellranger_workflow", 3),
    (["uBAM", "unmapped bam", "未比对", "read group"], "paired_fastq_to_unmapped_bam", 3),
    (["肿瘤突变负荷", "tmb", "TMB"], "tmb_survival_analysis", 3),
    (["her2", "HER2", "ERBB2"], "her2_pfs_survival", 3),
    (["驱动基因", "男女", "性别分层"], "driver_gene_gender_analysis", 3),
    (["突变景观", "oncoplot", "Oncoplot", "高频突变", "突变类型", "Top30", "top30"], "wes_somatic_maf_landscape", 3),
    (["体细胞突变检测", "somatic vcf", "体细胞变异", "配对", "tumor-normal", "肿瘤和正常"], "wes_somatic_pair", 3),
    (["免疫浸润", "免疫细胞", "CIBERSORT", "浸润"], "immune_infiltration_iobr", 3),
    (["wgcna", "WGCNA", "共表达", "模块", "hub 基因", "hub基因"], "wgcna", 3),
    (["无监督聚类", "分型", "亚型", "GMM", "聚类数", "聚类稳定性", "聚类"], "rnaseq_unsupervised_cluster", 3),
    (["rRNA", "完整上游", "质控、剪切", "质控、接头", "质控、比对和表达计数", "上游"], "rnaseq_singletask", 2),
    (["egfr", "EGFR"], "survival_analysis", 3),
    (["kegg", "KEGG", "Reactome", "信号通路", "通路富集"], "diff_expr_kegg", 2),
    (["GO", "go 富集", "生物学功能", "生物过程"], "diff_expr_go", 2),
    (["差异表达", "差异基因", "表达不同", "表达差异", "上调", "下调", "deg", "DEG", "limma", "功能"], "diff_expr_go", 1),
]

def _predict_baseline(query):
    scores = {}
    for terms, pid, w in RULES:
        if any(t.lower() in query.lower() for t in terms):
            scores[pid] = scores.get(pid, 0) + w
    if "GO" in query:
        scores["diff_expr_go"] = scores.get("diff_expr_go", 0) + 1
        if "kegg" not in query.lower() and "Reactome" not in query:
            scores.pop("diff_expr_kegg", None)
    elif "通路" in query:
        scores["diff_expr_kegg"] = scores.get("diff_expr_kegg", 0) + 1
        if "GO" not in query and "kegg" not in query.lower() and "Reactome" not in query:
            scores.pop("diff_expr_go", None)
    return [pid for pid, _ in sorted(scores.items(), key=lambda kv: -kv[1])][:3]

def tool_validate_plan(args):
    """接地校验：整份 tool-chain/v2 Plan 的名词必须图内/目录内可验证。
    模型输出前自检用——工具、文件、路径、队列号任一无法证实即 grounded=false，
    防调用方模型用内部知识编造答案内容。"""
    plan = args.get("plan")
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except json.JSONDecodeError as e:
            return {"status": "error", "detail": f"plan 不是合法 JSON: {e}"}
    if not isinstance(plan, dict):
        return {"status": "error", "detail": "plan 必须是 JSON 对象或其字符串"}
    if plan.get("status") == "rejected":
        ok = bool(plan.get("reason"))
        return {"status": "ok", "grounded": ok, "kind": "rejected",
                "violations": [] if ok else ["rejected 对象缺 reason"]}
    v = []
    if plan.get("schema_version") != "tool-chain/v2":
        v.append("schema_version 缺失或不是 tool-chain/v2")
    meta_to_graph = {c["meta_id"]: gid for gid, c in KC_MAP.items() if gid != c["meta_id"]}
    recs = plan.get("recommendations") or []
    # 空 recommendations 只在「本来就没有推荐可给」的状态下合法：information（纯数据分布/
    # 清单类回答）、unsupported（需求超出闭集）、no_candidate（图内查无）。此前一律判违规，
    # 逼得模型为「HRA001272 角色分布如何」这类信息题硬凑一个 rank1 推荐——既是编造，又白烧
    # 一轮修正（实测 q03/q06/q24 每次多花 20s）。
    _NO_REC_OK = {"information", "unsupported", "no_candidate", "missing_from_graph"}
    if not recs and str(plan.get("selection_status") or "").lower() not in _NO_REC_OK:
        v.append("recommendations 为空（selection_status 不是 information/unsupported/"
                 "no_candidate 时必须有 rank1 推荐，否则改用 rejected）")
    for i, rec in enumerate(recs):
        # 调用方偶发把整条推荐写成字符串（或把 assets 写成裸文件名数组）。以前这里直接
        # 抛 'str' object has no attribute 'get'，整轮校验丢失、模型收到一句无从修起的
        # 报错——校验器自己必须对畸形输入免疫，把畸形本身报成违规。
        if not isinstance(rec, dict):
            v.append(f"recommendations[{i}] 不是对象（应为 JSON 对象，不是字符串）")
            continue
        pid = rec.get("pipeline_id") or (rec.get("tool") or {}).get("tool_id")
        gid = meta_to_graph.get(pid, pid)
        if gid not in CATALOG:
            v.append(f"recommendations[{i}] 工具不在闭集目录（疑似模型编造）: {pid}")
        # 没有数据的推荐不可执行：selection_status 说 ok 就必须指出图内的具体文件。
        # 实测调用方对「我有 10x 单细胞 FASTQ」这类没点名队列的问题会直接交空 assets——
        # 等于把选数据这一半的活儿留给了用户。
        if not ((rec.get("data") or {}).get("assets") or []) and \
                str(plan.get("selection_status") or "").lower() == "ok":
            v.append(f"recommendations[{i}] data.assets 为空（selection_status=ok 必须给出"
                     f"图内真实文件；图里确实没有可用数据就改判 no_candidate）")
        for a in (rec.get("data") or {}).get("assets") or []:
            if isinstance(a, str):        # 裸文件名数组：当成只有 file_name 的资产
                a = {"file_name": a}
            elif not isinstance(a, dict):
                v.append(f"recommendations[{i}] asset 不是对象")
                continue
            fn = a.get("file_name") or a.get("name")
            if not fn:
                v.append(f"recommendations[{i}] asset 缺 file_name")
                continue
            if not _SAFE_FILE.fullmatch(str(fn)):
                v.append(f"asset 文件名含非法字符: {fn}")
                continue
            rows = neo4j_q([f"MATCH (n) WHERE (n:T1 OR n:T2) AND n.file_name = '{fn}' RETURN n.file_path LIMIT 1"])
            if not (rows and rows[0]):
                v.append(f"asset 图内不存在（疑似模型编造）: {fn}")
            else:
                fp = a.get("file_path")
                real = rows[0][0][0]
                if fp and real and fp != real:
                    v.append(f"asset file_path 与图内记录不符: {fn}")
        for st in (rec.get("data") or {}).get("study_accessions") or []:
            if not _SAFE_FILE.fullmatch(str(st)):
                v.append(f"study 号非法: {st}")
                continue
            rows = neo4j_q([f"MATCH (s:study {{study_accession: '{st}'}}) RETURN count(s)"])
            if not (rows and rows[0] and rows[0][0][0] > 0):
                v.append(f"study 图内不存在（疑似模型编造）: {st}")
    for i, c in enumerate(plan.get("candidates") or []):
        for stp in c.get("tool_chain") or []:
            tid = stp.get("tool_id")
            gid = meta_to_graph.get(tid, tid)
            if gid not in CATALOG or CATALOG[gid].get("tool_kind") != "atomic":
                v.append(f"candidates[{i}] 工具链含非闭集 atomic: {tid}")
    return {"status": "ok", "grounded": not v, "violations": v,
            "hint": "grounded=false 说明 Plan 含图谱无法证实的内容——回到查询结果修正，不要用模型内部知识补全"}

def _card_slots(card):
    """Knowledge Card 的 inputs/outputs → Plan 契约的槽位形态。"""
    def _slot(d, is_in):
        s = {"name": d.get("name"), "type": d.get("type") or "File",
             "optional": not bool(d.get("required", True)),
             "formats": [d["format"]] if d.get("format") else []}
        if is_in:
            s["is_file"] = (d.get("type") or "File") == "File"
        return s
    return ([_slot(d, True) for d in card.get("inputs") or []],
            [_slot(d, False) for d in card.get("outputs") or []])

def _graph_tool_io(gid):
    """pipeline 级工具无 Knowledge Card：I/O 槽位从图内 (tool)-[:input|output]->(format) 取。

    两个坑（都会静默返回空，不报错）：
    ① 图内匹配一律走 `t.tool_id` = 闭集的 `catalog_id`（T033 这类）。闭集的 `tool_name`
       与图内 `t.tool_name` 未必一致（T033 闭集写 immune_infiltration、图内是
       immune_infiltration_iobr），catalog_id 则 51/51 全对得上。
    ② format 节点的标识属性是 `f.format`，不是 `f.name`——写成 f.name 返回一行 null。"""
    cat = CATALOG.get(gid) or {}
    tid = str(cat.get("catalog_id") or "")
    if not _SAFE_TOKEN.fullmatch(tid):
        return [], []
    rows = neo4j_q([
        f"MATCH (t:tool)-[:input]->(f:format) WHERE t.tool_id = '{tid}' RETURN DISTINCT f.format",
        f"MATCH (t:tool)-[:output]->(f:format) WHERE t.tool_id = '{tid}' RETURN DISTINCT f.format"])
    def _names(r):
        return [x[0] for x in (r or []) if x and x[0]]
    ins = _names(rows[0] if rows else [])
    outs = _names(rows[1] if len(rows) > 1 else [])
    exts = [e.strip() for e in str(cat.get("input_format") or "").split(",") if e.strip()]
    oexts = [e.strip() for e in str(cat.get("output_format") or "").split(",") if e.strip()]
    return ([{"name": n.lower(), "type": "File", "is_file": True, "optional": False,
              "artifact": n.lower(), "formats": exts} for n in ins],
            [{"name": n.lower(), "artifact": n.lower(), "formats": oexts} for n in outs])

_ASSET_FIELDS = ("format", "file_format", "strategy", "data_level", "study_accession",
                 "sample_accession", "run_accession", "file_path", "specimen_type")

def _asset_facts(names):
    """按 file_name 批量取图内权威字段（T1/T2 通用），供 assets 补全。"""
    qs, keys = [], []
    for fn in dict.fromkeys(names):            # 去重但保序
        if _SAFE_FILE.fullmatch(str(fn)):
            qs.append(f"MATCH (n) WHERE (n:T1 OR n:T2) AND n.file_name = '{fn}' "
                      f"RETURN properties(n) LIMIT 1")
            keys.append(fn)
    if not qs:
        return {}
    rows = neo4j_q(qs)                         # 一次批量往返，别逐个查
    facts = {}
    for fn, r in zip(keys, rows):
        if r and r[0]:
            facts[fn] = r[0][0] or {}
    return facts

# 定量口径：同一队列的表达矩阵在图内有 FPKM/TPM/counts 三份，节点属性完全一致
# （semantic_format 都是 TABULAR_BIO_DATA、data_level 都是 2），只有文件名能区分。
# 该选哪一份由流程自己说了算——闭集描述里点名的口径就是它的默认口径。
_FLAVOR_PAT = re.compile(r"(?<![A-Za-z])(logCPM|FPKM|TPM|counts?)(?![A-Za-z])", re.I)
_FLAVOR_CANON = {"logcpm": "logCPM", "fpkm": "FPKM", "tpm": "TPM",
                 "count": "counts", "counts": "counts"}
_MATRIX_NAME = re.compile(r"^(.*-Genes-)([A-Za-z]+)(-.*\.tsv)$", re.I)

# 描述里没点名口径的流程，按方法本身要求的输入定：不定就等于让调用方随口挑一份，
# 同一个问题两次规划给出不同文件。只收方法学上没有争议的两族：
#   · WGCNA 族按官方推荐从原始 counts（VST）起步，不吃 TPM/FPKM；
#   · 跨样本比较某个基因的表达高低（生存分组、箱线图、热图、降维、预排序 GSEA）
#     必须先做长度+深度归一，counts 不可比 —— TPM。
_FLAVOR_FALLBACK = {
    "wgcna": "counts", "wgcna_hub": "counts", "wgcna_module_trait": "counts",
    "km_survival": "TPM", "cox_model": "TPM", "gene_boxplot": "TPM",
    "umap": "TPM", "stage_heatmap": "TPM", "gsea_pathway_enrichment": "TPM",
}

def _pipeline_flavor(gid):
    """闭集描述里**首个**点名的定量口径 = 该流程的默认矩阵形态。

    描述里写「适用于 FPKM/TPM 定量数据」这类并列时取首个：两份都能跑，但交付要有
    唯一口径，否则同一个问题两次规划会给出不同文件。描述整句不提口径时落
    `_FLAVOR_FALLBACK`（仍拿不到才 None——那种流程就随调用方选）。"""
    m = _FLAVOR_PAT.search((CATALOG.get(gid) or {}).get("description") or "")
    if m:
        return _FLAVOR_CANON.get(m.group(1).lower())
    return _FLAVOR_FALLBACK.get(gid)

def _study_assets(acc):
    """一个队列在图内的交付文件：{semantic_format: [file_name, ...]}。"""
    if not _SAFE_TOKEN.fullmatch(str(acc or "")):
        return {}
    rows = neo4j_q([f"MATCH (n) WHERE (n:T1 OR n:T2) AND n.study_accession = '{acc}' "
                    f"AND n.semantic_format IS NOT NULL "
                    f"RETURN n.semantic_format, collect(n.file_name)"])
    out = {}
    for r in (rows[0] if rows else []) or []:
        if r and r[0]:
            out[r[0]] = r[1] or []
    return out

# 临床表与样本元信息表必须成对：MetaInfo 是 sample↔patient 的连接表，缺了它临床字段
# 接不到表达矩阵/MAF 上。图内这两张表也确实每个队列各一份、总是成对交付。
_CLINICAL_PAIR = ("CLINICAL_DATA_EXCEL", "METADATA_SAMPLE_INFO")

# 队列级交付文件：`HRA*-SomaticSNV-1.0.maf` 是全队列汇总，`HRR1725089.maf` 只有一个病人。
# 突变景观/TMB 分组/生存这类队列级分析拿后者等于只分析了 1/77 的人。
_STUDY_LEVEL = re.compile(r"^HRA\d+-", re.I)

# 双端测序的 R1/R2 是同一次测序的两半，任何流程都必须成对拿。图内命名有 `_f1/_r2`、
# `_R1/_R2`、`.R1./.R2.` 几种，统一按这张表找对家。
_MATE = ((("_f1", "_r2"), ("_r1", "_r2"), ("_R1", "_R2"), (".R1.", ".R2.")))

def _mate_name(fn):
    """双端文件的对家文件名（不是双端命名则 None）。"""
    for a, b in _MATE:
        if a in fn:
            return fn.replace(a, b)
        if b in fn:
            return fn.replace(b, a)
    return None

def _complete_assets(gid, assets, facts):
    """按流程在图内声明的输入槽位补全 assets——只在图里挑，不发明文件。

    三条规则，都对应 96 例标准答案对照表里暴露的系统性缺项：
    ① 流程声明需要 CLINICAL_DATA_EXCEL 时，把该队列的临床表与样本元信息表补齐（见
       `_CLINICAL_PAIR`）。实测调用方十次有八次只给表达矩阵/MAF 就交卷。
    ② 表达矩阵口径按 `_pipeline_flavor` 归一：调用方选了同队列的其它口径就换成默认
       口径。换的是同一队列同一张表的另一个定量版本，不是换数据源。
    ③ 该语义格式在队列里**恰好**有一份队列级交付文件（见 `_STUDY_LEVEL`）时，把调用方
       选的逐样本文件换成它。恰好一份是关键：FASTQ 一份都没有（不动），表达矩阵有三份
       （口径之争交给 ②），只有 MAF/CNV 这类「汇总一份 + 逐样本 N 份」才落到这条上。
    只在能从已选资产反查到唯一 study_accession 时生效；资产为空时不做任何事——
    队列没定，图里 576 个 FASTQ 挑哪个都是猜。"""
    req = {s["name"].upper() for s in _graph_tool_io(gid)[0]}
    if not req or not assets:
        return assets, []
    acc = next((f.get("study_accession") for a in assets
                if (f := facts.get(a.get("file_name")) or {}).get("study_accession")), None)
    if not acc:
        return assets, []
    pool = _study_assets(acc)
    if not pool:
        return assets, []
    notes = []

    # ② 先归一口径（在补全之前做，免得补进来的表被当成"已有 TABULAR_BIO_DATA"）
    flav = _pipeline_flavor(gid)
    if flav and "TABULAR_BIO_DATA" in req:
        mats = {n.lower(): n for n in pool.get("TABULAR_BIO_DATA") or []}
        for a in assets:
            m = _MATRIX_NAME.match(str(a.get("file_name") or ""))
            if not m or m.group(2).lower() == flav.lower():
                continue
            tgt = mats.get(f"{m.group(1)}{flav}{m.group(3)}".lower())
            if tgt:
                notes.append(f"{a['file_name']}→{tgt}")
                a["file_name"] = tgt
                a["match_reason"] = f"{gid} 默认使用 {flav} 定量矩阵"
                for k in _ASSET_FIELDS:      # 换了文件，旧文件的图内字段全部失效
                    a.pop(k, None)

    # ③ 逐样本文件 → 队列级交付文件
    for fmt, files in pool.items():
        if fmt not in req:
            continue
        lvl = [f for f in files if _STUDY_LEVEL.match(str(f))]
        if len(lvl) != 1:                    # 0 份（FASTQ）或多份（矩阵三口径）都不动
            continue
        for a in assets:
            fn = str(a.get("file_name") or "")
            if fn in files and not _STUDY_LEVEL.match(fn):
                notes.append(f"{fn}→{lvl[0]}")
                a["file_name"] = lvl[0]
                a["match_reason"] = f"{gid} 是队列级分析，用 {acc} 的汇总交付文件"
                for k in _ASSET_FIELDS:
                    a.pop(k, None)

    # ① 临床/元信息成对补全
    if "CLINICAL_DATA_EXCEL" in req:
        have = {str(a.get("file_name") or "").lower() for a in assets}
        for fmt in _CLINICAL_PAIR:
            files = sorted(pool.get(fmt) or [])   # 定序：同一问题两次规划给同一份
            if files and not (have & {f.lower() for f in files}):
                assets.append({"file_name": files[0],
                               "match_reason": f"{gid} 声明需要 {fmt} 输入槽位，"
                                               f"按队列 {acc} 补全"})
                notes.append("+" + files[0])

    # ④ 双端补对家：调用方十次有九次只给 R1（实测 c01 只交 HRR572934_f1.fq.gz），
    # 而没有 R2 的双端流程根本跑不起来。只补图内确实存在的那一半。
    allf = {f for fs in pool.values() for f in fs}
    have = {str(a.get("file_name") or "") for a in assets}
    for fn in sorted(have):
        mate = _mate_name(fn)
        if mate and mate in allf and mate not in have:
            assets.append({"file_name": mate,
                           "match_reason": f"{fn} 的双端对家文件"})
            have.add(mate)
            notes.append("+" + mate)

    seen, uniq = set(), []                # 口径归一后同一张表可能出现两遍
    for a in assets:
        fn = str(a.get("file_name") or "").lower()
        if fn and fn in seen:
            continue
        seen.add(fn)
        uniq.append(a)
    if len(uniq) != len(assets):
        notes.append(f"去重 {len(assets) - len(uniq)}")
    return uniq, notes

def tool_hydrate_plan(args):
    """确定性补全：把 Plan 里所有「图谱/闭集目录本来就知道」的字段由服务端填上。

    调用方模型只需给出判断性内容（选哪个 pipeline、match_note、asset 的 file_name 与
    match_reason、intent），tool 的 description/inputs/outputs、asset 的
    format/data_level/file_path、原子链槽位、planner_metadata 等一律在此补全。
    好处有两个：省掉调用方逐 token 生成大段样板的时间；这些字段不再有被编造的机会
    （此前实测到模型自行编造 mcp_timing_ms 与 file_path）。"""
    t0 = time.time()
    plan = args.get("plan")
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except json.JSONDecodeError as e:
            return {"status": "error", "detail": f"plan 不是合法 JSON: {e}"}
    if not isinstance(plan, dict):
        return {"status": "error", "detail": "plan 必须是 JSON 对象或其字符串"}
    if plan.get("status") == "rejected":
        return {"status": "ok", "plan": plan, "filled": []}

    meta_to_graph = {c["meta_id"]: gid for gid, c in KC_MAP.items() if gid != c["meta_id"]}
    filled = []

    def _hydrate_tool(block, pid):
        gid = meta_to_graph.get(pid, pid)
        cat = CATALOG.get(gid)
        if not cat:
            return block          # 不在闭集：留给 validate_plan 报违规，不代为圆场
        card = KC_MAP.get(gid)
        block = dict(block or {})
        block.setdefault("tool_id", card["meta_id"] if card else gid)
        block["catalog_id"] = cat.get("catalog_id")
        block["tool_kind"] = cat.get("tool_kind")
        block.setdefault("name", cat.get("tool_name") or gid)
        if not block.get("description"):
            block["description"] = cat.get("description")
            filled.append(f"{pid}.description")
        if not block.get("inputs") or not block.get("outputs"):
            ins, outs = _card_slots(card) if card else _graph_tool_io(gid)
            block["inputs"] = block.get("inputs") or ins
            block["outputs"] = block.get("outputs") or outs
            filled.append(f"{pid}.io")
        return block

    # —— recommendations ——
    recs = plan.get("recommendations") or []
    want = [a.get("file_name") for r in recs for a in ((r.get("data") or {}).get("assets") or [])
            if a.get("file_name")]
    facts = _asset_facts(want)
    # 先按流程声明补齐/归一资产（会引入新文件名），再统一取图内字段
    for rec in recs:
        pid = rec.get("pipeline_id") or (rec.get("tool") or {}).get("tool_id")
        data = rec.get("data")
        if not pid or not isinstance(data, dict) or not isinstance(data.get("assets"), list):
            continue
        data["assets"], notes = _complete_assets(
            meta_to_graph.get(pid, pid), data["assets"], facts)
        if notes:
            filled.append(f"assets({pid}): " + ",".join(notes))
            data.pop("matched_count", None)      # 数量变了，别沿用调用方给的旧值
    facts.update(_asset_facts(
        [a.get("file_name") for r in recs for a in ((r.get("data") or {}).get("assets") or [])
         if a.get("file_name") and a.get("file_name") not in facts]))
    for i, rec in enumerate(recs):
        pid = rec.get("pipeline_id") or (rec.get("tool") or {}).get("tool_id")
        if not rec.get("match_id"):
            rec["match_id"] = "recommendation-" + hashlib.sha1(
                f"{pid}|{i}".encode()).hexdigest()[:6]
            filled.append(f"recommendations[{i}].match_id")
        rec.setdefault("rank", i + 1)
        rec.setdefault("source", "deterministic_rule+neo4j")
        rec.setdefault("reference_case_id", None)
        if pid:
            rec["tool"] = _hydrate_tool(rec.get("tool"), pid)
        data = rec.get("data")
        if isinstance(data, dict):
            for a in data.get("assets") or []:
                f = facts.get(a.get("file_name"))
                if not f:
                    continue
                for k in _ASSET_FIELDS:
                    if f.get(k) is not None and not a.get(k):
                        a[k] = f[k]
                if f.get("file_path"):        # 路径以图内记录为准，覆盖调用方给的值
                    a["file_path"] = f["file_path"]
                a.setdefault("read_pair", None)
                filled.append("asset:" + str(a.get("file_name")))
            data.setdefault("source", "neo4j")
            if data.get("assets"):
                data.setdefault("matched_count", len(data["assets"]))
                data.setdefault("missing_asset_names", [])
    plan["recommendation_count"] = len(recs)

    # —— candidates：原子链槽位一律按 Knowledge Card 补全 ——
    for c in plan.get("candidates") or []:
        for step in (c.get("tool_chain") or c.get("chain") or []):
            tid = step.get("tool_id")
            card = KC_MAP.get(meta_to_graph.get(tid, tid))
            if card and (not step.get("inputs") or not step.get("outputs")):
                ins, outs = _card_slots(card)
                step["tool_id"] = card["meta_id"]
                step["inputs"] = step.get("inputs") or ins
                step["outputs"] = step.get("outputs") or outs
                filled.append(f"chain:{tid}")
    plan["candidate_count"] = len(plan.get("candidates") or [])

    # —— 服务端元数据：这些是本 server 的运行事实，调用方不该也无法自行填写 ——
    plan.setdefault("schema_version", "tool-chain/v2")
    plan["planner_metadata"] = {"used": False, "status": "force_rule", "calls": 0, "stages": []}
    plan["data_matcher_mode"] = "neo4j"
    plan["mcp_timing_ms"] = round((time.time() - t0) * 1000, 1)
    return {"status": "ok", "plan": plan, "filled": filled}

def tool_health_check(args):
    try:
        rows = neo4j_q(["MATCH (n) RETURN count(n) AS nodes", "MATCH (n:tool) RETURN count(n) AS tools"])
        return {"status": "ok", "nodes": rows[0][0][0], "tools": rows[1][0][0],
                "atomic_closed_set": sorted(ATOMIC_IDS)}
    except Exception as e:
        return {"status": "unavailable", "detail": str(e)[:300]}

TOOLS = {
    "get_planning_guide": {
        "description": "返回生信链路规划 skill 全文（SKILL.md）。调用方模型应读取它后自行规划；本 server 不做推理。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": tool_get_planning_guide,
    },
    "read_cypher": {
        "description": "数据面：对 Neo4j 知识图谱执行只读 Cypher 查询。三重守卫：写入语句拒绝；患者级临床属性（`01_`–`13_` 全部编号前缀：人口学/家族史/生活史/血液学/病理/侵犯/分子指标/治疗史/生存；只有 `00_*` 操作性标识放行）只允许聚合统计或 IS NOT NULL 存在性判断，不允许取个体值；无 LIMIT 自动加 LIMIT 500。",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "description": "只读 Cypher，结果多时加 LIMIT"}}, "required": ["query"]},
        "handler": tool_read_cypher,
    },
    "read_cypher_batch": {
        "description": "批量只读 Cypher：一次调用执行多条相互独立的查询（每条与 read_cypher 同等守卫），结果按序返回 results[]。凡是不依赖上一条返回值的查询都必须打包进一次 batch，不要在多轮里逐条发。",
        "inputSchema": {"type": "object", "properties": {"queries": {"type": "array", "items": {"type": "string"}, "description": "相互独立的只读 Cypher 数组，单次最多 8 条"}}, "required": ["queries"]},
        "handler": tool_read_cypher_batch,
    },
    "get_study_overview": {
        "description": "队列画像一包到底：study 基本信息 + sample 节点数 + T1/T2 格式/策略分布 + T2 现成文件样例 + 样本角色分布（sample_roles/role_resolved/file_coverage）。选定队列后优先调它，一次拿齐「有什么数据、能不能配对/分组」，不要再用多条 read_cypher 分头查。",
        "inputSchema": {"type": "object", "properties": {"study": {"type": "string", "description": "队列号（如 HRA001272）"}}, "required": ["study"]},
        "handler": tool_get_study_overview,
    },
    "validate_atomic_chain": {
        "description": "确定性闭集校验：给定 atomic 工具链，校验闭集成员 + 图内 next_tool 邻接；输出 tool_chain 使用 Knowledge Card 的 meta.id 与卡内输入输出名称。",
        "inputSchema": {"type": "object", "properties": {"chain": {"type": "array", "items": {"type": "string"}, "description": "atomic tool_id 有序列表"}}, "required": ["chain"]},
        "handler": tool_validate_atomic_chain,
    },
    "resolve_sample_roles": {
        "description": "确定性样本角色判定（tumor/normal，规则移植自重版，不猜）。传 study 查图统计角色分布（sample_roles/role_resolved）+ 文件侧覆盖度（file_coverage），或传 records 对给定样本记录逐条判角色。配对/分组分析选数据前必须调用，不许模型自行推断角色。study 模式的 samples 默认只回 20 条预览，超出时带 samples_truncated——角色统计以 sample_roles 为准（已覆盖全部样本）。",
        "inputSchema": {"type": "object",
                        "properties": {"study": {"type": "string", "description": "队列号（如 HRA001272）"},
                                       "sample_limit": {"type": "integer", "description": f"study 模式下 samples 明细条数，默认 {SAMPLE_PREVIEW}，上限 {SAMPLE_LIMIT_MAX}"},
                                       "records": {"type": "array", "items": {"type": "object"},
                                                   "description": "样本记录数组，字段含 study_accession/tissue_type/specimen_type/sample_name"}},
                        "required": []},
        "handler": tool_resolve_sample_roles,
    },
    "validate_execution_chain": {
        "description": "提交前把关：五阶段探查（注册/卡契约必填输入/绑定结构/数据探查/链流转），输出 tool-chain-validation/v1.1 逐阶段报告 + execution_params（输入名→真实文件路径）+ execution_params_missing + submittable。steps: [{tool_id, inputs:{name: binding}}]。",
        "inputSchema": {"type": "object",
                        "properties": {"steps": {"type": "array", "items": {"type": "object"},
                                                 "description": "每步 {tool_id, inputs:{输入名: binding}}，binding 可为对象{file_id/file_name/format}或标量"},
                                       "cohort": {"type": "string", "description": "可选队列/癌种（如 肝癌），用于数据探查过滤"}},
                        "required": ["steps"]},
        "handler": tool_validate_execution_chain,
    },
    "validate_plan": {
        "description": "接地校验（模型输出前自检）：核验整份 tool-chain/v2 Plan 的工具是否在闭集目录、文件/路径/队列号是否图内真实存在。grounded=false 时按 violations 修正——防止调用方模型用内部知识编造答案内容。",
        "inputSchema": {"type": "object",
                        "properties": {"plan": {"description": "最终要输出的 tool-chain/v2 JSON（对象或字符串）"}},
                        "required": ["plan"]},
        "handler": tool_validate_plan,
    },
    "hydrate_plan": {
        "description": "确定性补全：把 Plan 中图谱/闭集目录本来就知道的字段由 server 填上（tool 的 description/inputs/outputs、asset 的 format/data_level/file_path、原子链的卡内槽位、match_id、planner_metadata/mcp_timing_ms）。调用方只需给判断性内容：pipeline_id、match_note、asset 的 file_name 与 match_reason、intent、selection_status。省生成时间，也杜绝这些字段被编造。",
        "inputSchema": {"type": "object",
                        "properties": {"plan": {"description": "精简版 tool-chain/v2 JSON（对象或字符串）"}},
                        "required": ["plan"]},
        "handler": tool_hydrate_plan,
    },
    "health_check": {
        "description": "检查 Neo4j 连通性、图谱规模与 atomic 闭集。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": tool_health_check,
    },
}

# ---------- MCP stdio 协议 ----------
def _send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": msg.get("id"),
                   "result": {"protocolVersion": "2024-11-05",
                              "capabilities": {"tools": {"listChanged": False}},
                              "serverInfo": {"name": "bio-pipeline-light", "version": "2.1.0"}}})
        elif method == "notifications/initialized":
            pass
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": msg.get("id"), "result": {}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": msg.get("id"),
                   "result": {"tools": [{"name": n, "description": t["description"], "inputSchema": t["inputSchema"]}
                                        for n, t in TOOLS.items()]}})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {}) or {}
            tool = TOOLS.get(name)
            if not tool:
                _send({"jsonrpc": "2.0", "id": msg.get("id"),
                       "result": {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}})
                continue
            try:
                out = tool["handler"](args)
                text = json.dumps(out, ensure_ascii=False, indent=1)
                _send({"jsonrpc": "2.0", "id": msg.get("id"),
                       "result": {"content": [{"type": "text", "text": text}], "structuredContent": out}})
            except Exception as e:  # noqa: BLE001
                _send({"jsonrpc": "2.0", "id": msg.get("id"),
                       "result": {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}})
        elif msg.get("id") is not None:
            _send({"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32601, "message": "method not found"}})

if __name__ == "__main__":
    main()
