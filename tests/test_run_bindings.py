"""运行绑定：绑定表自检 + 路由回归（2026-09-28 运行测试表 17 条）。全部离线，不连 Neo4j、不调模型。"""
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mcp_light_server as srv  # noqa: E402
import run_bindings as rb  # noqa: E402
import cohort_adapter as adapter  # noqa: E402


def _no_model(query):
    raise AssertionError(f"点名了流程的问句不该进模型循环：{query}")


def _no_graph(*a, **kw):
    raise AssertionError("运行绑定路径不该查图")


class RegistryIntegrityTests(unittest.TestCase):
    """绑定表本身的硬约束：任何一条不满足，平台拿到的合同就可能跑不了。"""

    def test_registry_not_empty(self):
        self.assertGreater(len(rb.bound_tools()), 0, "bindings/ 下没有可用绑定")

    def test_every_binding_is_complete(self):
        for gid, doc in rb.registry().items():
            card = rb.card(gid)
            self.assertTrue(card, f"{gid}: 知识卡片里没有这个流程")
            required = [i["name"] for i in card.get("inputs") or [] if i.get("required")
                        and not rb.is_reference_resource(gid, i["name"])]
            for b in doc["bindings"]:
                with self.subTest(tool=gid, study=b["study"]):
                    params = b["execution_params"]
                    self.assertEqual([n for n in required if n not in params], [],
                                     "缺卡片必填参数——平台按它提交必挂")
                    self.assertFalse([k for k in params if rb.is_platform_input(k)],
                                     "资源类参数混进了 execution_params")
                    self.assertRegex(b["study"], r"^HRA\d{6}$")
                    self.assertIn(b["study"], rb.studies(), "队列不在 bindings/studies.json")
                    self.assertTrue(b["source_run"]["cromwell_id"])

    def test_bound_paths_stay_in_their_own_study(self):
        """队列与资产必须落在同一个 study 上：A 队列的矩阵配 B 队列的样本表，执行端只会静默得到 0 个样本。"""
        for gid, doc in rb.registry().items():
            for b in doc["bindings"]:
                paths = [p for p in _strings(b["execution_params"]) if rb.is_data_path(p)]
                others = {s.upper() for p in paths for s in re.findall(r"HRA\d{6}", p)} - {b["study"]}
                with self.subTest(tool=gid, study=b["study"]):
                    self.assertTrue(paths, "绑定里没有任何真实数据路径")
                    self.assertEqual(others, set())

    def test_usable_groups_do_not_overlap(self):
        """同一个样本出现在两组里：运行照样 Succeeded，但比较没有意义，不能放行。"""
        for gid in rb.bound_tools():
            for b in rb.bindings_for(gid):
                p = b["execution_params"]
                for a, c in rb.GROUP_PAIRS:
                    if isinstance(p.get(a), list) and isinstance(p.get(c), list):
                        with self.subTest(tool=gid, study=b["study"], pair=a):
                            self.assertEqual(set(p[a]) & set(p[c]), set())

    def test_default_study_is_usable(self):
        for gid, doc in rb.registry().items():
            if not rb.bindings_for(gid):
                continue
            with self.subTest(tool=gid):
                self.assertIn(doc["default_study"], {b["study"] for b in doc["bindings"]})

    def test_bound_tools_are_registered_in_catalog(self):
        """平台按 catalog_status 静默过滤：未注册的流程绑了也到不了执行端。"""
        self.assertEqual([g for g in rb.bound_tools() if g not in srv.CATALOG], [])

    def test_workflow_map_targets_known_tools(self):
        self.assertEqual({g for g in rb.WORKFLOW_TOOLS.values()} - set(rb.all_tool_ids()), set())


def _strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)


