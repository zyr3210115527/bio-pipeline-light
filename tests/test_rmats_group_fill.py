"""Focused regressions for RNA-only, role-separated rMATS cohort filling."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mcp_light_server as srv
import cohort_adapter as adapter

class RmatsGroupFillTests(unittest.TestCase):
    def test_roles_duplicates_ambiguity_and_cap(self):
        roles = {'normal':['HRR1','HRR3','HRR9'], 'tumor':['HRR2','HRR4','HRR9']}
        rows = [['HRR1Aligned.sortedByCoord.out.bam','/analysis_bak/HRR1.bam','HRR1'],
                ['HRR1Aligned.sortedByCoord.out.bam','/analysis/HRR1.bam','HRR1'],
                ['HRR2Aligned.sortedByCoord.out.bam','/analysis/HRR2.bam','HRR2'],
                ['HRR3Aligned.sortedByCoord.out.bam','/analysis/HRR3.bam','HRR3'],
                ['HRR4Aligned.sortedByCoord.out.bam','/analysis/HRR4.bam','HRR4'],
                ['HRR9Aligned.sortedByCoord.out.bam','/analysis/HRR9.bam','HRR9'],
                ['HRR5Aligned.sortedByCoord.out.bam','/analysis/HRR5.bam','HRR5']]
        with patch.object(srv,'_study_run_lists',return_value=roles) as role_query, patch.object(srv,'neo4j_q',return_value=[rows]) as query:
            result = srv._rmats_group_bams('HRA001272')
            self.assertEqual(result,{'group1_bams':['/analysis/HRR1.bam','/analysis/HRR3.bam'],
                                     'group2_bams':['/analysis/HRR2.bam','/analysis/HRR4.bam']})
            role_query.assert_called_with('HRA001272',strategy='bulk_RNA')
            self.assertIn("ENDS WITH 'Aligned.sortedByCoord.out.bam'",query.call_args.args[0][0])
            with patch.object(srv,'_RUN_LIST_CAP',1):
                result = srv._rmats_group_bams('HRA001272')
                self.assertEqual([len(v) for v in result.values()],[1,1])

    def test_groups_do_not_intersect_by_bound_run(self):
        with patch.object(srv,'_rmats_group_bams',return_value={'group1_bams':['/n.bam'],'group2_bams':['/t.bam']}):
            entries = srv._cohort_fill_entries('rmats_alternative_splicing','HRA001272',{'HRR1'})
            self.assertEqual(entries['group2_bams'],[(frozenset(),'/t.bam')])

    def bind(self, groups, explicit=None):
        assets = [{'asset_id':'asset-1','file_name':'representative.bam','path':'/representative.bam',
                   'file_path':'/representative.bam','artifact_type':'bam','_fmt':'bam','study_accession':'HRA001272'}]
        with patch.object(srv,'_rmats_group_bams',return_value=groups), patch.object(srv,'_resolve_id_params',return_value=({},[])):
            return adapter._bind_step(srv,'rmats_alternative_splicing',srv.KC_MAP['rmats_alternative_splicing'],assets,[],'step-1',explicit_inputs=explicit),assets

    def test_representative_file_is_replaced_by_two_role_groups(self):
        (inputs,missing),assets = self.bind({'group1_bams':['/n1.bam','/n2.bam'],'group2_bams':['/t1.bam','/t2.bam']})
        self.assertFalse(missing)
        by_id = {a['asset_id']:a['path'] for a in assets}
        self.assertEqual([by_id[b['asset_id']] for b in inputs['group1_bams']],['/n1.bam','/n2.bam'])
        self.assertEqual([by_id[b['asset_id']] for b in inputs['group2_bams']],['/t1.bam','/t2.bam'])

    def test_empty_role_is_reported_not_filled_from_other_group(self):
        (inputs,missing),_ = self.bind({'group1_bams':['/n.bam'],'group2_bams':[]})
        self.assertNotIn('group2_bams',inputs)
        self.assertTrue(any(m['param']=='group2_bams' for m in missing))

    def test_explicit_group_is_not_replaced(self):
        (inputs,missing),assets = self.bind({'group1_bams':['/n.bam'],'group2_bams':['/t.bam']},
                                           {'group1_bams':[{'file_path':'/representative.bam'}]})
        self.assertFalse(missing)
        self.assertEqual(inputs['group1_bams'],[{'asset_id':'asset-1'}])
        by_id = {a['asset_id']:a['path'] for a in assets}
        self.assertEqual([by_id[b['asset_id']] for b in inputs['group2_bams']],['/t.bam'])

    def test_gene_boxplot_intent(self):
        self.assertFalse(adapter._prefer_gene_boxplot('MIR503HG（ENSG00000223749）的异常高表达是否对应稳定的共表达模块及相关生物学功能？'))
        self.assertTrue(adapter._prefer_gene_boxplot('MIR503HG 在食管鳞癌组织中的表达是升高还是降低？'))
        self.assertFalse(adapter._prefer_gene_boxplot('食管癌差异表达分析'))
        self.assertFalse(adapter._prefer_gene_boxplot('TP53 突变与生存分析'))

if __name__=='__main__':unittest.main()
