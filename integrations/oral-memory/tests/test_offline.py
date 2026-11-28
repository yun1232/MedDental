"""Input-label boundaries, actual crop/mask loops, native memory and pure Python CLI."""
from copy import deepcopy
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
from offline_memory import OfflineMemory
from offline_run import backup, main, read
from vision_loop import run_episode
from agents import retrieval_result, reasoning_result, visual_result, audit_result

ROOT = Path(__file__).resolve().parents[3]


class ScriptedClient:
    def __init__(self, actions=('crop', 'mask', 'finish')):
        self.actions, self.calls = actions, []

    def complete(self, stage, instruction, payload, images):
        self.calls.append((stage, deepcopy(payload), deepcopy(images)))
        count = payload.get('round', 1) - 1
        kind = self.actions[min(count, len(self.actions) - 1)]
        if stage == 'visual':
            detail = '细节' + str(count)
            return {'observations': [{'image_id': 'original-0', 'finding': '乳白色区域', 'box': []},
                                      {'image_id': 'original-0' if not count else 'view-' + str(count),
                                       'finding': detail, 'box': []}],
                    'new_evidence': [detail], 'quality_issues': [], 'conflicts': [], 'uncertainties': []}
        if stage == 'retrieval':
            ids = [m['case_id'] for m in payload['candidates']]
            action = self.actions[min(max(0, count - 1), len(self.actions) - 1)] if payload['task'] == 'visual' else 'none'
            return {'cases': [{'case_id': p['case_id'], 'score': 80, 'source_evidence': p['observations'][:1],
                              'similarities': ['有相近白色表征'], 'differences': ['病程未知'],
                              'applicability': '仅供区域核查参考',
                              'derivation': '有白色表征及既往标签；原始医生推导未知，模型推断需核查当前质地'}
                             for p in payload['candidates']],
                    'action': {'type': action, 'image_index': 0, 'box': [] if action == 'none' else [250, 250, 750, 750],
                               'reason': '核查当前原图表面细节', 'memory_ids': ids[:1] if action != 'none' else []}}
        if stage == 'reasoning':
            return {'etiology': '龋性白斑', 'treatment': '漂白',
                    'evidence': [{'statement': '有可见白色区域，病因仍需结合病史', 'source_ids': ['original-0']}],
                    'limitations': [], 'uncertainties': [] if kind == 'finish' else [{'question': '局部表面质地不清', 'kind': 'visual'}],
                    'confidence': 0.9 if kind == 'finish' else 0.5}
        if stage == 'review':
            pending = payload['reasoning']['uncertainties']
            return {'decision': 'visual' if pending else 'approve', 'conflicts': [],
                    'requests': ['核查局部表面质地'] if pending else [], 'reason': '已核查图像、视觉证据及病例适用性'}
        if stage == 'memory_review':
            return {'assessments': [{'case_id': cid, 'verdict': 'not_helpful', 'reason': '局部复核未增加有用信息',
                'steps': [v['inference_step'] for v in payload['regions'] if cid in v['memory_ids']]}
                for cid in payload['used_memory_ids']]}
        raise AssertionError('Unexpected stage: ' + stage)


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
        infer = [c for c in model.calls if c[0] == 'reasoning']
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
                if stage == 'visual':
                    result['observations'] = [{'image_id': 'original-0', 'finding': '乳白色区域', 'box': []}]
                    result['new_evidence'] = []
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
        value = ScriptedClient(('crop',)).complete('retrieval', '', {'round': 2, 'task': 'visual', 'candidates': []}, [])
        value['action']['box'] = [-1, 20, 40, 60]
        with self.assertRaises(ValueError):
            retrieval_result(value, [], 1, 'visual')
        value['action']['box'] = [0, 0, 20, 20]
        value['action']['memory_ids'] = ['invented']
        with self.assertRaises(ValueError):
            retrieval_result(value, [], 1, 'visual')

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
        self.assertIn('prior_case_labels', visual[0])
        diagnosis = memory.retrieve(next_case, ['乳白色区域'])
        self.assertEqual(diagnosis[0]['prior_case_labels'], self.rows[0]['labels'])
        second = run_episode(next_case, ScriptedClient(), memory, self.path / 'second')
        self.assertEqual(second['memory_assessments'][0]['verdict'], 'not_helpful')
        memory.remember({**next_case, 'labels': self.rows[1]['labels']}, second, 'dataset')
        published = memory.library.published('train-2', 1)
        self.assertEqual(published['regions'][0]['visual_judgment'], second['visual_passes'][1]['result'])
        self.assertEqual(published['reference_rationale'], None)
        self.assertEqual(published['reasoning_summary']['source'], 'model_inference')
        after = memory.retrieve(public_case(self.rows[2], 'history'))
        self.assertAlmostEqual(next(p['decay'] for p in after if p['case_id'] == 'train-1'), .9)
        duplicate = deepcopy(public_case(self.rows[2], 'history'))
        duplicate['images'] = first['images']
        self.assertNotIn('train-1', [p['case_id'] for p in memory.retrieve(duplicate)])

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
            model = ScriptedClient()
            episode = run_episode(public_case(self.rows[1], 'history'), model, frozen, self.path / 'eval')
            self.assertEqual(len([c for c in model.calls if c[0] == 'review']), episode['rounds'])
            self.assertFalse([c for c in model.calls if c[0] == 'memory_review'])
            with self.assertRaises(ValueError):
                frozen.remember(first, episode, 'dataset')
            with self.assertRaises(sqlite3.OperationalError):
                frozen.library.db.execute('DELETE FROM cases')
        finally:
            frozen.close()
        self.assertEqual(snapshot.read_bytes(), before)

    def test_multiple_originals_are_retained_and_crop_uses_selected_original(self):
        class SecondImage(ScriptedClient):
            def complete(self, stage, instruction, payload, images):
                result = super().complete(stage, instruction, payload, images)
                if stage == 'retrieval' and payload['task'] == 'visual':
                    result['action']['image_index'] = 1
                return result
        case = public_case(self.rows[0], 'clinical')
        case['images'].extend(public_case(self.rows[1], 'clinical')['images'])
        model = SecondImage(('crop', 'finish'))
        episode = run_episode(case, model, self.empty_memory(), self.path / 'multiple')
        self.assertEqual(episode['status'], 'resolved')
        for _, payload, images in model.calls:
            self.assertEqual([i['id'] for i in images[:2]], ['original-0', 'original-1'])
            self.assertEqual(payload['patient_text'], case['text'])
        with Image.open(episode['views'][0]['path']) as crop:
            self.assertEqual(crop.getpixel((0, 0)), (60, 70, 80))
        self.assertEqual(episode['views'][0]['image_index'], 1)

    def test_reasoning_only_audit_correction_reuses_visual_and_memory_without_crop(self):
        class CorrectLogic(ScriptedClient):
            def complete(self, stage, instruction, payload, images):
                result = super().complete(stage, instruction, payload, images)
                if stage == 'review' and payload['round'] == 1:
                    return {'decision': 'reasoning', 'conflicts': ['治疗依据未解释'],
                            'requests': ['补充治疗适用范围'], 'reason': '无需额外视觉证据'}
                return result
        model = CorrectLogic(('finish',))
        episode = run_episode(public_case(self.rows[0], 'history'), model, self.empty_memory(), self.path / 'logic')
        self.assertEqual(episode['status'], 'resolved')
        self.assertEqual(episode['rounds'], 2)
        self.assertEqual(episode['views'], [])
        self.assertEqual([c[0] for c in model.calls], ['visual', 'retrieval', 'reasoning', 'review', 'reasoning', 'review'])
        second_reason = [c for c in model.calls if c[0] == 'reasoning'][1][1]
        self.assertEqual(second_reason['review_feedback']['decision'], 'reasoning')
        self.assertEqual(len(episode['visual_passes']), 1)

    def test_review_conflict_requests_retrieval_and_actual_region_even_if_reasoner_is_certain(self):
        class Conflict(ScriptedClient):
            def complete(self, stage, instruction, payload, images):
                result = super().complete(stage, instruction, payload, images)
                if stage == 'retrieval' and payload['task'] == 'visual':
                    result['action'].update(type='crop', box=[250, 250, 750, 750])
                if stage == 'review' and payload['round'] == 1:
                    return {'decision': 'visual', 'conflicts': ['全局与局部质地描述冲突'],
                            'requests': ['核查原图中部白色区域质地'], 'reason': '证据冲突'}
                return result
        model = Conflict(('finish',))
        episode = run_episode(public_case(self.rows[0], 'history'), model, self.empty_memory(), self.path / 'conflict')
        self.assertEqual(episode['status'], 'resolved')
        self.assertEqual(episode['rounds'], 2)
        self.assertEqual(len(episode['views']), 1)
        self.assertEqual([r['task'] for r in episode['retrievals']], ['context', 'visual', 'context'])
        self.assertIn('全局与局部质地描述冲突', episode['retrievals'][1]['questions'])
        self.assertEqual(episode['views'][0]['memory_ids'], [])  # no invented memory in an empty library
        self.assertEqual(episode['steps'][1]['visual_pass'], 1)

    def test_reasoner_visual_uncertainty_cannot_be_suppressed_by_review_approval(self):
        class OverconfidentReview(ScriptedClient):
            def complete(self, stage, instruction, payload, images):
                result = super().complete(stage, instruction, payload, images)
                if stage == 'review':
                    return {'decision': 'approve', 'conflicts': [], 'requests': [], 'reason': '暂未发现其他冲突'}
                return result
        episode = run_episode(public_case(self.rows[0], 'history'), OverconfidentReview(('crop', 'finish')),
                              self.empty_memory(), self.path / 'override')
        self.assertEqual(episode['rounds'], 2)
        self.assertEqual(episode['steps'][0]['route'], 'visual')
        self.assertEqual(episode['status'], 'resolved')

    def test_case_retrieval_retry_uses_gap_without_forcing_a_crop(self):
        class OtherCases(ScriptedClient):
            def complete(self, stage, instruction, payload, images):
                result = super().complete(stage, instruction, payload, images)
                if stage == 'review' and payload['round'] == 1:
                    return {'decision': 'retrieve', 'conflicts': ['参考病例病程不同'],
                            'requests': ['检索相近病程病例'], 'reason': '需重新核查历史病例适用性'}
                return result
        model = OtherCases(('finish',))
        memory = self.empty_memory()
        with patch.object(memory, 'retrieve', wraps=memory.retrieve) as retrieve:
            episode = run_episode(public_case(self.rows[0], 'history'), model, memory, self.path / 'recall')
        self.assertEqual(episode['views'], [])
        self.assertEqual(episode['status'], 'resolved')
        self.assertEqual(retrieve.call_count, 2)
        self.assertIn('检索相近病程病例', retrieve.call_args.kwargs['gaps'])
        self.assertNotIn('purpose', retrieve.call_args.kwargs)
        self.assertEqual(len(episode['visual_passes']), 1)

    def test_unified_visual_recall_works_with_unknown_history_and_preserves_source_labels(self):
        memory = self.empty_memory()
        first = public_case(self.rows[0], 'history')
        first['text']['既往史'] = '未记录'
        episode = run_episode(first, ScriptedClient(('finish',)), memory, self.path / 'unified-seed')
        memory.remember({**first, 'labels': self.rows[0]['labels']}, episode, 'dataset')
        following = public_case(self.rows[1], 'history')
        following['text']['既往史'] = '未记录'
        model = ScriptedClient(('finish',))
        episode = run_episode(following, model, memory, self.path / 'unified')
        related = episode['retrievals'][0]['related_cases'][0]
        self.assertEqual(related['case_id'], 'train-1')
        self.assertEqual(related['prior_case_labels'], self.rows[0]['labels'])
        self.assertIsNone(related['reference_rationale'])
        self.assertEqual(related['derivation_source'], 'model_inference')
        self.assertTrue(related['source_ref'].endswith('case:1'))
        self.assertEqual(episode['memory_assessments'], [])  # delivered, but no region used
        self.assertEqual(memory.retrieve(following, ['乳白色区域'])[0]['decay'], 1)

    def test_utility_review_context_contains_only_actual_visual_judgments(self):
        memory = self.empty_memory()
        first = public_case(self.rows[0], 'history')
        episode = run_episode(first, ScriptedClient(('finish',)), memory, self.path / 'utility-seed')
        memory.remember({**first, 'labels': self.rows[0]['labels']}, episode, 'dataset')
        model = ScriptedClient()
        following = public_case(self.rows[1], 'history')
        episode = run_episode(following, model, memory, self.path / 'utility')
        payload = [c[1] for c in model.calls if c[0] == 'memory_review'][0]
        serialized = json.dumps(payload)
        for forbidden in ('etiology', 'treatment', 'prior_case_labels', 'related_cases', 'reasoning', 'decision'):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual([p['step'] for p in payload['region_judgments']], [1, 2])
        self.assertEqual(payload['region_judgments'][0]['result'], episode['visual_passes'][1]['result'])
        self.assertEqual(len([c for c in model.calls if c[0] == 'review']), 3)
        for stage, role_payload, _ in model.calls:
            if stage in ('visual', 'reasoning', 'review', 'retrieval'):
                self.assertEqual(role_payload['patient_text'], following['text'])
        for _, role_payload, images in model.calls:
            self.assertEqual(images[0]['kind'], 'original')
        self.assertNotIn('related_cases', [c[1] for c in model.calls if c[0] == 'visual'][0])

    def test_contracts_reject_cross_role_work_and_fabricated_sources(self):
        reasoning = ScriptedClient(('finish',)).complete('reasoning', '', {'round': 1}, [])
        reasoning['action'] = {'type': 'crop'}
        with self.assertRaises(ValueError):
            reasoning_result(reasoning, {'original-0'})
        reasoning.pop('action')
        reasoning['evidence'][0]['source_ids'] = ['invented-case']
        with self.assertRaises(ValueError):
            reasoning_result(reasoning, {'original-0'})
        visual = ScriptedClient().complete('visual', '', {'round': 1}, [])
        visual['etiology'] = '不属于视觉角色的结论'
        with self.assertRaises(ValueError):
            visual_result(visual, {'original-0'})
        candidate = {'case_id': 'source', 'observations': ['真实表征'], 'evidence': [], 'history': [],
                     'decay': 1, 'reference_rationale': None}
        derived = ScriptedClient().complete('retrieval', '', {'round': 1, 'task': 'context', 'candidates': [candidate]}, [])
        derived['cases'][0]['source_evidence'] = ['伪造证据']
        with self.assertRaises(ValueError):
            retrieval_result(derived, [candidate], 1, 'context')
        with self.assertRaises(ValueError):
            audit_result({'decision': 'approve', 'conflicts': ['有冲突'], 'requests': [], 'reason': '不应批准'})

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
