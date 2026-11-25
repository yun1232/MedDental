"""Persistent patient communication planning and explicitly confirmed doctor input."""

import hashlib
import json
import os
from pathlib import Path

from extensions import reference
from retrieval import known
from schemas import CASE, DOCTOR_SUBMIT, PATIENT_TURN, MemoryError, validate


def load_questionnaire():
    """Load a deployment-owned template; it is not a diagnostic decision protocol."""
    path = Path(os.environ.get("ORAL_MEMORY_QUESTIONNAIRE", Path(__file__).with_name("questionnaire.json")))
    data = json.loads(path.read_text(encoding="utf-8"))
    fields = data.get("fields")
    if not isinstance(fields, list) or not fields or type(data.get("version")) is not int:
        raise MemoryError("invalid_config", "Questionnaire requires a version and nonempty fields")
    ids = []
    for field in fields:
        if not isinstance(field, dict) or not all(isinstance(field.get(key), str) and field[key].strip() for key in ("id", "label", "question")):
            raise MemoryError("invalid_config", "Questionnaire fields require id, label and question")
        if type(field.get("required")) is not bool or field["id"] in ids:
            raise MemoryError("invalid_config", "Questionnaire required flag or duplicate field is invalid")
        if "depends_on" in field and field["depends_on"] not in ids:
            raise MemoryError("invalid_config", "Questionnaire dependencies must name an earlier field")
        ids.append(field["id"])
    return data


