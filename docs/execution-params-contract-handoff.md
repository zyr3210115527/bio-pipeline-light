# `tool_validate_execution_chain` 执行契约对齐 —— 交接说明

> 写给接手改 light 的会话。这份文档只描述**需求**和**现状**，代码由你写。
> 现状部分的每一条都有可复现的探针（见文末「复现」），不要只读代码就下结论。
>
> 上游背景：重版 `bio-pipeline-kg-matcher` 0823 完成了槽表按 WDL 重建（commit `58f6be6`），
> 契约规格见该仓库 `docs/execution_params_spec.md` 与
> `docs/mcp_delivery/MCP_AGENT_INTEGRATION_ZH.md` §3.1。light 这边的 Knowledge Card
> **已经是对的**，问题全部在读卡片的代码里。

---

## ✅ 落地状态（0823 已实施）

§6 的四条探针全部复现属实，已按 §4 的顺序改完。验收：A/B/C/D 四例的 `execution_params`
现在都含各自卡片声明的全部必需 File 输入、都不含那 5 个参考资源，C 例 `submittable: true`。
24 例回归 24/24（mean 7.8s→7.5s、p90 18.5s→17.6s、>30s 为 0）。

| 项 | 状态 |
|---|---|
| P0-1 `Array[File]+` 三处漏判 | 已修：抽 `_base_type`/`_is_file_type`/`_is_array_type`，三处改为统一调用 |
| P0-2 已绑定的 `File?` 被丢 | 已修：判据改为「必需的 **或** 用户绑了的」 |
| P0-3 参考资源被当用户数据 | 已修：`REFERENCE_RESOURCES` 显式二元组白名单，正好 5 条，未用关键字启发式 |
| P1-1 多步键加前缀 | 已改：键一律为卡片参数名；新增 `execution_params_by_step`。**确认过全仓库无消费方读取该键**（只判 `submittable`／`missing` 空否／路径是否 `/` 开头），且 README/SKILL/手册三处本来就写的是「输入名→路径」 |
| P1-2 `missing` 元素类型 | 已改：`list[dict]` `{param, tool_id, step, reason}`，`schema_version` 升 v1.2 |
| P1-3 `SKILL.md` 63 / 80-81 行 | 已修。另发现 light 自己发的 `tool_catalog.csv` 里 gatk 仍有 `single`，一并删掉，否则手册与随包 CSV 互相矛盾 |
| P1-3 `io_slot.csv` 同步 | **已完成（0823 补做）**。此前判断"做不了"是因为找错了目录：`~/bio-pipeline-kg-matcher` 是**未版本化的 pre-0823 副本**（无 `.git`、无 `58f6be6`），其 `io_slot.csv` 与 light 快照逐字节相同。真正的重版仓库在 **`/tmp/kgm`**（`git log -1` → `58f6be6`）。已从那里同步，现 246 行 19 列、与上游 md5 一致（`562c3aaa…`）。`SKILL.md` §3 的 pre-0823 标注已撤销并改为警告"别把 `~/bio-pipeline-kg-matcher` 当真值"。 |
| P2 补测试 | 已加 `integration_test.py` §4b，9 条断言。已拿改动前的代码验证过**每条 P0 断言都会失败**（能过的测试不等于能失败的测试） |

两点顺带发现，与本任务无关但都影响判断：

1. **`integration_test.py` 之前崩在第 109 行**（`13_survival_days` 在 0821 交付里退回 STRING，
   `str > int` 直接 TypeError），**第 4 段之后的十几条断言从来没跑到过**。已加 `float()` 兜底，
   断言意图不变。修完套件能跑到底：**28 PASS / 7 FAIL**，7 条全是 0821 数据问题
   （字符串型数值、format 大小写孤儿、`00_sample_accession` 与边错位 349 个），
   其中 5 条是这次才第一次被报出来。这些不在本文档范围内，但得有人去看。
2. 扁平 `execution_params` 在多步链下**无法**同时满足「键=卡片参数名」和「不丢参数」——
   `trim_galore` 和 `star` 都有 `read1`。现在的处理是：冲突键从扁平视图剔除并列进新增的
   `execution_params_ambiguous`，逐步值在 `by_step` 里完整保留。**宁可缺，不给静默覆盖的错路径**，
   与本文档反对的那个签名同源。手册已写明「扁平视图里没有的参数不等于缺，去 by_step 取」。


