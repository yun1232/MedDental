"""Unified experience category and enforced visual/diagnosis query boundaries."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import unittest
from unittest.mock import patch
import test_team as fixtures
from test_team import patient, visual
from test_memory import lesion, narrative, query
from schemas import MemoryError, tools
from server import call
from extensions import reference


class PatientExperienceTests(unittest.TestCase):
    setUp=fixtures.TeamTests.setUp
    tearDown=fixtures.TeamTests.tearDown
    project=fixtures.TeamTests.project
    start=fixtures.TeamTests.start

    def seed(self,rid,color,history,kind='experience'):
        r=self.p.store.put('lesion',{'record':lesion(rid,'prior-'+rid,color=color),'expected_version':0})['record']
        event=narrative(kind,'event-'+rid,'prior-'+rid,description=history,links=[rid])
        self.p.store.put(kind,{'record':event,'expected_version':0})
        return r

    def staged_query(self, purpose, history='补牙', color='白色'):
        return query(purpose=purpose,patient_experience_query=history,lesion_query={'color':color})

    def test_visual_retrieval_never_uses_appearance_even_if_caller_supplies_it(self):
        self.seed('appearance-only','白色','吸烟')
        self.seed('history-match','红色','补牙','care')
        with patch('retrieval.phenotype_score',side_effect=AssertionError('visual must not score phenotype')):
            first=self.p.store.retrieve(self.staged_query('visual'))
            changed=self.p.store.retrieve(self.staged_query('visual',color='红色'))
        self.assertEqual([c['lesion']['record_id'] for c in first['candidates']],['history-match'])
        self.assertEqual([c['lesion']['record_id'] for c in changed['candidates']],['history-match'])
        self.assertEqual(first['candidates'][0]['phenotype_score'],0)
        self.assertEqual(first['retrieval_channels'],['patient_experience'])

    def test_visual_has_no_phenotype_fallback_when_history_missing(self):
        self.seed('white','白色','检查')
        result=self.p.store.retrieve(self.staged_query('visual',history=''))
        self.assertEqual(result['status'],'needs_clarification')
        self.assertEqual(result['candidates'],[])

    def test_diagnosis_combines_phenotype_and_both_experience_subtypes(self):
        self.seed('appearance','白色','吸烟')
        self.seed('care','红色','补牙','care')
        self.seed('oral','红色','补牙后不适')
        result=self.p.store.retrieve(self.staged_query('diagnosis'))
        self.assertEqual({c['lesion']['record_id'] for c in result['candidates']},{'appearance','care','oral'})
        self.assertEqual(result['candidates'][0]['lesion']['record_id'],'appearance')
        self.assertEqual(result['retrieval_channels'],['lesion','patient_experience'])
        self.assertTrue(all(e['record']['category']=='patient_experience' for c in result['candidates'] for e in c['patient_experience']))

    def test_care_and_oral_records_share_one_view_and_keep_distinct_original_references(self):
        for origin in ('experience','care'):
            self.p.store.put(origin,{'record':narrative(origin,'same-id','old',description='患者经历'), 'expected_version':0})
        records=self.p.store.records('patient_experience')
        self.assertEqual(len(records),2)
        self.assertEqual({r['category'] for r in records},{'patient_experience'})
        self.assertEqual({reference(r)['category'] for r in records},{'experience','care'})
        self.assertEqual(self.p.db.execute('SELECT COUNT(*) FROM patient_experience_memory').fetchone()[0],2)
        result=call(self.p.store,'search',{'category':'patient_experience','query':'经历','top_k':10})
        self.assertEqual(len(result['delivered_refs']),2)
        self.assertEqual({r['category'] for r in result['delivered_refs']},{'experience','care'})
        self.assertEqual({r[0] for r in self.p.db.execute('SELECT DISTINCT category FROM memory_stats')},{'experience','care'})

    def test_new_unified_write_read_update_and_feedback_reuse_existing_state(self):
        event=narrative('care','care-1','old',description='复诊检查')
        event['event_kind']='care'
        written=call(self.p.store,'put_patient_experience',{'record':event,'expected_version':0})
        ref=written['record']['storage_reference']
        self.assertEqual(written['record']['category'],'patient_experience')
        self.assertEqual(ref['category'],'care')
        result=call(self.p.store,'get_patient_experience',{'reference':ref,'include_versions':True})
        self.assertEqual(result['record']['event_kind'],'care')
        self.assertEqual(result['versions'][0]['record']['category'],'patient_experience')
        doctor=self.project('patient-a','doctor')
        doctor.store.memory_feedback({'event_id':'same-event','retrieval_id':result['retrieval_id'],'reference':ref,'kind':'not_helpful','reason':'实际无帮助'})
        self.assertEqual(self.p.store.records('patient_experience')[0]['stats']['decay_score'],.9)
        self.assertEqual(self.p.store.records('care')[0]['stats']['decay_score'],.9)
        changed=dict(event);changed['description']='补充复诊结果'
        update=call(self.p.store,'put_patient_experience',{'record':changed,'expected_version':1})
        self.assertEqual(update['record']['version'],2)
        self.assertEqual(len(self.p.store.records('patient_experience')),1)
        with self.assertRaises(MemoryError):call(self.p.store,'get_patient_experience',{'reference':ref,'include_versions':False})

    def test_unified_write_retains_actor_permissions_and_kind_validation(self):
        event=narrative('care','event','old');event['event_kind']='care';event['source']='doctor';event['review_status']='doctor_confirmed'
        with self.assertRaises(MemoryError):call(self.p.store,'put_patient_experience',{'record':event,'expected_version':0})
        event=narrative('experience','event','old');event['event_kind']='care'
        with self.assertRaises(MemoryError):call(self.p.store,'put_patient_experience',{'record':event,'expected_version':0})
        self.assertIn('put_patient_experience',{t['name'] for t in tools('patient')})

    def test_history_only_cases_without_lesion_descriptions_remain_retrievable(self):
        self.p.store.put('care',{'record':narrative('care','no-image','prior',description='补牙'), 'expected_version':0})
        run=self.start();intake=patient(['白色']);intake['patient_experiences']=['补牙']
        self.p.save_output(run,'patient',intake)
        recalled=self.p.retrieve(run,'key1')
        self.assertEqual(recalled['cases'][0]['memory_reference']['category'],'care')
        self.assertEqual(recalled['cases'][0]['phenotype'],[])
        self.assertEqual(recalled['cases'][0]['images'],[])

    def test_team_key1_uses_canonical_experience_field_and_key2_adds_visual_description(self):
        self.seed('history','红色','补牙','care')
        self.seed('appearance','白色','吸烟')
        run=self.start();intake=patient(['白色']);intake['history']=['不应采用的旧字段'];intake['patient_experiences']=['补牙']
        self.p.save_output(run,'patient',intake)
        first=self.p.retrieve(run,'key1')
        self.assertEqual(first['query']['lesion_query'],{'text':'unknown'})
        self.assertEqual(first['query']['patient_experience_query'],'补牙')
        self.assertEqual([c['memory_reference']['record_id'] for c in first['cases']],['history'])
        self.p.save_output(run,'visual',visual(self.p.state('visit')['images'][0]['image_id'],'白色'))
        second=self.p.retrieve(run,'key2')
        self.assertEqual(second['query']['purpose'],'diagnosis')
        self.assertEqual({c['memory_reference']['record_id'] for c in second['cases']},{'history','appearance'})
        self.assertEqual(second['retrieval_channels'],['lesion','patient_experience'])

if __name__=='__main__':unittest.main()
