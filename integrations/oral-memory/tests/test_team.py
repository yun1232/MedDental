"""Private/shared separation, image grounding, clinical review and transfer invariants."""
import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from schemas import MemoryError
from store import MemoryStore
from team_memory import MedicalProject
from team_schemas import OUTPUTS


def sample_image():
    im = Image.new('RGB', (80, 60), (160, 90, 100))
    im.putpixel((35, 25), (255, 255, 255))
    buf = io.BytesIO(); im.save(buf, 'PNG')
    return base64.b64encode(buf.getvalue()).decode()


def patient(phenotype=None):
    return {'summary': '口内病变，需核查', 'phenotype': phenotype or ['白色斑块'], 'history': ['持续三个月'],
            'missing': ['触诊', '病理'], 'next_question': '有无检查报告？', 'red_flags': []}


def visual(image_id, descriptor='白色斑块', view=None):
    return {'findings': [{'id': 'f1', 'image_id': image_id, 'description': descriptor,
                         'supporting_views': [view or image_id], 'certainty': 'clear', 'tags': [descriptor]}],
            'regions': [{'image_id': image_id, 'box': [250, 250, 750, 750], 'description': '当前图像可见区域', 'basis': ['当前原图可见表征']}],
            'quality_issues': [], 'mask_sensitivity': []}


