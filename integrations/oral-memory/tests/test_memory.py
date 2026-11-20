"""Persistence, priority, separation and real stdio MCP regression tests."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from retrieval import phenotype_score, render_lesion
from schemas import MemoryError, tools
from server import call, dispatch
from store import MemoryStore, settings_from_env


def lesion(record_id="l1", encounter="v1", **features):
    """Return a synthetic descriptor, unrelated to any real patient."""
    return {"record_id": record_id, "encounter_id": encounter, "event_time": "约一月前",
            "source_ref": "synthetic/image", "source": "model", "review_status": "unreviewed",
            "lesion_id": record_id, "features": {"site": "颊黏膜", "side": "left",
                "tooth_positions": [], "color": "白色", "morphology": "片状", "surface": "unknown",
                "border": "unknown", "extent": "unknown", **features},
            "size_description": "unknown", "pathology": {"status": "unknown", "result": "", "source_ref": ""}}


def narrative(category, record_id, encounter="v1", description="synthetic", links=None):
    """Return one synthetic non-image record in a separately stored category."""
    record = {"record_id": record_id, "encounter_id": encounter, "event_time": "约一月前",
              "source_ref": "synthetic/turn-1", "source": "patient", "review_status": "unreviewed",
              "lesion_ids": [] if links is None else links, "description": description}
    if category == "experience":
        record["experience_type"] = "symptom"
    elif category == "care":
        record.update(care_type="surgery", outcome="")
    else:
        record.update(source="doctor", review_status="doctor_confirmed", note_type="follow_up",
                      related_record_ids=[], question_status="open", answer="")
    return record


def query(**changes):
    """Build a complete two-stage query with explicitly separate text channels."""
    return {"lesion_query": {"site": "颊黏膜", "color": "白色"}, "care_query": "手术切除",
            "experience_query": "", "doctor_query": "", "top_k": 10, **changes}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.store = MemoryStore(self.path, "p1")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def put(self, category, record, version=0):
        return self.store.put(category, {"record": record, "expected_version": version})

    def test_four_categories_persist_and_patient_scope_survives_restart(self):
        self.put("lesion", lesion())
        self.put("experience", narrative("experience", "e1"))
        self.put("care", narrative("care", "c1"))
        doctor = MemoryStore(self.path, "p1", "doctor")
        doctor.put("doctor", {"record": narrative("doctor", "d1"), "expected_version": 0})
        doctor.close()
        other = MemoryStore(self.path, "p2")
        self.assertEqual(other.search({"category": "lesion", "query": "", "top_k": 10})["matches"], [])
        other.close()
        self.store.close()
        self.store = MemoryStore(self.path, "p1")
        for category in ("lesion", "experience", "care", "doctor"):
            self.assertEqual(len(self.store.records(category)), 1)

    def test_care_match_cannot_override_better_phenotype(self):
        self.put("lesion", lesion("better", "v1"))
        self.put("lesion", lesion("worse", "v2", site="舌侧"))
        self.put("care", narrative("care", "c2", "v2", "手术切除"))
        result = self.store.retrieve(query())
        self.assertEqual([item["lesion"]["record_id"] for item in result["candidates"]], ["better", "worse"])
        self.assertEqual(result["candidates"][0]["care_score"], 0)
        self.assertEqual(result["candidates"][1]["care_score"], 1)

    def test_care_breaks_phenotype_tie(self):
        self.put("lesion", lesion("with_care", "v1"))
        self.put("care", narrative("care", "c1", "v1", "手术切除"))
        self.put("lesion", lesion("newer_without_care", "v2"))
        result = self.store.retrieve(query())
        self.assertEqual(result["candidates"][0]["lesion"]["record_id"], "with_care")

    def test_unrelated_care_cannot_leak_through_same_encounter(self):
        self.put("lesion", lesion("l1"))
        self.put("care", narrative("care", "wrong", "v1", "手术切除", ["other_lesion"]))
        self.put("care", narrative("care", "linked", "older_visit", "手术切除", ["l1"]))
        result = self.store.retrieve(query())
        self.assertEqual([item["record"]["record_id"] for item in result["candidates"][0]["care"]], ["linked"])

    def test_experience_and_doctor_text_never_affect_care_score(self):
        self.put("lesion", lesion())
        self.put("experience", narrative("experience", "e1", description="手术切除"))
        result = self.store.retrieve(query(experience_query="手术切除"))
        self.assertEqual(result["candidates"][0]["care_score"], 0)
        self.assertEqual(len(result["supplementary"]["experience"]), 1)
        self.assertEqual(self.store.search({"category": "care", "query": "手术切除", "top_k": 10})["matches"], [])

    def test_unknown_does_not_match_unknown_and_empty_query_needs_clarification(self):
        self.put("lesion", lesion())
        self.assertEqual(self.store.retrieve(query(lesion_query={"surface": "unknown"}))["status"], "needs_clarification")
        self.assertEqual(self.store.retrieve(query(lesion_query={"surface": "结节状"}))["status"], "no_match")
        score, _ = phenotype_score({"surface": "unknown"}, lesion(), self.store.settings["weights"])
        self.assertIsNone(score)

    def test_lexical_query_and_tooth_reference_are_independent(self):
        self.put("lesion", lesion(tooth_positions=["reference-1"]))
        self.assertEqual(self.store.retrieve(query(lesion_query={"text": "白色颊黏膜"}))["status"], "ok")
        self.assertEqual(self.store.retrieve(query(lesion_query={"tooth_positions": ["reference-1"]}))["status"], "ok")
        self.assertEqual(self.store.retrieve(query(lesion_query={"tooth_positions": ["reference-2"]}))["status"], "no_match")

    def test_idempotency_conflict_and_immutable_versions(self):
        record = narrative("experience", "e1", description="第一次描述")
        self.put("experience", record)
        self.assertTrue(self.put("experience", record)["idempotent"])
        updated = {**record, "description": "医生补充后的描述"}
        with self.assertRaisesRegex(MemoryError, "current version is 1"):
            self.put("experience", updated)
        self.put("experience", updated, 1)
        read = self.store.get({"category": "experience", "record_id": "e1", "include_versions": True})
        self.assertEqual([item["version"] for item in read["versions"]], [1, 2])
        self.assertEqual(read["versions"][0]["record"]["description"], "第一次描述")
        with self.assertRaisesRegex(MemoryError, "identities are immutable"):
            self.put("experience", {**updated, "encounter_id": "different"}, 2)

    def test_doctor_follow_up_and_latest_diagnosis_are_separately_queryable(self):
        self.store.close()
        self.store = MemoryStore(self.path, "p1", "doctor")
        note = narrative("doctor", "question")
        self.put("doctor", note)
        for identifier in ("diagnosis_1", "diagnosis_2"):
            self.put("doctor", {**narrative("doctor", identifier), "note_type": "final_diagnosis",
                                "question_status": "not_applicable"})
        pending = self.store.search({"category": "doctor", "query": "", "note_type": "follow_up",
                                     "question_status": "open", "top_k": 10})
        self.assertEqual(len(pending["matches"]), 1)
        self.put("doctor", {**note, "question_status": "answered", "answer": "患者提供补充"}, 1)
        self.assertEqual(self.store.search({"category": "doctor", "query": "", "question_status": "open", "top_k": 10})["matches"], [])
        final = self.store.search({"category": "doctor", "query": "", "note_type": "final_diagnosis", "top_k": 1})
        self.assertEqual(final["matches"][0]["record"]["record_id"], "diagnosis_2")

    def test_patient_cannot_forge_doctor_source_or_update_confirmed_records(self):
        self.assertNotIn("put_doctor", [tool["name"] for tool in tools("patient")])
        with self.assertRaises(MemoryError):
            self.put("doctor", narrative("doctor", "d1"))
        with self.assertRaises(MemoryError):
            self.put("experience", {**narrative("experience", "e1"), "source": "doctor"})
        doctor = MemoryStore(self.path, "p1", "doctor")
        record = {**narrative("care", "c1"), "source": "doctor", "review_status": "doctor_confirmed"}
        doctor.put("care", {"record": record, "expected_version": 0})
        doctor.close()
        with self.assertRaises(MemoryError):
            self.put("care", {**record, "source": "patient", "review_status": "unreviewed", "description": "overwrite"}, 1)

    def test_images_forged_patient_and_unfounded_pathology_are_rejected(self):
        for extra in ({"image_base64": "AAAA"}, {"patient_id": "p2"}):
            with self.assertRaises(MemoryError):
                self.put("lesion", {**lesion(), **extra})
        record = lesion()
        record["pathology"]["result"] = "unfounded"
        with self.assertRaisesRegex(MemoryError, "inferred"):
            self.put("lesion", record)

    def test_phenotype_recall_limit_and_threshold_are_explicit(self):
        for i in range(3):
            self.put("lesion", lesion(f"l{i}", f"v{i}"))
        self.store.settings["recall_limit"] = 2
        result = self.store.retrieve(query())
        self.assertTrue(result["stage1_truncated"])
        self.assertEqual(result["stage1_match_count"], 3)
        self.assertEqual(len(result["candidates"]), 2)
        self.store.settings["min_phenotype_score"] = 1
        self.assertEqual(self.store.retrieve(query(lesion_query={"site": "舌侧", "color": "白色"}))["status"], "no_match")

    def test_original_database_is_not_repurposed(self):
        path = Path(self.temp.name) / "other.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.close()
        with self.assertRaisesRegex(MemoryError, "non-memory"):
            MemoryStore(path, "p1")

    def test_invalid_config_inputs_and_mcp_errors(self):
        for env in ({"ORAL_MEMORY_MIN_PHENOTYPE_SCORE": "nan"}, {"ORAL_MEMORY_FIELD_WEIGHTS": "{\"color\":0}"},
                    {"ORAL_MEMORY_RECALL_LIMIT": "-1"}, {"ORAL_MEMORY_FIELD_WEIGHTS": "[]"}):
            with self.assertRaises(MemoryError):
                settings_from_env(env)
        for top_k in (0, True, 100):
            with self.assertRaises(MemoryError):
                self.store.retrieve(query(top_k=top_k))
        with self.assertRaises(MemoryError):
            call(self.store, "put_doctor", {})
        reply = dispatch(self.store, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": "retrieve", "arguments": {}}})
        self.assertTrue(reply["result"]["isError"])

    def test_dsl_contains_appearance_and_pathology_as_distinct_fields(self):
        value = render_lesion(lesion())
        self.assertTrue(value.startswith("OLK/1;"))
        self.assertIn('site="颊黏膜"', value)
        self.assertIn('pathology={', value)

    def test_stdio_entrypoint_round_trip_and_restart(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "put_lesion", "arguments": {"record": lesion(), "expected_version": 0}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "retrieve", "arguments": query()}},
        ]
        env = {**os.environ, "ORAL_MEMORY_DB": str(self.path), "ORAL_MEMORY_PATIENT_ID": "p1",
               "ORAL_MEMORY_ACTOR_ROLE": "patient"}
        run = subprocess.run([sys.executable, str(ROOT / "server.py")],
                             input="\n".join(json.dumps(message, ensure_ascii=False) for message in messages) + "\n",
                             text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        responses = [json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual([reply["id"] for reply in responses], [1, 2, 3, 4])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("优先级1", responses[0]["result"]["instructions"])
        self.assertEqual(responses[3]["result"]["structuredContent"]["candidates"][0]["lesion"]["record_id"], "l1")
        restart = subprocess.run([sys.executable, str(ROOT / "server.py")], input=json.dumps({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "get", "arguments": {
                "category": "lesion", "record_id": "l1", "include_versions": True}}}) + "\n",
            text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual(json.loads(restart.stdout)["result"]["structuredContent"]["record"]["version"], 1)


if __name__ == "__main__":
    unittest.main()
