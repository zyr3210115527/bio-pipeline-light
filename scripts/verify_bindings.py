#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把运行绑定逐条重新提交到 Cromwell，确认它们今天还跑得通。**会占用集群资源。**

每条绑定按它的源运行原样重放：同一份 WDL（源运行的 submittedFiles.workflow）、
同一套输入（execution_params + platform_inputs）。复跑不读写调用缓存（否则等于没跑），
输出留在 Cromwell 执行目录里，不写进平台用户的结果目录；每个任务都打上
bpl-verify / bpl-tool / bpl-study 标签，在 Cromwell 上能按批次查到、必要时批量中止。

结果写回两处：
  bindings/<tool>.json 每条绑定的 verification 字段（passed / failed + 复跑的 workflow id）
  bindings/verification/<batch>.json 这一批的逐条明细（含失败原因）
中途断了可以 `--resume <batch>` 接着跑，已提交的不会重复提交。

用法：
    python scripts/verify_bindings.py --dry-run                 # 只列计划，不提交
    python scripts/verify_bindings.py                           # 全部绑定，最多 8 个同时在跑
    python scripts/verify_bindings.py --tool umap --study HRA003107
    python scripts/verify_bindings.py --resume 20261014-1530
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import run_bindings as rb  # noqa: E402
from harvest_bindings import DEFAULT_CROMWELL, fetch_meta  # noqa: E402

TERMINAL = {"Succeeded", "Failed", "Aborted"}
OPTIONS = {
    "read_from_cache": False,          # 命中缓存等于没跑
    "write_to_cache": False,
    "workflow_failure_mode": "ContinueWhilePossible",
    "delete_intermediate_output_files": True,
    "memory_retry_multiplier": 2,
    "default_runtime_attributes": {"slurm_partition": "debug"},   # 与平台提交一致
}


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _slug(s):
    return re.sub(r"[^a-z0-9-]+", "-", str(s).lower()).strip("-")[:63]


def _req(url, data=None, headers=None, timeout=120):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _multipart(fields):
    boundary = uuid.uuid4().hex
    body = b""
    for name, content in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                 f"filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
        body += content.encode("utf-8") + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _duration(b):
    s = b["source_run"]
    try:
        a = dt.datetime.fromisoformat(s["submitted_at"].replace("Z", "+00:00"))
        z = dt.datetime.fromisoformat(s["ended_at"].replace("Z", "+00:00"))
        return (z - a).total_seconds()
    except Exception:
        return 3600.0


def plan(base, cache, tools=None, studies=None, only_unverified=False, max_minutes=None):
    """要复跑的绑定，按源运行耗时从短到长排（先拿到快的反馈）。

    max_minutes：只取源运行耗时不超过这么多分钟的（长任务单独排期，别一口气占满集群）。"""
    jobs = []
    for gid, doc in sorted(rb.registry().items()):
        if tools and gid not in tools:
            continue
        for b in doc["bindings"]:
            if studies and b["study"] not in studies:
                continue
            if only_unverified and (b.get("verification") or {}).get("status") == "passed":
                continue
            if max_minutes is not None and _duration(b) > max_minutes * 60:
                continue
            meta = fetch_meta(base, b["source_run"]["cromwell_id"], cache)
            wdl = (meta.get("submittedFiles") or {}).get("workflow") or ""
            m = re.search(r"^\s*workflow\s+(\w+)", wdl, re.M)
            job = {"key": f"{gid}|{b['study']}", "tool_id": gid, "study": b["study"],
                   "source_id": b["source_run"]["cromwell_id"],
                   "expected_s": _duration(b), "wdl": wdl,
                   "workflow_name": m.group(1) if m else None, "skip": None}
            if not wdl:
                job["skip"] = "源运行没有保存 WDL 原文"
            elif re.search(r"^\s*import\s+[\"']", wdl, re.M):
                job["skip"] = "WDL 有 import，Cromwell 没存依赖包，无法原样重放"
            elif not job["workflow_name"]:
                job["skip"] = "WDL 里解析不出 workflow 名"
            inputs = dict(b["execution_params"], **(b.get("platform_inputs") or {}))
            job["inputs"] = {f"{job['workflow_name']}.{k}": v for k, v in inputs.items()}
            jobs.append(job)
    return sorted(jobs, key=lambda j: j["expected_s"])


def submit(base, job, batch):
    labels = {"bpl-verify": _slug(batch), "bpl-tool": _slug(job["tool_id"]),
              "bpl-study": _slug(job["study"])}
    body, ctype = _multipart({
        "workflowSource": job["wdl"],
        "workflowInputs": json.dumps(job["inputs"], ensure_ascii=False),
        "workflowOptions": json.dumps(OPTIONS),
        "labels": json.dumps(labels),
    })
    r = _req(f"{base}/api/workflows/v1", data=body, headers={"Content-Type": ctype})
    return r["id"]


def status(base, wid):
    return _req(f"{base}/api/workflows/v1/{wid}/status").get("status")


def failure_text(base, wid):
    try:
        d = _req(f"{base}/api/workflows/v1/{wid}/metadata?includeKey=failures&expandSubWorkflows=false")
    except Exception as e:
        return f"(读失败原因出错：{e})"
    msgs = []

    def walk(fs):
        for f in fs or []:
            if f.get("message"):
                msgs.append(f["message"].strip().replace("\n", " ")[:400])
            walk(f.get("causedBy"))
    walk(d.get("failures"))
    return " // ".join(msgs[-3:]) if msgs else ""