---

## 0. 需求（一句话）

`tool_validate_execution_chain` 的 `submittable=true` 必须真的意味着"这份 `execution_params`
投给执行端能跑"。现在它做不到——**在若干条正常路径上，它对着一份正确的绑定返回
`execution_params: {}` + `execution_params_missing: []` + `submittable: true`。**

这个签名（"零个参数、且一个都不缺"）是重版这一整轮在清的东西。消费方按
`not execution_params_missing` 判可提交，就会把一个参数根本没解析出来的链当成能跑的。
它不报错、字段齐全、值都合理，只是错的——**错得像对**。

需求的验收标准就是这一条：**没有任何一条输入能让回包同时满足
`submittable == true` 且 `execution_params` 漏掉了卡片声明的必需 File 输入。**

---

## 1. 现状：四条会给出「错得像对」回包的路径

探针把 `neo4j_q` 全部 stub 成放行，隔离出纯逻辑问题（图连不连得上与这几条无关）。

### P0-1　`Array[File]+` 在三处类型判断里都匹配不上 —— 最严重

卡片里的类型字符串带 `+` 后缀（WDL 的非空数组）：

```
fastqc   fastqs    Array[File]+   required=true
multiqc  qc_files  Array[File]+   required=true
```

代码三处都在拿字符串**精确相等**去判：

| 行 | 代码 | 漏掉 `Array[File]+` 的后果 |
|---|---|---|
| `mcp_light_server.py:617` | `if i.get("type") in ("File", "Array[File]"):` | 绑定结构不校验，传个字符串也不报 |
| `mcp_light_server.py:640` | `if i.get("type") not in ("File", "Array[File]") or ...: continue` | 数据探查整个跳过，不查图内候选 |
| `mcp_light_server.py:697-698` | `file_inputs = [... if i.get("type") in ("File", "Array[File]") ...]` | **不进 `execution_params`，也不进 `missing`** |

`fastqc` 只有 `fastqs` 一个输入。三处全漏，等于这张卡在执行参数解析里**完全不存在**：

```
--- A. fastqc 单步（唯一的必需输入已正确绑定）
  errors          : []
  execution_params: {}          ← 空
  params_missing  : []          ← 也空
  submittable     : True        ← 却说可提交
```

`multiqc` 同理。这是最典型的那个签名，且只需一步链就能触发。

### P0-2　已绑定的可选 `File?` 输入被丢掉

`file_inputs` 的过滤条件里有 `and i.get("required", True)`——只收必需输入。于是**用户明确
绑了的可选文件不会出现在 `execution_params` 里**：

```
star           read2   File?   required=false
trim_galore    read2   File?   required=false
```

```
--- B. trim_galore 单步（read1 + read2 都正确绑定）
  execution_params: {"read1": "/d/a_R1.fq.gz"}    ← read2 没了
  params_missing  : []
  submittable     : True
```

后果比 P0-1 更隐蔽：执行端拿到只有 `read1` 的参数，`trim_galore` / STAR 里
`is_paired = defined(read2)` 变 false，**双端数据静默按单端跑完，一路绿灯，结果是错的**。
重版把 `star::clean_fastq_read` / `trim_galore::raw_fastq_read` 拆成 r1/r2 两个槽，就是为了
堵这个口；light 这边卡片是对的，是消费代码把它丢了。

判据应该是「必需的 **或** 用户绑了的」，不是「必需的」。

### P0-3　参考资源被当成用户数据要路径

卡片里这 5 个 File 参数是**有卡片默认值的参考资源**（师兄规则 4：参考/索引资源带默认值，
既不映射进 `execution_params`，也不报缺）：

```
star            rrna_star_index     File   STAR index directory
star            genome_star_index   File   STAR index directory
rsem            rsem_index          File   RSEM index directory
featurecounts   gtf_file            File   GTF
gatk            interval_list       File   GATK interval_list
```

现在它们照常参与必填校验和 `execution_params` 解析：

```
--- C. star 单步（只绑 read1/read2）
  errors        : ["star_rrna_and_genome_alignment 缺必填输入: ['rrna_star_index','genome_star_index']"]
  params_missing: ['rrna_star_index', 'genome_star_index']
  submittable   : False
```

