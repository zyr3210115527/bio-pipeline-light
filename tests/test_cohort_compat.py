#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cohort Agent 兼容性验收：route_pipeline_request 一次调用拿到可提交的执行合同。

    python3 tests/test_cohort_compat.py            # 全部（含 3 例真实模型循环）
    python3 tests/test_cohort_compat.py --offline  # 只跑离线部分，不调 LLM、不连 Neo4j

离线部分走点名流程的确定性路径（运行绑定，见 run_bindings.py），验证 MCP 工具声明与合同形状；
合同细节与绑定表自检在 tests/test_run_bindings.py。
在线部分走完整 route()（模型循环 + Neo4j），验证没点名流程的问句也落在已绑定的流程与队列上。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mcp_light_server as M          # noqa: E402
import run_bindings as rb             # noqa: E402

FAIL = []


def check(cond, label, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  ← {detail}"))
    if not cond:
        FAIL.append(label)


def test_tools_list():
    print("\n[1] tools/list 同时包含 health_check 与 route_pipeline_request")
    names = set(M.TOOLS)
    check("health_check" in names, "health_check 存在")
    check("route_pipeline_request" in names, "route_pipeline_request 存在")
    check("query" in M.TOOLS["route_pipeline_request"]["inputSchema"]["required"],
          "query 是必填参数")
    props = M.TOOLS["route_pipeline_request"]["inputSchema"]["properties"]
    check({"top_k", "data_matcher_mode"} <= set(props), "top_k / data_matcher_mode 可传")


def test_contract_shape():
    print("\n[2] 点名流程：顶层就是 tool-chain/v2，参数原样来自绑定")
    h = M.TOOLS["route_pipeline_request"]["handler"]
    gid = "manta_structural_variants"
    out = h({"query": f"我想用癌症数据进行{gid}分析", "top_k": 3})
    check(out.get("schema_version") == "tool-chain/v2", "顶层 schema_version")
    check("plan" not in out and "status" not in out, "没有 {status,plan} 外层信封", list(out)[:8])
    if not rb.bindings_for(gid):
        check(out.get("selection_status") == "unsupported", "没有绑定 → unsupported")
        return
    check(out.get("selection_status") == "ready", "ready", out.get("unsupported_reason"))
    rec = out["recommendations"][0]
    b = rb.get(gid, out["candidates"][0]["study_accession"])
    check(rec["execution_params"] == b["execution_params"], "execution_params == 绑定原值")
    check(rec["tool"]["catalog_status"] == "registered", "catalog_status=registered")
    check(all(s.get("builder_param") == s.get("name") for s in rec["tool"]["inputs"]),
          "builder_param 即卡片参数名")
    check(out.get("mcp_timing_ms") is not None, "带 mcp_timing_ms")

    print("\n[3] 没有绑定的流程：如实说做不了，不换流程")
    out = h({"query": "我想用癌症数据进行gatk_germline_cohort分析"})
    check(out.get("selection_status") == "unsupported", "unsupported", out.get("selection_status"))
    check(out.get("candidates") == [], "候选为空")
    check(bool(out.get("unsupported_reason")), "unsupported_reason 非空")


def test_live():
    print("\n[4] 真实模型循环（route_pipeline_request 端到端）")
    h = M.TOOLS["route_pipeline_request"]["handler"]
    r = h({"query": "我想对黑色素瘤 bulk RNA-seq 数据做免疫浸润分析",
           "top_k": 3, "data_matcher_mode": "neo4j"})
    check(r.get("schema_version") == "tool-chain/v2", "规划题：顶层 tool-chain/v2")
    check(r.get("selection_status") == "ready", "规划题：ready", r.get("unsupported_reason"))
    tid = (r.get("intent") or {}).get("tool_id")
    st = (r.get("candidates") or [{}])[0].get("study_accession")
    check(rb.get(tid, st) is not None, "规划题：落在已绑定的「流程 × 队列」上", (tid, st))

    r = h({"query": "gatk 支持哪些输入格式？"})
    check(r.get("selection_status") != "no_candidate", "知识题：不当作规划失败",
          r.get("selection_status"))
    check(bool(r.get("answer")), "知识题：answer 非空")

    r = h({"query": "今天天气怎么样"})
    check(r.get("selection_status") in ("unsupported", "no_candidate"),
          "拒绝题：不给推荐", r.get("selection_status"))
    check(bool(r.get("unsupported_reason")), "拒绝题：unsupported_reason 非空")
    check(r.get("candidates") == [], "拒绝题：候选为空")


def main():
    test_tools_list()
    test_contract_shape()
    if "--offline" not in sys.argv:
        test_live()
    print("\n" + ("全部通过" if not FAIL else f"失败 {len(FAIL)} 项：" + "; ".join(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
