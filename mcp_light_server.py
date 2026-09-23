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
        cards = json.load(open(path, encoding="utf-8"))   # Windows 默认 GBK，不写死会静默加载失败
    except Exception:
        return
    for card_id, c in cards.items():
        gid = c.get("graph_tool_id") or card_id
        KC_MAP[gid] = {"meta_id": card_id,
                       "inputs": c.get("inputs", []),
                       "outputs": c.get("outputs", []),
                       # 二选一约束，来自交付卡的 interface.validators[type=one_of]
                       "require_any": c.get("require_any", [])}
        if gid != card_id:
            KC_MAP.setdefault(card_id, KC_MAP[gid])

load_knowledge_cards()

# ---------- WDL 类型判读 + 参考资源白名单（执行契约解析用） ----------
# 卡片里的类型字符串带 WDL 后缀：`Array[File]+`（非空数组）、`File?`（可选）。
# 早先三处都在拿字符串**精确相等**判 ("File", "Array[File]")，`Array[File]+` 三处全漏，
# 等于 fastqc/multiqc 这两张卡在执行参数解析里完全不存在——回包会是
# execution_params={} 且 missing=[]（零个参数、且一个都不缺），消费方按 not missing
# 判可提交，就把一个参数根本没解析出来的链当成能跑的。统一走下面三个函数，别再手写比较。
def _base_type(t: str) -> str:
    """WDL 类型 → 基础类型：`Array[File]+`→`File`、`File?`→`File`、`String?`→`String`。"""
    t = (t or "").strip().rstrip("+?")
    if t.startswith("Array[") and t.endswith("]"):
        t = t[len("Array["):-1].strip().rstrip("+?")
    return t

def _is_file_type(t) -> bool:
    return _base_type(t) == "File"

def _is_array_type(t) -> bool:
    return (t or "").strip().rstrip("+?").startswith("Array[")

# 带卡片默认值的参考/索引资源：**既不映射进 execution_params，也不报缺**——执行端用容器内
# 的默认值，用户塞进来的路径反而会覆盖掉正确默认值。
#
# ⚠ 必须是显式 (meta_id, param) 白名单，**不许按名字猜**。"名字里带 index/reference/gtf
# 就是参考资源"这条启发式在本目录里是错的：
#     bcftools_somatic_postprocess.filtered_vcf_index   File  TBI  required=true
# 它名字里带 index，却是**数据文件的伴随索引**，没有卡片默认值，缺了 bcftools 的
# `ln -sf` 读不到 .tbi，执行直接失败。重版就是踩了这个坑（按 "index" 关键字把它判成参考
# 资源，于是既不映射也不报缺），0823 才修掉。新工具接入时手动往这张表里加。
REFERENCE_RESOURCES: set = {
    ("star_rrna_and_genome_alignment", "rrna_star_index"),
    ("star_rrna_and_genome_alignment", "genome_star_index"),
    ("rsem_quantification",            "rsem_index"),
    ("featurecounts_gene_counting",    "gtf_file"),
    ("gatk_wes_somatic",               "interval_list"),
    # 下面四条是同一个索引在 pipeline 级卡片上的写法。原子卡片里它们都带
    # reference_resource=True（star 的 rrna_star_index/genome_star_index、rsem 的
    # rsem_index、featurecounts 的 gtf_file），但 rnaseq_singletask 这张 pipeline 卡
    # 交付包既没给 artifact_type 也没给 /opt/... 默认值，生成器按「不许按名字猜」的
    # 判据只能留空——于是每次推荐 rnaseq_singletask 都凭空多报四条 no_confirmed_path
    # （0826 抽测 100 例里它被推荐 5 次，5 次全中）。证据是原子卡片上的那个标记，
    # 不是名字，所以列在这张手工表里而不是去放宽生成器判据。
    # 注意 pipeline 卡写的是 star_genome_index，原子卡是 genome_star_index，别对齐错。
    ("rnaseq_singletask",              "rrna_star_index"),
    ("rnaseq_singletask",              "star_genome_index"),
    ("rnaseq_singletask",              "rsem_index"),
    ("rnaseq_singletask",              "gtf_file"),
    ("wes_somatic_pair",               "interval_list"),   # 原子卡 gatk_wes_somatic 里带标记
}

def _is_reference_resource(card, name) -> bool:
    """参考资源判定：先看卡片自带的 reference_resource 标记，再看上面这张兜底表。

    标记由 scripts/build_knowledge_cards.py 从交付包生成，判据是两条硬证据——
    artifact_type 属于参考资源类，或卡片给了 /opt/... 这样的容器内绝对路径默认值。
    上面那张手工表覆盖不了 0824 新进来的 33 个工具，留着是防交付包哪天不带这个字段。
    """
    if not card:
        return False
    for i in card.get("inputs") or []:
        if i.get("name") == name:
            if i.get("reference_resource"):
                return True
            break
    return (card.get("meta_id"), name) in REFERENCE_RESOURCES

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
                "动态属性访问——这会导出患者级临床属性。请显式点取非临床字段（如 `00_individual_accession`）。")
        # RETURN 段禁止整节点导出（count(v)/id(v) 允许；v.prop 点取由属性守卫把关）
        for mseg in re.finditer(r"\bRETURN\b(.*?)(?=\b(?:MATCH|WHERE|UNWIND|CALL|UNION|ORDER|SKIP|LIMIT)\b|$)",
                                query, re.IGNORECASE | re.S):
            seg = mseg.group(1)
            seg = re.sub(rf"\bcount\s*\(\s*(?:DISTINCT\s+)?{v}\s*\)", " ", seg, flags=re.IGNORECASE)
            seg = re.sub(rf"\b(?:id|elementId)\s*\(\s*{v}\s*\)", " ", seg, flags=re.IGNORECASE)
            if re.search(rf"(?<![\w.]){v}(?![\w.])", seg):
                raise ValueError(
                    f"read_cypher 隐私守卫：禁止整体 RETURN individual 节点（变量 {v}）——"
                    "请显式点取所需的非临床字段（如 `00_individual_accession`）或用 count() 聚合。")

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
            "（`00_individual_accession` 等），或改成 count/avg 等聚合。")

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
    pending_any = []        # 延后的 require_any 组：(step_idx, meta_id, grp)，见下
    for s_i, s in enumerate(steps):
        gid, given = _norm(s.get("tool_id"))
        card = KC_MAP.get(gid)
        bindings = s.get("inputs") or {}
        if not card:
            warnings.append(f"{given}: 无 Knowledge Card（pipeline 级或未收录），跳过契约校验")
            normalized.append({"tool_id": given, "card": None})
            continue
        # 必填输入检查（参考资源有卡片默认值，缺了不算缺——见 REFERENCE_RESOURCES）。
        # 临床表/样本元信息表同样不算调用方欠的：手册要求「一律不查、不写进 inputs」，
        # 服务端按队列号补（见 _needs_clinical）。这里不豁免就变成「照手册做 = 报错」。
        # 标识参数（sample_id/pair_id/…）同理豁免：它们由服务端从绑定文件与队列确定性
        # 解析（见 _resolve_id_params），补不出来在执行参数阶段报 literal_required。
        _clin_names = _needs_clinical(gid)
        missing = [i["name"] for i in card["inputs"]
                   if i.get("required", True) and i["name"] not in bindings
                   and not _is_reference_resource(card, i["name"])
                   and i["name"] not in _clin_names
                   and i["name"] not in _ID_RESOLVABLE
                   # 队列级数组槽（cnvkit 角色槽/germline analysis_ready_*/肿瘤演化数组）
                   # 由服务端按队列补齐（见执行参数阶段 _cohort_fill_entries），不算调用方欠的
                   and not _cohort_fillable_slot(gid, i["name"], i.get("type"), i.get("format"))]
        if missing:
            errors.append(f"{card['meta_id']} 缺必填输入: {missing}")
        # 二选一约束：卡片 interface.validators 里的 one_of，每组至少绑一个。
        # bulk10 十个工具的样本表就是这个形状——meta_xlsx 一张表顶 sample_csv +
        # individual_csv 两张，三个参数各自 required=false，只查必填查不出「一个都没给」。
        # 但 bulk10 的这两张 CNCB 原生元数据表由服务端按队列号推（_bulk10_params），
        # 手册明写「不写进 inputs 也不查图」，所以含它们的组不能反过来要求调用方绑。
        # 评估延后到执行参数解析之后：paired_fastq 的 sample_name/sample_accession 这组
        # 由服务端补（_ID_RESOLVABLE），只看调用方 bindings 会把已补上的组误判成没绑。
        for grp in card.get("require_any") or []:
            if gid in _BULK10 and set(grp) & {"sample_csv", "individual_csv"}:
                continue
            pending_any.append((s_i, card["meta_id"], grp))
        # 绑定结构检查（对齐重版：binding 必须为对象；Array[File] 额外允许对象数组）
        bad_bind = []
        for i in card["inputs"]:
            b = bindings.get(i["name"])
            if b is None:
                continue
            if _is_file_type(i.get("type")):
                if _is_array_type(i.get("type")):
                    ok = isinstance(b, dict) or (isinstance(b, list) and b
                                                 and all(isinstance(x, dict) for x in b))
                    if not ok:
                        bad_bind.append(f"{i['name']} binding 必须为对象或非空对象数组")
                elif not isinstance(b, dict):
                    bad_bind.append(f"{i['name']} binding 必须为对象")
            elif _base_type(i.get("type")) in ("Boolean", "Int", "Float"):
                if not isinstance(b, (bool, int, float)):
                    bad_bind.append(f"{i['name']} binding 类型应为 {i['type']}")
        if bad_bind:
            errors.extend(f"{card['meta_id']}: {x}" for x in bad_bind)
        # 卡片之外的参数会被执行端静默忽略（如实测 her2_pfs_survival 没有 gene 槽位，
        # 传入 ERBB3 被脚本 hardcode 兜回 HER2，属 no-op）——如实告警，别悄悄答非所问。
        _known = {i["name"] for i in card["inputs"]}
        for _bn in bindings:
            if _bn not in _known:
                warnings.append(f"{card['meta_id']}: 绑定了卡片之外的参数 {_bn}（执行端会忽略）")
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
            # 只探查"必需的、或用户明确绑了的"File 输入；参考资源不探（走容器内默认值）
            if not _is_file_type(i.get("type")) or _is_reference_resource(card, i["name"]):
                continue
            b = (s.get("inputs") or {}).get(i["name"])
            if not i.get("required", True) and b is None:
                continue
            fmt = (i.get("format") or "").upper()
            kw = next((k for k in ("FASTQ", "BAM", "BAI", "VCF", "TSV", "GTF", "FASTA", "TBI") if k in fmt), None)
            _bs = b if isinstance(b, list) else [b]
            bound = any(isinstance(x, dict) and (x.get("file_id") or x.get("file_name")) for x in _bs)
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
                # 同名多路径时按 `_node_rank` 择优，别随手取第一条：HRA007169 那 76 个 VCF
                # 在 `analysis_bak/mutect2` 下各有一份备份，取到备份就是拿旧结果去跑。
                f = _asset_facts([fname]).get(fname) or {}
                p = str(f.get("file_path") or "")
                if p.startswith("/") and "NOT_FOUND" not in p:
                    return p
            except Exception:
                pass
        return ""
    def _resolve(binding, is_array):
        """返回 array 参数的路径数组（并集去重、保持顺序）或标量参数的路径字符串；空=未解析。"""
        if not is_array:
            return _real_path(binding)
        items = binding if isinstance(binding, list) else [binding]
        paths, seen = [], set()
        for it in items:
            p = _real_path(it)
            if p and p not in seen:
                seen.add(p)
                paths.append(p)
        return paths
    by_step, execution_params, exec_missing, ambiguous = [], {}, [], set()
    # 链级样本事实一次算好、逐步共享：多步链里中游步骤的文件输入是上游产物，自己没有
    # 图内文件可查，但同一条数据在链里流动，sample_id 等标识沿链一致（见 _resolve_id_params）。
    _all_fns = [fn for _s in steps
                for fns in _bound_file_names(_s.get("inputs") or {}).values() for fn in fns]
    chain_facts = _file_sample_facts(_all_fns) if _all_fns else None
    for idx, s in enumerate(steps):
        gid, given = _norm(s.get("tool_id"))
        card = KC_MAP.get(gid)
        bindings = s.get("inputs") or {}
        tool_key = card["meta_id"] if card else given
        _clin = _needs_clinical(gid) if card else {}
        # 队列号先定（下面的角色数组补全要用）：从已绑资产反查（图内文件名与路径里都带
        # HRA######），恰好一个才认；零个说明队列还没定，多个说明这一步混了队列。
        _accs0 = set()
        for _b in bindings.values():
            _accs0 |= set(_HRA.findall(json.dumps(_b, ensure_ascii=False)))
        _study = cohort or (next(iter(_accs0)) if len(_accs0) == 1 else None)
        # 队列级数组槽的服务端补全（三族：cnvkit 配对角色槽 / gatk_germline 的
        # analysis_ready_* / tumor_evolution 的三证数组——见 _cohort_fill_entries）：
        # 这些槽是队列语义，调用方只给代表性文件时缺的由服务端补齐。已绑的槽不动：
        # 补全集按已绑 run 过滤，保证各数组按对/按 run 平行（48 个 id 配 1 个 BAM
        # 会被执行端 Validate 拒，2026-09 实踩）。
        _fill_slots = [i["name"] for i in (card["inputs"] if card else [])
                       if _cohort_fillable_slot(gid, i.get("name"), i.get("type"), i.get("format"))]
        if _fill_slots and _study and any(not bindings.get(_n) for _n in _fill_slots):
            _slot_l = {str(_n).lower() for _n in _fill_slots}
            _bound_runs = {m.group(0) for _pn, _fns in _bound_file_names(bindings).items()
                           if str(_pn).lower() in _slot_l
                           for _f in _fns
                           for m in [re.search(r"HRR\d+|HRS\d+", str(_f))] if m}
            _entries = _cohort_fill_entries(gid, _study, _bound_runs)
            for _pn in _fill_slots:
                if bindings.get(_pn):
                    continue
                _vals = [{"file_path": _p, "file_name": _p.rsplit("/", 1)[-1]}
                         for _, _p in _entries.get(str(_pn).lower(), [])]
                if _vals:
                    bindings[_pn] = _vals
                    warnings.append(f"{tool_key}.{_pn}: 调用方未绑，服务端按队列 {_study} "
                                    f"补齐 {len(_vals)} 项（与同族槽位平行）")
        # 标量角色槽（tumor_bam/normal_bam 单对 File，manta/gatk 的契约）：配对的另一侧
        # 没绑时按同个体补上（manta 卡片把 normal 标可选=肿瘤单样本模式能跑，但图里有
        # 配对就补上，配对分析才是完整答案）。什么都没绑时四个槽一起落第一对。
        _scalar_role = [i["name"] for i in (card["inputs"] if card else [])
                        if re.fullmatch(r"(tumor|normal)_(bam|bai)", str(i.get("name") or "").lower())
                        and not _is_array_type(str(i.get("type") or ""))]
        if _scalar_role and _study and any(not bindings.get(_n) for _n in _scalar_role):
            _first, _by_run = _paired_scalar_fill(_study)
            if _first:
                _any_run = next((m.group(0)
                                 for _fns in _bound_file_names(bindings).values()
                                 for _f in _fns
                                 for m in [re.search(r"HRR\d+|HRS\d+", str(_f))] if m), None)
                _src = _by_run.get(_any_run) or _first
                for _sn in _scalar_role:
                    if not bindings.get(_sn) and _src.get(_sn):
                        _p = _src[_sn]
                        bindings[_sn] = {"file_path": _p, "file_name": _p.rsplit("/", 1)[-1]}
                        warnings.append(f"{tool_key}.{_sn}: 服务端按队列 {_study} 的同个体配对补齐")
        # 随文件索引补全（BAI/TBI/FAI/CRAI）：调用方只交数据文件时，索引槽从本步已绑文件
        # 推导候选名（X.bam.bai 追加 / X.bai 换尾两种写法都试）并在图内验真——hydrate_plan
        # 的 ⑤ 在计划资产层做过同一件事（_INDEX_SEM），这里是提交层的对应补齐。
        if card:
            _all_bound = [fn for fns in _bound_file_names(bindings).values() for fn in fns]
            for _i in card["inputs"]:
                _n = str(_i.get("name") or "")
                if bindings.get(_n) or not _is_file_type(_i.get("type")) \
                        or _is_reference_resource(card, _n):
                    continue
                _ext = next((_e for _e, _k in ((".bai", "BAI"), (".tbi", "TBI"),
                                               (".fai", "FAI"), (".crai", "CRAI"))
                             if _k in str(_i.get("format") or "").upper() or _k in _n.lower()), None)
                if not _ext or not _all_bound:
                    continue
                _hit = None
                for _bf in _all_bound:
                    for _cand in _index_names(_bf, _ext):
                        try:
                            _f = _asset_facts([_cand]).get(_cand) or {}
                        except Exception:
                            break                    # 图不通就放弃补索引，原样报缺
                        if str(_f.get("file_path") or "").startswith("/"):
                            _hit = _cand
                            break
                    if _hit:
                        break
                if _hit:
                    bindings[_n] = {"file_name": _hit}
                    warnings.append(f"{tool_key}.{_n}: 服务端补随文件索引 {_hit}")
        # wanted 必须在补全之后构造：服务端补上的可选槽（如 manta normal_bam）要在列，
        # 不然补了 bindings 却不解析进 params——补了等于没补（manta 实踩）。
        if card:
            # 判据是「必需的 **或** 用户绑了的」，不是「必需的」：可选的 File? 被用户明确绑上
            # 却丢掉，执行端 `is_paired = defined(read2)` 就变 false，**双端数据静默按单端跑完，
            # 一路绿灯而结果是错的**。参考资源反过来一律不映射（塞进去会覆盖容器内默认值）。
            wanted = [(i["name"], _is_array_type(i.get("type"))) for i in card["inputs"]
                      if _is_file_type(i.get("type"))
                      and not _is_reference_resource(card, i["name"])
                      and (i.get("required", True) or i["name"] in bindings)
                      # bulk10 的两张 CNCB 原生元数据表由服务端从队列号推（见 _bulk10_params）。
                      # 图里只有 HRA000001 一份 sample.csv/individual.csv，走 _resolve 必然
                      # 解析失败并留下一条假的 no_confirmed_path，误导调用方去数据侧要文件。
                      and not (gid in _BULK10 and i["name"] in ("sample_csv", "individual_csv"))
                      # 非 bulk10 的临床表/样本元信息表同理由服务端按队列号补（见 _clin）：
                      # 手册明写「一律不查」，调用方照办后这两个必填参数就永远解析不出来。
                      and i["name"] not in _clin]
        else:   # pipeline 级无卡：有对象/对象数组 binding 的输入都当 File 处理
            wanted = [(k, isinstance(v, list)) for k, v in bindings.items()
                      if isinstance(v, (dict, list))]
        params = {}
        for name, is_arr in wanted:
            val = _resolve(bindings.get(name), is_arr)
            if val:
                params[name] = val
            else:
                # reason 有处置含义：no_confirmed_path = 绑定正确但图里没有该资产的确认路径
                # （数据侧补 file_path）。light 走 Knowledge Card 而非槽表，没有 slot_not_bound。
                exec_missing.append({"param": name, "tool_id": tool_key, "step": idx,
                                     "reason": "no_confirmed_path"})
        # String 型标识参数（sample_id/pair_id/tumor_id/group_*_samples…）：不是判断性内容，
        # 服务端从绑定文件与队列确定性解析（见 _resolve_id_params），模型不需要也不该自己编
        # （编了也不会报错，是最危险的一种幻觉）。图内事实覆盖调用方给的值（与 file_path
        # 的处置一致）；补不出来且调用方没给的，如实报 literal_required。
        # （_study 已在执行参数解析前定好，见上）
        # 实跑失败黑名单：该「流程 × 队列」组合明确跑挂过（数据侧原因），直接判不可提交
        _fail = _failed_run(gid, _study)
        if _fail:
            errors.append(f"{tool_key} × {_study} 有实跑失败记录（{_fail}）——不可提交，"
                          f"请改选别的队列（failed_runs.tsv，解禁就删行）")
        else:
            # 白名单闸门：有成功记录的工具只放行表内组合（「只推实跑验证过的」）
            _unp = _unproven_combo(gid, _study)
            if _unp:
                errors.append(f"{tool_key}: {_unp}")
        _id_vals, _id_missing = _resolve_id_params(gid, card, _bound_file_names(bindings), _study,
                                                   chain_facts=chain_facts)
        for _n, _v in _id_vals.items():
            # 分组/样本清单是「意图」不是「事实」：调用方（或前端用户）显式给了就照用，
            # 不让服务端的角色默认覆盖用户选择。事实型标识仍以图为准覆盖。
            if _n in bindings and (_n in _ID_ARRAY_GROUP or _n in _ID_ARRAY_ALL):
                continue
            params[_n] = _v
        # 调用方显式给了值、服务端没覆盖的标识参数，原值搬进 params（不留空）
        for _n in _ID_RESOLVABLE:
            if _n in bindings and _n not in params and bindings[_n] is not None:
                params[_n] = bindings[_n]
        # 其它调用方给的**卡内**标量字面量（gene 这类问题语义参数：图里推不出来、也不该推）
        # 同样原样进 params——不搬就是 silent drop：以前 survival 的 gene_symbol 就是这么丢的，
        # 合同绿灯而执行端拿不到基因。卡外的标量保持不动（stage-2 已对它们告警）。
        _card_names = {i["name"] for i in card["inputs"]} if card else None
        for _n, _b in bindings.items():
            if _n not in params and isinstance(_b, (str, int, float, bool)) \
                    and (_card_names is None or _n in _card_names):
                params[_n] = _b
        for _n in _id_missing:
            if _n not in bindings:
                exec_missing.append({"param": _n, "tool_id": tool_key, "step": idx,
                                     "reason": "literal_required"})
        # 延后的二选一约束：服务端的解析结果也算满足（见 stage 2 的注释）
        for _ci, _mid, _grp in pending_any:
            if _ci == idx and not any(n in bindings or n in params for n in _grp):
                errors.append(f"{_mid} 缺必填输入: {_grp} 至少需提供一个")
        if _clin:
            # 队列号从已绑资产反查（图内文件名与路径里都带 HRA######）。**恰好一个**才补：
            # 零个说明队列还没定，多个说明这一步混了队列，补哪份临床表都是错的，如实报缺。
            _accs = set()
            for _b in list(bindings.values()) + list(params.values()):
                _accs |= set(_HRA.findall(json.dumps(_b, ensure_ascii=False)))
            if not _accs and _study:
                _accs = {_study}      # 绑定里不含队列号（如全补全场景）时回落到调用方给的队列
            # 两族分开查图：XLSX 那对与 CSV 那组同队列各一份，但语义格式不重叠。
            # 一次查全套会把没声明的那族也捞进来，`_clinical_pair_files` 的
            # 「三张齐全的目录胜出」判据随之失真。
            _pairs = {}
            for _fam in ({v for v in _clin.values() if v in _CLINICAL_PARAM_FMT.values()},
                         {v for v in _clin.values() if v in _CSV_META_PARAM_FMT.values()}):
                if _fam and len(_accs) == 1:
                    _pairs.update(_clinical_pair_files(next(iter(_accs)), gid, fmts=_fam))
            for _n, _fmt in _clin.items():
                _hit = _pairs.get(_fmt)
                if _hit:
                    params[_n] = _hit[1]
                else:
                    # 两种缺法要分开：队列没定是调用方还没选数据（补 assets 就能解），
                    # 队列定了但图里没这张表是数据侧的事。混成一个 reason 会把前者
                    # 误导成"去数据侧要文件"——师兄 0826 反馈的正是这一条。
                    exec_missing.append({"param": _n, "tool_id": tool_key, "step": idx,
                                         "reason": "no_confirmed_path" if len(_accs) == 1
                                                   else "study_not_resolved"})
        if gid in _BULK10:
            params.update(_bulk10_params(gid, params.get("expr", ""), bindings, errors))
        by_step.append({"step": idx, "tool_id": tool_key, "params": params})
        # 扁平视图：键就是卡片参数名（不加 `tool.` 前缀，与 knowledge_card 的 name 完全一致）。
        # 同名参数跨步取到不同路径时**从扁平视图里剔除并记进 ambiguous**——宁可缺，
        # 也不能让消费方拿到静默被覆盖的错路径。多步链请读 execution_params_by_step。
        for name, val in params.items():
            if name in execution_params and execution_params[name] != val:
                ambiguous.add(name)
            execution_params[name] = val
    # require_any 的违规是延后在执行参数解析阶段才进 errors 的，这里补齐阶段结论
    for _st in stages:
        if _st["stage"] == "knowledge_card_contract":
            _st["passed"] = not any("缺必填输入" in e for e in errors)
            _st["findings"] = [e for e in errors if "缺必填输入" in e]
    for name in ambiguous:
        execution_params.pop(name, None)
    submittable = not errors and not exec_missing
    return {"schema_version": "tool-chain-validation/v1.2", "mode": "execution_contract",
            "valid": not errors, "validation": {"ok": not errors, "errors": errors, "warnings": warnings},
            "stages": stages, "normalized_steps": normalized,
            "execution_params": execution_params,
            "execution_params_by_step": by_step,
            "execution_params_missing": exec_missing,
            "execution_params_ambiguous": sorted(ambiguous),
            "submittable": submittable,
            "hint": "提交前把关：errors 清零且 execution_params_missing 为空（submittable=true）才可提交执行端。"
                    "键与 Knowledge Card 参数名一致；Array[File] 参数的值是路径数组，消费方不要假定一定是字符串。"
                    "多步链以 execution_params_by_step 为准——execution_params 是扁平便捷视图，"
                    "同名参数跨步冲突时会被剔除并列进 execution_params_ambiguous。"
                    "sample_id/pair_id/tumor_id 等标识参数由服务端从绑定文件与队列确定性解析"
                    "（reason=literal_required 表示图内推不出，请补绑数据文件或队列号）。"}

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