一条完全正常的 RNA-seq 比对链被判成不可提交。反过来，用户要是随手塞个路径进去（案例 D），
它们又会混进 `execution_params` 被投给执行端，覆盖掉容器内的正确默认值。

**⚠️ 这条有个坑，必须用显式白名单，不要写关键字启发式。**
"名字里带 index / reference / genome / gtf 就是参考资源"这条规则在这个目录里是错的：

```
bcftools_somatic_postprocess   filtered_vcf_index   File   TBI   required=true
```

它名字里带 `index`，但它是**数据文件的伴随索引**，没有卡片默认值，缺了
`bcftools` 的 `ln -sf` 读不到 `.tbi`，执行直接失败。重版就是踩了这个坑——
`_role_for_input` 按 "index" 关键字把它判成参考资源，于是既不映射也不报缺，
0823 才修掉（见重版 `docs/execution_params_spec.md` 倒数第二条）。

所以请写成一张 **`(card_id, param_name)` 二元组的显式白名单，正好上面 5 条**，
新工具接入时手动加。不要按名字猜。

顺带说明：`bcftools` / `bwa` 的 `reference_fasta` 是 `String` 类型，本来就不进 `file_inputs`，
不用管；只有上面 5 个是 `File` 类型的参考资源。

### P0 合并效果

```
--- D. trim_galore→fastqc→star 三步（8 个绑定全部正确）
  errors          : []
  execution_params: {"trim_galore.read1": "/d/a_R1.fq.gz",
                     "star.read1": "/d/a_R1_val_1.fq.gz",
                     "star.rrna_star_index": "/ref/rrna",
                     "star.genome_star_index": "/ref/genome"}
  params_missing  : []
  submittable     : True
```

8 个正确绑定进去，4 个路径出来。丢了 `trim_galore.read2`（P0-2）、`star.read2`（P0-2）、
`fastqc.fastqs` 整张卡（P0-1）；同时混进了两个不该出现的 STAR 索引（P0-3）。
回包 `submittable: true`，看不出任何异常。

---

## 2. P1：契约形状与重版不一致（要改，但要先确认下游）

### P1-1　多步时 key 被加了前缀

`mcp_light_server.py:702`：

```python
key = f"{given}.{name}" if len(steps) > 1 else name
```

一步链给 `read1`，多步链给 `trim_galore.read1`。**同一个字段在不同请求下是两种键空间**，
消费方没法统一处理；而重版规格第 1 条明确要求键必须与 `knowledge_card.yaml`
`interface.params[].name` **完全一致**。

建议改成按步骤分组，键本身保持卡片原名：

```json
"execution_params_by_step": [
  {"step": 0, "tool_id": "trim_galore", "params": {"read1": "...", "read2": "..."}},
  {"step": 1, "tool_id": "fastqc",      "params": {"fastqs": ["...", "..."]}}
]
```

**这是破坏性变更**，改之前需要确认前端/调用方有没有依赖当前的点号拼接键。
（`docs/frontend-mcp-connection.md` 里如果有约定，以那份为准。）

### P1-2　`execution_params_missing` 的元素类型与重版不同构

- light（`mcp_light_server.py:707`）：`list[str]`，只有一个键名
- 重版：`list[dict]`，每条是 `{param, slot, role, reason}`

`reason` 是有处置含义的，**两个取值不能合并**：

| `reason` | 含义 | 谁来修 |
|---|---|---|
| `no_confirmed_path` | 绑定正确，但图里没有该资产的确认路径 | 数据侧补 `file_path` |
| `slot_not_bound` | 该输入槽没有 `builder_param`，映射不到 WDL 参数（`param` 为 `null`） | 目录侧补 `io_slot.csv` |

light 现在只有一个纯字符串，下游分不清该找谁。建议与重版同构，并把
`schema_version` 从 `tool-chain-validation/v1.1` 升到 `v1.2`。

注：light 走的是 Knowledge Card 而不是槽表，所以 `slot_not_bound` 在 light 侧未必用得上；
但字段形状建议保持一致，`reason` 至少给出 `no_confirmed_path`。

### P1-3　`skill/references/` 快照落后于重版，`SKILL.md` 有两处已失效

`skill/references/io_slot.csv` 是 0823 之前的快照：

| | light `skill/references/io_slot.csv` | 重版 `data/csv/catalog/io_slot.csv` |
|---|---|---|
| 行数 | 248 | 246 |
| 列 | 18 列 | 19 列（新增 `cardinality`） |

