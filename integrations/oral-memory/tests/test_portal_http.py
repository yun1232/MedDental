"""Actual loopback HTTP requests through the doctor's local entry point."""

import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from doctor_portal import DoctorApplication, make_handler
from test_workflow import doctor_input


class DoctorHTTPTests(unittest.TestCase):
    def test_real_form_assets_and_confirmed_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            application = DoctorApplication(Path(directory) / 'memory.sqlite3', 'synthetic-http', 'synthetic-http-token')
            server = HTTPServer(('127.0.0.1', 0), make_handler(application))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                connection.request('GET', '/')
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn('医生输入工作台', response.read().decode())
                connection.request('GET', '/api/state?encounter_id=v1')
                response = connection.getresponse()
                self.assertEqual(response.status, 401)
                response.read()
                headers = {'Authorization': 'Bearer synthetic-http-token', 'Content-Type': 'application/json'}
                connection.request('POST', '/api/submit', body=json.dumps(doctor_input('http1', 0, target='prior_treatment')), headers=headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 200, response.read().decode() if response.status != 200 else '')
                response.read()
                connection.request('GET', '/api/state?encounter_id=v1', headers=headers)
                response = connection.getresponse()
                result = json.loads(response.read())
                self.assertEqual(result['revision'], 1)
                self.assertEqual(result['next_action']['priority'], 1)
                self.assertEqual(result['patient_id'], 'synthetic-http')
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