def _step_tool_id(step):
    """原子链的一步 → tool_id。

    契约里一步是 `{"tool_id": "..."}`，但手册说「链每步除 tool_id 外全部由服务端补」，
    调用方很自然就写成裸名字数组 `["fastqc","star",...]`。以前这里直接 `step.get` 抛
    `'str' object has no attribute 'get'`：**整个 validate_plan / hydrate_plan 崩掉**，
    接地校验静默失效——实测 c56/c92 因此把 recommendations 为空的 Plan 直接交付了
    （本该被「selection_status=ok 必须有 rank1」拦下并修一轮）。裸名字是合法写法，收下。
    """
    return step.get("tool_id") if isinstance(step, dict) else str(step)

_HRA = re.compile(r"HRA\d{6}")

def _rec_studies(rec):
    """一条推荐实际指向的队列号集合。

    队列号在契约里有四个可能的落点，模型每次挑的不一样：`data.study_accessions`、
    每个 asset 的 `study_accession`、文件名前缀（`HRA003107-Genes-counts-1.0.tsv`）、
    路径里的 `/HRA003107/`。只认其中一处就会漏掉——bulk10 白名单是硬约束，漏判等于没判，
    所以四处全扫取并集。
    """
    out = set()
    data = rec.get("data") or {}
    for st in data.get("study_accessions") or []:
        out |= set(_HRA.findall(str(st)))
    out |= set(_HRA.findall(str(rec.get("study_accession") or "")))
    for a in data.get("assets") or []:
        if isinstance(a, str):
            out |= set(_HRA.findall(a))
        elif isinstance(a, dict):
            for k in ("study_accession", "file_name", "name", "file_path", "path"):
                out |= set(_HRA.findall(str(a.get(k) or "")))
    return out

