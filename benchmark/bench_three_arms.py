#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bench_three_arms.py — 三条臂评测（floor / ceiling / SUT）

臂              是什么                          预期
floor           永远猜最高频工具                  ~15%（随机线）
ceiling         当前 RULES 关键词表               原集高、去名集暴跌（它见过答案）
SUT             skill + read-cypher + 真实模型     去名集 70%+（本脚本只跑 floor/ceiling，
                                                 SUT 由 real_agent_bench.py 在 QUERIES_JSON 模式下跑，
                                                 结果由 collect_sut() 汇总到 same JSON）

用法：
  python3 bench_three_arms.py                    # floor + ceiling + 汇总已跑的 SUT
  QUERIES_JSON=benchmark/data/de_named_set.json python3 benchmark/real_agent_bench.py
"""
import json, os, sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bench_light_96 import predict_tool, read_xlsx_rows  # noqa: E402

ORIGINAL_XLSX = os.environ.get("ORIGINAL_XLSX", "")
DENAMED_JSON = os.path.join(HERE, "data", "de_named_set.json")
SUT_DIR = "/tmp/agent_final_"  # real_agent_bench 每查询落盘前缀


def load_original():
    if not ORIGINAL_XLSX:
        return []
    rows = read_xlsx_rows(ORIGINAL_XLSX)
    return [{"query": r["q"], "expected_pipeline_id": r["expected"]} for r in rows]


def load_denamed():
    return json.load(open(DENAMED_JSON))


def most_frequent_tool(cases):
    return Counter(c["expected_pipeline_id"] for c in cases).most_common(1)[0][0]


def run_arm(name, cases, predict):
    ok = 0
    per_tool = Counter()
    for c in cases:
        preds = predict(c["query"])
        hit = bool(preds) and preds[0] == c["expected_pipeline_id"]
        ok += hit
        per_tool[c["expected_pipeline_id"]] += hit
    return {"arm": name, "cases": len(cases), "hit": ok,
            "accuracy": round(ok / len(cases), 4) if cases else None}


def collect_sut(denamed):
    ok = total = 0
    for c in denamed:
        f = f"{SUT_DIR}{c['case_id']}.json"
        if not os.path.exists(f):
            continue
        r = json.load(open(f))
        total += 1
        if c["expected_pipeline_id"] in (r.get("final") or ""):
            ok += 1
    return {"arm": "SUT(skill+模型)", "cases": total, "hit": ok,
            "accuracy": round(ok / total, 4) if total else None,
            "note": "expected_pipeline_id 出现在最终答案文本中"}


def main():
    orig = load_original()
    de = load_denamed()
    if not orig:
        print("设置 ORIGINAL_XLSX=<96例xlsx路径> 以包含原集数字")
    results = []
    if orig:
        floor_tool = most_frequent_tool(orig)
        print(f"floor 工具 = {floor_tool}")
        results.append(run_arm(f"floor(猜{floor_tool})", de, lambda q: [floor_tool]))
        results.append(run_arm("ceiling(RULES)@原集", orig, predict_tool))
    results.append(run_arm("ceiling(RULES)@去名集", de, predict_tool))
    if os.path.exists(SUT_DIR + de[0]["case_id"] + ".json"):
        results.append(collect_sut(de))
    else:
        print("SUT 结果未找到，先跑: QUERIES_JSON=benchmark/data/de_named_set.json python3 benchmark/real_agent_bench.py")
    print(json.dumps(results, ensure_ascii=False, indent=1))
    out = os.path.join(HERE, "data", "three_arms_results.json")
    json.dump(results, open(out, "w"), ensure_ascii=False, indent=1)
    print(f"saved {out}")


if __name__ == "__main__":
    main()
