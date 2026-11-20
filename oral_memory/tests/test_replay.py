"""Replay committed tool-call inputs and compare model-visible policy and questions."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReplayTests(unittest.TestCase):
    def test_committed_stdio_workflow_and_policy_snapshot(self):
        scenario = json.loads((ROOT / "tests/fixtures/workflow-replay.json").read_text())
        messages = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}]
        messages += [{"jsonrpc": "2.0", "id": index+1, "method": "tools/call", "params": item}
                     for index, item in enumerate(scenario["messages"])]
        with tempfile.TemporaryDirectory() as directory:
            run = subprocess.run([sys.executable, str(ROOT / "server.py")],
                input="\n".join(json.dumps(item, ensure_ascii=False) for item in messages)+"\n", text=True,
                capture_output=True, timeout=10, env={**os.environ, "ORAL_MEMORY_DB": str(Path(directory)/"memory.sqlite3"),
                    "ORAL_MEMORY_PATIENT_ID": "synthetic-replay", "ORAL_MEMORY_ACTOR_ROLE": "patient"})
            self.assertEqual(run.returncode, 0, run.stderr)
            replies = [json.loads(line) for line in run.stdout.splitlines()]
            self.assertEqual(replies[0]["result"]["instructions"], (ROOT/"tests/fixtures/memory-policy.expected.txt").read_text())
            actual = []
            for reply in replies[1:]:
                self.assertFalse(reply["result"]["isError"], reply)
                result = reply["result"]["structuredContent"]
                action = result["next_action"]
                actual.append({"revision": result["revision"], "action": action["action"],
                               "field": action.get("field"), "question": action.get("question")})
            self.assertEqual(actual, scenario["expected"])


if __name__ == "__main__":
    unittest.main()