def _alias_recs(plan):
    """把 `recommendations` 的拼写变体收编回正名，改了返回 True。

    100 例回归实测：模型偶发把这个键写成 `recommenditions`（同一批 id 上一轮 0 次、
    下一轮 3 次，纯采样抖动）。后果是整条推荐凭空消失——`recommendation_count` 归零、
    前端与评分都读不到，而模型其实已经把该给的内容完整生成了。与下面「顶层 answer 归一」
    同一处置：已经生成的内容不因一个键名拼错而作废，也不必为此多烧一轮修正。
    判据卡得很紧——只认 `recommend` 开头**且值是对象数组**的键，`recommendation_count`
    是整数，不会被误收。"""
    if plan.get("recommendations"):
        return False
    for k in list(plan):
        if k == "recommendations" or not str(k).startswith("recommend"):
            continue
        val = plan[k]
        if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
            plan["recommendations"] = plan.pop(k)
            return True
    return False

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
    _alias_recs(plan)
    if plan.get("schema_version") != "tool-chain/v2":
        v.append("schema_version 缺失或不是 tool-chain/v2")
    meta_to_graph = {c["meta_id"]: gid for gid, c in KC_MAP.items() if gid != c["meta_id"]}
    recs = plan.get("recommendations") or []
    # 空 recommendations 只在「闭集里真的没有可给的流程」时合法：unsupported（需求超出闭集）、
    # no_candidate / missing_from_graph（图内查无）。**information 已从白名单撤下**——它原本是
    # 给「HRA001272 角色分布如何」这类纯信息题留的口子，实测却成了模型逃避出流程的通道：
    # qa_final class3 450 例里 73 例（16.2%）判 information 交空 recommendations，其中 66 例
    # answer 里工具名是对的，也就是模型答对了、执行端却拿不到任何可提交的东西。产品口径已改为
    # 「任何问句都要出流程」，所以这里让它直接判违规、走修正轮补 rank1。
    _NO_REC_OK = {"unsupported", "no_candidate", "missing_from_graph"}
    _sel = str(plan.get("selection_status") or "").lower()
    if _sel == "rejected":
        # 拒绝写成了 v2 信封里的 selection_status，而契约要的是**裸对象**。以前这条落到下面
        # 那句「必须有 rank1 推荐，否则改用 rejected」上——模型明明已经写了 rejected，这句
        # 读起来就是个空操作，于是修正轮原样再交一遍（实测 q15 连续两轮同一违规）。
        # 违规文案必须指出「形状错了」而不是「状态错了」，否则这一轮白烧。
        v.append("拒绝不能包在 tool-chain/v2 信封里：请只输出裸对象 "
                 '{"status":"rejected","reason":"off_topic: …"} 或 '
                 '{"status":"rejected","reason":"privacy: …"}，'
                 "不要 schema_version/candidates/recommendations 等任何其他字段")
    elif not recs and _sel not in _NO_REC_OK:
        v.append("recommendations 为空：任何问句都必须给 rank1 推荐——问工具属性的问句也一样"
                 "（属性写进 answer，同时给出问句所指工具那条推荐）。只有 unsupported"
                 "（需求超出闭集）/ no_candidate（图内查无）允许为空，"
                 "非生信或隐私问题改用裸 rejected 对象。"
                 "selection_status=information 已废弃，不再是空推荐的合法理由")
    # 空 recommendations 合法**不等于**可以不回答。165 例实测：99 例交了空 recommendations，
    # 其中 72 例整个 JSON 里一个自然语言字段都没有——「cellranger_workflow 和 breast_cellchat
    # 有哪些共同输入格式」这种题，模型查完图、判了 information，然后交了个空壳，用户什么也
    # 没拿到。剩下 27 例把答案挂在自创顶层字段上（match_note 21 / note 3 / summary 2 /
    # explanation 1），前端一个都不认。所以顶层 `answer` 是契约字段：没有推荐时它就是答案本身。
    if not recs and _sel in _NO_REC_OK and not str(plan.get("answer") or "").strip():
        v.append("recommendations 为空时必须给顶层 answer：用自然语言直接回答问题"
                 "（涉及的 tool_id/格式/队列号要写全）。空 recommendations + 空 answer "
                 "= 什么也没回答")
    # `no_candidate` 字面意思是「闭集里没有能做这件事的工具」。可实测里它几乎总是被用来说
    # 「没有匹配的**数据**」——「我想在急性早幼粒细胞白血病队列里做细胞通讯分析」判 no_candidate，
    # 而同一份 answer 里白纸黑字写着 scrna_cell_communication。自己刚点了名的候选，不是「没有候选」。
    # 手册写了这条规则但只有六七成会照做（165 例里反复出现 Q_0044/Q_0055 这类抖动），
    # 所以在这儿判死：answer 里出现任何闭集 tool_id，`no_candidate` + 空推荐就是自相矛盾。
    # 只卡 `no_candidate`——`unsupported` 要留给「因果推断/机制断言」这类本就不该给推荐的问题
    # （实测 Q_0190/Q_0290 就是靠空推荐才判对的），一起卡会把拒绝纪律拆掉。
    if not recs and _sel == "no_candidate":
        _ans = str(plan.get("answer") or "")
        _named = [t for t in CATALOG
                  if re.search(r"(?<![A-Za-z0-9_])" + re.escape(t) + r"(?![A-Za-z0-9_])", _ans)]
        if _named:
            v.append(
                f"selection_status=no_candidate 与 answer 自相矛盾：answer 里已经点名了闭集工具"
                f"（{'、'.join(_named[:4])}）。no_candidate 只用于「闭集 55 个工具没有一个能做」；"
                f"缺的是数据/队列就不算。把其中最接近目标的一条放进 recommendations[0]、"
                f"状态改回 ok，数据缺口写进 match_note")
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
        # 实跑失败黑名单：该「流程 × 队列」组合明确跑挂过（数据侧原因），不可交付，
        # 让模型修正轮改选别的队列——别等执行端再烧一次（failed_runs.tsv，解禁就删行）。
        for _st in _rec_studies(rec):
            _reason = _failed_run(gid, _st)
            if _reason:
                v.append(f"recommendations[{i}] {gid} × {_st} 有实跑失败记录（{_reason}）。"
                         f"该组合不可交付：请改选其它队列重出 Plan")
            else:
                # 白名单闸门：有成功记录的工具只放行表内组合（「只推实跑验证过的」）
                _unp = _unproven_combo(gid, _st)
                if _unp:
                    v.append(f"recommendations[{i}] 未过实跑白名单：{_unp}")
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
            # 旧版临床表整族不在图内（见 `_LEGACY5_PATHS`），照常校验就是把服务端自己
            # 按实跑记录补的路径判成"模型编造"。豁免绑死在「文件名 + 那条 analysis 路径」
            # 这一对上：换个路径照样进下面的图内校验。
            if a.get("file_path") and a["file_path"] == _LEGACY5_PATHS.get(str(fn)):
                continue
            # 「属于」而不是「等于」：同名多路径时（见 `_name_paths`）按某一条比对，
            # 调用方给的另一条同样真实的路径会被误判成编造——HRA003107/HRA007167
            # 那 310 个同名 BAM 会稳定踩中这条。
            real = _name_paths([fn]).get(str(fn)) or set()
            if not real:
                v.append(f"asset 图内不存在（疑似模型编造）: {fn}")
            else:
                fp = a.get("file_path")
                if fp and fp not in real:
                    v.append(f"asset file_path 与图内记录不符: {fn}")
        for st in (rec.get("data") or {}).get("study_accessions") or []:
            if not _SAFE_FILE.fullmatch(str(st)):
                v.append(f"study 号非法: {st}")
                continue
            rows = neo4j_q([f"MATCH (s:study {{study_accession: '{st}'}}) RETURN count(s)"])
            if not (rows and rows[0] and rows[0][0][0] > 0):
                v.append(f"study 图内不存在（疑似模型编造）: {st}")
        # bulk10 的「流程 × 队列」白名单：图内存在 ≠ 这条流程在它上面跑过。
        # 这份白名单本来只挂在 validate_execution_chain（提交路径）上，validate_plan
        # 一路放行——网页 agent 循环走的正是 validate_plan，于是 450 例里出现
        # km_survival × HRA001272（该流程只跑过 HRA003107/000073/000074/002693/006117）
        # 7 次、功能富集默认落 HRA001272 19 次（图里有 Genes-counts，但十条流程一条都
        # 没在它上面跑过）。手册 §8.2 白纸黑字写了这张表，模型照样违反——跟 information
        # 一样，光靠劝没用，得在这儿判违规、走修正轮。
        _proven = _BULK10_RUNS.get(gid)
        if _proven:
            for st in sorted(_rec_studies(rec)):
                if st not in _proven:
                    v.append(
                        f"recommendations[{i}] {gid} × {st} 没有实跑记录：{gid} 只在 "
                        f"{'/'.join(sorted(_proven))} 上跑通过。选数据只能从这几个队列里选，"
                        f"不许按七队列并集选。要么换成这些队列之一的 "
                        f"{{STUDY}}-Genes-counts-1.0.tsv，要么改荐一条支持 {st} 的流程；"
                        f"用户点名的组合不在表内就直说该流程支持哪几个队列，别静默替换")
        # 五个旧工具同理，白名单来自 legacy5_proven_runs.tsv（22 条 Succeeded 实跑）。
        # 这五条比 bulk10 更严：它们吃的是 analysis 目录下的旧版 Clinical/MetaInfo，
        # 白名单外的队列连那张表都不存在，选了必然 execution_params_missing。
        _lg = _LEGACY5_RUNS.get(gid)
        if _lg:
            for st in sorted(_rec_studies(rec)):
                if st not in _lg:
                    v.append(
                        f"recommendations[{i}] {gid} × {st} 没有实跑记录：{gid} 只在 "
                        f"{'/'.join(sorted(_lg))} 上跑通过。这条流程用的是 "
                        f"/hpcdisk1/cbb_group/data/analysis/ 下的旧版 Clinical/MetaInfo，"
                        f"{st} 没有那份表。换成这几个队列之一，或改荐一条支持 {st} 的流程")
    for i, c in enumerate(plan.get("candidates") or []):
        if not isinstance(c, dict):
            v.append(f"candidates[{i}] 不是对象（应为 JSON 对象，不是字符串）")
            continue
        for stp in c.get("tool_chain") or []:
            tid = _step_tool_id(stp)
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
            # 与全 server 同一判据：`File?`/`Array[File]+` 都是 File（裸等号会漏）
            s["is_file"] = _is_file_type(d.get("type"))
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

# **`file_name` 不是主键。** 0826 图里 396 个文件名对应多条不同 `file_path`，其中 318 个
# 还跨队列。最狠的一组是 HRA003107 与 HRA007167 各 310 个同名 BAM（两个队列的 BAM 目录
# 文件名完全撞车），其次是 HRA007169 那 76 个 VCF 在 `analysis_bak/mutect2` 下各有一份备份。
# 所以「按文件名查一条」的写法是在同名兄弟里随机挑——Cypher 不带 ORDER BY 时行序无保证。
# 挑错不报错，是**静默串队列**：asset 的 study_accession 被写成另一个队列，`_complete_assets`
# 再顺着这个错队列去补临床表、MAF、索引，整条推荐跟着偏；同一个问题两次规划还可能给不同路径。
# 统一在这里定序，三个从强到弱的判据：已知队列 > 非备份目录 > 字典序兜底。
_BAK_DIR = re.compile(r"/(analysis_bak|bak|backup|old|deprecated|tmp)(/|$)", re.I)
# 同名节点全取，不截断——截断就等于把择优退化回随机挑。上限只防病态数据（实测最多 17 条）。
_NAME_FANOUT = 64

def _node_rank(props, acc=None):
    """同名多节点时的择优键，越小越优先。"""
    fp = str((props or {}).get("file_path") or "")
    same_acc = 0 if (acc and str((props or {}).get("study_accession") or "") == acc) else 1
    return (same_acc, 1 if _BAK_DIR.search(fp) else 0, fp)

def _asset_facts(names, acc=None):
    """按 file_name 批量取图内权威字段（T1/T2 通用），供 assets 补全。

    `acc` 是调用方已声明的队列，同名跨队列时用它消歧（见 `_node_rank`）。"""
    qs, keys = [], []
    for fn in dict.fromkeys(names):            # 去重但保序
        if _SAFE_FILE.fullmatch(str(fn)):
            qs.append(f"MATCH (n) WHERE (n:T1 OR n:T2) AND n.file_name = '{fn}' "
                      f"RETURN properties(n) LIMIT {_NAME_FANOUT}")
            keys.append(fn)
    if not qs:
        return {}
    rows = neo4j_q(qs)                         # 一次批量往返，别逐个查
    facts = {}
    for fn, r in zip(keys, rows):
        cands = [(x[0] or {}) for x in (r or []) if x and x[0]]
        if cands:
            facts[fn] = min(cands, key=lambda p: _node_rank(p, acc))
    return facts


# ---------- String 型样本/run 标识参数的确定性解析 ----------
# 卡片里有一批非 File 的标识参数（sample_id / pair_id / tumor_id / normal_id /
# group_a_samples …），它们不是判断性内容，是图谱里本来就记着的 accession——该由服务端
# 确定性填，而不是让模型编一个（编了也不会报错，是最危险的一种幻觉）。
#
# 取值口径按交付包实跑输入定（归档的 input.json / example_inputs.json）：
#   · WES 配对族（fastp / gatk / wes_somatic_pair / snpeff）用**样本号** HRS*
#     （fastp input.json: sample_id="HRS280607"；gatk: tumor_id="HRS280607" normal_id="HRS280608"）
#   · cellranger / cnvkit / gatk_germline / star_fusion / diff_expr 族用 **run 号** HRR*
#     （cellranger example: sample_id="HRR572934"；diff_expr_go 的 group_a_samples 实测全是 tumor run）
#   · manta 用样本名（"BDESCC0671"）
#   · 其余默认 run 号
# 数据侧注意：**T2 节点永远没有 sample_accession**（0826 图实测 0/35572），BAM/VCF 这类
# 结果文件的样本归属必须走 (T2)-[:generated_from]->(T1)-[:in_sample]->(sample)。
_ID_USES_SAMPLE = {"fastp", "gatk", "wes_somatic_pair", "snpeff"}
_ID_USES_NAME = {"manta_structural_variants"}
_ID_SINGLE = ("sample_id", "sample_name", "sample_accession")
_ID_ROLE = {"tumor_id": "tumor", "normal_id": "normal"}
_ID_STUDY = ("dataset_id", "report_id", "output_prefix")     # 队列号本身就是稳定标识
# 交付样例实测：diff_expr_go 的 group_a_samples 全是 tumor run（HRA000074），group_b 即对照组
_ID_ARRAY_GROUP = {"group_a_samples": "tumor", "group_b_samples": "normal"}
_ID_ARRAY_ALL = ("sample_ids", "input_samples", "selected_run_accessions")     # 队列级工具的整队列 run（gatk_germline / cnvkit / driver 性别分层 / wgcna run 选择，交付样例均为 run 号列表）
# 队列级 run 列表的交付上限：全量枚举（HRA001272 摊平后 899 个 tumor run）会把执行合同
# 撑到没法读，交付样例本来就是精选小组。默认每组取定序后的前 48 个（同一队列两次规划
# 给同一份）；0 = 不截断。
_RUN_LIST_CAP = int(os.environ.get("BIO_RUN_LIST_CAP", "48"))
# 服务端可确定性补的 String 参数全集：stage-2 不再把它们当「调用方欠的必填」，
# 补不出来时由执行参数阶段报 literal_required。
_ID_RESOLVABLE = (frozenset(_ID_SINGLE) | frozenset(_ID_ROLE) | frozenset(_ID_STUDY)
                  | frozenset(_ID_ARRAY_GROUP) | frozenset(_ID_ARRAY_ALL)
                  | frozenset({"pair_id", "quant_type", "assay_type", "input_scale"}))


