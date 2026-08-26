#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web/tests/qa_final_test.py — qa_final 系列（class1/class2/class3）跑批

数据来源：图谱交付方给的 `qa_final_*.xlsx`，三套共用同一批 550 个标注，
只是问法不同（转成 TSV 见 benchmark/qa_final_class*.tsv）：

  class1_tool_recommend       给了数据和目标，问**用哪个工具**
  class2_data_recommend       给了工具和目标，问**要什么数据**
  class3_data_tool_recommend  只给目标，问**数据和工具都要**

每套 550 = 450 single_tool + 50 tool_chain + 50 negative。

与既有三个跑批器的分工：
  batch_test.py     24 例 · 判契约 · 门禁
  accuracy_test.py  96 例 · 判选型（单一期望工具 + 期望文件）
  normal_test.py   165 例 · 判选型召回（98 例有期望工具）
  本文件        550×N 例 · 判**选型 + 数据类型召回 + 拒绝纪律**，标注最全

标注口径（原样照搬交付方的表，没有改写）：
  · tool_name    single_tool 是单个名字；tool_chain 是 "fastp -> star" 有序链
  · required_inputs  语义格式数组，是「这个目标需要什么数据」的标准答案
  · study        标注方挑的那个队列。**问句没点名项目时它不是唯一解**——
                 图里别的队列有同样格式的数据也对，所以分「问句点名 / 没点名」两个分母
  · negative_type  no_matching_data(33) / no_tool(10) / missing_tumor_type(7)

