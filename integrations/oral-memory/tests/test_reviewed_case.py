"""Doctor judgment automatically creates a retrievable, private, versioned case."""
from unittest.mock import patch
import unittest
from copy import deepcopy
import test_team as team
from test_team import patient, visual, sample_image
from schemas import MemoryError
from clinical_control import stage_value


class ReviewedCaseTests(unittest.TestCase):
    setUp = team.TeamTests.setUp
    tearDown = team.TeamTests.tearDown
    project = team.TeamTests.project
    start = team.TeamTests.start
    complete = team.TeamTests.complete

    def reviewed(self, success=None):
        run_id = self.complete()
        doc = self.project('patient-a', 'doctor')
        run = doc.review(run_id, 1, True, '应补充检查后再判断', '暂不能确认病理', success)
        return doc, run

    def next_visit(self, project, encounter='next-visit'):
        run_id = project.ingest(encounter, encounter, 0,
            [{'id': encounter, 'text': '白色斑块持续三个月，请评估'}],
            [{'data': sample_image()}])['run_id']
        project.save_output(run_id, 'patient', patient())
        return run_id

    def test_review_saves_case_recalled_by_both_stages_without_publication(self):
        doc, run = self.reviewed()
        saved = run['case_memory']
        self.assertEqual(saved['scope'], 'patient')
        self.assertEqual(len(saved['references']), 3)
        self.assertEqual(doc.shared.execute('SELECT COUNT(*) FROM cases').fetchone()[0], 0)
        anchor = doc.store.get({**{k: saved['anchor'][k] for k in ('category', 'record_id')},
                                'include_versions': False})['record']
        self.assertEqual(anchor['pathology']['status'], 'unknown')
        self.assertIn('患者陈述', anchor['visual_description'])
        self.assertIn('模型图像观察', anchor['visual_description'])
        self.assertEqual(anchor['visual'][0]['review_status'], 'unreviewed')
        history = next(r for r in doc.store.records('experience') if r['record_id'].endswith('-history'))
        self.assertEqual((history['source'], history['review_status']), ('patient', 'unreviewed'))
        later = self.next_visit(self.p)
        first = self.p.retrieve(later, 'key1')
        case = next(c for c in first['cases'] if c['memory_reference'] == saved['anchor'])
        self.assertEqual(case['memory_scope'], 'patient')
        self.assertIn('暂不能确认病理', case['outcome'])
        self.assertIn('应补充检查', case['experience'])
        self.assertEqual(case['images'][0]['image_id'], saved['image_ids'][0])
        self.assertTrue(case['regions'])
        self.p.save_output(later, 'visual', visual(self.p.state('next-visit')['images'][0]['image_id']))
        second = self.p.retrieve(later, 'key2')
        self.assertTrue(any(c['memory_reference'] == saved['anchor'] and c['phenotype_score'] > 0
                            for c in second['cases']))
        other = self.project('other-patient')
        self.assertFalse(other.retrieve(self.next_visit(other), 'key1')['cases'])

    def test_unknown_and_later_failure_are_stored_without_self_penalty(self):
        doc, run = self.reviewed()
        ref = run['case_memory']['anchor']
        before = doc.store.get({'category': 'care',
            'record_id': next(r['record_id'] for r in run['case_memory']['references'] if r['category'] == 'care'),
            'include_versions': True})
        self.assertIn('尚未判定', before['record']['outcome'])
        changed = doc.outcome(run['run_id'], 1, True, False, '随访确认未达到评估目标')
        self.assertEqual(changed['case_memory']['anchor'], ref)
        after = doc.store.get({'category': 'care', 'record_id': before['record']['record_id'], 'include_versions': True})
        self.assertEqual(after['record']['version'], 2)
        self.assertEqual(len(after['versions']), 2)
        self.assertIn('失败', after['record']['outcome'])
        self.assertIn('尚未判定', after['versions'][0]['record']['outcome'])
        self.assertEqual(stage_value(doc.store, ref, 'visual'), 1)
        self.assertEqual(stage_value(doc.store, ref, 'diagnosis'), 1)

    def test_retries_do_not_duplicate_cases_and_unconfirmed_or_patient_review_cannot_write(self):
        run_id = self.complete()
        doc = self.project('patient-a', 'doctor')
        for p, confirmed in ((self.p, True), (doc, False)):
            with self.assertRaises(MemoryError): p.review(run_id, 1, confirmed, '意见', '判断')
        self.assertFalse(any(r['record_id'].startswith('reviewed-case-') for r in doc.store.records('lesion')))
        saved = doc.review(run_id, 1, True, '意见', '判断')
        counts = [doc.db.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0]
                  for t in ('revisions', 'memory_visual_revisions')]
        self.assertEqual(doc.review(run_id, 1, True, '意见', '判断'), saved)
        self.assertEqual(counts, [doc.db.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0]
                                 for t in ('revisions', 'memory_visual_revisions')])
        with self.assertRaises(MemoryError): doc.review(run_id, 1, True, '不同意见', '不同判断')
        anchor = doc.store.get({'category': 'lesion', 'record_id': saved['case_memory']['anchor']['record_id'],
                                'include_versions': False})['record']
        from schemas import RECORDS
        record = {k: deepcopy(anchor[k]) for k in RECORDS['lesion']['properties'] if k in anchor}
        with self.assertRaises(MemoryError): self.p.store.put('lesion', {'record': record, 'expected_version': 1})

    def test_case_and_review_roll_back_together(self):
        run_id = self.complete()
        doc = self.project('patient-a', 'doctor')
        with patch.object(doc.store, 'link_visual', side_effect=MemoryError('synthetic_failure', 'cannot link')):
            with self.assertRaises(MemoryError): doc.review(run_id, 1, True, '意见', '判断')
        self.assertEqual(doc.run(run_id)['status'], 'awaiting_doctor')
        self.assertIsNone(doc.run(run_id)['doctor_review'])
        self.assertFalse(any(r['record_id'].startswith('reviewed-case-') for r in doc.store.records('lesion')))
        self.assertFalse(doc.store.records('doctor'))

    def test_later_review_versions_the_same_case_and_retains_earlier_snapshot(self):
        doc, first = self.reviewed()
        old = first['case_memory']['anchor']
        doc.correct('visit', 1, 'new-fact', '医生补充事实')
        second = self.p.ingest('visit', 'reevaluation', 2,
            [{'id': 'reevaluation', 'text': '请结合医生补充信息重新评估'}], [])['run_id']
        intake = patient(['白色斑块，补充新的表面描述'])
        intake['history'] = []
        self.p.save_output(second, 'patient', intake)
        self.p.finish(second, 'needs_human')
        current = doc.review(second, 3, True, '复核新信息', '需要进一步随访')
        new = current['case_memory']['anchor']
        self.assertEqual(new['record_id'], old['record_id'])
        self.assertEqual(new['version'], old['version'] + 1)
        versions = doc.store.get({'category': 'lesion', 'record_id': old['record_id'], 'include_versions': True})
        self.assertEqual(len(versions['versions']), 2)
        self.assertNotIn('补充新的表面描述', versions['versions'][0]['record']['visual_description'])
        self.assertIn('补充新的表面描述', versions['record']['visual_description'])
        history = doc.store.get({'category': 'experience', 'record_id': old['record_id'] + '-history',
                                 'include_versions': True})
        self.assertEqual(history['record']['description'], 'unknown')
        self.assertEqual(history['versions'][0]['record']['description'], '持续三个月')
        self.assertEqual(doc.run(first['run_id'], current=False)['case_memory']['anchor'], old)

    def test_doctor_judgment_before_visual_completion_still_saves_private_case(self):
        run_id = self.start()
        self.p.save_output(run_id, 'patient', patient())
        self.p.finish(run_id, 'needs_input')
        doc = self.project('patient-a', 'doctor')
        saved = doc.review(run_id, 1, True, '需要进一步检查', '尚不能确诊')
        later = self.next_visit(self.p)
        found = self.p.retrieve(later, 'key1')
        case = next(c for c in found['cases'] if c['memory_reference'] == saved['case_memory']['anchor'])
        self.assertTrue(case['images'])
        self.assertEqual(case['regions'], [])
        self.assertIn('尚不能确诊', case['outcome'])


if __name__ == '__main__':
    unittest.main()
