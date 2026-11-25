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
import sqlite3
import time
import uuid
from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError
from schemas import MemoryError, validate, obj, text, strings, PHENOTYPE
from extensions import reference
from retrieval import descriptor_text, known
from case_library import CaseLibrary, record_id as published_record_id
from store import BUSINESS_CATEGORIES
from team_schemas import OUTPUTS, check_output, REGION, array
from workflow import PatientCommunicationAgent
from clinical_control import ClinicalControlMixin, initialize, stage_value, select_diverse

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 20_000_000


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


def fail(code, message):
    raise MemoryError(code, message)


class MedicalProject(ClinicalControlMixin):
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
        try:
            self.top_k = int(os.environ.get("MEDICAL_TOP_K", "5"))
            self.candidate_limit = int(os.environ.get("MEDICAL_CANDIDATE_LIMIT", "12"))
            if not 1 <= self.top_k <= 12 or not self.top_k <= self.candidate_limit <= 12:
                raise ValueError()
        except ValueError:
            fail("invalid_config", "Use 1 <= MEDICAL_TOP_K <= MEDICAL_CANDIDATE_LIMIT <= 12")
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
          CREATE TABLE IF NOT EXISTS medical_case_feedback (
            patient TEXT NOT NULL, run TEXT NOT NULL, scope TEXT NOT NULL,
            category TEXT NOT NULL, record_id TEXT NOT NULL, version INTEGER NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(patient,run,scope,category,record_id,version));
          CREATE TABLE IF NOT EXISTS medical_events (
            patient TEXT NOT NULL, event TEXT NOT NULL, hash TEXT NOT NULL,
            response TEXT NOT NULL, PRIMARY KEY(patient,event));
        ''')
        self.library = CaseLibrary(self.shared_path, self.domain, self.role, self.store.settings)
        self.shared = self.library.db  # compatibility: published headers remain in this database
        initialize(self.db)
        initialize(self.shared)

    def close(self):
        self.library.close()

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
        if current and "memory_epoch" in run and run["memory_epoch"] != self._memory_epoch():
            fail("stale_revision", "Original patient memory changed; start a fresh consultation")
        return run

    def _memory_epoch(self):
        clinical = self.db.execute("SELECT COALESCE(MAX(seq),0) FROM revisions WHERE patient_id=?", (self.patient,)).fetchone()[0]
        visual = self.db.execute("SELECT COALESCE(MAX(seq),0) FROM memory_visual_revisions WHERE patient_id=?", (self.patient,)).fetchone()[0]
        return [clinical, visual]

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
                   "revision": revision, "legacy_revision": state["legacy_revision"], "domain": self.domain, "status": "running", "outputs": [], "retrievals": [], "views": [], "doctor_review": None, "input": dict(state), "memory_epoch": self._memory_epoch(), "memory_bindings": []}
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
                "current_memories": {category: [r for r in self.store.records(category) if r["encounter_id"] == run["encounter_id"]] for category in BUSINESS_CATEGORIES},
                "doctor_notes": [r for r in self.store.records("doctor") if r["encounter_id"] == run["encounter_id"]][-10:],
                "reusable_lessons": self.lessons(" ".join(m["text"] for m in run["input"]["messages"])),
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
            if role == "patient":
                self._bind_originals(run, value)
            run["outputs"].append({"role": role, "step": len(run["outputs"]) + 1, "value": value})
            if role == "retrieval":
                initial = run["retrievals"][-1]
                by_id = {(c["case_id"], c["version"]): c for c in initial["cases"]}
                ranking = [{**r, "model_score": r["relevance"], "decay_score": by_id[(r["case_id"], r["version"])]["decay_score"],
                            "final_score": round(r["relevance"] * by_id[(r["case_id"], r["version"])]["decay_score"], 10)} for r in value["ranking"]]
                ranking.sort(key=lambda r: (-r["final_score"], -r["model_score"], r["case_id"], r["version"]))
                scored = {(r["case_id"], r["version"]) for r in ranking}
                selected, duplicate_cases = select_diverse(ranking, initial["cases"], self.top_k)
                run.setdefault("rerankings", []).append({"stage": "model_reranking", "retrieval_id": initial["retrieval_id"],
                    "key": initial["key"], "diversity_omissions": duplicate_cases, "ranking": ranking, "selected": selected, "top_k": self.top_k,
                    "score_rule": "final_score = temporary_model_score * iteration_decay_score",
                    "unscored": [{"case_id": c["case_id"], "version": c["version"]} for c in initial["cases"] if (c["case_id"], c["version"]) not in scored]})
            if role == "visual":
                run["visual_gains"] = self.visual_gains(run, value)
            run["memory_epoch"] = self._memory_epoch()
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
                selected = {(r["case_id"], r["version"]) for ranking in run.get("rerankings", []) if ranking["key"] == "key1" for r in ranking["selected"]}
                for source in region.get("source_cases", []):
                    if (source["case_id"], source["version"]) not in selected:
                        fail("invalid_reference", "Region source must be a selected visual-stage case")
                    historical = next(c for r in run["retrievals"] for c in r["cases"] if c["case_id"] == source["case_id"] and c["version"] == source["version"])
                    if not historical.get("regions"):
                        fail("invalid_reference", "Case has no historical mask location to transfer semantically")
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
        if role == "retrieval":
            delivered = {(c["case_id"], c["version"]) for c in run["retrievals"][-1]["cases"]} if run["retrievals"] else set()
        if role == "reasoning" and run.get("rerankings"):
            delivered = {(r["case_id"], r["version"]) for r in run["rerankings"][-1]["selected"]}
        if role in ("retrieval", "reasoning"):
            refs = value["ranking"] if role == "retrieval" else value["case_refs"]
            pairs = [(x["case_id"], x["version"]) for x in refs]
            if role == "retrieval" and set(pairs) != delivered:
                fail("incomplete_reranking", "Every current candidate needs a fresh temporary model score, including zero for unsuitable cases")
            if len(set(pairs)) != len(pairs) or any(pair not in delivered for pair in pairs):
                fail("invalid_reference", "Only distinct delivered case versions may be cited")
        if role == "reasoning":
            known = {"patient"} | {x["id"] for x in (self.latest(run, "visual") or {"findings": []})["findings"]}
            for hyp in value["hypotheses"]:
                if any(x not in known for x in hyp["support_ids"] + hyp["against_ids"]):
                    fail("invalid_evidence", "Hypothesis refers to an unknown observation")

    def latest(self, run, role):
        return next((x["value"] for x in reversed(run["outputs"]) if x["role"] == role), None)

    def _bind_originals(self, run, patient):
        """Attach images to exact original memory versions, never replace clinical facts."""
        state = self.state(run["encounter_id"])
        current = [r for r in self.store.records("lesion") if r["encounter_id"] == run["encounter_id"]]
        requested = {b["image_id"]: b["reference"] for b in patient.get("image_bindings", [])}
        if len(requested) != len(patient.get("image_bindings", [])) or set(requested) - {i["image_id"] for i in state["images"]}:
            fail("invalid_binding", "Each current image may have one explicit lesion binding")
        saved = {b["image_id"]: b["reference"] for b in state.get("memory_bindings", [])}
        bindings = []
        for image in state["images"]:
            ref = requested.get(image["image_id"]) or saved.get(image["image_id"])
            if ref:
                if ref["category"] != "lesion":
                    fail("invalid_binding", "Original images must reference a lesion observation")
                record = self.store.get({"category": "lesion", "record_id": ref["record_id"], "include_versions": False})["record"]
                if record["encounter_id"] != run["encounter_id"] or (image["image_id"] in requested and record["version"] != ref["version"]):
                    fail("invalid_binding", "Image binding requires a current observation from this encounter")
                ref = reference(record)
            elif len(current) == 1 and len(state["images"]) == 1:
                ref = reference(current[0])
            else:
                rid = "visual-observation-" + digest([run["encounter_id"], image["image_id"]])[:40]
                existing = next((r for r in current if r["record_id"] == rid), None)
                if existing:
                    ref = reference(existing)
                else:
                    # This is an unreviewed observation identity, not a confirmed diagnosis or a lesion count.
                    features = {key: [] if key == "tooth_positions" else "unknown" for key in PHENOTYPE}
                    if len(state["images"]) == 1:
                        features.update({k: v for k, v in patient.get("features", {}).items() if k in PHENOTYPE})
                    record = {"record_id": rid, "encounter_id": run["encounter_id"], "event_time": "unknown",
                              "source_ref": "medical-input:" + run["run_id"], "source": "model", "review_status": "unreviewed",
                              "lesion_id": "unreviewed:" + rid, "features": features, "size_description": "unknown",
                              "pathology": {"status": "unknown", "result": "", "source_ref": ""}}
                    created = self.store.put("lesion", {"record": record, "expected_version": 0})["record"]
                    ref = reference(created)
            linked = self.store.get({"category": "lesion", "record_id": ref["record_id"], "include_versions": False})["record"]["visual"]
            if not any(v["image"]["image_id"] == image["image_id"] and not v["stale"] for v in linked):
                self.store.link_visual(ref, image, [], [], run["run_id"], "admit:" + digest([run["run_id"], image["image_id"], ref]))
            bindings.append({"image_id": image["image_id"], "reference": ref})
        run["memory_bindings"] = bindings
        state["memory_bindings"] = bindings
        self.db.execute("UPDATE medical_cases SET payload=? WHERE patient=? AND encounter=?", (packed(state), self.patient, run["encounter_id"]))

    def _sync_visual(self, run):
        visual = self.latest(run, "visual")
        if not visual:
            fail("missing_visual", "Key2 requires grounded visual observations")
        images = {i["image_id"]: i for i in run["input"]["images"]}
        for binding in run.get("memory_bindings", []):
            image_id = binding["image_id"]
            self.store.link_visual(binding["reference"], images[image_id],
                [r for r in visual["regions"] if r["image_id"] == image_id],
                [f for f in visual["findings"] if f["image_id"] == image_id], run["run_id"],
                "evidence:" + digest([run["run_id"], binding, visual]))
        run["memory_epoch"] = self._memory_epoch()

    def _query(self, run, key, extra):
        patient = self.latest(run, "patient")
        if not patient:
            fail("missing_intake", "Patient assessment must precede retrieval")
        history = patient.get("patient_experiences", patient.get("history", []))
        history_text = " ".join(history).strip()
        if not history_text:
            history_text = " ".join(r["description"] + " " + r.get("outcome", "") for r in self.store.records("patient_experience") if r["encounter_id"] == run["encounter_id"])
        features = {"text": "unknown"}
        if key == "key2":
            features = dict(patient.get("features", {}))
            descriptions = list(patient["phenotype"])
            visual = self.latest(run, "visual")
            views = {v["view_id"]: v for v in run["views"]}
            for finding in visual["findings"]:
                if finding["certainty"] == "clear" and any(v == finding["image_id"] or views.get(v, {}).get("kind") == "crop" for v in finding["supporting_views"]):
                    descriptions.extend([finding["description"], *finding["tags"]])
            if extra:
                validate(extra, text())
                descriptions.append(extra)
            combined = " ".join([features.get("text", ""), *descriptions]).strip()
            if combined:
                features["text"] = combined
            if not features:
                features = {"text": "unknown"}
        excluded = [r["record_id"] for category in ("lesion", "experience", "care", "doctor")
                    for r in self.store.records(category) if r["encounter_id"] == run["encounter_id"]]
        if len(excluded) > 100:
            fail("context_limit", "Current encounter exceeds the bounded exclusion budget")
        return {"purpose": "visual" if key == "key1" else "diagnosis", "patient_experience_query": history_text,
                "lesion_query": features, "care_query": "", "experience_query": "", "doctor_query": "",
                "exclude_record_ids": excluded, "top_k": self.candidate_limit}

    def _candidate(self, item, result):
        lesion = item["lesion"]
        scope = lesion.get("memory_scope", "patient")
        ref = reference(lesion)
        packet = next(p for p in result["source_packets"] if p["scope"] == scope)
        if scope == "library":
            case = self.library.published(lesion["case_id"], lesion["version"])
            identifier = case["case_id"]
        else:
            identifier = "personal:" + digest([self.patient, lesion["record_id"]] if lesion["category"] == "lesion" else [self.patient, lesion["category"], lesion["record_id"]])[:32]
            experience = [e["record"]["description"] + " " + e["record"].get("outcome", "") for e in item.get("patient_experience", [])]
            notes = [m["record"]["description"] for m in result["supplementary"]["doctor"] if m["record"].get("patient_id") == self.patient]
            case = {"case_id": identifier, "version": lesion["version"], "domain": self.domain,
                    "phenotype": [descriptor_text(lesion)] if lesion["category"] == "lesion" else [], "context": experience,
                    "evidence": ["来源：" + lesion["source_ref"], *notes],
                    "experience": " ".join(c["record"]["description"] for c in item["care"]),
                    "pitfalls": [], "outcome": " ".join(c["record"]["outcome"] for c in item["care"]),
                    "source_ref": lesion["source_ref"], "review_status": lesion["review_status"],
                    "images": [v["image"] for v in lesion.get("visual", [])],
                    "regions": [r for v in lesion.get("visual", []) for r in v["regions"]]}
        return {**case, "case_id": identifier, "memory_scope": scope, "memory_reference": ref,
                "memory_retrieval_id": packet["retrieval_id"], "lesion_id": lesion.get("lesion_id", ""),
                "patient_experience": item.get("patient_experience", []), "patient_experience_score": item.get("patient_experience_score", 0),
                "retrieval_channels": result.get("retrieval_channels", []),
                "phenotype_score": item["phenotype_score"], "care_score": item["care_score"],
                "history_score": item.get("patient_experience_score", item["care_score"]), "utility": lesion["stats"]["utility_score"],
                "decay_score": lesion["stats"]["decay_score"], "frequency": lesion["stats"]["frequency"], "exposures": lesion["stats"]["exposure_count"],
                "associated_care": item["care"], "visual_annotations": lesion.get("visual", [])}

    def retrieve(self, run_id, key, query=None):
        if key not in ("key1", "key2"):
            fail("invalid_key", "Use key1 or key2")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            run = self.run(run_id)
            if run["status"] != "running" or len(run["retrievals"]) >= 6:
                fail("retrieval_limit", "Retrieval budget exhausted or run closed")
            if key == "key2":
                self._sync_visual(run)
            arguments = self._query(run, key, query)
            # One global initial ranking by the original engine. The retrieval Agent reranks afterward.
            result = self.store.retrieve(arguments, sources=(self.library,), initial_only=True, consultation_id=run_id)
            fresh = [self._candidate(item, result) for item in result["candidates"]]
            merged = {c["case_id"]: dict(c) for c in (run["retrievals"][-1]["cases"] if key == "key2" and run["retrievals"] else [])}
            for c in fresh:
                if c["case_id"] not in merged or c["version"] >= merged[c["case_id"]]["version"]:
                    merged[c["case_id"]] = c
            # Rescore the UNION, refreshing D once per stage. Never carry an earlier model score forward.
            cases = []
            for c in merged.values():
                target = self.store if c["memory_scope"] == "patient" else self.library
                current = target.get({"category": c["memory_reference"]["category"], "record_id": c["memory_reference"]["record_id"], "include_versions": False})["record"]
                if current["version"] == c["version"]:
                    cases.append(c)
            for c in cases:
                target = self.store if c["memory_scope"] == "patient" else self.library
                c["stage_values"] = {stage: stage_value(target, c["memory_reference"], stage) for stage in ("visual", "diagnosis")}
                c["decay_score"] = c["stage_values"][arguments["purpose"]]
            response = {"retrieval_id": result["retrieval_id"], "key": key, "query": arguments,
                        "cases": cases, "initial_candidates": [{"case_id": c["case_id"], "version": c["version"]} for c in fresh], "stage": "initial_retrieval", "status": result["status"], "required_context": result["required_context"],
                        "supplementary": result["supplementary"], "source_packets": result["source_packets"],
                        "searched_entire_domain": True, "retrieval_channels": result.get("retrieval_channels", []),
                        "scoring": "initial: patient experience only; model reranking: M * D, then Top-K" if key == "key1" else "initial: lesion description + patient experience; model reranking: M * D, then Top-K",
                        "ranking": result["ranking"], "algorithm": result["algorithm"] if "algorithm" in result else "original hierarchical memory retrieval"}
            run["retrievals"].append(response)
            self._write_run(run)
            self.shared.commit()
            self.db.commit()
            return response
        except BaseException:
            self.shared.rollback()
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
                                "basis": region["basis"], "source_cases": region.get("source_cases", []), "asset": asset}
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
        historical_private = {x["image_id"] for r in run["retrievals"] for c in r["cases"]
                              if c.get("memory_scope") == "patient" for x in c["images"]}
        if image_id in private | historical_private:
            path = self._private_dir() / (image_id + ".png")
        else:
            delivered = {x["image_id"] for r in run["retrievals"] for c in r["cases"] if c.get("memory_scope", "library") == "library" for x in c["images"]}
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

    def _doctor_memory(self, encounter, identifier, description, note_type):
        note = {"record_id": "medical-" + digest(identifier)[:40], "encounter_id": encounter,
                "event_time": str(int(time.time())), "source_ref": identifier, "source": "doctor",
                "review_status": "doctor_confirmed", "lesion_ids": [], "note_type": note_type,
                "description": description, "related_record_ids": [], "question_status": "not_applicable", "answer": ""}
        self.store.put("doctor", {"record": note, "expected_version": 0})

    def review(self, run_id, expected_revision, confirmed, note, outcome, success=None):
        self.doctor()
        validate(expected_revision, {"type": "integer", "minimum": 1})
        validate(note, text())
        validate(outcome, text())
        if success is not None:
            validate(success, {"type": "boolean"})
        if confirmed is not True:
            fail("confirmation_required", "Explicit doctor confirmation required")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            run = self.run(run_id)
            if run["revision"] != expected_revision or run["status"] not in ("awaiting_doctor", "needs_input", "needs_human"):
                fail("version_conflict", "Review a completed current consultation")
            run["doctor_review"] = {"success": success, "note": note, "outcome": outcome, "time": int(time.time()), "revision": expected_revision}
            run["status"] = "doctor_reviewed"
            self._doctor_memory(run["encounter_id"], "review:" + run_id, note + "；医生确认结果：" + outcome, "supplement")
            run["memory_epoch"] = self._memory_epoch()
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
            self._doctor_memory(encounter_id, "correction:" + event_id, note, "correction")
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
        if case_id.startswith("personal:"):
            fail("invalid_case_id", "personal: is reserved for original patient memory identities")
        validate(expected_version, {"type": "integer", "minimum": 0})
        validate(case, obj({"phenotype": strings(), "context": strings(), "evidence": strings(),
                           "experience": text(), "pitfalls": strings(), "outcome": text(), "source_ref": text(),
                           "image_ids": strings(), "regions": array(REGION, 6), "redactions": array(REGION, 10), "features": obj(PHENOTYPE, PHENOTYPE)},
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
            regions.append({**{k: v for k, v in region.items() if k != "source_cases"}, "image_id": mapping[region["image_id"]]})
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
            self.shared.execute("INSERT INTO cases(id,version,domain,payload) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,domain=excluded.domain,payload=excluded.payload", (case_id, value["version"], self.domain, packed(value)))
            self.shared.execute("INSERT INTO case_versions VALUES(?,?,?)", (case_id, value["version"], packed(value)))
            self.library.index_case(value)
            self.shared.commit()
        except BaseException:
            self.shared.rollback()
            raise
        return value

    def feedback(self, run_id, case_id, version, kind, reason):
        """Route a case verdict to the original versioned memory_feedback mechanism."""
        self.doctor()
        validate(version, {"type": "integer", "minimum": 1}); validate(reason, text())
        if kind not in ("helpful", "not_helpful"):
            fail("invalid_feedback", "Only observed helpful/not_helpful feedback changes utility")
        run = self.run(run_id, current=False)
        candidate = next((c for r in run["retrievals"] for c in r["cases"] if c["case_id"] == case_id and c["version"] == version), None)
        if candidate is None:
            fail("invalid_reference", "Case version was not delivered in this consultation")
        scope = candidate.get("memory_scope", "library")
        ref = candidate.get("memory_reference") or {"category": "lesion", "record_id": published_record_id(case_id, self.domain, "lesion"), "version": version}
        receipt = candidate.get("memory_retrieval_id") or self.library.legacy_receipt(run_id, case_id, version)
        target = self.store if scope == "patient" else self.library
        payload = packed({"kind": kind, "reason": reason})
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute('''SELECT payload FROM medical_case_feedback WHERE patient=? AND run=? AND scope=? AND category=? AND record_id=? AND version=?''',
                (self.patient, run_id, scope, ref["category"], ref["record_id"], ref["version"])).fetchone()
            if old:
                if old["payload"] != payload:
                    fail("feedback_conflict", "One outcome per original memory version per consultation")
                self.db.commit()
                return {"recorded": False, "duplicate": True, "reference": ref, "memory_scope": scope}
            # Legacy feedback rows were migrated without incrementing their archived counters again.
            legacy = target.db.execute('''SELECT kind,reason FROM memory_feedback WHERE patient_id=? AND retrieval_id=?
                AND category=? AND record_id=? AND version=? AND kind!='used' LIMIT 1''',
                (target.patient_id, receipt, ref["category"], ref["record_id"], ref["version"])).fetchone()
            if legacy and (legacy["kind"] != kind or legacy["reason"] != reason):
                fail("feedback_conflict", "This delivered memory already has a verdict")
            response = target.memory_feedback({"event_id": "team:" + digest([run_id, scope, ref]),
                "retrieval_id": receipt, "reference": ref, "kind": kind, "reason": reason})
            self.db.execute("INSERT INTO medical_case_feedback VALUES(?,?,?,?,?,?,?)", (self.patient, run_id, scope, ref["category"], ref["record_id"], ref["version"], payload))
            self.db.commit()
            return {"recorded": not response["idempotent"], "duplicate": response["idempotent"], "reference": ref, "memory_scope": scope}
        except BaseException:
            self.db.rollback()
            raise