def _file_sample_facts(names):
    """文件名 → 样本事实 {sample_accession, run_accession, sample_name, individual_accession, role}。

    三级解析，逐级兜底：
    ① T1 直接读节点属性（0821 起 sample_accession 就落在 T1 上）；
    ② T2 节点自身永远没有 sample_accession（0826 实测 0/35572），走
       generated_from→T1→in_sample→sample（用 OPTIONAL，边不全也把文件自己的
       run_accession 带回来）；
    ③ ②还拿不到样本的（图谱 in_sample 边不全），按文件自己的 run_accession
       反查 sample 节点（sample.run_accession 可能有分号多值，按 split 匹配）。
    角色用 `sample_role` 判，与 resolve_sample_roles 同一套规则。"""
    keys = [str(fn) for fn in dict.fromkeys(names) if _SAFE_FILE.fullmatch(str(fn))]
    if not keys:
        return {}
    in_list = ",".join("'" + k + "'" for k in keys)
    rows = neo4j_q([
        f"MATCH (n:T1) WHERE n.file_name IN [{in_list}] "
        "RETURN n.file_name, n.sample_accession, n.run_accession, n.sample_name, "
        "n.individual_accession, n.study_accession, n.specimen_type, NULL, n.strategy",
        f"MATCH (t2:T2) WHERE t2.file_name IN [{in_list}] "
        "OPTIONAL MATCH (t2)-[:generated_from]->(:T1)-[:in_sample]->(sp:sample) "
        "OPTIONAL MATCH (sp)-[:in_individual]->(i:individual) "
        "RETURN t2.file_name, sp.sample_accession, t2.run_accession, sp.sample_name, "
        "i.`00_individual_accession`, t2.study_accession, sp.specimen_type, sp.tissue_type, "
        "t2.strategy",
    ])
    facts = {}
    for r in (rows[0] if rows else []) or []:      # T1
        if not r or len(r) < 8 or not r[0]:
            continue                                # 数据层给不出整行（mock/异常）时静默跳过
        rec = {"sample_accession": r[1], "run_accession": r[2], "sample_name": r[3],
               "individual_accession": r[4], "study_accession": r[5], "specimen_type": r[6],
               "tissue_type": None, "strategy": r[7] if len(r) > 7 else None}
        rec["role"] = sample_role(rec)
        facts[r[0]] = rec
    for r in (rows[1] if len(rows) > 1 else []) or []:      # T2
        if not r or len(r) < 8 or not r[0] or r[0] in facts:
            continue
        rec = {"sample_accession": r[1], "run_accession": r[2], "sample_name": r[3],
               "individual_accession": r[4], "study_accession": r[5], "specimen_type": r[6],
               "tissue_type": r[7], "strategy": r[8] if len(r) > 8 else None}
        rec["role"] = sample_role(rec)
        facts[r[0]] = rec
    # ③ run→sample 反查兜底（如 HRA000021 的 BAM 没有 in_sample 边）
    orphan_runs = sorted({f["run_accession"] for f in facts.values()
                          if not f.get("sample_accession") and f.get("run_accession")})
    orphan_runs = [r for r in orphan_runs if _SAFE_FILE.fullmatch(r)]
    if orphan_runs:
        rin = ",".join("'" + r + "'" for r in orphan_runs)
        rows3 = neo4j_q([f"MATCH (sp:sample) WHERE any(x IN "
                         f"split(coalesce(sp.run_accession,''),';') WHERE x IN [{rin}]) "
                         "OPTIONAL MATCH (sp)-[:in_individual]->(i:individual) "
                         "RETURN sp.sample_accession, sp.run_accession, sp.sample_name, "
                         "i.`00_individual_accession`, sp.study_accession, sp.specimen_type, sp.tissue_type"])
        run_map = {}
        for r in (rows3[0] if rows3 else []) or []:
            if not r or len(r) < 7 or not r[0]:
                continue
            rec = {"sample_accession": r[0], "run_accession": r[1], "sample_name": r[2],
                   "individual_accession": r[3], "study_accession": r[4], "specimen_type": r[5],
                   "tissue_type": r[6]}
            rec["role"] = sample_role(rec)
            for run in str(r[1] or "").split(";"):
                if run:
                    run_map.setdefault(run, rec)
        for f in facts.values():
            if f.get("sample_accession"):
                continue
            hit = run_map.get(str(f.get("run_accession") or ""))
            if hit:
                for k, v in hit.items():
                    if v is not None and k != "run_accession":
                        f[k] = v
                f["role"] = sample_role(f)
    return facts


def _study_run_lists(study, strategy=None):
    """队列级样本枚举：{"tumor": [run…], "normal": [run…], "all": [run…]}（定序，同一问题两次
    规划给同一份）。

    **必须按测序策略过滤**：sample 的 run_accession 字符串把 WES/RNA 混在一格里
    （HRA001272 实踩：分组数组被 WES run 污染，矩阵列名全对不上），而 strategy 只挂在
    T1 节点上。所以这里不读 sample.run_accession，改走 sample←in_sample←T1 边，
    按 `strategy`（如 bulk_RNA / WES）逐样本取该策略下的 run。"""
    if not study or not _SAFE_TOKEN.fullmatch(str(study)):
        return None
    if strategy and not _SAFE_TOKEN.fullmatch(str(strategy)):
        strategy = None
    q = (f"MATCH (sp:sample) WHERE sp.study_accession = '{study}' "
         f"OPTIONAL MATCH (t:T1)-[:in_sample]->(sp)" +
         (f" WHERE t.strategy = '{strategy}'" if strategy else "") +
         " RETURN sp.sample_name, sp.tissue_type, sp.specimen_type, "
         "collect(DISTINCT t.run_accession)")
    rows = neo4j_q([q])
    out = {"tumor": [], "normal": [], "all": []}
    for r in (rows[0] if rows else []) or []:
        if not r or len(r) < 4:
            continue                                  # 数据层给不出整行（mock/异常）时静默跳过
        role = sample_role({"study_accession": study, "sample_name": r[0],
                            "tissue_type": r[1], "specimen_type": r[2]})
        for run in (r[3] or []):
            if not run:
                continue
            out["all"].append(str(run))
            if role in ("tumor", "normal"):
                out[role].append(str(run))
    for k in out:
        out[k] = sorted(set(out[k]))
    return out


def _paired_bam_fill(study, cap=0):
    """队列级配对角色数组槽（tumor_bams/tumor_bais/normal_bams/normal_bais）的服务端补全。

    cnvkit 这类队列级配对流程的四个数组槽是**队列语义**（要放几十上百对 BAM），调用方
    按手册「只给主数据资产」给出一两个代表文件后，肿瘤/正常必有一边凑不齐——
    0817 起实测 cnvkit 的 tumor_bams/tumor_bais 恒 no_confirmed_path。角色划分与同个体
    配对图里都有，服务端补全是确定性活，不该让调用方枚举几百个文件。

    配对规则：同一 individual 下角色为 tumor 与 normal 的样本各取一个**有 BQSR BAM+BAI
    交付文件**的 run。BQSR BAM 只产自 DNA 流程，这一约束顺带消掉了 WES/WGS 策略选择
    问题（RNA 比对产物的 semantic_format 不同，天然进不来）；没有交付文件的 run 跳过。

    返回 {"tumor_bams"/"tumor_bais"/"normal_bams"/"normal_bais": [路径…]（四数组按对平行，
    tumor_bams[i] 与 normal_bams[i] 同个体）, "sample_ids": [各对肿瘤 run…],
    "pair_runs": [(t_run, n_run)…]}；配不出对返回 None。

    cap>0 才截断，默认全量。截断必须发生在「按调用方已绑 run 过滤」之后（统一在
    _cohort_fill_entries 做）——先截断会把已绑 run 的配对截出界，过滤就找不到它
    （HRR573240 排在 HRA001749 第 48 名之后实踩）。"""
    if not study or not _SAFE_TOKEN.fullmatch(str(study)):
        return None
    rows = neo4j_q([f"MATCH (t2:T2) WHERE t2.study_accession = '{study}' "
                    "AND t2.semantic_format IN ['DNA_ALIGNMENT_BQSR_BAM','DNA_ALIGNMENT_INDEX_BAI'] "
                    "AND t2.file_path IS NOT NULL "
                    "RETURN t2.run_accession, t2.semantic_format, t2.file_path"])
    files = {}
    for r in (rows[0] if rows else []) or []:
        if not r or not r[0] or not r[2]:
            continue
        files.setdefault(str(r[0]), {})[r[1]] = str(r[2])
    good = {run for run, d in files.items()
            if "DNA_ALIGNMENT_BQSR_BAM" in d and "DNA_ALIGNMENT_INDEX_BAI" in d}
    if not good:
        return None
    rows = neo4j_q([f"MATCH (sp:sample)-[:in_individual]->(i:individual) "
                    f"WHERE sp.study_accession = '{study}' "
                    "OPTIONAL MATCH (t:T1)-[:in_sample]->(sp) "
                    "RETURN i.`00_individual_accession`, sp.sample_name, sp.tissue_type, "
                    "sp.specimen_type, collect(DISTINCT t.run_accession)"])
    per_ind = {}
    for r in (rows[0] if rows else []) or []:
        if not r or len(r) < 5 or not r[0]:
            continue
        role = sample_role({"study_accession": study, "sample_name": r[1],
                            "tissue_type": r[2], "specimen_type": r[3]})
        if role not in ("tumor", "normal"):
            continue
        runs = sorted({str(x) for x in (r[4] or []) if x and str(x) in good})
        if runs:
            per_ind.setdefault(str(r[0]), {}).setdefault(role, set()).update(runs)
    pairs = []
    for ind in sorted(per_ind):
        d = per_ind[ind]
        if d.get("tumor") and d.get("normal"):
            pairs.append((sorted(d["tumor"])[0], sorted(d["normal"])[0]))
    if not pairs:
        # 同个体配不出对（HRA000071：血液对照与肿瘤不属同一个体）——退回角色组间对齐：
        # 肿瘤组/正常组各自定序后按位成对。cnvkit 队列批处理只要求两侧等长
        # （ValidateCnvInputs 的口径），白名单里 HRA000071 的实跑就是组间形态。
        rl = _study_run_lists(study) or {}
        ts = [r for r in (rl.get("tumor") or []) if r in good]
        ns = [r for r in (rl.get("normal") or []) if r in good]
        pairs = list(zip(ts[:min(len(ts), len(ns))], ns[:min(len(ts), len(ns))]))
    if not pairs:
        return None
    if cap and cap > 0:
        pairs = pairs[:cap]
    return {"tumor_bams": [files[t]["DNA_ALIGNMENT_BQSR_BAM"] for t, _ in pairs],
            "tumor_bais": [files[t]["DNA_ALIGNMENT_INDEX_BAI"] for t, _ in pairs],
            "normal_bams": [files[n]["DNA_ALIGNMENT_BQSR_BAM"] for _, n in pairs],
            "normal_bais": [files[n]["DNA_ALIGNMENT_INDEX_BAI"] for _, n in pairs],
            "sample_ids": [t for t, _ in pairs],
            "pair_runs": pairs}


def _role_array_slot(name, typ, fmt):
    """是「配对角色数组槽」吗（tumor_bams/normal_bais 这种，cnvkit 契约的四个）。"""
    n = str(name or "").lower()
    return (_is_array_type(typ) and re.fullmatch(r"(tumor|normal)_(bams|bais)", n)
            and any(k in str(fmt or "").upper() for k in ("BAM", "BAI")))


def _cohort_bam_fill(study, role=None, cap=0):
    """非配对的队列级 BAM 数组补全（gatk_germline_cohort / tumor_evolution_inference 用）。

    返回 {"bams": [...], "bais": [...], "runs": [...]}（按下标平行），配不出返回 None。
    role=None 取全队列有 BQSR BAM+BAI 的 run（胚系联合分型不分肿瘤正常）；
    role="tumor" 只取肿瘤样本的 run（肿瘤演化这类流程不吃正常样本）。"""
    if not study or not _SAFE_TOKEN.fullmatch(str(study)):
        return None
    rows = neo4j_q([f"MATCH (t2:T2) WHERE t2.study_accession = '{study}' "
                    "AND t2.semantic_format IN ['DNA_ALIGNMENT_BQSR_BAM','DNA_ALIGNMENT_INDEX_BAI'] "
                    "AND t2.file_path IS NOT NULL "
                    "RETURN t2.run_accession, t2.semantic_format, t2.file_path"])
    files = {}
    for r in (rows[0] if rows else []) or []:
        if not r or not r[0] or not r[2]:
            continue
        files.setdefault(str(r[0]), {})[r[1]] = str(r[2])
    runs = sorted(run for run, d in files.items()
                  if "DNA_ALIGNMENT_BQSR_BAM" in d and "DNA_ALIGNMENT_INDEX_BAI" in d)
    if not runs:
        return None
    if role in ("tumor", "normal"):
        rl = _study_run_lists(study) or {}
        keep = set(rl.get(role) or [])
        runs = [r for r in runs if r in keep]
    if not runs:
        return None
    if cap and cap > 0:
        runs = runs[:cap]                     # 同 _paired_bam_fill：截断在过滤之后（入口统一做）
    return {"bams": [files[r]["DNA_ALIGNMENT_BQSR_BAM"] for r in runs],
            "bais": [files[r]["DNA_ALIGNMENT_INDEX_BAI"] for r in runs],
            "runs": runs}


def _tei_fill(study, cap=0):
    """tumor_evolution_inference 的图内可补槽位（2026-09 运行测试表口径）：
    三证齐全（BQSR BAM+BAI 且 SomaticSNV-VCF 在图）的肿瘤 run 出 tumor_bams /
    tumor_bam_indexes / somatic_small_variant_vcfs 三个平行数组；allele_specific_cnv_files
    补队列级 SOMATIC_CNV_TSV（全图唯一一份才补）。
    sample_manifest 图内没有——它是 MakeManifest 辅助流程的运行产物（如
    MakeHra001272TestManifest），不在这里补，由调用方如实报缺。

    返回 {"tumor_bams": [...], "tumor_bam_indexes": [...], "somatic_small_variant_vcfs": [...],
          "allele_specific_cnv_files": [...], "runs": [...]}；关键件缺了返回 None。"""
    base = _cohort_bam_fill(study, role="tumor", cap=cap)
    if not base:
        return None
    rows = neo4j_q([f"MATCH (t2:T2) WHERE t2.study_accession = '{study}' "
                    "AND t2.file_path CONTAINS '/SomaticSNV-VCF' "
                    "AND (t2.file_name ENDS WITH '.vcf' OR t2.file_name ENDS WITH '.vcf.gz') "
                    "AND t2.file_path IS NOT NULL "
                    "RETURN t2.run_accession, t2.file_path",
                    f"MATCH (n) WHERE (n:T1 OR n:T2) AND n.study_accession = '{study}' "
                    "AND n.semantic_format = 'SOMATIC_CNV_TSV' AND n.file_path IS NOT NULL "
                    "RETURN DISTINCT n.file_path"])
    vcfs = {}
    for r in (rows[0] if rows else []) or []:
        if not r or not r[1]:
            continue
        run = str(r[0] or "") or next((m.group(0) for m in
                                       [re.search(r"(HRR\d+|HRS\d+)", str(r[1]))] if m), "")
        if run:
            vcfs.setdefault(run, str(r[1]))
    keep = [r for r in base["runs"] if r in vcfs]
    if not keep:
        return None
    idx = [base["runs"].index(r) for r in keep]
    cnv = [str(r[0]) for r in (rows[1] if len(rows) > 1 else []) or [] if r and r[0]]
    return {"runs": keep,
            "tumor_bams": [base["bams"][i] for i in idx],
            "tumor_bam_indexes": [base["bais"][i] for i in idx],
            "somatic_small_variant_vcfs": [vcfs[r] for r in keep],
            "allele_specific_cnv_files": cnv[:1]}


# 队列级数组槽的三族补全口径（槽位名 → fill 返回里的键）：
# 配对角色槽走 _paired_bam_fill（同个体）；这两族走 _cohort_bam_fill / _tei_fill。
_GERMLINE_FILL = {"analysis_ready_bams": "bams", "analysis_ready_bais": "bais"}
_TEI_FILL_SLOTS = {"tumor_bams": "tumor_bams", "tumor_bam_indexes": "tumor_bam_indexes",
                   "somatic_small_variant_vcfs": "somatic_small_variant_vcfs",
                   "allele_specific_cnv_files": "allele_specific_cnv_files"}
# 槽位表是按工具分的——直接拿 gid 查平铺表永远查不中（实踩）
_GID_FILL = {"gatk_germline_cohort": _GERMLINE_FILL,
             "tumor_evolution_inference": _TEI_FILL_SLOTS}


def _cohort_fillable_slot(gid, name, typ, fmt):
    """该槽位是不是「队列语义、可由服务端补全」的数组槽（三族任一）。"""
    if _role_array_slot(name, typ, fmt):
        return True
    if not _is_array_type(typ):
        return False
    return str(name or "").lower() in (_GID_FILL.get(gid) or {})


