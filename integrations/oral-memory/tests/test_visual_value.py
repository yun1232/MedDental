"""Regional visual evidence is scored by the review Agent, never by the doctor."""
import unittest
from copy import deepcopy
import test_clinical_control as clinical
import test_team as team
from test_team import patient, sample_image
from test_fusion import ranking
from test_team import visual
from schemas import MemoryError
from clinical_control import stage_value


class VisualValueTests(unittest.TestCase):
    setUp = clinical.ClinicalControlTests.setUp
    tearDown = clinical.ClinicalControlTests.tearDown
    project = clinical.ClinicalControlTests.project
    start = clinical.ClinicalControlTests.start
    complete = clinical.ClinicalControlTests.complete
    setup_visual = clinical.ClinicalControlTests.setup_visual
    visual_review = clinical.ClinicalControlTests.visual_review

    def test_aggregated_summary_cannot_replace_a_regional_judgment(self):
        run, case, local = self.setup_visual()
        aggregate = deepcopy(local)
        aggregate['findings'][0]['id'] = 'aggregate-only'
        aggregate['contributions'][0]['finding_id'] = 'aggregate-only'
        self.p.save_output(run, 'visual', aggregate)
        judgment = self.visual_review(run, case)
        bad = deepcopy(judgment)
        bad['assessments'][0]['finding_refs'][0]['finding_id'] = 'aggregate-only'
        with self.assertRaises(MemoryError): self.p.save_visual_value_review(run, bad)
        saved = self.p.save_visual_value_review(run, judgment)
        self.assertEqual(saved['source'], 'review_agent')
        self.assertEqual(saved['results'][0]['value'], 1)
        self.assertIsNone(self.p.run(run)['doctor_review'])

    def test_uncertain_judgment_does_not_deduct_either_stage(self):
        run, case, _ = self.setup_visual()
        result = self.p.save_visual_value_review(run, self.visual_review(run, case, 'uncertain'))
        self.assertFalse(result['results'][0]['scored'])
        self.assertEqual(stage_value(self.p.store, case['memory_reference'], 'visual'), 1)
        self.assertEqual(stage_value(self.p.store, case['memory_reference'], 'diagnosis'), 1)
        self.assertEqual(self.p.db.execute('SELECT COUNT(*) FROM stage_verdicts').fetchone()[0], 0)

    def test_completed_regional_work_cannot_skip_the_automated_audit(self):
        run, case, _ = self.setup_visual()
        with self.assertRaises(MemoryError): self.p.finish(run, 'needs_input')
        self.p.save_visual_value_review(run, self.visual_review(run, case, 'uncertain'))
        self.assertEqual(self.p.finish(run, 'needs_input')['status'], 'needs_input')

    def test_shared_reference_value_updates_without_exposing_personal_case(self):
        source, doctor, body, _ = team.TeamTests.publish_case(self)
        original = doctor.state('visit')['images'][0]['image_id']
        body['regions'] = visual(original)['regions']
        doctor.publish(source, 'case-1', 1, True, True, body)
        other = self.project('another-patient')
        run = self.start(other)
        other.save_output(run, 'patient', patient())
        candidates = other.retrieve(run, 'key1')
        case = next(c for c in candidates['cases'] if c['case_id'] == 'case-1')
        other.save_output(run, 'retrieval', ranking(candidates['cases'], {}))
        image = other.state('visit')['images'][0]['image_id']
        other.save_baseline(run, visual(image))
        proposal = visual(image)
        proposal['regions'][0]['source_cases'] = [{'case_id': case['case_id'], 'version': case['version']}]
        other.save_output(run, 'visual', proposal)
        pair = other.make_views(run)
        other.save_output(run, 'visual', visual(image), region_view_ids=[v['view_id'] for v in pair])
        task = other.context(run)['visual_value_tasks'][0]
        value = {'summary': '未发现区域引导的新信息', 'assessments': [{
            'case_id': case['case_id'], 'version': case['version'], 'verdict': 'no_new_information',
            'review_steps': [r['step'] for r in task['regions']], 'finding_refs': [], 'reason': '区域判断与独立基线相同'}]}
        self.assertEqual(other.save_visual_value_review(run, value)['results'][0]['value'], .9)
        self.assertEqual(stage_value(other.library, case['memory_reference'], 'diagnosis'), 1)
        self.assertFalse(other.store.records('doctor'))

    def test_omitting_or_inventing_a_tested_case_is_rejected_before_any_update(self):
        run, case, _ = self.setup_visual()
        judgment = self.visual_review(run, case, 'no_new_information')
        missing = deepcopy(judgment); missing['assessments'] = []
        invented = deepcopy(judgment); invented['assessments'][0]['case_id'] = 'not-used'
        for invalid in (missing, invented):
            with self.assertRaises(MemoryError): self.p.save_visual_value_review(run, invalid)
        self.assertEqual(stage_value(self.p.store, case['memory_reference'], 'visual'), 1)

    def test_all_independent_regions_must_be_reviewed_and_ids_are_step_scoped(self):
        run, case, output = self.setup_visual()
        proposal = deepcopy(output)
        proposal['contributions'] = []
        proposal['regions'][0]['source_cases'] = [{'case_id': case['case_id'], 'version': case['version']}]
        proposal['regions'][0]['box'] = [0, 0, 200, 200]
        self.p.save_output(run, 'visual', proposal)
        pair = self.p.make_views(run)
        local = visual(pair[0]['image_id'], '另一区域的独立观察', pair[0]['view_id'])
        self.p.save_output(run, 'visual', local, region_view_ids=[v['view_id'] for v in pair])
        judgment = self.visual_review(run, case)
        self.assertEqual(len(judgment['assessments'][0]['review_steps']), 2)
        incomplete = deepcopy(judgment); incomplete['assessments'][0]['review_steps'].pop()
        with self.assertRaises(MemoryError): self.p.save_visual_value_review(run, incomplete)
        wrong_step = deepcopy(judgment)
        wrong_step['assessments'][0]['finding_refs'][0]['step'] = judgment['assessments'][0]['review_steps'][-1]
        with self.assertRaises(MemoryError): self.p.save_visual_value_review(run, wrong_step)
        self.assertEqual(self.p.save_visual_value_review(run, judgment)['results'][0]['value'], 1)

    def test_local_worker_must_be_bound_to_the_actual_crop_mask_pair(self):
        run, case, output = self.setup_visual()
        views = self.p.run(run)['views']
        for invalid in ([views[0]['view_id']], [views[0]['view_id'], 'invented']):
            with self.assertRaises(MemoryError):
                self.p.save_output(run, 'visual', output, region_view_ids=invalid)
        self.assertEqual(len(self.p.run(run)['region_reviews']), 1)

    def test_worker_without_new_baseline_evidence_cannot_receive_positive_value(self):
        run, case, output = self.setup_visual()
        same = deepcopy(output); same['findings'][0]['description'] = '原始白色区域'
        pair = [v['view_id'] for v in self.p.run(run)['views']]
        self.p.save_output(run, 'visual', same, region_view_ids=pair)
        judgment = self.visual_review(run, case)
        self.assertFalse(judgment['assessments'][0]['finding_refs'])
        with self.assertRaises(MemoryError): self.p.save_visual_value_review(run, judgment)
        judgment['assessments'][0]['verdict'] = 'no_new_information'
        self.assertEqual(self.p.save_visual_value_review(run, judgment)['results'][0]['value'], .9)

    def test_legacy_region_outputs_are_not_retroactively_scored(self):
        run = self.complete()
        self.assertEqual(self.p.context(run)['visual_value_tasks'], [])
        self.assertNotIn('visual_value_review', self.p.run(run))


if __name__ == '__main__':
    unittest.main()
