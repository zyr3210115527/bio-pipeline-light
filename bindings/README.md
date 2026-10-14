# 运行绑定（run bindings）

这个目录回答一个问题：**哪个流程、在哪个队列上、用哪一整套参数，确实跑通过。**
`route_pipeline_request` 给出的执行合同只从这里取参数；没有绑定的流程/队列直接回
「暂无已验证可运行的数据」，不再现场查图拼路径。

## 文件

| 文件 | 内容 | 谁写 |
|---|---|---|
| `<tool_id>.json` | 一个流程的全部绑定（每个队列一条） | `scripts/harvest_bindings.py` 生成，可人工改 `default_study` / `disabled` |
| `index.json` | 汇总：已绑定流程、未绑定流程及原因、没收进来的成功运行及原因 | 生成 |
| `studies.json` | 队列 → 癌种与常见叫法（把「用食管鳞癌数据」落到队列上） | 人工维护 |
| `verification/<批次>.json` | 复跑明细 | `scripts/verify_bindings.py` 生成 |
| `.cache/` | Cromwell 元数据缓存、复跑断点（不入库） | 脚本 |

## 一条绑定长什么样

```json
{
  "study": "HRA000873",
  "execution_params": { "...": "平台当时提交的参数，去掉资源类参数，原样下发" },
  "platform_inputs": { "docker_image": "...", "cpu": 16, "memory": "32 GB" },
  "source_run": {
    "cromwell_id": "57c4a71b-…",           // 跑通的那次运行，在 Cromwell 上可查
    "workflow_name": "MantaStructuralVariants",
    "via": "platform",                    // platform = 平台提交；direct = 开发者直接提交
    "wdl_is_latest": true                 // 是否与平台最近一次提交用的 WDL 相同
  },
  "evidence": { "succeeded_runs": 3 },    // 这个「流程 × 队列」一共成功过几次
  "verification": { "status": "passed", "workflow_id": "…", "batch": "…" },
  "disabled": { "reason": "…" }           // 可选：人工停用
}
```

`execution_params` 包含卡片参数之外的分析参数（例如 cox_model 在 HRA000073 等队列上的
`native_status_source_col`），这些是跑通的一部分，一并绑定。只有镜像、CPU、内存、磁盘这类资源参数放在
`platform_inputs`，由平台按自己的流程配置填。

## 收录口径

只收 Cromwell 上 `Succeeded`、且输入引用了真实队列数据（`/hpcdisk1/cbb_group/data`、`/cbb-data` 下带
HRA 队列号的路径）的运行。以下不收，原因写进 `index.json` 的 `skipped_runs`：

- 测试数据 / 只有参考文件的运行
- 冒烟测试（`smoke_test=true`，抽样跑的，不代表完整分析能跑通）
- 一次运行混用多个队列
- 缺知识卡片必填参数

同一「流程 × 队列」有多次成功时，优先平台提交的，再取最近一次。

## 日常操作

```bash
# 重新生成（只读 Cromwell；保留人工改过的 default_study / disabled，以及源运行没变的复跑结果）
python scripts/harvest_bindings.py

# 复跑验证（会占用集群；先 --dry-run 看计划）
python scripts/verify_bindings.py --dry-run
python scripts/verify_bindings.py --max-minutes 120 --only-unverified
python scripts/verify_bindings.py --resume <批次号>          # 中断后接着跑

# 停用一条绑定：在对应 <tool_id>.json 的那条绑定里加
#   "disabled": {"reason": "为什么停用", "since": "YYYY-MM-DD"}
# 删掉这个字段即解禁。重新生成不会覆盖它。

# 自检
python tests/test_run_bindings.py
```

新流程接入时，在 `run_bindings.py` 的 `WORKFLOW_TOOLS` 里加一行「Cromwell workflowName → tool_id」，
再跑一次生成脚本。平台界面上的展示名（用户会照着说的那种）加到 `DISPLAY_ALIASES`。
