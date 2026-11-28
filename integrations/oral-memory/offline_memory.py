"""Batch adapter over the existing case library, retrieval and decay mechanisms."""
import hashlib
import json
from pathlib import Path
import sqlite3

from case_library import CaseLibrary, library_identity
from extensions import reference
from retrieval import unified
from schemas import PHENOTYPE
from store import settings_from_env


class OfflineMemory:
    def __init__(self, path, writable=False, domain='enamel-white-spot'):
        self.path, self.writable, self.domain = Path(path).resolve(), writable, domain
        self.settings = settings_from_env({'ORAL_MEMORY_EXPLORATION_RATE': '0.1'})
        if writable:
            self.library = CaseLibrary(self.path, domain, 'doctor', self.settings)
        else:
            if not self.path.is_file():
                raise ValueError('Train a memory snapshot before testing')
            self.library = CaseLibrary.__new__(CaseLibrary)
            self.library.domain, self.library.role = domain, 'patient'
            self.library.settings, self.library.patient_id = self.settings, library_identity(domain)
            self.library.db = sqlite3.connect(self.path.as_uri() + '?mode=ro&immutable=1', uri=True)
            self.library.db.row_factory = sqlite3.Row
            app = self.library.db.execute('PRAGMA application_id').fetchone()[0]
            version = self.library.db.execute('PRAGMA user_version').fetchone()[0]
            if app != 0x4D444331 or version != 2:
                self.library.close()
                raise ValueError('Not a MedDental case library')
        self.receipts = {}

    def close(self):
        self.library.close()

    def retrieve(self, case, observations=(), gaps=(), top_k=5):
        rows = self.library.db.execute('SELECT payload FROM cases WHERE domain=?', (self.domain,)).fetchall()
        originals = {i['sha256'] for i in case['images']}
        eligible = {}
        for row in rows:
            value = json.loads(row['payload'])
            if value.get('source_case_id') == case['id'] or originals & set(value.get('original_hashes', [])):
                continue
            eligible[value['case_id']] = value
        records = {category: [r for r in self.library.records(category) if r.get('case_id') in eligible]
                   for category in ('lesion', 'experience', 'care', 'doctor')}
        unknown = {'', '/', '未记录', '不明', '未知', 'unknown'}
        history = case['text'].get('既往史', '')
        phenotype = '；'.join([*(v for k, v in case['text'].items() if k != '既往史' and v not in unknown),
                              *observations, *gaps])
        query = {'patient_experience_query': history if history not in unknown else '',
                 'lesion_query': {'text': phenotype}, 'top_k': top_k}
        packets = []
        for candidate in unified(records, query, self.settings):
            value = eligible[candidate['lesion']['case_id']]
            packets.append({'case_id': value['case_id'], 'version': value['version'],
                            'history': value['context'], 'observations': value['phenotype'],
                            'evidence': value.get('evidence', value['phenotype']),
                            'regions': value['regions'], 'prior_case_labels': value.get('labels', {}),
                            'source_ref': value['source_ref'],
                            'reference_rationale': value.get('reference_rationale'),
                            'observation_source': value.get('observation_source', 'unknown'),
                            'initial_visual': value.get('initial_visual'), 'final_visual': value.get('final_visual'),
                            'reasoning_summary': value.get('reasoning_summary'),
                            'outcome': value.get('outcome', '未知'),
                            'retrieval_score': candidate['retrieval_score'],
                            'matching_score': candidate['matching_score'],
                            'decay': candidate['lesion']['stats']['decay_score'], '_images': value['images']})
        return packets

    def delivered(self, packets, episode_id):
        """Only actual model-visible memories receive exposure/delivery receipts."""
        if not self.writable or not packets:
            return
        ids = {p['case_id'] for p in packets}
        records = [r for r in self.library.records('lesion') if r['case_id'] in ids]
        receipt = self.library._deliver({'records': records}, consultation_id=episode_id)
        for record in records:
            self.receipts[(episode_id, record['case_id'])] = (receipt['retrieval_id'], reference(record))

    def remember(self, case, episode, dataset_hash):
        if not self.writable:
            raise ValueError('Test memory is read-only')
        case_id = 'train-' + case['id']
        old = self.library.db.execute('SELECT payload FROM cases WHERE id=?', (case_id,)).fetchone()
        if old:
            raise ValueError('This training case has already been committed')
        images = []
        for image in case['images']:
            images.append({'image_id': 'memory-' + image['sha256'][:24], 'path': image['path'],
                           'sha256': image['sha256']})
        judgments = {p['input_view']: p['result'] for p in episode['visual_passes'] if p['input_view']}
        regions = [{**view, 'image_id': images[view['image_index']]['image_id'],
                    'visual_judgment': judgments[view['id']]} for view in episode['views']]
        # Only visible text + model visual observations become retrieval descriptors.
        phenotype = [v for k, v in case['text'].items() if k != '既往史']
        phenotype.extend(episode['final']['observations'])
        if not phenotype:
            phenotype = ['未取得可靠视觉表征']
        value = {'case_id': case_id, 'version': 1, 'domain': self.domain,
                 'source_case_id': case['id'], 'original_hashes': [i['sha256'] for i in case['images']],
                 'phenotype': list(dict.fromkeys(phenotype)), 'context': [case['text'].get('既往史', '未记录')],
                 'evidence': episode['final']['observations'], 'labels': case['labels'],
                 'initial_visual': episode['baseline'], 'final_visual': episode['visual_passes'][-1]['result'],
                 'reference_rationale': None,
                 'reasoning_summary': {'source': 'model_inference', 'etiology': episode['final']['etiology'],
                                       'treatment': episode['final']['treatment'],
                                       'evidence': episode['final']['evidence'],
                                       'limitations': episode['final']['limitations']},
                 'experience': '病因标签：' + case['labels']['病因'] + '；治疗标签：' + case['labels']['治疗方案'],
                 'pitfalls': episode['final']['uncertainties'], 'outcome': '仅有参考标注，真实疗效未知',
                 'source_ref': 'dataset:' + dataset_hash + ':case:' + case['id'],
                 'images': images, 'regions': regions, 'review_status': 'dataset_supervised',
                 'supervision': 'offline_labels', 'observation_source': 'model',
                 'features': {k: [] if k == 'tooth_positions' else 'unknown' for k in PHENOTYPE}}
        with self.library.transaction():
            self.library.db.execute('INSERT INTO cases(id,version,domain,payload) VALUES(?,?,?,?)',
                                    (case_id, 1, self.domain, json.dumps(value, ensure_ascii=False)))
            self.library.db.execute('INSERT INTO case_versions VALUES(?,?,?)',
                                    (case_id, 1, json.dumps(value, ensure_ascii=False)))
            self.library.index_case(value)
        return {'case_id': case_id, 'version': 1}

    def feedback(self, episode_id, case_id, verdict, reason):
        if not self.writable or verdict == 'uncertain':
            return
        if verdict not in ('helpful', 'not_helpful') or (episode_id, case_id) not in self.receipts:
            raise ValueError('Feedback must name a delivered memory and a known verdict')
        receipt, ref = self.receipts[(episode_id, case_id)]
        event = hashlib.sha256((episode_id + ':' + case_id).encode()).hexdigest()
        return self.library.memory_feedback({'event_id': event, 'retrieval_id': receipt, 'reference': ref,
                                             'kind': verdict, 'reason': reason})
