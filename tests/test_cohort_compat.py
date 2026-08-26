#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cohort Agent 兼容性验收：对应任务单 §6。

    python3 tests/test_cohort_compat.py            # 全部（含 3 例真实模型循环）
    python3 tests/test_cohort_compat.py --offline  # 只跑合同翻译，不调 LLM

离线部分不烧 token：直接喂一份 hydrate 过的 plan 给 to_cohort_v2，验证合同形状。
在线部分走完整 route()，验证「只调一次工具」的客户端确实能拿到可提交的东西。
两部分都要连 Neo4j——资产路径必须是图内真实记录，这正是要验的东西之一。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mcp_light_server as M          # noqa: E402
import cohort_adapter as C            # noqa: E402

FAIL = []


def check(cond, label, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  ← {detail}"))
    if not cond:
        FAIL.append(label)


def _plan(pid, files, acc):
    """构造一份「模型刚交出来」的精简 plan，再走服务端补全，模拟真实终答。"""
    return M.tool_hydrate_plan({"plan": {
        "schema_version": "tool-chain/v2", "selection_status": "ok", "candidates": [],
        "recommendations": [{"pipeline_id": pid, "match_note": "test",
                             "tool": {"tool_id": pid},
                             "data": {"status": "available", "study_accessions": [acc],
                                      "assets": [{"file_name": f, "match_reason": "test"}
                                                 for f in files]}}],
        "intent": {"query_text": "test"}}})["plan"]


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
    print("\n[2] 顶层就是 tool-chain/v2，没有额外包装")
    out = C.to_cohort_v2(_plan("immune_infiltration_iobr",
                               ["HRA001272-Genes-TPM-1.0.tsv"], "HRA001272"), "test", 3)
    check(out.get("schema_version") == "tool-chain/v2", "顶层 schema_version")
    check("plan" not in out and "status" not in out, "没有 {status,plan} 外层信封",
          list(out)[:8])

    print("\n[3] ready 候选字段齐全")
    check(bool(out.get("candidates")), "有候选")
    c = out["candidates"][0]
    check(c["feasibility_status"] == "ready", "feasibility_status=ready", c["feasibility_status"])
    check(c["validation_ok"] is True, "validation_ok=true")
    check(bool(c.get("match_id")), "match_id 非空")
    check(bool(c.get("study_accession")), "study_accession 非空")
    for a in c["assets"]:
        check(bool(a.get("asset_id")), f"{a.get('file_name')} 有 asset_id")
        check(bool(a.get("path")) and a["path"].startswith("/"),
              f"{a.get('file_name')} path 是绝对路径", a.get("path"))
        check(a.get("path") == a.get("file_path"), "path 与 file_path 一致")
        check(bool(a.get("artifact_type")), f"{a.get('file_name')} 有 artifact_type")
    ids = {a["asset_id"] for a in c["assets"]}
    check(len(ids) == len(c["assets"]), "asset_id 唯一")
    for s in c["tool_chain"]:
        check(bool(s.get("step_id")), "step 有 step_id")
        check(s.get("tool_id") in M.KC_MAP or s.get("tool_id") in M.CATALOG,
              f"{s.get('tool_id')} 在闭集/卡片里注册", s.get("tool_id"))
        check(isinstance(s.get("inputs"), dict) and bool(s["inputs"]), "step 有 inputs 绑定")
        for k, b in s["inputs"].items():
            ok = (isinstance(b, dict) and ({"asset_id", "value"} & set(b) or "from" in b)) \
                 or isinstance(b, list)
            check(ok, f"{k} 是绑定对象而非 IO 描述", b)
            if isinstance(b, dict) and "asset_id" in b:
                check(b["asset_id"] in ids, f"{k} 指向已声明的资产", b["asset_id"])

    print("\n[4] recommendations 满足 Dingent 过滤条件")
    rec = out["recommendations"][0]
    check(rec["tool"]["catalog_status"] == "registered", "tool.catalog_status=registered")
    check(rec["data"]["status"] == "available", "data.status=available")
    bps = [i.get("builder_param") for i in rec["tool"]["inputs"]]
    check(all(bps), "每个 tool.inputs[] 都有 builder_param", bps)
    check(set(rec["execution_params"]) <= set(bps),
          "execution_params 的键都在 builder_param 里",
          sorted(set(rec["execution_params"]) - set(bps)))
    check(rec["execution_params_missing"] == [], "execution_params_missing 为空")
    for v in rec["execution_params"].values():
        vs = v if isinstance(v, list) else [v]
        check(all(str(x).startswith("/") for x in vs if not str(x).isidentifier() or "/" in str(x)),
              "execution_params 的路径值都是绝对路径", v)

    print("\n[5] 缺路径 / 缺绑定 / 未注册工具不得 ready")
    # ① 缺路径：给一个图里不存在的文件名，hydrate 补不出 file_path
    bad = C.to_cohort_v2(_plan("immune_infiltration_iobr",
                               ["NOT_A_REAL_FILE.tsv"], "HRA001272"), "test", 3)
    check(bad["candidates"][0]["feasibility_status"] != "ready", "缺路径 → 非 ready",
          bad["candidates"][0]["feasibility_status"])
    check(bad["selection_status"] != "ready", "顶层状态也不是 ready", bad["selection_status"])
    # ② 缺字面量绑定：diff_expr_go 的分组要人来定，服务端不许猜
    need = C.to_cohort_v2(_plan("diff_expr_go",
                                ["HRA001272-Genes-counts-1.0.tsv"], "HRA001272"), "test", 3)
    miss = {m["param"] for m in need["candidates"][0]["execution_params_missing"]}
    check(need["candidates"][0]["feasibility_status"] != "ready", "缺分组 → 非 ready")
    check({"group_a_samples", "group_b_samples"} <= miss, "报缺点名到具体参数", sorted(miss))
    # ③ 未注册工具
    ghost = C.to_cohort_v2({"schema_version": "tool-chain/v2", "selection_status": "ok",
                            "recommendations": [{"pipeline_id": "no_such_tool",
                                                 "tool": {"tool_id": "no_such_tool"},
                                                 "data": {"assets": []}}]}, "test", 3)
    check(ghost["candidates"][0]["feasibility_status"] != "ready", "未注册 → 非 ready")
    check(ghost["recommendations"][0]["tool"]["catalog_status"] == "unregistered",
          "catalog_status=unregistered")

    print("\n[6] 参考资源不进合同，二选一约束会报缺")
    # manta 的 reference_fasta/fai 走容器内默认值，不该出现在 execution_params 里
    card = M.KC_MAP.get("manta_structural_variants")
    inputs, missing = C._bind_step(M, "manta_structural_variants", card, [], [], "step-1")
    check("reference_fasta" not in inputs, "参考基因组不绑")
    check(not any(m["param"] == "reference_fasta" for m in missing), "参考基因组也不报缺")
    # cellchat：seurat_rds / combined_counts 二选一，一个都没有要点名整组
    card = M.KC_MAP.get("scrna_cell_communication")
    _, missing = C._bind_step(M, "scrna_cell_communication", card, [], [], "step-1")
    check(any(m["reason"] == "require_any_unbound" for m in missing),
          "二选一一个都没绑 → require_any_unbound", missing)
    # bulk10 的两张 CNCB 表由服务端推，不该反过来要调用方绑
    card = M.KC_MAP.get("km_survival")
    _, missing = C._bind_step(M, "km_survival", card, [], [], "step-1")
    check(not any("sample_csv" in m["param"] for m in missing),
          "bulk10 sample_csv 不报缺", missing)


def test_live():
    print("\n[7] 真实模型循环（route_pipeline_request 端到端）")
    h = M.TOOLS["route_pipeline_request"]["handler"]
    r = h({"query": "我想对肝癌 bulk RNA-seq 数据做免疫浸润分析",
           "top_k": 3, "data_matcher_mode": "neo4j"})
    check(r.get("schema_version") == "tool-chain/v2", "规划题：顶层 tool-chain/v2")
    check(r.get("selection_status") == "ready", "规划题：ready", r.get("selection_status"))
    check(bool(r.get("candidates")), "规划题：有候选")
    check(bool(r.get("mcp_timing_ms")), "带 mcp_timing_ms")

    r = h({"query": "gatk 支持哪些输入格式？"})
    check(r.get("selection_status") == "information", "知识题：information",
          r.get("selection_status"))
    check(bool(r.get("answer")), "知识题：answer 非空")
    check("unsupported_reason" not in r, "知识题不该带 unsupported_reason")

    r = h({"query": "今天天气怎么样"})
    check(r.get("selection_status") in ("unsupported", "no_candidate"),
          "拒绝题：不给推荐", r.get("selection_status"))
    check(bool(r.get("unsupported_reason")), "拒绝题：unsupported_reason 非空")
    check(r.get("candidates") == [], "拒绝题：候选为空")


def main():
    M.load_knowledge_cards()
    M.load_catalog()
    M.load_bulk10_runs()
    test_tools_list()
    test_contract_shape()
    if "--offline" not in sys.argv:
        test_live()
    print("\n" + ("全部通过" if not FAIL else f"失败 {len(FAIL)} 项：" + "; ".join(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
