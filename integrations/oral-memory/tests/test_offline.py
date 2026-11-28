"""Input-label boundaries, actual crop/mask loops, native memory and pure Python CLI."""
import base64
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from PIL import Image
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from batch_data import public_case, read_cases, split_cases, treatment_metrics
from model_client import ModelClient
from offline_memory import OfflineMemory
from offline_run import backup, main, prepare, read, run_phase
from vision_loop import decision, run_episode

ROOT = Path(__file__).resolve().parents[3]


class ScriptedClient:
    def __init__(self, actions=('crop', 'mask', 'finish')):
        self.actions, self.calls = actions, []

    def complete(self, stage, instruction, payload, images):
        self.calls.append((stage, deepcopy(payload), deepcopy(images)))
        if stage == 'observe':
            return {'observations': ['乳白色区域'], 'quality_issues': []}
        if stage == 'memory_review':
            return {'assessments': [{'case_id': cid, 'verdict': 'not_helpful', 'reason': '局部复核未增加信息',
                'steps': [v['inference_step'] for v in payload['regions'] if cid in v['memory_ids']]}
                for cid in payload['used_memory_ids']]}
        count = payload['round'] - 1
        kind = self.actions[min(count, len(self.actions) - 1)]
        ids = [m['case_id'] for m in payload['memory']]
        return {'etiology': '龋性白斑', 'treatment': '漂白', 'observations': ['乳白色区域', '细节' + str(count)],
            'uncertainties': [] if kind == 'finish' else ['局部表面质地不清'], 'confidence': 0.9 if kind == 'finish' else 0.5,
            'action': {'type': kind, 'image_index': 0, 'box': [] if kind == 'finish' else [250, 250, 750, 750],
                       'reason': '核查当前表面细节', 'memory_ids': ids[:1] if kind != 'finish' else []},
            'memory_scores': [{'case_id': cid, 'score': 80} for cid in ids]}


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        (self.path / 'pic').mkdir()
        book = Workbook()
        page = book.active
        page.append(['编号', '既往史', '病因（ICDAS分级）', '分布（部位）', '质地', '色调', '均一性', '边界', '治疗方案', None])
        for cid in range(1, 7):
            page.append([cid, '正畸结束后', '龋性白斑', '个别牙', '光滑', '乳白色', '均一', 'DO_NOT_SEND_BORDER', '漂白'])
            im = Image.new('RGB', (80, 60), (30 * cid, 70, 80))
            im.putpixel((40, 30), (255, 255, 255))
            im.save(self.path / 'pic' / f'{cid}.png')
        book.save(self.path / 'label.xlsx')
        self.rows, self.excluded, self.headers = read_cases(self.path / 'label.xlsx', self.path / 'pic')

    def test_exact_two_input_allowlists_and_two_hidden_labels(self):
        case = deepcopy(self.rows[0])
        case['labels'] = {'病因': 'ETIOLOGY_SECRET', '治疗方案': 'TREATMENT_SECRET'}
        for mode, keys in [('history', {'既往史'}), ('clinical', {'既往史', '分布', '质地', '色调', '均一性'})]:
            visible = public_case(case, mode)
            self.assertEqual(set(visible['text']), keys)
            self.assertNotIn('SECRET', json.dumps(visible))
            self.assertNotIn('DO_NOT_SEND_BORDER', json.dumps(visible))
            self.assertEqual(case['labels']['病因'], 'ETIOLOGY_SECRET')

    def test_split_is_stable_and_identical_images_stay_together(self):
        cases = deepcopy(self.rows)
        cases[1]['images'] = cases[0]['images']
        a = split_cases(cases, 42, .3)
        self.assertEqual(a, split_cases(list(reversed(cases)), 42, .3))
        self.assertEqual('1' in a['test'], '2' in a['test'])
        self.assertFalse(set(a['train']) & set(a['test']))

    def empty_memory(self):
        memory = OfflineMemory(self.path / ('empty-' + str(len(list(self.path.glob('empty-*')))) + '.sqlite3'), True)
        self.addCleanup(memory.close)
        return memory

    def test_every_reinspection_keeps_original_and_adds_real_crop_or_mask(self):
        case = public_case(self.rows[0], 'history')
        before = Path(case['images'][0]['path']).read_bytes()
        model = ScriptedClient()
        episode = run_episode(case, model, self.empty_memory(), self.path / 'episode')
        infer = [c for c in model.calls if c[0] == 'infer']
        self.assertEqual(episode['status'], 'resolved')
        self.assertEqual(episode['rounds'], 3)
        self.assertEqual([v['kind'] for v in episode['views']], ['crop', 'mask'])
        for _, payload, images in infer:
            self.assertEqual(payload['patient_text'], {'既往史': '正畸结束后'})
            self.assertEqual(images[0]['kind'], 'original')
        self.assertEqual([x['kind'] for x in infer[1][2]], ['original', 'crop'])
        self.assertEqual([x['kind'] for x in infer[2][2]], ['original', 'mask'])
        with Image.open(episode['views'][0]['path']) as crop:
            self.assertEqual(crop.size, (40, 30))
        with Image.open(episode['views'][1]['path']) as mask:
            self.assertEqual(mask.size, (80, 60))
            self.assertEqual(mask.getpixel((40, 30)), (127, 127, 127))
            self.assertEqual(mask.getpixel((0, 0)), (30, 70, 80))
        self.assertEqual(Path(case['images'][0]['path']).read_bytes(), before)

    def test_labels_cannot_enter_the_loop_even_by_accidental_full_case_call(self):
        case = {**public_case(self.rows[0], 'history'), 'labels': self.rows[0]['labels']}
        with self.assertRaisesRegex(ValueError, 'labels are forbidden'):
            run_episode(case, ScriptedClient(), self.empty_memory(), self.path / 'forbidden')

    def test_hidden_label_cannot_be_smuggled_into_the_text_dictionary(self):
        case = public_case(self.rows[0], 'history')
        case['text']['病因'] = 'hidden cause'
        with self.assertRaisesRegex(ValueError, 'allowlist'):
            run_episode(case, ScriptedClient(), self.empty_memory(), self.path / 'bad-text')

    def test_no_new_evidence_stops_after_two_unproductive_rechecks(self):
        class SameEvidence(ScriptedClient):
            def complete(self, stage, instruction, payload, images):
                result = super().complete(stage, instruction, payload, images)
                if stage == 'infer':
                    result['observations'] = ['乳白色区域']
                return result
        episode = run_episode(public_case(self.rows[0], 'history'), SameEvidence(('crop', 'mask', 'crop')),
                              self.empty_memory(), self.path / 'stale', max_rounds=8)
        self.assertEqual(episode['status'], 'no_new_evidence')
        self.assertEqual(episode['rounds'], 3)
        self.assertEqual(len(episode['views']), 2)
        self.assertTrue(episode['final']['uncertainties'])

    def test_model_failure_does_not_commit_training_labels_or_memory_pointer(self):
        class FailedModel:
            def complete(self, *args):
                raise RuntimeError('synthetic inference failure')
        args = ['--workbook', str(self.path / 'label.xlsx'), '--images', str(self.path / 'pic'),
                '--output', str(self.path / 'failed-run'), '--limit', '1']
        with patch('offline_run.ModelClient', return_value=FailedModel()):
            self.assertEqual(main('train', args), 1)
        self.assertFalse((self.path / 'failed-run/history/memory/current.json').exists())
        self.assertTrue(list((self.path / 'failed-run/history/train').glob('*/failure.json')))

    def test_repeated_region_stops_and_does_not_fake_certainty(self):
        episode = run_episode(public_case(self.rows[0], 'history'), ScriptedClient(('crop',)),
                              self.empty_memory(), self.path / 'repeat', max_rounds=8)
        self.assertEqual(episode['status'], 'repeated_view')
        self.assertEqual(episode['rounds'], 2)
        self.assertTrue(episode['final']['uncertainties'])

    def test_round_budget_does_not_generate_an_unused_view(self):
        episode = run_episode(public_case(self.rows[0], 'history'), ScriptedClient(), self.empty_memory(),
                              self.path / 'budget', max_rounds=1)
        self.assertEqual(episode['status'], 'round_budget')
        self.assertEqual(episode['views'], [])

    def test_invalid_box_or_invented_memory_is_rejected(self):
        value = ScriptedClient(('crop',)).complete('infer', '', {'round': 1, 'memory': []}, [])
        value['action']['box'] = [-1, 20, 40, 60]
        with self.assertRaises(ValueError):
            decision(value, set(), 1)
        value['action']['box'] = [0, 0, 20, 20]
        value['action']['memory_ids'] = ['invented']
        with self.assertRaises(ValueError):
            decision(value, set(), 1)

    def test_memory_learns_after_episode_excludes_self_and_decays_only_tested_reference(self):
        memory = self.empty_memory()
        first = public_case(self.rows[0], 'history')
        episode = run_episode(first, ScriptedClient(('finish',)), memory, self.path / 'first')
        self.assertEqual(memory.library.db.execute('SELECT COUNT(*) FROM cases').fetchone()[0], 0)
        memory.remember({**first, 'labels': self.rows[0]['labels']}, episode, 'dataset')
        stored = memory.library.records('lesion')[0]
        self.assertEqual(stored['source'], 'model')
        self.assertEqual(stored['review_status'], 'unreviewed')
        self.assertEqual(memory.retrieve(first), [])
        next_case = public_case(self.rows[1], 'history')
        visual = memory.retrieve(next_case)
        self.assertEqual(len(visual), 1)
        self.assertEqual(memory.library.db.execute('SELECT COUNT(*) FROM delivery_packets').fetchone()[0], 0)
        self.assertNotIn('prior_case_labels', visual[0])
        diagnosis = memory.retrieve(next_case, ['乳白色区域'], 'diagnosis')
        self.assertEqual(diagnosis[0]['prior_case_labels'], self.rows[0]['labels'])
        second = run_episode(next_case, ScriptedClient(), memory, self.path / 'second')
        self.assertEqual(second['memory_assessments'][0]['verdict'], 'not_helpful')
        after = memory.retrieve(public_case(self.rows[2], 'history'))
        self.assertAlmostEqual(after[0]['decay'], .9)
        duplicate = deepcopy(public_case(self.rows[2], 'history'))
        duplicate['images'] = first['images']
        self.assertEqual(memory.retrieve(duplicate), [])

    def test_unknown_history_does_not_retrieve_an_unknown_history_case(self):
        memory = self.empty_memory()
        first = public_case(self.rows[0], 'history')
        first['text']['既往史'] = '未记录'
        episode = run_episode(first, ScriptedClient(('finish',)), memory, self.path / 'unknown')
        memory.remember({**first, 'labels': self.rows[0]['labels']}, episode, 'dataset')
        following = public_case(self.rows[1], 'history')
        following['text']['既往史'] = '未记录'
        self.assertEqual(memory.retrieve(following), [])

    def test_read_only_memory_does_not_mutate_even_exposure_counters(self):
        memory = self.empty_memory()
        first = public_case(self.rows[0], 'history')
        episode = run_episode(first, ScriptedClient(('finish',)), memory, self.path / 'seed')
        memory.remember({**first, 'labels': self.rows[0]['labels']}, episode, 'dataset')
        snapshot = self.path / 'frozen.sqlite3'
        backup(memory.path, snapshot)
        before = snapshot.read_bytes()
        frozen = OfflineMemory(snapshot)
        try:
            run_episode(public_case(self.rows[1], 'history'), ScriptedClient(), frozen, self.path / 'eval')
            with self.assertRaises(ValueError):
                frozen.remember(first, episode, 'dataset')
            with self.assertRaises(sqlite3.OperationalError):
                frozen.library.db.execute('DELETE FROM cases')
        finally:
            frozen.close()
        self.assertEqual(snapshot.read_bytes(), before)

    def test_real_python_train_test_cli_with_synthetic_multimodal_api(self):
        calls = []
        scripted = ScriptedClient()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                blocks = body['messages'][1]['content']
                payload = json.loads(blocks[0]['text'])
                stage = payload.pop('stage')
                images = [b for b in blocks if b['type'] == 'image_url']
                calls.append((stage, deepcopy(payload), images))
                value = scripted.complete(stage, '', payload, [])
                response = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(response)))
                self.end_headers()
                self.wfile.write(response)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/v1'
            for mode in ('history', 'clinical'):
                args = ['--input-mode', mode, '--workbook', str(self.path / 'label.xlsx'), '--images', str(self.path / 'pic'),
                        '--output', str(self.path / 'run'), '--base-url', url, '--model', 'synthetic', '--limit', '2']
                for entry in ('train.py', 'test.py'):
                    result = subprocess.run([sys.executable, str(ROOT / entry), *args], capture_output=True, text=True, cwd=ROOT, timeout=40)
                    self.assertEqual(result.returncode, 0, result.stderr)
                summary = read(self.path / 'run' / mode / 'test/summary.json')
                self.assertEqual(summary['count'], 1)  # six cases -> one test case at ratio .2
                self.assertEqual(summary['unresolved_rate'], 0)
                pointer = read(self.path / 'run' / mode / 'memory/current.json')
                before = Path(pointer['snapshot']).read_bytes()
                call_count = len(calls)
                result = subprocess.run([sys.executable, str(ROOT / 'train.py'), *args], capture_output=True, text=True, cwd=ROOT, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(calls), call_count, 'Resume repeated completed model calls')
                self.assertEqual(Path(pointer['snapshot']).read_bytes(), before)
            self.assertTrue(all('DO_NOT_SEND_BORDER' not in json.dumps(payload) for _, payload, _ in calls))
            self.assertTrue(all(images for stage, _, images in calls if stage != 'memory_review'))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