# 2026-09-28 运行测试表：问句 → 期望（流程, 状态）。旧版 6 条点名了流程却被换成别的流程；
# 现在点名即锁定，没有绑定的如实回 unsupported。
DECK = [
    ("我想用癌症数据进行FASTQ 转 uBAM分析", "paired_fastq_to_unmapped_bam", "unsupported"),
    ("我想用癌症数据进行paired_fastq_to_unmapped_bam分析", "paired_fastq_to_unmapped_bam", "unsupported"),
    ("我想用癌症数据进行WGS 单样本分析", None, "unsupported"),
    ("我想用癌症数据进行wes_somatic_pair分析", "wes_somatic_pair", "unsupported"),
    ("我想用癌症数据进行gatk_germline_cohort分析", "gatk_germline_cohort", "unsupported"),
    ("我想用癌症数据进行cnvkit_cnv_clinical分析", "cnvkit_cnv_clinical", "ready"),
    ("我想用癌症数据进行manta_structural_variants分析", "manta_structural_variants", "ready"),
    ("我想用癌症数据进行wes_somatic_maf_landscape分析", "wes_somatic_maf_landscape", "ready"),
    ("我想用癌症数据进行driver_gene_gender_analysis分析", "driver_gene_gender_analysis", "ready"),
    ("我想用癌症数据进行tumor_evolution_inference分析", "tumor_evolution_inference", "unsupported"),
    ("我想用癌症数据进行rnaseq_singletask分析", "rnaseq_singletask", "unsupported"),
    ("我想用癌症数据进行preprocess_counts分析", "preprocess_counts", "ready"),
    ("我想用食管鳞癌数据进行rnaseq_unsupervised_cluster分析", "rnaseq_unsupervised_cluster", "ready"),
    ("我想用食管鳞癌数据进行hvg_pca_gmm分析", "hvg_pca_gmm", "ready"),
    ("我想用肝细胞癌数据进行rmats_alternative_splicing分析", "rmats_alternative_splicing", "unsupported"),
    # 09-28 这两条标的是「成功」，但那次运行两组样本重叠（48 vs 48 有 25 个相同），比较无意义；
    # 胶质瘤上的这两个绑定已停用 / 不收，问到要如实说做不了
    ("我想用恶性胶质瘤数据进行diff_expr_go分析", "diff_expr_go", "unsupported"),
    ("我想用恶性胶质瘤数据进行diff_expr_kegg分析", "diff_expr_kegg", "unsupported"),
]


class DeckRegressionTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(srv, "neo4j_q", side_effect=_no_graph)
        p.start()
        self.addCleanup(p.stop)

    def route(self, q):
        return adapter.route(q, plan_fn=_no_model)

    def test_named_workflow_is_never_swapped(self):
        for q, gid, status in DECK:
            with self.subTest(q=q):
                if gid and not rb.bindings_for(gid):
                    status = "unsupported"          # 绑定表变了，期望跟着绑定走
                r = self.route(q)
                self.assertEqual(r["intent"].get("tool_id"), gid)
                self.assertEqual(r["selection_status"], status, r.get("unsupported_reason"))
                for c in r["candidates"]:
                    self.assertEqual(c["pipeline_id"], gid, "候选里混进了别的流程")

    def test_disease_words_pick_matching_cohort(self):
        r = self.route("我想用食管鳞癌数据进行hvg_pca_gmm分析")
        self.assertEqual(rb.studies()[r["candidates"][0]["study_accession"]]["tumor_type"], "esophageal cancer")
        r = self.route("我想用恶性胶质瘤数据进行wgcna分析")
        self.assertEqual(rb.studies()[r["candidates"][0]["study_accession"]]["tumor_type"], "malignant glioma")

    def test_unbound_disease_is_not_substituted(self):
        """点名的癌种没有绑定：说清楚有哪些可选，不换成别的癌种。"""
        gid = "diff_expr_kegg"
        bound = {b["study"] for b in rb.bindings_for(gid)}
        if "HRA001272" in bound:
            self.skipTest("绑定表里已有肝细胞癌")
        r = self.route(f"我想用肝细胞癌数据进行{gid}分析")
        self.assertEqual(r["selection_status"], "unsupported")
        self.assertEqual(r["candidates"], [])
        self.assertTrue(all(s in r["unsupported_reason"] for s in bound))

    def test_explicit_study(self):
        gid = "manta_structural_variants"
        bound = [b["study"] for b in rb.bindings_for(gid)]
        r = self.route(f"用 {bound[-1]} 跑 {gid}")
        self.assertEqual(r["candidates"][0]["study_accession"], bound[-1])
        r = self.route(f"用 HRA000001 跑 {gid}")
        self.assertEqual(r["selection_status"], "unsupported")

    def test_unknown_identifier_is_rejected_not_guessed(self):
        r = self.route("我想用癌症数据进行fq_to_ubam分析")
        self.assertEqual(r["selection_status"], "unsupported")
        self.assertIsNone(r["intent"].get("tool_id"))

    def test_gene_expression_question_goes_to_boxplot(self):
        if not rb.bindings_for("gene_boxplot"):
            self.skipTest("gene_boxplot 没有绑定")
        r = self.route("MIR503HG 在食管鳞癌组织中的表达是升高还是降低？")
        self.assertEqual(r["intent"]["tool_id"], "gene_boxplot")
        self.assertEqual(r["intent"]["resolved_by"], "gene_expression_priority")


