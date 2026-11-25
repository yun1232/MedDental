"""Executable controller routes, evidence novelty, stage independence and lesson isolation."""
from copy import deepcopy
import unittest
import test_team as team
from test_team import patient, visual, reasoning
from test_memory import lesion, narrative
from test_fusion import ranking
from extensions import reference
from schemas import MemoryError
from clinical_control import stage_value, select_diverse
from store import settings_from_env


class ClinicalControlTests(unittest.TestCase):
    setUp = team.TeamTests.setUp
    tearDown = team.TeamTests.tearDown
    project = team.TeamTests.project
    start = team.TeamTests.start
    complete = team.TeamTests.complete

    def setup_visual(self):
        s = self.p.store
        r = s.put('lesion', {'record': lesion('old', 'old-visit'), 'expected_version': 0})['record']
        s.put('experience', {'record': narrative('experience', 'history-old', 'old-visit', description='持续三个月', links=['old']), 'expected_version': 0})
        # Historical region must exist, and belongs to an admitted image.
        from test_team import sample_image
        image = self.p._save_image(sample_image(), self.p._private_dir())
        s.link_visual(reference(r), image, visual(image['image_id'])['regions'], [], 'old', 'bind-old')
        run = self.start()
        im = self.p.state('visit')['images'][0]['image_id']
        self.p.save_output(run, 'patient', patient())
        found = self.p.retrieve(run, 'key1')
        self.p.save_output(run, 'retrieval', ranking(found['cases'], {'old': 90}))
        c = found['cases'][0]
        baseline = visual(im, '原始白色区域')
        self.p.save_baseline(run, baseline)
        proposal = visual(im, '候选观察')
        proposal['regions'][0]['source_cases'] = [{'case_id': c['case_id'], 'version': c['version']}]
        self.p.save_output(run, 'visual', proposal)
        views = self.p.make_views(run)
        output = visual(im, '当前裁剪可见新增表面细节', views[0]['view_id'])
        output['contributions'] = [{'case_id': c['case_id'], 'version': c['version'], 'view_id': views[0]['view_id'], 'finding_id': 'f1'}]
        self.p.save_output(run, 'visual', output)
        return run, c, output

    def test_independent_stage_verdicts_and_retry(self):
        run, c, _ = self.setup_visual()
        doc = self.project('patient-a', 'doctor')
        result = doc.stage_feedback(run, c['case_id'], 1, 'visual', False, '区域未提供有效新增信息')
        self.assertEqual(result['value'], .9)
        self.assertEqual(stage_value(doc.store, c['memory_reference'], 'diagnosis'), 1)
        self.assertTrue(doc.stage_feedback(run, c['case_id'], 1, 'visual', False, '区域未提供有效新增信息')['duplicate'])
        self.assertEqual(stage_value(doc.store, c['memory_reference'], 'visual'), .9)
        with self.assertRaises(MemoryError): self.p.stage_feedback(run, c['case_id'], 1, 'visual', False, '模型不能自评')

    def test_new_information_needs_crop_lineage_and_doctor_confirmation(self):
        run, c, out = self.setup_visual()
        self.assertEqual(len(self.p.run(run)['visual_gains']), 1)
        doc = self.project('patient-a', 'doctor')
        with self.assertRaises(MemoryError): doc.stage_feedback(run, c['case_id'], 1, 'visual', True, '新增', ['invented'])
        self.assertEqual(doc.stage_feedback(run, c['case_id'], 1, 'visual', True, '已核对新增细节', ['f1'])['value'], 1)
        bad = deepcopy(out); bad['contributions'][0]['view_id'] = self.p.run(run)['views'][1]['view_id']
        with self.assertRaises(MemoryError): self.p.save_output(run, 'visual', bad)
        same = deepcopy(out); same['findings'][0]['description'] = '原始白色区域'
        self.p.save_output(run, 'visual', same)
        self.assertEqual(self.p.run(run)['visual_gains'], [])

    def test_success_is_doctor_outcome_not_audit_approval(self):
        run, c, _ = self.setup_visual()
        second = self.p.retrieve(run, 'key2')
        self.p.save_output(run, 'retrieval', ranking(second['cases'], {'old': 80}))
        self.p.save_output(run, 'reasoning', reasoning())
        self.p.save_output(run, 'review', {'route': 'approve', 'issues': [], 'required_changes': ''})
        self.p.finish(run, 'awaiting_doctor')
        doc = self.project('patient-a', 'doctor')
        with self.assertRaises(MemoryError): doc.stage_feedback(run, c['case_id'], 1, 'diagnosis', True, '不能用审查通过代替成功')
        doc.review(run, 1, True, '已核查', '本次评估未成功', success=False)
        self.assertEqual(doc.stage_feedback(run, c['case_id'], 1, 'diagnosis', False, '医生确认失败')['value'], .9)
        self.assertEqual(stage_value(doc.store, c['memory_reference'], 'visual'), 1)
        with self.assertRaises(MemoryError): doc.stage_feedback(run, c['case_id'], 1, 'diagnosis', True, '反向伪造')

    def test_case_driven_control_and_audit_cannot_be_bypassed(self):
        run, c, _ = self.setup_visual()
        decision = {'action': 'ask', 'question': '既往检查结果是什么？', 'reason': '相似病例的检查结果影响适用性',
                    'gaps': [{'field': 'prior_examination', 'why': '比较检查证据', 'case_refs': [{'case_id': c['case_id'], 'version': 1}]}]}
        self.p.control(run, 'after_key1', decision)
        self.assertEqual(self.p.run(run)['controls'][0]['value']['action'], 'ask')
        bad = deepcopy(decision); bad['gaps'][0]['case_refs'][0]['case_id'] = 'fake'
        with self.assertRaises(MemoryError): self.p.control(run, 'after_key1', bad)
        with self.assertRaises(MemoryError): self.p.control(run, 'after_review', {**decision, 'action': 'finish'})

    def test_baseline_cannot_borrow_historical_or_mask_evidence(self):
        run, c, _ = self.setup_visual()
        with self.assertRaises(MemoryError): self.p.save_baseline(run, visual(self.p.state('visit')['images'][0]['image_id']))
        self.assertNotIn('visual_baseline', self.p.context(run)['case'])

    def test_lessons_require_review_and_preserve_patient_boundary(self):
        run = self.complete()
        doc = self.project('patient-a', 'doctor')
        lesson = {'condition': '持续三个月', 'mistake': '漏问检查', 'correction': '先核对检查结果', 'action': 'ask', 'limits': '仅作问询经验，不推断病理'}
        with self.assertRaises(MemoryError): doc.lesson_submit(run, 'lesson1', 0, True, lesson)
        doc.review(run, 1, True, '医生纠正', '需补充检查')
        with self.assertRaises(MemoryError): doc.lesson_submit(run, 'lesson1', 0, False, lesson)
        doc.lesson_submit(run, 'lesson1', 0, True, lesson)
        self.assertEqual(self.p.lessons('持续三个月')[0]['correction'], lesson['correction'])
        other = self.project('other')
        self.assertEqual(other.lessons('持续三个月'), [])
        with self.assertRaises(MemoryError): doc.lesson_submit(run, 'shared', 0, True, lesson, shared=True)
        doc.lesson_submit(run, 'shared', 0, True, lesson, shared=True, deidentified=True)
        self.assertEqual(other.lessons('持续三个月')[0]['id'], 'shared')
        self.assertNotEqual(other.lessons('持续三个月')[0]['source_run'], run)
        with self.assertRaises(MemoryError): doc.lesson_submit(run, 'lesson1', 0, True, lesson)

    def test_unknown_outcome_can_be_confirmed_later_without_automatic_penalty(self):
        run = self.complete()
        doc = self.project('patient-a', 'doctor')
        doc.review(run, 1, True, '已核查但等待结果', '尚未判定')
        self.assertIsNone(doc.run(run)['doctor_review']['success'])
        known = doc.outcome(run, 1, True, False, '随访确认本次未成功')
        self.assertIs(known['doctor_review']['success'], False)
        self.assertEqual(doc.outcome(run, 1, True, False, '重复请求'), known)
        with self.assertRaises(MemoryError): doc.outcome(run, 1, True, True, '不允许静默反转')

    def test_near_duplicates_do_not_fill_all_slots_but_best_score_is_kept(self):
        cases = [{'case_id': str(i), 'version': 1, 'phenotype': [v], 'context': [], 'experience': '', 'outcome': ''} for i, v in enumerate(['白色斑块', '白色斑块', '红色区域'])]
        scores = [{'case_id': str(i), 'version': 1, 'final_score': v} for i, v in enumerate([90, 89, 85])]
        selected, omitted = select_diverse(scores, cases, 3)
        self.assertEqual([r['case_id'] for r in selected], ['0', '2'])
        self.assertEqual(omitted[0]['case_id'], '1')

    def test_patient_decide_persists_a_grounded_question_and_is_idempotent(self):
        from workflow import PatientCommunicationAgent
        from test_workflow import turn
        s = self.p.store
        s.settings['case_driven'] = True
        s.put('experience', {'record': narrative('experience', 'history', 'old', description='持续三个月'), 'expected_version': 0})
        inquiry = PatientCommunicationAgent(s)
        args = turn('new-turn', 0); args['encounter_id'] = 'visit'
        result = inquiry.patient_turn(args)
        self.assertEqual(result['next_action']['action'], 'decide_from_cases')
        receipt = s.retrieve({'purpose': 'visual', 'patient_experience_query': '持续三个月', 'lesion_query': {'text': 'unknown'}, 'care_query': '', 'experience_query': '', 'doctor_query': '', 'top_k': 5})
        args = {'encounter_id': 'visit', 'event_id': 'chosen', 'expected_revision': 1, 'retrieval_id': receipt['retrieval_id'],
                'question': '请补充既往检查结果？', 'field': 'prior_examination', 'case_refs': receipt['delivered_refs'][:1]}
        chosen = inquiry.patient_decide(args)
        self.assertEqual(chosen['next_action']['question'], args['question'])
        self.assertEqual(inquiry.patient_decide(args), chosen)
        self.assertEqual(inquiry.case_state({'encounter_id': 'visit'})['state']['pending_question']['case_refs'], args['case_refs'])


if __name__ == '__main__': unittest.main()
