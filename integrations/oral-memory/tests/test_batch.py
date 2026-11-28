"""Split/label isolation, actual memory teacher writeback, frozen evaluation and resume."""
import base64
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from openpyxl import Workbook
from batch_data import public_case, read_cases, split_cases, treatment_metrics
from batch import prepare, phase_run, get_snapshot, tree_hash, read_json
from store import MemoryStore
from team_memory import MedicalProject
from test_team import patient, visual, reasoning


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'pic').mkdir()
        book = Workbook()
        page = book.active
        page.append(['编号', '既往史', '分布', '色调', '文件名', '治疗方案', None])
        for cid in range(1, 7):
            page.append([cid, '正畸结束后', '局部', '乳白色', '姓名-答案家漂', '家漂'])
            Image.new('RGB', (20, 10), (cid * 25, 50, 70)).save(self.root / 'pic' / f'{cid}.JPG')
        page.append([7, '未知', '局部', '白色', '源文件', None])
        page.append([8])
        book.save(self.root / 'label.xlsx')
        self.args = SimpleNamespace(workbook=self.root / 'label.xlsx', images=self.root / 'pic', sheet=None,
            seed=42, test_ratio=0.34, domain='enamel-white-spot', max_turns=3,
            output=self.root / 'run', limit=None, provider='mock', model='mock')
        self.cases, self.manifest = prepare(self.args)

    def test_workbook_last_effective_column_and_two_images_per_case(self):
        Image.new('RGB', (20, 10)).save(self.root / 'pic/1-2.JPG')
        cases, excluded, headers = read_cases(self.args.workbook, self.args.images)
        self.assertEqual(headers[-1], '治疗方案')
        self.assertEqual(len(cases[0]['images']), 2)
        self.assertEqual(excluded, [{'id': '7', 'reason': 'missing_treatment'}, {'id': '8', 'reason': 'number_only'}])

    def test_target_and_answer_bearing_filename_never_enter_worker_document(self):
        document = json.dumps(public_case(self.cases['1'], 'patient', 3), ensure_ascii=False)
        self.assertNotIn('reference_treatment', document)
        self.assertNotIn('家漂', document)
        self.assertNotIn('姓名', document)
        self.assertIn('正畸结束后', document)

    def test_split_is_stable_disjoint_and_groups_identical_images(self):
        cases = list(self.cases.values())
        cases[1]['images'] = cases[0]['images']
        split = split_cases(cases, 42, 0.34)
        self.assertEqual(split, split_cases(list(reversed(cases)), 42, 0.34))
        self.assertFalse(set(split['train']) & set(split['test']))
        self.assertEqual('1' in split['test'], '2' in split['test'])

    def fake_worker(self, case, mode, work, args):
        """Scripted roles, actual existing store/retrieval/review/publication paths."""
        store = MemoryStore(work / 'private.sqlite3', 'batch:' + case['id'])
        project = MedicalProject(store, shared_path=work / 'shared.sqlite3', asset_root=work / 'images', domain=args.domain)
        try:
            encoded = base64.b64encode(Path(case['images'][0]['path']).read_bytes()).decode()
            run = project.ingest('batch-case-' + case['id'], 'input', 0,
                                 [{'id': 'original', 'text': '牙齿白色区域，正畸结束后'}], [{'data': encoded}])
            rid = run['run_id']
            image = project.state('batch-case-' + case['id'])['images'][0]['image_id']
            project.save_output(rid, 'patient', patient())
            delivered = project.retrieve(rid, 'key1')
            project.save_output(rid, 'retrieval', {'ranking': [{'case_id': c['case_id'], 'version': c['version'],
                'relevance': 80, 'supports': ['可比较'], 'contradictions': [], 'reason': '匹配'} for c in delivered['cases']],
                'transferable_lessons': [], 'limitations': []})
            project.save_output(rid, 'visual', visual(image))
            project.save_output(rid, 'reasoning', reasoning())
            project.save_output(rid, 'review', {'route': 'approve', 'issues': [], 'required_changes': ''})
            final = project.finish(rid, 'awaiting_doctor')
            return {'case_id': case['id'], 'mode': mode, 'status': final['status'], 'revision': final['revision'],
                    'run_id': rid, 'turns': 1, 'proposal': {'treatment_plan': '家漂', 'abstain': False}}
        finally:
            project.close()
            store.close()

    def test_train_learns_labels_and_test_cannot_change_snapshot_or_write_labels(self):
        train = phase_run('train', 'doctor', self.cases, self.manifest, self.args, self.fake_worker)
        self.assertEqual(train['count'], 4)
        memory = self.args.output / 'doctor/memory'
        snapshot = get_snapshot(memory)
        before = tree_hash(snapshot)
        with sqlite3.connect(snapshot / 'shared.sqlite3') as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM cases').fetchone()[0], 4)
            payload = json.loads(connection.execute('SELECT payload FROM cases LIMIT 1').fetchone()[0])
            self.assertIn('家漂', payload['experience'])
            self.assertEqual(payload['outcome'], '真实治疗效果未提供')
        with patch('batch.learn_case', side_effect=AssertionError('Test attempted teacher writeback')):
            summary = phase_run('test', 'doctor', self.cases, self.manifest, self.args, self.fake_worker)
        self.assertEqual(summary['count'], 2)
        self.assertEqual(tree_hash(snapshot), before)
        for path in (self.args.output / 'doctor/test').glob('case-*-attempt-*'):
            with sqlite3.connect(path / 'shared.sqlite3') as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM cases').fetchone()[0], 4)
        with patch('batch.learn_case', side_effect=AssertionError('Completed case replayed')):
            phase_run('train', 'doctor', self.cases, self.manifest, self.args,
                      lambda *a: self.fail('Resume repeated model work'))
        self.assertEqual(tree_hash(snapshot), before)

    def test_changed_data_or_configuration_requires_fresh_output(self):
        self.args.max_turns += 1
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            prepare(self.args)

    def test_testing_before_training_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Train first'):
            phase_run('test', 'doctor', self.cases, self.manifest, self.args, self.fake_worker)

    def test_treatment_proxy_and_abstention_denominator(self):
        self.assertTrue(treatment_metrics('磨除 ＋ 树脂修复', '树脂修复+磨除')['normalized_exact_match'])
        self.assertEqual(treatment_metrics('', '家漂')['component_f1'], 0)
        self.assertAlmostEqual(treatment_metrics('磨除', '磨除+树脂修复')['component_f1'], 2 / 3)

    def test_training_failure_does_not_publish_a_partial_snapshot(self):
        def failed(*args):
            raise RuntimeError('model transport failed')
        with self.assertRaisesRegex(RuntimeError, 'transport'):
            phase_run('train', 'patient', self.cases, self.manifest, self.args, failed)
        self.assertIsNone(get_snapshot(self.args.output / 'patient/memory'))


if __name__ == '__main__':
    unittest.main()
