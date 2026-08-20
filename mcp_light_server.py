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
import json
import os
import re
import subprocess
import sys
import tempfile

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
# individual 的编号前缀临床属性是患者级敏感数据：01_ 人口学(年龄/性别/种族)、03_ 生活史、
# 09_ 肿瘤病理、11_ 分子指标、13_ 生存。规划只允许聚合统计或存在性判断，不允许取个体值。
_SENSITIVE_PROP = r"`?(?:01|03|09|11|13)_\w+`?"
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
            f"read_cypher 隐私守卫：{hit.group(0)} 是患者级临床属性（01_人口学/03_生活史/09_病理/"
            "11_分子指标/13_生存），只允许聚合统计（count/avg/min/max…）或存在性判断（IS NOT NULL），"
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
        if len(samples) < 200:
            samples.append({**rec, "sample_role": role,
                            "sample_role_label": SAMPLE_ROLE_LABELS.get(role or "")})
    files, linked = (rows[1][0] if rows[1] else [0, 0])
    file_runs, sample_runs, orphan_runs = (rows[2][0] if rows[2] else [0, 0, 0])
    cover = {"t1_files": files, "t1_files_linked_to_sample": linked,
             "t1_files_unlinked": files - linked, "runs_on_files": file_runs,
             "runs_on_samples": sample_runs, "runs_without_sample_node": orphan_runs}
    notes = ["聚合类文件（表达矩阵/MAF/临床表）本就跨样本，sample_accession 为 null 属正常"]
    if orphan_runs:
        notes.append(f"本队列有 {orphan_runs}/{file_runs} 个 run 在图谱中没有对应 sample 节点，"
                     f"这些 run 的文件无法定位到样本——是图谱 run→sample 映射缺失，不要猜测归属")
    return {"status": "ok", "study": study,
            "sample_roles": counts,   # 按 distinct sample 计数（重版按 T1 文件计数，口径不同）
            "role_resolved": counts["tumor"] > 0 and counts["normal"] > 0,
            "samples": samples, "sample_count": sum(counts.values()),
            "file_coverage": cover, "notes": notes}

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
    if not recs:
        v.append("recommendations 为空（信息型回答也应有 rank1 推荐或改用 rejected/unsupported）")
    for i, rec in enumerate(recs):
        pid = rec.get("pipeline_id") or (rec.get("tool") or {}).get("tool_id")
        gid = meta_to_graph.get(pid, pid)
        if gid not in CATALOG:
            v.append(f"recommendations[{i}] 工具不在闭集目录（疑似模型编造）: {pid}")
        for a in (rec.get("data") or {}).get("assets") or []:
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
        "description": "数据面：对 Neo4j 知识图谱执行只读 Cypher 查询。三重守卫：写入语句拒绝；患者级临床属性（01_/03_/09_/11_/13_ 前缀）只允许聚合统计或 IS NOT NULL 存在性判断，不允许取个体值；无 LIMIT 自动加 LIMIT 500。",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "description": "只读 Cypher，结果多时加 LIMIT"}}, "required": ["query"]},
        "handler": tool_read_cypher,
    },
    "validate_atomic_chain": {
        "description": "确定性闭集校验：给定 atomic 工具链，校验闭集成员 + 图内 next_tool 邻接；输出 tool_chain 使用 Knowledge Card 的 meta.id 与卡内输入输出名称。",
        "inputSchema": {"type": "object", "properties": {"chain": {"type": "array", "items": {"type": "string"}, "description": "atomic tool_id 有序列表"}}, "required": ["chain"]},
        "handler": tool_validate_atomic_chain,
    },
    "resolve_sample_roles": {
        "description": "确定性样本角色判定（tumor/normal，规则移植自重版，不猜）。传 study 查图统计角色分布（sample_roles/role_resolved），或传 records 对给定样本记录逐条判角色。配对/分组分析选数据前必须调用，不许模型自行推断角色。",
        "inputSchema": {"type": "object",
                        "properties": {"study": {"type": "string", "description": "队列号（如 HRA001272）"},
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