class ContractShapeTests(unittest.TestCase):
    """平台（Dingent / PipelineBuilder）读的形状：与旧版 to_cohort_v2 对齐。"""

    def setUp(self):
        p = patch.object(srv, "neo4j_q", side_effect=_no_graph)
        p.start()
        self.addCleanup(p.stop)

    def test_every_binding_yields_a_submittable_contract(self):
        for gid in rb.bound_tools():
            for b in rb.bindings_for(gid):
                with self.subTest(tool=gid, study=b["study"]):
                    r = adapter.route(f"用 {b['study']} 的数据进行{gid}分析", plan_fn=_no_model)
                    self.assertEqual(r["schema_version"], "tool-chain/v2")
                    self.assertEqual(r["selection_status"], "ready", r.get("unsupported_reason"))
                    rec, cand = r["recommendations"][0], r["candidates"][0]
                    self.assertEqual(rec["tool"]["catalog_status"], "registered")
                    self.assertEqual(rec["execution_params"], b["execution_params"],
                                     "合同参数必须原样等于绑定")
                    self.assertEqual(cand["feasibility_status"], "ready")
                    self.assertEqual(cand["execution_params_missing"], [])
                    builder = {s["builder_param"] for s in rec["tool"]["inputs"]}
                    card_keys = {i["name"] for i in rb.card(gid)["inputs"]}
                    self.assertLessEqual(set(rec["execution_params"]) & card_keys, builder)
                    self.assertFalse([a["path"] for a in cand["assets"] if a["path"].startswith("/opt/")],
                                     "容器内路径不是数据资产")
                    by_id = {a["asset_id"]: a for a in cand["assets"]}
                    for name, v in cand["tool_chain"][0]["inputs"].items():
                        refs = v if isinstance(v, list) else [v]
                        for ref in refs:
                            if "asset_id" in ref:
                                self.assertTrue(by_id[ref["asset_id"]]["path"].startswith("/"))

    def test_alternatives_are_other_bound_cohorts(self):
        gid = next(g for g in rb.bound_tools() if len(rb.bindings_for(g)) >= 3)
        r = adapter.route(f"我想用癌症数据进行{gid}分析", top_k=3, plan_fn=_no_model)
        self.assertEqual(len(r["candidates"]), 3)
        studies = [c["study_accession"] for c in r["candidates"]]
        self.assertEqual(len(set(studies)), 3)
        self.assertEqual([c["selection_source"] for c in r["candidates"]],
                         ["run_binding", "alternative_binding", "alternative_binding"])

    def test_user_gene_overrides_only_whitelisted_literal(self):
        gid = "her2_pfs_survival"
        if not rb.bindings_for(gid):
            self.skipTest("her2_pfs_survival 没有绑定")
        r = adapter.route(f"用 {gid} 看 EGFR 表达高低与 PFS", plan_fn=_no_model)
        params = r["recommendations"][0]["execution_params"]
        self.assertEqual(params["gene"], "EGFR")
        b = rb.get(gid, r["candidates"][0]["study_accession"])
        self.assertEqual({k: v for k, v in params.items() if k != "gene"},
                         {k: v for k, v in b["execution_params"].items() if k != "gene"})