def _cohort_fill_entries(gid, study, bound_runs, cap=0):
    """{槽位名: [(frozenset(run…), 绝对路径)…]}：队列级数组槽的确定性补全。

    `bound_runs` 是调用方已绑的同族槽位里的 run 号：非空时按它过滤补全集，
    保证两侧数组按对/按 run 平行（教训：48 个 sample_ids 配 1 个 BAM 必炸
    ValidateCnvInputs）。entry 的 run 集为空（队列级文件）时不受过滤影响。"""
    out = {}
    if not study or not _SAFE_TOKEN.fullmatch(str(study)):
        return out
    card = KC_MAP.get(gid) or {}

    def _keep(es):
        # 先按已绑 run 过滤（平行性），再截断（顺序不能反，见 _paired_bam_fill docstring）
        kept = [e for e in es if not bound_runs or not e[0] or (e[0] & bound_runs)]
        return kept[:_RUN_LIST_CAP] if _RUN_LIST_CAP > 0 else kept

    if any(_role_array_slot(i.get("name"), i.get("type"), i.get("format"))
           for i in card.get("inputs") or []):
        pf = _paired_bam_fill(study, cap=cap)
        if pf:
            for slot in ("tumor_bams", "tumor_bais", "normal_bams", "normal_bais"):
                out[slot] = _keep([(frozenset(pr), p)
                                   for pr, p in zip(pf["pair_runs"], pf[slot])])
    if gid == "gatk_germline_cohort":
        cf = _cohort_bam_fill(study, cap=cap)
        if cf:
            for slot, key in _GERMLINE_FILL.items():
                out[slot] = _keep([(frozenset({r}), p)
                                   for r, p in zip(cf["runs"], cf[key])])
    if gid == "tumor_evolution_inference":
        tf = _tei_fill(study, cap=cap)
        if tf:
            for slot, key in _TEI_FILL_SLOTS.items():
                vals = tf.get(key)
                if not vals:
                    continue
                if slot == "allele_specific_cnv_files":
                    out[slot] = [(frozenset(), p) for p in vals]
                else:
                    out[slot] = _keep([(frozenset({r}), p)
                                       for r, p in zip(tf["runs"], vals)])
    return out


def _paired_scalar_fill(study):
    """标量角色槽（tumor_bam/normal_bam 这种单对 File，manta/gatk 的契约）的配对补全：
    返回 {(槽位名): (run, 路径)} 的第一对，以及按 run 索引全对的映射。
    manta 的 normal_bam/normal_bai 卡片标可选（肿瘤单样本模式能跑），但图里有配对就补上——
    配对分析才是完整答案。配不出返回 ({}, {})。"""
    pf = _paired_bam_fill(study) if study else None
    if not pf:
        return {}, {}
    first = {}
    by_run = {}
    for j, (t, n) in enumerate(pf["pair_runs"]):
        m = {"tumor_bam": pf["tumor_bams"][j], "tumor_bai": pf["tumor_bais"][j],
             "normal_bam": pf["normal_bams"][j], "normal_bai": pf["normal_bais"][j]}
        if j == 0:
            first = m
        by_run[t] = m
        by_run[n] = m
    return first, by_run


def _study_gender_lists(study, strategy=None):
    """队列级性别分组枚举：{"female": [run…], "male": [run…]}（定序）。
    与 _study_run_lists 同走 sample←in_sample←T1 边、同按 strategy 过滤；性别读
    sample.gender（取值 Female/Male/female/male/missing，大小写归一，missing 丢弃）。
    用途：diff_expr 族在单臂队列（全 Tumor，角色分不出对照）的分组兜底——
    实跑记录就是按性别分（HRA000073：group_a=female 122 / group_b=male）。"""
    if not study or not _SAFE_TOKEN.fullmatch(str(study)):
        return None
    if strategy and not _SAFE_TOKEN.fullmatch(str(strategy)):
        strategy = None
    q = (f"MATCH (sp:sample) WHERE sp.study_accession = '{study}' "
         f"OPTIONAL MATCH (t:T1)-[:in_sample]->(sp)" +
         (f" WHERE t.strategy = '{strategy}'" if strategy else "") +
         " RETURN sp.gender, collect(DISTINCT t.run_accession)")
    rows = neo4j_q([q])
    out = {"female": [], "male": []}
    for r in (rows[0] if rows else []) or []:
        if not r or len(r) < 2:
            continue
        g = str(r[0] or "").strip().lower()
        if g not in ("female", "male"):
            continue
        for run in (r[1] or []):
            if run:
                out[g].append(str(run))
    return {k: sorted(set(v)) for k, v in out.items()}


def _patient_key(t_name, n_name):
    """配对的患者键：剥角色前缀（T_/B_/N_/P_）后，两侧一致就用全名（T_CGGA_1251/B_CGGA_1251
    → CGGA_1251），否则取最长公共前缀截到完整 token（M019_RT1…/M019_LN1… → M019）。
    对不上返回 None，由调用方回退。"""
    a = re.sub(r"^[TBNP]_", "", str(t_name or "").strip())
    b = re.sub(r"^[TBNP]_", "", str(n_name or "").strip())
    if a and a == b:
        return a
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    if i >= 3:
        return a[:i].rstrip("_-. ") or None
    return None


def _resolve_id_params(gid, card, bound_files, study, chain_facts=None):
    """String 型标识参数的确定性取值。`bound_files` 是本步已绑定的 {槽位名: [文件名…]}；
    `chain_facts` 是整条链的样本事实（`_file_sample_facts` 的输出），用于链里中游步骤：
    它们的文件输入是上游产物、自己没有图内文件可查，同一条数据在链里流动，标识沿链共享。

    返回 ({param: value}, [missing_param…])。**推不出来的一律进 missing，不给猜的值**——
    但以下都是图内/交付包里有唯一答案的：单样本 id 从本步绑定文件取；tumor_id/normal_id
    从角色对应槽位的文件取（槽位名对不上时按样本角色事实兜底）；pair_id 取配对双方样本名
    的共享患者键（T_CGGA_1251/B_CGGA_1251 → CGGA_1251；M019_RT1…/M019_LN1… → M019），退回
    共享的 individual 号；数组型按 `sample_role` 角色从队列遍历产 run 列表。"""
    out, missing = {}, []
    inputs = (card or {}).get("inputs") or []
    if not inputs:
        return out, missing
    names = [fn for fns in bound_files.values() for fn in fns]
    facts = _file_sample_facts(names) if names else {}
    if chain_facts:
        if names and all(fn in chain_facts for fn in names):
            facts = chain_facts                     # 链级已覆盖本步全部文件，省一次查图
        elif not facts:
            facts = dict(chain_facts)               # 本步无图内文件（中游步骤）→ 沿链共享
    # 缺省报告的证据门：绑定文件在图里查得到、或队列已定，才说明这个 id 「本应推得出」，
    # 报 literal_required 才有处置意义。规划早期/图外路径（文件根本不在图里）时静默，
    # 别给已经够长的缺项清单添噪声。
    evidence = bool(facts) or bool(study)
    flavor = ("sample" if gid in _ID_USES_SAMPLE else
              "name" if gid in _ID_USES_NAME else "run")

    def pick(f, pname=""):
        if not f:
            return None
        # 参数名直接点名的按名字给：sample_name 要名字、sample_accession 要样本号
        if pname == "sample_name":
            return f.get("sample_name") or f.get("sample_accession") or f.get("run_accession")
        if pname == "sample_accession":
            return f.get("sample_accession") or f.get("run_accession")
        if flavor == "sample":
            return f.get("sample_accession") or f.get("run_accession")
        if flavor == "name":
            return f.get("sample_name") or f.get("sample_accession") or f.get("run_accession")
        return f.get("run_accession") or f.get("sample_accession")

    def slot_value(prefixes, pname=""):
        for pn, fns in bound_files.items():
            if any(str(pn).lower().startswith(p) for p in prefixes):
                for fn in fns:
                    v = pick(facts.get(fn), pname)
                    if v:
                        return v
        return None

    run_lists = None                # 惰性：只有数组参数才做队列级枚举
    gender_lists = None             # 惰性：单臂队列的分组兜底（性别分组）才查
    for i in inputs:
        name, typ = str(i.get("name") or ""), str(i.get("type") or "")
        if _is_file_type(typ) or _is_reference_resource(card, name):
            continue
        v = None
        if name in _ID_SINGLE:
            v = slot_value(("",), name)              # 本步任一绑定文件（R1/R2 同一样本）
            if v is None and not names and facts:
                # 链里中游步骤：本步没有自己的绑定文件，沿链共享上游样本
                v = pick(next(iter(facts.values()), None), name)
        elif name in _ID_ROLE:
            v = slot_value((name.split("_")[0],), name)   # tumor_id ← tumor_* 槽位的文件
            if v is None:
                # 槽位名对不上时按样本角色事实兜底（链场景：配对文件绑在上游步骤名下）
                v = next((pick(f, name) for f in facts.values() if f.get("role") == _ID_ROLE[name]),
                         None)
        elif name == "pair_id":
            t_f = next((facts.get(fn) for pn, fns in bound_files.items()
                        if str(pn).lower().startswith("tumor") for fn in fns
                        if facts.get(fn)), None)
            n_f = next((facts.get(fn) for pn, fns in bound_files.items()
                        if str(pn).lower().startswith("normal") for fn in fns
                        if facts.get(fn)), None)
            if not (t_f and n_f):
                # 槽位名对不上（链场景）时按角色事实兜底
                t_f = t_f or next((f for f in facts.values() if f.get("role") == "tumor"), None)
                n_f = n_f or next((f for f in facts.values() if f.get("role") == "normal"), None)
            if t_f and n_f:
                v = _patient_key(t_f.get("sample_name"), n_f.get("sample_name"))
                if v is None and (t_f.get("individual_accession") and
                                  t_f.get("individual_accession") == n_f.get("individual_accession")):
                    v = t_f["individual_accession"]
            if v is None and study:
                v = f"{study}_pair"      # 队列级占位（沿用既有行为），总好过空着
        elif name in _ID_STUDY:
            v = study
        elif name == "quant_type":
            v = next((m.group(1) for fn in names
                      for m in [_FLAVOR_PAT.search(str(fn))] if m), None)
        elif name == "assay_type":
            # cnvkit 的 assay_type（wgs/wes）：取绑定文件的测序策略（图内事实），小写化
            v = next((str(f.get("strategy")).lower() for fn in names
                      for f in [facts.get(fn)]
                      if f and str(f.get("strategy") or "").lower() in ("wes", "wgs")), None)
        elif name == "input_scale":
            # gsea 的 input_scale（counts/tpm/fpkm）：与 quant_type 同口径，从文件名推
            v = next((m.group(1) for fn in names
                      for m in [_FLAVOR_PAT.search(str(fn))] if m), None)
        elif name in _ID_ARRAY_GROUP or name in _ID_ARRAY_ALL:
            # 配对/队列数组流程（卡片带 tumor_bams 槽的 cnvkit、带 analysis_ready_bams 的
            # gatk_germline_cohort）：sample_ids 必须与已绑的 BAM 数组按位平行（交付样例口径：
            # sample_ids[i] 就是数组第 i 项的 run），不能取整队列——长度对不上执行端就拒
            # （ValidateCnvInputs 实踩）。
            _tb = next((fns for pn, fns in bound_files.items()
                        if str(pn).lower() in ("tumor_bams", "analysis_ready_bams") and fns), None)
            if name in _ID_ARRAY_ALL and _tb:
                v = [r for r in ((facts.get(fn) or {}).get("run_accession") for fn in _tb)
                     if r]
                if len(v) != len(_tb):
                    v = None          # 有文件查不到 run：退回队列枚举，别给残缺的平行数组
            if v is None and study:
                if run_lists is None:
                    # 按绑定输入的测序策略过滤 run（表达矩阵→bulk_RNA；BAM/MAF→WES），
                    # 防 WES run 混进 RNA 矩阵的分组（HRA001272 实踩过的坑）
                    strat = next((f.get("strategy") for fn in names
                                  for f in [facts.get(fn)] if f and f.get("strategy")), None)
                    run_lists = _study_run_lists(study, strategy=strat)
                if run_lists:
                    if name in _ID_ARRAY_ALL:
                        v = run_lists["all"] or None
                    else:
                        grp = run_lists[_ID_ARRAY_GROUP[name]]
                        v = grp or None
                    if v and _RUN_LIST_CAP > 0:
                        v = v[:_RUN_LIST_CAP]       # 交付上限：每组取定序后的前 N 个
                    if v is None and name in _ID_ARRAY_GROUP:
                        # 单臂队列（全 Tumor）角色分不出对照组：按性别分组兜底——
                        # 实跑记录就是这么分的（HRA000073 diff_expr：group_a=female/
                        # group_b=male）。调用方显式给了的仍以调用方为准（外层跳过逻辑）。
                        if gender_lists is None:
                            gender_lists = _study_gender_lists(study, strategy=strat)
                        if gender_lists:
                            v = gender_lists[{"group_a_samples": "female",
                                              "group_b_samples": "male"}[name]] or None
                            if v and _RUN_LIST_CAP > 0:
                                v = v[:_RUN_LIST_CAP]
        if v is not None and v != []:
            out[name] = v
        elif i.get("required", True) and name in _ID_RESOLVABLE and evidence:
            missing.append(name)
    return out, missing


def _bound_file_names(bindings):
    """从调用方绑定里收集 {槽位名: [文件名…]}（dict/list 两种形态都吃）。"""
    out = {}
    for pname, b in (bindings or {}).items():
        items = b if isinstance(b, list) else [b]
        fns = []
        for it in items:
            if not isinstance(it, dict):
                continue
            fn = str(it.get("file_name") or it.get("file_id") or "").strip()
            if fn and _SAFE_FILE.fullmatch(fn):
                fns.append(fn)
        if fns:
            out[str(pname)] = fns
    return out


def _name_paths(names):
    """按 file_name 批量取图内**全部**同名路径：{file_name: {path, …}}。

    接地校验用它做「属于」判断而不是「等于」某一条：同名多路径时按一条比对，
    调用方给的另一条真实路径会被误判成"模型编造"。"""
    qs, keys = [], []
    for fn in dict.fromkeys(names):
        if _SAFE_FILE.fullmatch(str(fn)):
            qs.append(f"MATCH (n) WHERE (n:T1 OR n:T2) AND n.file_name = '{fn}' "
                      f"RETURN n.file_path LIMIT {_NAME_FANOUT}")
            keys.append(fn)
    if not qs:
        return {}
    rows = neo4j_q(qs)
    return {fn: {str(x[0]) for x in (r or []) if x and x[0]}
            for fn, r in zip(keys, rows) if r}

# 定量口径：同一队列的表达矩阵在图内有 FPKM/TPM/counts 三份，节点属性完全一致
# （semantic_format 都是 TABULAR_BIO_DATA、data_level 都是 2），只有文件名能区分。
# 该选哪一份由流程自己说了算——闭集描述里点名的口径就是它的默认口径。
_FLAVOR_PAT = re.compile(r"(?<![A-Za-z])(logCPM|FPKM|TPM|counts?)(?![A-Za-z])", re.I)
_FLAVOR_CANON = {"logcpm": "logCPM", "fpkm": "FPKM", "tpm": "TPM",
                 "count": "counts", "counts": "counts"}
_MATRIX_NAME = re.compile(r"^(.*-Genes-)([A-Za-z]+)(-.*\.tsv)$", re.I)

