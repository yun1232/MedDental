"""Versioned consultations and doctor-published cross-patient case experience.

No model calls or training. Patient identity/role come only from the host. Images
are private by default. Publishing requires an explicit doctor-reviewed redacted
case; raw questions/history are never copied into the shared library.
"""
import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError
from schemas import MemoryError, validate, obj, text, strings
from team_schemas import OUTPUTS, check_output, REGION, array
from workflow import PatientCommunicationAgent

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 20_000_000


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


def fail(code, message):
    raise MemoryError(code, message)


def terms(value):
    """Deterministic multilingual baseline, replaceable by a frozen embedding index."""
    s = " ".join(value) if isinstance(value, list) else str(value)
    words = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", s.lower())
    return {piece for word in words for piece in
            ([word[i:i + 2] for i in range(len(word) - 1)] if re.fullmatch(r"[\u3400-\u9fff]+", word) and len(word) > 1 else [word])}


def similarity(a, b):
    left, right = terms(a), terms(b)
    return len(left & right) / math.sqrt(len(left) * len(right)) if left and right else 0.0


class MedicalProject:
    def __init__(self, store, shared_path=None, asset_root=None, profile_path=None, domain=None):
        self.store, self.db = store, store.db
        self.patient, self.role = store.patient_id, store.role
        self.inquiry = PatientCommunicationAgent(store)
        private_path = Path(store.db.execute("PRAGMA database_list").fetchone()[2])
        self.shared_path = Path(shared_path or os.environ.get("MEDICAL_SHARED_DB") or private_path.with_name("shared-cases.sqlite3")).resolve()
        self.assets = Path(asset_root or os.environ.get("MEDICAL_ASSET_ROOT") or private_path.with_name("medical-images")).resolve()
        if self.shared_path == private_path.resolve():
            fail("invalid_config", "Shared and private databases must be separate")
        self.domain = domain or os.environ.get("MEDICAL_DOMAIN", "oral-leukoplakia")
        profiles = json.loads(Path(profile_path or os.environ.get("MEDICAL_PROFILES") or Path(__file__).with_name("profiles") / "diseases.json").read_text())
        if self.domain not in profiles:
            fail("unknown_domain", "Select a configured disease profile")
        self.profile = profiles[self.domain]
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS medical_cases (
            patient TEXT NOT NULL, encounter TEXT NOT NULL, revision INTEGER NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(patient, encounter));
          CREATE TABLE IF NOT EXISTS medical_runs (
            id TEXT PRIMARY KEY, patient TEXT NOT NULL, encounter TEXT NOT NULL,
            revision INTEGER NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS medical_runs_patient ON medical_runs(patient,encounter);
          CREATE TABLE IF NOT EXISTS medical_session_bindings (
            patient TEXT NOT NULL, session TEXT NOT NULL, encounter TEXT NOT NULL,
            PRIMARY KEY(patient,session));
          CREATE TABLE IF NOT EXISTS medical_events (
            patient TEXT NOT NULL, event TEXT NOT NULL, hash TEXT NOT NULL,
            response TEXT NOT NULL, PRIMARY KEY(patient,event));
        ''')
        self.shared_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.shared = sqlite3.connect(self.shared_path, timeout=5, isolation_level=None)
        self.shared.row_factory = sqlite3.Row
        application = self.shared.execute("PRAGMA application_id").fetchone()[0]
        version = self.shared.execute("PRAGMA user_version").fetchone()[0]
        tables = self.shared.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        if (application, version) not in ((0, 0), (0x4D444331, 1)) or (application == 0 and tables):
            self.shared.close()
            fail("wrong_database", "Shared case library must be a new file or a MedDental case library")
        self.shared.execute("PRAGMA journal_mode=WAL")
        self.shared.execute("PRAGMA synchronous=FULL")
        self.shared.executescript('''
          BEGIN IMMEDIATE;
          CREATE TABLE IF NOT EXISTS cases (
            id TEXT PRIMARY KEY, version INTEGER NOT NULL, domain TEXT NOT NULL,
            payload TEXT NOT NULL, helpful INTEGER NOT NULL DEFAULT 0,
            unhelpful INTEGER NOT NULL DEFAULT 0, exposures INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS case_versions (
            id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(id,version));
          CREATE TABLE IF NOT EXISTS deliveries (
            run TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL,
            feedback TEXT, PRIMARY KEY(run,id,version));
          PRAGMA application_id=1296319281;
          PRAGMA user_version=1;
          COMMIT;
        ''')
        self.shared_path.chmod(0o600)

    def close(self):
        self.shared.close()

    def doctor(self):
        if self.role != "doctor":
            fail("forbidden", "Doctor host required")

    def state(self, encounter):
        validate(encounter, text())
        row = self.db.execute("SELECT revision,payload FROM medical_cases WHERE patient=? AND encounter=?", (self.patient, encounter)).fetchone()
        state = json.loads(row["payload"]) if row else {"question": "", "messages": [], "images": [], "domain": self.domain}
        if state["domain"] != self.domain:
            fail("domain_conflict", "An encounter cannot change disease profile")
        state["revision"] = row["revision"] if row else 0
        state["encounter_id"] = encounter
        state["profile"] = self.profile
        state["legacy_revision"] = self._legacy_revision(encounter)
        return state

    def _legacy_revision(self, encounter):
        row = self.db.execute("SELECT revision FROM inquiry_cases WHERE patient_id=? AND encounter_id=?", (self.patient, encounter)).fetchone()
        return row["revision"] if row else 0

    def run(self, run_id, current=True):
        row = self.db.execute("SELECT * FROM medical_runs WHERE id=? AND patient=?", (run_id, self.patient)).fetchone()
        if not row:
            fail("not_found", "Consultation not found for this patient")
        run = json.loads(row["payload"])
        if current and (self.state(row["encounter"])["revision"] != row["revision"] or self._legacy_revision(row["encounter"]) != run["legacy_revision"]):
            fail("stale_revision", "Patient or doctor information changed; start a fresh consultation")
        return run

    def _write_run(self, run):
        self.db.execute("UPDATE medical_runs SET payload=?,status=? WHERE id=? AND patient=?", (packed(run), run["status"], run["run_id"], self.patient))

    def _private_dir(self):
        return self.assets / "private" / hashlib.sha256(self.patient.encode()).hexdigest()

    def _save_image(self, encoded, directory, image_id=None):
        try:
            data = base64.b64decode(encoded, validate=True)
            if not 0 < len(data) <= MAX_IMAGE_BYTES:
                fail("image_limit", "An image must be at most 8 MiB")
            with Image.open(io.BytesIO(data)) as opened:
                if opened.width * opened.height > Image.MAX_IMAGE_PIXELS or getattr(opened, "n_frames", 1) != 1:
                    fail("image_limit", "Use a single-frame image of at most 20 megapixels")
                im = ImageOps.exif_transpose(opened).convert("RGB")
                buf = io.BytesIO()
                im.save(buf, format="PNG")
                normalized = buf.getvalue()
                if len(normalized) > MAX_IMAGE_BYTES:
                    fail("image_limit", "Normalized image exceeds 8 MiB; reduce resolution")
        except (ValueError, UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
            if isinstance(exc, MemoryError):
                raise
            fail("invalid_image", "Invalid or oversized image")
        image_id = image_id or "img-" + hashlib.sha256(normalized).hexdigest()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (image_id + ".png")
        if not path.exists():
            # Content is immutable; the atomic replace prevents readers seeing partial writes.
            temp = directory / ("tmp-" + uuid.uuid4().hex)
            temp.write_bytes(normalized)
            temp.chmod(0o600)
            temp.replace(path)
        return {"image_id": image_id, "media_type": "image/png", "width": im.width, "height": im.height,
                "sha256": hashlib.sha256(normalized).hexdigest(), "bytes": len(normalized)}

    def ingest(self, encounter_id, event_id, expected_revision, messages, images, source_session=None):
        validate({"encounter_id": encounter_id, "event_id": event_id, "expected_revision": expected_revision,
                  "messages": messages, "images": images}, obj({"encounter_id": text(), "event_id": text(),
                  "expected_revision": {"type": "integer", "minimum": 0},
                  "messages": array(obj({"id": text(), "text": text(empty=True)}, ("id", "text")), 30),
                  "images": array(obj({"data": {"type": "string", "maxLength": 12 * 1024 * 1024}, "source_message_ids": strings()}, ("data",)), 3)},
                  ("encounter_id", "event_id", "expected_revision", "messages", "images")))
        if sum(len(x["data"]) for x in images) > MAX_TOTAL_BYTES * 4 // 3:
            fail("image_limit", "Image batch exceeds 24 MiB")
        if source_session is not None:
            validate(source_session, text())
        request_hash = digest({"encounter": encounter_id, "messages": messages, "images": images, "session": source_session})
        previous = self.db.execute("SELECT hash,response FROM medical_events WHERE patient=? AND event=?", (self.patient, event_id)).fetchone()
        if previous:
            if previous["hash"] != request_hash:
                fail("event_conflict", "An input event cannot change")
            previous_run = json.loads(previous["response"])
            self.run(previous_run["run_id"])  # a retry after a correction must not resurrect stale work
            return previous_run
        saved_images = [{**self._save_image(x["data"], self._private_dir()), "source_message_ids": x.get("source_message_ids", [])} for x in images]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            state = self.state(encounter_id)
            if source_session:
                binding = self.db.execute("SELECT encounter FROM medical_session_bindings WHERE patient=? AND session=?", (self.patient, source_session)).fetchone()
                if binding and binding["encounter"] != encounter_id:
                    fail("session_encounter_conflict", "Start a new Harness session for another encounter")
                self.db.execute("INSERT OR IGNORE INTO medical_session_bindings VALUES(?,?,?)", (self.patient, source_session, encounter_id))
            if expected_revision != state["revision"]:
                fail("version_conflict", "Re-read the current patient revision")
            merged = {x["id"]: x for x in state["messages"]}
            for message in messages:
                if message["id"] in merged and merged[message["id"]] != message:
                    fail("event_conflict", "An original message cannot be rewritten")
                merged[message["id"]] = message
            history = list(merged.values())
            if len(history) > 30 or sum(len(x["text"]) for x in history) > 48000:
                fail("context_limit", "Start another encounter; original question context cannot be silently truncated")
            originals = {x["image_id"]: x for x in state["images"]}
            for image in saved_images:
                origins = set(originals.get(image["image_id"], {}).get("source_message_ids", [])) | set(image["source_message_ids"])
                if any(source not in merged for source in origins):
                    fail("invalid_image_source", "Image source must be an original message in this encounter")
                originals[image["image_id"]] = {**image, "source_message_ids": sorted(origins)}
            if len(originals) > 3:
                fail("image_limit", "At most three original images per encounter")
            if not any(x["text"].strip() for x in history):
                fail("missing_question", "An original patient question is required")
            if not originals:
                fail("missing_image", "Upload an original patient image before visual consultation")
            revision = state["revision"] + 1
            state.update({"question": state["question"] or next(x["text"] for x in history if x["text"].strip()),
                          "messages": history, "images": list(originals.values()), "revision": revision})
            self.db.execute("INSERT OR REPLACE INTO medical_cases VALUES(?,?,?,?)", (self.patient, encounter_id, revision, packed(state)))
            run = {"run_id": "run-" + uuid.uuid4().hex, "encounter_id": encounter_id,
                   "revision": revision, "legacy_revision": state["legacy_revision"], "domain": self.domain, "status": "running", "outputs": [], "retrievals": [], "views": [], "doctor_review": None, "input": dict(state)}
            self.db.execute("INSERT INTO medical_runs VALUES(?,?,?,?,?,?)", (run["run_id"], self.patient, encounter_id, revision, run["status"], packed(run)))
            response = {"run_id": run["run_id"], "revision": revision}
            self.db.execute("INSERT INTO medical_events VALUES(?,?,?,?)", (self.patient, event_id, request_hash, packed(response)))
            self.db.commit()
            return response
        except BaseException:
            self.db.rollback()
            raise

    def context(self, run_id):
        run = self.run(run_id)
        return {"case": self.state(run["encounter_id"]), "run": run,
                "longitudinal": self.inquiry.case_state({"encounter_id": run["encounter_id"]}),
                "doctor_notes": [r for r in self.store.records("doctor") if r["encounter_id"] == run["encounter_id"]][-10:],
                "prior_reviews": [{"encounter_id": r["encounter"], "input_revision": r["revision"],
                                   "doctor_review": json.loads(r["payload"])["doctor_review"]}
                                  for r in self.db.execute("SELECT encounter,revision,payload FROM medical_runs WHERE patient=? AND status='doctor_reviewed' ORDER BY rowid DESC LIMIT 10", (self.patient,))]}

    def save_output(self, run_id, role, value):
        if role not in OUTPUTS:
            fail("invalid_role", "Unknown team role")
        check_output(role, value)
        if len(packed(value)) > 48000:
            fail("context_limit", "Agent output too large")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            run = self.run(run_id)
            if run["status"] != "running" or len(run["outputs"]) >= 24:
                fail("run_closed", "Consultation ended or step budget exhausted")
            self._check_evidence(run, role, value)
            run["outputs"].append({"role": role, "step": len(run["outputs"]) + 1, "value": value})
            self._write_run(run)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return run

    def _check_evidence(self, run, role, value):
        state = self.state(run["encounter_id"])
        originals = {x["image_id"] for x in state["images"]}
        views = {x["view_id"]: x for x in run["views"]}
        delivered = {(x["case_id"], x["version"]) for r in run["retrievals"] for x in r["cases"]}
        if role == "visual":
            for region in value["regions"]:
                if region["image_id"] not in originals:
                    fail("invalid_evidence", "Regions must be located on a current original image")
            ids = [f["id"] for f in value["findings"]]
            if len(set(ids)) != len(ids):
                fail("invalid_evidence", "Visual finding ids must be distinct")
            for finding in value["findings"]:
                if finding["image_id"] not in originals or not finding["supporting_views"]:
                    fail("invalid_evidence", "A finding requires a current image and supporting view")
                for view_id in finding["supporting_views"]:
                    if view_id != finding["image_id"] and (view_id not in views or views[view_id]["image_id"] != finding["image_id"]):
                        fail("invalid_evidence", "View is unrelated to the finding image")
                if finding["certainty"] == "clear" and not any(v == finding["image_id"] or views.get(v, {}).get("kind") == "crop" for v in finding["supporting_views"]):
                    fail("invalid_evidence", "A clear finding requires original/crop support; mask alone is insufficient")
        if role in ("retrieval", "reasoning"):
            refs = value["ranking"] if role == "retrieval" else value["case_refs"]
            pairs = [(x["case_id"], x["version"]) for x in refs]
            if len(set(pairs)) != len(pairs) or any(pair not in delivered for pair in pairs):
                fail("invalid_reference", "Only distinct delivered case versions may be cited")
        if role == "reasoning":
            known = {"patient"} | {x["id"] for x in (self.latest(run, "visual") or {"findings": []})["findings"]}
            for hyp in value["hypotheses"]:
                if any(x not in known for x in hyp["support_ids"] + hyp["against_ids"]):
                    fail("invalid_evidence", "Hypothesis refers to an unknown observation")

    def latest(self, run, role):
        return next((x["value"] for x in reversed(run["outputs"]) if x["role"] == role), None)

    def retrieve(self, run_id, key, query=None):
        if key not in ("key1", "key2"):
            fail("invalid_key", "Use key1 or key2")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            run = self.run(run_id)
            if run["status"] != "running" or len(run["retrievals"]) >= 6:
                fail("retrieval_limit", "Retrieval budget exhausted or run closed")
            patient = self.latest(run, "patient")
            if not patient:
                fail("missing_intake", "Patient assessment must precede retrieval")
            phenotype = list(patient["phenotype"])
            if key == "key2":
                visual = self.latest(run, "visual")
                if not visual:
                    fail("missing_visual", "Key2 requires visual observations")
                views = {x["view_id"]: x for x in run["views"]}
                # Occlusion changes never become standalone positive visual evidence.
                for f in visual["findings"]:
                    if f["certainty"] == "clear" and any(v == f["image_id"] or views.get(v, {}).get("kind") == "crop" for v in f["supporting_views"]):
                        phenotype.extend([f["description"], *f["tags"]])
            if query is not None:
                validate(query, text())
                # Extra search text only broadens candidate recall; it cannot validate an observation.
            scan = self.shared.execute("SELECT * FROM cases WHERE domain=? ORDER BY id LIMIT 10001", (self.domain,)).fetchall()
            if len(scan) > 10000:
                fail("index_required", "Shared library exceeds baseline index capacity; install an indexed retriever")
            ranked = []
            for row in scan:
                case = json.loads(row["payload"])
                p = similarity(phenotype, case["phenotype"])
                extra = similarity(query or "", case["phenotype"])
                if max(p, extra) <= 0:
                    continue
                h = similarity(patient["history"], case["context"])
                utility = (row["helpful"] + 1) / (row["helpful"] + row["unhelpful"] + 2)
                frequency = "low_frequency" if row["unhelpful"] >= 3 and utility < .3 else "normal"
                ranked.append({**case, "phenotype_score": round(p, 6), "history_score": round(h, 6),
                               "search_score": round(extra, 6), "utility": utility, "frequency": frequency,
                               "exposures": row["exposures"]})
            # Clinical/visual relevance dominates. Utility breaks comparable relevance ties.
            ranked.sort(key=lambda c: (int(max(c["phenotype_score"], c["search_score"]) * 10),
                                      c["history_score"], c["utility"] * (.2 if c["frequency"] == "low_frequency" else 1),
                                      c["phenotype_score"], c["case_id"]), reverse=True)
            cases = ranked[:10]
            # Deterministic rotating exploration from otherwise low-frequency matching cases.
            low = [c for c in ranked[10:] if c["frequency"] == "low_frequency"]
            if low:
                cases.append(low[int(digest(run_id + key)[:8], 16) % len(low)])
            self.shared.execute("BEGIN IMMEDIATE")
            try:
                for c in cases:
                    cursor = self.shared.execute("INSERT OR IGNORE INTO deliveries(run,id,version) VALUES(?,?,?)", (run_id, c["case_id"], c["version"]))
                    if cursor.rowcount:
                        self.shared.execute("UPDATE cases SET exposures=exposures+1 WHERE id=?", (c["case_id"],))
                self.shared.commit()
            except BaseException:
                self.shared.rollback()
                raise
            result = {"retrieval_id": uuid.uuid4().hex, "key": key, "query_terms": phenotype,
                      "additional_query": query or "", "cases": cases, "searched_entire_domain": True,
                      "scoring": "phenotype band > history relevance > utility/frequency tie-break; model ranking is separate"}
            run["retrievals"].append(result)
            self._write_run(run)
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def make_views(self, run_id):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            run = self.run(run_id)
            visual = self.latest(run, "visual")
            if not visual or run["status"] != "running":
                fail("missing_visual", "Current visual proposals required")
            state = self.state(run["encounter_id"])
            metadata = {x["image_id"]: x for x in state["images"]}
            result = []
            for region in visual["regions"][:2]:
                meta = metadata[region["image_id"]]
                with Image.open(self._private_dir() / (meta["image_id"] + ".png")) as im:
                    w, h = im.size
                    b = region["box"]
                    pixels = (int(b[0] * w / 1000), int(b[1] * h / 1000), math.ceil(b[2] * w / 1000), math.ceil(b[3] * h / 1000))
                    if pixels[2] <= pixels[0] or pixels[3] <= pixels[1]:
                        fail("invalid_region", "Region has no pixels")
                    # Each masked copy hides ONE candidate region; the original remains available.
                    for kind in ("crop", "mask"):
                        changed = im.crop(pixels) if kind == "crop" else im.copy()
                        if kind == "mask":
                            ImageDraw.Draw(changed).rectangle((pixels[0], pixels[1], pixels[2] - 1, pixels[3] - 1), fill=(127, 127, 127))
                        buf = io.BytesIO()
                        changed.save(buf, format="PNG")
                        asset = self._save_image(base64.b64encode(buf.getvalue()).decode(), self._private_dir())
                        view_id = "view-" + digest({"run": run_id, "region": region, "kind": kind})[:24]
                        view = {"view_id": view_id, "image_id": meta["image_id"], "asset_id": asset["image_id"],
                                "kind": kind, "box": b, "pixel_box": pixels, "description": region["description"],
                                "basis": region["basis"], "asset": asset}
                        if not any(x["view_id"] == view_id for x in run["views"]):
                            if len(run["views"]) >= 12:
                                fail("view_limit", "Visual view budget exhausted")
                            run["views"].append(view)
                        result.append(view)
            self._write_run(run)
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def image(self, run_id, image_id):
        run = self.run(run_id, current=False)
        state = self.state(run["encounter_id"])
        private = {x["image_id"] for x in run["input"]["images"]} | {v["asset_id"] for v in run["views"]}
        if image_id in private:
            path = self._private_dir() / (image_id + ".png")
        else:
            delivered = {x["image_id"] for r in run["retrievals"] for c in r["cases"] for x in c["images"]}
            if image_id not in delivered:
                fail("forbidden_image", "Image is not part of this patient's consultation")
            path = self.assets / "shared" / (image_id + ".png")
        return path.read_bytes()

    def finish(self, run_id, status):
        if status not in ("awaiting_doctor", "needs_input", "needs_human", "failed"):
            fail("invalid_status", "Invalid consultation status")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            run = self.run(run_id)
            if run["status"] != "running":
                if run["status"] != status:
                    fail("run_closed", "Consultation already ended")
                self.db.commit()
                return run
            if status == "awaiting_doctor":
                if not all(self.latest(run, r) is not None for r in OUTPUTS) or self.latest(run, "review")["route"] != "approve":
                    fail("incomplete_review", "All five roles and an approving audit are required")
            run["status"] = status
            self._write_run(run)
            self.db.commit()
            return run
        except BaseException:
            self.db.rollback()
            raise

    def list_runs(self, encounter_id):
        return [json.loads(r["payload"]) for r in self.db.execute("SELECT payload FROM medical_runs WHERE patient=? AND encounter=? ORDER BY rowid DESC LIMIT 10", (self.patient, encounter_id))]

    def review(self, run_id, expected_revision, confirmed, note, outcome):
        self.doctor()
        validate(expected_revision, {"type": "integer", "minimum": 1})
        validate(note, text())
        validate(outcome, text())
        if confirmed is not True:
            fail("confirmation_required", "Explicit doctor confirmation required")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            run = self.run(run_id)
            if run["revision"] != expected_revision or run["status"] not in ("awaiting_doctor", "needs_input", "needs_human"):
                fail("version_conflict", "Review a completed current consultation")
            run["doctor_review"] = {"note": note, "outcome": outcome, "time": int(time.time()), "revision": expected_revision}
            run["status"] = "doctor_reviewed"
            self._write_run(run)
            self.db.commit()
            return run
        except BaseException:
            self.db.rollback()
            raise

    def correct(self, encounter_id, expected_revision, event_id, note):
        self.doctor()
        validate(expected_revision, {"type": "integer", "minimum": 1})
        validate(note, text())
        validate(event_id, text())
        self.db.execute("BEGIN IMMEDIATE")
        try:
            state = self.state(encounter_id)
            if state["revision"] != expected_revision:
                fail("version_conflict", "Re-read the patient revision")
            if len(state["messages"]) >= 30:
                fail("context_limit", "Start another encounter")
            state["messages"].append({"id": "doctor:" + event_id, "text": "医生纠正：" + note})
            state["revision"] += 1
            self.db.execute("UPDATE medical_cases SET revision=?,payload=? WHERE patient=? AND encounter=?", (state["revision"], packed(state), self.patient, encounter_id))
            self.db.commit()
            return state
        except BaseException:
            self.db.rollback()
            raise

    def publish(self, run_id, case_id, expected_version, confirmed, deidentified, case):
        self.doctor()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result = self._publish(run_id, case_id, expected_version, confirmed, deidentified, case)
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def _publish(self, run_id, case_id, expected_version, confirmed, deidentified, case):
        self.doctor()
        run = self.run(run_id)
        if not run["doctor_review"] or run["status"] != "doctor_reviewed" or confirmed is not True or deidentified is not True:
            fail("publication_not_authorized", "Doctor review and explicit deidentification/authorization confirmation are required")
        validate(case_id, text())
        validate(expected_version, {"type": "integer", "minimum": 0})
        validate(case, obj({"phenotype": strings(), "context": strings(), "evidence": strings(),
                           "experience": text(), "pitfalls": strings(), "outcome": text(), "source_ref": text(),
                           "image_ids": strings(), "regions": array(REGION, 6), "redactions": array(REGION, 10)},
                          ("phenotype", "context", "evidence", "experience", "pitfalls", "outcome", "source_ref", "image_ids", "regions")))
        if not case["phenotype"] or not case["image_ids"] or len(case["image_ids"]) > 3 or len(packed(case)) > 24000:
            fail("invalid_publication", "A bounded case needs phenotype and one to three reviewed images")
        originals = {x["image_id"] for x in self.state(run["encounter_id"])["images"]}
        if any(i not in originals for i in case["image_ids"]) or len(set(case["image_ids"])) != len(case["image_ids"]):
            fail("invalid_image", "Only current original patient images may be published")
        mapping, images = {}, []
        for image_id in case["image_ids"]:
            public_id = "shared-" + uuid.uuid4().hex
            data = self.image(run_id, image_id)
            with Image.open(io.BytesIO(data)) as im:
                for redaction in case.get("redactions", []):
                    check_output("visual", {"findings": [], "regions": [redaction], "quality_issues": [], "mask_sensitivity": []})
                    if redaction["image_id"] not in case["image_ids"]:
                        fail("invalid_redaction", "Redaction must refer to a selected image")
                    if redaction["image_id"] == image_id:
                        b = redaction["box"]
                        ImageDraw.Draw(im).rectangle((int(b[0]*im.width/1000), int(b[1]*im.height/1000), math.ceil(b[2]*im.width/1000)-1, math.ceil(b[3]*im.height/1000)-1), fill=(0, 0, 0))
                buf = io.BytesIO()
                im.save(buf, format="PNG")
            asset = self._save_image(base64.b64encode(buf.getvalue()).decode(), self.assets / "shared", public_id)
            mapping[image_id] = public_id
            images.append(asset)
        regions = []
        for region in case["regions"]:
            check_output("visual", {"findings": [], "regions": [region], "quality_issues": [], "mask_sensitivity": []})
            if region["image_id"] not in mapping:
                fail("invalid_region", "Published regions must belong to the selected images")
            regions.append({**region, "image_id": mapping[region["image_id"]]})
        value = {k: v for k, v in case.items() if k not in ("image_ids", "regions", "redactions")}
        value.update({"case_id": case_id, "version": expected_version + 1, "domain": self.domain,
                      "images": images, "regions": regions, "review_status": "doctor_confirmed"})
        self.shared.execute("BEGIN IMMEDIATE")
        try:
            previous = self.shared.execute("SELECT version,domain FROM cases WHERE id=?", (case_id,)).fetchone()
            if previous and previous["domain"] != self.domain:
                fail("domain_conflict", "Case id belongs to another disease profile")
            if (previous["version"] if previous else 0) != expected_version:
                fail("version_conflict", "Shared case version changed")
            self.shared.execute("INSERT INTO cases(id,version,domain,payload) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,domain=excluded.domain,payload=excluded.payload,helpful=0,unhelpful=0,exposures=0", (case_id, value["version"], self.domain, packed(value)))
            self.shared.execute("INSERT INTO case_versions VALUES(?,?,?)", (case_id, value["version"], packed(value)))
            self.shared.commit()
        except BaseException:
            self.shared.rollback()
            raise
        return value

    def feedback(self, run_id, case_id, version, kind, reason):
        self.doctor()
        validate(version, {"type": "integer", "minimum": 1})
        validate(reason, text())
        if kind not in ("helpful", "not_helpful"):
            fail("invalid_feedback", "Only observed helpful/not_helpful feedback changes utility")
        run = self.run(run_id, current=False)
        if not run["doctor_review"]:
            fail("review_required", "Doctor must review the consultation first")
        if not any(c["case_id"] == case_id and c["version"] == version for r in run["retrievals"] for c in r["cases"]):
            fail("invalid_reference", "Case version was not delivered in this consultation")
        self.shared.execute("BEGIN IMMEDIATE")
        try:
            row = self.shared.execute("SELECT feedback FROM deliveries WHERE run=? AND id=? AND version=?", (run_id, case_id, version)).fetchone()
            if not row:
                fail("invalid_reference", "Missing delivery receipt")
            if row["feedback"]:
                old = json.loads(row["feedback"])
                if old != {"kind": kind, "reason": reason}:
                    fail("feedback_conflict", "One outcome per case version per consultation")
                self.shared.commit()
                return {"recorded": False, "duplicate": True}
            self.shared.execute("UPDATE deliveries SET feedback=? WHERE run=? AND id=? AND version=?", (packed({"kind": kind, "reason": reason}), run_id, case_id, version))
            column = "helpful" if kind == "helpful" else "unhelpful"
            self.shared.execute(f"UPDATE cases SET {column}={column}+1 WHERE id=? AND version=?", (case_id, version))
            self.shared.commit()
            return {"recorded": True}
        except BaseException:
            self.shared.rollback()
            raise