重版这一轮删了 23 个幽灵槽、拆了 7 个槽、改了 bcftools 的槽名方向、加了 `cardinality` 列。
**唯一读这个文件的是 `light_router.py`（`load_slots()`，第 26-47 行），而它是已下线的离线对照臂。**
所以这份漂移目前不影响生产路径——但它是作为"契约真值"随 skill 分发的，同步一下更稳妥，
或者在文件头加一行注明它只服务离线对照臂、真值在重版仓库。

`skill/SKILL.md` 有两处现在是错的：

- **第 80-81 行**："`gatk` has `single` (sorted_dedup_bam) and `paired` (four slots ...)"
  —— `single` 变体 0823 已删除。`GatkWesSomaticWorkflow` 是严格 tumor-normal Mutect2，
  四个 BAM/BAI 全必需，没有单样本入口，`tool_id.csv` 的 `input_variants_json` 已同步去掉 `single`。
  **现在只有 `paired`。**
- **第 63 行**："truth = bio-pipeline-kg-matcher `data/csv/catalog`; 0821 verified identical to graph"
  —— 目录 0823 已变更，这个日期戳失效了。

（第 81 行提到 `fastp` 有 single_end / paired_end 变体，这一条与 CSV 一致，但
`FastpPairedEndWorkflow` 的 `read2` 是非可选 `File` 且 task 无条件展开 `--in2`，
**实际跑不了单端**。这是重版侧的待办，light 这边先不用动，知道即可。）

**图谱不需要重载。** 重版这一轮改的是 `data/csv/catalog/`（执行侧契约，按设计就不进图），
以及 `tool_relationship.csv` 的两行。图里的 `next_tool` 边只带 `kind`、不带槽名，
`T007→T008`（samtools→gatk）经由四条 paired 行仍然连通。
light 的 stage 5 邻接查询不受影响。

---

## 3. P2：测试覆盖不到出问题的那条路

`integration_test.py` 对 `tool_validate_execution_chain` 只测了 **card-less 分支**
（`mcp_light_server.py:700`，`file_inputs = [k for k,v in bindings.items() if isinstance(v, dict)]`）。
那一支不看 `type` 字段，所以上面 P0-1/P0-2/P0-3 三条**在现有测试下全绿**。

请为每条 P0 补一个断言，并且断言要落在**"不能声称可提交"**上，而不只是字段值：

```python
# 反例锚点：绑定正确时，唯一的必需输入必须出现在 execution_params 里
r = tool_validate_execution_chain({"steps": [
    {"tool_id": "fastqc", "inputs": {"fastqs": {"file_name": "a.fq.gz", "file_path": "/d/a.fq.gz"}}}]})
assert r["execution_params"], (
    "fastqs 是 Array[File]+，被三处精确相等的类型判断漏掉了。回包会是 "
    "execution_params={} 且 missing=[]——零个参数且一个都不缺，消费方按 not missing "
    "判可提交，就把一个参数根本没解析出来的链当成能跑的。")
```

注释要写清**为什么**，不要只写断言——这几条都是"回包看着完全正常"的错，
后来人光看断言失败信息判断不出严重性。

---

## 4. 建议的落地顺序

1. **P0-1 + P0-3 一起做**（互不相干，都在类型/白名单判断上，一次改完一次验）。
   建议抽两个小工具函数供三处复用，避免以后再漏一处：

   ```python
   def _base_type(t: str) -> str:          # "Array[File]+" -> "File"，"File?" -> "File"
       t = (t or "").strip().rstrip("+?")
       return t.removeprefix("Array[").removesuffix("]") if t.startswith("Array[") else t

   def _is_file_type(t)  -> bool: return _base_type(t) == "File"
   def _is_array_type(t) -> bool: return (t or "").strip().rstrip("+?").startswith("Array[")
   ```

   `_is_array_type` 为真的参数，`execution_params` 里的值应当是**路径数组**
   （重版规格第 2 条；两个槽共用一个参数时是并集去重、保持顺序，不是覆盖）。
   **消费方不能假定 `execution_params` 的值一定是 `str`。**

2. **再做 P0-2**（判据从 `required` 改成 `required or 已绑定`），单独验，因为它会改变
   已有用例的 `execution_params` 内容。

3. **P2 补测试**，锁住上面三条。