# 描述里没点名口径的流程，按方法本身要求的输入定：不定就等于让调用方随口挑一份，
# 同一个问题两次规划给出不同文件。
#   · WGCNA 族按官方推荐从原始 counts（VST）起步，不吃 TPM/FPKM；
#   · bulk10 族（见 _BULK10）**一律 counts**——不是方法学推导，是 2026-08-24 交付的
#     10 条流程 × 7 个队列全部 Succeeded 的实跑记录，每一条的 expr 都是
#     `{STUDY}-Genes-counts-1.0.tsv`。这条早先按「跨样本比表达高低要归一」推成了 TPM，
#     于是 km_survival/cox_model/gene_boxplot/umap/stage_heatmap 五个流程被服务端把
#     调用方选对的 counts **主动换成没跑通过的 TPM**——归一在流程内部自己做。
#   · gsea_pathway_enrichment 不属于 bulk10，预排序确实要 TPM，保留。
_FLAVOR_FALLBACK = {
    "wgcna": "counts", "gsea_pathway_enrichment": "TPM",
}

# bulk10：2026-08-24 交付的十条 CNCB 原生元数据流程。共同契约见 SKILL.md §3.1。
# tool_id 不带 task 前缀，docker 镜像带（task310_cox_model:v1）——两边都别写错。
_BULK10 = {"de_enrichment", "deg_enrichment", "deg_trend", "gene_boxplot", "stage_heatmap",
           "umap", "wgcna_module_trait", "wgcna_hub", "cox_model", "km_survival"}
_FLAVOR_FALLBACK.update({t: "counts" for t in _BULK10})

# 已验证组合是**逐流程**的，不是十条共用一份队列白名单。真值表在
# skill/references/bulk10_proven_runs.tsv（26 条 Succeeded 的 Cromwell 记录，一行一次实跑）。
# union 恰好是 7 个队列，但按 union 放行就会批准 de_enrichment×HRA007167 这类从没跑过的组合
# ——那条流程只在 HRA003107 上跑过。HRA000122 更极端：十条里只有 umap 碰过它。
# 图内另有 HRA001272 / HRA007413 两份 Genes-counts，任何一条 bulk10 都没在上面跑过
# （HRA001272 的 counts 多一层 `/RNAseq/` 目录，HRA007413 只有 1.2MB），一律不放行。
_BULK10_RUNS: dict = {}

def load_bulk10_runs() -> None:
    """加载 bulk10 已验证的 (tool_id, study) 组合。文件缺失则留空 = 不放行任何组合。"""
    path = os.path.join(SKILL_REF, "bulk10_proven_runs.tsv")
    if not os.path.exists(path):
        return
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                t, s = (row.get("tool_id") or "").strip(), (row.get("study") or "").strip()
                if t and s:
                    _BULK10_RUNS.setdefault(t, set()).add(s)
    except Exception:
        _BULK10_RUNS.clear()

load_bulk10_runs()

# sample.csv / individual.csv 是 CNCB 原生元数据，路径由队列号唯一确定，且**不在图内**
# （图里只有 HRA000001 一份）。所以既不能让调用方去图里查（查不到），也不能让它写进
# assets（validate_plan 的接地校验会按 file_name 撞上 HRA000001 那份、报 file_path 不符）。
# 与临床表/样本元信息表同一处置：服务端从队列号直接推，调用方不写也不查。
_BULK10_META = "/cbb-data/gsa/agent/{study}/{name}.csv"

# 四条流程带 case/control 分组；实跑记录里两个值都是字面量 case / control。
_BULK10_LABELS = {"de_enrichment", "deg_enrichment", "deg_trend", "gene_boxplot"}

# cox_model 在这四个队列上要显式指定生存状态列；HRA003107 用默认值。
_BULK10_STATUS_COL = {"cox_model": ({"HRA000073", "HRA000074", "HRA002693", "HRA006117"},
                                    "13_vital_status")}

_STUDY_IN_PATH = re.compile(r"/(HRA\d+)[/-]")

def _bulk10_params(gid, expr_path, bindings, errors):
    """bulk10 流程的确定性补全：两张 CNCB 原生元数据表 + 分组标签 + 生存状态列。

    队列号从已解析的 expr 路径里取——不另开一次图查询，也不信调用方另给的队列号
    （给错了就会把 A 队列的表达矩阵配上 B 队列的样本表，样本 ID 对不上，
    执行端只会得到 0 个可用样本而不是报错）。
    """
    if not expr_path:
        return {}
    m = _STUDY_IN_PATH.search(expr_path)
    if not m:
        errors.append(f"{gid}: 无法从 expr 路径解析队列号，bulk10 流程必须能定位到 HRA 队列")
        return {}
    study = m.group(1)
    ok = _BULK10_RUNS.get(gid) or set()
    if study not in ok:
        errors.append(
            f"{gid}: 没有 {gid} × {study} 的实跑记录。{gid} 只在 "
            f"{'/'.join(sorted(ok)) if ok else '（无）'} 上跑通过，选数据只能从这里面选")
        return {}
    out = {n + "_csv": _BULK10_META.format(study=study, name=n)
           for n in ("sample", "individual")}
    if gid in _BULK10_LABELS:
        out["case_label"] = str(bindings.get("case_label") or "case")
        out["control_label"] = str(bindings.get("control_label") or "control")
    studies, col = _BULK10_STATUS_COL.get(gid, (set(), ""))
    if study in studies:
        out["native_status_source_col"] = col
    return out

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

# 六条非 bulk10 流程把这一对表写成必填输入，交付卡的参数名有两套写法（`_xls/_xlsx`
# 与 `_file`）。手册说「临床表/样本元信息表一律不查，服务端按队列补」，但服务端此前
# 只补 bulk10 的 sample_csv/individual_csv，这一对谁都没补——于是
# driver_gene_gender_analysis / wgcna / her2_pfs_survival / immune_infiltration_iobr /
# survival_analysis / tmb_survival_analysis 六条**必然**留下两条
# `no_confirmed_path`，selection_status 永远退到 needs_input。补这里。
_CLINICAL_PARAM_FMT = {"clinical_xls": "CLINICAL_DATA_EXCEL", "clinical_file": "CLINICAL_DATA_EXCEL",
                       "metainfo_xlsx": "METADATA_SAMPLE_INFO", "metainfo_file": "METADATA_SAMPLE_INFO"}

# 260902 交付把上面五条流程的临床输入从「两张 XLSX」换成了「三张 CSV」（individual/sample/T1）。
# 这三张表同样是**服务端按队列补**、调用方一律不查（手册 §8 的口径没变，变的只是槽位名），
# 所以走同一套补全：图内每个 HRA 队列各一份，`semantic_format` 是 `INDIVIDUAL_META` /
# `SAMPLE_META` / `T1_META`，路径 `/cbb-data/gsa/agent/<ACC>/<name>.csv`。
# 不接这一族的话这五条流程会各留三条 `no_confirmed_path`——换格式前是两条（临床对），
# 换完变三条，因为槽位名从 `_CLINICAL_PARAM_FMT` 里查不到，`_needs_clinical` 直接返回空。
_CSV_META_PARAM_FMT = {"individual_csv": "INDIVIDUAL_META",
                       "sample_csv": "SAMPLE_META",
                       "t1_csv": "T1_META",
                       # gatk_germline_cohort 的交付口径（运行测试表）：同一套 CNCB 原生 CSV，
                       # 只是槽位名不同——sample_metadata=sample.csv、clinical_metadata=individual.csv
                       "sample_metadata": "SAMPLE_META",
                       "clinical_metadata": "INDIVIDUAL_META"}

def _needs_clinical(gid):
    """该流程按卡片声明需要哪几个「由服务端按队列补」的元数据参数：{参数名: 语义格式}。

    含两族，各有各的理由不该由调用方查：
      · `clinical_xls`/`metainfo_xlsx` 那对 XLSX（`_CLINICAL_PARAM_FMT`）
      · `individual_csv`/`sample_csv`/`t1_csv` 那组 CSV（`_CSV_META_PARAM_FMT`，260902 起）

    bulk10 不在这里补：它走 `_bulk10_params` 的 CNCB 原生 CSV 那条路（按路径模板推、不看图），
    它自己的 clinical_xls 是可选的——两条路混着补会把 CSV 和 XLSX 两套表同时塞进同一次提交。"""
    if gid in _BULK10:
        return {}
    card = KC_MAP.get(gid) or {}
    want = dict(_CLINICAL_PARAM_FMT)
    want.update(_CSV_META_PARAM_FMT)
    return {i["name"]: want[i["name"]] for i in card.get("inputs") or []
            if i.get("name") in want}

# 五个旧版工具：它们吃的临床表/样本元信息表是 `/hpcdisk1/cbb_group/data/analysis/<ACC>/`
# 下的**旧版**表，而 0826 图内 18 份 Clinical/MetaInfo 全在扁平 `/hpcdisk1/cbb_group/data/<ACC>/`
# 下、一律 .xlsx。两套表结构不同，喂新版进去跑不动——旧版这一族图里一个节点都没有，
# 只能按实跑记录覆盖（同 sample.csv/individual.csv 的处置）。真值表在
# skill/references/legacy5_proven_runs.tsv（22 条 Succeeded 的 Cromwell 记录）。
# Clinical 扩展名逐队列不同（HRA000873/HRA000071 是 .xlsx，其余 .xls），不能按队列号硬拼。
# MAF 与表达矩阵两栏与图内路径完全一致（含 HRA001272 多一层 `/RNAseq/`），不在覆盖范围。
_LEGACY5_DIR = "/hpcdisk1/cbb_group/data/analysis/{acc}/{name}"
_LEGACY5_RUNS: dict = {}     # tool_id -> {study: (clinical_name, metainfo_name)}
# 旧版表文件名 → analysis 目录下的绝对路径。接地校验与路径回填都拿它当豁免凭据：
# **只认「这个文件名配这条路径」这一对**，写别的路径照样报错。
# 不能只按文件名放行：HRA000873/HRA000071 那两份 Clinical 是 .xlsx，图内扁平目录下
# 同名也有一份，按名放行就等于对这两个队列彻底关掉了路径校验。
_LEGACY5_PATHS: dict = {}
# 队列级回退：{study: (clinical_name, metainfo_name)}，**只在该队列所有实跑行都写同一个名字时
# 才有值**。表里 wgcna×HRA007167/HRA003107/HRA001272 三行的临床列是空的（那三次实跑只交了
# 表达矩阵），但同一队列另有别的流程跑过、文件名是确定的——文件名是队列的属性，不是流程的。
# 反过来 HRA000873/HRA000071 的 Clinical 逐流程不同（driver 用 .xls，survival/tmb 用 .xlsx），
# 这两个队列证据冲突，就不回退、如实报缺——猜一个扩展名等于赌执行端读不读得开。
_LEGACY5_COHORT: dict = {}

def load_legacy5_runs() -> None:
    """加载五个旧版工具的实跑组合与旧版表文件名。文件缺失则留空 = 一条都不覆盖。"""
    path = os.path.join(SKILL_REF, "legacy5_proven_runs.tsv")
    if not os.path.exists(path):
        return
    try:
        seen: dict = {}     # study -> [{clinical 名}, {metainfo 名}]，用来判队列级是否唯一
        with open(path, newline="", encoding="utf-8") as f:
            lines = [ln for ln in f if not ln.startswith("#")]
        for row in csv.DictReader(lines, delimiter="\t"):
            t, s = (row.get("tool_id") or "").strip(), (row.get("study") or "").strip()
            if not (t and s):
                continue
            clin = (row.get("clinical_name") or "").strip()
            meta = (row.get("metainfo_name") or "").strip()
            _LEGACY5_RUNS.setdefault(t, {})[s] = (clin, meta)
            names = seen.setdefault(s, [set(), set()])
            for j, n in enumerate((clin, meta)):
                if n:
                    names[j].add(n)
                    _LEGACY5_PATHS[n] = _LEGACY5_DIR.format(acc=s, name=n)
        for s, (cs, ms) in seen.items():
            _LEGACY5_COHORT[s] = (next(iter(cs)) if len(cs) == 1 else "",
                                  next(iter(ms)) if len(ms) == 1 else "")
    except Exception:
        _LEGACY5_RUNS.clear()
        _LEGACY5_PATHS.clear()
        _LEGACY5_COHORT.clear()

load_legacy5_runs()

# 实跑失败黑名单（skill/references/failed_runs.tsv）：与 bulk10/legacy5 的白名单互为反面——
# 「流程 × 队列」明确跑挂过（数据侧原因）的组合，validate_plan 判违规、
# validate_execution_chain 判不可提交，让模型改选别的队列，别把已知跑不了的交出去。
# 上游数据修好后从表里删行即解禁。
_FAILED_RUNS: dict = {}     # tool_id -> {study: reason}


def load_failed_runs() -> None:
    path = os.path.join(SKILL_REF, "failed_runs.tsv")
    _FAILED_RUNS.clear()
    if not os.path.exists(path):
        return
    try:
        with open(path, newline="", encoding="utf-8") as f:
            lines = [ln for ln in f if not ln.startswith("#")]
        for row in csv.DictReader(lines, delimiter="\t"):
            t = (row.get("tool_id") or "").strip()
            s = (row.get("study") or "").strip()
            if t and s:
                _FAILED_RUNS.setdefault(t, {})[s] = (row.get("reason") or "").strip()
    except Exception:
        _FAILED_RUNS.clear()


def _failed_run(gid, acc):
    """该「流程 × 队列」在实跑黑名单里吗？在则返回原因，不在返回 None。"""
    if not gid or not acc:
        return None
    return (_FAILED_RUNS.get(gid) or {}).get(acc)


load_failed_runs()

# 实跑成功白名单（skill/references/succeeded_runs.tsv）：与黑名单互为正面。
# 口径是「**只推实跑验证过的**」：凡是有成功记录的工具（2026-09 运行测试表回流，
# 49 个工具 233 条组合，pipeline 与原子工具都有），只放行表内的「流程/工具 × 队列」——
# validate_plan 判违规（模型修正轮改荐表内队列）、validate_execution_chain 判不可提交。
# 没有任何成功记录的工具不受此约束（不添乱）。bulk10 十条另受 bulk10_proven_runs.tsv
# 一道同口径闸门，两表组合保持一致。
_SUCCEEDED_RUNS: dict = {}     # tool_id -> {study: cromwell_id}


def load_succeeded_runs() -> None:
    path = os.path.join(SKILL_REF, "succeeded_runs.tsv")
    _SUCCEEDED_RUNS.clear()
    if not os.path.exists(path):
        return
    try:
        with open(path, newline="", encoding="utf-8") as f:
            lines = [ln for ln in f if not ln.startswith("#")]
        for row in csv.DictReader(lines, delimiter="\t"):
            t = (row.get("tool_id") or "").strip()
            s = (row.get("study") or "").strip()
            if t and s:
                _SUCCEEDED_RUNS.setdefault(t, {})[s] = (row.get("cromwell_id") or "").strip()
    except Exception:
        _SUCCEEDED_RUNS.clear()


