"""Keyless tests of utility, questionnaire transitions and the doctor entry point."""

import json
import random
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from doctor_portal import DoctorApplication
from extensions import reference, select_ties
from schemas import MemoryError, tools
from server import call
from store import APPLICATION_ID, MemoryStore, settings_from_env
from workflow import PatientCommunicationAgent
from test_memory import lesion, narrative, query


def observation(field, value, status="known", **extra):
    return {"field": field, "value": value, "status": status, "source_ref": "synthetic/turn", "memory_refs": [], **extra}


def turn(event, revision, observations=None, **changes):
    return {"encounter_id": "v1", "event_id": event, "expected_revision": revision, "message": "合成测试输入",
            "intent": "provide_information", "observations": observations or [], "writes": [], "doctor_answers": [], **changes}


def doctor_input(event, revision, *, note=None, target="", observations=None, **changes):
    return {"encounter_id": "v1", "event_id": event, "expected_revision": revision, "confirmed": True,
            "note": note or narrative("doctor", "d1", description="请补充既往处理后的变化"),
            "expected_note_version": 0, "target_field": target, "observations": observations or [], "updates": [], **changes}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.patient = MemoryStore(self.path, "p1")
        self.doctor = MemoryStore(self.path, "p1", "doctor")
        self.agent = PatientCommunicationAgent(self.patient)
        self.doctor_agent = PatientCommunicationAgent(self.doctor)

    def tearDown(self):
        self.patient.close()
        self.doctor.close()
        self.temp.cleanup()

    def test_one_question_and_known_fields_are_not_repeated(self):
        first = self.agent.patient_turn(turn("t1", 0))
        self.assertEqual(first["next_action"]["field"], "lesion_location")
        second = self.agent.patient_turn(turn("t2", 1, [observation("lesion_location", "左侧颊黏膜")]))
        self.assertEqual(second["next_action"]["field"], "symptom_duration")
        self.assertEqual(len(self.patient.records("lesion")), 1)
        self.assertEqual(second["state"]["fields"]["lesion_location"]["status"], "known")
        self.assertNotIn("questions", second["next_action"])

    def test_existing_descriptor_hydrates_location(self):
        self.patient.put("lesion", {"record": lesion(), "expected_version": 0})
        state = self.agent.case_state({"encounter_id": "v1"})
        self.assertEqual(state["next_action"]["field"], "symptom_duration")
        self.assertEqual(state["revision"], 0)

    def test_retry_does_not_repeat_writes_exposure_or_questions(self):
        request = turn("t1", 0, [observation("lesion_location", "颊黏膜")])
        first = self.agent.patient_turn(request)
        second = self.agent.patient_turn(request)
        self.assertEqual(first, second)
        self.assertEqual(len(self.patient.records("lesion")), 1)
        self.assertEqual(self.patient.records("lesion")[0]["stats"]["exposure_count"], 1)
        with self.assertRaisesRegex(MemoryError, "reused"):
            self.agent.patient_turn({**request, "message": "changed"})

    def test_conflicts_are_kept_and_explicit_clarification_resolves(self):
        first = self.agent.patient_turn(turn("t1", 0, [observation("symptom_duration", "一月")]))
        second = self.agent.patient_turn(turn("t2", 1, [observation("symptom_duration", "两月")]))
        self.assertEqual(second["next_action"]["priority"], 2)
        self.assertEqual(second["state"]["fields"]["symptom_duration"]["value"], "一月")
        third = self.agent.patient_turn(turn("t3", 2, [observation("symptom_duration", "两月", resolves_conflict=True)]))
        self.assertEqual(third["state"]["fields"]["symptom_duration"]["status"], "known")
        self.assertEqual(third["state"]["fields"]["symptom_duration"]["value"], "两月")

    def test_uncertain_answers_are_bounded_and_pause_is_respected(self):
        self.agent.patient_turn(turn("t1", 0))
        self.agent.patient_turn(turn("t2", 1, [observation("lesion_location", "记不清", "uncertain")]))
        result = self.agent.patient_turn(turn("t3", 2, [observation("lesion_location", "还是记不清", "uncertain")]))
        self.assertNotEqual(result["next_action"].get("field"), "lesion_location")
        self.assertEqual(result["state"]["fields"]["lesion_location"]["asked_count"], 2)
        paused = self.agent.patient_turn(turn("t4", 3, intent="pause"))
        self.assertEqual(paused["next_action"]["action"], "paused")

    def test_denial_deferral_and_dependency_are_distinct(self):
        obs = [observation("lesion_location", "颊黏膜"), observation("symptom_duration", "一月"),
               observation("symptom_changes", "没有明显变化"), observation("prior_examination", "暂不提供", "deferred"),
               observation("prior_treatment", "没有", "denied"), observation("oral_experiences", "不清楚", "deferred")]
        result = self.agent.patient_turn(turn("t1", 0, obs))
        self.assertEqual(result["next_action"]["action"], "ready_for_review")
        self.assertEqual(result["state"]["fields"]["treatment_outcome"]["asked_count"], 0)

    def test_patient_question_takes_precedence_without_marking_new_question(self):
        result = self.agent.patient_turn(turn("t1", 0, intent="ask_question", message="怎么提供资料？"))
        self.assertEqual(result["next_action"]["action"], "answer_patient_first")
        self.assertEqual(result["state"]["fields"]["lesion_location"]["asked_count"], 0)

    def test_doctor_task_preempts_missing_fields_and_partial_reply_does_not_close(self):
        self.doctor_agent.doctor_submit(doctor_input("d1", 0, target="treatment_outcome"))
        first = self.agent.patient_turn(turn("t1", 1))
        self.assertEqual(first["next_action"]["priority"], 1)
        reply = {"record_id": "d1", "version": 1, "answer": "记不清", "sufficient": False, "source_ref": "synthetic/reply"}
        second = self.agent.patient_turn(turn("t2", 2, doctor_answers=[reply]))
        self.assertEqual(second["next_action"]["priority"], 1)
        self.assertEqual(len(self.agent.case_state({"encounter_id": "v1"})["open_doctor_questions"]), 1)
        full = {**reply, "answer": "患者补充了变化经过", "sufficient": True}
        self.agent.patient_turn(turn("t3", 3, [observation("treatment_outcome", "患者补充了变化经过")], doctor_answers=[full]))
        self.assertEqual(self.agent.case_state({"encounter_id": "v1"})["open_doctor_questions"], [])
        self.assertEqual(self.patient.get({"category": "doctor", "record_id": "d1", "include_versions": False})["record"]["question_status"], "open")

    def test_doctor_without_target_requires_doctor_confirmation(self):
        self.doctor_agent.doctor_submit(doctor_input("d1", 0))
        answer = {"record_id": "d1", "version": 1, "answer": "患者回复", "sufficient": True, "source_ref": "synthetic"}
        self.agent.patient_turn(turn("t1", 1, doctor_answers=[answer]))
        self.assertEqual(len(self.agent.case_state({"encounter_id": "v1"})["open_doctor_questions"]), 1)

    def test_cancelled_doctor_task_no_longer_blocks(self):
        self.doctor_agent.doctor_submit(doctor_input("d1", 0))
        note = {**narrative("doctor", "d1"), "question_status": "cancelled"}
        self.doctor_agent.doctor_submit(doctor_input("d2", 1, note=note, expected_note_version=1))
        self.assertEqual(self.agent.case_state({"encounter_id": "v1"})["open_doctor_questions"], [])

    def test_doctor_correction_is_atomic_and_patient_cannot_overwrite_confirmed_field(self):
        self.doctor_agent.doctor_submit(doctor_input("d1", 0,
            observations=[observation("symptom_duration", "医生确认时间")]))
        result = self.agent.patient_turn(turn("t1", 1, [observation("symptom_duration", "不同描述", resolves_conflict=True)]))
        field = result["state"]["fields"]["symptom_duration"]
        self.assertEqual(field["status"], "conflict")
        self.assertEqual(field["value"], "医生确认时间")
        self.assertTrue(field["confirmed"])
        bad = doctor_input("d2", 2, note=narrative("doctor", "other"), updates=[
            {"category": "care", "record": narrative("care", "new-care"), "expected_version": 99}])
        with self.assertRaises(MemoryError):
            self.doctor_agent.doctor_submit(bad)
        with self.assertRaises(MemoryError):
            self.doctor.get({"category": "doctor", "record_id": "other", "include_versions": False})
        self.assertEqual(self.agent.case_state({"encounter_id": "v1"})["revision"], 2)

    def test_bad_patient_turn_rolls_back_memory_and_case(self):
        request = turn("t1", 0, [observation("unknown-field", "bad")], writes=[
            {"category": "care", "record": narrative("care", "new-care"), "expected_version": 0}])
        with self.assertRaises(MemoryError):
            self.agent.patient_turn(request)
        self.assertEqual(self.patient.records("care"), [])
        self.assertEqual(self.agent.case_state({"encounter_id": "v1"})["revision"], 0)

    def test_case_revision_conflict_and_restart(self):
        self.agent.patient_turn(turn("t1", 0))
        with self.assertRaises(MemoryError):
            self.agent.patient_turn(turn("t2", 0))
        self.patient.close()
        self.patient = MemoryStore(self.path, "p1")
        self.agent = PatientCommunicationAgent(self.patient)
        self.assertEqual(self.agent.case_state({"encounter_id": "v1"})["revision"], 1)

    def _packet(self):
        return self.patient.search({"category": "experience", "query": "", "top_k": 10})

    def test_unseen_memory_stays_neutral_and_use_does_not_self_reward(self):
        self.patient.put("experience", {"record": narrative("experience", "e1"), "expected_version": 0})
        record = self.patient.records("experience")[0]
        self.assertEqual(record["stats"]["utility_score"], 0.5)
        explicit = call(self.patient, "get", {"category": "experience", "record_id": "e1", "include_versions": False})
        self.assertEqual(explicit["delivered_refs"], [reference(record)])
        packet = self._packet()
        feedback = {"event_id": "use1", "retrieval_id": packet["retrieval_id"], "reference": reference(record), "kind": "used", "reason": "用于追问"}
        self.patient.memory_feedback(feedback)
        self.patient.memory_feedback({**feedback, "event_id": "duplicate"})
        updated = self.patient.records("experience")[0]["stats"]
        self.assertEqual(updated["use_count"], 1)
        self.assertEqual(updated["utility_score"], 0.5)
        with self.assertRaises(MemoryError):
            self.patient.memory_feedback({**feedback, "kind": "helpful", "event_id": "forged"})

    def test_low_utility_decreases_frequency_without_deleting_and_new_version_resets(self):
        record = narrative("experience", "e1")
        self.patient.put("experience", {"record": record, "expected_version": 0})
        for index in range(10):
            packet = self._packet()
            self.doctor.memory_feedback({"event_id": f"negative{index}", "retrieval_id": packet["retrieval_id"],
                "reference": packet["delivered_refs"][0], "kind": "not_helpful", "reason": "医生确认本次无帮助"})
        current = self.patient.records("experience")[0]
        self.assertEqual(current["stats"]["frequency"], "low_frequency")
        self.assertEqual(len(self._packet()["matches"]), 1)
        self.assertEqual(len(self.patient.records("experience")), 1)
        self.patient.put("experience", {"record": {**record, "description": "补充了新内容"}, "expected_version": 1})
        self.assertEqual(self.patient.records("experience")[0]["stats"]["utility_score"], 0.5)

    def test_feedback_requires_exposed_version_and_preserves_event_identity(self):
        self.patient.put("experience", {"record": narrative("experience", "e1"), "expected_version": 0})
        packet = self._packet()
        feedback = {"event_id": "help", "retrieval_id": packet["retrieval_id"], "reference": packet["delivered_refs"][0], "kind": "helpful", "reason": "医生确认"}
        self.doctor.memory_feedback(feedback)
        with self.assertRaises(MemoryError):
            self.doctor.memory_feedback({**feedback, "reason": "different"})
        with self.assertRaises(MemoryError):
            self.doctor.memory_feedback({**feedback, "event_id": "other", "reference": {**feedback["reference"], "version": 2}})
        with self.assertRaisesRegex(MemoryError, "already has"):
            self.doctor.memory_feedback({**feedback, "event_id": "opposite", "kind": "not_helpful"})

    def test_required_context_bypasses_low_frequency(self):
        self.doctor_agent.doctor_submit(doctor_input("d1", 0))
        record = self.doctor.records("doctor")[0]
        result = self.doctor.set_frequency({"reference": reference(record), "frequency": "low_frequency", "protected": False, "reason": "测试"})
        self.assertEqual(result["stats"]["frequency"], "normal")
        retrieved = self.patient.retrieve(query(lesion_query={"site": "不存在的部位"}))
        self.assertEqual(retrieved["status"], "no_match")
        self.assertEqual(len(retrieved["required_context"]["doctor_questions"]), 1)

    def test_sampling_respects_relevance_and_retains_exploration(self):
        settings = settings_from_env({})
        items = [{"score": 1, "record": {"record_id": "normal", "stats": {"utility_score": .8, "frequency": "normal"}}},
                 {"score": 1, "record": {"record_id": "low", "stats": {"utility_score": .1, "frequency": "low_frequency"}}}]
        rng = random.Random(42)
        hits = [select_ties(items, 1, ["score"], "record", settings, rng)[0]["record"]["record_id"] for _ in range(500)]
        self.assertGreater(hits.count("normal"), hits.count("low"))
        self.assertGreater(hits.count("low"), 0)
        items[1]["score"] = .5
        for _ in range(20):
            self.assertEqual(select_ties(items, 1, ["score"], "record", settings, rng)[0]["record"]["record_id"], "normal")

    def test_v1_database_upgrade_preserves_memory_and_revisions(self):
        self.patient.put("experience", {"record": narrative("experience", "e1"), "expected_version": 0})
        old_rows = list(self.patient.db.execute("SELECT * FROM revisions"))
        self.patient.close()
        self.doctor.close()
        connection = sqlite3.connect(self.path)
        for table in ("memory_stats", "delivery_packets", "memory_feedback", "inquiry_cases", "workflow_events", "doctor_tasks", "doctor_answers", "frequency_events"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        connection.close()
        self.patient = MemoryStore(self.path, "p1")
        self.doctor = MemoryStore(self.path, "p1", "doctor")
        self.assertEqual([tuple(row) for row in self.patient.db.execute("SELECT * FROM revisions")], [tuple(row) for row in old_rows])
        self.assertEqual(self.patient.db.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_doctor_entry_authentication_explicit_confirmation_and_shared_state(self):
        application = DoctorApplication(self.path, "p1", "synthetic-secret-token")
        self.assertEqual(application.handle("GET", "/api/state?encounter_id=v1", {})[0], 401)
        headers = {"Authorization": "Bearer synthetic-secret-token", "Content-Type": "application/json"}
        self.assertEqual(application.handle("POST", "/api/submit", headers, json.dumps(doctor_input("d1", 0, confirmed=False)).encode())[0], 400)
        response = application.handle("POST", "/api/submit", headers, json.dumps(doctor_input("d1", 0, target="prior_treatment")).encode())
        self.assertEqual(response[0], 200, response[2])
        view = json.loads(application.handle("GET", "/api/state?encounter_id=v1", headers)[2])
        self.assertEqual(view["patient_id"], "p1")
        self.assertEqual(view["revision"], 1)
        self.assertEqual(view["next_action"]["priority"], 1)
        self.assertEqual(application.handle("GET", "/api/state?encounter_id=v1", {**headers, "Origin": "http://evil", "Host": "127.0.0.1"})[0], 403)

    def test_patient_mcp_cannot_submit_doctor_input_or_set_frequency(self):
        names = [tool["name"] for tool in tools("patient")]
        self.assertNotIn("doctor_submit", names)
        self.assertNotIn("set_frequency", names)
        with self.assertRaises(MemoryError):
            call(self.patient, "doctor_submit", doctor_input("d1", 0))
        self.assertFalse(any("delete" in name for name in names))


if __name__ == "__main__":
    unittest.main()
