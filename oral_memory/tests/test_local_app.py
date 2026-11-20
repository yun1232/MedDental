"""Exercise the replacement launcher, shared HTTP workflow and model extraction failures."""

import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from doctor_portal import make_handler
from local_app import LocalApplication
from schemas import MemoryError
from test_workflow import doctor_input


class FakeModel:
    enabled = True

    def __init__(self, extraction=None, failure=None):
        self.extraction = extraction or {"intent": "provide_information", "observations": [], "phenotype": {"site": "左颊黏膜", "color": "白色"}}
        self.failure = failure
        self.calls = 0

    def complete(self, messages, json_output=False):
        self.calls += 1
        if self.failure:
            raise self.failure
        return json.dumps(self.extraction) if json_output else "这些描述可作为医生复核的参考。"


class LocalAppTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "memory.sqlite3"
        self.model = FakeModel()
        self.application = LocalApplication(self.database, "synthetic-local", "synthetic-doctor-token", self.model)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.application))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.directory.cleanup()

    def request(self, method, path, body=None, doctor=False, extra=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Content-Type": "application/json", **(extra or {})}
        if doctor:
            headers["Authorization"] = "Bearer synthetic-doctor-token"
        try:
            connection.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if "application/json" in response.getheader("Content-Type", "") else raw.decode()
        finally:
            connection.close()

    def turn(self, revision=0, event="turn1", mode="guided", field="lesion_location", message="左颊黏膜"):
        return {"encounter_id": "v1", "event_id": event, "expected_revision": revision,
                "mode": mode, "field": field, "message": message, "status": "known"}

    def test_patient_and_doctor_share_one_port_and_history_survives_restart(self):
        self.assertIn("患者交流", self.request("GET", "/")[1])
        self.assertIn("医生输入工作台", self.request("GET", "/doctor")[1])
        self.assertEqual(self.request("GET", "/api/state?encounter_id=v1")[0], 401)
        self.assertEqual(self.request("POST", "/api/patient/turn", self.turn())[0], 200)
        note = doctor_input("doctor-local", 1, target="prior_treatment")
        self.assertEqual(self.request("POST", "/api/submit", note, doctor=True)[0], 200)
        state = self.request("GET", "/api/patient/state?encounter_id=v1")[1]
        self.assertEqual(state["next_action"]["priority"], 1)
        answer = self.turn(2, "answer", field="prior_treatment", message="做过治疗")
        self.assertEqual(self.request("POST", "/api/patient/turn", answer)[0], 200)
        restarted = LocalApplication(self.database, "synthetic-local", "synthetic-doctor-token", self.model)
        store = restarted.open_store()
        try:
            state = restarted._state(store, "v1")
            self.assertEqual(state["revision"], 3)
            self.assertEqual(len(state["history"]), 2)
            self.assertEqual(state["open_doctor_questions"], [])
            self.assertEqual(store.records("doctor")[0]["question_status"], "open")
        finally:
            store.close()

    def test_chat_extracts_descriptor_and_retry_never_repeats_model_or_exposure(self):
        arguments = self.turn(mode="chat")
        first = self.request("POST", "/api/patient/turn", arguments)
        self.assertEqual(first[0], 200, first)
        self.assertEqual(first, self.request("POST", "/api/patient/turn", arguments))
        self.assertEqual(self.model.calls, 1)
        records = self.request("GET", "/api/state?encounter_id=v1", doctor=True)[1]["memories"]["lesion"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["features"]["color"], "白色")
        self.assertEqual(records[0]["pathology"]["status"], "unknown")
        changed = {**arguments, "message": "其他描述"}
        self.assertEqual(self.request("POST", "/api/patient/turn", changed)[0], 409)

    def test_model_failure_or_invalid_extraction_does_not_write(self):
        for failure, extraction in [(MemoryError("model_unavailable", "模型暂不可用"), None),
                                    (None, {"intent": "provide_information", "observations": [{"field": "invented", "status": "known", "value": "x"}], "phenotype": {}})]:
            self.model.failure, self.model.extraction = failure, extraction
            response = self.request("POST", "/api/patient/turn", self.turn(mode="chat"))
            self.assertEqual(response[0], 400, response)
            state = self.request("GET", "/api/patient/state?encounter_id=v1")[1]
            self.assertEqual(state["revision"], 0)
            self.assertEqual(state["history"], [])

    def test_patient_cannot_call_doctor_tools_or_change_host_identity(self):
        result = self.request("POST", "/api/patient/tool", {"name": "doctor_submit", "arguments": {}})
        self.assertEqual(result[0], 400)
        self.assertEqual(result[1]["error"], "unknown_tool")
        self.assertEqual(self.request("GET", "/api/patient/state", extra={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/patient/turn", self.turn(), extra={"Origin": "https://attacker.example"})[0], 403)

    def test_stale_revision_preserves_doctor_update(self):
        self.assertEqual(self.request("POST", "/api/submit", doctor_input("doctor", 0), doctor=True)[0], 200)
        result = self.request("POST", "/api/patient/turn", self.turn(mode="chat"))
        self.assertEqual(result[0], 409)
        self.assertEqual(self.model.calls, 0)

    def test_patient_can_clarify_a_conflict_with_an_existing_alternative(self):
        self.assertEqual(self.request("POST", "/api/patient/turn", self.turn())[0], 200)
        self.assertEqual(self.request("POST", "/api/patient/turn", self.turn(1, "change", message="右颊黏膜"))[0], 200)
        state = self.request("GET", "/api/patient/state?encounter_id=v1")[1]
        self.assertEqual(state["state"]["fields"]["lesion_location"]["status"], "conflict")
        self.assertEqual(self.request("POST", "/api/patient/turn", self.turn(2, "clarify", message="右颊黏膜"))[0], 200)
        state = self.request("GET", "/api/patient/state?encounter_id=v1")[1]
        self.assertEqual(state["state"]["fields"]["lesion_location"]["status"], "known")
        self.assertEqual(state["state"]["fields"]["lesion_location"]["value"], "右颊黏膜")

    def test_launcher_defaults_to_localhost_8080_without_node_or_model(self):
        env = {**os.environ, "ORAL_MEMORY_DB": str(Path(self.directory.name) / "launcher.sqlite3"),
               "ORAL_DOCTOR_TOKEN": "synthetic-launcher-token", "DEEPSEEK_API_KEY": ""}
        process = subprocess.Popen([sys.executable, str(ROOT.parent / "run.py")], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            first = process.stdout.readline()
            self.assertIn("http://localhost:8080", first)
            connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=5)
            try:
                connection.request("GET", "/api/patient/state")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertFalse(json.loads(response.read())["model_enabled"])
            finally:
                connection.close()
        finally:
            process.terminate()
            process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
