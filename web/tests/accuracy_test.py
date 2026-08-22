#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web/tests/accuracy_test.py — 96 例「问题-数据-工具」对应表准确率回归

与 batch_test.py 的区别：batch 判的是**契约**（该规划的出规划、该拒的拒、格式合法），
这里判的是**选得对不对**——用例自带标准答案（期望工具 + 期望数据文件），逐条比对：

  · tool_hit   rank1 推荐的 pipeline_id == 期望工具（主指标）
  · tool_any   期望工具出现在任一 recommendation / candidate 里（召回，用来区分
               「完全没想到」和「想到了但没排第一」——后者调排序即可，前者是检索问题）
  · data_hit   期望数据文件全部出现在 rank1 的 data.assets[].file_name 里

两个口径上的坑：
  ① 标准答案写 `HRA001272-Clinical-1.0.xls`，本地图内是同名 `.xlsx`。属于交付版本差异，
     比对时按「去掉 .xls/.xlsx 后缀」归一，不算错。
  ② NVM0598_* / ENCSR142YZV_* 这几个文件在本地这份旧图里根本不存在（图 81,494 节点，
     文档口径 81,621）。涉及它们的用例数据项标为 data_na，单独统计，不计入 data 分母——
     图里没有的东西，模型选不出来不是代码问题。

