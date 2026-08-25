# normal_questions.tsv —— 165 题通用问答集（2026-08-24 交付）

来源：师兄给的 `普通问题.xlsx`，原样转成 TSV，一行一题，未改写题面。

## 列

| 列 | 说明 |
|---|---|
| `question_id` | `Q_xxxx`，原表编号，不连续 |
| `question` | 中文问句 |
| `intent` | 12 类意图（见下） |
| `difficulty` | High 65 / Med 56 / Low 44 |
| `工具` | 期望命中的工具，`;` 分隔。**98 题有值，67 题为空** |
| `数据文件` | 期望命中的数据文件。**只有 6 题有值** |

意图分布：Goal-Function-Tool Mapping 64、Data Recommendation 16、Tool Recommendation 15、
Toolchain/Workflow 13、Statistics 13、Tool/Format Comparison 9、Data Quality/Gap 8、
Format Conversion 8、Metadata/Concept Lookup 5、Clinical/Survival Analysis 5、Negative 5、
Lineage/Provenance 4。

## 怎么用它，以及不能怎么用

**这不是 24 例回归的替代品。** 24 例回归判的是「输出契约是否成立」（能不能产出合法
tool-chain/v2 JSON、接地校验过不过），有确定性判据、可以当门禁。这 165 题里只有 98 题带期望
工具、6 题带期望数据，其余 67 题没有任何可判定的标准答案——**当门禁会把「没写答案」判成
「答错」**。它的正确用法是：

- 带 `工具` 的 98 题 → 算 top-1 / top-3 工具命中率，看**选型**准不准；
- `Negative` 那 5 题 → 验拒绝纪律，期望零工具调用；
- 其余 → 只能人工抽样看，别进自动评分。

## 一条已经用上的结论：atomic 工具名不要跟着新语料改名

新语料（`归档 2.zip`，57 个工具）把 9 个 atomic 工具改成了长名，旧短名保留为 alias：

```
bcftools → bcftools_somatic_postprocess      bwa → bwa_mem_paired
fastp → fastp_paired_end                     featurecounts → featurecounts_gene_counting
gatk → gatk_wes_somatic                      rsem → rsem_quantification
samtools → samtools_alignment_processing     snpeff → snpeff_annotation
star → star_rrna_and_genome_alignment
```

**本表的 `工具` 列用的是旧短名**，与仓库 `tool_catalog.csv` 和图内 51 个 tool 节点一致。
所以改名没有跟进——跟了会让这 165 题的期望值全部对不上，而收益为零（长名只是 alias）。
新语料里真正新增的 6 个工具（`gatk_germline_cohort` `joint_genotyping`
`manta_structural_variants` `star_fusion` `tumor_evolution_inference` `wgs_single_sample`）
本表一次都没引用，图内也没有对应节点，因此同样未入目录。