4. **P1-1 / P1-2 单独一轮**，改契约形状，要先确认调用方，并升 `schema_version` 到
   `tool-chain-validation/v1.2`。

5. **P1-3 同步快照 + 修 `SKILL.md` 第 63、80-81 行**，随手可做。

---

## 5. 明确不要动的

- **`light_router.py`** —— 已下线的离线对照臂（v2.1）。生产路径不允许静默降级到规则规划，
  这是架构主张（推理必须来自调用方模型），不是遗留代码。别顺手"修好"它接回去。
- **`mcp_light_server.py:88-89` 的 `if kind == "atomic" and tid != "multiqc"`** ——
  `multiqc` 是终端工具、只做扇入不参与编排，这个排除是有意的。
- **`_SAFE_FILE` / `_SAFE_TOKEN` 白名单和 stage 5 那段注释**（`mcp_light_server.py:657-661`）
  —— 0820 修的 Cypher 注入。那段注释记录了漏掉校验时能绕开 `_assert_read_only`
  并把不存在的邻接伪造成 `passed=True`。任何改动都不要削弱它。
- **Knowledge Card 本身**（`skill/references/knowledge_cards_map.json`）——
  已核对与重版 0823 后的状态一致：bcftools 的 `filtered_vcf` + `filtered_vcf_index` 配对、
  star/trim_galore 的 read1/read2 拆分、star 的两个索引、`rsem_index`、
  fastqc/multiqc 的两个 `Array[File]+`，全部正确。**契约源不用改，改的是读它的代码。**

---

## 6. 复现

```bash
cat > /tmp/probe_light.py <<'PY'
import json, sys
sys.path.insert(0, "/Users/zhouyiran/bio-pipeline-light")
import mcp_light_server as L
L.neo4j_q = lambda stmts: [[[1]] for _ in stmts]   # 图查询全放行，隔离纯逻辑问题

def show(title, steps):
    r = L.tool_validate_execution_chain({"steps": steps})
    print("---", title)
    print("  errors          :", r["validation"]["errors"])
    print("  execution_params:", json.dumps(r["execution_params"], ensure_ascii=False))
    print("  params_missing  :", r["execution_params_missing"])
    print("  submittable     :", r["submittable"])

f = lambda p: {"file_name": p.rsplit("/", 1)[-1], "file_path": p}

show("A. fastqc 单步：Array[File]+", [
    {"tool_id": "fastqc", "inputs": {"fastqs": f("/d/a_R1.fq.gz")}}])
show("B. trim_galore 单步：绑了 read2（File?）", [
    {"tool_id": "trim_galore", "inputs": {"read1": f("/d/a_R1.fq.gz"), "read2": f("/d/a_R2.fq.gz")}}])
show("C. star 单步：参考索引被要路径", [
    {"tool_id": "star", "inputs": {"read1": f("/d/a_R1.fq.gz"), "read2": f("/d/a_R2.fq.gz")}}])
show("D. 三步链", [
    {"tool_id": "trim_galore", "inputs": {"read1": f("/d/a_R1.fq.gz"), "read2": f("/d/a_R2.fq.gz")}},
    {"tool_id": "fastqc",      "inputs": {"fastqs": f("/d/a_R1_val_1.fq.gz")}},
    {"tool_id": "star",        "inputs": {"read1": f("/d/a_R1_val_1.fq.gz"), "read2": f("/d/a_R2_val_2.fq.gz"),
                                          "rrna_star_index": f("/ref/rrna"), "genome_star_index": f("/ref/genome")}}])
PY
python3 /tmp/probe_light.py
```

改完之后，A/B/D 三例的 `execution_params` 必须包含各自卡片声明的全部必需 File 输入
（且不含那 5 个参考资源），C 例必须 `submittable: true`。

---

## 7. 检查过但没问题的

- Knowledge Card 12 张卡的输入类型/必需性/format 与重版 0823 后状态一致（见 §5 末条）。
- stage 1 注册校验、stage 5 邻接查询逻辑本身正确，且 Cypher 注入已在 0820 修过。
- `_real_path()`（`mcp_light_server.py:675-690`）的真实路径判据正确：要求 `/` 开头、
  排除 `NOT_FOUND`，回退按 `file_name` 查图时有 `_SAFE_FILE` 白名单。**不臆造路径**这条守住了。
- 图谱不需要重载（理由见 §P1-3 末段）。