class ModelPathTests(unittest.TestCase):
    """没点名流程时走模型循环：模型只定流程（和队列提示），参数仍然来自绑定。"""

    def plan(self, pid, studies=()):
        return lambda q: ({"schema_version": "tool-chain/v2", "selection_status": "ok",
                           "answer": "模型的说明",
                           "recommendations": [{"pipeline_id": pid,
                                                "data": {"study_accessions": list(studies)}}]}, None)

    def test_model_choice_is_bound_from_registry(self):
        gid = rb.bound_tools()[0]
        study = rb.bindings_for(gid)[-1]["study"]
        r = adapter.route("帮我分析一下这批数据", plan_fn=self.plan(gid, [study]))
        self.assertEqual(r["selection_status"], "ready")
        self.assertEqual(r["intent"]["resolved_by"], "model")
        self.assertEqual(r["candidates"][0]["study_accession"], study)
        self.assertEqual(r["recommendations"][0]["execution_params"], rb.get(gid, study)["execution_params"])
        self.assertEqual(r["model_answer"], "模型的说明")

    def test_model_picking_unbound_tool_is_unsupported(self):
        r = adapter.route("帮我分析一下这批数据", plan_fn=self.plan("gatk_germline_cohort"))
        self.assertEqual(r["selection_status"], "unsupported")

    def test_card_id_and_task_prefix_are_mapped(self):
        self.assertEqual(rb.tool_id_of("fastp_paired_end"), "fastp")
        self.assertEqual(rb.tool_id_of("task310_cox_model"), "cox_model")
        self.assertEqual(rb.tool_id_of("MantaStructuralVariants"), "manta_structural_variants")

    def test_information_passthrough(self):
        r = adapter.route("什么是 STAR 比对", plan_fn=lambda q: (
            {"schema_version": "tool-chain/v2", "selection_status": "information",
             "answer": "占位", "recommendations": [], "candidates": []}, None))
        self.assertEqual(r["selection_status"], "information")
        self.assertNotIn("unsupported_reason", r)

    def test_rejected(self):
        r = adapter.route("今天天气怎么样", plan_fn=lambda q: (
            {"status": "rejected", "reason": "off_topic"}, None))
        self.assertEqual(r["selection_status"], "unsupported")
        self.assertEqual(r["candidates"], [])

    def test_model_failure_is_no_candidate(self):
        r = adapter.route("帮我分析一下这批数据", plan_fn=lambda q: (None, "超时"))
        self.assertEqual(r["selection_status"], "no_candidate")


class GateTests(unittest.TestCase):
    """validate_plan / validate_execution_chain 用的两个闸门函数。"""

    def test_unbound_tool_is_blocked_even_without_study(self):
        self.assertIsNotNone(srv._unproven_combo("gatk_germline_cohort", None))

    def test_bound_combo_passes_and_unbound_study_is_blocked(self):
        gid = rb.bound_tools()[0]
        study = rb.bindings_for(gid)[0]["study"]
        self.assertIsNone(srv._unproven_combo(gid, study))
        self.assertIsNotNone(srv._unproven_combo(gid, "HRA000001"))

    def test_disabled_binding_reports_reason(self):
        for gid, doc in rb.registry().items():
            for b in doc["bindings"]:
                if b.get("disabled"):
                    self.assertTrue(srv._failed_run(gid, b["study"]))
                    self.assertNotIn(b["study"], [x["study"] for x in rb.bindings_for(gid)])
                    return
        self.skipTest("当前没有停用的绑定")

    def test_bulk10_runs_come_from_registry(self):
        for gid, studies in srv._BULK10_RUNS.items():
            self.assertEqual(studies, {b["study"] for b in rb.bindings_for(gid)})

    def test_planning_guide_lists_bindings(self):
        text = srv.tool_get_planning_guide({})["skill"]
        self.assertIn("运行绑定（硬约束）", text)
        for gid in rb.bound_tools():
            self.assertIn(f"- {gid}（", text)


if __name__ == "__main__":
    unittest.main()
