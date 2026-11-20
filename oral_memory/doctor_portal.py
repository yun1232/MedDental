"""Loopback doctor form and token-bound JSON input for the same patient memory database."""

import argparse
import json
import os
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from schemas import MemoryError
from store import MemoryStore, settings_from_env
from workflow import PatientCommunicationAgent

MAX_BODY = 512_000


class DoctorApplication:
    """Serve static public form assets; authenticate every patient-data operation."""

    def __init__(self, database, patient_id, token, settings=None):
        if not token or len(token) < 16:
            raise MemoryError("invalid_config", "ORAL_DOCTOR_TOKEN requires at least 16 characters")
        if not patient_id:
            raise MemoryError("invalid_config", "ORAL_MEMORY_PATIENT_ID is required")
        self.database, self.patient_id, self.token = database, patient_id, token
        self.settings = settings if settings is not None else settings_from_env({})

    def handle(self, method, path, headers, raw=b""):
        """Return HTTP status, media type and bytes without leaking internal exceptions."""
        parts = urlsplit(path)
        assets = {"/": ("doctor.html", "text/html"), "/doctor.js": ("doctor.js", "text/javascript"),
                  "/doctor.css": ("doctor.css", "text/css")}
        if method == "GET" and parts.path in assets:
            filename, media_type = assets[parts.path]
            return 200, media_type + "; charset=utf-8", (Path(__file__).parent / "web" / filename).read_bytes()
        authorization = headers.get("Authorization", "")
        if not secrets.compare_digest(authorization, "Bearer " + self.token):
            return 401, "application/json", b'{"error":"unauthorized"}'
        origin = headers.get("Origin")
        if origin and urlsplit(origin).netloc != headers.get("Host"):
            return 403, "application/json", b'{"error":"origin_refused"}'
        if len(raw) > MAX_BODY:
            return 413, "application/json", b'{"error":"request_too_large"}'
        store = None
        try:
            store = MemoryStore(self.database, self.patient_id, "doctor", self.settings)
            agent = PatientCommunicationAgent(store)
            if method == "GET" and parts.path == "/api/state":
                parameters = parse_qs(parts.query)
                encounter = parameters.get("encounter_id", [""])[0]
                result = {"patient_id": self.patient_id, **agent.case_state({"encounter_id": encounter}),
                          "memories": {category: store.records(category) for category in ("lesion", "experience", "care", "doctor")}}
                result["recent_retrievals"] = [{"retrieval_id": row["retrieval_id"], "refs": json.loads(row["refs"])}
                    for row in store.db.execute("SELECT retrieval_id,refs FROM delivery_packets WHERE patient_id=? ORDER BY rowid DESC LIMIT 100", (self.patient_id,))]
            elif method == "POST" and parts.path in ("/api/submit", "/api/feedback", "/api/frequency"):
                if not headers.get("Content-Type", "").startswith("application/json"):
                    raise MemoryError("invalid_input", "Content-Type must be application/json")
                arguments = json.loads(raw)
                operation = {"/api/submit": agent.doctor_submit, "/api/feedback": store.memory_feedback,
                             "/api/frequency": store.set_frequency}[parts.path]
                result = operation(arguments)
            else:
                return 404, "application/json", b'{"error":"not_found"}'
            return 200, "application/json; charset=utf-8", json.dumps(result, ensure_ascii=False).encode()
        except MemoryError as error:
            status = 409 if error.code in ("version_conflict", "event_conflict") else 400
            return status, "application/json", json.dumps({"error": error.code, "message": str(error)}, ensure_ascii=False).encode()
        except (ValueError, TypeError):
            return 400, "application/json", b'{"error":"invalid_json"}'
        except (sqlite3.Error, OSError):
            return 500, "application/json", b'{"error":"storage_failure"}'
        finally:
            if store is not None:
                store.close()


def make_handler(application):
    """Bind a request handler to one application; avoid global patient selections."""
    class Handler(BaseHTTPRequestHandler):
        def _run(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if not 0 <= length <= MAX_BODY:
                self.send_error(413)
                return
            status, media, body = application.handle(self.command, self.path, self.headers,
                                                     self.rfile.read(length) if length else b"")
            self.send_response(status)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        do_GET = _run
        do_POST = _run

        def log_message(self, format, *arguments):
            # Request URLs may contain encounter identifiers; do not log them.
            return
    return Handler


def main():
    """Launch a local doctor input service; no database or token is generated silently."""
    parser = argparse.ArgumentParser(description="Local doctor input for oral leukoplakia memory")
    parser.add_argument("--port", type=int, default=int(os.environ.get("ORAL_DOCTOR_PORT", "8765")))
    options = parser.parse_args()
    application = DoctorApplication(os.environ.get("ORAL_MEMORY_DB", ""),
        os.environ.get("ORAL_MEMORY_PATIENT_ID", ""), os.environ.get("ORAL_DOCTOR_TOKEN", ""), settings_from_env(os.environ))
    if not application.database:
        raise MemoryError("invalid_config", "ORAL_MEMORY_DB is required")
    server = HTTPServer(("127.0.0.1", options.port), make_handler(application))
    print(f"Doctor input: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
