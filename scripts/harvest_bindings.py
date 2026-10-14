#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 Cromwell 的成功运行记录生成运行绑定表（bindings/*.json）。只读，不提交任何任务。

绑定表回答一个问题：**哪个流程、在哪个队列上、用哪一整套输入参数，确实跑通过。**
口径（2026-10 起）：只认 Cromwell 上 status=Succeeded、且输入里引用了真实队列数据
（/hpcdisk1/cbb_group/data、/cbb-data 下带 HRA 队列号的路径）的运行。测试数据、
跨队列混拼的运行、知识卡片必填参数不齐的运行都不收，原因写进 bindings/index.json。

每个「流程 × 队列」只留一条绑定：优先平台（cncb-web-server-agent）提交的，再取最近一次。
参数取 submittedFiles.inputs——平台当时真正发出去的那份，不含 WDL 默认值。

重新生成会保留人工字段：每个流程的 default_study（人工改过的）、每条绑定的
disabled（人工停用，写明 reason）与 verification（scripts/verify_bindings.py 写入的复跑
结果，源运行没变时才保留）。

用法：
    python scripts/harvest_bindings.py                       # 默认 Cromwell 地址
    python scripts/harvest_bindings.py --cromwell http://host:8088 --out bindings
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import run_bindings as rb  # noqa: E402  (WORKFLOW_TOOLS / 路径口径与运行时共用一份)

DEFAULT_CROMWELL = os.environ.get("CROMWELL_URL", "http://192.168.121.231:8088")
PLATFORM_MARK = "cncb-web-server-agent"          # 平台提交的 options 里带这个目录
_HRA = re.compile(r"HRA\d{6}")


def _get(url, timeout=120):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def list_succeeded(base):
    out, page = [], 1
    while True:
        q = urllib.parse.urlencode({"status": "Succeeded", "pageSize": 500, "page": page})
        d = _get(f"{base}/api/workflows/v1/query?{q}")
        out.extend(d.get("results") or [])
        if len(out) >= int(d.get("totalResultsCount") or 0) or not d.get("results"):
            return out
        page += 1


def fetch_meta(base, wid, cache_dir):
    """一次运行的元数据（含 submittedFiles）。成功运行的元数据不会再变，缓存即可。"""
    path = os.path.join(cache_dir, f"{wid}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    keys = ["workflowName", "status", "submission", "start", "end", "submittedFiles"]
    q = "&".join(f"includeKey={k}" for k in keys) + "&expandSubWorkflows=false"
    d = _get(f"{base}/api/workflows/v1/{wid}/metadata?{q}")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    return d


def submitted_inputs(meta):
    """submittedFiles.inputs → 去掉 `<WorkflowName>.` 前缀的参数表。"""
    raw = (meta.get("submittedFiles") or {}).get("inputs") or "{}"
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except ValueError:
        return None
    name = meta.get("workflowName") or ""
    out = {}
    for k, v in data.items():
        out[k[len(name) + 1:] if k.startswith(name + ".") else k] = v
    return out


def _strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)


def classify(gid, meta):
    """一条成功运行 → (binding 草稿, None) 或 (None, 不收的原因)。"""
    inputs = submitted_inputs(meta)
    if inputs is None:
        return None, "submittedFiles.inputs 无法解析"
    card = rb.card(gid)
    if not card:
        return None, f"知识卡片里没有 {gid}"
    # 资源类参数（镜像、CPU、内存、磁盘）由平台按自己的流程配置填，其余全部绑定——
    # 包括卡片里没有的分析参数：cox_model 在 HRA000073 等 4 个队列上必须带
    # native_status_source_col，只绑卡片参数的话那 4 条绑定就是坏的。
    params = {k: v for k, v in inputs.items() if not rb.is_platform_input(k)}
    platform = {k: v for k, v in inputs.items() if rb.is_platform_input(k)}

    if any(v is True for k, v in params.items() if "smoke_test" in k.lower()):
        # 单细胞那几条的 9 月记录是冒烟测试（每类细胞抽 150 个），只证明流程能起来，
        # 不证明整套数据跑得完；按它绑定，用户拿到的是抽样结果
        return None, "冒烟测试运行（smoke_test=true，抽样），不代表完整分析能跑通"
    data_paths = [s for s in _strings(params) if rb.is_data_path(s)]
    if not data_paths:
        return None, "输入里没有真实队列数据路径（测试数据或参考文件）"
    studies = sorted({m for p in data_paths for m in _HRA.findall(p)})
    if not studies:
        return None, "数据路径里解析不出 HRA 队列号"
    if len(studies) > 1:
        return None, f"一次运行混用了多个队列：{'/'.join(studies)}"
    missing = [i["name"] for i in card.get("inputs") or []
               if i.get("required") and i["name"] not in params
               and not rb.is_reference_resource(gid, i["name"])]
    if missing:
        return None, f"缺知识卡片必填参数：{','.join(missing)}"

    sf = meta.get("submittedFiles") or {}
    wdl = sf.get("workflow") or ""
    options = sf.get("options") or ""
    return {
        "study": studies[0],
        "execution_params": params,
        "platform_inputs": platform,
        "source_run": {
            "cromwell_id": meta.get("id"),
            "workflow_name": meta.get("workflowName"),
            "submitted_at": meta.get("submission"),
            "ended_at": meta.get("end"),
            "via": "platform" if PLATFORM_MARK in options else "direct",
            "wdl_sha256": hashlib.sha256(wdl.encode("utf-8")).hexdigest() if wdl else None,
            "wdl_has_imports": bool(re.search(r"^\s*import\s+[\"']", wdl, re.M)),
        },
    }, None


