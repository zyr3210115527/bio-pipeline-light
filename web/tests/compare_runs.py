#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web/tests/compare_runs.py — 跨轮次回归对比：把多个 trajectories_* 目录的结果并表输出。

用法：python3 web/tests/compare_runs.py [目录...]（默认取全部 trajectories_*，按时间排序）
"""
import glob
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

RUN_LABELS = {
    "173424": "中文SKILL+gemini ①",
    "174600": "中文SKILL+gemini ②(断流修复)",
    "180431": "中文SKILL+gemini ③(失败复测)",
    "181815": "英文SKILL+gemini",
    "183855": "英文SKILL+deepseek 基线",
    "184704": "英文SKILL+deepseek+提示词纪律",
    "185922": "快照手册+thinking+内联前",
    "191856": "组合工具+内联手冊(v11)",
    "192755": "v12: +nudge@6+校验预算",
    "193311": "v13: +契约置末尾(最终)",
    "203615": "v13+24例扩展集(8并发)",
    "221455": "mimo-v2.5-pro(中断,23/24)",
    "222504": "deepseek+速度纪律+防空烧",
}


def load_run(d):
    rs = []
    for f in sorted(glob.glob(os.path.join(d, "q*.json"))):
        rs.append(json.load(open(f, encoding="utf-8")))
    return rs


def stats(rs):
    if not rs:
        return None
    plan = [r for r in rs if r["expect"] == "plan"]
    rej = [r for r in rs if r["expect"].startswith("reject")]
    ok = [r for r in rs if r["pass"]]
    plan_ok = [r for r in plan if r["pass"]]
    out = {
        "n": len(rs), "pass": len(ok),
        "plan_pass": len(plan_ok), "plan_n": len(plan),
        "rej_pass": sum(1 for r in rej if r["pass"]), "rej_n": len(rej),
        "avg_all": statistics.mean(r["duration_s"] for r in rs),
        "avg_plan": statistics.mean(r["duration_s"] for r in plan) if plan else 0,
        "avg_rej": statistics.mean(r["duration_s"] for r in rej) if rej else 0,
        "rounds_plan": statistics.mean(r["rounds"] for r in plan_ok if r["rounds"]) if any(r["rounds"] for r in plan_ok) else 0,
        "calls_plan": statistics.mean(len(r["tools"]) for r in plan) if plan else 0,
        "infra_err": sum(1 for r in rs if r.get("error") and ("400" in r["error"] or "中断" in r["error"] or "未产出" in r["error"])),
        "fmt_bad": sum(1 for r in rs if r["fmt"] in ("prose", "json_wrapped")),
    }
    return out


def main():
    dirs = sys.argv[1:] or sorted(glob.glob(os.path.join(HERE, "trajectories_*")))
    rows = []
    for d in dirs:
        ts = os.path.basename(d).replace("trajectories_", "")
        label = RUN_LABELS.get(ts, ts)
        st = stats(load_run(d))
        if st:
            rows.append((ts, label, st))
    print(f"{'轮次':<34} {'通过':>6} {'相关':>6} {'拒绝':>6} {'均时s':>6} {'相关均s':>7} {'拒绝均s':>7} {'轮数':>5} {'调用':>5} {'格式坏':>5} {'infra':>5}")
    for ts, label, st in rows:
        print(f"{label:<34} {st['pass']:>3}/{st['n']:<3} {st['plan_pass']:>3}/{st['plan_n']:<3} "
              f"{st['rej_pass']:>3}/{st['rej_n']:<3} {st['avg_all']:>6.0f} {st['avg_plan']:>7.0f} "
              f"{st['avg_rej']:>7.1f} {st['rounds_plan']:>5.1f} {st['calls_plan']:>5.1f} "
              f"{st['fmt_bad']:>5} {st['infra_err']:>5}")


if __name__ == "__main__":
    main()