用法：
  python3 web/tests/qa_final_test.py class3 [--base http://127.0.0.1:8017]
                                            [--workers 16] [--limit N] [--sample N] [--ids a,b|@file]
                                            [--type single_tool|tool_chain|negative]
                                            [--rescore web/tests/qa_YYYYMMDD_HHMMSS]
输出目录：web/tests/qa_<class>_YYYYMMDD_HHMMSS/
"""
import csv
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from accuracy_test import extract_json, rebuild_text, run_case as _raw_run  # noqa: E402

BENCH = os.path.join(HERE, "..", "..", "benchmark")
KC_MAP = os.path.join(HERE, "..", "..", "skill", "references", "knowledge_cards_map.json")


def _alias():
    """knowledge card 变体名 → 图内 tool_name。

    标准答案用的是图里的 `tool_name`（fastp / star / bwa），而模型输出的
    tool_id 可能是 knowledge card 的变体（fastp_paired_end / star_rrna_and_
    genome_alignment），两者指同一个工具。卡片里的 `graph_tool_id` 就是这层映射，
    不归一会把「答对了」判成「答错」（实测 32 例抽样里错判 3 条）。
    """
    try:
        with open(KC_MAP, encoding="utf-8") as f:
            m = json.load(f)
    except Exception:
        return {}
    return {k: v["graph_tool_id"] for k, v in m.items()
            if isinstance(v, dict) and v.get("graph_tool_id")
            and v["graph_tool_id"] != k}


ALIAS = _alias()


def norm_tool(t):
    return ALIAS.get(t, t)


# ---------------------------------------------------------------- 载入
def _jlist(cell):
    """required_inputs / functions 等列是 JSON 字面量，但偶有裸字符串。"""
    s = str(cell or "").strip()
    if not s:
        return []
    try:
        v = json.loads(s)
    except Exception:
        return [s]
    return v if isinstance(v, list) else [v]


def _chain(tool_name):
    """'fastp -> star' → ['fastp','star']；单工具 → ['fastp']。"""
    return [t.strip() for t in re.split(r"->|→", str(tool_name or "")) if t.strip()]


def load_cases(klass):
    path = os.path.join(BENCH, f"qa_final_{klass}.tsv")
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    out = []
    for r in rows:
        tools = _chain(r["tool_name"])
        out.append({
            "id": r["qa_id"], "question": r["question"], "type": r["qa_type"],
            "goal": r["goal"], "tools": tools, "study": r["study"].strip(),
            "project": r["project_name"].strip(), "tumor": r["tumor_subtype"].strip(),
            "formats": [str(x) for x in _jlist(r["required_inputs"])],
            "refs": [str(x) for x in _jlist(r["reference_inputs"])],
            "neg": r["negative_type"].strip(),
            "gold": r["answer"],
        })

    # 同一句问话在表里出现多次、标着不同的标准答案（450 例 single_tool 里 268 例如此：
    # 「目标是功能富集分析，应该选择什么数据和什么工具？」的标准答案有 5 个）。
    # 这类题 top1 的上限就是 1/k，判分必须并记一个「命中同题任一标准答案」的口径，
    # 否则量到的是标注的多义性，不是系统的选型能力。
    alt = {}
    for c in out:
        if c["type"] == "single_tool":
            alt.setdefault(c["question"], set()).update(c["tools"])
    for c in out:
        s = alt.get(c["question"]) or set(c["tools"])
        c["alt_tools"] = sorted(s)
        c["ambiguous"] = len(s) > 1
    return out


# ---------------------------------------------------------------- 判定
def _tok(hay, needle):
    """工具名/格式名的词边界匹配。

    不能用 `\\b`：工具名紧邻中文时（"推荐工具fastp。"）中文与 `f` 之间没有 ASCII
    词边界，`\\bfastp\\b` 判 False。改成显式的「前后都不是标识符字符」。
    """
    return bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(needle) + r"(?![A-Za-z0-9_])", hay))


def _pool(parsed):
    """(有序推荐工具, 全部出现过的工具, 有序工具链)。"""
    if not isinstance(parsed, dict):
        return [], set(), []
    recs = parsed.get("recommendations") or []
    cands = parsed.get("candidates") or []
    ranked = [norm_tool(r.get("pipeline_id")) for r in recs if isinstance(r, dict)]
    allset = {x for x in ranked if x}
    chain = []
    for c in cands:
        if not isinstance(c, dict):
            continue
        if c.get("pipeline_id"):
            allset.add(norm_tool(c["pipeline_id"]))
        steps = [norm_tool(s.get("tool_id")) for s in (c.get("tool_chain") or [])
                 if isinstance(s, dict) and s.get("tool_id")]
        allset |= set(steps)
        if len(steps) > len(chain):
            chain = steps
    # 单工具推荐里也可能带 tool_chain
    for r in recs:
        for s in ((r or {}).get("tool") or {}).get("chain") or []:
            if isinstance(s, str):
                allset.add(norm_tool(s))
    return ranked, allset, chain


def _got_formats(parsed, text):
    """模型给出的语义格式集合：结构化字段优先，其次全文里的大写格式 token。"""
    got = set()
    if isinstance(parsed, dict):
        for r in parsed.get("recommendations") or []:
            for a in ((r or {}).get("data") or {}).get("assets") or []:
                if isinstance(a, dict) and a.get("semantic_format"):
                    got.add(a["semantic_format"])
            for i in ((r or {}).get("tool") or {}).get("inputs") or []:
                if isinstance(i, dict) and i.get("semantic_format"):
                    got.add(i["semantic_format"])
        for c in parsed.get("candidates") or []:
            for a in (c or {}).get("assets") or []:
                if isinstance(a, dict) and a.get("semantic_format"):
                    got.add(a["semantic_format"])
    got |= set(re.findall(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,}\b", text))
    return got


NEG_MARK = re.compile(
    r"未找到|找不到|没有找到|无法|不支持|缺少|缺失|未登记|不具备|不齐备|没有(?:可用|对应|合适)|"
    r"no_candidate|unsupported|missing_from_graph|not_found|rejected", re.I)

# 「系统认为这次没给出可执行答案」的状态集合
NEG_STATUS = {"no_candidate", "unsupported", "missing_from_graph", "needs_input",
              "information", "rejected", "not_available"}

REV_ALIAS = {}
for _v, _g in ALIAS.items():
    REV_ALIAS.setdefault(_g, []).append(_v)


def _mentions(body, tool):
    """工具名出现在终答里——变体名也算（fastp_paired_end 就是 fastp）。"""
    return _tok(body, tool) or any(_tok(body, v) for v in REV_ALIAS.get(tool, []))


def _subseq(gold, got):
    """gold 是否为 got 的有序子序列。

    标准答案只标了关键的两步（trim_galore -> star），模型给出完整链
    （fastqc -> trim_galore -> star -> rsem）时逐位相等必然为假，但顺序是对的。
    """
    it = iter(got)
    return all(any(g == x for x in it) for g in gold)


def judge_negative(r, text, ranked, chain, status):
    """negative 三种成因判定口径不同——用同一把尺子会误判。

    · no_tool            图里没有能处理该数据的工具 → 不许给出任何工具推荐
    · no_matching_data   工具存在、但点名的队列没有它要的数据 → **允许点名工具**
                         （标准答案自己就点了 snpeff），要求的是「说清数据不齐」
    · missing_tumor_type 项目未登记癌种 → 要求指出这个前提缺失，不能直接当齐备来答
    """
    says_no = bool(NEG_MARK.search(text))
    not_ok = status in NEG_STATUS
    if r["neg"] == "no_tool":
        return (not ranked and not chain), says_no
    # 另两类：只要没把它当作「一切齐备、可以执行」来回答，就算守住了
    return (not_ok or (says_no and not ranked)), says_no


def judge(case, events, error, duration):
    text = rebuild_text(events)
    parsed, fmt = None, "empty"
    if text.strip():
        try:
            parsed = extract_json(text)
            fmt = "json"
        except Exception:
            mm = re.search(r"(\{.*\})", text, re.S)
            try:
                parsed = json.loads(mm.group(1)) if mm else None
                fmt = "json_wrapped" if isinstance(parsed, dict) else "prose"
            except Exception:
                fmt = "prose"
    is_plan = isinstance(parsed, dict) and parsed.get("schema_version") == "tool-chain/v2"
    ranked, allset, chain = _pool(parsed)

    # 问句原样回显在 intent.query_text 里，点名判定必须先把它挖掉，否则问句自己
    # 提到的工具名会被算成模型的答案
    body = re.sub(r'"query_text"\s*:\s*"(?:[^"\\]|\\.)*"', '"query_text":""', text)

    exp = case["tools"]
    named = any(_tok(case["question"], t) for t in exp) if exp else False
    ment = [t for t in exp if _mentions(body, t)]

    r = {
        "id": case["id"], "type": case["type"], "goal": case["goal"],
        "question": case["question"], "neg": case["neg"],
        "expect_tools": exp, "ranked": ranked[:5], "chain": chain,
        "question_names_tool": named,
        "fmt": fmt, "is_plan": is_plan,
        "selection_status": parsed.get("selection_status") if isinstance(parsed, dict) else None,
        "error": error, "duration_s": duration,
        "rounds": next((e.get("rounds") for e in events if e["type"] == "done"), None),
        "tools_called": [e["name"] for e in events if e["type"] == "tool_call"],
        "final_text": text, "events": events,
    }

    if case["type"] == "negative":
        r["neg_pass"], r["neg_says_no"] = judge_negative(
            r, text, ranked, chain, r["selection_status"])
        r["top1"] = r["top3"] = r["any"] = r["mention"] = None
        r["chain_exact"] = r["chain_subseq"] = r["chain_cover"] = None
        r["fmt_recall"] = r["study_hit"] = None
        r["ambiguous"] = r["top1_alt"] = r["any_alt"] = None
        return r

    r["neg_pass"] = r["neg_says_no"] = None
    # ---- 工具选型 ----
    if case["type"] == "tool_chain":
        # 连续去重后与标准答案比：逐位相同（严）/ 有序子序列（宽，模型常给完整链）
        seq, prev = [], None
        for t in chain or ranked:
            if t != prev:
                seq.append(t)
            prev = t
        r["chain_exact"] = seq == exp
        r["chain_subseq"] = _subseq(exp, seq)
        r["chain_cover"] = all(t in allset for t in exp)
        r["top1"] = bool(ranked) and ranked[0] == exp[0]
        r["top3"] = exp[0] in ranked[:3]
        r["any"] = exp[0] in allset
    else:
        r["chain_exact"] = r["chain_subseq"] = r["chain_cover"] = None
        r["top1"] = bool(ranked) and ranked[0] in exp
        r["top3"] = bool(set(exp) & set(ranked[:3]))
        r["any"] = bool(set(exp) & allset)
    # 同题多解：命中该问句在表里出现过的任一标准答案
    alt = set(case.get("alt_tools") or exp)
    r["ambiguous"] = bool(case.get("ambiguous"))
    r["alt_n"] = len(alt)
    r["top1_alt"] = bool(ranked) and ranked[0] in alt
    r["any_alt"] = bool(alt & allset)
    # 大量问题是「要什么数据/哪条流程」，模型合理地走 information 分支、recommendations
    # 留空，答案写在自然语言里。rank 口径对这些必然判 0，所以并记一条文本召回。
    r["mention"] = bool(ment)

    # ---- 数据类型召回 ----
    got = _got_formats(parsed, text)
    exf = [f for f in case["formats"] if f]
    r["expect_formats"] = exf
    r["fmt_recall"] = round(len([f for f in exf if f in got]) / len(exf), 3) if exf else None
    r["fmt_full"] = (all(f in got for f in exf)) if exf else None
    r["missed_formats"] = [f for f in exf if f not in got]

    # ---- 队列 ----
    # 问句没点名项目时，标注的 study 只是众多可行解之一，命中与否不能当对错
    r["study_named"] = bool(case["project"] and case["project"][:20] in case["question"]) \
        or bool(case["study"] and case["study"] in case["question"])
    r["study_hit"] = (case["study"] in text) if case["study"] else None
    return r


def run_one(base, case):
    raw = _raw_run(base, {"id": case["id"], "question": case["question"],
                          "tool": None, "data": []})
    return judge(case, raw["events"], raw["error"], raw["duration_s"])


# ---------------------------------------------------------------- 汇总
def summarize(ordered, meta):
    def cnt(rows, key):
        return sum(1 for r in rows if r.get(key))

    st = [r for r in ordered if r["type"] == "single_tool"]
    ch = [r for r in ordered if r["type"] == "tool_chain"]
    ng = [r for r in ordered if r["type"] == "negative"]
    sel = [r for r in st if not r["question_names_tool"]]   # 真·选型题
    kno = [r for r in st if r["question_names_tool"]]       # 问句已点名工具
    amb = [r for r in st if r.get("ambiguous")]             # 同题在表里有多个标准答案
    uni = [r for r in st if not r.get("ambiguous")]         # 同题唯一解

    fr = [r for r in ordered if r.get("fmt_recall") is not None]
    sn = [r for r in ordered if r.get("study_named")]

    ds = sorted(r["duration_s"] or 0 for r in ordered)
    n = len(ds)
    pct = lambda q: ds[min(n - 1, int(round((n - 1) * q)))] if n else 0  # noqa: E731

    confusion = {}
    for r in st + ch:
        if not r["top1"]:
            k = f"{'->'.join(r['expect_tools'])} → {(r['ranked'] or ['-'])[0]}"
            confusion[k] = confusion.get(k, 0) + 1
    by_neg = {}
    for r in ng:
        b = by_neg.setdefault(r["neg"], {"n": 0, "pass": 0, "says_no": 0})
        b["n"] += 1
        b["pass"] += 1 if r["neg_pass"] else 0
        b["says_no"] += 1 if r["neg_says_no"] else 0

    agg = dict(meta)
    agg.update({
        "total": len(ordered),
        "single_tool": {"n": len(st), "top1": cnt(st, "top1"), "top3": cnt(st, "top3"),
                        "any": cnt(st, "any"), "mention": cnt(st, "mention"),
                        "top1_alt": cnt(st, "top1_alt"), "any_alt": cnt(st, "any_alt"),
                        "empty_rank": sum(1 for r in st if not r["ranked"])},
        "label_unique": {"n": len(uni), "top1": cnt(uni, "top1"), "top3": cnt(uni, "top3"),
                         "any": cnt(uni, "any"), "mention": cnt(uni, "mention")},
        "label_ambiguous": {"n": len(amb), "top1": cnt(amb, "top1"),
                            "top1_alt": cnt(amb, "top1_alt"), "any_alt": cnt(amb, "any_alt"),
                            "mention": cnt(amb, "mention"),
                            "avg_alt": round(sum(r.get("alt_n") or 1 for r in amb)
                                             / max(1, len(amb)), 2)},
        "selection_only": {"n": len(sel), "top1": cnt(sel, "top1"), "top3": cnt(sel, "top3"),
                           "any": cnt(sel, "any"), "mention": cnt(sel, "mention")},
        "question_named_tool": {"n": len(kno), "top1": cnt(kno, "top1"),
                                "mention": cnt(kno, "mention")},
        "tool_chain": {"n": len(ch), "exact": cnt(ch, "chain_exact"),
                       "subseq": cnt(ch, "chain_subseq"),
                       "cover": cnt(ch, "chain_cover"), "first_top1": cnt(ch, "top1"),
                       "mention": cnt(ch, "mention")},
        "negative": {"n": len(ng), "pass": cnt(ng, "neg_pass"),
                     "says_no": cnt(ng, "neg_says_no"), "by_type": by_neg},
        "data_formats": {"n": len(fr),
                         "avg_recall": round(sum(r["fmt_recall"] for r in fr) / max(1, len(fr)), 3),
                         "full_hit": cnt(fr, "fmt_full")},
        "study": {"named_n": len(sn), "named_hit": cnt(sn, "study_hit"),
                  "unnamed_n": len([r for r in ordered
                                    if r.get("study_hit") is not None and not r.get("study_named")]),
                  "unnamed_hit": cnt([r for r in ordered
                                      if r.get("study_hit") is not None
                                      and not r.get("study_named")], "study_hit")},
        "fmt_bad": sum(1 for r in ordered if r["fmt"] not in ("json", "json_wrapped")),
        "not_plan": sum(1 for r in ordered if not r["is_plan"]),
        "errors": sum(1 for r in ordered if r["error"]),
        "avg_s": round(sum(ds) / max(1, n), 2), "p50_s": pct(.5), "p90_s": pct(.9),
        "p95_s": pct(.95), "p99_s": pct(.99), "max_s": ds[-1] if ds else 0,
        "over_30s": sum(1 for d in ds if d > 30),
        "confusion": dict(sorted(confusion.items(), key=lambda kv: -kv[1])[:40]),
    })
    return agg


def report(agg):
    def p(a, b):
        return f"{a}/{b} ({a / b * 100:5.1f}%)" if b else f"{a}/0 (  n/a)"

    L = [f"# qa_final {agg['klass']} 跑批  {agg['started_at']}",
         "",
         f"- base `{agg['base']}` · {agg['workers']} 并行 · {agg['total']} 例 · "
         f"墙钟 {agg['wall_time_s']}s · 错 {agg['errors']}",
         f"- 耗时 avg {agg['avg_s']}s · p50 {agg['p50_s']}s · p90 {agg['p90_s']}s · "
         f"p95 {agg['p95_s']}s · p99 {agg['p99_s']}s · max {agg['max_s']}s · "
         f">30s {agg['over_30s']} 例",
         "",
         "## 工具选型",
         "",
         "| 池子 | n | top1 | top3 | any | 文本提及 |",
         "|---|---|---|---|---|---|"]
    for key, label in [("single_tool", "single_tool 全部"),
                       ("selection_only", "└ 问句**没**点名工具（真选型）"),
                       ("question_named_tool", "└ 问句已点名工具（知识题）")]:
        d = agg[key]
        L.append(f"| {label} | {d['n']} | {p(d.get('top1', 0), d['n'])} | "
                 f"{p(d.get('top3', 0), d['n'])} | {p(d.get('any', 0), d['n'])} | "
                 f"{p(d.get('mention', 0), d['n'])} |")

    u, a = agg["label_unique"], agg["label_ambiguous"]
    L += ["",
          "### 按标注是否唯一拆开",
          "",
          "同一句问话在表里出现多次、标着不同的标准答案（「目标是功能富集分析，应该选择什么"
          "数据和什么工具？」的标准答案有 5 个：de_enrichment / deg_enrichment / diff_expr_go /"
          " diff_expr_kegg / gsea_pathway_enrichment）。**这类题 top1 的天花板就是 1/k**，"
          "拿它量系统等于量标注的多义性，所以拆开报：",
          "",
          "| 池子 | n | top1（严格） | top1（同题任一标准答案） | 文本提及 |",
          "|---|---|---|---|---|",
          f"| 标注唯一 | {u['n']} | {p(u['top1'], u['n'])} | — | {p(u['mention'], u['n'])} |",
          f"| 标注多解（平均 {a['avg_alt']} 解） | {a['n']} | {p(a['top1'], a['n'])} | "
          f"{p(a['top1_alt'], a['n'])} | {p(a['mention'], a['n'])} |"]
    sa = agg["single_tool"]
    L.append(f"| **合计** | {sa['n']} | {p(sa['top1'], sa['n'])} | "
             f"{p(sa['top1_alt'], sa['n'])} | {p(sa['mention'], sa['n'])} |")
    c = agg["tool_chain"]
    L += ["",
          f"**tool_chain（{c['n']} 例）**：整链逐位相同 {p(c['exact'], c['n'])} · "
          f"标注链是输出链的**有序子序列** {p(c['subseq'], c['n'])} · "
          f"链上工具全部出现 {p(c['cover'], c['n'])} · 首工具 top1 {p(c['first_top1'], c['n'])} · "
          f"文本提及 {p(c['mention'], c['n'])}",
          "",
          "（模型常给完整链 `fastqc→trim_galore→star→rsem`，标注只写关键两步 "
          "`trim_galore→star`，逐位相同必然为假——子序列口径才是有意义的那个。）",
          "",
          "## 数据类型召回（required_inputs 语义格式）",
          "",
          f"- 有标注的 {agg['data_formats']['n']} 例：平均召回 "
          f"{agg['data_formats']['avg_recall'] * 100:.1f}% · "
          f"全中 {p(agg['data_formats']['full_hit'], agg['data_formats']['n'])}",
          f"- 队列命中：问句点名项目的 "
          f"{p(agg['study']['named_hit'], agg['study']['named_n'])}；"
          f"没点名的 {p(agg['study']['unnamed_hit'], agg['study']['unnamed_n'])}"
          f"（**没点名时标注的 study 不是唯一解，这个数只作参考**）",
          "",
          "## 拒绝纪律（negative）",
          "",
          "| negative_type | n | 未给推荐 | 文本明确说了不行 |",
          "|---|---|---|---|"]
    for k, v in sorted(agg["negative"]["by_type"].items()):
        L.append(f"| {k} | {v['n']} | {p(v['pass'], v['n'])} | {p(v['says_no'], v['n'])} |")
    d = agg["negative"]
    L += [f"| **合计** | {d['n']} | {p(d['pass'], d['n'])} | {p(d['says_no'], d['n'])} |",
          "",
          "## 输出契约",
          "",
          f"- 非 JSON {agg['fmt_bad']} 例 · 不是 tool-chain/v2 {agg['not_plan']} 例",
          "",
          "## top1 错在哪（期望 → 实际 rank1，前 20）",
          "",
          "| 期望 → 实际 | 次数 |", "|---|---|"]
    for k, v in list(agg["confusion"].items())[:20]:
        L.append(f"| `{k}` | {v} |")
    return "\n".join(L) + "\n"


def write_outputs(outdir, agg, ordered):
    os.makedirs(outdir, exist_ok=True)
    # summary.json 只留判定结果；原始轨迹（含 events，用于 --rescore）另存
    drop = {"final_text", "events"}
    keep = [k for k in ordered[0] if k not in drop] if ordered else []
    agg["cases"] = [{k: r.get(k) for k in keep} for r in ordered]
    with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(agg, f, ensure_ascii=False, indent=1)
    with open(os.path.join(outdir, "trajectories.json"), "w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=1)
    md = report(agg)
    with open(os.path.join(outdir, "report.md"), "w", encoding="utf-8") as f:
        f.write(md)
    print("\n" + md)
    print(f"→ {outdir}")


def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        sys.exit("用法：python3 web/tests/qa_final_test.py <class1|class2|class3> [选项]")
    klass = args.pop(0)

    def opt(name, default=None):
        return args[args.index(name) + 1] if name in args else default

    base = opt("--base", "http://127.0.0.1:8017")
    workers = int(opt("--workers", "16"))
    cases = load_cases(klass)
    if "--type" in args:
        cases = [c for c in cases if c["type"] == opt("--type")]
    if "--ids" in args:
        # 定点重跑：逗号分隔的 qa_id，或 @文件（每行一个）。改完手册验证某一批失败例专用，
        # 不用为了看 73 例的变化再烧 550 例。
        raw = opt("--ids")
        if raw.startswith("@"):
            with open(raw[1:], encoding="utf-8") as f:
                want = {ln.strip() for ln in f if ln.strip()}
        else:
            want = {x.strip() for x in raw.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in want]
        miss = want - {c["id"] for c in cases}
        if miss:
            print(f"警告：{len(miss)} 个 id 在 {klass} 里不存在，已跳过", file=sys.stderr)
    if "--sample" in args:
        # 分层抽样：三类各按原比例抽，随机种子固定，跑两次可比
        k = int(opt("--sample"))
        rnd = random.Random(20260826)
        buckets = {}
        for c in cases:
            buckets.setdefault(c["type"], []).append(c)
        tot = len(cases)
        picked = []
        for t, lst in buckets.items():
            m = max(1, round(k * len(lst) / tot))
            picked += rnd.sample(lst, min(m, len(lst)))
        cases = sorted(picked, key=lambda c: c["id"])
    if "--limit" in args:
        cases = cases[:int(opt("--limit"))]

    ts = time.strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(HERE, f"qa_{klass}_{ts}")
    meta = {"klass": klass, "started_at": ts, "base": base, "workers": workers}

    if "--rescore" in args:
        src = opt("--rescore")
        with open(os.path.join(src, "trajectories.json"), encoding="utf-8") as f:
            old = json.load(f)
        idx = {c["id"]: c for c in load_cases(klass)}
        ordered = [judge(idx[o["id"]], o.get("events", []), o.get("error"), o["duration_s"])
                   for o in old if o["id"] in idx]
        meta["wall_time_s"] = 0
        write_outputs(src, summarize(ordered, meta), ordered)
        return 0

    print(f"qa_final {klass}：{len(cases)} 例 · {workers} 并行 · {base}", flush=True)
    t0 = time.time()
    done = [0]

    def work(c):
        r = run_one(base, c)
        done[0] += 1
        if done[0] % 25 == 0 or done[0] == len(cases):
            print(f"  {done[0]}/{len(cases)}  {time.time() - t0:.0f}s", flush=True)
        return r

    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(work, cases))
    meta["wall_time_s"] = round(time.time() - t0, 1)
    ordered = sorted(res, key=lambda r: r["id"])
    write_outputs(outdir, summarize(ordered, meta), ordered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