def _rank(b):
    """同一「流程 × 队列」多条成功：平台提交的优先，其次最近一次。"""
    s = b["source_run"]
    return (s["via"] == "platform", s.get("submitted_at") or "")


def _load_existing(out_dir):
    old = {}
    for gid in rb.all_tool_ids():
        path = os.path.join(out_dir, f"{gid}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                old[gid] = json.load(f)
    return old


def harvest(base, out_dir, cache_dir, workers=6):
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    runs = [r for r in list_succeeded(base) if r.get("name") in rb.WORKFLOW_TOOLS]
    with cf.ThreadPoolExecutor(workers) as ex:
        metas = list(ex.map(lambda r: fetch_meta(base, r["id"], cache_dir), runs))

    grouped, skipped = {}, []
    for meta in metas:
        gid = rb.WORKFLOW_TOOLS[meta["workflowName"]]
        b, why = classify(gid, meta)
        if why:
            skipped.append({"tool_id": gid, "cromwell_id": meta.get("id"),
                            "workflow_name": meta.get("workflowName"),
                            "submitted_at": meta.get("submission"), "reason": why})
            continue
        grouped.setdefault(gid, {}).setdefault(b["study"], []).append(b)

    # 每个流程「平台最近一次成功提交」用的 WDL：绑定的源运行用的不是这一版时要标出来，
    # 平台按现行 WDL 提交，参数名对不上会在执行端才暴露。
    latest_wdl = {}
    for meta in sorted(metas, key=lambda m: m.get("submission") or ""):
        sf = meta.get("submittedFiles") or {}
        if PLATFORM_MARK in (sf.get("options") or "") and sf.get("workflow"):
            latest_wdl[rb.WORKFLOW_TOOLS[meta["workflowName"]]] = hashlib.sha256(
                sf["workflow"].encode("utf-8")).hexdigest()

    old = _load_existing(out_dir)
    studies = rb.studies()
    index_tools = {}
    for gid in rb.all_tool_ids():
        per_study = grouped.get(gid) or {}
        prev = old.get(gid) or {}
        prev_by_study = {b["study"]: b for b in prev.get("bindings") or []}
        bindings = []
        for study in sorted(per_study):
            cands = sorted(per_study[study], key=_rank, reverse=True)
            b = cands[0]
            if gid in latest_wdl:
                b["source_run"]["wdl_is_latest"] = b["source_run"]["wdl_sha256"] == latest_wdl[gid]
            b["evidence"] = {"succeeded_runs": len(cands),
                             "workflow_names": sorted({c["source_run"]["workflow_name"] for c in cands})}
            b["tumor_type"] = (studies.get(study) or {}).get("tumor_type")
            p = prev_by_study.get(study) or {}
            same_source = (p.get("source_run") or {}).get("cromwell_id") == b["source_run"]["cromwell_id"]
            b["verification"] = p.get("verification") if same_source else None
            if p.get("disabled"):           # 人工停用与源运行无关，换了源运行也保留
                b["disabled"] = p["disabled"]
            bindings.append(b)
        default = prev.get("default_study")
        if default not in {b["study"] for b in bindings}:
            # 证据最多的队列作默认；平局取最近跑通的那个
            default = max(bindings, key=lambda b: (b["evidence"]["succeeded_runs"],
                                                  b["source_run"]["submitted_at"] or ""),
                          default={}).get("study")
        doc = {
            "schema_version": rb.SCHEMA_VERSION,
            "tool_id": gid,
            "card_id": rb.card(gid).get("meta_id"),
            "name_cn": rb.name_cn(gid),
            "workflow_names": sorted(n for n, t in rb.WORKFLOW_TOOLS.items() if t == gid),
            "default_study": default,
            "bindings": bindings,
        }
        path = os.path.join(out_dir, f"{gid}.json")
        if bindings:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
                f.write("\n")
        elif os.path.exists(path):
            os.remove(path)
        index_tools[gid] = {"bound_studies": [b["study"] for b in bindings],
                            "default_study": default}

    unbound = {}
    for gid, info in index_tools.items():
        if info["bound_studies"]:
            continue
        reasons = sorted({s["reason"] for s in skipped if s["tool_id"] == gid})
        unbound[gid] = ("Cromwell 上没有该流程的成功运行" if not reasons
                        else "成功运行均不合格：" + "；".join(reasons))
    index = {
        "schema_version": rb.SCHEMA_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": {"cromwell": base, "succeeded_runs_considered": len(metas)},
        "bound_tools": {g: i for g, i in index_tools.items() if i["bound_studies"]},
        "unbound_tools": unbound,
        "skipped_runs": sorted(skipped, key=lambda s: (s["tool_id"], s["submitted_at"] or "")),
    }
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return index


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cromwell", default=DEFAULT_CROMWELL)
    ap.add_argument("--out", default=os.path.join(REPO, "bindings"))
    ap.add_argument("--cache", default=os.path.join(REPO, "bindings", ".cache", "metadata"))
    a = ap.parse_args()
    idx = harvest(a.cromwell.rstrip("/"), a.out, a.cache)
    n = sum(len(i["bound_studies"]) for i in idx["bound_tools"].values())
    print(f"绑定：{len(idx['bound_tools'])} 个流程 / {n} 个「流程 × 队列」组合；"
          f"未绑定 {len(idx['unbound_tools'])} 个流程；不收的成功运行 {len(idx['skipped_runs'])} 条")


if __name__ == "__main__":
    main()
