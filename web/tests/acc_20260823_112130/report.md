# 96 例准确率报告 20260823_112130

- 服务 http://127.0.0.1:8017 · 96 例 · 并发 8 · 总耗时 112.3s

- **整体通过（工具+数据都对且格式合法）: 90/96**
- 工具命中 rank1: 96/96 · 出现在候选里: 96/96
- 数据命中 rank1: 84/96 · 出现在任一推荐: 84/96
- 格式非法 0 · 报错 0 · 含图内缺失文件的用例 6
- 耗时 avg 8.5s · p50 7.1s · p90 12.6s · >30s 2 例

## 按期望工具

| 期望工具 | 例数 | 工具命中 | 数据命中 |
|---|---|---|---|
| diff_expr_go | 12 | 12 | 12 |
| diff_expr_kegg | 12 | 12 | 12 |
| immune_infiltration_iobr | 12 | 12 | 12 |
| rnaseq_unsupervised_cluster | 12 | 12 | 12 |
| wes_somatic_maf_landscape | 12 | 12 | 9 |
| her2_pfs_survival | 9 | 9 | 9 |
| survival_analysis | 6 | 6 | 6 |
| cellranger_workflow | 3 | 3 | 3 |
| driver_gene_gender_analysis | 3 | 3 | 3 |
| paired_fastq_to_unmapped_bam | 3 | 3 | 0 |
| rnaseq_singletask | 3 | 3 | 0 |
| tmb_survival_analysis | 3 | 3 | 3 |
| wes_somatic_pair | 3 | 3 | 0 |
| wgcna | 3 | 3 | 3 |

## 逐例