def write_back(results, batch):
    """把 passed/failed 写回 bindings/<tool>.json；其余状态（跳过、未完成）不动原记录。"""
    by_tool = {}
    for r in results.values():
        if r.get("status") in ("passed", "failed"):
            by_tool.setdefault(r["tool_id"], {})[r["study"]] = r
    for gid, per in by_tool.items():
        path = os.path.join(rb.BINDINGS_DIR, f"{gid}.json")
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        for b in doc["bindings"]:
            r = per.get(b["study"])
            if r and r.get("source_id") == b["source_run"]["cromwell_id"]:
                b["verification"] = {k: r.get(k) for k in
                                     ("status", "workflow_id", "batch", "submitted_at",
                                      "ended_at", "duration_s", "failure") if r.get(k) is not None}
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")


def run(base, jobs, batch, max_running, poll, state_path, report_path):
    state = {}
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)

    def save():
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        rows = sorted(state.values(), key=lambda r: (r["tool_id"], r["study"]))
        with open(report_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"batch": batch, "cromwell": base, "updated_at": _now(),
                       "summary": {s: sum(1 for r in rows if r["status"] == s)
                                   for s in sorted({r["status"] for r in rows})},
                       "results": rows}, f, ensure_ascii=False, indent=2)
            f.write("\n")

    for j in jobs:
        if j["key"] not in state:
            state[j["key"]] = {"tool_id": j["tool_id"], "study": j["study"], "batch": batch,
                               "source_id": j["source_id"],
                               "status": "skipped" if j["skip"] else "pending",
                               "failure": j["skip"]}
    save()
    pending = [j for j in jobs if state[j["key"]]["status"] == "pending"]
    while True:
        running = [k for k, r in state.items() if r["status"] == "running"]
        for k in running:
            r = state[k]
            try:
                st = status(base, r["workflow_id"])
            except Exception as e:
                print(f"[warn] 查状态失败 {k}: {e}", flush=True)
                continue
            if st in TERMINAL:
                r["ended_at"] = _now()
                a = dt.datetime.fromisoformat(r["submitted_at"])
                r["duration_s"] = round((dt.datetime.fromisoformat(r["ended_at"]) - a).total_seconds())
                r["cromwell_status"] = st
                r["status"] = "passed" if st == "Succeeded" else "failed"
                if st != "Succeeded":
                    r["failure"] = failure_text(base, r["workflow_id"])
                print(f"[{r['status']}] {k} {r['workflow_id']} {r['duration_s']}s "
                      f"{r.get('failure') or ''}"[:300], flush=True)
                write_back({k: r}, batch)
        running = [k for k, r in state.items() if r["status"] == "running"]
        while pending and len(running) < max_running:
            j = pending.pop(0)
            r = state[j["key"]]
            try:
                r["workflow_id"] = submit(base, j, batch)
                r["submitted_at"] = _now()
                r["status"] = "running"
                running.append(j["key"])
                print(f"[submit] {j['key']} → {r['workflow_id']}", flush=True)
            except urllib.error.HTTPError as e:
                r["status"] = "failed"
                r["failure"] = f"提交被拒：HTTP {e.code} {e.read().decode('utf-8', 'replace')[:300]}"
                print(f"[failed] {j['key']} {r['failure']}", flush=True)
                write_back({j["key"]: dict(r, submitted_at=_now())}, batch)
        save()
        if not pending and not running:
            break
        time.sleep(poll)
    save()
    return state


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cromwell", default=DEFAULT_CROMWELL)
    ap.add_argument("--tool", action="append", help="只复跑这些流程（可重复）")
    ap.add_argument("--study", action="append", help="只复跑这些队列（可重复）")
    ap.add_argument("--only-unverified", action="store_true", help="跳过已经 passed 的绑定")
    ap.add_argument("--max-minutes", type=float, help="只复跑源运行耗时不超过这么多分钟的绑定")
    ap.add_argument("--max-running", type=int, default=8)
    ap.add_argument("--poll", type=int, default=60, help="查状态间隔（秒）")
    ap.add_argument("--resume", help="接着跑某个批次（批次号见 bindings/verification/）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    base = a.cromwell.rstrip("/")
    cache = os.path.join(REPO, "bindings", ".cache", "metadata")
    os.makedirs(cache, exist_ok=True)
    batch = a.resume or dt.datetime.now().strftime("%Y%m%d-%H%M")
    jobs = plan(base, cache, set(a.tool or []), {s.upper() for s in a.study or []},
                a.only_unverified, a.max_minutes)
    total_h = sum(j["expected_s"] for j in jobs if not j["skip"]) / 3600
    print(f"批次 {batch}：{len(jobs)} 条绑定，跳过 {sum(1 for j in jobs if j['skip'])} 条；"
          f"源运行耗时合计约 {total_h:.1f} 小时（最多 {a.max_running} 个同时在跑）")
    for j in jobs:
        tag = f"  跳过：{j['skip']}" if j["skip"] else ""
        print(f"  {j['key']:45} 源运行 {j['source_id']}  约 {j['expected_s'] / 60:.0f} 分钟{tag}")
    if a.dry_run:
        return
    state_path = os.path.join(REPO, "bindings", ".cache", f"verify-{batch}.json")
    report_path = os.path.join(REPO, "bindings", "verification", f"{batch}.json")
    state = run(base, jobs, batch, a.max_running, a.poll, state_path, report_path)
    summary = {}
    for r in state.values():
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    print(f"完成：{summary}；明细见 {os.path.relpath(report_path, REPO)}")


if __name__ == "__main__":
    main()
