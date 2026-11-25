"""Patient-directed checkpoints, stage-isolated values and reusable doctor lessons."""
import json
from schemas import MemoryError, validate, obj, text, strings, choice
from retrieval import tokens, text_score
from team_schemas import CONTROL, LESSON


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS stage_values (
        patient TEXT, category TEXT, record_id TEXT, version INTEGER, stage TEXT,
        value REAL NOT NULL, PRIMARY KEY(patient,category,record_id,version,stage));
      CREATE TABLE IF NOT EXISTS stage_verdicts (
        patient TEXT, consultation TEXT, category TEXT, record_id TEXT, version INTEGER,
        stage TEXT, payload TEXT, PRIMARY KEY(patient,consultation,category,record_id,version,stage));
      CREATE TABLE IF NOT EXISTS doctor_lessons (
        patient TEXT, domain TEXT, id TEXT, version INTEGER, payload TEXT,
        PRIMARY KEY(patient,domain,id,version));
    ''')


def stage_value(store, ref, stage):
    """Seed each channel once from legacy D, without reinterpreting old verdicts."""
    if stage not in ('visual', 'diagnosis'):
        raise MemoryError('invalid_stage', 'Use visual or diagnosis')
    args = (store.patient_id, ref['category'], ref['record_id'], ref['version'], stage)
    store.db.execute('INSERT OR IGNORE INTO stage_values VALUES(?,?,?,?,?,?)', (*args, store.memory_stats(ref)['decay_score']))
    return store.db.execute('SELECT value FROM stage_values WHERE patient=? AND category=? AND record_id=? AND version=? AND stage=?', args).fetchone()[0]


def select_diverse(ranking, candidates, limit):
    """Keep best F, collapse near-duplicate evidence, fill remaining slots by novelty.

    No case is deleted. Similarity is lexical; small differences are not diagnoses.
    A 10% score band permits diversity without promoting unrelated low-score cases.
    """
    by_id = {(c['case_id'], c['version']): c for c in candidates}
    def content(row):
        c = by_id[(row['case_id'], row['version'])]
        return tokens(' '.join([*c.get('phenotype', []), *c.get('context', []), c.get('experience', ''), c.get('outcome', '')]))
    def sim(a, b):
        x, y = content(a), content(b)
        return len(x & y) / len(x | y) if x | y else 0
    remaining = [r for r in ranking if r['final_score'] > 0]
    selected, omitted = [], []
    while remaining and len(selected) < limit:
        eligible = [r for r in remaining if not any(sim(r, s) >= .9 for s in selected)]
        if not eligible:
            break
        best = eligible[0]['final_score']
        band = [r for r in eligible if r['final_score'] >= best * .9]
        chosen = min(band, key=lambda r: (max((sim(r, s) for s in selected), default=0), -r['final_score'], r['case_id'])) if selected else eligible[0]
        selected.append(chosen)
        remaining.remove(chosen)
    for r in remaining:
        if any(sim(r, s) >= .9 for s in selected):
            omitted.append({'case_id': r['case_id'], 'version': r['version'], 'reason': 'near_duplicate'})
    return selected, omitted


class ClinicalControlMixin:
    def control(self, run_id, checkpoint, value):
        validate(value, CONTROL)
        if checkpoint not in ('after_key1', 'after_key2', 'after_review'):
            raise MemoryError('invalid_checkpoint', 'Unknown patient control checkpoint')
        with self.store.transaction():
            run = self.run(run_id)
            if run['status'] != 'running' or len(run.setdefault('controls', [])) >= 6:
                raise MemoryError('control_limit', 'Consultation is closed or checkpoint budget exhausted')
            if checkpoint == 'after_key1' and (not run['retrievals'] or run['retrievals'][-1]['key'] != 'key1'):
                raise MemoryError('invalid_checkpoint', 'First checkpoint requires Key1 retrieval')
            if checkpoint == 'after_key2' and (not run['retrievals'] or run['retrievals'][-1]['key'] != 'key2'):
                raise MemoryError('invalid_checkpoint', 'Second checkpoint requires Key2 retrieval')
            if checkpoint == 'after_review' and self.latest(run, 'review') is None:
                raise MemoryError('invalid_checkpoint', 'Audit checkpoint requires a review')
            if not run['retrievals']:
                raise MemoryError('missing_retrieval', 'Retrieve cases before deciding missing information')
            candidates = {(c['case_id'], c['version']) for c in run['retrievals'][-1]['cases']}
            for gap in value['gaps']:
                if any((r['case_id'], r['version']) not in candidates for r in gap['case_refs']):
                    raise MemoryError('invalid_reference', 'A missing field must reference a delivered case')
                if not gap['case_refs']:
                    raise MemoryError('invalid_reference', 'Case-driven gaps require a case reference')
            action = value['action']
            allowed = {'after_key1': ('ask', 'visual', 'human'),
                       'after_key2': ('ask', 'visual', 'retrieve', 'reasoning', 'human'),
                       'after_review': ('ask', 'visual', 'retrieve', 'reasoning', 'finish', 'human')}[checkpoint]
            if action not in allowed:
                raise MemoryError('invalid_route', 'Action is not executable at this checkpoint')
            if action == 'ask' and (not value['question'].strip() or (candidates and not value['gaps'])):
                raise MemoryError('missing_question', 'Ask one grounded question and identify its case-driven gap')
            if action == 'ask':
                state = self.state(run['encounter_id'])
                asked = state.setdefault('case_questions', [])
                if sum(q['question'].strip() == value['question'].strip() for q in asked) >= self.store.settings['max_question_attempts']:
                    raise MemoryError('question_blocked', 'Repeated case question budget exhausted')
                longitudinal = self.inquiry.case_state({'encounter_id': run['encounter_id']})['state']
                for gap in value['gaps']:
                    current = longitudinal['fields'].get(gap['field'])
                    if current and current['status'] in ('known', 'denied', 'deferred'):
                        raise MemoryError('question_blocked', 'Patient has already answered or deferred this field')
                asked.append({'question': value['question'], 'gaps': value['gaps'], 'run_id': run_id})
                self.db.execute('UPDATE medical_cases SET payload=? WHERE patient=? AND encounter=?',
                    (json.dumps(state, ensure_ascii=False), self.patient, run['encounter_id']))
            if action == 'finish' and (not self.latest(run, 'review') or self.latest(run, 'review')['route'] != 'approve'):
                raise MemoryError('incomplete_review', 'Patient controller cannot bypass evidence review')
            run['controls'].append({'checkpoint': checkpoint, 'value': value})
            self._write_run(run)
            return value

    def save_baseline(self, run_id, value):
        from team_schemas import check_output
        check_output('visual', value)
        with self.store.transaction():
            run = self.run(run_id)
            if run['status'] != 'running' or 'visual_baseline' in run:
                raise MemoryError('baseline_conflict', 'An independent baseline is immutable per run')
            if any(r.get('source_cases') for r in value['regions']) or value.get('contributions'):
                raise MemoryError('invalid_evidence', 'Independent baseline cannot cite historical cases')
            self._check_evidence(run, 'visual', value)
            if any(v not in {i['image_id'] for i in run['input']['images']} for f in value['findings'] for v in f['supporting_views']):
                raise MemoryError('invalid_evidence', 'Independent baseline can cite current originals only')
            run['visual_baseline'] = value
            self._write_run(run)
            return value

    def visual_gains(self, run, value):
        """Only original/crop-supported new observations with a delivered region lineage."""
        baseline = run.get('visual_baseline')
        if baseline is None:
            return []  # Legacy runs cannot retrospectively manufacture novelty evidence.
        known = {f['description'].strip() for f in baseline['findings']}
        views = {v['view_id']: v for v in run['views']}
        findings = {f['id']: f for f in value['findings']}
        first = next((r for r in reversed(run.get('rerankings', [])) if r['key'] == 'key1'), {})
        selected = {(r['case_id'], r['version']) for r in first.get('selected', [])}
        gains = []
        for c in value.get('contributions', []):
            pair = (c['case_id'], c['version'])
            view, finding = views.get(c['view_id']), findings.get(c['finding_id'])
            if pair not in selected or not view or not finding or view['kind'] != 'crop':
                raise MemoryError('invalid_contribution', 'New evidence must cite a selected case, crop and finding')
            lineage = {(s['case_id'], s['version']) for s in view.get('source_cases', [])}
            if pair not in lineage or c['view_id'] not in finding['supporting_views'] or view['image_id'] != finding['image_id']:
                raise MemoryError('invalid_contribution', 'Finding is not grounded in the case-guided current region')
            if finding['certainty'] != 'clear' or finding['description'].strip() in known:
                continue
            # Lexical paraphrases are proposals, not automatic positive feedback. Doctor confirms novelty.
            gains.append({**c, 'description': finding['description'], 'status': 'awaiting_doctor'})
        return gains

    def stage_feedback(self, run_id, case_id, version, stage, successful, reason, finding_ids=None):
        self.doctor()
        validate(successful, {'type': 'boolean'})
        validate(reason, text())
        validate(stage, choice('visual', 'diagnosis'))
        validate(finding_ids or [], strings())
        run = self.run(run_id, current=False)
        key = 'key1' if stage == 'visual' else 'key2'
        rerank = next((r for r in reversed(run.get('rerankings', [])) if r['key'] == key), None)
        if not rerank or not any(r['case_id'] == case_id and r['version'] == version for r in rerank['selected']):
            raise MemoryError('not_selected', 'Only the selected reference at this stage can be evaluated')
        if stage == 'visual':
            gains = [g for g in run.get('visual_gains', []) if g['case_id'] == case_id and g['version'] == version]
            if successful and (not finding_ids or any(i not in {g['finding_id'] for g in gains} for i in finding_ids)):
                raise MemoryError('missing_new_evidence', 'Confirm actual new findings supported by a case-guided region')
            if not any({'case_id': case_id, 'version': version} in v.get('source_cases', []) for v in run['views'] if v['kind'] == 'mask'):
                raise MemoryError('missing_region', 'This reference did not provide a tested mask location')
        else:
            review = run.get('doctor_review') or {}
            if review.get('success') is not successful:
                raise MemoryError('missing_outcome', 'Confirm task success or failure in doctor review first')
        candidate = next(c for r in reversed(run['retrievals']) for c in r['cases'] if c['case_id'] == case_id and c['version'] == version)
        target = self.store if candidate['memory_scope'] == 'patient' else self.library
        ref = candidate['memory_reference']
        payload = json.dumps({'successful': successful, 'reason': reason, 'finding_ids': sorted(finding_ids or [])}, sort_keys=True)
        args = (target.patient_id, run_id, ref['category'], ref['record_id'], ref['version'], stage)
        with target.transaction():
            old = target.db.execute('SELECT payload FROM stage_verdicts WHERE patient=? AND consultation=? AND category=? AND record_id=? AND version=? AND stage=?', args).fetchone()
            if old:
                if old[0] != payload:
                    raise MemoryError('feedback_conflict', 'Stage verdict already exists')
                return {'duplicate': True, 'stage': stage}
            current = stage_value(target, ref, stage)
            updated = current if successful else max(target.settings['decay_floor'], round(current-target.settings['decay_step'], 10))
            target.db.execute('INSERT INTO stage_verdicts VALUES(?,?,?,?,?,?,?)', (*args, payload))
            target.db.execute('UPDATE stage_values SET value=? WHERE patient=? AND category=? AND record_id=? AND version=? AND stage=?', (updated, target.patient_id, ref['category'], ref['record_id'], ref['version'], stage))
            return {'duplicate': False, 'stage': stage, 'value': updated}

    def outcome(self, run_id, expected_revision, confirmed, successful, note):
        """A previously unknown result may be confirmed later without replaying review."""
        self.doctor()
        validate(successful, {'type': 'boolean'})
        validate(expected_revision, {'type': 'integer', 'minimum': 1})
        validate(note, text())
        if confirmed is not True:
            raise MemoryError('confirmation_required', 'Explicit outcome confirmation required')
        with self.store.transaction():
            run = self.run(run_id)
            if run['revision'] != expected_revision or run['status'] != 'doctor_reviewed':
                raise MemoryError('version_conflict', 'Review the current consultation first')
            review = run['doctor_review']
            if review.get('success') is not None:
                if review['success'] is not successful:
                    raise MemoryError('feedback_conflict', 'An established outcome cannot be reversed silently')
                return run
            review.update(success=successful, outcome_note=note)
            self._doctor_memory(run['encounter_id'], 'outcome:' + run_id, note, 'supplement')
            run['memory_epoch'] = self._memory_epoch()
            self._write_run(run)
            return run

    def lesson_submit(self, run_id, lesson_id, expected_version, confirmed, lesson, shared=False, deidentified=False):
        self.doctor()
        validate(lesson_id, text())
        validate(expected_version, {'type': 'integer', 'minimum': 0})
        validate(lesson, LESSON)
        if confirmed is not True:
            raise MemoryError('confirmation_required', 'Doctor must confirm the reusable lesson')
        validate(shared, {"type": "boolean"})
        validate(deidentified, {"type": "boolean"})
        if shared and not deidentified:
            raise MemoryError("publication_not_authorized", "Shared lessons require explicit deidentification and reuse authorization")
        run = self.run(run_id, current=False)
        if not run.get('doctor_review'):
            raise MemoryError('missing_review', 'Review the source consultation first')
        target = self.library if shared else self.store
        with target.transaction():
            old = target.db.execute('SELECT MAX(version) FROM doctor_lessons WHERE patient=? AND domain=? AND id=?', (target.patient_id, self.domain, lesson_id)).fetchone()[0] or 0
            if expected_version != old:
                raise MemoryError('version_conflict', 'Lesson changed; reload before editing')
            result = {**lesson, 'id': lesson_id, 'version': old+1, 'source_run': run_id if not shared else 'authorized-doctor-lesson', 'status': 'doctor_confirmed', 'shared': shared}
            target.db.execute('INSERT INTO doctor_lessons VALUES(?,?,?,?,?)', (target.patient_id, self.domain, lesson_id, old+1, json.dumps(result, ensure_ascii=False)))
            return result

    def lessons(self, query):
        values = []
        for target in (self.store, self.library):
            rows = target.db.execute('''SELECT a.payload FROM doctor_lessons a WHERE patient=? AND domain=?
                AND version=(SELECT MAX(b.version) FROM doctor_lessons b WHERE b.patient=a.patient AND b.domain=a.domain AND b.id=a.id)''', (target.patient_id, self.domain))
            admitted = rows.fetchmany(self.store.settings['max_scan_records'] + 1)
            if len(admitted) > self.store.settings['max_scan_records']:
                raise MemoryError('lesson_scan_limit', 'Lesson index exceeds bounded scan limit')
            values.extend(json.loads(r[0]) for r in admitted)
        scored = [(text_score(query, x['condition']), x) for x in values]
        return [x for score, x in sorted(scored, key=lambda p: (-p[0], p[1]['id'])) if score > 0][:5]