class PatientCommunicationAgent:
    """Select one question from doctor tasks, conflicts and configured information gaps."""

    def __init__(self, store):
        self.store = store
        self.template = load_questionnaire()
        self.fields = {field["id"]: field for field in self.template["fields"]}

    def _read(self, encounter):
        row = self.store.db.execute("SELECT revision,state FROM inquiry_cases WHERE patient_id=? AND encounter_id=?",
                                    (self.store.patient_id, encounter)).fetchone()
        if row:
            state = json.loads(row["state"])
            if state["template_version"] != self.template["version"] or set(state["fields"]) != set(self.fields):
                raise MemoryError("template_changed", "Persisted questionnaire differs; migrate its fields explicitly")
            return row["revision"], state
        state = {"template_version": self.template["version"], "fields": {field: {
            "status": "unknown", "value": "", "asked_count": 0, "evidence": [], "alternatives": [],
            "confirmed": False} for field in self.fields}, "pending_question": None, "doctor_attempts": {}}
        lesions = [record for record in self.store.records("lesion") if record["encounter_id"] == encounter]
        if lesions and "lesion_location" in state["fields"]:
            latest = max(lesions, key=lambda record: record["updated_order"])
            if known(latest["features"]["site"]):
                state["fields"]["lesion_location"].update(status="known", value=latest["features"]["site"],
                    evidence=[{"source_ref": latest["source_ref"], "memory_refs": [reference(latest)], "actor": latest["source"]}],
                    confirmed=latest["review_status"] == "doctor_confirmed")
        return 0, state

    def _open_tasks(self, encounter=None, state=None):
        notes = [record for record in self.store.records("doctor") if record["note_type"] == "follow_up"
                 and record["question_status"] == "open" and (encounter is None or record["encounter_id"] == encounter)]
        result = []
        for note in notes:
            meta = self.store.db.execute("SELECT target_field FROM doctor_tasks WHERE patient_id=? AND record_id=? AND version=?",
                (self.store.patient_id, note["record_id"], note["version"])).fetchone()
            field = meta["target_field"] if meta else ""
            answers = self.store.db.execute("""SELECT answer,sufficient,source_ref FROM doctor_answers
                WHERE patient_id=? AND encounter_id=? AND record_id=? AND version=? ORDER BY rowid""",
                (self.store.patient_id, note["encounter_id"], note["record_id"], note["version"])).fetchall()
            note_state = state if state is not None and encounter == note["encounter_id"] else self._read(note["encounter_id"])[1]
            complete = bool(field and note_state["fields"].get(field, {}).get("status") in ("known", "denied")
                            and any(answer["sufficient"] for answer in answers))
            if not complete:
                result.append({"record": note, "target_field": field, "answers": [dict(answer) for answer in answers]})
        return sorted(result, key=lambda item: item["record"]["updated_order"])

    def required_context(self, encounter=None):
        """Read unresolved doctor tasks, effective diagnoses and conflicts without frequency filtering."""
        tasks = self._open_tasks(encounter)
        finals = {}
        for record in self.store.records("doctor"):
            if record["note_type"] == "final_diagnosis" and (encounter is None or record["encounter_id"] == encounter):
                key = record["encounter_id"]
                if key not in finals or record["updated_order"] > finals[key]["updated_order"]:
                    finals[key] = record
        rows = self.store.db.execute("SELECT encounter_id,state FROM inquiry_cases WHERE patient_id=?", (self.store.patient_id,)).fetchall()
        conflicts = []
        for row in rows:
            if encounter is None or encounter == row["encounter_id"]:
                for field, value in json.loads(row["state"])["fields"].items():
                    if value["status"] == "conflict":
                        conflicts.append({"encounter_id": row["encounter_id"], "field": field, "details": value})
        return {"doctor_questions": tasks, "current_diagnoses": list(finals.values()), "conflicts": conflicts}

    def case_state(self, arguments):
        """Read the current case revision without marking a previewed question as asked."""
        validate(arguments, CASE)
        with self.store.transaction():
            revision, state = self._read(arguments["encounter_id"])
            tasks = self._open_tasks(arguments["encounter_id"], state)
            return {"encounter_id": arguments["encounter_id"], "revision": revision, "state": state,
                    "questionnaire": self.template,
                    "open_doctor_questions": tasks, "next_action": ({"action": "decide_from_cases", "pending_question": state["pending_question"]} if self.store.settings.get("case_driven") else self._plan(state, tasks, mark=False))}

    def _cached(self, kind, arguments):
        digest = hashlib.sha256((kind + json.dumps(arguments, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
        row = self.store.db.execute("SELECT request_hash,response FROM workflow_events WHERE patient_id=? AND event_id=?",
                                    (self.store.patient_id, arguments["event_id"])).fetchone()
        if row:
            if row["request_hash"] != digest:
                raise MemoryError("event_conflict", "Workflow event ID was reused with different data")
            return digest, json.loads(row["response"])
        return digest, None

    def _save(self, arguments, state, revision, digest, response):
        self.store.db.execute("""INSERT INTO inquiry_cases VALUES (?,?,?,?) ON CONFLICT(patient_id,encounter_id)
            DO UPDATE SET revision=excluded.revision,state=excluded.state""", (self.store.patient_id,
            arguments["encounter_id"], revision, json.dumps(state, ensure_ascii=False)))
        self.store.db.execute("INSERT INTO workflow_events VALUES (?,?,?,?,?)", (self.store.patient_id,
            arguments["event_id"], digest, json.dumps(response, ensure_ascii=False), json.dumps(arguments, ensure_ascii=False)))

    def _observe(self, state, observations, doctor=False):
        for observation in observations:
            field = observation["field"]
            if field not in state["fields"]:
                raise MemoryError("invalid_input", f"Unknown inquiry field: {field}")
            if observation["status"] == "known" and not observation["value"].strip():
                raise MemoryError("invalid_input", "Known information requires a nonempty value")
            for ref in observation["memory_refs"]:
                record = self.store.get({"category": ref["category"], "record_id": ref["record_id"], "include_versions": True})
                if not any(version["version"] == ref["version"] for version in record["versions"]):
                    raise MemoryError("not_found", "Inquiry evidence references an unavailable version")
            current = state["fields"][field]
            evidence = {"source_ref": observation["source_ref"], "memory_refs": observation["memory_refs"],
                        "actor": "doctor" if doctor else "patient"}
            changed = current["status"] in ("known", "denied") and (
                observation["status"] != current["status"] or observation["value"] != current["value"])
            clarify = bool(observation.get("resolves_conflict") and not current["confirmed"]
                           and observation["status"] in ("known", "denied") and observation["value"] in
                           [current["value"], *[item["value"] for item in current["alternatives"]]])
            if not doctor and not clarify and (current["status"] == "conflict" or changed):
                current["alternatives"].append({"status": observation["status"], "value": observation["value"], "evidence": evidence})
                current["status"] = "conflict"
            else:
                current.update(status=observation["status"], value=observation["value"], confirmed=doctor or current["confirmed"])
                if doctor or clarify:
                    current["alternatives"] = []
            current["evidence"].append(evidence)
            if state["pending_question"] and state["pending_question"].get("field") == field:
                state["pending_question"] = None

    def _plan(self, state, tasks, mark):
        maximum = self.store.settings["max_question_attempts"]
        blocked_tasks = []
        for task in tasks:
            record = task["record"]
            key = f"{record['record_id']}:{record['version']}"
            if state["doctor_attempts"].get(key, 0) >= maximum:
                blocked_tasks.append(record["record_id"])
                continue
            pending = state["pending_question"]
            if pending and pending.get("doctor_key") == key:
                return {"action": "await_answer", **pending}
            question = {"priority": 1, "field": task["target_field"], "doctor_key": key,
                        "doctor_record_id": record["record_id"], "question": record["description"]}
            if mark:
                state["doctor_attempts"][key] = state["doctor_attempts"].get(key, 0) + 1
                if task["target_field"]:
                    state["fields"][task["target_field"]]["asked_count"] += 1
                state["pending_question"] = question
            return {"action": "ask", **question}
        pending = state["pending_question"]
        urgent_conflict = any(value["status"] == "conflict" and value["asked_count"] < maximum for value in state["fields"].values())
        if pending and not pending.get("doctor_key") and not urgent_conflict and state["fields"].get(pending["field"], {"status": "unknown"})["status"] not in ("known", "denied", "deferred"):
            return {"action": "await_answer", **pending}
        for priority, statuses in ((2, ("conflict",)), (3, ("unknown", "uncertain"))):
            for field, spec in sorted(self.fields.items(), key=lambda item: not item[1]["required"]):
                current = state["fields"][field]
                dependency = spec.get("depends_on")
                if dependency and state["fields"][dependency]["status"] != "known":
                    continue
                if current["status"] not in statuses or current["asked_count"] >= maximum:
                    continue
                question_text = spec["question"]
                if current["status"] == "conflict":
                    alternatives = [item["value"] for item in current["alternatives"] if item["value"]]
                    question_text = f"关于{spec['label']}，已有描述为“{current['value']}”，另有描述“{'；'.join(alternatives)}”。您方便澄清一下吗？"
                question = {"priority": priority, "field": field, "question": question_text}
                if mark:
                    current["asked_count"] += 1
                    state["pending_question"] = question
                return {"action": "ask", **question}
        gaps = [field for field, spec in self.fields.items() if spec["required"] and state["fields"][field]["status"] not in ("known", "denied")]
        return {"action": "ready_for_review_with_gaps" if gaps or blocked_tasks else "ready_for_review",
                "missing_required_fields": gaps, "doctor_questions_needing_review": blocked_tasks}

    def _remember_observations(self, arguments, saved):
        observations = json.loads(json.dumps(arguments["observations"]))
        for index, item in enumerate(observations):
            if item["memory_refs"]:
                continue
            if item["field"] not in self.fields:
                raise MemoryError("invalid_input", f"Unknown inquiry field: {item['field']}")
            record_id = "turn-" + hashlib.sha256((arguments["event_id"] + ":" + str(index)).encode()).hexdigest()[:24]
            common = {"record_id": record_id, "encounter_id": arguments["encounter_id"],
                "event_time": item.get("event_time", "unknown（本轮患者陈述）"), "source_ref": item["source_ref"],
                "source": "patient", "review_status": "unreviewed"}
            description = f"{self.fields[item['field']]['label']}；状态={item['status']}；患者描述={item['value']}"
            if item["field"] == "lesion_location" and item["status"] == "known":
                category = "lesion"
                record = {**common, "lesion_id": arguments["encounter_id"] + ":focus",
                    "features": {"site": item["value"], "side": "unknown", "tooth_positions": [], "color": "unknown",
                                 "morphology": "unknown", "surface": "unknown", "border": "unknown", "extent": "unknown"},
                    "size_description": "unknown", "pathology": {"status": "unknown", "result": "", "source_ref": ""}}
            elif item["field"] in ("prior_examination", "prior_treatment", "treatment_outcome"):
                category = "care"
                record = {**common, "lesion_ids": [], "care_type": {"prior_examination": "examination",
                    "prior_treatment": "visit", "treatment_outcome": "follow_up"}[item["field"]], "description": description, "outcome": ""}
            else:
                category = "experience"
                record = {**common, "lesion_ids": [], "experience_type": "other_oral", "description": description}
            result = self.store.put(category, {"record": record, "expected_version": 0})
            saved.append(result)
            item["memory_refs"] = [reference(result["record"])]
        return observations

    def patient_turn(self, arguments):
        """Atomically save a turn, extracted memories, observations and one next question."""
        validate(arguments, PATIENT_TURN)
        with self.store.transaction():
            digest, cached = self._cached("patient_turn", arguments)
            if cached is not None:
                return cached
            revision, state = self._read(arguments["encounter_id"])
            if revision != arguments["expected_revision"]:
                raise MemoryError("version_conflict", f"Current case revision is {revision}")
            saved = []
            for write in arguments["writes"]:
                if write["record"].get("encounter_id") != arguments["encounter_id"]:
                    raise MemoryError("invalid_input", "Turn writes must belong to its encounter")
                saved.append(self.store.put(write["category"], {"record": write["record"], "expected_version": write["expected_version"]}))
            self._observe(state, self._remember_observations(arguments, saved))
            for answer in arguments["doctor_answers"]:
                task = self.store.get({"category": "doctor", "record_id": answer["record_id"], "include_versions": False})["record"]
                if task["encounter_id"] != arguments["encounter_id"] or task["version"] != answer["version"] or task.get("question_status") != "open":
                    raise MemoryError("invalid_input", "Doctor answer must reference an open question in this encounter")
                meta = self.store.db.execute("SELECT target_field FROM doctor_tasks WHERE patient_id=? AND record_id=? AND version=?",
                    (self.store.patient_id, task["record_id"], task["version"])).fetchone()
                field = meta["target_field"] if meta else ""
                sufficient = bool(answer["sufficient"] and field and state["fields"][field]["status"] in ("known", "denied"))
                self.store.db.execute("INSERT INTO doctor_answers VALUES (?,?,?,?,?,?,?,?)", (self.store.patient_id,
                    arguments["encounter_id"], task["record_id"], task["version"], arguments["event_id"], answer["answer"],
                    int(sufficient), answer["source_ref"]))
                if state["pending_question"] and state["pending_question"].get("doctor_record_id") == task["record_id"]:
                    state["pending_question"] = None
            tasks = self._open_tasks(arguments["encounter_id"], state)
            if arguments["intent"] == "pause":
                next_action = {"action": "paused"}
            elif arguments["intent"] == "ask_question":
                next_action = {"action": "answer_patient_first", "patient_question": arguments["message"], "resume": self._plan(state, tasks, False)}
            elif self.store.settings.get('case_driven'):
                next_action = {'action': 'decide_from_cases', 'doctor_questions': tasks,
                    'reason': 'Patient communication agent chooses a missing fact using retrieved cases'}
            else:
                next_action = self._plan(state, tasks, True)
            response = {"encounter_id": arguments["encounter_id"], "revision": revision+1,
                        "state": state, "saved_memories": saved, "next_action": next_action}
            # Persist the updated case before assembling mandatory conflict context.
            self.store.db.execute("""INSERT INTO inquiry_cases VALUES (?,?,?,?) ON CONFLICT(patient_id,encounter_id)
                DO UPDATE SET revision=excluded.revision,state=excluded.state""", (self.store.patient_id,
                arguments["encounter_id"], revision+1, json.dumps(state, ensure_ascii=False)))
            events = [record for record in self.store.records("patient_experience") if record["encounter_id"] == arguments["encounter_id"]]
            history_query = " ".join(r["description"] + " " + r.get("outcome", "") for r in events)
            response["memory_context"] = self.store.retrieve({"purpose": "visual", "patient_experience_query": history_query,
                **({"exclude_record_ids": [r['record_id'] for category in ('lesion','experience','care','doctor') for r in self.store.records(category) if r['encounter_id'] == arguments['encounter_id']]} if self.store.settings.get('case_driven') else {"encounter_id": arguments["encounter_id"]}), "lesion_query": {"text": "unknown"},
                "care_query": "", "experience_query": "", "doctor_query": "", "top_k": 5})
            self._save(arguments, state, revision+1, digest, response)
            return response

    def patient_decide(self, arguments):
        """The patient agent chooses the question after inspecting a retrieval receipt."""
        from schemas import PATIENT_DECIDE
        validate(arguments, PATIENT_DECIDE)
        with self.store.transaction():
            digest, cached = self._cached("patient_decide", arguments)
            if cached is not None:
                return cached
            revision, state = self._read(arguments['encounter_id'])
            if revision != arguments['expected_revision']:
                raise MemoryError('version_conflict', 'Re-read inquiry state')
            receipt = self.store.db.execute('SELECT refs FROM delivery_packets WHERE patient_id=? AND retrieval_id=?',
                (self.store.patient_id, arguments['retrieval_id'])).fetchone()
            if receipt is None or not arguments['case_refs'] or any(r not in json.loads(receipt[0]) for r in arguments['case_refs']):
                raise MemoryError('invalid_reference', 'Question must cite cases in a delivered receipt')
            field = arguments['field']
            if field in state['fields']:
                value = state['fields'][field]
                if value['status'] in ('known', 'denied', 'deferred') or value['asked_count'] >= self.store.settings['max_question_attempts']:
                    raise MemoryError('question_blocked', 'Respect answered, declined and exhausted fields')
                value['asked_count'] += 1
            state.setdefault('case_question_attempts', {})
            attempts = state['case_question_attempts'].get(field, 0)
            if attempts >= self.store.settings['max_question_attempts']:
                raise MemoryError('question_blocked', 'Question attempt budget exhausted')
            state['case_question_attempts'][field] = attempts + 1
            state['pending_question'] = {'question': arguments['question'], 'field': field,
                'case_refs': arguments['case_refs'], 'retrieval_id': arguments['retrieval_id']}
            result = {'encounter_id': arguments['encounter_id'], 'revision': revision+1, 'state': state,
                'next_action': {'action': 'ask', **state['pending_question']}}
            self._save(arguments, state, revision+1, digest, result)
            return result

    def doctor_submit(self, arguments):
        """Commit confirmed doctor input, corrected memories and case state together."""
        validate(arguments, DOCTOR_SUBMIT)
        if self.store.role != "doctor" or arguments["confirmed"] is not True:
            raise MemoryError("forbidden", "An explicitly confirmed doctor-bound submission is required")
        if arguments["target_field"] and arguments["target_field"] not in self.fields:
            raise MemoryError("invalid_input", "Doctor target field is absent from the questionnaire")
        if arguments["note"]["encounter_id"] != arguments["encounter_id"]:
            raise MemoryError("invalid_input", "Doctor note must belong to its encounter")
        with self.store.transaction():
            digest, cached = self._cached("doctor_submit", arguments)
            if cached is not None:
                return cached
            revision, state = self._read(arguments["encounter_id"])
            if revision != arguments["expected_revision"]:
                raise MemoryError("version_conflict", f"Current case revision is {revision}")
            note = self.store.put("doctor", {"record": arguments["note"], "expected_version": arguments["expected_note_version"]})["record"]
            for update in arguments["updates"]:
                if update["record"].get("encounter_id") != arguments["encounter_id"]:
                    raise MemoryError("invalid_input", "Doctor corrections must belong to the encounter")
                self.store.put(update["category"], {"record": update["record"], "expected_version": update["expected_version"]})
            observations = json.loads(json.dumps(arguments["observations"]))
            for observation in observations:
                if not observation["memory_refs"]:
                    observation["memory_refs"] = [reference(note)]
            self._observe(state, observations, doctor=True)
            if note["note_type"] == "follow_up":
                self.store.db.execute("""INSERT INTO doctor_tasks VALUES (?,?,?,?,?) ON CONFLICT(patient_id,record_id,version)
                    DO UPDATE SET target_field=excluded.target_field""", (self.store.patient_id,
                    note["record_id"], note["version"], note["encounter_id"], arguments["target_field"]))
                if state["pending_question"] and state["pending_question"].get("doctor_record_id") == note["record_id"]:
                    state["pending_question"] = None
            response = {"encounter_id": arguments["encounter_id"], "revision": revision+1, "note": note, "state": state}
            self._save(arguments, state, revision+1, digest, response)
            return response
