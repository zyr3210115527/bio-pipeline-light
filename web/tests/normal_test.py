#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web/tests/normal_test.py — 165 例通用问答集跑批

与另外两个跑批器的分工：
  batch_test.py     24 例 · 判**契约**（格式合法、该规划的出规划、该拒的拒）· 是门禁
  accuracy_test.py  96 例 · 判**选型**（单一期望工具 + 期望数据文件）· 有确定答案
  本文件           165 例 · 判**选型召回** · 只有 98 例有期望工具、6 例有期望数据

**这个不是门禁**（理由见 benchmark/normal_questions.README.md）：另外 67 例没有任何
可判定标准答案，当门禁会把「没写答案」判成「答错」。所以分三个池子分别统计：

  · scored(98)   带期望工具 → top1 / top3 / any。期望列是 `;` 分隔的**集合**，
                 命中任一即算命中（同一需求常有多条等价流程）
  · negative(5)  期望零工具调用 → 判拒绝纪律：不许出 recommendations，
                 要么 selection_status 非 available，要么干脆不是 plan
  · unscored(62) 只记格式合法性与耗时，**不进准确率分母**

用法：python3 web/tests/normal_test.py [--base http://127.0.0.1:8017] [--workers 8]
                                       [--limit N] [--intent Negative]
                                       [--rescore web/tests/normal_YYYYMMDD_HHMMSS]
输出目录：web/tests/normal_YYYYMMDD_HHMMSS/
"""
import csv
import json
import re
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from accuracy_test import extract_json, rebuild_text, norm_file, run_case as _raw_run  # noqa: E402

CASES_FILE = os.path.join(HERE, "..", "..", "benchmark", "normal_questions.tsv")


def _split(cell):
    return [t.strip() for t in str(cell or "").replace("；", ";").split(";") if t.strip()]


def load_cases():
    with open(CASES_FILE, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    out = []
    for r in rows:
        out.append({
            "id": r["question_id"].strip(),
            "question": r["question"].strip(),
            "intent": r["intent"].strip(),
            "difficulty": r["difficulty"].strip(),
            # 期望工具是集合，不是单值——命中任一即可
            # 分隔符是**全角** `；`（原表 58 个多工具单元全用它），半角一并兼容
            "tools": _split(r["工具"]),
            "files": _split(r["数据文件"]),
        })
    return out


def _pool(parsed):
    """(rank 有序的推荐工具列表, 全部出现过的工具集合)。"""
    recs = (parsed.get("recommendations") or []) if isinstance(parsed, dict) else []
    cands = (parsed.get("candidates") or []) if isinstance(parsed, dict) else []
    ranked = [r.get("pipeline_id") for r in recs if isinstance(r, dict)]
    allset = set(ranked)
    for c in cands:
        if not isinstance(c, dict):
            continue
        allset.add(c.get("pipeline_id"))
        for s in c.get("tool_chain") or []:
            if isinstance(s, dict):
                allset.add(s.get("tool_id"))
    return ranked, {x for x in allset if x}


def _files(parsed):
    out = set()
    for r in (parsed.get("recommendations") or []) if isinstance(parsed, dict) else []:
        for a in ((r or {}).get("data") or {}).get("assets") or []:
            if isinstance(a, dict) and a.get("file_name"):
                out.add(norm_file(a["file_name"]))
            elif isinstance(a, str):
                out.add(norm_file(a))
    return out


def judge(case, events, error, duration):
    text = rebuild_text(events)
    parsed, fmt = None, "empty"
    if text.strip():
        try:
            parsed = extract_json(text)
            fmt = "json"
        except Exception:
            fmt = "prose"
    is_plan = isinstance(parsed, dict) and parsed.get("schema_version") == "tool-chain/v2"
    ranked, allset = _pool(parsed)
    exp = set(case["tools"])

    if case["intent"] == "Negative":
        bucket = "negative"
        # 拒绝纪律：不许给推荐；给了就是编数据
        hit = {"top1": not ranked, "top3": not ranked, "any": not ranked}
    elif exp:
        bucket = "scored"
        hit = {"top1": bool(ranked) and ranked[0] in exp,
               "top3": bool(exp & set(ranked[:3])),
               "any": bool(exp & allset)}
    else:
        bucket = "unscored"
        hit = {"top1": None, "top3": None, "any": None}

    # 一半以上的问题是「有哪些工具能…」这类知识查询，模型正确地走 information/unsupported
    # 分支、recommendations 留空，答案写在 answer/自由文本里。对这些例子 rank1 口径必然判 0，
    # 所以另记一条召回：期望工具名有没有在终答文本里出现过。
    #
    # 词边界**不能用 `\b`**：工具名紧邻中文时（"工具cellranger_workflow和…"）中文与 `c`
    # 之间没有 ASCII 词边界，`\bcellranger_workflow\b` 判 False，实测少算 17 例。
    # 另外 intent.query_text 是原样回显的问句，问句自己点名了工具就会假阳性——
    # 判定只看去掉 query_text 之后的文本。
    body = re.sub(r'"query_text"\s*:\s*"(?:[^"\\]|\\.)*"', '"query_text":""', text)
    tok = lambda s, x: bool(re.search(  # noqa: E731
        r"(?<![A-Za-z0-9_])" + re.escape(x) + r"(?![A-Za-z0-9_])", s))
    mention = any(tok(body, x) for x in exp) if exp else None
    # 问句自身就点名了期望工具的例子，mention 不能算模型的功劳，单独标出来
    self_named = any(tok(case["question"], x) for x in exp) if exp else None

    expf = {norm_file(f) for f in case["files"]}
    gotf = _files(parsed)
    # 空 recommendations 时答案只能落在顶层 answer 上。165 例基线里 99 例交了空推荐，
    # 其中 72 例整个 JSON 没有任何自然语言字段 = 什么都没回答。这一条就是那个洞的度量。
    ans = ""
    if isinstance(parsed, dict):
        for k in ("answer", "match_note", "summary", "note", "explanation"):
            if isinstance(parsed.get(k), str) and parsed[k].strip():
                ans = parsed[k].strip()
                break
        if not ans:
            for r in parsed.get("recommendations") or []:
                if isinstance(r, dict) and str(r.get("match_note") or "").strip():
                    ans = str(r["match_note"]).strip()
                    break
    answered = bool(ans) or (isinstance(parsed, dict) and parsed.get("status") == "rejected")
    return {
        "id": case["id"], "question": case["question"], "intent": case["intent"],
        "difficulty": case["difficulty"], "bucket": bucket,
        "expect_tools": sorted(exp), "ranked": ranked[:5],
        "top1": hit["top1"], "top3": hit["top3"], "any": hit["any"], "mention": mention,
        "self_named": self_named, "answered": answered, "answer_len": len(ans),
        "n_rec": len(ranked),
        "expect_files": sorted(expf), "missed_files": sorted(expf - gotf)[:8] if expf else [],
        # 6 例里有 5 例期望 30 个 FASTQ——全中是苛刻口径，同时记覆盖率
        "file_hit": (bool(expf) and not (expf - gotf)) if expf else None,
        "file_cover": (round(len(expf & gotf) / len(expf), 3) if expf else None),
        "fmt": fmt, "is_plan": is_plan,
        "selection_status": parsed.get("selection_status") if isinstance(parsed, dict) else None,
        "error": error, "duration_s": duration,
        "rounds": next((e.get("rounds") for e in events if e["type"] == "done"), None),
        "tools_called": [e["name"] for e in events if e["type"] == "tool_call"],
        "final_text": text,
    }


def run_one(base, case):
    r = _raw_run(base, {"id": case["id"], "question": case["question"], "tool": None, "data": []})
    return judge(case, r["events"], r["error"], r["duration_s"])


def write_outputs(outdir, ts, ordered, base, workers, wall):

    def frac(rows, key):
        n = len(rows)
        return sum(1 for r in rows if r[key]), n

    sc = [r for r in ordered if r["bucket"] == "scored"]
    ng = [r for r in ordered if r["bucket"] == "negative"]
    un = [r for r in ordered if r["bucket"] == "unscored"]
    # 问句自身点名了期望工具的（知识题）vs 真要模型选型的，两者 top1 口径含义完全不同，
    # 混在一个分母里会把「答对了的知识题」记成选型错误
    sel = [r for r in sc if not r["self_named"]]
    kno = [r for r in sc if r["self_named"]]
    fh = [r for r in ordered if r["file_hit"] is not None]
    ds = sorted(r["duration_s"] or 0 for r in ordered)
    n = len(ds)
    pct = lambda q: ds[min(n - 1, int(round((n - 1) * q)))] if n else 0  # noqa: E731

    by_intent = {}
    for r in sc:
        b = by_intent.setdefault(r["intent"], {"n": 0, "top1": 0, "top3": 0, "any": 0,
                                               "mention": 0, "empty": 0})
        b["n"] += 1
        for k in ("top1", "top3", "any", "mention"):
            b[k] += 1 if r[k] else 0
        b["empty"] += 0 if r["ranked"] else 1
    confusion = {}
    for r in sc:
        if not r["top1"]:
            k = f"{'/'.join(r['expect_tools'])} → {(r['ranked'] or ['-'])[0]}"
            confusion[k] = confusion.get(k, 0) + 1

    agg = {
        "started_at": ts, "base": base, "workers": workers, "wall_time_s": wall,
        "total": len(ordered),
        "scored": {"n": len(sc), "top1": frac(sc, "top1")[0], "top3": frac(sc, "top3")[0],
                   "any": frac(sc, "any")[0], "mention": frac(sc, "mention")[0],
                   "empty_rank1": sum(1 for r in sc if not r["ranked"]),
                   "nonempty": sum(1 for r in sc if r["ranked"]),
                   "top1_of_nonempty": sum(1 for r in sc if r["ranked"] and r["top1"])},
        "negative": {"n": len(ng), "pass": frac(ng, "top1")[0]},
        "unscored": {"n": len(un)},
        # 选型题（问句没点名工具）：top1 是有意义的
        "selection": {"n": len(sel), "top1": frac(sel, "top1")[0], "top3": frac(sel, "top3")[0],
                      "any": frac(sel, "any")[0], "mention": frac(sel, "mention")[0],
                      "empty_rank1": sum(1 for r in sel if not r["ranked"])},
        # 知识题（问句已点名工具）：只看有没有真的答上
        "knowledge": {"n": len(kno), "answered": frac(kno, "answered")[0],
                      "avg_answer_len": round(sum(r["answer_len"] for r in kno) / max(1, len(kno)))},
        # 这一条覆盖全部 165 例：交了空壳（没有任何自然语言答案）的有多少
        "unanswered": sum(1 for r in ordered if not r["answered"] and not r["error"]),
        "multi_rec": sum(1 for r in ordered if r["n_rec"] > 1),
        "data": {"n": len(fh), "hit": sum(1 for r in fh if r["file_hit"]),
                 "avg_cover": round(sum(r["file_cover"] for r in fh) / max(1, len(fh)), 3)},
        "fmt_bad": sum(1 for r in ordered if r["fmt"] != "json"),
        "not_plan": sum(1 for r in ordered if not r["is_plan"]),
        "errors": sum(1 for r in ordered if r["error"]),
        "avg_s": round(sum(ds) / max(1, n), 2), "p50_s": pct(.5), "p90_s": pct(.9),
        "p99_s": pct(.99), "max_s": ds[-1] if ds else 0,
        "over_30s": sum(1 for d in ds if d > 30),
        "by_intent": by_intent, "confusion": confusion,
        "cases": [{k: r[k] for k in ("id", "question", "intent", "difficulty", "bucket",
                                     "expect_tools", "ranked", "top1", "top3", "any", "mention",
                                     "self_named", "answered", "answer_len", "n_rec",
                                     "expect_files", "missed_files", "file_hit", "fmt",
                                     "file_cover", "is_plan", "selection_status", "error", "duration_s",
                                     "rounds", "tools_called")} for r in ordered],
    }
    with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(agg, f, ensure_ascii=False, indent=1)
    with open(os.path.join(outdir, "trajectories.json"), "w", encoding="utf-8") as f:
        json.dump({r["id"]: r["final_text"] for r in ordered}, f, ensure_ascii=False, indent=1)

    s, g, u = agg["scored"], agg["negative"], agg["unscored"]
    sl, kn = agg["selection"], agg["knowledge"]
    L = [f"# 165 例通用问答报告 {ts}", "",
         f"- {base} · {agg['total']} 例 · 并发 {workers} · 总耗时 {wall}s",
         f"- **交了空壳（无任何自然语言答案）{agg['unanswered']} 例** · "
         f"违反严格 top-1（给了 ≥2 条推荐）{agg['multi_rec']} 例",
         f"- **选型题 {sl['n']} 例（问句没点名工具）：top1 {sl['top1']} "
         f"({sl['top1']/max(1,sl['n']):.1%}) · top3 {sl['top3']} ({sl['top3']/max(1,sl['n']):.1%})"
         f" · any {sl['any']} ({sl['any']/max(1,sl['n']):.1%}) · 空推荐 {sl['empty_rank1']}**",
         f"- **知识题 {kn['n']} 例（问句已点名工具，top1 口径无意义）：答上了 {kn['answered']} "
         f"({kn['answered']/max(1,kn['n']):.1%}) · 答案均长 {kn['avg_answer_len']} 字符**",
         f"- 合计带期望工具 {s['n']} 例：top1 {s['top1']} ({s['top1']/max(1,s['n']):.1%})"
         f" · top3 {s['top3']} ({s['top3']/max(1,s['n']):.1%})"
         f" · 出现在候选里 {s['any']} ({s['any']/max(1,s['n']):.1%})",
         f"  - 其中 {s['empty_rank1']} 例是知识查询（information/unsupported，recommendations 为空，"
         f"rank1 口径必然判 0）；**真正给了推荐的 {s['nonempty']} 例里 top1 命中 "
         f"{s['top1_of_nonempty']} ({s['top1_of_nonempty']/max(1,s['nonempty']):.1%})**",
         f"  - 期望工具名出现在终答里（已剔除 query_text 回显）：{s['mention']} "
         f"({s['mention']/max(1,s['n']):.1%})",
         f"- Negative {g['n']} 例（期望零推荐）：{g['pass']} 通过",
         f"- 带期望数据 {agg['data']['n']} 例：全中 {agg['data']['hit']} · "
         f"平均文件覆盖率 {agg['data']['avg_cover']:.1%}",
         f"- 无标准答案 {u['n']} 例：不计入准确率，仅看格式与耗时",
         f"- 格式非法 {agg['fmt_bad']} · 非 plan {agg['not_plan']} · 报错 {agg['errors']}",
         f"- 耗时 avg {agg['avg_s']}s · p50 {agg['p50_s']}s · p90 {agg['p90_s']}s · "
         f"p99 {agg['p99_s']}s · max {agg['max_s']}s · >30s {agg['over_30s']} 例", "",
         "## 按意图（仅带期望工具的例）", "",
         "| 意图 | 例数 | 空推荐 | top1 | top3 | any | 文本提及 |", "|---|---|---|---|---|---|---|"]
    for k, b in sorted(by_intent.items(), key=lambda kv: -kv[1]["n"]):
        L.append(f"| {k} | {b['n']} | {b['empty']} | {b['top1']} | {b['top3']} | {b['any']} | "
                 f"{b['mention']} |")
    if confusion:
        L += ["", "## 误选分布（期望 → rank1）", ""]
        for k, c in sorted(confusion.items(), key=lambda kv: -kv[1])[:40]:
            L.append(f"- {k} × {c}")
    L += ["", "## 逐例（带期望工具的）", "",
          "| id | 问题 | 期望 | rank1 | top1 | top3 | any | 轮 | 耗时 |", "|---|---|---|---|---|---|---|---|---|"]
    for r in sc + ng:
        L.append(f"| {r['id']} | {r['question'][:26]} | {'/'.join(r['expect_tools']) or '(零推荐)'} | "
                 f"{(r['ranked'] or ['-'])[0]} | {'✅' if r['top1'] else '❌'} | "
                 f"{'✅' if r['top3'] else '❌'} | {'✅' if r['any'] else '❌'} | "
                 f"{r['rounds']} | {r['duration_s']}s |")
    with open(os.path.join(outdir, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")

    print(f"\n[normal] 选型 {sl['n']}: top1 {sl['top1']} / top3 {sl['top3']} / any {sl['any']}"
          f" · 知识 {kn['n']}: 答上 {kn['answered']}"
          f" · **空壳 {agg['unanswered']}** · 多推荐 {agg['multi_rec']}"
          f" · negative {g['pass']}/{g['n']} · data {agg['data']['hit']}/{agg['data']['n']}")
    print(f"[normal] avg {agg['avg_s']}s p90 {agg['p90_s']}s p99 {agg['p99_s']}s "
          f"max {agg['max_s']}s >30s {agg['over_30s']} → {outdir}")


def main():
    base, workers = "http://127.0.0.1:8017", 8
    args = sys.argv[1:]
    if "--base" in args:
        base = args[args.index("--base") + 1]
    if "--workers" in args:
        workers = int(args[args.index("--workers") + 1])
    cases = load_cases()
    if "--intent" in args:
        want = args[args.index("--intent") + 1]
        cases = [c for c in cases if c["intent"] == want]
    if "--limit" in args:
        cases = cases[: int(args[args.index("--limit") + 1])]

    # --rescore：只改判分口径时不重跑模型，直接用已落盘的 trajectories.json 重算
    if "--rescore" in args:
        d = args[args.index("--rescore") + 1]
        traj = json.load(open(os.path.join(d, "trajectories.json"), encoding="utf-8"))
        prev = json.load(open(os.path.join(d, "summary.json"), encoding="utf-8"))
        meta = {c["id"]: c for c in prev["cases"]}
        ordered = []
        for c in cases:
            m = meta.get(c["id"], {})
            r = judge(c, [{"type": "text", "delta": traj.get(c["id"], "")}],
                      m.get("error"), m.get("duration_s") or 0)
            r["rounds"] = m.get("rounds")
            r["tools_called"] = m.get("tools_called") or []
            ordered.append(r)
        write_outputs(d, prev["started_at"], ordered, prev["base"],
                      prev.get("workers", 0), prev.get("wall_time_s", 0))
        return

    ts = time.strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(HERE, f"normal_{ts}")
    os.makedirs(outdir, exist_ok=True)
    print(f"[normal] {len(cases)} 例 · 并发 {workers} · {base}")
    t0 = time.time()
    res = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(run_one, base, c): c for c in cases}
        for fu in futs:
            pass
        for fu, c in futs.items():
            r = fu.result()
            res[c["id"]] = r
            mark = {"scored": {True: "✅", False: "❌"}.get(r["top1"], "?"),
                    "negative": {True: "✅", False: "❌"}.get(r["top1"], "?"),
                    "unscored": "·"}[r["bucket"]]
            print(f"[normal] {r['id']} {mark} {r['bucket']:<8} {r['duration_s']:>5.1f}s "
                  f"rank1={ (r['ranked'] or ['-'])[0] } exp={'/'.join(r['expect_tools']) or '-'}")
    wall = round(time.time() - t0, 1)
    ordered = [res[c["id"]] for c in cases]
    write_outputs(outdir, ts, ordered, base, workers, wall)


if __name__ == "__main__":
    main()