def _unproven_combo(gid, acc):
    """白名单判定。返回 None=放行；否则返回一句可用的违规理由。

    只有「这个工具有成功记录、而该组合不在表里」才拦——工具一条记录都没有时
    白名单对它不构成约束（不然等于把这些工具整体关掉）。"""
    ok = _SUCCEEDED_RUNS.get(gid)
    if not ok or not acc:
        return None
    if acc in ok:
        return None
    return f"{gid} 只在实跑验证过的队列上放行：{'/'.join(sorted(ok))}（{acc} 不在其中）"


load_succeeded_runs()

def _legacy5_pair(gid, acc):
    """五个旧版工具在该队列的旧版临床对：{语义格式: (file_name, file_path)}。不适用则空。"""
    hit = (_LEGACY5_RUNS.get(gid) or {}).get(acc)
    if not hit:
        return {}
    back = _LEGACY5_COHORT.get(acc) or ("", "")
    out = {}
    for i, fmt in enumerate(_CLINICAL_PAIR):
        name = hit[i] or back[i]        # 本行没写就用队列级唯一名（见 `_LEGACY5_COHORT`）
        if name:
            out[fmt] = (name, _LEGACY5_DIR.format(acc=acc, name=name))
    return out

def _clinical_pair_files(acc, gid=None, fmts=None):
    """一个队列的临床表/样本元信息表：{语义格式: (file_name, file_path)}。

    这两张表**在图内**（每个队列各一份、都带真实 file_path），与 bulk10 的
    sample.csv/individual.csv 不同——后者才是图外、按路径模板推的。

    **按目录整体选，不逐个格式选。** HRA001748/HRA005191 这两个单细胞队列在扁平
    `/data/<ACC>/`（`.xlsx` + `.xlsx`）与 `/data/scRNAseq/<ACC>/`（`.xls` + `.xlsx`）
    下**各有一套完整的两张表**，且 MetaInfo 两边同名。逐个格式独立挑会挑出跨目录的
    组合——Clinical 取 scRNAseq 那份（`.xls` 文件名排前），MetaInfo 取扁平那份
    （同名时路径排前）——等于把两次不同导出的表配成一对喂进同一条流程。
    这里先把候选按目录分组，选出「两张齐全 > 非备份 > 字典序」最优的那个目录，
    再从中取两张，保证成对同源，也保证同一个问题两次规划给同一份。
    扁平那套排在前，与手册记的口径一致（图内 18 份 Clinical/MetaInfo 都在扁平目录、
    一律 `.xlsx`）。
    五个旧版工具例外：它们要 analysis 目录下的旧版表，图内没有，见 `_legacy5_pair`。

    `fmts` 只取哪几个语义格式，默认那对 XLSX。260902 起五条流程改吃三张 CSV
    （`INDIVIDUAL_META`/`SAMPLE_META`/`T1_META`），此时传 `_CSV_META_PARAM_FMT` 的值：
    它们同样在**同一个目录**下成套交付（`/cbb-data/gsa/agent/<ACC>/`），
    分目录分组的逻辑照用，三张齐全的那个目录胜出。"""
    if gid in _LEGACY5_RUNS:
        # 这五条一律不回落到图内新版表：回落等于把跑不动的表当答案交出去。
        # 表里 wgcna×HRA007167/HRA003107/HRA001272 三行本就没有临床列（实跑只给了表达矩阵），
        # 返回空 = 如实报缺，比补一份新版表强。
        return _legacy5_pair(gid, acc)
    if not _SAFE_TOKEN.fullmatch(str(acc or "")):
        return {}
    want_fmts = list(fmts or _CLINICAL_PAIR)
    in_list = ",".join("'" + f + "'" for f in want_fmts)
    rows = neo4j_q([f"MATCH (n) WHERE (n:T1 OR n:T2) AND n.study_accession = '{acc}' "
                    f"AND n.semantic_format IN [{in_list}] "
                    f"AND n.file_path IS NOT NULL "
                    f"RETURN n.semantic_format, n.file_name, n.file_path"])
    by_dir = {}
    for r in (rows[0] if rows else []) or []:
        if not (r and r[0] and r[2]):
            continue
        fmt, fn, fp = r[0], str(r[1] or ""), str(r[2] or "")
        if not fp.startswith("/") or "NOT_FOUND" in fp:
            continue
        d = by_dir.setdefault(os.path.dirname(fp), {})
        if fmt not in d or (fn, fp) < d[fmt]:      # 同目录同格式再有重名，字典序兜底
            d[fmt] = (fn, fp)
    if not by_dir:
        return {}
    return min(by_dir.items(),
               key=lambda kv: (-len(kv[1]), 1 if _BAK_DIR.search(kv[0]) else 0, kv[0]))[1]

# 队列级交付文件：`HRA*-SomaticSNV-1.0.maf` 是全队列汇总，`HRR1725089.maf` 只有一个病人。
# 突变景观/TMB 分组/生存这类队列级分析拿后者等于只分析了 1/77 的人。
_STUDY_LEVEL = re.compile(r"^HRA\d+-", re.I)

# 交付卡的 format（扩展名口径）→ 图内 semantic_format。用来给 `_complete_assets` 的
# `req` 兜底：那六条 pipeline 级流程的图内 io 声明是错的（driver_gene_gender_analysis
# 声明 scrna_object_rds/tabular_bio_data、wgcna 声明 scrna_object_rds/metadata_sample_info），
# 光看图内声明，口径归一和队列级汇总替换这两条规则对它们一条都不生效——
# 实测 wgcna 因此把 TPM 矩阵塞进名叫 `counts_tsv` 的参数。
#
# 这张表的取值必须对着**当前图**核，不能照着扩展名想当然写。0826 图实测：
# `DNA_GENOMIC_ALIGNMENT_BAM` 一个节点都没有（真正的 BAM 是 BQSR 与转录组两种），
# `SCRNA_OBJECT_RDS` 也是 0（rds 全在 `BIO_DATA_CONTAINER_OBJECT` 下）。
# 写错的后果不是报错而是静默失配：req 里那个语义格式在 pool 里永远查不到，
# 下面每一条补全规则对该格式整条失效——问题二 tumor_bai、问题三 input_rds 都是这么丢的。
_CARD_FMT_SEM = {
    "MAF": ("MUTATION_ANNOTATION_FORMAT_MAF",),
    "TSV": ("TABULAR_BIO_DATA",),
    "XLS": _CLINICAL_PAIR, "XLSX": _CLINICAL_PAIR,
    "BAM": ("DNA_ALIGNMENT_BQSR_BAM", "RNA_TRANSCRIPTOME_ALIGNMENT_BAM"),
    "BAI": ("DNA_ALIGNMENT_INDEX_BAI",),
    "VCF": ("DNA_VARIANT_VCF_GENERAL",),
    "TBI": ("DNA_VARIANT_INDEX_TBI",),
    "RDS": ("BIO_DATA_CONTAINER_OBJECT",),
}

def _card_req(gid):
    """交付卡声明的输入语义格式集合（图内 io 声明不可信时的补充来源）。"""
    card = KC_MAP.get(gid) or {}
    out = set()
    for i in card.get("inputs") or []:
        if _is_reference_resource(card, i.get("name")):
            continue
        for sem in _CARD_FMT_SEM.get(str(i.get("format") or "").upper(), ()):
            out.add(sem)
    return out

# 已验证样例输入：某个语义格式在图内有多份候选，但实测只有一份能真正跑通，其余的
# 对象结构对不上。与 `_BULK10_RUNS` 同一性质——是既成事实的白名单，不是启发式。
# `BIO_DATA_CONTAINER_OBJECT` 图内 18 份 rds（HRA001748 十份、HRA005191 六份、
# HRA000087 两份），用户实测只有 HRA000087-merge.rds 这一份是能跑的样例输入，
# 九个吃 rds 的流程（breast_cellchat / scrna_cell_communication / lung_tme_annotation_cnv …）
# 一律锁到它：**没绑就补上，绑了别的就换成它**。绑一份跑不动的 rds 不比不绑强。
_PROVEN_FMT = {
    "BIO_DATA_CONTAINER_OBJECT": "HRA000087-merge.rds",
}

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

# 索引文件的语义格式 → （被索引的语义格式们, 索引扩展名）。索引不是"另一份数据"，
# 是同一份数据的随文件，samtools/GATK 一族没有它直接拒跑。
_INDEX_SEM = {
    "DNA_ALIGNMENT_INDEX_BAI": (("DNA_ALIGNMENT_BQSR_BAM", "RNA_TRANSCRIPTOME_ALIGNMENT_BAM"), ".bai"),
    "DNA_VARIANT_INDEX_TBI": (("DNA_VARIANT_VCF_GENERAL",), ".tbi"),
}

def _index_names(fn, ext):
    """`fn` 的索引文件候选名。图内两种写法都有：0826 实测 4318 个 bai 是
    `X.bam.bai`（追加），1859 个是 `X.BQSR.bai`（换掉最后一段扩展名）。两个都试，
    最终以图内 pool 里存不存在为准，所以多试一个不会凭空造出文件。"""
    out = [fn + ext]
    base = fn.rsplit(".", 1)[0]
    if base != fn:
        out.append(base + ext)
    return out

