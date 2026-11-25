"""Index doctor-reviewed cases in the patient's existing memory engine.

The case anchor does not claim all images show one lesion. Patient reports and
model observations retain provenance. Publication remains separately authorized.
"""
import hashlib
import json
from extensions import reference
from schemas import PHENOTYPE


def remember_reviewed_case(project, run):
    """Inside the review/outcome transaction; no shared writes or model calls."""
    review = run['doctor_review']
    patient = project.latest(run, 'patient') or {}
    visual = project.latest(run, 'visual') or {'findings': [], 'regions': []}
    identity = hashlib.sha256(json.dumps([project.domain, run['encounter_id']],
                                       ensure_ascii=False).encode()).hexdigest()[:40]
    prefix = 'reviewed-case-' + identity
    source = 'reviewed-run:' + run['run_id']
    common = {'encounter_id': run['encounter_id'], 'event_time': str(review['time']),
              'source_ref': source, 'source': 'doctor', 'review_status': 'doctor_confirmed'}

    def save(category, record):
        previous = project.db.execute(
            f'SELECT version FROM {category}_memory WHERE patient_id=? AND record_id=?',
            (project.patient, record['record_id'])).fetchone()
        return project.store.put(category, {'record': record,
            'expected_version': previous['version'] if previous else 0})['record']

    # Attribute each observation; never turn proposed pathology into fact.
    descriptions = ['患者陈述的表征：' + x for x in patient.get('phenotype', [])]
    descriptions.extend('模型图像观察（' + f['image_id'] + '）：' + f['description']
                        for f in visual['findings'] if f['certainty'] == 'clear')
    anchor = save('lesion', {**common, 'record_id': prefix, 'lesion_id': prefix,
        'features': {key: [] if key == 'tooth_positions' else 'unknown' for key in PHENOTYPE},
        'visual_description': '；'.join(dict.fromkeys(descriptions)),
        'size_description': 'unknown',
        'pathology': {'status': 'unknown', 'result': '', 'source_ref': ''}})
    refs = [reference(anchor)]
    history = patient.get('patient_experiences', patient.get('history', []))
    previous_history = project.db.execute(
        'SELECT 1 FROM experience_memory WHERE patient_id=? AND record_id=?',
        (project.patient, prefix + '-history')).fetchone()
    if history or previous_history:
        event = save('experience', {**common, 'record_id': prefix + '-history',
            'source': 'patient', 'review_status': 'unreviewed',
            'lesion_ids': [prefix], 'experience_type': 'other_oral',
            'description': '；'.join(dict.fromkeys(history)) if history else 'unknown'})
        refs.append(reference(event))
    success = {True: '成功', False: '失败', None: '尚未判定'}[review.get('success')]
    judgment = save('care', {**common, 'record_id': prefix + '-judgment',
        'lesion_ids': [prefix], 'care_type': 'diagnosis',
        'description': '医生复核意见：' + review['note'],
        'outcome': '医生判断：' + review['outcome'] + '；本次评估结果：' + success
            + ('；后续确认：' + review['outcome_note'] if review.get('outcome_note') else '')})
    refs.append(reference(judgment))
    # Originals stay private; visual annotations retain their model provenance.
    for image in run['input']['images']:
        regions = [r for r in visual['regions'] if r['image_id'] == image['image_id']]
        findings = [f for f in visual['findings'] if f['image_id'] == image['image_id']]
        event = hashlib.sha256(json.dumps([run['run_id'], reference(anchor), image['image_id'],
            regions, findings], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        project.store.link_visual(reference(anchor), image, regions, findings,
                                  run['run_id'], 'reviewed-case:' + event)
    result = {'scope': 'patient', 'status': 'doctor_reviewed', 'source_run': run['run_id'],
              'anchor': reference(anchor), 'references': refs,
              'image_ids': [i['image_id'] for i in run['input']['images']]}
    run['case_memory'] = result
    return result
