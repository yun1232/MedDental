"""Evidence-bound review of independent regional workers, without doctor scoring."""
import json
from schemas import MemoryError, validate, strings
from team_schemas import VISUAL_VALUE_REVIEW


def record_region_review(project, run, value, view_ids):
    validate(view_ids, strings())
    views = {v['view_id']: v for v in run['views']}
    if len(view_ids) != 2 or any(v not in views for v in view_ids):
        raise MemoryError('invalid_region_review', 'One local worker must inspect one crop/mask pair')
    pair = [views[v] for v in view_ids]
    if ({v['kind'] for v in pair} != {'crop', 'mask'} or
            pair[0]['image_id'] != pair[1]['image_id'] or pair[0]['box'] != pair[1]['box']):
        raise MemoryError('invalid_region_review', 'Views must share the current image and region')
    # A local contribution cannot borrow another worker's crop.
    if any(c['view_id'] not in view_ids for c in value.get('contributions', [])):
        raise MemoryError('invalid_region_review', 'Local observations must cite this worker\'s views')
    run.setdefault('region_reviews', []).append({'step': len(run['outputs']),
        'view_ids': view_ids, 'source_cases': pair[0].get('source_cases', []),
        'gains': project.visual_gains(run, value)})


def visual_value_tasks(run):
    if not run.get('visual_baseline'):
        return []  # No retrospective novelty scores for legacy runs.
    first = next((r for r in run.get('rerankings', []) if r['key'] == 'key1'), {})
    selected = {(c['case_id'], c['version']) for c in first.get('selected', [])}
    latest = {tuple(sorted(r['view_ids'])): r for r in run.get('region_reviews', [])}
    outputs = {o['step']: o for o in run['outputs']}
    tasks = {}
    for regional in latest.values():
        output = outputs[regional['step']]['value']
        for ref in regional['source_cases']:
            key = (ref['case_id'], ref['version'])
            if key not in selected:
                continue
            task = tasks.setdefault(key, {**ref, 'regions': []})
            task['regions'].append({'step': regional['step'], 'view_ids': regional['view_ids'],
                'judgment': output, 'new_information_candidates': [g for g in regional['gains']
                    if (g['case_id'], g['version']) == key]})
    return list(tasks.values())


def save_visual_value_review(project, run_id, value):
    validate(value, VISUAL_VALUE_REVIEW)
    if len(json.dumps(value, ensure_ascii=False)) > 48000:
        raise MemoryError('context_limit', 'Regional value review exceeds the bounded result size')
    with project.store.transaction():
        run = project.run(run_id)
        previous = run.get('visual_value_review')
        if previous:
            if previous['judgment'] != value:
                raise MemoryError('feedback_conflict', 'Visual value review already committed for this run')
            return previous
        if run['status'] != 'running':
            raise MemoryError('run_closed', 'Audit the current regional work before ending the run')
        tasks = {(t['case_id'], t['version']): t for t in visual_value_tasks(run)}
        assessments = {(a['case_id'], a['version']): a for a in value['assessments']}
        if len(assessments) != len(value['assessments']) or assessments.keys() != tasks.keys():
            raise MemoryError('incomplete_visual_review', 'Review every tested case and no untested references')
        # Validate the entire review before changing any private/shared value.
        for key, assessment in assessments.items():
            regions = tasks[key]['regions']
            steps = {r['step'] for r in regions}
            if set(assessment['review_steps']) != steps or len(assessment['review_steps']) != len(steps):
                raise MemoryError('incomplete_visual_review', 'Read every regional worker for this case')
            gains = {(r['step'], g['finding_id']) for r in regions for g in r['new_information_candidates']}
            refs = {(r['step'], r['finding_id']) for r in assessment['finding_refs']}
            if len(refs) != len(assessment['finding_refs']) or not refs.issubset(gains):
                raise MemoryError('invalid_evidence', 'Value evidence must come from a regional worker\'s grounded new observation')
            if assessment['verdict'] == 'new_information' and not refs:
                raise MemoryError('missing_new_evidence', 'Positive visual value needs local crop-supported novelty')
        results = []
        for assessment in value['assessments']:
            if assessment['verdict'] == 'uncertain':
                results.append({**assessment, 'stage': 'visual', 'scored': False})
                continue
            result = project._record_stage_feedback(run, assessment['case_id'], assessment['version'],
                'visual', assessment['verdict'] == 'new_information',
                assessment['reason'], [json.dumps(r, sort_keys=True) for r in assessment['finding_refs']])
            results.append({**assessment, **result, 'scored': True})
        saved = {'source': 'review_agent', 'judgment': value, 'results': results}
        run['visual_value_review'] = saved
        project._write_run(run)
        return saved