| 用例 | 问题 | 期望工具 | 实际 | 工具 | 数据 | 缺失文件 | 轮数 | 耗时 | 判定 |
|---|---|---|---|---|---|---|---|---|---|
| c01 | 我有 10x 单细胞测序的 FASTQ 文件 | cellranger_workflow | cellranger_workflow | ✅ | ✅ | - | 3 | 9.0s | ✅ |
| c02 | 单细胞 RNA-seq 原始数据能不能直接做 | cellranger_workflow | cellranger_workflow | ✅ | ✅ | - | 2 | 13.5s | ✅ |
| c03 | 如何从 10x Genomics 单细胞 F | cellranger_workflow | cellranger_workflow | ✅ | ✅ | - | 2 | 8.7s | ✅ |
| c04 | 胶质瘤病例组和对照组的表达矩阵，怎么做差异表 | diff_expr_go | diff_expr_go | ✅ | ✅ | - | 2 | 7.5s | ✅ |
| c05 | 我想找胶质瘤中上调和下调的差异基因，并看看它 | diff_expr_go | diff_expr_go | ✅ | ✅ | - | 2 | 6.7s | ✅ |
| c06 | 基于胶质瘤队列的 bulk RNA-seq  | diff_expr_go | diff_expr_go | ✅ | ✅ | - | 2 | 6.5s | ✅ |
| c07 | 黑色素瘤转录组数据想做 GO 富集，应该先用 | diff_expr_go | diff_expr_go | ✅ | ✅ | - | 2 | 6.6s | ✅ |
| c08 | 我有黑色素瘤病例和对照的表达矩阵，想比较差异 | diff_expr_go | diff_expr_go | ✅ | ✅ | - | 2 | 6.5s | ✅ |
| c09 | 如何基于黑色素瘤队列的 FPKM 表达矩阵， | diff_expr_go | diff_expr_go | ✅ | ✅ | - | 2 | 6.7s | ✅ |
| c10 | 食管癌差异表达基因想做 GO 分析，用哪个工 | diff_expr_go | diff_expr_go | ✅ | ✅ | - | 2 | 5.3s | ✅ |
| c11 | 我想用食管癌 RNA-seq 表达矩阵找差异 | diff_expr_go | diff_expr_go | ✅ | ✅ | - | 2 | 11.3s | ✅ |
| c12 | 基于食管癌病例组和对照组的转录组数据，如何进 | diff_expr_go | diff_expr_go | ✅ | ✅ | - | 2 | 7.1s | ✅ |
| c13 | 肝癌 bulk RNA-seq 数据能不能做 | diff_expr_go | diff_expr_go | ✅ | ✅ | - | 2 | 5.9s | ✅ |
| c14 | 我想知道肝癌病例组和对照组之间哪些基因表达不 | diff_expr_go | diff_expr_go | ✅ | ✅ | - | 2 | 8.2s | ✅ |
| c15 | 如何利用肝癌队列的 FPKM 表达矩阵筛选差 | diff_expr_go | diff_expr_go | ✅ | ✅ | - | 2 | 7.7s | ✅ |
| c16 | 胶质瘤差异表达基因想做通路富集，应该用什么流 | diff_expr_kegg | diff_expr_kegg | ✅ | ✅ | - | 2 | 6.7s | ✅ |
| c17 | 我有胶质瘤表达矩阵，想先做差异分析，再看相关 | diff_expr_kegg | diff_expr_kegg | ✅ | ✅ | - | 2 | 7.5s | ✅ |
| c18 | 基于胶质瘤病例组和对照组的转录组数据，如何对 | diff_expr_kegg | diff_expr_kegg | ✅ | ✅ | - | 3 | 31.2s | ✅ |
| c19 | 黑色素瘤差异基因可以做 Reactome 通 | diff_expr_kegg | diff_expr_kegg | ✅ | ✅ | - | 2 | 7.2s | ✅ |
| c20 | 我想从黑色素瘤的表达矩阵里筛差异基因，并进一 | diff_expr_kegg | diff_expr_kegg | ✅ | ✅ | - | 3 | 12.6s | ✅ |
| c21 | 如何基于黑色素瘤队列的 RNA-seq 表达 | diff_expr_kegg | diff_expr_kegg | ✅ | ✅ | - | 2 | 9.1s | ✅ |
| c22 | 食管癌差异表达基因想做通路富集，应该用哪个工 | diff_expr_kegg | diff_expr_kegg | ✅ | ✅ | - | 2 | 6.3s | ✅ |
| c23 | 我有食管癌病例组和对照组的 FPKM 表达矩 | diff_expr_kegg | diff_expr_kegg | ✅ | ✅ | - | 2 | 9.4s | ✅ |
| c24 | 基于食管癌转录组数据，如何分别对上调和下调差 | diff_expr_kegg | diff_expr_kegg | ✅ | ✅ | - | 2 | 10.5s | ✅ |
| c25 | 肝癌差异表达基因怎么做通路富集？ | diff_expr_kegg | diff_expr_kegg | ✅ | ✅ | - | 2 | 7.6s | ✅ |
| c26 | 我想用肝癌表达矩阵找出差异基因，并分析它们涉 | diff_expr_kegg | diff_expr_kegg | ✅ | ✅ | - | 3 | 10.2s | ✅ |
| c27 | 如何基于肝癌 bulk RNA-seq 数据 | diff_expr_kegg | diff_expr_kegg | ✅ | ✅ | - | 2 | 8.0s | ✅ |
| c28 | 肝癌突变数据能不能比较男女患者的驱动基因差异 | driver_gene_gender_analysis | driver_gene_gender_analysis | ✅ | ✅ | - | 2 | 7.4s | ✅ |
| c29 | 我想看肝癌男性和女性患者中，哪些癌症驱动基因 | driver_gene_gender_analysis | driver_gene_gender_analysis | ✅ | ✅ | - | 2 | 7.5s | ✅ |
| c30 | 结合肝癌患者的 WES 体细胞突变 MAF  | driver_gene_gender_analysis | driver_gene_gender_analysis | ✅ | ✅ | - | 2 | 8.1s | ✅ |
| c31 | 肝癌 HER2 表达高低和 PFS 有关系吗 | her2_pfs_survival | her2_pfs_survival | ✅ | ✅ | - | 1 | 4.7s | ✅ |
| c32 | 我想把肝癌患者按 ERBB2 表达量分成高低 | her2_pfs_survival | her2_pfs_survival | ✅ | ✅ | - | 2 | 5.1s | ✅ |
| c33 | 如何结合肝癌患者的 TPM 表达矩阵、临床随 | her2_pfs_survival | her2_pfs_survival | ✅ | ✅ | - | 2 | 7.4s | ✅ |
| c34 | 胶质瘤能做 HER2 表达和 PFS 的生存 | her2_pfs_survival | her2_pfs_survival | ✅ | ✅ | - | 2 | 6.0s | ✅ |
| c35 | 我想知道胶质瘤患者中 ERBB2 高表达组和 | her2_pfs_survival | her2_pfs_survival | ✅ | ✅ | - | 2 | 6.9s | ✅ |
| c36 | 基于胶质瘤队列的 TPM 表达矩阵和临床随访 | her2_pfs_survival | her2_pfs_survival | ✅ | ✅ | - | 2 | 7.1s | ✅ |
| c37 | 黑色素瘤 HER2 表达和 PFS 怎么分析 | her2_pfs_survival | her2_pfs_survival | ✅ | ✅ | - | 2 | 6.6s | ✅ |
| c38 | 我有黑色素瘤的 TPM 表达数据和随访信息， | her2_pfs_survival | her2_pfs_survival | ✅ | ✅ | - | 2 | 6.4s | ✅ |
| c39 | 如何利用黑色素瘤队列的转录组表达矩阵和临床生 | her2_pfs_survival | her2_pfs_survival | ✅ | ✅ | - | 2 | 5.6s | ✅ |
| c40 | 肝癌 TPM 表达矩阵能做免疫浸润分析吗？ | immune_infiltration_iobr | immune_infiltration_iobr | ✅ | ✅ | - | 2 | 5.8s | ✅ |
| c41 | 我想看肝癌样本里的免疫细胞组成，比如 T 细 | immune_infiltration_iobr | immune_infiltration_iobr | ✅ | ✅ | - | 1 | 4.4s | ✅ |
| c42 | 基于肝癌 bulk RNA-seq TPM  | immune_infiltration_iobr | immune_infiltration_iobr | ✅ | ✅ | - | 1 | 4.0s | ✅ |
| c43 | 胶质瘤样本能不能做免疫浸润分析？ | immune_infiltration_iobr | immune_infiltration_iobr | ✅ | ✅ | - | 3 | 8.0s | ✅ |
| c44 | 我有胶质瘤 TPM 表达矩阵，想估计不同免疫 | immune_infiltration_iobr | immune_infiltration_iobr | ✅ | ✅ | - | 2 | 5.8s | ✅ |
| c45 | 如何基于胶质瘤转录组表达数据推断肿瘤免疫微环 | immune_infiltration_iobr | immune_infiltration_iobr | ✅ | ✅ | - | 2 | 6.7s | ✅ |
| c46 | 黑色素瘤 RNA-seq 数据能分析免疫细胞 | immune_infiltration_iobr | immune_infiltration_iobr | ✅ | ✅ | - | 2 | 5.0s | ✅ |
| c47 | 我想比较黑色素瘤样本中不同免疫细胞的比例，有 | immune_infiltration_iobr | immune_infiltration_iobr | ✅ | ✅ | - | 2 | 4.9s | ✅ |
| c48 | 结合黑色素瘤队列的 TPM 表达矩阵和临床样 | immune_infiltration_iobr | immune_infiltration_iobr | ✅ | ✅ | - | 2 | 6.2s | ✅ |
| c49 | 食管癌 TPM 矩阵可以推断免疫浸润吗？ | immune_infiltration_iobr | immune_infiltration_iobr | ✅ | ✅ | - | 2 | 11.7s | ✅ |
| c50 | 我想用食管癌 bulk RNA-seq 数据 | immune_infiltration_iobr | immune_infiltration_iobr | ✅ | ✅ | - | 2 | 5.5s | ✅ |
| c51 | 基于食管癌队列的转录组 TPM 表达数据，如 | immune_infiltration_iobr | immune_infiltration_iobr | ✅ | ✅ | - | 2 | 9.3s | ✅ |
| c52 | 双端 FASTQ 文件怎么转成 unmapp | paired_fastq_to_unmapped_bam | paired_fastq_to_unmapped_bam | ✅ | ❌ | - | 2 | 11.4s | ✅ |
| c53 | 我想把原始测序 FASTQ 整理成 GATK | paired_fastq_to_unmapped_bam | paired_fastq_to_unmapped_bam | ✅ | ❌ | - | 3 | 17.6s | ✅ |
| c54 | 如何将 paired-end FASTQ 数 | paired_fastq_to_unmapped_bam | paired_fastq_to_unmapped_bam | ✅ | ❌ | - | 2 | 20.3s | ✅ |
| c55 | RNA-seq FASTQ 原始数据怎么做完 | rnaseq_singletask | rnaseq_singletask | ✅ | ❌ | - | 2 | 11.2s | ✅ |
| c56 | 我有双端 RNA-seq 测序数据，想完成质 | rnaseq_singletask | rnaseq_singletask | ✅ | ❌ | - | 3 | 18.8s | ✅ |
| c57 | 如何从 RNA-seq paired-end | rnaseq_singletask | rnaseq_singletask | ✅ | ❌ | - | 6 | 36.9s | ✅ |
| c58 | 肝癌 counts 矩阵能不能做无监督聚类？ | rnaseq_unsupervised_cluster | rnaseq_unsupervised_cluster | ✅ | ✅ | - | 2 | 6.0s | ✅ |
| c59 | 我想看看肝癌样本能不能根据表达谱自动分成几个 | rnaseq_unsupervised_cluster | rnaseq_unsupervised_cluster | ✅ | ✅ | - | 2 | 6.1s | ✅ |
| c60 | 基于肝癌 RNA-seq counts 数据 | rnaseq_unsupervised_cluster | rnaseq_unsupervised_cluster | ✅ | ✅ | - | 2 | 9.2s | ✅ |
| c61 | 黑色素瘤表达矩阵可以做样本聚类吗？ | rnaseq_unsupervised_cluster | rnaseq_unsupervised_cluster | ✅ | ✅ | - | 2 | 5.8s | ✅ |
| c62 | 我想用黑色素瘤 RNA-seq counts | rnaseq_unsupervised_cluster | rnaseq_unsupervised_cluster | ✅ | ✅ | - | 2 | 5.6s | ✅ |
| c63 | 如何基于黑色素瘤队列的 counts 矩阵进 | rnaseq_unsupervised_cluster | rnaseq_unsupervised_cluster | ✅ | ✅ | - | 2 | 6.3s | ✅ |
| c64 | 食管癌 counts 数据怎么做无监督聚类？ | rnaseq_unsupervised_cluster | rnaseq_unsupervised_cluster | ✅ | ✅ | - | 2 | 6.3s | ✅ |
| c65 | 我想根据食管癌 RNA-seq 表达谱自动识 | rnaseq_unsupervised_cluster | rnaseq_unsupervised_cluster | ✅ | ✅ | - | 2 | 6.0s | ✅ |
| c66 | 基于食管癌队列的 RNA-seq count | rnaseq_unsupervised_cluster | rnaseq_unsupervised_cluster | ✅ | ✅ | - | 3 | 9.5s | ✅ |
| c67 | 胶质瘤 RNA-seq counts 能用来 | rnaseq_unsupervised_cluster | rnaseq_unsupervised_cluster | ✅ | ✅ | - | 2 | 6.3s | ✅ |
| c68 | 我想判断胶质瘤队列里是否存在不同的表达亚型。 | rnaseq_unsupervised_cluster | rnaseq_unsupervised_cluster | ✅ | ✅ | - | 2 | 5.5s | ✅ |
| c69 | 如何利用胶质瘤 counts 矩阵进行无监督 | rnaseq_unsupervised_cluster | rnaseq_unsupervised_cluster | ✅ | ✅ | - | 2 | 8.0s | ✅ |
| c70 | 肝癌 EGFR 突变和 PFS 有关系吗？ | survival_analysis | survival_analysis | ✅ | ✅ | - | 2 | 8.1s | ✅ |
| c71 | 我有肝癌 WES 突变数据和随访资料，想按  | survival_analysis | survival_analysis | ✅ | ✅ | - | 2 | 7.8s | ✅ |
| c72 | 结合肝癌患者的全外显子测序体细胞突变数据、临 | survival_analysis | survival_analysis | ✅ | ✅ | - | 2 | 8.8s | ✅ |
| c73 | 黑色素瘤 EGFR 突变会影响 PFS 吗？ | survival_analysis | survival_analysis | ✅ | ✅ | - | 2 | 5.8s | ✅ |
| c74 | 我想用黑色素瘤体细胞突变数据和临床信息做 E | survival_analysis | survival_analysis | ✅ | ✅ | - | 2 | 5.8s | ✅ |
| c75 | 基于黑色素瘤患者的 MAF 文件、临床随访数 | survival_analysis | survival_analysis | ✅ | ✅ | - | 2 | 7.2s | ✅ |
| c76 | 黑色素瘤患者的 TMB 高低和生存预后有没有 | tmb_survival_analysis | tmb_survival_analysis | ✅ | ✅ | - | 2 | 6.0s | ✅ |
| c77 | 我手里有黑色素瘤的 WES 突变数据和临床随 | tmb_survival_analysis | tmb_survival_analysis | ✅ | ✅ | - | 2 | 7.0s | ✅ |
| c78 | 结合黑色素瘤患者的 WES 体细胞突变数据、 | tmb_survival_analysis | tmb_survival_analysis | ✅ | ✅ | - | 2 | 7.4s | ✅ |
| c79 | 肝癌 MAF 文件怎么画突变景观图？ | wes_somatic_maf_landscape | wes_somatic_maf_landscape | ✅ | ✅ | - | 2 | 6.1s | ✅ |
| c80 | 我想展示肝癌队列里 Top30 高频突变基因 | wes_somatic_maf_landscape | wes_somatic_maf_landscape | ✅ | ✅ | - | 2 | 7.3s | ✅ |
| c81 | 基于肝癌全外显子测序得到的体细胞突变 MAF | wes_somatic_maf_landscape | wes_somatic_maf_landscape | ✅ | ✅ | - | 2 | 5.8s | ✅ |
| c82 | 胶质瘤突变数据能画 Oncoplot 吗？ | wes_somatic_maf_landscape | wes_somatic_maf_landscape | ✅ | ✅ | - | 2 | 4.8s | ✅ |
| c83 | 我想看胶质瘤队列中哪些基因突变频率最高，并展 | wes_somatic_maf_landscape | wes_somatic_maf_landscape | ✅ | ✅ | - | 1 | 4.1s | ✅ |
| c84 | 如何基于胶质瘤 WES 体细胞突变 MAF  | wes_somatic_maf_landscape | wes_somatic_maf_landscape | ✅ | ✅ | - | 2 | 7.0s | ✅ |
| c85 | 黑色素瘤 MAF 数据怎么生成突变景观图？ | wes_somatic_maf_landscape | wes_somatic_maf_landscape | ✅ | ✅ | - | 2 | 5.7s | ✅ |
| c86 | 我想把黑色素瘤样本的高频突变基因和突变类型分 | wes_somatic_maf_landscape | wes_somatic_maf_landscape | ✅ | ✅ | - | 2 | 6.6s | ✅ |
| c87 | 基于黑色素瘤体细胞突变数据，如何绘制 Top | wes_somatic_maf_landscape | wes_somatic_maf_landscape | ✅ | ✅ | - | 2 | 5.5s | ✅ |
| c88 | 肝癌相关突变 MAF 数据可以做突变景观展示 | wes_somatic_maf_landscape | wes_somatic_maf_landscape | ✅ | ❌ | hra001749-somaticsnv-1.0.maf | 2 | 5.9s | ❌ |
| c89 | 我想统计肝癌样本中的高频突变基因、样本突变情 | wes_somatic_maf_landscape | wes_somatic_maf_landscape | ✅ | ❌ | hra001749-somaticsnv-1.0.maf | 2 | 8.8s | ❌ |
| c90 | 如何利用肝癌体细胞突变 MAF 文件生成 T | wes_somatic_maf_landscape | wes_somatic_maf_landscape | ✅ | ❌ | hra001749-somaticsnv-1.0.maf | 2 | 7.2s | ❌ |
| c91 | 肝癌 tumor-normal WES 数据 | wes_somatic_pair | wes_somatic_pair | ✅ | ❌ | hrr365660_f1.fastq.gz,hrr365660_r2.fastq | 5 | 18.6s | ❌ |
| c92 | 我有一对肿瘤和正常配对的 WES 双端 FA | wes_somatic_pair | wes_somatic_pair | ✅ | ❌ | hrr365660_f1.fastq.gz,hrr365660_r2.fastq | 7 | 19.2s | ❌ |
| c93 | 从肝癌患者配对 WES 原始数据出发，如何完 | wes_somatic_pair | wes_somatic_pair | ✅ | ❌ | hrr365660_f1.fastq.gz,hrr365660_r2.fastq | 2 | 21.3s | ❌ |
| c94 | 胶质瘤 counts 矩阵怎么做 WGCNA | wgcna | wgcna | ✅ | ✅ | - | 2 | 4.9s | ✅ |
| c95 | 我想找胶质瘤中和肿瘤分级或性别相关的共表达模 | wgcna | wgcna | ✅ | ✅ | - | 2 | 7.1s | ✅ |
| c96 | 结合胶质瘤 RNA-seq counts 矩 | wgcna | wgcna | ✅ | ✅ | - | 3 | 11.3s | ✅ |
