"""Original memory + visual evidence + temporary scoring/iteration decay integration."""
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from schemas import MemoryError
from store import MemoryStore
from extensions import reference
from case_library import CaseLibrary, record_id
from test_memory import lesion, narrative, query
import test_team as fixtures
from test_team import patient, visual, sample_image


def ranking(cases, scores=None):
    return {'ranking': [{'case_id': c['case_id'], 'version': c['version'], 'relevance': (scores or {}).get(c['memory_reference']['record_id'], 80),
                         'supports': ['当前证据可比较'], 'contradictions': [], 'reason': '本轮参考价值'} for c in cases],
            'transferable_lessons': [], 'limitations': []}


class FusionTests(unittest.TestCase):
    setUp = fixtures.TeamTests.setUp
    tearDown = fixtures.TeamTests.tearDown
    project = fixtures.TeamTests.project
    start = fixtures.TeamTests.start
    complete = fixtures.TeamTests.complete
    publish_case = fixtures.TeamTests.publish_case

    def put(self, record, category='lesion'):
        return self.p.store.put(category, {'record': record, 'expected_version': 0})['record']

    def assess(self, features=None, phenotype=None):
        run = self.start()
        intake = patient(phenotype)
        if features: intake['features'] = features
        self.p.save_output(run, 'patient', intake)
        return run

    def verdict(self, doctor, ref, kind='not_helpful', event='verdict'):
        packet = self.p.store.retrieve(query(top_k=50))
        return doctor.store.memory_feedback({'event_id': event, 'retrieval_id': packet['retrieval_id'],
                                             'reference': ref, 'kind': kind, 'reason': '实际使用后确认'})

    def test_current_original_image_links_existing_record_without_replacing_clinical_facts(self):
        original = self.put(lesion('existing', 'visit'))
        run = self.assess()
        self.assertEqual(self.p.run(run)['memory_bindings'][0]['reference'], reference(original))
        self.assertEqual(len(self.p.store.records('lesion')), 1)
        image_id = self.p.state('visit')['images'][0]['image_id']
        self.p.save_output(run, 'visual', visual(image_id, '特殊红白交界'))
        self.p.retrieve(run, 'key2')
        stored = self.p.store.get({'category':'lesion','record_id':'existing','include_versions':True})
        self.assertEqual(stored['record']['features'], original['features'])
        self.assertEqual(stored['record']['version'], 1)
        self.assertEqual(stored['record']['visual'][0]['image']['image_id'], image_id)
        recalled = self.p.store.retrieve(query(lesion_query={'text':'特殊红白交界'}))
        self.assertEqual(recalled['candidates'][0]['lesion']['record_id'], 'existing')
        searched = self.p.store.search({'category':'lesion','query':'特殊红白交界','top_k':10})
        self.assertEqual(searched['matches'][0]['record']['record_id'], 'existing')
        self.assertEqual(self.p.run(run)['retrievals'][-1]['cases'], [])  # do not cite this encounter as past experience

    def test_original_personal_memory_with_image_is_retrievable_without_publication(self):
        old = self.put(lesion('prior', 'earlier-visit'))
        buf = io.BytesIO(); Image.new('RGB',(60,40),(20,30,40)).save(buf,'PNG')
        asset = self.p._save_image(__import__('base64').b64encode(buf.getvalue()).decode(), self.p._private_dir())
        self.p.store.link_visual(reference(old), asset, [], [], 'old-source', 'old-image')
        run = self.assess()
        result = self.p.retrieve(run, 'key1')
        self.assertEqual(len(result['cases']), 1)
        case = result['cases'][0]
        self.assertEqual(case['memory_reference'], reference(old))
        self.assertEqual(case['memory_scope'], 'patient')
        self.assertEqual(case['images'][0]['image_id'], asset['image_id'])
        self.assertTrue(self.p.image(run, asset['image_id']).startswith(b'\x89PNG'))
        foreign = self.project('another-patient'); other_run = self.start(foreign)
        foreign.save_output(other_run, 'patient', patient())
        self.assertEqual(foreign.retrieve(other_run,'key1')['cases'], [])
        with self.assertRaises(MemoryError): foreign.image(other_run, asset['image_id'])

    def test_final_score_is_model_times_iteration_once_and_does_not_update_memory(self):
        a = self.put(lesion('a', 'prior-a')); b = self.put(lesion('b', 'prior-b', site='舌'))
        doctor = self.project('patient-a', 'doctor')
        for i in range(6): self.verdict(doctor, reference(a), event='a'+str(i))
        for i in range(2): self.verdict(doctor, reference(b), event='b'+str(i))
        doctor.store.set_frequency({'reference':reference(b),'frequency':'low_frequency','protected':False,'reason':'核查重复降权'})
        self.p.top_k = 1
        run = self.assess({'site':'颊黏膜','color':'白色'}, ['白色'])
        initial = self.p.retrieve(run, 'key1')
        self.assertEqual(initial['cases'][0]['memory_reference']['record_id'], 'a')
        self.p.save_output(run, 'retrieval', ranking(initial['cases'], {'a':90,'b':80}))
        reranked = self.p.run(run)['rerankings'][-1]
        self.assertEqual([r['final_score'] for r in reranked['ranking']], [64,36])
        self.assertEqual(reranked['selected'][0]['model_score'], 80)
        self.assertEqual(len(reranked['selected']), 1)
        self.assertEqual(self.p.store.memory_stats(reference(a))['decay_score'], .4)
        self.assertEqual(self.p.store.memory_stats(reference(b))['decay_score'], .8)
        self.assertEqual(self.p.store.memory_stats(reference(a))['negative'], 6)

    def test_key2_unions_and_rescores_old_and_new_candidates_with_frozen_stage_scores(self):
        a = self.put(lesion('white', 'prior-white'))
        self.put(lesion('red', 'prior-red', color='红色', morphology='红色区域'))
        self.p.top_k = self.p.candidate_limit = 1
        run = self.assess(phenotype=['白色'])
        first = self.p.retrieve(run, 'key1')
        self.assertEqual([c['memory_reference']['record_id'] for c in first['cases']], ['white'])
        self.p.save_output(run,'retrieval',ranking(first['cases'],{'white':90}))
        doctor = self.project('patient-a','doctor'); self.verdict(doctor,reference(a))
        image = self.p.state('visit')['images'][0]['image_id']
        self.p.save_output(run, 'visual', visual(image, '红色区域'))
        second = self.p.retrieve(run, 'key2')
        self.assertEqual(len(second['initial_candidates']), 1)
        self.assertEqual({c['memory_reference']['record_id'] for c in second['cases']}, {'white','red'})
        self.p.save_output(run, 'retrieval', ranking(second['cases'], {'white':20,'red':80}))
        history = self.p.run(run)
        self.assertEqual(history['retrievals'][0]['cases'][0]['decay_score'], 1)
        self.assertEqual(history['rerankings'][0]['ranking'][0]['model_score'], 90)
        self.assertEqual(history['rerankings'][-1]['ranking'][-1]['final_score'], 18)
        self.assertEqual(history['rerankings'][-1]['ranking'][0]['model_score'], 80)
        self.assertEqual(len(history['rerankings'][-1]['selected']),1)

    def test_missing_model_scores_never_reuse_old_scores_or_decay_only_ranking(self):
        self.put(lesion('old','prior'))
        run = self.assess(); self.p.retrieve(run,'key1')
        with self.assertRaisesRegex(MemoryError,'fresh temporary model score'):
            self.p.save_output(run,'retrieval',{'ranking':[],'transferable_lessons':[],'limitations':[]})
        self.assertNotIn('rerankings',self.p.run(run))

    def test_feedback_decays_linearly_once_with_zero_floor_and_positive_feedback_does_not_inflate_it(self):
        old = self.put(lesion('old','prior')); ref = reference(old)
        doctor = self.project('patient-a','doctor')
        self.assertEqual(self.p.store.memory_stats(ref)['decay_score'],1)
        packet = self.p.store.retrieve(query())
        args = {'event_id':'one','retrieval_id':packet['retrieval_id'],'reference':ref,'kind':'not_helpful','reason':'确认无帮助'}
        doctor.store.memory_feedback(args); doctor.store.memory_feedback(args)
        self.assertEqual(self.p.store.memory_stats(ref)['decay_score'],.9)
        self.verdict(doctor,ref,event='two')
        self.assertEqual(self.p.store.memory_stats(ref)['decay_score'],.8)
        self.verdict(doctor,ref,kind='helpful',event='positive')
        self.assertEqual(self.p.store.memory_stats(ref)['decay_score'],.8)
        for i in range(20): self.verdict(doctor,ref,event='more'+str(i))
        self.assertEqual(self.p.store.memory_stats(ref)['decay_score'],0)
        self.assertEqual(self.p.store.get({'category':'lesion','record_id':'old','include_versions':False})['record']['version'],1)

    def test_old_and_team_feedback_interfaces_do_not_double_decay_one_consultation(self):
        old = self.put(lesion('history','prior'))
        run = self.assess(); first = self.p.retrieve(run,'key1')
        image = self.p.state('visit')['images'][0]['image_id']
        self.p.save_output(run,'visual',visual(image))
        second = self.p.retrieve(run,'key2')
        doctor = self.project('patient-a','doctor')
        c = second['cases'][0]
        # Existing memory UI chooses the most recent delivery, while the team can reference Key1.
        doctor.store.memory_feedback({'event_id':'old-ui','retrieval_id':c['memory_retrieval_id'],
            'reference':reference(old),'kind':'not_helpful','reason':'旧入口确认'})
        duplicate = doctor.feedback(run,first['cases'][0]['case_id'],1,'not_helpful','团队入口确认')
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(doctor.store.memory_stats(reference(old))['decay_score'],.9)
        with self.assertRaises(MemoryError):
            doctor.store.memory_feedback({'event_id':'opposite','retrieval_id':first['cases'][0]['memory_retrieval_id'],
                'reference':reference(old),'kind':'helpful','reason':'矛盾反馈'})

    def test_clinical_edits_preserve_photos_but_invalidate_old_visual_interpretations(self):
        old = self.put(lesion('old','prior',color='unknown',morphology='unknown'))
        asset = self.p._save_image(sample_image(), self.p._private_dir())
        finding = visual(asset['image_id'],'独特视觉信号')['findings']
        self.p.store.link_visual(reference(old),asset,[],finding,'historical-run','historical-visual')
        updated = deepcopy(lesion('old','prior',color='unknown',morphology='unknown')); updated['size_description']='医生纠正尺寸'
        self.p.store.put('lesion',{'record':updated,'expected_version':1})
        value = self.p.store.get({'category':'lesion','record_id':'old','include_versions':False})['record']
        self.assertTrue(value['visual'][0]['stale']); self.assertEqual(value['visual'][0]['image']['image_id'],asset['image_id'])
        self.assertEqual(self.p.store.retrieve(query(lesion_query={'text':'独特视觉信号'}))['candidates'],[])

    def test_wrong_record_version_or_encounter_image_bindings_are_rejected_atomically(self):
        old = self.put(lesion('old','prior'))
        run = self.start(); intake = patient()
        intake['image_bindings']=[{'image_id':self.p.state('visit')['images'][0]['image_id'],'reference':reference(old)}]
        with self.assertRaises(MemoryError): self.p.save_output(run,'patient',intake)
        self.assertEqual(self.p.run(run)['outputs'],[])

    def test_doctor_team_reviews_are_original_doctor_memory(self):
        run = self.complete(); doc = self.project('patient-a','doctor')
        doc.review(run,1,True,'原始记忆可见的复核','暂不确诊')
        notes = doc.store.records('doctor')
        self.assertTrue(any('原始记忆可见' in n['description'] for n in notes))
        self.assertEqual(doc.run(run)['status'],'doctor_reviewed')

    def test_native_v2_scores_upgrade_from_old_immutable_verdicts_once(self):
        path = self.path/'legacy-personal.sqlite3'
        original = MemoryStore(path,'legacy','doctor'); original.close()
        db = sqlite3.connect(path)
        db.execute('ALTER TABLE memory_stats DROP COLUMN decay_score')
        db.execute("INSERT INTO memory_stats(patient_id,category,record_id,version,negative) VALUES('legacy','lesion','old',1,2)")
        for i in range(2): db.execute("INSERT INTO memory_feedback VALUES(?,?,?,?,?,?,?,?,?,?)",('legacy',str(i),str(i),'lesion','old',1,'not_helpful','旧反馈','doctor','old'))
        db.commit(); db.close()
        for _ in range(2):
            upgraded = MemoryStore(path,'legacy','doctor')
            self.assertEqual(upgraded.memory_stats({'category':'lesion','record_id':'old','version':1})['decay_score'],.8)
            upgraded.close()

    def test_old_published_library_and_feedback_upgrade_without_duplicate_penalty(self):
        _, _, _, case = self.publish_case()
        path = self.path/'legacy-library.sqlite3'; db = sqlite3.connect(path)
        db.executescript('''CREATE TABLE cases(id TEXT PRIMARY KEY,version INTEGER,domain TEXT,payload TEXT,helpful INTEGER,unhelpful INTEGER,exposures INTEGER);
            CREATE TABLE case_versions(id TEXT,version INTEGER,payload TEXT,PRIMARY KEY(id,version));
            CREATE TABLE deliveries(run TEXT,id TEXT,version INTEGER,feedback TEXT,PRIMARY KEY(run,id,version));
            PRAGMA application_id=1296319281;PRAGMA user_version=1;''')
        db.execute('INSERT INTO cases VALUES(?,?,?,?,?,?,?)',('case-1',1,self.p.domain,json.dumps(case),0,2,4))
        db.execute('INSERT INTO case_versions VALUES(?,?,?)',('case-1',1,json.dumps(case)))
        db.execute('INSERT INTO deliveries VALUES(?,?,?,?)',('old-run','case-1',1,json.dumps({'kind':'not_helpful','reason':'旧反馈'})))
        db.commit(); db.close()
        for _ in range(2):
            lib = CaseLibrary(path,self.p.domain,'doctor',self.p.store.settings)
            try:
                ref={'category':'lesion','record_id':record_id('case-1',self.p.domain,'lesion'),'version':1}
                self.assertEqual(lib.memory_stats(ref)['decay_score'],.8)
                self.assertEqual(len(lib.records('lesion')),1)
                result = lib.memory_feedback({'event_id':'replay','retrieval_id':lib.legacy_receipt('old-run','case-1',1),
                    'reference':ref,'kind':'not_helpful','reason':'旧反馈'})
                self.assertTrue(result['idempotent']); self.assertEqual(lib.memory_stats(ref)['decay_score'],.8)
                self.assertEqual(lib.db.execute('SELECT unhelpful FROM cases').fetchone()[0],2)
            finally: lib.close()

if __name__ == '__main__': unittest.main()