def reasoning(refs=None):
    return {'summary': '待医生复核的鉴别评估', 'hypotheses': [{'name': '待查病变', 'support_ids': ['f1'], 'against_ids': [], 'missing': ['病理']}],
            'case_refs': refs or [], 'next_steps': ['由医生安排必要检查'], 'uncertainties': ['病理未知'], 'red_flags': []}


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name)
        self.resources = []
        self.p = self.project('patient-a')

    def tearDown(self):
        for project, store in reversed(self.resources): project.close(); store.close()
        self.temp.cleanup()

    def project(self, identity, role='patient', domain=None):
        store = MemoryStore(self.path/'private.sqlite3', identity, role)
        project = MedicalProject(store, shared_path=self.path/'shared.sqlite3', asset_root=self.path/'images', domain=domain)
        self.resources.append((project, store)); return project

    def start(self, project=None, event='input-1', revision=0, message='原始患者问题', image=True):
        project = project or self.p
        return project.ingest('visit', event, revision, [{'id': event, 'text': message}], [{'data': sample_image()}] if image else [])['run_id']

    def complete(self, project=None, phenotype=None):
        p = project or self.p; run_id = self.start(p)
        image_id = p.state('visit')['images'][0]['image_id']
        p.save_output(run_id, 'patient', patient(phenotype))
        p.retrieve(run_id, 'key1')
        p.save_output(run_id, 'retrieval', {'ranking': [], 'transferable_lessons': [], 'limitations': ['无匹配病例']})
        p.save_output(run_id, 'visual', visual(image_id))
        p.make_views(run_id)
        p.save_output(run_id, 'reasoning', reasoning())
        p.save_output(run_id, 'review', {'route': 'approve', 'issues': [], 'required_changes': ''})
        p.finish(run_id, 'awaiting_doctor')
        return run_id

    def publish_case(self, phenotype=None):
        run_id = self.complete(phenotype=phenotype)
        doctor = self.project('patient-a', 'doctor')
        doctor.review(run_id, 1, True, '医生已核对原图', '医生确认结果')
        image_id = doctor.state('visit')['images'][0]['image_id']
        case = {'phenotype': phenotype or ['白色斑块'], 'context': ['持续三个月'], 'evidence': ['医生确认的可见表征'],
                'experience': '迁移需要补充检查的经验，不能迁移诊断结论', 'pitfalls': ['外观相似不代表病理相同'],
                'outcome': '已复核', 'source_ref': 'authorized-case', 'image_ids': [image_id], 'regions': []}
        published = doctor.publish(run_id, 'case-1', 0, True, True, case)
        return run_id, doctor, case, published

    def test_original_question_image_persist_and_retries_are_idempotent(self):
        run = self.start(); retry = self.start()
        self.assertEqual(run, retry)
        self.assertEqual(self.p.context(run)['case']['question'], '原始患者问题')
        im = self.p.state('visit')['images'][0]
        self.assertTrue(self.p.image(run, im['image_id']).startswith(b'\x89PNG'))
        self.assertEqual(self.p.state('visit')['revision'], 1)
        with self.assertRaises(MemoryError): self.start(message='篡改原始消息')

    def test_missing_image_does_not_create_a_case(self):
        with self.assertRaisesRegex(MemoryError, 'Upload an original'): self.start(image=False)
        self.assertEqual(self.p.state('visit')['revision'], 0)

    def test_two_patient_inputs_invalidate_old_agent_writes(self):
        old = self.start(); self.start(event='input-2', revision=1, message='补充信息', image=False)
        self.assertEqual(self.p.state('visit')['question'], '原始患者问题')
        self.assertEqual(len(self.p.state('visit')['images']), 1)
        with self.assertRaisesRegex(MemoryError, 'changed'): self.p.save_output(old, 'patient', patient())
        with self.assertRaises(MemoryError): self.start()  # even an idempotent retry cannot revive stale work

    def test_doctor_correction_invalidates_old_team_output(self):
        run = self.start(); doc = self.project('patient-a', 'doctor')
        doc.correct('visit', 1, 'correction1', '实际部位为左侧')
        with self.assertRaises(MemoryError): self.p.save_output(run, 'patient', patient())
        self.assertIn('实际部位', self.p.state('visit')['messages'][-1]['text'])

    def test_legacy_patient_turn_invalidates_new_team_output(self):
        from test_workflow import turn
        run = self.start()
        self.p.inquiry.patient_turn(turn('legacy-input', 0))
        # Legacy test helper uses v1, while this consultation uses visit.
        self.assertEqual(self.p.run(run)['legacy_revision'], 0)
        args = turn('legacy-input-2', 0); args['encounter_id'] = 'visit'
        self.p.inquiry.patient_turn(args)
        with self.assertRaises(MemoryError): self.p.save_output(run, 'patient', patient())

    def test_patient_cannot_publish_review_correct_or_reward(self):
        run = self.start()
        for operation in [lambda: self.p.review(run, 1, True, 'note', 'outcome'),
                          lambda: self.p.correct('visit', 1, 'event', 'note'),
                          lambda: self.p.publish(run, 'x', 0, True, True, {}),
                          lambda: self.p.feedback(run, 'x', 1, 'helpful', 'reason')]:
            with self.assertRaisesRegex(MemoryError, 'Doctor host'): operation()

    def test_foreign_patient_run_and_images_are_not_readable(self):
        run = self.start(); other = self.project('patient-b')
        with self.assertRaises(MemoryError): other.run(run)
        with self.assertRaises(MemoryError): other.image(run, self.p.state('visit')['images'][0]['image_id'])
        self.assertEqual(other.state('visit')['images'], [])

    def test_mask_and_crop_have_correct_geometry_and_original_is_immutable(self):
        run = self.start(); image_id = self.p.state('visit')['images'][0]['image_id']
        original = self.p.image(run, image_id)
        self.p.save_output(run, 'visual', visual(image_id)); views = self.p.make_views(run)
        self.assertEqual([v['kind'] for v in views], ['crop', 'mask'])
        crop = Image.open(io.BytesIO(self.p.image(run, views[0]['asset_id'])))
        mask = Image.open(io.BytesIO(self.p.image(run, views[1]['asset_id'])))
        self.assertEqual(crop.size, (40, 30)); self.assertEqual(mask.size, (80, 60))
        self.assertEqual(mask.getpixel((35, 25)), (127, 127, 127))
        self.assertEqual(mask.getpixel((0, 0)), (160, 90, 100))
        self.assertEqual(original, self.p.image(run, image_id))
        self.assertEqual(views[0]['box'], [250, 250, 750, 750])

    def test_mask_only_positive_evidence_and_hallucinated_refs_rejected(self):
        run = self.start(); im = self.p.state('visit')['images'][0]['image_id']
        self.p.save_output(run, 'visual', visual(im)); views = self.p.make_views(run)
        with self.assertRaises(MemoryError): self.p.save_output(run, 'visual', visual(im, view=views[1]['view_id']))
        with self.assertRaises(MemoryError): self.p.save_output(run, 'visual', visual('foreign-image'))
        with self.assertRaises(MemoryError): self.p.save_output(run, 'reasoning', reasoning([{'case_id':'invented', 'version':1}]))
        bad = reasoning(); bad['hypotheses'][0]['support_ids'] = ['invented']
        with self.assertRaises(MemoryError): self.p.save_output(run, 'reasoning', bad)

    def test_second_key_searches_full_library_and_can_find_new_cases(self):
        _, _, _, published = self.publish_case(['红色区域'])
        other = self.project('patient-b'); run = self.start(other)
        other.save_output(run, 'patient', patient(['完全不同']))
        self.assertEqual(other.retrieve(run, 'key1')['cases'], [])
        image_id = other.state('visit')['images'][0]['image_id']
        other.save_output(run, 'visual', visual(image_id, '红色区域'))
        second = other.retrieve(run, 'key2')
        self.assertTrue(second['searched_entire_domain']); self.assertEqual(second['cases'][0]['case_id'], published['case_id'])
        self.assertNotIn('原始患者问题', json.dumps(second, ensure_ascii=False))
        historical = published['images'][0]['image_id']
        self.assertTrue(other.image(run, historical).startswith(b'\x89PNG'))
        self.assertNotEqual(historical, image_id)

    def test_doctor_confirmation_deidentification_version_and_redaction(self):
        run, doc, case, published = self.publish_case()
        self.assertNotIn('patient_id', published); self.assertNotIn('encounter_id', published)
        self.assertNotIn('question', published)
        with self.assertRaises(MemoryError): doc.publish(run, 'case-2', 0, True, False, case)
        with self.assertRaises(MemoryError): doc.publish(run, 'case-1', 0, True, True, case)
        redacted = deepcopy(case)
        redacted['redactions'] = [{'image_id':case['image_ids'][0], 'box':[0,0,100,100], 'description':'身份标识', 'basis':[]}]
        updated = doc.publish(run, 'case-1', 1, True, True, redacted)
        im = Image.open(doc.assets/'shared'/(updated['images'][0]['image_id']+'.png'))
        self.assertEqual(im.getpixel((0,0)), (0,0,0))
        self.assertEqual(doc.shared.execute('SELECT COUNT(*) FROM case_versions').fetchone()[0], 2)

    def test_exposure_does_not_reward_feedback_counts_once_after_doctor_review(self):
        _, doc, _, _ = self.publish_case()
        other = self.project('patient-b'); run = self.complete(other)
        self.assertEqual(other.shared.execute('SELECT helpful FROM cases').fetchone()[0], 0)
        d2 = self.project('patient-b', 'doctor')
        with self.assertRaises(MemoryError): d2.feedback(run, 'case-1', 1, 'helpful', 'reason')
        d2.review(run, 1, True, '核对完毕', '已确认')
        self.assertTrue(d2.feedback(run, 'case-1', 1, 'helpful', 'reason')['recorded'])
        self.assertTrue(d2.feedback(run, 'case-1', 1, 'helpful', 'reason')['duplicate'])
        self.assertEqual(doc.shared.execute('SELECT helpful FROM cases').fetchone()[0], 1)
        with self.assertRaises(MemoryError): d2.feedback(run, 'case-1', 1, 'not_helpful', 'changed')

    def test_approving_audit_requires_all_five_roles(self):
        run = self.start()
        with self.assertRaises(MemoryError): self.p.finish(run, 'awaiting_doctor')
        result = self.p.finish(run, 'needs_human'); self.assertEqual(result['status'], 'needs_human')
        with self.assertRaises(MemoryError): self.p.save_output(run, 'patient', patient())

    def test_disease_profile_isolates_shared_retrieval(self):
        self.publish_case()
        other = self.project('patient-c', domain='general-medical'); run = self.start(other)
        other.save_output(run, 'patient', patient())
        self.assertEqual(other.retrieve(run, 'key1')['cases'], [])

    def test_doctor_review_is_available_to_the_next_patient_consultation(self):
        run = self.complete()
        doc = self.project('patient-a', 'doctor')
        doc.review(run, 1, True, '已确认需要进一步检查', '尚不能确认病理')
        next_run = self.start(event='next-input', revision=1, image=False)
        prior = self.p.context(next_run)['prior_reviews']
        self.assertEqual(prior[0]['doctor_review']['outcome'], '尚不能确认病理')
        other = self.project('another-patient'); other_run = self.start(other)
        self.assertEqual(other.context(other_run)['prior_reviews'], [])

    def test_session_cannot_be_reused_for_another_encounter(self):
        self.p.ingest('visit', 'first-session', 0, [{'id':'m1','text':'问题'}], [{'data':sample_image()}], source_session='session-1')
        with self.assertRaisesRegex(MemoryError, 'new Harness session'):
            self.p.ingest('another-visit', 'another-event', 0, [{'id':'m2','text':'问题'}], [{'data':sample_image()}], source_session='session-1')

    def test_shared_database_does_not_repurpose_private_data(self):
        private = MemoryStore(self.path/'another-private.sqlite3', 'another')
        try:
            with self.assertRaisesRegex(MemoryError, 'new file'):
                MedicalProject(self.p.store, shared_path=self.path/'another-private.sqlite3', asset_root=self.path/'images')
        finally:
            private.close()

    def test_contract_json_matches_python_validation_source(self):
        actual = json.loads((Path(__file__).resolve().parents[1]/'team-contracts.json').read_text())
        self.assertEqual(actual, OUTPUTS)


if __name__ == '__main__': unittest.main()