用法：python3 web/tests/accuracy_test.py [--base http://127.0.0.1:8017] [--workers 8]
                                         [--only c01,c02] [--limit 20]
                                         [--rescore web/tests/acc_YYYYMMDD_HHMMSS]
输出目录：web/tests/acc_YYYYMMDD_HHMMSS/
"""
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CASES_FILE = os.path.join(HERE, "cases96.json")

# 本地旧版图谱里确实不存在的期望文件（见模块 docstring 坑②）
MISSING_IN_GRAPH = {
    "nvm0598_r1.clean.clean.fastq.gz", "nvm0598_r2.clean.clean.fastq.gz",
    "encsr142yzv_chr19only_10000_reads_r1.fastq.gz",
    "encsr142yzv_chr19only_10000_reads_r2.fastq.gz",
}

TIMEOUT = 480


def norm_file(name):
    """文件名归一：小写 + 去掉 .xls/.xlsx 差异（标准答案与图内交付版本不一致，见坑①）。"""
    n = str(name or "").strip().lower()
    if n.endswith(".xlsx"):
        n = n[:-1]
    return n


def extract_json(text):
    mm = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    return json.loads(mm.group(1) if mm else text)


def rebuild_text(events):
    buf = []
    for e in events:
        if e["type"] == "text":
            buf.append(e.get("delta", ""))
        elif e["type"] == "text_reset":
            buf = ["".join(buf)[: e.get("keep", 0)]]
    return "".join(buf)


def _plan_files(rec):
    """一条推荐里出现的所有资产文件名（归一后）。"""
    data = (rec or {}).get("data") or {}
    out = set()
    for a in data.get("assets") or []:
        if isinstance(a, dict) and a.get("file_name"):
            out.add(norm_file(a["file_name"]))
        elif isinstance(a, str):
            out.add(norm_file(a))
    return out


def judge(case, events, error, duration):
    """按标准答案判定单条用例。"""
    final_text = rebuild_text(events)
    parsed, fmt = None, "empty"
    if final_text.strip():
        try:
            parsed = extract_json(final_text)
            fmt = "json"
        except Exception:
            mm = re.search(r"(\{.*\})", final_text, re.S)
            try:
                parsed = json.loads(mm.group(1)) if mm else None
                fmt = "json_wrapped" if isinstance(parsed, dict) else "prose"
            except Exception:
                fmt = "prose"
    is_plan = isinstance(parsed, dict) and parsed.get("schema_version") == "tool-chain/v2"
    recs = (parsed.get("recommendations") or []) if isinstance(parsed, dict) else []
    cands = (parsed.get("candidates") or []) if isinstance(parsed, dict) else []

    exp_tool = case["tool"]
    rank1 = recs[0] if recs else None
    got_tool = (rank1 or {}).get("pipeline_id")
    tool_hit = got_tool == exp_tool

    # 召回：任一推荐 / 任一候选链的任一步命中
    pool = {r.get("pipeline_id") for r in recs if isinstance(r, dict)}
    for c in cands:
        if not isinstance(c, dict):
            continue
        pool.add(c.get("pipeline_id"))
        for s in c.get("tool_chain") or []:
            if isinstance(s, dict):
                pool.add(s.get("tool_id"))
    tool_any = exp_tool in pool

    exp_files = {norm_file(f) for f in case.get("data") or []}
    na_files = exp_files & MISSING_IN_GRAPH
    scorable = exp_files - MISSING_IN_GRAPH
    got_files = _plan_files(rank1)
    got_all = set()
    for r in recs:
        got_all |= _plan_files(r)
    missed = sorted(scorable - got_files)
    data_hit = bool(scorable) and not missed
    data_hit_any = bool(scorable) and not (scorable - got_all)

    ok = bool(is_plan) and fmt == "json" and tool_hit and (data_hit or not scorable)
    if error:
        ok = False

    tools = [e["name"] for e in events if e["type"] == "tool_call"]
    done = next((e for e in events if e["type"] == "done"), {})
    return {
        "id": case["id"], "question": case["question"], "expect_tool": exp_tool,
        "expect_data": sorted(exp_files), "data_na": sorted(na_files),
        "pass": ok, "tool_hit": tool_hit, "tool_any": tool_any,
        "data_hit": data_hit, "data_hit_any": data_hit_any,
        "got_tool": got_tool, "got_files": sorted(got_files), "missed_files": missed,
        "error": error, "duration_s": duration,
        "rounds": done.get("rounds"), "finishReason": done.get("finishReason"),
        "fmt": fmt,
        "selection_status": parsed.get("selection_status") if isinstance(parsed, dict) else None,
        "tools": tools,
        "final_text": final_text, "events": events,
    }


def run_case(base, case):
    sess = f"acc-{case['id']}-{int(time.time())}"
    body = json.dumps({"session": sess, "message": case["question"]}).encode()
    req = urllib.request.Request(base + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    events, error = [], None
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            buf = b""
            while True:
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n\n" in buf:
                    raw, buf = buf.split(b"\n\n", 1)
                    for line in raw.split(b"\n"):
                        if not line.startswith(b"data:"):
                            continue
                        try:
                            ev = json.loads(line[5:].decode("utf-8"))
                        except Exception:
                            continue
                        ev["_t"] = round(time.time() - t0, 1)
                        events.append(ev)
                        if ev["type"] == "error":
                            error = ev.get("message")
    except Exception as e:
        error = f"客户端异常: {e}"
    return judge(case, events, error, round(time.time() - t0, 1))


def write_outputs(outdir, ts, ordered, base, workers, wall):
    n = len(ordered)
    agg = {
        "started_at": ts, "base": base, "total": n, "wall_time_s": wall, "workers": workers,
        "pass": sum(1 for r in ordered if r["pass"]),
        "tool_hit": sum(1 for r in ordered if r["tool_hit"]),
        "tool_any": sum(1 for r in ordered if r["tool_any"]),
        "data_hit": sum(1 for r in ordered if r["data_hit"]),
        "data_hit_any": sum(1 for r in ordered if r["data_hit_any"]),
        "fmt_bad": sum(1 for r in ordered if r["fmt"] != "json"),
        "errors": sum(1 for r in ordered if r["error"]),
        "cases_with_na_data": sum(1 for r in ordered if r["data_na"]),
        "by_tool": {}, "confusion": {},
    }
    ds = sorted(r["duration_s"] or 0 for r in ordered)
    agg["avg_s"] = round(sum(ds) / max(1, n), 1)
    agg["p50_s"] = ds[n // 2] if n else 0
    agg["p90_s"] = ds[int(n * 0.9)] if n else 0
    agg["over_30s"] = sum(1 for d in ds if d > 30)
    for r in ordered:
        b = agg["by_tool"].setdefault(r["expect_tool"], {"total": 0, "tool_hit": 0, "data_hit": 0})
        b["total"] += 1
        b["tool_hit"] += 1 if r["tool_hit"] else 0
        b["data_hit"] += 1 if r["data_hit"] else 0
        if not r["tool_hit"]:
            key = f"{r['expect_tool']} → {r['got_tool']}"
            agg["confusion"][key] = agg["confusion"].get(key, 0) + 1
    agg["cases"] = [{k: r[k] for k in ("id", "question", "expect_tool", "got_tool", "pass",
                                       "tool_hit", "tool_any", "data_hit", "data_hit_any",
                                       "missed_files", "data_na", "error", "duration_s",
                                       "rounds", "fmt", "selection_status", "tools")}
                    for r in ordered]
    with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(agg, f, ensure_ascii=False, indent=1)

    lines = [f"# 96 例准确率报告 {ts}", "",
             f"- 服务 {base} · {n} 例 · 并发 {workers} · 总耗时 {wall}s", "",
             f"- **整体通过（工具+数据都对且格式合法）: {agg['pass']}/{n}**",
             f"- 工具命中 rank1: {agg['tool_hit']}/{n} · 出现在候选里: {agg['tool_any']}/{n}",
             f"- 数据命中 rank1: {agg['data_hit']}/{n} · 出现在任一推荐: {agg['data_hit_any']}/{n}",
             f"- 格式非法 {agg['fmt_bad']} · 报错 {agg['errors']} · "
             f"含图内缺失文件的用例 {agg['cases_with_na_data']}",
             f"- 耗时 avg {agg['avg_s']}s · p50 {agg['p50_s']}s · p90 {agg['p90_s']}s · "
             f">30s {agg['over_30s']} 例", "",
             "## 按期望工具", "", "| 期望工具 | 例数 | 工具命中 | 数据命中 |", "|---|---|---|---|"]
    for t, b in sorted(agg["by_tool"].items(), key=lambda kv: -kv[1]["total"]):
        lines.append(f"| {t} | {b['total']} | {b['tool_hit']} | {b['data_hit']} |")
    if agg["confusion"]:
        lines += ["", "## 误选分布（期望 → 实际）", ""]
        for k, c in sorted(agg["confusion"].items(), key=lambda kv: -kv[1]):
            lines.append(f"- {k} × {c}")
    lines += ["", "## 逐例", "",
              "| 用例 | 问题 | 期望工具 | 实际 | 工具 | 数据 | 缺失文件 | 轮数 | 耗时 | 判定 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in ordered:
        lines.append(
            f"| {r['id']} | {r['question'][:22]} | {r['expect_tool']} | {r['got_tool'] or '-'} | "
            f"{'✅' if r['tool_hit'] else ('◐' if r['tool_any'] else '❌')} | "
            f"{'✅' if r['data_hit'] else '❌'} | {','.join(r['missed_files'])[:40] or '-'} | "
            f"{r['rounds']} | {r['duration_s']}s | {'✅' if r['pass'] else '❌'} |")
    with open(os.path.join(outdir, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return agg


def main():
    base, workers = "http://127.0.0.1:8017", 8
    args = sys.argv[1:]
    if "--base" in args:
        base = args[args.index("--base") + 1]
    if "--workers" in args:
        workers = int(args[args.index("--workers") + 1])
    cases = json.load(open(CASES_FILE, encoding="utf-8"))
    if "--only" in args:
        wanted = set(args[args.index("--only") + 1].split(","))
        cases = [c for c in cases if c["id"] in wanted]
    if "--limit" in args:
        cases = cases[: int(args[args.index("--limit") + 1])]

    if "--rescore" in args:
        outdir = args[args.index("--rescore") + 1]
        all_cases = {c["id"]: c for c in json.load(open(CASES_FILE, encoding="utf-8"))}
        ordered = []
        for cid, c in all_cases.items():
            path = os.path.join(outdir, f"{cid}.json")
            if not os.path.exists(path):
                continue
            old = json.load(open(path, encoding="utf-8"))
            r = judge(c, old["events"], old.get("error"), old.get("duration_s"))
            with open(path, "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False, indent=1)
            ordered.append(r)
        agg = write_outputs(outdir, os.path.basename(outdir).replace("acc_", ""),
                            ordered, base, 0, "-")
        print(f"[rescore] pass {agg['pass']}/{agg['total']} tool {agg['tool_hit']} "
              f"data {agg['data_hit']} → {outdir}", flush=True)
        return

    ts = time.strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(HERE, f"acc_{ts}")
    os.makedirs(outdir, exist_ok=True)
    print(f"[acc] {len(cases)} 例, workers={workers} → {outdir}", flush=True)

    results = {}

    def work(case):
        r = run_case(base, case)
        results[case["id"]] = r
        with open(os.path.join(outdir, f"{case['id']}.json"), "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1)
        print(f"[acc] {case['id']} {'PASS' if r['pass'] else 'FAIL'} "
              f"tool={'Y' if r['tool_hit'] else ('~' if r['tool_any'] else 'N')} "
              f"data={'Y' if r['data_hit'] else 'N'} "
              f"({r['duration_s']}s, rounds={r['rounds']}, fmt={r['fmt']}, "
              f"got={r['got_tool']}, err={r['error']})", flush=True)
        return r

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, cases))
    ordered = [results[c["id"]] for c in cases]
    agg = write_outputs(outdir, ts, ordered, base, workers, round(time.time() - t0, 1))
    print(f"[acc] 完成: pass {agg['pass']}/{agg['total']} · tool_hit {agg['tool_hit']} "
          f"· tool_any {agg['tool_any']} · data_hit {agg['data_hit']} "
          f"· avg {agg['avg_s']}s → {outdir}", flush=True)


if __name__ == "__main__":
    main()