def _complete_assets(gid, assets, facts):
    """按流程在图内声明的输入槽位补全 assets——只在图里挑，不发明文件。

    三条规则，都对应 96 例标准答案对照表里暴露的系统性缺项：
    ① 流程需要临床表时，把该队列的临床表与样本元信息表补齐（见 `_CLINICAL_PAIR`）。
       实测调用方十次有八次只给表达矩阵/MAF 就交卷。判据是「图内 io 声明了
       CLINICAL_DATA_EXCEL **或**交付卡把这一对写成了参数」——只看图内声明会漏：
       driver_gene_gender_analysis 的图内 io 是 scrna_object_rds/tabular_bio_data，
       wgcna 是 scrna_object_rds/metadata_sample_info，两条都不含
       CLINICAL_DATA_EXCEL，而它们的卡片明写 clinical_xls+metainfo_xlsx 必填。
    ② 表达矩阵口径按 `_pipeline_flavor` 归一：调用方选了同队列的其它口径就换成默认
       口径。换的是同一队列同一张表的另一个定量版本，不是换数据源。
    ③ 该语义格式在队列里**恰好**有一份队列级交付文件（见 `_STUDY_LEVEL`）时，把调用方
       选的逐样本文件换成它。恰好一份是关键：FASTQ 一份都没有（不动），表达矩阵有三份
       （口径之争交给 ②），只有 MAF/CNV 这类「汇总一份 + 逐样本 N 份」才落到这条上。
    ④ 双端补对家（见 `_mate_name`）。
    ⑤ 补随文件索引 BAM→BAI、VCF.GZ→TBI（见 `_INDEX_SEM`）：manta/GATK/bcftools 的卡片
       把 `tumor_bai`/`filtered_vcf_index` 写成必填，少一个就是 execution_params_missing。
    ②③④⑤ 只在能从已选资产反查到唯一 study_accession 时生效；资产为空时不做任何事——
    队列没定，图里 576 个 FASTQ 挑哪个都是猜。唯一的例外是 ⓪ 已验证样例输入
    （见 `_PROVEN_ASSET`），那是白名单里写死的一份，没有可猜的余地。"""
    # 图内 io 声明与交付卡声明取并集：前者对原子工具准，对那六条 pipeline 级流程是错的
    # （见 `_CARD_FMT_SEM`），后者反过来只在有卡片时有。少一边就有规则整条失效。
    req = {s["name"].upper() for s in _graph_tool_io(gid)[0]} | _card_req(gid)
    _meta = _needs_clinical(gid)
    need_clin = bool(_meta)

    # ⓪ 已验证样例输入（见 `_PROVEN_FMT`）。必须排在下面"资产为空就整条早退"之前：
    # 问题三的实况正是调用方一个 asset 都没给，早退之后就再没有第二次机会补。
    # 这里不违反"资产为空时不做任何事"的初衷——那条防的是从几百个候选里瞎猜，
    # 而这条填的是唯一已知能跑通的那一份，没有可猜的余地。
    pre, pinned = [], []
    assets = list(assets)
    for sem, want in _PROVEN_FMT.items():
        if sem not in req:
            continue
        got = [a for a in assets
               if (facts.get(str(a.get("file_name") or "")) or {}).get("semantic_format") == sem]
        if any(str(a.get("file_name") or "") == want for a in got):
            continue
        for a in got:                      # 绑了别的候选：换掉，不是叠加
            pre.append(f"{a['file_name']}→{want}")
            assets.remove(a)
        if not got:
            pre.append("+" + want)
        assets.append({"file_name": want,
                       "match_reason": f"{gid} 已验证可跑通的样例输入"})
        pinned.append(want)

    if (not req and not need_clin) or not assets:
        return assets, pre
    acc = next((f.get("study_accession") for a in assets
                if (f := facts.get(a.get("file_name")) or {}).get("study_accession")), None)
    if not acc and pinned:   # ⓪ 刚补进来的文件不在 facts 里（facts 建于调用方那批），补查一次
        facts = {**facts, **_asset_facts(pinned)}
        acc = next((f.get("study_accession") for n in pinned
                    if (f := facts.get(n) or {}).get("study_accession")), None)
    if not acc:
        return assets, pre
    pool = _study_assets(acc)
    if not pool:
        return assets, pre
    notes = list(pre)

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

    # ① 临床/元信息成对补全。bulk10 一律不补：它走 CNCB 原生 CSV 那条路（`_bulk10_params`
    # 按队列号推 sample.csv/individual.csv），再塞图内的 Clinical/MetaInfo xlsx 就是两套
    # 元数据同时喂进去——卡片那组 require_any 本来就是二选一，喂两套跑起来不报错，
    # 读错哪一套只有结果不对时才看得出来。§3.1 的契约是「只有 expr 一个必填输入」。
    #
    # 家族随卡片走：260902 的 5 条改声明 individual_csv/sample_csv/t1_csv 之后吃 CSV 那组，
    # 其余仍吃 XLSX 那对。**被淘汰的那一族要从 assets 里摘掉**——否则同一次提交里
    # XLSX 和 CSV 两套元数据同时在，执行端读哪套看心情（旧版遗留问题四就是这么来的）。
    _clin_fam = {v for v in _meta.values() if v in _CSV_META_PARAM_FMT.values()} \
        or ({v for v in _meta.values() if v in _CLINICAL_PARAM_FMT.values()} or None)
    if gid not in _BULK10 and _clin_fam and ("CLINICAL_DATA_EXCEL" in req or need_clin
                                             or "INDIVIDUAL_META" in req):
        # 五个旧工具走 analysis 目录下的旧版表（见 `_LEGACY5_RUNS`）。这里必须**先把图内
        # 新版表从 assets 里摘掉再补旧版**：只补不摘就是两套元数据一起交，而卡片只吃一份，
        # 读到哪一份看执行端心情。PDF 问题四报的就是这条补出来的
        # `HRA001272-Clinical-1.0.xlsx | /hpcdisk1/cbb_group/data/HRA001272/…`。
        legacy = _legacy5_pair(gid, acc) if gid in _LEGACY5_RUNS else {}
        # 只摘**另一族**：CSV 族内部三张表是一起交的，按 fmt 逐个摘会把同族刚补的删掉。
        # 判据用「文件名落在那族的 pool 里」而不是 facts 的 semantic_format：
        # 调用方自己绑上来的资产未必进了 facts，按 facts 判会漏摘。
        _drop_names = {f.lower()
                       for d in (set(_CLINICAL_PAIR) | set(_CSV_META_PARAM_FMT.values()))
                       - _clin_fam
                       for f in (pool.get(d) or [])}
        for fmt in sorted(_clin_fam):
            files = sorted(pool.get(fmt) or [])   # 定序：同一问题两次规划给同一份
            want = legacy.get(fmt)
            if gid in _LEGACY5_RUNS:
                wrong = [a for a in assets
                         if str(a.get("file_name") or "") in files
                         and str(a.get("file_name") or "") != (want or ("", ""))[0]]
                for a in wrong:
                    notes.append("-" + str(a.get("file_name")))
                    assets.remove(a)
                if not want:            # 实跑记录里这条流程在该队列本就不吃这张表
                    continue
            # 先摘掉不属于本族的另一族（把 XLSX 换成 CSV 时，旧的 xlsx 留在 assets 里
            # 会被 `have` 判成"已有了"从而跳过补全，最后两套表一起交出去）。
            for a in [a for a in assets
                      if str(a.get("file_name") or "").lower() in _drop_names]:
                notes.append("-" + str(a.get("file_name")))
                assets.remove(a)
            have = {str(a.get("file_name") or "").lower() for a in assets}
            if want:
                if want[0].lower() in have:
                    continue
                new = {"file_name": want[0], "file_path": want[1],
                       "match_reason": f"{gid} 在 {acc} 上的实跑记录用的是 analysis 目录下的"
                                       f"旧版 {fmt}（图内新版表结构不同，跑不通）"}
            else:
                if not files or (have & {f.lower() for f in files}):
                    continue
                new = {"file_name": files[0],
                       "match_reason": f"{gid} 声明需要 {fmt} 输入槽位，按队列 {acc} 补全"}
            assets.append(new)
            notes.append("+" + new["file_name"])

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

    # ⑤ 补随文件索引（BAM→BAI、VCF.GZ→TBI）：调用方只交数据文件，而 manta/GATK/bcftools
    # 这些卡片把 `tumor_bai`/`filtered_vcf_index` 写成必填槽位——少一个索引就是
    # execution_params_missing。只在卡片确实声明了该索引格式时补，且只补 pool 里真有的。
    for idx_fmt, (data_fmts, ext) in _INDEX_SEM.items():
        if idx_fmt not in req:
            continue
        idx_pool = set(pool.get(idx_fmt) or ())
        if not idx_pool:
            continue
        data_pool = {f for df in data_fmts for f in (pool.get(df) or ())}
        for fn in sorted(have & data_pool):
            hit = next((c for c in _index_names(fn, ext) if c in idx_pool and c not in have), None)
            if hit:
                assets.append({"file_name": hit, "match_reason": f"{fn} 的索引文件"})
                have.add(hit)
                notes.append("+" + hit)

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
    if _alias_recs(plan):
        filled.append("recommendations←拼写变体")

    # —— 顶层 answer 归一 ——
    # 没有推荐可给时，答案本身就是交付物，但 v2 信封里 match_note 长在 recommendations[i]
    # 下面，空推荐时无处可写。实测模型会自己造一个顶层字段来装（match_note 21 / note 3 /
    # summary 2 / explanation 1，共 4 种形态），前端一个都不认，等于白写。这里统一收编到
    # `answer`：已经生成的内容不浪费，也不必为此多烧一轮修正。
    if not str(plan.get("answer") or "").strip():
        for k in ("match_note", "summary", "note", "explanation", "information"):
            cand = plan.get(k)
            if isinstance(cand, str) and cand.strip():
                plan["answer"] = cand.strip()
                if k != "answer":
                    plan.pop(k, None)
                filled.append(f"answer←{k}")
                break

    # —— no_candidate 自相矛盾：把 answer 里已经点名的工具提成 rank1 ——
    # `no_candidate` 的字面意思是「闭集 55 个工具没有一个能做」，但实测它几乎总是被用来说
    # 「没有匹配的**数据**」或「输入模态对不上」——Q_0054/Q_0055/Q_0235 三例都判了
    # no_candidate + 空推荐，而同一份 answer 开头就写着「闭集内能做聚类分型的是
    # rnaseq_unsupervised_cluster」。手册写过这条规则，回传违规让它自己改也试过：
    # 修正轮照样原样再交一遍（模型认为输入格式不符就不该推荐），一轮白烧。
    # 所以在服务端确定性地做掉：按出现顺序取 answer 里第一个闭集 tool_id 提成 rank1，
    # 差距说明由 answer 承载（match_note 指回 answer）。用户至少拿到一条可执行的东西，
    # 也不必为此多花一轮。只处理 `no_candidate`——`unsupported` 要留给「因果推断/机制断言」
    # 这类本就不该给推荐的问题（Q_0190/Q_0290 靠空推荐才判对），一起处理会拆掉拒绝纪律。
    if (not plan.get("recommendations")
            and str(plan.get("selection_status") or "").lower() == "no_candidate"):
        _ans = str(plan.get("answer") or "")
        _hits = sorted(
            ((mm.start(), t) for t in CATALOG
             for mm in [re.search(r"(?<![A-Za-z0-9_])" + re.escape(t) + r"(?![A-Za-z0-9_])", _ans)]
             if mm),
            key=lambda p: p[0])
        if _hits:
            _tid = _hits[0][1]
            plan["recommendations"] = [{
                "pipeline_id": _tid, "rank": 1,
                "match_note": "闭集内最接近目标的流程；与本次请求的差距见 answer",
                "tool": {"tool_id": _tid},
                # 空 assets 而不是不给 data：下面 `_complete_assets` 的入口条件是
                # `isinstance(data["assets"], list)`，少这一层这条 rank1 就永远拿不到
                # 已验证样例输入/临床对这些补全——问题三的 breast_cellchat 正是这么空手交卷的。
                "data": {"status": "missing_from_graph", "assets": []},
            }]
            # 状态改 `missing_from_graph` 而不是 `ok`：这类问题的缺口恰恰在数据侧
            # （WES 想要聚类分型 / 从 MAF 起步做体细胞检测），assets 本来就填不出来，
            # 判 `ok` 会立刻撞上下面「ok 必须给出图内真实文件」那条违规、白烧一轮修正。
            # `missing_from_graph` 在 _NO_REC_OK 里，语义正是「工具有、图内没有对得上的数据」。
            plan["selection_status"] = "missing_from_graph"
            filled.append(f"rank1←answer({_tid})")

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
    # 同名跨队列时的消歧锚点（见 `_node_rank`）：**恰好一个**队列才用。多个队列说明这批
    # assets 本就混着队列，拿其中一个当锚点会把另一批的同名文件全体拽错边，不如不锚、
    # 退回「避开备份目录 + 字典序」。锚点两个来源：声明的 study_accessions，以及调用方
    # 已经写在 file_path 里的队列号。
    _hint = {str(s) for r in recs for s in ((r.get("data") or {}).get("study_accessions") or [])}
    _hint |= {m for r in recs for a in ((r.get("data") or {}).get("assets") or [])
              for m in _HRA.findall(str(a.get("file_path") or ""))}
    acc_hint = next(iter(_hint)) if len(_hint) == 1 else None
    facts = _asset_facts(want, acc_hint)
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
    allnames = [a.get("file_name") for r in recs
                for a in ((r.get("data") or {}).get("assets") or []) if a.get("file_name")]
    facts.update(_asset_facts([n for n in allnames if n not in facts], acc_hint))
    # 同名的全部合法路径，供下面判断「调用方给的路径要不要覆盖」（见 `_name_paths`）
    allpaths = _name_paths(allnames)

    # `missing_from_graph` 但手里攥着图内查得到的文件 —— 这是自相矛盾的交卷：一边说
    # "图里没有对得上的数据"，一边把真实路径列出来。调用方判这个状态往往是拿卡片声明的
    # 格式名去查图查空了（`SCRNA_OBJECT_RDS` 图内 0 个，见 `_CARD_FMT_SEM`），不是真缺数据。
    # 以图为准把状态改回来：facts 里查得到就是图内确实有。
    for rec in recs:
        data = rec.get("data")
        if not isinstance(data, dict) or str(data.get("status") or "").lower() != "missing_from_graph":
            continue
        if any(facts.get(str(a.get("file_name") or "")) for a in (data.get("assets") or [])):
            data["status"] = "available"
            filled.append(f"{rec.get('pipeline_id')}.data.status←available(图内已确认)")
            if str(plan.get("selection_status") or "").lower() == "missing_from_graph":
                plan["selection_status"] = "ok"

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
                # 路径以图内记录为准，覆盖调用方给的值。两个例外：
                # ① 旧版临床表：HRA000873/HRA000071 那两份 .xlsx 图内扁平目录下同名也有
                #    一份，照图回填会把实跑用的 analysis 路径改掉（见 `_LEGACY5_PATHS`）。
                # ② 调用方给的路径本身就是这个文件名在图内的合法路径之一（见 `_name_paths`）：
                #    同名多路径时 `f` 只是择优挑出的那一条，照它覆盖等于把调用方明确选中的
                #    HRA007167 的 BAM 改写成 HRA003107 的同名 BAM——静默串队列。
                fn_ = str(a.get("file_name") or "")
                if f.get("file_path") and a.get("file_path") != _LEGACY5_PATHS.get(fn_) \
                        and a.get("file_path") not in (allpaths.get(fn_) or set()):
                    a["file_path"] = f["file_path"]
                a.setdefault("read_pair", None)
                filled.append("asset:" + str(a.get("file_name")))
            data.setdefault("source", "neo4j")
            if data.get("assets"):
                data.setdefault("matched_count", len(data["assets"]))
                data.setdefault("missing_asset_names", [])
    # —— alternatives 的角色信息以图为准补齐 ——
    # 手册要求配对/分组分析在 data 下附 alternatives[]（候选队列）。sample_roles /
    # role_resolved 目前是模型自己写的，可能与图不符——前端要拿它标「有无对照组」，
    # 标错比不标更糟。这两个字段以图为准覆盖（selected 尊重模型/用户的选择不动）。
    alt_accs = [a.get("study_accession") for r in recs
                for a in ((r.get("data") or {}).get("alternatives") or [])
                if a.get("study_accession")]
    alt_roles = {}
    for acc in dict.fromkeys(alt_accs):
        if not _SAFE_FILE.fullmatch(str(acc)):
            continue
        try:
            rr = tool_resolve_sample_roles({"study": acc, "sample_limit": 0})
            if rr.get("status") == "ok":
                alt_roles[acc] = (rr.get("sample_roles") or {}, bool(rr.get("role_resolved")))
        except Exception:
            pass                        # 图不通不影响交付，角色信息留模型原值
    for rec in recs:
        for a in ((rec.get("data") or {}).get("alternatives") or []):
            got = alt_roles.get(a.get("study_accession"))
            if got:
                a["sample_roles"], a["role_resolved"] = got
                filled.append(f"alternatives[{a.get('study_accession')}].roles")
    plan["recommendation_count"] = len(recs)

    # —— candidates：原子链槽位一律按 Knowledge Card 补全 ——
    for c in plan.get("candidates") or []:
        if not isinstance(c, dict):
            continue
        key = "tool_chain" if c.get("tool_chain") else "chain"
        chain = c.get(key) or []
        # 裸名字数组先就地升格成对象，后面的补全才有地方落（见 _step_tool_id）
        if any(not isinstance(s, dict) for s in chain):
            chain = [s if isinstance(s, dict) else {"tool_id": str(s)} for s in chain]
            c[key] = chain
        for step in chain:
            tid = _step_tool_id(step)
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
    # 这个字段是重版 MCP 的遗留：重版在 server 内嵌了 LLM planner，字段记录它跑了几轮。
    # light 版把 route_pipeline_request/rule_baseline_plan 整个删了，规划归调用方模型，
    # server 只出手册 + read_cypher + 确定性校验，所以「server 内 planner 没参与」恒真。
    # 原值写的是 status="force_rule"，字面意思是「被迫降级到规则」——跟本架构
    # 「生产路径不存在规则规划，也就不存在静默降级」的主张正好相反，而且它出现在每一份
    # 返回给客户端的信封里、手册示例里也照抄了，实测会把人读岔。改成如实描述。
    plan["planner_metadata"] = {"used": False, "reason": "no_server_side_planner",
                                "planning_owner": "caller_model", "arch": "light"}
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

def tool_route_pipeline_request(args):
    """一次调用拿完整方案：server 内部跑一整轮规划循环，返回顶层 tool-chain/v2 执行合同。

    这是给「只会调一次工具」的客户端准备的兼容层（Cohort Agent 原来接重版
    knowledge-graph-mcp 就是这么调的）。light 的正常用法是调用方模型自己驱动
    read_cypher/validate_execution_chain/hydrate_plan——那条路少一次模型嵌套，
    延迟低一半，能自己跑 agent 循环的客户端应该走那条。

    实现放在 cohort_adapter 里惰性 import：这个工具要用 LLM，而 server 绝大多数
    进程（web 层拉起来的那个子进程、benchmark、CI）根本不碰它，不该为它付
    import web/server.py 的成本、更不该被它顶层的 config.local 注入改掉环境。
    """
    import cohort_adapter
    return cohort_adapter.route(args.get("query"),
                                args.get("top_k", 3),
                                args.get("data_matcher_mode", "neo4j"))

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
        "description": "提交前把关：五阶段探查（注册/卡契约必填输入/绑定结构/数据探查/链流转），输出 tool-chain-validation/v1.2 逐阶段报告 + execution_params（键=卡片参数名，值=真实文件路径；Array[File] 参数的值是路径数组）+ execution_params_by_step（多步链以此为准；其 tool_id 是卡片 meta.id 而非入参的图谱 id，对步骤请按 step 下标取）+ execution_params_missing（对象 {param,tool_id,step,reason}）+ submittable。带卡片默认值的参考/索引资源（star 两个索引、rsem_index、gtf_file、interval_list）不映射也不报缺。steps: [{tool_id, inputs:{name: binding}}]。",
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
    "route_pipeline_request": {
        "description": "一次调用拿完整方案（给只会调一次工具的客户端，如 Cohort Agent）：server 内部跑完整规划循环（手册→模型→取数→接地校验→确定性补全），返回**顶层 tool-chain/v2 执行合同**，不再包一层信封。candidates[].tool_chain 每步是执行绑定（step_id/tool_id/inputs 的 asset_id|value|from 对象），assets 带 asset_id/path/artifact_type，recommendations[].tool.inputs[].builder_param 与 execution_params 的键逐字一致。模型或 Neo4j 不可用时返回 selection_status=no_candidate + unsupported_reason，不做规则降级。**已经自己在跑 agent 循环的客户端不要用它**——用 read_cypher + validate_execution_chain + hydrate_plan 那条路，延迟低一半。",
        "inputSchema": {"type": "object",
                        "properties": {"query": {"type": "string", "description": "用户原始问题"},
                                       "top_k": {"type": "integer", "description": "推荐条数上限，默认 3；light 严格 top-1，实际只返回 1 条 recommendation。candidates 不受它限制，最少保留 3 条（一站式流程 + 原子链拆法）"},
                                       "data_matcher_mode": {"type": "string", "description": "数据匹配后端，light 只有 neo4j"}},
                        "required": ["query"]},
        "handler": tool_route_pipeline_request,
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
