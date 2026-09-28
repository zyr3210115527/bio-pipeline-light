"""CNVkit WDL contract regressions; all graph calls are isolated fixtures."""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mcp_light_server as srv
import cohort_adapter as adapter

GID = "cnvkit_cnv_clinical"


class CnvkitContractTests(unittest.TestCase):
    def setUp(self):
        self.strategy = "WGS"
        self.patches = [
            patch.object(srv, "neo4j_q", side_effect=AssertionError("unexpected graph call")),
            patch.object(srv, "_file_sample_facts", side_effect=self.facts),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def facts(self, names):
        return {name: {"run_accession": "HRR1" if name.startswith("tumor") else "HRR2",
                       "role": "tumor" if name.startswith("tumor") else "normal",
                       "strategy": self.strategy} for name in names}

    def bindings(self, **extra):
        b = {"sample_ids": ["HRR1"]}
        for slot in ("tumor_bams", "tumor_bais", "normal_bams", "normal_bais"):
            filename = slot[:-1].replace("_", ".")
            b[slot] = [{"file_name": filename, "file_path": "/fixture/" + filename}]
        b.update(extra)
        return b

    def validate(self, **extra):
        return srv.tool_validate_execution_chain({"steps": [{"tool_id": GID, "inputs": self.bindings(**extra)}]})

    def adapted(self, **explicit):
        assets = []
        for name in ("tumor.bam", "normal.bam", "tumor.bai", "normal.bai"):
            assets.append({"asset_id": name, "file_name": name, "path": "/fixture/" + name,
                           "file_path": "/fixture/" + name, "_fmt": name.rsplit(".", 1)[1],
                           "artifact_type": name.rsplit(".", 1)[1], "study_accession": None})
        inputs, missing = adapter._bind_step(srv, GID, srv.KC_MAP[GID], assets, [], "step-1", explicit_inputs=explicit)
        params, gaps = adapter._flat_params(srv, GID, inputs, {a["asset_id"]: a for a in assets}, GID, "step-1")
        return inputs, params, missing + gaps

    def test_card_matches_delivered_wdl(self):
        card = srv.KC_MAP[GID]
        slots = {p["name"]: p for p in card["inputs"]}
        self.assertNotIn("clinical_metadata", slots)
        self.assertNotIn("run_clinical_association", slots)
        self.assertEqual(slots["tumor_ploidies"]["type"], "Array[Int]")
        self.assertEqual(slots["targets_bed"]["type"], "File?")
        self.assertTrue(all(p["type"] == "Array[File]" for p in card["outputs"]))
        self.assertNotIn("clinical_metadata", srv._needs_clinical(GID))

    def test_wgs_no_bed_is_submittable(self):
        r = self.validate()
        self.assertTrue(r["submittable"], r)
        self.assertEqual(r["execution_params"]["assay_type"], "wgs")

    def test_wes_infers_hybrid_and_requires_bed(self):
        self.strategy = "WES"
        r = self.validate()
        self.assertFalse(r["submittable"])
        self.assertEqual(r["execution_params"]["assay_type"], "hybrid")
        self.assertIn("targets_bed", [i["param"] for i in r["execution_params_missing"]])

    def test_wes_with_bed_is_submittable(self):
        self.strategy = "WES"
        r = self.validate(targets_bed={"file_path": "/fixture/capture.bed"})
        self.assertTrue(r["submittable"], r)

    def test_explicit_mode_is_preserved_and_checked(self):
        for mode in ("hybrid", "amplicon"):
            with self.subTest(mode=mode):
                r = self.validate(assay_type=mode)
                self.assertFalse(r["submittable"])
                self.assertEqual(r["execution_params"]["assay_type"], mode)
                self.assertTrue(self.validate(assay_type=mode, targets_bed={"file_path": "/fixture/capture.bed"})["submittable"])

    def test_legacy_wes_mode_is_normalized(self):
        r = self.validate(assay_type="wes", targets_bed={"file_path": "/fixture/capture.bed"})
        self.assertTrue(r["submittable"], r)
        self.assertEqual(r["execution_params"]["assay_type"], "hybrid")

    def test_invalid_mode_is_blocked(self):
        r = self.validate(assay_type="rna")
        self.assertFalse(r["submittable"])

    def test_numeric_arrays_survive_contract(self):
        r = self.validate(tumor_purities=[0.7], tumor_ploidies=[2])
        self.assertTrue(r["submittable"], r)
        self.assertEqual(r["execution_params"]["tumor_purities"], [0.7])
        self.assertEqual(r["execution_params"]["tumor_ploidies"], [2])

    def test_non_integer_ploidies_blocked(self):
        for values in ([2.5], [True], "2"):
            with self.subTest(values=values):
                self.assertFalse(self.validate(tumor_purities=[0.7], tumor_ploidies=values)["submittable"])

    def test_purity_ploidy_pair_and_lengths(self):
        for extra in ({"tumor_purities": [0.7]}, {"tumor_ploidies": [2]},
                      {"tumor_purities": [0.7, 0.8], "tumor_ploidies": [2, 2]}):
            with self.subTest(extra=extra):
                self.assertFalse(self.validate(**extra)["submittable"])
        self.assertTrue(self.validate(tumor_purities=[], tumor_ploidies=[])["submittable"])

    def test_bam_id_lengths_must_match(self):
        self.assertFalse(self.validate(sample_ids=["HRR1", "HRR3"])["submittable"])

    def test_unsupported_clinical_fields_not_emitted(self):
        r = self.validate(clinical_metadata={"file_path": "/fixture/clinical.csv"}, run_clinical_association=True)
        self.assertTrue(r["submittable"], r)
        self.assertNotIn("clinical_metadata", r["execution_params"])
        self.assertNotIn("run_clinical_association", r["execution_params"])

    def test_adapter_wgs_and_wes_share_validation(self):
        inputs, params, gaps = self.adapted()
        self.assertFalse(gaps, gaps)
        self.assertEqual(params["assay_type"], "wgs")
        self.strategy = "WES"
        inputs, params, gaps = self.adapted()
        self.assertEqual(params["assay_type"], "hybrid")
        self.assertEqual(inputs["assay_type"], {"value": "hybrid"})
        self.assertIn("targets_bed", [g["param"] for g in gaps])

    def test_adapter_explicit_numeric_arrays_and_mode(self):
        inputs, params, gaps = self.adapted(assay_type={"value": "amplicon"}, tumor_purities=[0.8], tumor_ploidies=[2])
        self.assertEqual(params["assay_type"], "amplicon")
        self.assertEqual(params["tumor_ploidies"], [2])
        self.assertIn("targets_bed", [g["param"] for g in gaps])

    def test_adapter_keeps_deferred_bed_binding(self):
        inputs = {"assay_type": {"value": "hybrid"}, "targets_bed": {"from": {"step_id": "step-1", "output": "targets"}}}
        params, gaps = adapter._flat_params(srv, GID, inputs, {}, GID, "step-2")
        self.assertFalse(gaps, gaps)
        self.assertNotIn("targets_bed", params)

    def test_other_pipeline_not_subject_to_cnvkit_rules(self):
        params, gaps = adapter._flat_params(srv, "other_pipeline", {"assay_type": {"value": "wes"}}, {}, "other_pipeline", "step-1")
        self.assertEqual(params, {"assay_type": "wes"})
        self.assertFalse(gaps)


if __name__ == "__main__":
    unittest.main()
